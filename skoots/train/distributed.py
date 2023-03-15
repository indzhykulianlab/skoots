import warnings
from functools import partial
from typing import Tuple, Callable, Dict
import os.path

from skoots.train.dataloader import dataset, MultiDataset, skeleton_colate
from skoots.train.sigma import Sigma
from skoots.train.loss import tversky, soft_dice_cldice
from skoots.train.merged_transform import merged_transform_3D, background_transform_3D
from skoots.train.engine import engine
from skoots.train.setup import setup_process, cleanup, find_free_port

from torch import Tensor
import torch.nn as nn
import torch.optim.lr_scheduler
import torch.multiprocessing as mp
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from lion_pytorch import Lion

"""
ASSUMPTIONS: 
    - Model predicts spatially accurate vector strengths. Ie. 2 in a vector is 60*(0.085nm)
    - Vector Scale Factor turns 1,1,1 -> 60 60 12
    - Sigma is the spatial correction factor, which can vary between XYZ. Sigma represents distance in PX space
    - Closest Skeleton must take into account SPATIAL distance, not px distance

PLACES WHICH HANDLE ANISOTROPY
    - skoots.train.generate_skeletons.calculate_skeletons also takes some anisotropy into account...
    - vec2embed handles anisotropy
    - sigma in embed2prob handles anisotropy
    - Baked Skeleton: When calculating the optimal skeleton location, it 
    
CURRENT STRATEGY
    - Baked Skeleton Anisotropy (1, 1, 1) # should be 1, 1, 5 in TRAIN mode but (1, 1, 1) in eval
    - Vec2Embed (60, 60, 12)  # ratio:(1, 1, 5)
    - Sigma (20, 20, 4)  # ratio:(1, 1, 5)
    
    
NEW STRATEGY (Oct 21):
    - These models seem to like embedding to the current Z slice more than up or down. Adjust bkaed
    skeleon anisotropy to (1, 1, 15) and see what happens
    - set skeleton loss start at epoch 100 (from 500) for pretrained models...
    - skeleton overlap loss causes skeletons to be phat
    - EVAL anisotropy param for bake skeletons should also probably be huge (1. 1. 15)
"""

torch.manual_seed(101196)


