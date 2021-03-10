"""
Adapted from https://github.com/NVlabs/MUNIT
Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
from utils import get_all_data_loaders, prepare_sub_folder, write_html, write_loss, get_config, write_2images, Timer
import argparse
from torch.autograd import Variable
from trainer import MUNIT_Trainer, UNIT_Trainer
import torch.backends.cudnn as cudnn
import torch
import torch.multiprocessing as mp

try:
    from itertools import izip as zip
except ImportError: # will be 3.x series
    pass
import os
import sys
import tensorboardX
import shutil
import time
import numpy as np
from tqdm import tqdm

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default='configs/edges2handbags_folder.yaml', help='Path to the config file.')
parser.add_argument('--output_path', type=str, default='.', help="outputs path")
parser.add_argument("--resume", action="store_true")
parser.add_argument('--trainer', type=str, default='MUNIT', help="MUNIT|UNIT")
opts = parser.parse_args()

cudnn.benchmark = True

# Load experiment setting
config = get_config(opts.config)
max_iter = config['max_iter']
display_size = config['display_size']
config['vgg_model_path'] = opts.output_path

# Setup model and data loader
if opts.trainer == 'MUNIT':
    trainer = MUNIT_Trainer(config)
elif opts.trainer == 'UNIT':
    trainer = UNIT_Trainer(config)
else:
    sys.exit("Only support MUNIT|UNIT")
trainer.cuda()
train_loader_a, train_loader_b, test_loader_a, test_loader_b = get_all_data_loaders(config)

np.random.seed(1000)
fixed_inds = [np.random.randint(len(train_loader_a)) for i in range(display_size)]
train_display_images_a = torch.stack([train_loader_a.dataset[i] for i in fixed_inds]).cuda()
fixed_inds = [np.random.randint(len(train_loader_b)) for i in range(display_size)]
train_display_images_b = torch.stack([train_loader_b.dataset[i] for i in fixed_inds]).cuda()
fixed_inds = [np.random.randint(len(test_loader_a)) for i in range(display_size)]
test_display_images_a = torch.stack([test_loader_a.dataset[i] for i in fixed_inds]).cuda()
fixed_inds = [np.random.randint(len(test_loader_b)) for i in range(display_size)]
test_display_images_b = torch.stack([test_loader_b.dataset[i] for i in fixed_inds]).cuda()
np.random.seed(int(time.time()))

# Setup logger and output folders
model_name = os.path.splitext(os.path.basename(opts.config))[0]
train_writer = tensorboardX.SummaryWriter(os.path.join(opts.output_path + "/logs", model_name))
output_directory = os.path.join(opts.output_path + "/outputs", model_name)
checkpoint_directory, image_directory = prepare_sub_folder(output_directory)
shutil.copy(opts.config, os.path.join(output_directory, 'config.yaml')) # copy config file to output folder

state_msg = ''
# Start training
iterations = trainer.resume(checkpoint_directory, hyperparameters=config) if opts.resume else 0
while True:

    pbar = tqdm(range(iterations, max_iter))
    pbar_it = iter(pbar)


    for images_a, images_b in zip(train_loader_a, train_loader_b):

        images_a, images_b = images_a.cuda().detach(), images_b.cuda().detach()

        start_time = time.time()
            # Main training code
        trainer.dis_update(images_a, images_b, config)
        trainer.gen_update(images_a, images_b, config)
        torch.cuda.synchronize()
        trainer.update_learning_rate()

        elapsed_time = time.time() - start_time

        # Dump training stats in log file
        if (iterations + 1) % config['log_iter'] == 0:
            # print("Training Progress: %08d/%08d" % (iterations + 1, max_iter))
            write_loss(iterations, trainer, train_writer)
        pbar.set_description(f'Elapsed time: {elapsed_time:.3f}')


        # Write images
        if (iterations + 1) % config['image_save_iter'] == 0:
            with torch.no_grad():
                test_image_outputs = trainer.sample(test_display_images_a, test_display_images_b)
                train_image_outputs = trainer.sample(train_display_images_a, train_display_images_b)
            write_2images(test_image_outputs, display_size, image_directory, 'test_%08d' % (iterations + 1))
            write_2images(train_image_outputs, display_size, image_directory, 'train_%08d' % (iterations + 1))
            # HTML
            write_html(output_directory + "/index.html", iterations + 1, config['image_save_iter'], 'images')

        if (iterations + 1) % config['image_display_iter'] == 0:
            with torch.no_grad():
                image_outputs = trainer.sample(train_display_images_a, train_display_images_b)
            write_2images(image_outputs, display_size, image_directory, 'train_current')

        # Save network weights
        if (iterations + 1) % config['snapshot_save_iter'] == 0:
            trainer.save(checkpoint_directory, iterations)

        iterations += 1
        next(pbar_it)
        if iterations >= max_iter:
            sys.exit('Finish training')