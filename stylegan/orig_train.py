import argparse
import random
import math

from tqdm import tqdm
import numpy as np
from PIL import Image

import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.autograd import Variable, grad
from torch.utils.data import DataLoader
from torchvision import datasets, transforms, utils
from collections import OrderedDict


from dataset import MultiResolutionDataset, AlignedDatasetLoader
from model import StyledGenerator, Discriminator, PerceptualLoss_v1, OrigStyledGenerator
import os
import time

def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        #par1[k].data.mul_(decay).add_(1 - decay, par2[k].data)
        par1[k].data.mul_(decay).add_(par2[k].data, alpha = 1 - decay)

def sample_data(dataset, batch_size, image_size=4):
    dataset.resolution = image_size
    loader = DataLoader(dataset, shuffle=True, batch_size=batch_size, num_workers=1, drop_last=True)

    return loader


def adjust_lr(optimizer, lr):
    for group in optimizer.param_groups:
        mult = group.get('mult', 1)
        group['lr'] = lr * mult


def train(args, dataset, generator, discriminator, step = None, i = 0, i_step = None):
    if step is None:
        step = int(math.log2(args.init_size)) - 2

    resolution = 4 * 2 ** step

    
    loader = sample_data(
        dataset, args.batch.get(resolution, args.batch_default), resolution
    )
    data_loader = iter(loader)

    adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
    adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

    args.total_iters = sum(args.iters.values())

    pbar = tqdm(range(i, args.total_iters))

    requires_grad(generator, False)
    requires_grad(discriminator, True)

    disc_loss_val = 0
    gen_loss_val = 0
    grad_loss_val = 0

    alpha = 0
    used_sample = 0

    max_step = int(math.log2(args.max_size)) - 2
    final_progress = False

    if i_step is None:
        i_step = 0


    for i in pbar:
        t0 = time.time()

        discriminator.zero_grad()

        alpha = min(1, (i_step / (args.iters[resolution] * args.alpha_iters)))

        # alpha = min(1, 1 / args.phase * (used_sample + 1))

        if (resolution == args.init_size and args.ckpt_name is None) or final_progress:
            alpha = 1

        if i_step == args.iters[resolution]:  

            used_sample = 0
            i_step = 0
            step += 1

            if step > max_step:
                step = max_step
                final_progress = True
                ckpt_step = step + 1

            else:
                alpha = 0
                ckpt_step = step

            resolution = 4 * 2 ** step

            loader = sample_data(
                dataset, args.batch.get(resolution, args.batch_default), resolution
            )
            data_loader = iter(loader)

            adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
            adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

        try:
            data_step = next(data_loader)
            real_image = data_step['img_to'].cuda()
            img_enc = data_step['img_enc'].cuda()
            img_id_from = data_step['img_from_id'].cuda()
            img_from = data_step['img_from'].cuda()

        except (OSError, StopIteration):
            data_loader = iter(loader)
            data_step = next(data_loader)
            real_image = data_step['img_to'].cuda()
            img_enc = data_step['img_enc'].cuda()
            img_id_from = data_step['img_from_id'].cuda()
            img_from = data_step['img_from'].cuda()

        used_sample += real_image.shape[0]

        b_size = real_image.size(0)
        real_image = real_image.cuda()

        if args.loss == 'wgan-gp' and not args.no_use_gan:
            real_predict = discriminator(real_image, step=step, alpha=alpha)
            real_predict = real_predict.mean() - 0.001 * (real_predict ** 2).mean()
            (-real_predict).backward()

        elif args.loss == 'r1' and not args.no_use_gan:
            real_image.requires_grad = True
            real_scores = discriminator(real_image, step=step, alpha=alpha)
            real_predict = F.softplus(-real_scores).mean()
            real_predict.backward(retain_graph=True)

            grad_real = grad(
                outputs=real_scores.sum(), inputs=real_image, create_graph=True
            )[0]
            grad_penalty = (
                grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
            ).mean()
            grad_penalty = 10 / 2 * grad_penalty
            grad_penalty.backward()
            if i%10 == 0:
                grad_loss_val = grad_penalty.item()

        if args.mixing and random.random() < 0.9:
            gen_in11, gen_in12, gen_in21, gen_in22 = torch.randn(
                4, b_size, args.code_size, device='cuda'
            ).chunk(4, 0)
            gen_in1 = [gen_in11.squeeze(0), gen_in12.squeeze(0)]
            gen_in2 = [gen_in21.squeeze(0), gen_in22.squeeze(0)]

        else:
            gen_in1, gen_in2 = torch.randn(2, b_size, args.code_size, device='cuda').chunk(
                2, 0
            )
            gen_in1 = gen_in1.squeeze(0)
            gen_in2 = gen_in2.squeeze(0)

        fake_image = generator(gen_in1, step=step, alpha=alpha)
        fake_predict = discriminator(fake_image, step=step, alpha=alpha)

        if args.loss == 'wgan-gp' and not args.no_use_gan:
            fake_predict = fake_predict.mean()
            fake_predict.backward()

            eps = torch.rand(b_size, 1, 1, 1).cuda()
            x_hat = eps * real_image.data + (1 - eps) * fake_image.data
            x_hat.requires_grad = True
            hat_predict = discriminator(x_hat, step=step, alpha=alpha)
            grad_x_hat = grad(
                outputs=hat_predict.sum(), inputs=x_hat, create_graph=True
            )[0]
            grad_penalty = (
                (grad_x_hat.view(grad_x_hat.size(0), -1).norm(2, dim=1) - 1) ** 2
            ).mean()
            grad_penalty = 10 * grad_penalty
            grad_penalty.backward()
            if i%10 == 0:
                grad_loss_val = grad_penalty.item()
                disc_loss_val = (-real_predict + fake_predict).item()

        elif args.loss == 'r1' and not args.no_use_gan:
            fake_predict = F.softplus(fake_predict).mean()
            fake_predict.backward()
            if i%10 == 0:
                disc_loss_val = (real_predict + fake_predict).item()

        d_optimizer.step()

        if (i + 1) % n_critic == 0:
            generator.zero_grad()

            requires_grad(generator, True)
            requires_grad(discriminator, False)

            fake_image = generator(gen_in2, step=step, alpha=alpha)

            predict = discriminator(fake_image, step=step, alpha=alpha)

            loss = 0

            if args.loss == 'wgan-gp' and not args.no_use_gan:
                loss = -predict.mean()

            elif args.loss == 'r1' and not args.no_use_gan:
                loss = F.softplus(-predict).mean()

            if i%10 == 0 and not args.no_use_gan:
                gen_loss_val = loss.item()

            loss.backward()
            g_optimizer.step()
            accumulate(g_running, generator.module)

            requires_grad(generator, False)
            requires_grad(discriminator, True)





        if (i + 1) % args.sample_iters == 0:
            images = []

            gen_i, gen_j = args.gen_sample.get(resolution, (4, 4))

            with torch.no_grad():
                for k in range(gen_i):
                    images.append(
                        g_running(torch.randn(gen_j, args.code_size).cuda(), step=step, alpha=alpha).data.cpu()
                    )

            save_name = os.path.join(args.experiment_dir, "sample")
            os.makedirs(save_name, exist_ok = True)
            save_name = os.path.join(save_name, f'{str(i + 1).zfill(6)}.png')
            utils.save_image(
                torch.cat(images, 0),
                save_name,
                nrow=gen_i,
                normalize=True,
                range=(-1, 1),
            )

            if args.multisize_vis:
                save_name = os.path.join(args.experiment_dir, "sample/multi_size")
                os.makedirs(save_name, exist_ok = True)
                images = []
                fixed_style = torch.randn(1, args.code_size).cuda()
                with torch.no_grad():
                    for k in range(1, step + 1):
                        images.append(
                            (g_running(fixed_style, step=k, alpha=alpha).data.cpu(), k)
                        )

                for im in images:
                    save_name1 = os.path.join(save_name, f'{str(i + 1).zfill(6)}_{4 * 2 ** im[1]}.png')
                    utils.save_image(
                        im[0],
                        save_name1,
                        nrow=1,
                        normalize=True,
                        range=(-1, 1),
                    )

        if (i + 1) % args.save_iters == 0 or i_step + 1 == args.iters[resolution]:

            save_name = os.path.join(args.experiment_dir, f'{str(i + 1).zfill(6)}_{4 * 2 ** step}.model')
            torch.save(
                {
                    'generator': generator.module.state_dict(),
                    'discriminator': discriminator.module.state_dict(),
                    'g_optimizer': g_optimizer.state_dict(),
                    'd_optimizer': d_optimizer.state_dict(),
                    'g_running': g_running.state_dict(),
                    'step': step,
                    'i': i,
                    'i_step' : i_step,
                },
                save_name
            )



        state_msg = (
            f'Iters: {i + 1}; Iter time: {(time.time() - t0):.2f}; Size: {4 * 2 ** step}'
        )
        if not args.no_use_gan:
            state_msg += f'; G: {gen_loss_val:.3f}; D: {disc_loss_val:.3f}; Grad: {grad_loss_val:.3f}'
        state_msg += f'; Alpha: {alpha:.5f}'

        i_step += 1
        pbar.set_description(state_msg)


