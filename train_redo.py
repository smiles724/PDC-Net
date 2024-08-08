import argparse
import os
import shutil

import torch.utils.tensorboard
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader
from tqdm import tqdm as tq
from tqdm.auto import tqdm

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

from src.utils.misc import inf_iterator, load_config, seed_all, get_logger, get_new_log_dir
from src.utils.data import PaddingCollate
from src.utils.train import *
from src.datasets.pdbredo_chain import get_pdbredo_chain_dataset

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=str)
    parser.add_argument('--logdir', type=str, default='./logs_pdc_redo')
    parser.add_argument('--debug', action='store_true', default=False)
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--resume', type=str, default=None)
    args = parser.parse_args()

    # Load configs
    config, config_name = load_config(args.config)
    seed_all(config.train.seed)

    # Logging
    if args.debug:
        logger = get_logger('train', None)
        writer = BlackHole()
    else:
        if args.resume:
            log_dir = get_new_log_dir(args.logdir, prefix='%s-resume' % config_name)
        else:
            log_dir = get_new_log_dir(args.logdir, prefix=config_name)
        ckpt_dir = os.path.join(log_dir, 'checkpoints')
        if not os.path.exists(ckpt_dir): os.makedirs(ckpt_dir)
        logger = get_logger('train', log_dir)
        writer = torch.utils.tensorboard.SummaryWriter(log_dir)
        tensorboard_trace_handler = torch.profiler.tensorboard_trace_handler(log_dir)
        if not os.path.exists(os.path.join(log_dir, os.path.basename(args.config))):
            shutil.copyfile(args.config, os.path.join(log_dir, os.path.basename(args.config)))
    logger.info(args)
    logger.info(config)

    # Data
    logger.info('Loading datasets...')
    train_dataset = get_pdbredo_chain_dataset('train', config.data, use_plm=config.model.use_plm)
    val_dataset = get_pdbredo_chain_dataset('val', config.data, use_plm=config.model.use_plm)
    train_loader = DataLoader(train_dataset, batch_size=config.train.batch_size, shuffle=True, collate_fn=PaddingCollate(), num_workers=args.num_workers)
    train_iterator = inf_iterator(train_loader)
    val_loader = DataLoader(val_dataset, batch_size=config.train.batch_size, shuffle=False, collate_fn=PaddingCollate(), num_workers=args.num_workers)
    logger.info('Train %d | Val %d' % (len(train_dataset), len(val_dataset)))

    # Model & Optimizer & Scheduler
    if config.model.checkpoint.path:
        from src.models.pdc import ProbabilityDensityCloud
        print(f'Loading pre-trained weights from {config.model.checkpoint.path}...')
        ckpt = torch.load(config.model.checkpoint.path, map_location='cpu')
        model = ProbabilityDensityCloud(ckpt['config'].model).to(args.device)
        model.load_state_dict(ckpt['model'])
        model.target = 'chi_angle'
    else:
        logger.info('Building model from scratch...')
        if 'type' in config.model and config.model.type == 'equiformer':
            from src.models.equiformer import EquiformerNet
            model = EquiformerNet(config.model).to(args.device)
        elif 'pos' in config.model:
            from src.models.pdc_ddg_refine import DDG_PDC_Network
            model = DDG_PDC_Network(config.model).to(args.device)      # use the same architecture as the downstream task
        else:
            from src.models.pdc import ProbabilityDensityCloud
            model = ProbabilityDensityCloud(config.model).to(args.device)
    logger.info('Number of parameters: %d M' % (count_parameters(model) / 1e6))
    optimizer = get_optimizer(config.train.optimizer, model)
    scheduler = get_scheduler(config.train.scheduler, optimizer)
    optimizer.zero_grad()
    it_first = 1

    if args.resume is not None:
        logger.info('Resuming from checkpoint: %s' % args.resume)
        ckpt = torch.load(args.resume, map_location=args.device)
        it_first = ckpt['iteration']  # + 1
        model.load_state_dict(ckpt['model'], )

    def train(it):
        model.train()

        batch = recursive_to(next(train_iterator), args.device)
        loss_dict = model(batch)
        loss = sum_weighted_losses(loss_dict, config.train.loss_weights)
        loss.backward()
        orig_grad_norm = clip_grad_norm_(model.parameters(), config.train.max_grad_norm)
        optimizer.step()
        optimizer.zero_grad()

        # Logging
        scalar_dict = {'grad': orig_grad_norm, 'lr(1e-4)': optimizer.param_groups[0]['lr'] * 1e4, }
        logstr = '[train] Iter %05d | loss %.2f' % (it, loss.item())
        for k, v in scalar_dict.items():
            logstr += ' | %s %.2f' % (k, v.item() if isinstance(v, torch.Tensor) else v)
        write_losses(loss, loss_dict, scalar_dict, it=it, tag='train', writer=writer)
        return logstr

    def validate(it):
        scalar_accum = ScalarMetricAccumulator()
        model.eval()
        with torch.no_grad():
            for i, batch in enumerate(tqdm(val_loader, desc='Validate', dynamic_ncols=True)):
                batch = recursive_to(batch, args.device)
                loss_dict = model(batch)
                loss = sum_weighted_losses(loss_dict, config.train.loss_weights)
                scalar_accum.add(name='loss', value=loss, batchsize=batch['size'], mode='mean')

        avg_loss = scalar_accum.get_average('loss')
        scalar_accum.log(it, 'val', logger=logger, writer=writer)
        write_losses(loss, loss_dict, {}, it=it, tag='val', writer=writer)

        if it != it_first:  # Don't step optimizers after resuming from checkpoint
            if config.train.scheduler.type == 'plateau':
                scheduler.step(avg_loss)
            else:
                scheduler.step()
        return avg_loss


    try:
        it_tqdm = tq(range(it_first, config.train.max_iters + 1))
        for it in it_tqdm:
            message = train(it)
            it_tqdm.set_description(message)

            if it % config.train.val_freq == 0:
                avg_val_loss = validate(it)
                if not args.debug:
                    ckpt_path = os.path.join(ckpt_dir, '%d.pt' % it)
                    torch.save({'config': config, 'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'iteration': it,
                                'avg_val_loss': avg_val_loss, }, ckpt_path)
    except KeyboardInterrupt:
        logger.info('Terminating...')
