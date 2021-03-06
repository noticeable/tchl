from tcn import resnet_3d_models
from kitti_horizon.kitti_horizon_torch import KITTIHorizon
from utilities.tee import Tee
import torch
from torch import nn
from torchvision import transforms
from tensorboardX import SummaryWriter
import datetime
import os
import numpy as np
import math
import time
import platform
import shutil
import sklearn.metrics
from torch.nn.modules.loss import _Loss
from torch.nn import functional as F
import argparse
from utilities.losses import *


class CosineAnnealingCustom:

    def __init__(self, begin, end, T_max):
        self.T_max = T_max
        self.begin = begin
        self.end = end
        self.inv = begin < end

    def get(self, epoch):
        if not self.inv:
            return self.end + (self.begin - self.end) * (1 + math.cos(math.pi * epoch / self.T_max)) / 2
        else:
            return self.begin + (self.end - self.begin) * (1 - math.cos(math.pi * epoch / self.T_max)) / 2


def adjust_learning_rate(optimizer, lr):
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


class Cutout(object):
    def __init__(self, length, bias=False):
        self.length = length
        self.central_bias = bias

    def __call__(self, img):
        h, w = img.size(1), img.size(2)
        mask = np.ones((h, w), np.float32)

        if self.central_bias:
            x = int(np.around(w / 4. * np.random.rand(1) + w / 2.))
            y = int(np.around(h / 4. * np.random.rand(1) + h / 2.))

        else:
            y = np.random.randint(h)
            x = np.random.randint(w)

        lx = np.random.randint(1, self.length)
        ly = np.random.randint(1, self.length)

        y1 = np.clip(y - ly // 2, 0, h)
        y2 = np.clip(y + ly // 2, 0, h)
        x1 = np.clip(x - lx // 2, 0, w)
        x2 = np.clip(x + lx // 2, 0, w)

        mask[y1: y2, x1: x2] = 0.
        mask = torch.from_numpy(mask)
        mask = mask.expand_as(img)
        img *= mask
        return img


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--dataset_path', default="/data/kluger/tmp/kitti_horizon/", type=str,
                        help='path to preprocessed dataset')
    parser.add_argument('--checkpoint_path', default="./tmp/training_results/", type=str,
                        help='folder where checkpoints will be stored')
    parser.add_argument('--image_width', default=625, type=int, help='image width')
    parser.add_argument('--image_height', default=190, type=int, help='image height')
    parser.add_argument('--finetune', dest='finetune', action='store_true', help='finetune the CNN')
    parser.add_argument('--lb1', default="BB13", type=str, metavar='DS', help='define 1st set of 3d basic blocks')
    parser.add_argument('--lb2', default="BB13", type=str, metavar='DS', help='define 2nd set of 3d basic blocks')
    parser.add_argument('--resume', dest='resume', action='store_true', help='')
    parser.add_argument('--gpu', default='0', type=str, help='GPU ID')
    parser.add_argument('--epochs', default=160, type=int, help='num epochs')
    parser.add_argument('--baselr', default=0.1 / 128, type=float, help='base learning rate')
    parser.add_argument('--lr_reduction', default=1e-2, type=float, help='min. learning rate')
    parser.add_argument('--seqlength', default=32, type=int, help='sequence length')
    parser.add_argument('--seqlength_val', default=512, type=int, metavar='N', help='sequence length (validation)')
    parser.add_argument('--batch', default=4, type=int, help='batch size')
    parser.add_argument('--optimizer', default='sgd', type=str, help='optimizer: sgd or adam')
    parser.add_argument('--loss', default='huber', type=str, help='loss function')
    parser.add_argument('--lossmax', default='l1', type=str, help='loss function (for max. horizon error)')
    parser.add_argument('--seed', default=1, type=int, help='random seed')
    parser.add_argument('--cutout', default=512, type=int, help='use cutout')
    parser.add_argument('--angle_loss_weight', default=1., type=float, help='weighting of the angle (slope) loss')
    parser.add_argument('--load', default=None, type=str, help='load pretrained model')
    parser.add_argument('--eval', dest='eval', action='store_true', help='run single validation step (no training)')
    parser.add_argument('--max_error_loss', dest='max_error_loss', action='store_true',
                        help='use max horizon error loss')
    parser.add_argument('--max_error_loss_only', dest='max_error_loss_only', action='store_true',
                        help='only use max horizon error loss')
    parser.add_argument('--no_modelzoo_load', dest='nomzload', action='store_true',
                        help='do not load NN weights pretrained on imagenet')

    args = parser.parse_args()

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    args_load = args.load
    args_resume = args.resume
    if args.resume and args_load is not None:
        load_from_path = args.load
        print("load weights from ", load_from_path)
        checkpoint = torch.load(load_from_path, map_location=lambda storage, loc: storage)
        args = checkpoint['args']

    hostname = platform.node()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu', 0)

    torch.backends.cudnn.deterministic = True

    pixel_mean = [0.362365, 0.377767, 0.366744]

    learning_rate = args.baselr * args.batch * args.seqlength

    images_per_batch = args.batch * args.seqlength

    torch.manual_seed(args.seed)

    csv_base = "./kitti_horizon/split/"

    modelfun = resnet_3d_models.resnet18_2_2d_2_3d
    model, blocks = modelfun(order='BDCHW', blocknames=[args.lb1, args.lb2])
    model = model.to(device)

    fov_increase = model.fov_increase

    target_directory = args.checkpoint_path + "/%s/%s/%d/" % ("kitti", "resnet18_2_2d_2_3d", args.seqlength)

    date_and_time = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

    checkpoint_directory = target_directory + "b%d_" % args.batch + date_and_time

    if not os.path.exists(checkpoint_directory):
        os.makedirs(checkpoint_directory)

    log_file = os.path.join(checkpoint_directory, "output.log")
    log = Tee(log_file, "w", file_only=False)

    print("hostname: ", hostname)

    for arg in vars(args):
        print(arg, getattr(args, arg))

    print("fov increase: ", model.fov_increase)

    for b in blocks: print(b.block_name)

    if args.loss == 'mse':
        criterion = nn.MSELoss()
    elif args.loss == 'huber':
        criterion = nn.SmoothL1Loss()
    elif args.loss == 'l1':
        criterion = nn.L1Loss()
    else:
        assert False

    if args.lossmax == 'mse':
        criterionmax = nn.MSELoss(size_average=False, reduce=False)
    elif args.lossmax == 'huber':
        criterionmax = nn.SmoothL1Loss(size_average=False, reduce=False)
    elif args.lossmax == 'l1':
        criterionmax = nn.L1Loss(size_average=False, reduce=False)
    elif args.lossmax == 'sqrt':
        criterionmax = SqrtL1Loss(size_average=False, reduce=False)
    else:
        assert False

    if args.optimizer == 'adam':
        optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate, weight_decay=1e-4)
    elif args.optimizer == 'sgd':
        optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate, weight_decay=1e-4, momentum=0.9)
    else:
        assert False

    horizon_error_function = horizon_error(args.image_width, args.image_height)

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, int(args.epochs), eta_min=learning_rate * args.lr_reduction)

    start_epoch = 0
    if args_load is not None:
        load_from_path = args_load
        print("load weights from ", load_from_path)
        checkpoint = torch.load(load_from_path, map_location=lambda storage, loc: storage)
        model.load_state_dict(checkpoint['state_dict'], strict=True)

        if args_resume:
            optimizer.load_state_dict(checkpoint['optimizer'])
            start_epoch = checkpoint['epoch']

            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, int(args.epochs), eta_min=learning_rate * args.lr_reduction, last_epoch=start_epoch)

    max_err_scheduler = CosineAnnealingCustom(0, 1., args.epochs)

    tfs = transforms.Compose([
                transforms.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.25, hue=0.25),
                transforms.RandomGrayscale(p=0.1),
                transforms.ToTensor(),
                transforms.Normalize(mean=pixel_mean, std=[1., 1., 1.]),
            ])
    if args.cutout:
        tfs.transforms.append(Cutout(length=args.cutout, bias=False))

    tfs_val = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize(mean=pixel_mean, std=[1., 1., 1.]),
            ])

    train_dataset = KITTIHorizon(root_dir=args.dataset_path, csv_file=csv_base + "/train.csv",
                                 seq_length=args.seqlength,
                                 fill_up=True, transform=tfs, padding=fov_increase)
    val_dataset = KITTIHorizon(root_dir=args.dataset_path, augmentation=False, csv_file=csv_base + "/val.csv",
                               seq_length=args.seqlength_val, fill_up=False, transform=tfs_val, padding=fov_increase)


    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                               batch_size=args.batch,
                                               shuffle=True, num_workers=4)

    val_loader = torch.utils.data.DataLoader(dataset=val_dataset,
                                              batch_size=1,
                                              shuffle=False, num_workers=4)

    # For updating learning rate
    def update_lr(optimizer, lr):
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    def update_batchsize(loader, batch_size):
        loader = torch.utils.data.DataLoader(dataset=loader.dataset, batch_size=batch_size, shuffle=True, num_workers=loader.num_workers)
        return loader

    def save_checkpoint(state, is_best, folder, epoch, loss):
        filename = folder + "/" + "%03d_%.6f.ckpt" % (epoch, loss)
        torch.save(state, filename)
        if is_best:
            shutil.copyfile(filename, folder + '/model_best.ckpt')


    tensorboard_directory = checkpoint_directory + "/tensorboard/"
    if not os.path.exists(tensorboard_directory):
        os.makedirs(tensorboard_directory)
    tensorboard_writer = SummaryWriter(tensorboard_directory)

    print(checkpoint_directory)

    total_step = len(train_loader)
    curr_lr = learning_rate
    curr_batch_size = args.batch
    best_val_loss = 10000.

    calc_hlr = calc_horizon_leftright(width=args.image_width, height=args.image_height)

    best_auc = {'epoch': 0, 'max_err': np.inf, 'auc':0}
    best_err = {'epoch': 0, 'max_err': np.inf, 'auc':0}


    model = nn.DataParallel(model)

    i = 0

    for epoch in range(start_epoch, args.epochs):

        if not args.eval:
            scheduler.step(epoch=epoch)
            adjust_learning_rate(optimizer, scheduler.get_lr()[0])

            losses = []
            offset_losses = []
            angle_losses = []
            max_err_losses = []

            tt0 = time.time()

            model.train()
            for i, sample in enumerate(train_loader):

                images = sample['images'].to(device, non_blocking=True)
                offsets = sample['offsets'].to(device, non_blocking=True)
                angles = sample['angles'].to(device, non_blocking=True)
                # Forward pass
                output_offsets, output_angles = model(images)
                output_offsets = output_offsets[:,fov_increase:args.seqlength+fov_increase]
                output_angles = output_angles[:,fov_increase:args.seqlength+fov_increase]
                offsets = offsets[:,fov_increase:args.seqlength+fov_increase]
                angles = angles[:,fov_increase:args.seqlength+fov_increase]

                offset_loss = criterion(output_offsets, offsets)
                angle_loss = criterion(output_angles, angles)

                loss = 0

                hl_true, hr_true = calc_hlr(offsets, angles)
                hl_estm, hr_estm = calc_hlr(output_offsets, output_angles)
                hl_err = criterionmax(hl_estm, hl_true)
                hr_err = criterionmax(hr_estm, hr_true)
                h_errs = torch.clamp(torch.max(hl_err, hr_err), 0, 1.)
                max_err_loss = torch.mean(h_errs)
                max_err_losses += [max_err_loss]

                loss += offset_loss + angle_loss * args.angle_loss_weight

                if args.max_error_loss:
                    loss = max_err_scheduler.get(epoch) * max_err_loss * 0.1 + (1-max_err_scheduler.get(epoch)) * loss

                tt3 = time.time()


                # Backward and optimize
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                losses.append(loss)
                offset_losses.append(offset_loss)
                angle_losses.append(angle_loss)

                if (i+1) % 100 == 0:
                    losses_tensor = torch.stack(losses, dim=0).view(-1)
                    average_loss = losses_tensor.mean().item()

                    offset_losses_tensor = torch.stack(offset_losses, dim=0).view(-1)
                    average_offset_loss = offset_losses_tensor.mean().item()
                    angle_losses_tensor = torch.stack(angle_losses, dim=0).view(-1)
                    average_angle_loss = angle_losses_tensor.mean().item()

                    num_iteration = int((epoch*total_step + i) * images_per_batch / 128.)

                    max_err_losses_tensor = torch.stack(max_err_losses, dim=0).view(-1)
                    average_max_err_loss = max_err_losses_tensor.mean().item()
                    tensorboard_writer.add_scalar('train/max_err_loss', max_err_loss.item(), num_iteration)
                    tensorboard_writer.add_scalar('train/max_err_loss_avg', average_max_err_loss, num_iteration)


                    print ("Epoch [{}/{}], Step [{}/{}] Losses: {:.6f} {:.6f} {:.6f}, Avg.: {:.6f} {:.6f} {:.6f}"
                           .format(epoch+1, args.epochs, i+1, total_step, offset_loss.item(), angle_loss.item(), loss.item(),
                                   average_offset_loss, average_angle_loss, average_loss), end="\r")

                    tensorboard_writer.add_scalar('train/loss', loss.item(), num_iteration)
                    tensorboard_writer.add_scalar('train/offset_loss', offset_loss.item(), num_iteration)
                    tensorboard_writer.add_scalar('train/angle_loss', angle_loss.item(), num_iteration)
                    tensorboard_writer.add_scalar('train/loss_avg', average_loss, num_iteration)
                    tensorboard_writer.add_scalar('train/offset_loss_avg', average_offset_loss, num_iteration)
                    tensorboard_writer.add_scalar('train/angle_loss_avg', average_angle_loss, num_iteration)

                    tensorboard_writer.add_scalar('learning_rate', scheduler.get_lr()[0], num_iteration)

                tt0 = time.time()


        # test on validation set:
        model.eval()
        with torch.no_grad():
            losses = []
            offset_losses = []
            angle_losses = []
            offset_ema_losses = []
            angle_ema_losses = []
            offset_dif_losses = []
            angle_dif_losses = []
            max_err_losses = []

            all_horizon_errors = []

            image_count = 0

            for idx, sample in enumerate(val_loader):
                images = sample['images'].to(device)
                offsets = sample['offsets'].to(device)
                angles = sample['angles'].to(device)
                    
                image_count += images.shape[0]*images.shape[1]

                output_offsets, output_angles = model(images)
                output_offsets = output_offsets[:,fov_increase:offsets.shape[1]]
                output_angles = output_angles[:,fov_increase:angles.shape[1]]
                offsets = offsets[:,fov_increase:offsets.shape[1]]
                angles = angles[:,fov_increase:angles.shape[1]]

                offset_loss = criterion(output_offsets, offsets)
                angle_loss = criterion(output_angles, angles)

                loss = offset_loss + angle_loss * args.angle_loss_weight

                hl_true, hr_true = calc_hlr(offsets, angles)
                hl_estm, hr_estm = calc_hlr(output_offsets, output_angles)
                hl_err = criterionmax(hl_estm, hl_true)
                hr_err = criterionmax(hr_estm, hr_true)
                h_errs = torch.max(hl_err, hr_err)
                max_err_loss = torch.mean(h_errs)
                max_err_losses += [max_err_loss]

                if args.max_error_loss:
                    loss = max_err_scheduler.get(epoch) * 0.1 * max_err_loss + (1 - max_err_scheduler.get(epoch)) * loss

                all_horizon_errors += horizon_error_function(output_angles,
                                                             output_offsets,
                                                             angles,
                                                             offsets)
                losses.append(loss.item())
                offset_losses.append(offset_loss.item())
                angle_losses.append(angle_loss.item())

            average_loss = np.mean(losses)
            average_offset_loss = np.mean(offset_losses)
            average_angle_loss = np.mean(angle_losses)

            num_iteration = int((epoch * total_step + i) * images_per_batch / 128.)

            max_err_losses_tensor = torch.stack(max_err_losses, dim=0).view(-1)
            average_max_err_loss = max_err_losses_tensor.mean().item()
            tensorboard_writer.add_scalar('val/max_err_loss_avg', average_max_err_loss, num_iteration)

            error_arr = np.array(all_horizon_errors)
            error_arr_idx = np.argsort(error_arr)
            error_arr = np.sort(error_arr)
            num_values = len(all_horizon_errors)

            plot_points = np.zeros((num_values, 2))

            err_cutoff = 0.25

            midfraction = 1.

            try:
                for i in range(num_values):
                    fraction = (i + 1) * 1.0 / num_values
                    value = error_arr[i]
                    plot_points[i, 1] = fraction
                    plot_points[i, 0] = value
                    if i > 0:
                        lastvalue = error_arr[i - 1]
                        if lastvalue < err_cutoff and value > err_cutoff:
                            midfraction = (lastvalue * plot_points[i - 1, 1] + value * fraction) / (value + lastvalue)

                if plot_points[-1, 0] < err_cutoff:
                    plot_points = np.vstack([plot_points, np.array([err_cutoff, 1])])
                else:
                    plot_points = np.vstack([plot_points, np.array([err_cutoff, midfraction])])

                sorting = np.argsort(plot_points[:, 0])
                plot_points = plot_points[sorting, :]

                auc = sklearn.metrics.auc(plot_points[plot_points[:, 0] <= err_cutoff, 0],
                                          plot_points[plot_points[:, 0] <= err_cutoff, 1])
                auc = auc / err_cutoff
            except:
                auc = 0

            print("\nValidation [{}/{}],  Avg.: {:.4f} {:.4f} {:.4f} {:.4f}"
                  .format(epoch + 1, args.epochs, average_offset_loss, average_angle_loss, average_loss, auc))

            if best_err['max_err'] > average_max_err_loss:
                best_err['epoch'] = epoch
                best_err['max_err'] = average_max_err_loss
                best_err['auc'] = auc

            if best_auc['auc'] < auc:
                best_auc['epoch'] = epoch
                best_auc['max_err'] = average_max_err_loss
                best_auc['auc'] = auc

            print("Best Err: %.4f -- AUC: %.4f -- epoch %d" % (best_err['max_err'], best_err['auc'], best_err['epoch']))
            print("Best AUC: %.4f -- Err: %.4f -- epoch %d" % (best_auc['auc'], best_auc['max_err'], best_auc['epoch']))

            if args.eval:
                exit(0)

            tensorboard_writer.add_scalar('val/loss_avg', average_loss, num_iteration)
            tensorboard_writer.add_scalar('val/offset_loss_avg', average_offset_loss, num_iteration)
            tensorboard_writer.add_scalar('val/angle_loss_avg', average_angle_loss, num_iteration)

        is_best = (average_loss < best_val_loss)
        best_val_loss = average_loss if is_best else best_val_loss

        save_checkpoint({
                'epoch': epoch,
                'args': args,
                'state_dict': model.module.state_dict(),
                'val_loss': average_loss,
                'optimizer' : optimizer.state_dict(),
            }, is_best, checkpoint_directory, epoch, average_loss)


    tensorboard_writer.close()
    log.__del__()