if __name__ == '__main__':
    n_critic = 1

    parser = argparse.ArgumentParser(description='Progressive Growing of GANs')

    # parser.add_argument('path', type=str, help='path of specified dataset')
    parser.add_argument('--name', type=str, help='Name of experiment')

    parser.add_argument('--source_path', type = str)
    parser.add_argument('--target_path', type = str)
    parser.add_argument('--sample_iters', type = int, default = 100)
    parser.add_argument('--save_iters', type = int, default = 10_000)

    parser.add_argument('--total_iters', type=int, default=100_000, help='number of samples used for each training phases')
    parser.add_argument('--phase', type=int, default=10_000, help='number of samples used for each training phases')
    parser.add_argument('--lr', default=0.001, type=float, help='learning rate')
    parser.add_argument('--sched', action='store_true', help='use lr scheduling')
    parser.add_argument('--init_size', default=8, type=int, help='initial image size')
    parser.add_argument('--max_size', default=256, type=int, help='max image size')
    parser.add_argument('--code_size', default=512, type=int)
    parser.add_argument('--n_mlp', default=None, type=int)
    parser.add_argument('--loss', type=str, default='wgan-gp', choices=['wgan-gp', 'r1'], help='class of gan loss')
    parser.add_argument('--no_from_rgb_activate', action='store_true', help='use activate in from_rgb (original implementation)')
    parser.add_argument('--alpha_iters', type = float, default = 0.5)
    parser.add_argument('--no_use_gan', action = 'store_true')

    parser.add_argument('--ckpt_name', default=None, type=str, help='load from previous checkpoints')
    
    parser.add_argument('--mixing', action='store_true', help='use mixing regularization')

    parser.add_argument('--params_file', type = str, default = 'params/default_iter_params')

    parser.add_argument('--multisize_vis', action = 'store_true')
    parser.add_argument('--use_face_weights', action = 'store_true')

    
    args = parser.parse_args()
    args.experiment_dir = os.path.join("./checkpoints", args.name)
    os.makedirs(args.experiment_dir, exist_ok = True)

    transform = transforms.Compose(
        [
            # transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True),
        ]
    )

    dataset = AlignedDatasetLoader(args.source_path, args.target_path, transform, enc_resolution = args.max_size)



    generator = nn.DataParallel(OrigStyledGenerator(args.code_size, n_mlp = args.n_mlp)).cuda()
    discriminator = nn.DataParallel(
        Discriminator(from_rgb_activate=not args.no_from_rgb_activate)
    ).cuda()
    g_running = OrigStyledGenerator(args.code_size, n_mlp = args.n_mlp).cuda()
    g_running.train(False)

    g_optimizer = optim.Adam(
        generator.module.generator.parameters(), lr=args.lr, betas=(0.0, 0.99)
    )
    g_optimizer.add_param_group(
        {
            'params': generator.module.style.parameters(),
            'lr': args.lr * 0.01,
            'mult': 0.01,
        }
    )

    d_optimizer = optim.Adam(discriminator.parameters(), lr=args.lr, betas=(0.0, 0.99))

    accumulate(g_running, generator.module, 0)

    if args.use_face_weights:
        ckpt = torch.load("./stylegan-256px-new.model")

        old_ckpt_gen = ckpt['generator']
        new_ckpt_gen = OrderedDict()
        for k in old_ckpt_gen:
            splited = k.split('.')
            if (splited[0] == 'generator' and (splited[1] == 'progression' or splited[1] == 'to_rgb') and int(splited[2]) <= 6) or splited[0] == 'style':
                new_ckpt_gen.update({k: old_ckpt_gen[k]})

        old_ckpt_disc = ckpt['discriminator']
        new_ckpt_disc = OrderedDict()
        for k in old_ckpt_disc:
            splited = k.split('.')
            if (splited[0] == 'progression' or splited[0] == 'from_rgb') and int(splited[1]) > 0:
                splited[1] = str(int(splited[1]) - 1)
                # if splited[0] == 'from_rgb':
                #     splited.pop(2)
                new_k = '.'.join(splited)
                new_ckpt_disc.update({new_k: old_ckpt_disc[k]})
            if splited[0] == 'linear':
                new_ckpt_disc.update({k: old_ckpt_disc[k]})

        old_ckpt_g = ckpt['g_running']
        new_ckpt_g = OrderedDict()
        for k in old_ckpt_g:
            splited = k.split('.')
            if (splited[0] == 'generator' and (splited[1] == 'progression' or splited[1] == 'to_rgb') and int(splited[2]) <= 6) or splited[0] == 'style':
                new_ckpt_g.update({k: old_ckpt_g[k]})

        generator.module.load_state_dict(new_ckpt_gen)
        discriminator.module.load_state_dict(new_ckpt_disc)
        g_running.load_state_dict(new_ckpt_g)


    step = None
    i = 0
    i_step = None
    if args.ckpt_name is not None:
        # ckpt_name = os.path.join("./checkpoints", args.ckpt_name)
        ckpt = torch.load(args.ckpt_name)

        generator.module.load_state_dict(ckpt['generator'])
        discriminator.module.load_state_dict(ckpt['discriminator'])
        g_running.load_state_dict(ckpt['g_running'])
        g_optimizer.load_state_dict(ckpt['g_optimizer'])
        d_optimizer.load_state_dict(ckpt['d_optimizer'])
        step = ckpt['step']
        i = ckpt['i'] + 1
        i_step = ckpt['i_step'] + 1

    if args.sched:
        args.lr = {128: 0.0015, 256: 0.002}
        args.batch = {}
        args.iters = {}
        if args.params_file == '':
            args.batch = {4 : 128,   8 : 64,    16 : 64,    32 : 32,    64 : 16,    128 : 8,     256 : 4}
            args.iters = {4 : 0  ,   8 : 10000, 16 : 10000, 32 : 20000, 64 : 40000, 128 : 80000, 256 : 160000}
        else:
            with open(args.params_file, 'r') as f:
                for line in f.readlines():
                    resolution = int(line.split()[0])
                    bs = int(line.split()[1])
                    iters = int(line.split()[2])
                    args.batch[resolution] = bs
                    args.iters[resolution] = iters
        # args.iters = {4 : 0, 8 : 10, 16 : 10, 32 : 20, 64 : 10, 128 : 10, 256 : 10}

    else:
        args.lr = {}
        args.batch = {}

    args.gen_sample = {512: (8, 4), 1024: (4, 2)}

    args.batch_default = 32

    train(args, dataset, generator, discriminator, step, i, i_step)