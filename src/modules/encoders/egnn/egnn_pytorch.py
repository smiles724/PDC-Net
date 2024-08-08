import torch
from einops import rearrange, repeat
from torch import nn, einsum, broadcast_tensors


# helper functions

def exists(val):
    return val is not None


def safe_div(num, den, eps=1e-8):
    res = num.div(den.clamp(min=eps))
    res.masked_fill_(den == 0, 0.)
    return res


def batched_index_select(values, indices, dim=1):
    value_dims = values.shape[(dim + 1):]
    values_shape, indices_shape = map(lambda t: list(t.shape), (values, indices))
    indices = indices[(..., *((None,) * len(value_dims)))]
    indices = indices.expand(*((-1,) * len(indices_shape)), *value_dims)
    value_expand_len = len(indices_shape) - (dim + 1)
    values = values[(*((slice(None),) * dim), *((None,) * value_expand_len), ...)]

    value_expand_shape = [-1] * len(values.shape)
    expand_slice = slice(dim, (dim + value_expand_len))
    value_expand_shape[expand_slice] = indices.shape[expand_slice]
    values = values.expand(*value_expand_shape)

    dim += value_expand_len
    return values.gather(dim, indices)


def fourier_encode_dist(x, num_encodings=4, include_self=True):
    x = x.unsqueeze(-1)
    device, dtype, orig_x = x.device, x.dtype, x
    scales = 2 ** torch.arange(num_encodings, device=device, dtype=dtype)
    x = x / scales
    x = torch.cat([x.sin(), x.cos()], dim=-1)
    x = torch.cat((x, orig_x), dim=-1) if include_self else x
    return x


def embedd_token(x, dims, layers):
    stop_concat = -len(dims)
    to_embedd = x[:, stop_concat:].long()
    for i, emb_layer in enumerate(layers):
        # the portion corresponding to `to_embedd` part gets dropped
        x = torch.cat([x[:, :stop_concat], emb_layer(to_embedd[:, i])], dim=-1)
        stop_concat = x.shape[-1]
    return x


# swish activation fallback
class Swish_(nn.Module):
    def forward(self, x):
        return x * x.sigmoid()


SiLU = nn.SiLU if hasattr(nn, 'SiLU') else Swish_


# helper classes

# this follows the same strategy for normalization as done in SE3 Transformers
# https://github.com/lucidrains/se3-transformer-pytorch/blob/main/se3_transformer_pytorch/se3_transformer_pytorch.py#L95

class CoorsNorm(nn.Module):
    def __init__(self, eps=1e-8, scale_init=1.):
        super().__init__()
        self.eps = eps
        scale = torch.zeros(1).fill_(scale_init)
        self.scale = nn.Parameter(scale)

    def forward(self, coors):
        norm = coors.norm(dim=-1, keepdim=True)
        normed_coors = coors / norm.clamp(min=self.eps)
        return normed_coors * self.scale


# global linear attention

class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64):
        super().__init__()
        inner_dim = heads * dim_head
        self.heads = heads
        self.scale = dim_head ** -0.5

        self.to_q = nn.Linear(dim, inner_dim, bias=False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias=False)
        self.to_out = nn.Linear(inner_dim, dim)

    def forward(self, x, context, mask=None):
        h = self.heads

        q = self.to_q(x)
        kv = self.to_kv(context).chunk(2, dim=-1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, *kv))
        dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

        if exists(mask):
            mask_value = -torch.finfo(dots.dtype).max
            mask = rearrange(mask, 'b n -> b () () n')
            dots.masked_fill_(~mask, mask_value)

        attn = dots.softmax(dim=-1)
        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)', h=h)
        return self.to_out(out)


class GlobalLinearAttention(nn.Module):
    def __init__(self, *, dim, heads=8, dim_head=64):
        super().__init__()
        self.norm_seq = nn.LayerNorm(dim)
        self.norm_queries = nn.LayerNorm(dim)
        self.attn1 = Attention(dim, heads, dim_head)
        self.attn2 = Attention(dim, heads, dim_head)

        self.ff = nn.Sequential(nn.LayerNorm(dim), nn.Linear(dim, dim * 4), nn.GELU(), nn.Linear(dim * 4, dim))

    def forward(self, x, queries, mask=None):
        res_x, res_queries = x, queries
        x, queries = self.norm_seq(x), self.norm_queries(queries)

        induced = self.attn1(queries, x, mask=mask)
        out = self.attn2(x, induced)

        x = out + res_x
        queries = induced + res_queries

        x = self.ff(x) + x
        return x, queries


