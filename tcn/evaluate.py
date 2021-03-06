import os
os.environ["CUDA_VISIBLE_DEVICES"]="0"
from tcn import resnet_3d_models
from kitti_horizon.kitti_horizon_torch import KITTIHorizon
from utilities.tee import Tee
import torch
from torch import nn
import datetime
from torchvision import transforms
import os
import numpy as np
import time
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.animation as manimation
import platform
import logging
logger = logging.getLogger('matplotlib.animation')
logger.setLevel(logging.DEBUG)
hostname = platform.node()
import argparse
import glob
import contextlib
import math
import sklearn.metrics
from utilities.auc import *
from utilities.losses import calc_horizon_leftright

np.seterr(all='raise')

parser = argparse.ArgumentParser(description='')

parser.add_argument('--load', default=None, type=str, help='path to NN model weights')
parser.add_argument('--results', default="./tmp/results", type=str, help='path to store results in')
parser.add_argument('--dataset_path', default="/data/kluger/tmp/kitti_horizon", type=str, help='path to KITTI Horizon dataset')
parser.add_argument('--set', default='val', type=str, help='dataset to evaluate on: val or test')
parser.add_argument('--whole', dest='whole_sequence', action='store_true', help='process whole sequence at once')
parser.add_argument('--video', dest='video', action='store_true', help='generate video output (possibly very slow!)')
parser.add_argument('--seqlength', default=10000, type=int, help='maximum frames per sequence')
parser.add_argument('--cpu', dest='cpu', action='store_true', help='use CPU only')
parser.add_argument('--gpu', default='0', type=str, help='which GPU to use')
parser.add_argument('--tee', dest='tee', action='store_true', help='save console output to logfile')
parser.add_argument('--image_width', default=625, type=int, help='image width')
parser.add_argument('--image_height', default=190, type=int, help='image height')
parser.add_argument('--lb1', default="BB13", type=str, help='')
parser.add_argument('--lb2', default="BB13", type=str, help='')
args = parser.parse_args()

checkpoint_path = args.load if not (args.load == '') else None
set_type = args.set

result_folder = args.results

if not os.path.exists(result_folder):
    os.makedirs(result_folder)

os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

if args.cpu:
    device = torch.device('cpu', 0)
else:
    device = torch.device('cuda', 0)

seq_length = args.seqlength
whole_sequence = args.whole_sequence

if checkpoint_path is not None:

    cp_path = checkpoint_path

    load_from_path = cp_path
    print("load weights from ", load_from_path)
    checkpoint = torch.load(load_from_path, map_location=lambda storage, loc: storage)
    model_args = checkpoint['args']

    modelfun = resnet_3d_models.resnet18_2_2d_2_3d
    model, blocks = modelfun(order='BDCHW', blocknames=[model_args.lb1, model_args.lb2])
    model = model.to(device)

    fov_increase = model.fov_increase
    model.load_state_dict(checkpoint['state_dict'], strict=True)
    model.eval()


pixel_mean = [0.362365, 0.377767, 0.366744]

tfs_val = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=pixel_mean, std=[1., 1., 1.]),
        ])

im_width = args.image_width
im_height = args.image_height

calc_hlr = calc_horizon_leftright(im_width, im_height)

csv_base = "./kitti_horizon/split/"

val_dataset = KITTIHorizon(root_dir=args.dataset_path, augmentation=False, csv_file=csv_base + "/val.csv",
                           seq_length=seq_length, fill_up=False, transform=tfs_val, return_info=True,
                           padding=fov_increase)
test_dataset = KITTIHorizon(root_dir=args.dataset_path, augmentation=False, csv_file=csv_base + "/test.csv",
                            seq_length=seq_length, fill_up=False, transform=tfs_val, return_info=True,
                            padding=fov_increase)
train_dataset = KITTIHorizon(root_dir=args.dataset_path, augmentation=False, csv_file=csv_base + "/train.csv",
                             seq_length=seq_length, fill_up=False, transform=tfs_val, return_info=True,
                             padding=fov_increase)

train_loader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=1, shuffle=False)
val_loader = torch.utils.data.DataLoader(dataset=val_dataset, batch_size=1, shuffle=False)
test_loader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=1, shuffle=False)

if set_type == 'val':
    loader = val_loader
elif set_type == 'test':
    loader = test_loader
else:
    loader = train_loader

result_folder = os.path.join(result_folder, "%d/" % args.seqlength)

FFMpegWriter = manimation.writers['ffmpeg']
metadata = dict(title='Movie Test', artist='Matplotlib',
                comment='Movie support!', codec='vp9')
