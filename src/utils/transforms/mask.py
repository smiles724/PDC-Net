import math
import random

import torch
import torch.nn.functional as F

from ._base import _index_select_data, register_transform, _get_CB_positions


def _extend_mask(mask, chain_nb):
    """
    Args:
        mask, chain_nb: (L, ).
    """
    # Shift right
    mask_sr = torch.logical_and(F.pad(mask[:-1], pad=(1, 0), value=0), (F.pad(chain_nb[:-1], pad=(1, 0), value=-1) == chain_nb))
    # Shift left
    mask_sl = torch.logical_and(F.pad(mask[1:], pad=(0, 1), value=0), (F.pad(chain_nb[1:], pad=(0, 1), value=-1) == chain_nb))
    return torch.logical_or(mask, torch.logical_or(mask_sr, mask_sl))


def _mask_sidechains(pos_atoms, mask_atoms, mask_idx):
    """
    Args:
        pos_atoms:  (L, A, 3)
        mask_atoms: (L, A)
    """
    pos_atoms = pos_atoms.clone()
    pos_atoms[mask_idx, 4:] = 0.0  # mask atom to 0.0, not good

    mask_atoms = mask_atoms.clone()
    mask_atoms[mask_idx, 4:] = False
    return pos_atoms, mask_atoms


@register_transform('random_mask_amino_acids')
class RandomMaskAminoAcids(object):

    def __init__(self, mask_ratio_in_all=0.05, ratio_in_maskable_limit=0.5, mask_token=20, maskable_flag_attr='core_flag', extend_maskable_flag=False,
                 mask_ratio_mode='constant', ):
        super().__init__()
        self.mask_ratio_in_all = mask_ratio_in_all
        self.ratio_in_maskable_limit = ratio_in_maskable_limit
        self.mask_token = mask_token  # constant.AA - > 20 : UNK
        self.maskable_flag_attr = maskable_flag_attr
        self.extend_maskable_flag = extend_maskable_flag
        assert mask_ratio_mode in ('constant', 'random')
        self.mask_ratio_mode = mask_ratio_mode

    def __call__(self, data):
        if self.maskable_flag_attr is None:
            maskable_flag = torch.ones([data['aa'].size(0), ], dtype=torch.bool)
        else:
            maskable_flag = data[self.maskable_flag_attr]
            if self.extend_maskable_flag:
                maskable_flag = _extend_mask(maskable_flag, data['chain_nb'])

        num_masked_max = math.ceil(self.mask_ratio_in_all * data['aa'].size(0))
        if self.mask_ratio_mode == 'random':
            num_masked = random.randint(1, num_masked_max)
        else:
            num_masked = num_masked_max
        mask_idx = torch.multinomial(maskable_flag.float() / maskable_flag.sum(), num_samples=num_masked, )  # sample from the multinomial probability distribution
        mask_idx = mask_idx[:math.ceil(self.ratio_in_maskable_limit * maskable_flag.sum().item())]

        aa_masked = data['aa'].clone()
        aa_masked[mask_idx] = self.mask_token
        data['aa_true'] = data['aa']
        data['aa_masked'] = aa_masked

        data['pos_atoms'], data['mask_atoms'] = _mask_sidechains(data['pos_atoms'], data['mask_atoms'], mask_idx)

        return data