class EGNN(nn.Module):
    def __init__(self, dim, edge_dim=0, m_dim=16, fourier_features=0, num_nearest_neighbors=0, dropout=0.0, init_eps=1e-3, norm_feats=False, norm_coors=False,
                 norm_coors_scale_init=1e-2, update_feats=True, update_coors_mean=True, update_coors_var=True, only_sparse_neighbors=False, valid_radius=float('inf'),
                 m_pool_method='sum', soft_edges=False, coor_weights_clamp_value=None, distri_input=True):
        super().__init__()
        assert m_pool_method in {'sum', 'mean'}, 'pool method must be either sum or mean'
        assert update_feats or update_coors_mean or update_coors_var, 'you must update either features, coordinates mean or variance, or all'

        self.distri_input = distri_input  # coordinates of input 3D graph are dosy
        self.fourier_features = fourier_features

        edge_input_dim = (fourier_features * 2) + (dim * 2) + edge_dim + 1 + 1
        dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()

        self.edge_mlp = nn.Sequential(nn.Linear(edge_input_dim, edge_input_dim * 2), dropout, SiLU(), nn.Linear(edge_input_dim * 2, m_dim), SiLU())
        self.edge_gate = nn.Sequential(nn.Linear(m_dim, 1), nn.Sigmoid()) if soft_edges else None

        self.node_norm = nn.LayerNorm(dim) if norm_feats else nn.Identity()
        self.coors_norm = CoorsNorm(scale_init=norm_coors_scale_init) if norm_coors else nn.Identity()

        self.m_pool_method = m_pool_method

        self.node_mlp = nn.Sequential(nn.Linear(dim + m_dim, dim * 2), dropout, SiLU(), nn.Linear(dim * 2, dim), ) if update_feats else None
        self.coors_mean_mlp = nn.Sequential(nn.Linear(m_dim, m_dim * 4), dropout, SiLU(), nn.Linear(m_dim * 4, 1)) if update_coors_mean else None
        self.coors_var_mlp = nn.Sequential(nn.Linear(m_dim, m_dim * 4), dropout, SiLU(), nn.Linear(m_dim * 4, 1)) if update_coors_var else None

        self.num_nearest_neighbors = num_nearest_neighbors
        self.only_sparse_neighbors = only_sparse_neighbors
        self.valid_radius = valid_radius

        self.coor_weights_clamp_value = coor_weights_clamp_value

        self.init_eps = init_eps
        self.apply(self.init_)

    def init_(self, module):
        if type(module) in {nn.Linear}:
            # seems to be needed to keep the network from exploding to NaN with greater depths
            nn.init.normal_(module.weight, std=self.init_eps)

    def forward(self, feats, coors_mean, coors_var, edges=None, mask=None, adj_mat=None, diagonal_var=True):
        b, n, d = feats.shape
        device, num_nearest, valid_radius, only_sparse_neighbors = feats.device, self.num_nearest_neighbors, self.valid_radius, self.only_sparse_neighbors

        use_nearest = num_nearest > 0 or only_sparse_neighbors

        if diagonal_var:
            rel_coors_var = rearrange(coors_var, 'b i d-> b i () d') + rearrange(coors_var, 'b j d -> b () j d')   # relative pos variance (B, N, N, 3)
        else:
            rel_coors_var = rearrange(coors_var, 'b i d k-> b i () d k') + rearrange(coors_var, 'b j d k -> b () j d k')   # relative pos variance (B, N, N, 3, 3)

        rel_coors_mean = rearrange(coors_mean, 'b i d -> b i () d') - rearrange(coors_mean, 'b j d -> b () j d')          # relative pos mean (B, N, N, 3)

        # compute the distribution of atomic distances
        rel_dist_sum = (rel_coors_mean ** 2).sum(dim=-1)  # (B, N, N)
        if diagonal_var:
            rel_coors_var_trace = rel_coors_var.sum(dim=-1)   # (B, N, N)
            rel_dist_std = 2 * rel_coors_var_trace + 4 * (rel_coors_mean ** 2 * rel_coors_var).sum(-1)   # (B, N, N)
        else:
            rel_coors_var_trace = rel_coors_var.diagonal(offset=0, dim1=-2, dim2=-1).sum(dim=-1)   # (B, N, N)
            rel_dist_std = 2 * rel_coors_var_trace + 4 * (rel_coors_mean.unsqueeze(-2) @ rel_coors_var @ rel_coors_mean.unsqueeze(-1)).squeeze(-1).squeeze(-1)   # (B, N, N)

        rel_dist_mean = rel_dist_sum + rel_coors_var_trace    # (B, N, N)

        if use_nearest:
            ranking = rel_dist_mean.clone()

            if exists(mask):
                rank_mask = mask[:, :, None] * mask[:, None, :]
                ranking.masked_fill_(~rank_mask, 1e5)

            if exists(adj_mat):
                if len(adj_mat.shape) == 2:
                    adj_mat = repeat(adj_mat.clone(), 'i j -> b i j', b=b)

                if only_sparse_neighbors:
                    num_nearest = int(adj_mat.float().sum(dim=-1).max().item())
                    valid_radius = 0

                self_mask = rearrange(torch.eye(n, device=device, dtype=torch.bool), 'i j -> () i j')

                adj_mat = adj_mat.masked_fill(self_mask, False)
                ranking.masked_fill_(self_mask, -1.)
                ranking.masked_fill_(adj_mat, 0.)

            nbhd_ranking, nbhd_indices = ranking.topk(num_nearest, dim=-1, largest=False)

            nbhd_mask = nbhd_ranking <= valid_radius

            rel_coors_mean = batched_index_select(rel_coors_mean, nbhd_indices, dim=2)
            rel_coors_var = batched_index_select(rel_coors_var, nbhd_indices, dim=2)
            rel_dist_mean = batched_index_select(rel_dist_mean, nbhd_indices, dim=2)
            rel_dist_std = batched_index_select(rel_dist_std, nbhd_indices, dim=2)

            if exists(edges):
                edges = batched_index_select(edges, nbhd_indices, dim=2)

        if not self.distri_input and self.fourier_features > 0:
            rel_dist_mean = fourier_encode_dist(rel_dist_mean, num_encodings=self.fourier_features)
            rel_dist_mean = rearrange(rel_dist_mean, 'b i j () d -> b i j d')    # (B, N, N, C)

        if use_nearest:
            feats_j = batched_index_select(feats, nbhd_indices, dim=1)
        else:
            feats_j = rearrange(feats, 'b j d -> b () j d')

        feats_i = rearrange(feats, 'b i d -> b i () d')
        feats_i, feats_j = broadcast_tensors(feats_i, feats_j)

        edge_input = torch.cat((feats_i, feats_j, rel_dist_mean.unsqueeze(-1), rel_dist_std.unsqueeze(-1)), dim=-1)       # distribution of distance (mean and var)

        if exists(edges):
            edge_input = torch.cat((edge_input, edges), dim=-1)

        m_ij = self.edge_mlp(edge_input)
        if exists(self.edge_gate):
            m_ij = m_ij * self.edge_gate(m_ij)

        if exists(mask):
            mask_i = rearrange(mask, 'b i -> b i ()')

            if use_nearest:
                mask_j = batched_index_select(mask, nbhd_indices, dim=1)
                mask = (mask_i * mask_j) & nbhd_mask
            else:
                mask_j = rearrange(mask, 'b j -> b () j')
                mask = mask_i * mask_j

        if exists(self.coors_mean_mlp):
            coor_mean_weights = self.coors_mean_mlp(m_ij).squeeze(-1)  # (B, N, N)
            rel_coors_mean = self.coors_norm(rel_coors_mean)

            if exists(mask):
                coor_mean_weights.masked_fill_(~mask, 0.)

            if exists(self.coor_weights_clamp_value):
                clamp_value = self.coor_weights_clamp_value
                coor_mean_weights.clamp_(min=-clamp_value, max=clamp_value)

            coors_mean_out = einsum('b i j, b i j c -> b i c', coor_mean_weights, rel_coors_mean) + coors_mean    # TODO: compute the mean instead of the sum
        else:
            coors_mean_out = coors_mean

        if exists(self.coors_var_mlp):
            coor_var_weights = self.coors_var_mlp(m_ij).squeeze(-1)
            rel_coors_var = self.coors_norm(rel_coors_var)
            if exists(mask):
                coor_var_weights.masked_fill_(~mask, 0.)

            if exists(self.coor_weights_clamp_value):
                clamp_value = self.coor_weights_clamp_value
                coor_var_weights.clamp_(min=-clamp_value, max=clamp_value)

            if diagonal_var:
                coors_var_out = einsum('b i j, b i j k -> b i k', coor_var_weights, rel_coors_var) + coors_var  # (B, N, N, 3)
            else:
                coors_var_out = einsum('b i j, b i j k c -> b i k c', coor_var_weights, rel_coors_var) + coors_var

        else:
            coors_var_out = coors_var

        if exists(self.node_mlp):
            if exists(mask):
                m_ij_mask = rearrange(mask, '... -> ... ()')
                m_ij = m_ij.masked_fill(~m_ij_mask, 0.)

            if self.m_pool_method == 'mean':
                if exists(mask):
                    # masked mean
                    mask_sum = m_ij_mask.sum(dim=-2)
                    m_i = safe_div(m_ij.sum(dim=-2), mask_sum)
                else:
                    m_i = m_ij.mean(dim=-2)

            elif self.m_pool_method == 'sum':
                m_i = m_ij.sum(dim=-2)

            normed_feats = self.node_norm(feats)
            node_mlp_input = torch.cat((normed_feats, m_i), dim=-1)
            node_out = self.node_mlp(node_mlp_input) + feats
        else:
            node_out = feats

        return node_out, coors_mean_out, coors_var_out