writer = FFMpegWriter(fps=10, metadata=metadata, codec='libx264', extra_args=['-intra'])

video_folder = result_folder
if not os.path.exists(video_folder):
    os.makedirs(video_folder)

png_folder = os.path.join(video_folder, "png")
if not os.path.exists(png_folder):
    os.makedirs(png_folder)
svg_folder = os.path.join(video_folder, "svg")
if not os.path.exists(svg_folder):
    os.makedirs(svg_folder)

if args.tee:
    log_file = os.path.join(result_folder, "log")
    log = Tee(os.path.join(result_folder, log_file), "w", file_only=False)

all_errors = []
all_angular_errors = []

image_count = 0

percs = [50, 80, 90, 95, 99]
print("percentiles: ", percs)

error_grads = []

with torch.no_grad():
    losses = []
    offset_losses = []
    angle_losses = []
    for idx, sample in enumerate(loader):

        images = sample['images']
        offsets = sample['offsets']
        angles = sample['angles']
        Gs = sample['G'][0]
        padding = sample['padding']
        scale = sample['scale'].numpy()
        K = np.matrix(sample['K'][0])

        print("idx ", idx, end="\t")
        all_offsets = []
        all_offsets_estm = []
        all_angles = []
        all_angles_estm = []

        all_errors_per_sequence = []
        all_angular_errors_per_sequence = []

        if args.video:
            fig = plt.figure(figsize=(6.4, 3.0))

            l1, = plt.plot([], [], '-', lw=2, c='#99C000')
            l2, = plt.plot([], [], '--', lw=2, c='#0083CC')


        with writer.saving(fig, video_folder + "%s%05d.mp4" % ("", idx), 300) \
                                                                if args.video else contextlib.suppress():
            if whole_sequence and checkpoint_path is not None:

                output_offsets, output_angles = model(images.to(device))

            for si in range(images.shape[1]-2*fov_increase):

                image_count += 1

                image = images.numpy()[0,si+2*fov_increase,:,:,:].transpose((1,2,0))
                width = image.shape[1]
                height = image.shape[0]

                offset = offsets[0, si].detach().numpy().squeeze()
                angle = angles[0, si].detach().numpy().squeeze()

                yl, yr = calc_hlr(offsets[0, si], angles[0, si])

                all_offsets += [-offset.copy()]
                all_angles += [angle.copy()]

                offset += 0.5
                offset *= height

                true_mp = np.array([width/2., offset])
                true_nv = np.array([np.sin(angle), np.cos(angle)])
                true_hl = np.array([true_nv[0], true_nv[1], -np.dot(true_nv, true_mp)])
                true_h1 = np.cross(true_hl, np.array([1, 0, 0]))
                true_h2 = np.cross(true_hl, np.array([1, 0, -width]))
                true_h1 /= true_h1[2]
                true_h2 /= true_h2[2]

                h1_ = true_h1/scale - np.array([padding[0], padding[2], 1/scale-1])
                h2_ = true_h2/scale - np.array([padding[0], padding[2], 1/scale-1])
                h_ = np.cross(h1_, h2_)
                Gt = K.T * np.matrix(h_).T
                Gt /= np.linalg.norm(Gt)

                plt.subplots_adjust(left=0, bottom=0, right=1, top=1, wspace=None, hspace=None)

                if checkpoint_path is not None:
                    if not whole_sequence:
                        output_offsets, output_angles = model(images[:,si,:,:,:].unsqueeze(1).to(device))
                        offset_estm = output_offsets[0,fov_increase].cpu().detach().numpy().squeeze()
                        angle_estm = output_angles[0,fov_increase].cpu().detach().numpy().squeeze()

                        yle, yre = calc_hlr(output_offsets[0, fov_increase], output_angles[0, fov_increase])
                    else:
                        offset_estm = output_offsets[0,si+fov_increase].cpu().detach().numpy().squeeze()
                        angle_estm = output_angles[0,si+fov_increase].cpu().detach().numpy().squeeze()

                        yle, yre = calc_hlr(output_offsets[0, si+fov_increase], output_angles[0, si+fov_increase])


                    all_offsets_estm += [-offset_estm.copy()]
                    all_angles_estm += [angle_estm.copy()]

                    offset_estm += 0.5
                    offset_estm *= height

                    estm_mp = np.array([width/2., offset_estm])
                    estm_nv = np.array([np.sin(angle_estm), np.cos(angle_estm)])
                    estm_hl = np.array([estm_nv[0], estm_nv[1], -np.dot(estm_nv, estm_mp)])
                    estm_h1 = np.cross(estm_hl, np.array([1, 0, 0]))
                    estm_h2 = np.cross(estm_hl, np.array([1, 0, -width]))
                    estm_h1 /= estm_h1[2]
                    estm_h2 /= estm_h2[2]

                    h1_ = estm_h1 / scale - np.array([padding[0], padding[2], 1 / scale - 1])
                    h2_ = estm_h2 / scale - np.array([padding[0], padding[2], 1 / scale - 1])
                    h_ = np.cross(h1_, h2_)
                    Ge = K.T * np.matrix(h_).T
                    Ge /= np.linalg.norm(Ge)

                    G = np.matrix(Gs[si]).T
                    G /= np.linalg.norm(G)

                    err1 = (yl-yle)
                    err2 = (yr-yre)

                    if np.abs(err1) > np.abs(err2):
                        err = err1
                    else:
                        err = err2

                    all_errors.append(err)
                    all_errors_per_sequence.append(err)

                    try:
                        angular_error = np.abs((np.arccos(np.clip(np.abs(np.dot(Ge.T, G)), 0, 1))*180/np.pi)[0,0])
                    except:
                        angular_error = 0
                        print(Ge)
                        print(G)
                        print(np.dot(Ge.T, G))
                        exit(0)

                    all_angular_errors.append(angular_error)
                    all_angular_errors_per_sequence.append(angular_error)

                if args.video:
                    image[:,:,0] += pixel_mean[0]
                    image[:,:,1] += pixel_mean[1]
                    image[:,:,2] += pixel_mean[2]


                if args.video:
                    plt.imshow(image)
                    plt.axis('off')
                    plt.autoscale(False)

                    l1.set_data([true_h1[0], true_h2[0]], [true_h1[1], true_h2[1]])
                    if checkpoint_path is not None:
                        l2.set_data([estm_h1[0], estm_h2[0]], [estm_h1[1], estm_h2[1]])
                        plt.suptitle("true: %.1f px, %.1f deg --- error: %.1f px, %.1f deg" %
                                     (offset, angle*180./np.pi, np.abs(offset-offset_estm),
                                      np.abs(angle-angle_estm)*180./np.pi), family='monospace', y=0.9)
                    else:
                        plt.suptitle("%.1f px, %.1f deg" %
                                     (offset, angle*180./np.pi), family='monospace', y=0.9)

                    writer.grab_frame()

        if args.video:
            plt.close()


        if checkpoint_path is not None:
            mean_err = np.mean(all_errors_per_sequence)
            stdd_err = np.std(all_errors_per_sequence)
            mean_abserr = np.mean(np.abs(all_errors_per_sequence))
            stdd_abserr = np.std(np.abs(all_errors_per_sequence))
            max_err = np.max(np.abs(all_errors_per_sequence))

            perc_values = np.percentile(np.abs(all_errors_per_sequence), percs)
            for pv in perc_values: print("%.3f " % pv, end="")
            perc_values = np.percentile(np.abs(all_angular_errors_per_sequence), percs)
            print(" | ")
            for pv in perc_values: print("%.3f " % pv, end="")
            print("")

            plt.figure()
            x = np.arange(0, len(all_offsets))
            all_offsets = np.array(all_offsets)
            all_offsets_estm = np.array(all_offsets_estm)

            corr = np.correlate(all_offsets, all_offsets_estm, "same")
            max_corr_off = np.max(corr)

            error_gradient = np.gradient(all_errors_per_sequence)
            abs_error_grad = np.sum(np.abs(error_gradient)) / len(all_errors_per_sequence)
            print("abs_error_grad: %.9f" % abs_error_grad)
            error_grads += [error_gradient]

        plt.plot(x, all_offsets, '-', c='#99C000')
        if checkpoint_path is not None:
            plt.plot(x, all_offsets_estm, '-', c='#0083CC')
        plt.ylim(-.4, .4)

        if checkpoint_path is not None:
            errors = np.abs(all_offsets-all_offsets_estm)
            err_mean = np.mean(errors).squeeze()
            err_stdd = np.std(errors).squeeze()
            plt.suptitle("mean: %.4f - stdd: %.4f | mean, std, absmean, absstd: %.3f %.3f %.3f %.3f %.3f | corr: %.3f" %
                         (err_mean, err_stdd, mean_err, stdd_err, mean_abserr, stdd_abserr, max_err, max_corr_off), fontsize=8)


        plt.savefig(os.path.join(png_folder, "offsets_%03d.png" % idx), dpi=300)
        plt.savefig(os.path.join(svg_folder, "offsets_%03d.svg" % idx), dpi=300)
        plt.close()

        plt.figure()
        x = np.arange(0, len(all_angles))
        all_angles = np.array(all_angles)
        all_angles_estm = np.array(all_angles_estm)

        corr = np.correlate(all_angles, all_angles_estm, "same")
        max_corr_ang = np.max(corr)
        print("mean, std, absmean, absstd, max: %.3f %.3f %.3f %.3f %.3f | corr: %.3f, %.3f" %
              (mean_err, stdd_err, mean_abserr, stdd_abserr, max_err, max_corr_off, max_corr_ang))

        # print(all_offsets)
        plt.plot(x, all_angles, '-', c='#99C000')
        if checkpoint_path is not None:
            plt.plot(x, all_angles_estm, '-', c='#0083CC')
        plt.ylim(-.4, .4)

        if checkpoint_path is not None:
            errors = np.abs(all_angles-all_angles_estm)
            err_mean = np.mean(errors).squeeze()
            err_stdd = np.std(errors).squeeze()
            plt.suptitle("mean: %.4f - stdd: %.4f | mean, std, absmean, absstd: %.3f %.3f %.3f %.3f %.3f | corr: %.3f" %
                         (err_mean, err_stdd, mean_err, stdd_err, mean_abserr, stdd_abserr, max_err, max_corr_ang), fontsize=8)

        plt.savefig(os.path.join(png_folder, "angles_%03d.png" % idx), dpi=300)
        plt.savefig(os.path.join(svg_folder, "angles_%03d.svg" % idx), dpi=300)
        plt.close()