@register_transform('random_mask_pos_and_multiple_patch')
class RandomMasPositionAndFocusedMultiplePatch(object):

    def __init__(self, focus_attr, seed_nbh_size, patch_size, mask_ratio=0.05, mask_max_length=10, mask_noise_scale=1.0, num_patch=1):
        super().__init__()
        self.focus_attr = focus_attr
        self.seed_nbh_size = seed_nbh_size
        self.patch_size = patch_size
        self.num_patch = num_patch

        self.mask_ratio = mask_ratio
        self.mask_noise_scale = mask_noise_scale
        self.mask_max_length = mask_max_length

    def __call__(self, data):
        focus_flag = (data[self.focus_attr] > 0)  # (L, )
        if focus_flag.sum() < self.num_patch:  # If there is no enough active residues, randomly pick some.
            for i in random.sample(range(0, focus_flag.size(0) - 1), self.num_patch):
                focus_flag[i] = True

        num_masked = math.ceil(self.mask_ratio * data['aa'].size(0))
        num_masked = num_masked if num_masked < self.mask_max_length else self.mask_max_length

        l_r = num_masked // 2 + 1
        data['pos_gt'] = data['pos_atoms'].clone()
        data['pos_change_flag'] = torch.zeros_like(focus_flag).bool()
        mask_c_ids = torch.multinomial(focus_flag.float(), num_samples=self.num_patch, replacement=False)  # randomly select center positions
        mask_c_idx = mask_c_ids[0]

        # the atom order remains (important)
        if mask_c_idx - l_r >= 0 and mask_c_idx + l_r <= len(focus_flag) - 1:
            for i in range(mask_c_idx - l_r + 1, mask_c_idx + l_r):
                data['pos_atoms'][i] += torch.rand(data['pos_gt'][i].shape) * self.mask_noise_scale
                data['pos_change_flag'][i] = True

        else:
            l_idx = max(1, mask_c_idx - l_r + 1)
            r_idx = min(len(focus_flag) - 2, mask_c_idx + l_r - 1)
            delta_pos = (data['pos_atoms'][r_idx + 1] - data['pos_atoms'][l_idx - 1]) / (r_idx - l_idx + 1)
            for i in range(l_idx, r_idx + 1):
                data['pos_atoms'][i] = data['pos_atoms'][l_idx - 1] + (i - l_idx + 1) * delta_pos + torch.rand(data['pos_gt'][i].shape) * self.mask_noise_scale
                data['pos_change_flag'][i] = True

        for i in range(self.num_patch):
            if i > 0:
                mask_c_idx = mask_c_ids[i]
                assert focus_flag[mask_c_idx], f'Residue {mask_c_idx} is not focused. \n Selected: {mask_c_ids}\n {focus_flag}\n {focus_flag.sum()}'
                data['pos_atoms'] = data['pos_gt'].clone()  # use the ground truth coordinates

            pos_CB = _get_CB_positions(data['pos_atoms'], data['mask_atoms'])  # (L, )
            pos_c = pos_CB[mask_c_idx: mask_c_idx + 1]  # (1, )
            dist_from_seed = torch.cdist(pos_CB, pos_c)[:, 0]  # (L, 1) -> (L, )
            nbh_mask_center_idx = dist_from_seed.argsort()[:self.seed_nbh_size]  # (Nb, )

            core_idx = nbh_mask_center_idx[focus_flag[nbh_mask_center_idx]]  # (Ac, ), the core-set must be a subset of the focus-set
            dist_from_core = torch.cdist(pos_CB, pos_CB[core_idx]).min(dim=1)[0]  # (L, )

            patch_idx = dist_from_core.argsort()[:self.patch_size]  # (P, )
            patch_idx = patch_idx.sort()[0]

            if i == 0:
                data_patch = _index_select_data(data, patch_idx)
            else:
                tmp = _index_select_data(data, patch_idx)
                data_patch[f'patch_{i}'] = tmp

        return data_patch


@register_transform('mask_selected_amino_acids')
class MaskSelectedAminoAcids(object):

    def __init__(self, select_attr, mask_token=20):
        super().__init__()
        self.select_attr = select_attr
        self.mask_token = mask_token

    def __call__(self, data):
        mask_flag = (data[self.select_attr] > 0)

        aa_masked = data['aa'].clone()
        aa_masked[mask_flag] = self.mask_token
        data['aa_true'] = data['aa']
        data['aa_masked'] = aa_masked

        data['pos_atoms'], data['mask_atoms'] = _mask_sidechains(data['pos_atoms'], data['mask_atoms'], mask_flag)

        return data