class EGNN_Network(nn.Module):
    def __init__(self, *, depth, dim, num_tokens=None, num_edge_tokens=None, num_positions=None, edge_dim=0, num_adj_degrees=None,
                 adj_dim=0, global_linear_attn_every=0, global_linear_attn_heads=8, global_linear_attn_dim_head=64, num_global_tokens=4, **kwargs):
        super().__init__()
        assert not (exists(num_adj_degrees) and num_adj_degrees < 1), 'make sure adjacent degrees is greater than 1'
        self.num_positions = num_positions

        self.token_emb = nn.Embedding(num_tokens, dim) if exists(num_tokens) else None
        self.pos_emb = nn.Embedding(num_positions, dim) if exists(num_positions) else None
        self.edge_emb = nn.Embedding(num_edge_tokens, edge_dim) if exists(num_edge_tokens) else None
        self.has_edges = edge_dim > 0

        self.num_adj_degrees = num_adj_degrees
        self.adj_emb = nn.Embedding(num_adj_degrees + 1, adj_dim) if exists(num_adj_degrees) and adj_dim > 0 else None

        edge_dim = edge_dim if self.has_edges else 0
        adj_dim = adj_dim if exists(num_adj_degrees) else 0

        has_global_attn = global_linear_attn_every > 0
        self.global_tokens = None
        if has_global_attn:
            self.global_tokens = nn.Parameter(torch.randn(num_global_tokens, dim), requires_grad=True)

        self.layers = nn.ModuleList([])
        for ind in range(depth):
            is_global_layer = has_global_attn and (ind % global_linear_attn_every) == 0

            self.layers.append(nn.ModuleList([GlobalLinearAttention(dim=dim, heads=global_linear_attn_heads, dim_head=global_linear_attn_dim_head) if is_global_layer else None,
                                              EGNN(dim=dim, edge_dim=(edge_dim + adj_dim), norm_feats=True, **kwargs), ]))

    def forward(self, feats, coors_mean, coors_var, adj_mat=None, edges=None, mask=None, return_coor_changes=False, diagonal_var=True, pos_change_flag=None):
        b, device = feats.shape[0], feats.device

        if exists(self.token_emb):
            feats = self.token_emb(feats)

        if exists(self.pos_emb):
            n = feats.shape[1]
            assert n <= self.num_positions, f'given sequence length {n} must be less than the number of positions {self.num_positions} set at init'
            pos_emb = self.pos_emb(torch.arange(n, device=device))
            feats += rearrange(pos_emb, 'n d -> () n d')

        if exists(edges) and exists(self.edge_emb):
            edges = self.edge_emb(edges)

        # create N-degrees adjacent matrix from 1st degree connections
        if exists(self.num_adj_degrees):
            assert exists(adj_mat), 'adjacency matrix must be passed in (keyword argument adj_mat)'

            if len(adj_mat.shape) == 2:
                adj_mat = repeat(adj_mat.clone(), 'i j -> b i j', b=b)

            adj_indices = adj_mat.clone().long()

            for ind in range(self.num_adj_degrees - 1):
                degree = ind + 2

                next_degree_adj_mat = (adj_mat.float() @ adj_mat.float()) > 0
                next_degree_mask = (next_degree_adj_mat.float() - adj_mat.float()).bool()
                adj_indices.masked_fill_(next_degree_mask, degree)
                adj_mat = next_degree_adj_mat.clone()

            if exists(self.adj_emb):
                adj_emb = self.adj_emb(adj_indices)
                edges = torch.cat((edges, adj_emb), dim=-1) if exists(edges) else adj_emb

        # setup global attention
        global_tokens = None
        if exists(self.global_tokens):
            global_tokens = repeat(self.global_tokens, 'n d -> b n d', b=b)

        # go through layers
        if return_coor_changes:
            coor_changes = [coors_mean]

        coors_mean_ = coors_mean.clone()
        for global_attn, egnn in self.layers:
            if exists(global_attn):
                feats, global_tokens = global_attn(feats, global_tokens, mask=mask)

            feats, coors_mean, coors_var = egnn(feats, coors_mean, coors_var, adj_mat=adj_mat, edges=edges, mask=mask, diagonal_var=diagonal_var)
            if pos_change_flag is not None:
                coors_mean[~pos_change_flag] = coors_mean_[~pos_change_flag]

            if return_coor_changes:
                coor_changes.append(coors_mean)

        if return_coor_changes:
            return feats, coors_mean, coors_var, coor_changes

        return feats, coors_mean, coors_var