print("%d images " % image_count)

mean_err = np.mean(all_errors)
stdd_err = np.std(all_errors)
mean_abserr = np.mean(np.abs(all_errors))
stdd_abserr = np.std(np.abs(all_errors))
max_err = np.max(np.abs(all_errors))

perc_values = np.percentile(np.abs(all_errors), percs)
for pv in perc_values: print("%.3f " % pv, end="")
print(" | ")
perc_values = np.percentile(np.abs(all_angular_errors), percs)
for pv in perc_values: print("%.3f " % pv, end="")
print("")

print("total: mean, std, absmean, absstd, max: %.3f %.3f %.3f %.3f %.3f" %
      (mean_err, stdd_err, mean_abserr, stdd_abserr, max_err))

error_grads = np.concatenate(error_grads)
abs_error_grad = np.sum(np.abs(error_grads)) / error_grads.shape[0]
print("abs_error_grad: %.9f" % abs_error_grad)

error_arr = np.abs(np.array(all_errors))
MSE = np.mean(np.square(error_arr))
print("MSE: %.8f" % MSE)
auc, plot_points = calc_auc(error_arr, cutoff=0.25)
print("auc: ", auc)
print("mean error: ", np.mean(error_arr))

plt.figure()
plt.plot(plot_points[:,0], plot_points[:,1], 'b-')
plt.xlim(0, 0.25)
plt.ylim(0, 1.0)
plt.text(0.175, 0.05, "AUC: %.8f" % auc, fontsize=12)
plt.suptitle("mean, std, absmean, absstd: %.3f %.3f %.3f %.3f %.3f" %
             (mean_err, stdd_err, mean_abserr, stdd_abserr, max_err), fontsize=10)
plt.savefig(os.path.join(png_folder, "error_histogram.png"), dpi=300)
plt.savefig(os.path.join(svg_folder, "error_histogram.svg"), dpi=300)

print("angular errors:")
error_arr = np.abs(np.array(all_angular_errors))
auc, plot_points = calc_auc(error_arr, cutoff=5)
print("auc: ", auc)
print("mean error: ", np.mean(error_arr))

plt.figure()
plt.plot(plot_points[:,0], plot_points[:,1], 'b-')
plt.xlim(0, 5)
plt.ylim(0, 1.0)
plt.text(0.175, 0.05, "AUC: %.8f" % auc, fontsize=12)
plt.suptitle("mean, std, absmean, absstd: %.3f %.3f %.3f %.3f %.3f" %
             (mean_err, stdd_err, mean_abserr, stdd_abserr, max_err), fontsize=10)
plt.savefig(os.path.join(png_folder, "error_histogram_angular.png"), dpi=300)
plt.savefig(os.path.join(svg_folder, "error_histogram_angular.svg"), dpi=300)