def train(rank: str,
          port: str,
          world_size: int,
          model: nn.Module,
          hyperparams,
          train_dir: str = '/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/data/unscaled/train',
          validation_dir: str = '/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/data/unscaled/validate',
          bacground_dir: str = '/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/data/background',
          vector_scale: Tuple[float, float, float] = (60, 60, 60 // 5),
          anisotropy: Tuple[float, float, float] = (1.0, 1.0, 3.0),
          ):
    setup_process(rank, world_size, port, backend='nccl')

    device = f'cuda:{rank}'

    model = model.to(device)
    model = torch.nn.parallel.DistributedDataParallel(model)
    # model = torch.compile(model)

    _ = model(torch.rand((1, 1, 300, 300, 20), device=device))

    augmentations: Callable[[Dict[str, Tensor]], Dict[str, Tensor]] = partial(merged_transform_3D,
                                                                              bake_skeleton_anisotropy=anisotropy,
                                                                              device=device)

    # Training Dataset - MultiDataset[Mitochondria, Background]
    data = dataset(path=train_dir,
                   transforms=augmentations,
                   sample_per_image=32,
                   device=device,
                   pad_size=10).to(device)

    print('data yes')

    # # data1 = dataset(path='/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/data/rutherford',
    # #                transforms=partial(merged_transform_3D, device=device), sample_per_image=32, device=device,
    # #                pad_size=100).to('cpu')
    #
    # data2 = dataset(path='/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/data/MitoR/train',
    #                 transforms=partial(merged_transform_3D, device=device), sample_per_image=32, device=device,
    #                 pad_size=100).to('cpu')

    background = dataset(path=bacground_dir,
                         transforms=partial(background_transform_3D, device=device), sample_per_image=6, device=device,
                         pad_size=100).to('cpu')

    merged = MultiDataset(data, background)

    train_sampler = torch.utils.data.distributed.DistributedSampler(merged)
    dataloader = DataLoader(merged, num_workers=0, batch_size=2, sampler=train_sampler, collate_fn=skeleton_colate)

    # Validation Dataset
    vl = dataset(path=validation_dir,
                 transforms=augmentations, device=device, sample_per_image=8,
                 pad_size=100).to(device)
    test_sampler = torch.utils.data.distributed.DistributedSampler(vl)
    valdiation_dataloader = DataLoader(vl, num_workers=0, batch_size=2, sampler=test_sampler,
                                       collate_fn=skeleton_colate)

    torch.backends.cudnn.benchmark = True
    torch.autograd.profiler.profile = False
    torch.autograd.profiler.emit_nvtx(enabled=False)
    torch.autograd.set_detect_anomaly(False)

    # anisotropy is roughly (1, 1, 5)

    initial_sigma = torch.tensor([20., 20., 20.], device=device)
    a = {'multiplier': 0.66, 'epoch': 200}
    b = {'multiplier': 0.66, 'epoch': 800}
    c = {'multiplier': 0.66, 'epoch': 1500}
    d = {'multiplier': 0.5, 'epoch': 20000}
    f = {'multiplier': 0.5, 'epoch': 20000}
    sigma = Sigma([a, b, c, d, f], initial_sigma, device)

    # The constants dict contains everything needed to replicate a training run.
    # will get serialized and saved.
    epochs = 10000
    constants = {
        'model': model,
        'vector_scale': vector_scale,
        'anisotropy': anisotropy,
        'lr': 5e-4 / 3, #5e-4,
        'wd': 1e-6 / 0.33, #1e-6,
        'optimizer': partial(Lion, use_triton=False), #partial(torch.optim.AdamW, eps=1e-16),
        'scheduler': partial(torch.optim.lr_scheduler.CosineAnnealingWarmRestarts, T_0=epochs+ 1),
        'sigma': sigma,
        'loss_embed': soft_dice_cldice(), #tversky(alpha=0.25, beta=0.75, eps=1e-8, device=device),
        'loss_prob': soft_dice_cldice(), #tversky(alpha=0.5, beta=0.5, eps=1e-8, device=device),
        'loss_skele': tversky(alpha=0.5, beta=1.5, eps=1e-8, device=device),
        'epochs': epochs,
        'device': device,
        'train_data': dataloader,
        'val_data': valdiation_dataloader,
        'train_sampler': train_sampler,
        'test_sampler': test_sampler,
        'distributed': True,
        'mixed_precision': True,
        'rank': rank,
        'n_warmup': 1500,
        'savepath': '/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/models',
    }

    writer = SummaryWriter() if rank == 0 else None
    if writer:
        print('SUMMARY WRITER LOG DIR: ', writer.get_logdir())
    model_state_dict, optimizer_state_dict, avg_loss = engine(writer=writer, verbose=True, force=True, **constants)
    avg_loss = torch.tensor(avg_loss)
    if writer:
        writer.add_hparams(hyperparams,
                           {'hparam/loss': avg_loss[-20:-1].mean().item()})

    if rank == 0:
        for k in constants:
            if k in ['model', 'train_data', 'val_data', 'train_sampler', 'test_sampler', 'loss_embed', 'loss_prob']:
                constants[k] = str(constants[k])

        constants['model_state_dict'] = model_state_dict
        constants['optimizer_state_dict'] = optimizer_state_dict

        torch.save(constants,
                   f'/home/chris/Dropbox (Partners HealthCare)/trainMitochondriaSegmentation/models/{os.path.split(writer.log_dir)[-1]}.trch')

    cleanup(rank)


if __name__ == '__main__':
    port = find_free_port()
    world_size = 2
    mp.spawn(train, args=(port, world_size), nprocs=world_size, join=True)
