# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on 6 Jan 2023
@author: vgopakum
FNO modelled over the MHD data built using JOREK for multi-blob diffusion.

Multivariable FNO
"""
# %%
configuration = {"Case": 'Isothermal Blob',
                 "Field": 'rho, Phi',
                 "Field_Mixing": 'Channel',
                 "Type": '2D Time',
                 "Epochs": 500,
                 "Batch Size": 4,
                 "Optimizer": 'Adam',
                 "Learning Rate": 0.001,
                 "Scheduler Step": 100,
                 "Scheduler Gamma": 0.5,
                 "Activation": 'GELU',
                 "Normalisation Strategy": 'Min-Max. Same',
                 "Instance Norm": 'No',
                 "Log Normalisation": 'No',
                 "Physics Normalisation": 'Yes',
                 "T_in": 10,
                 "T_out": 40,
                 "Step":5,
                 "Modes": 16,
                 "Width_time": 32,  # FNO
                 "Width_vars": 0,  # U-Net
                 "Variables": 2,
                 "Noise": 0.0,
                 "Loss Function": 'LP Loss',
                 "Spatial Resolution": 1,
                 "Temporal Resolution": 1,
                 "Gradient Clipping Norm": None,
                 #  "UQ": 'Dropout',
                 #  "Dropout Rate": 0.9
                 }

# %%
from simvue import Run
run = Run()
run.init(folder="/FNO_MHD/pre_IAEA", tags=['Isothermal', 'Single-Blob', 'MultiVariable', "Z_Li", "Skip-connect", "Recon", "Finals", "Normalisation-test_train"], metadata=configuration)

# %%
import os
CODE = ['FNO_multiple_zl.py']

# Save code files
for code_file in CODE:
    if os.path.isfile(code_file):
        run.save(code_file, 'code')
    elif os.path.isdir(code_file):
        run.save_directory(code_file, 'code', 'text/plain', preserve_path=True)
    else:
        print('ERROR: code file %s does not exist' % code_file)


# %%

import numpy as np
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F

import matplotlib.pyplot as plt
import matplotlib
# matplotlib.use('TkAgg')
from matplotlib import cm

import operator
from functools import reduce
from functools import partial
from collections import OrderedDict

import time
from timeit import default_timer
from tqdm import tqdm

torch.manual_seed(0)
np.random.seed(0)

# %%
#Setting up the directories - data location, model location and plots. 
path = os.getcwd()
data_loc = os.path.dirname(os.path.dirname(os.path.dirname(os.getcwd())))
# model_loc = os.path.dirname(os.path.dirname(os.getcwd()))
file_loc = os.getcwd()

#Setting up CUDA
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# %%
##################################
# Normalisation Functions
##################################


# normalization, pointwise gaussian
class UnitGaussianNormalizer(object):
    def __init__(self, x, eps=0.01):
        super(UnitGaussianNormalizer, self).__init__()

        # x could be in shape of ntrain*n or ntrain*T*n or ntrain*n*T
        self.mean = torch.mean(x, 0)
        self.std = torch.std(x, 0)
        self.eps = eps

    def encode(self, x):
        x = (x - self.mean) / (self.std + self.eps)
        return x

    def decode(self, x, sample_idx=None):
        if sample_idx is None:
            std = self.std + self.eps  # n
            mean = self.mean
        else:
            if len(self.mean.shape) == len(sample_idx[0].shape):
                std = self.std[sample_idx] + self.eps  # batch*n
                mean = self.mean[sample_idx]
            if len(self.mean.shape) > len(sample_idx[0].shape):
                std = self.std[:, sample_idx] + self.eps  # T*batch*n
                mean = self.mean[:, sample_idx]

        # x is in shape of batch*n or T*batch*n
        x = (x * std) + mean
        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()


# normalization, Gaussian
class GaussianNormalizer(object):
    def __init__(self, x, eps=0.01):
        super(GaussianNormalizer, self).__init__()

        self.mean = torch.mean(x)
        self.std = torch.std(x)
        self.eps = eps

    def encode(self, x):
        x = (x - self.mean) / (self.std + self.eps)
        return x

    def decode(self, x, sample_idx=None):
        x = (x * (self.std + self.eps)) + self.mean
        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()


# normalization, scaling by range
class RangeNormalizer(object):
    def __init__(self, x, low=-1.0, high=1.0):
        super(RangeNormalizer, self).__init__()
        mymin = torch.min(x, 0)[0].view(-1)
        mymax = torch.max(x, 0)[0].view(-1)

        self.a = (high - low) / (mymax - mymin)
        self.b = -self.a * mymax + high

    def encode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = self.a * x + self.b
        x = x.view(s)
        return x

    def decode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = (x - self.b) / self.a
        x = x.view(s)
        return x

    def cuda(self):
        self.a = self.a.cuda()
        self.b = self.b.cuda()

    def cpu(self):
        self.a = self.a.cpu()
        self.b = self.b.cpu()


# # normalization, rangewise but single value.
# class MinMax_Normalizer(object):
#     def __init__(self, x, low=0.0, high=1.0):
#         super(MinMax_Normalizer, self).__init__()
#         min_u = torch.min(x[:, 0, :, :, :])
#         max_u = torch.max(x[:, 0, :, :, :])

#         self.a_u = (high - low) / (max_u - min_u)
#         self.b_u = -self.a_u * max_u + high

#         min_v = torch.min(x[:, 1, :, :, :])
#         max_v = torch.max(x[:, 1, :, :, :])

#         self.a_v = (high - low) / (max_v - min_v)
#         self.b_v = -self.a_v * max_v + high

#         # min_p = torch.min(x[:, 2, :, :, :])
#         # max_p = torch.max(x[:, 2, :, :, :])

#         # self.a_p = (high - low) / (max_p - min_p)
#         # self.b_p = -self.a_p * max_p + high

#         print(min_u, max_u, min_v, max_v)

#     def encode(self, x):
#         s = x.size()

#         u = x[:, 0, :, :, :]
#         u = self.a_u * u + self.b_u

#         v = x[:, 1, :, :, :]
#         v = self.a_v * v + self.b_v

#         # p = x[:,2,:,:,:]
#         # p = self.a_p*p + self.b_p
        
#         # x = torch.stack((u,v,p), dim=1)
#         x = torch.stack((u,v), dim=1)

#         return x

#     def decode(self, x):
#         s = x.size()

#         u = x[:,0,:,:,:]
#         u = (u - self.b_u)/self.a_u
        
#         v = x[:,1,:,:,:]
#         v = (v - self.b_v)/self.a_v

#         # p = x[:,2,:,:,:]
#         # p = (p - self.b_p)/self.a_p
#         # x = torch.stack((u,v,p), dim=1)
#         x = torch.stack((u,v), dim=1)

#         return x

#     def cuda(self):
#         self.a_u = self.a_u.cuda()
#         self.b_u = self.b_u.cuda()

#         self.a_v = self.a_v.cuda()
#         self.b_v = self.b_v.cuda()

#         # self.a_p = self.a_p.cuda()
#         # self.b_p = self.b_p.cuda()

#     def cpu(self):
#         self.a_u = self.a_u.cpu()
#         self.b_u = self.b_u.cpu()

#         self.a_v = self.a_v.cpu()
#         self.b_v = self.b_v.cpu()

#         # self.a_p = self.a_p.cpu()
#         # self.b_p = self.b_p.cpu()

class LogNormalizer(object):
    def __init__(self, x,  low=0.0, high=1.0, eps=0.01):
        super(LogNormalizer, self).__init__()

        # x could be in shape of ntrain*n or ntrain*T*n or ntrain*n*T
        min_u = torch.min(x[:, 0, :, :, :])
        max_u = torch.max(x[:, 0, :, :, :])

        self.a_u = (high - low) / (max_u - min_u)
        self.b_u = -self.a_u * max_u + high

        min_v = torch.min(x[:, 1, :, :, :])
        max_v = torch.max(x[:, 1, :, :, :])

        self.a_v = (high - low) / (max_v - min_v)
        self.b_v = -self.a_v * max_v + high

        # min_p = torch.min(x[:, 2, :, :, :])
        # max_p = torch.max(x[:, 2, :, :, :])

        # self.a_p = (high - low) / (max_p - min_p)
        # self.b_p = -self.a_p * max_p + high

        self.eps = eps

    def encode(self, x):
        u = x[:, 0, :, :, :]
        u = self.a_u * u + self.b_u

        v = x[:, 1, :, :, :]
        v = self.a_v * v + self.b_v

        # p = x[:, 2, :, :, :]
        # p = self.a_p * p + self.b_p

        x = torch.stack((u, v), dim=1)

        x = torch.log(x + 1 + self.eps)

        return x

    def decode(self, x):
        x = torch.exp(x) - 1 - self.eps

        u = x[:, 0, :, :, :]
        u = (u - self.b_u) / self.a_u

        v = x[:, 1, :, :, :]
        v = (v - self.b_v) / self.a_v

        # p = x[:, 2, :, :, :]
        # p = (p - self.b_p) / self.a_p

        x = torch.stack((u, v), dim=1)

        return x

    def cuda(self):
        self.a_u = self.a_u.cuda()
        self.b_u = self.b_u.cuda()

        self.a_v = self.a_v.cuda()
        self.b_v = self.b_v.cuda()

        # self.a_p = self.a_p.cuda()
        # self.b_p = self.b_p.cuda()

    def cpu(self):
        self.a_u = self.a_u.cpu()
        self.b_u = self.b_u.cpu()

        self.a_v = self.a_v.cpu()
        self.b_v = self.b_v.cpu()

        # self.a_p = self.a_p.cpu()
        # self.b_p = self.b_p.cpu()


#normalization, rangewise but across the full domain 
class MinMax_Normalizer(object):
    def __init__(self, x, low=-1.0, high=1.0):
        super(MinMax_Normalizer, self).__init__()
        mymin = torch.min(x)
        mymax = torch.max(x)

        self.a = (high - low)/(mymax - mymin)
        self.b = -self.a*mymax + high

    def encode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = self.a*x + self.b
        x = x.view(s)
        return x

    def decode(self, x):
        s = x.size()
        x = x.reshape(s[0], -1)
        x = (x - self.b)/self.a
        x = x.view(s)
        return x

    def cuda(self):
        self.a = self.a.cuda()
        self.b = self.b.cuda()

    def cpu(self):
        self.a = self.a.cpu()
        self.b = self.b.cpu()

# %%
##################################
# Loss Functions
##################################

# loss function with rel/abs Lp loss
class LpLoss(object):
    def __init__(self, d=2, p=2, size_average=True, reduction=True):
        super(LpLoss, self).__init__()

        # Dimension and Lp-norm type are postive
        assert d > 0 and p > 0

        self.d = d
        self.p = p
        self.reduction = reduction
        self.size_average = size_average

    def abs(self, x, y):
        num_examples = x.size()[0]

        # Assume uniform mesh
        h = 1.0 / (x.size()[1] - 1.0)

        all_norms = (h ** (self.d / self.p)) * torch.norm(x.view(num_examples, -1) - y.view(num_examples, -1), self.p,
                                                          1)

        if self.reduction:
            if self.size_average:
                return torch.mean(all_norms)
            else:
                return torch.sum(all_norms)

        return all_norms

    def rel(self, x, y):
        num_examples = x.size()[0]

        diff_norms = torch.norm(x.reshape(num_examples, -1) - y.reshape(num_examples, -1), self.p, 1)
        y_norms = torch.norm(y.reshape(num_examples, -1), self.p, 1)

        if self.reduction:
            if self.size_average:
                return torch.mean(diff_norms / y_norms)
            else:
                return torch.sum(diff_norms / y_norms)

        return diff_norms / y_norms

    def __call__(self, x, y):
        return self.rel(x, y)

# class HsLoss(object):
#     def __init__(self, d=2, p=2, k=1, a=None, group=False, size_average=True, reduction=True):
#         super(HsLoss, self).__init__()

#         #Dimension and Lp-norm type are postive
#         assert d > 0 and p > 0

#         self.d = d
#         self.p = p
#         self.k = k
#         self.balanced = group
#         self.reduction = reduction
#         self.size_average = size_average

#         if a == None:
#             a = [1,] * k
#         self.a = a

#     def rel(self, x, y):
#         num_examples = x.size()[0]
#         diff_norms = torch.norm(x.reshape(num_examples,-1) - y.reshape(num_examples,-1), self.p, 1)
#         y_norms = torch.norm(y.reshape(num_examples,-1), self.p, 1)
#         if self.reduction:
#             if self.size_average:
#                 return torch.mean(diff_norms/y_norms)
#             else:
#                 return torch.sum(diff_norms/y_norms)
#         return diff_norms/y_norms

#     def __call__(self, x, y, a=None):
#         nx = x.size()[1]
#         ny = x.size()[2]
#         k = self.k
#         balanced = self.balanced
#         a = self.a
#         x = x.view(x.shape[0], nx, ny, -1)
#         y = y.view(y.shape[0], nx, ny, -1)

#         k_x = torch.cat((torch.arange(start=0, end=nx//2, step=1),torch.arange(start=-nx//2, end=0, step=1)), 0).reshape(nx,1).repeat(1,ny)
#         k_y = torch.cat((torch.arange(start=0, end=ny//2, step=1),torch.arange(start=-ny//2, end=0, step=1)), 0).reshape(1,ny).repeat(nx,1)
#         k_x = torch.abs(k_x).reshape(1,nx,ny,1).to(x.device)
#         k_y = torch.abs(k_y).reshape(1,nx,ny,1).to(x.device)

#         x = torch.fft.fftn(x, dim=[1, 2])
#         y = torch.fft.fftn(y, dim=[1, 2])

#         if balanced==False:
#             weight = 1
#             if k >= 1:
#                 weight += a[0]**2 * (k_x**2 + k_y**2)
#             if k >= 2:
#                 weight += a[1]**2 * (k_x**4 + 2*k_x**2*k_y**2 + k_y**4)
#             weight = torch.sqrt(weight)
#             loss = self.rel(x*weight, y*weight)
#         else:
#             loss = self.rel(x, y)
#             if k >= 1:
#                 weight = a[0] * torch.sqrt(k_x**2 + k_y**2)
#                 loss += self.rel(x*weight, y*weight)
#             if k >= 2:
#                 weight = a[1] * torch.sqrt(k_x**4 + 2*k_x**2*k_y**2 + k_y**4)
#                 loss += self.rel(x*weight, y*weight)
#             loss = loss / (k+1)

#         return loss

# %%
# Extracting the configuration settings

modes = configuration['Modes']
width_time = configuration['Width_time']
width_vars = configuration['Width_vars']
output_size = configuration['Step']

batch_size = configuration['Batch Size']

batch_size2 = batch_size

t1 = default_timer()

T_in = configuration['T_in']
T = configuration['T_out']
step = configuration['Step']
num_vars = configuration['Variables']

# %%
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')




# %%
################################################################
# fourier layer
################################################################
class SpectralConv2d(nn.Module):
    def __init__(self, in_channels, out_channels, modes1, modes2):
        super(SpectralConv2d, self).__init__()

        """
        2D Fourier layer. It does FFT, linear transform, and Inverse FFT.    
        """

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.modes1 = modes1  # Number of Fourier modes to multiply, at most floor(N/2) + 1
        self.modes2 = modes2

        self.scale = (1 / (in_channels))
        self.weights1 = nn.Parameter(
            self.scale * torch.rand(in_channels, out_channels, num_vars, self.modes1, self.modes2, dtype=torch.cfloat))
        self.weights2 = nn.Parameter(
            self.scale * torch.rand(in_channels, out_channels, num_vars, self.modes1, self.modes2, dtype=torch.cfloat))

    # Complex multiplication
    def compl_mul2d(self, input, weights):
        # (batch, in_channel, x,y ), (in_channel, out_channel, x,y) -> (batch, out_channel, x,y)
        return torch.einsum("bivxy,iovxy->bovxy", input, weights)

    def forward(self, x):
        batchsize = x.shape[0]
        # Compute Fourier coeffcients up to factor of e^(- something constant)
        x_ft = torch.fft.rfft2(x)

        # Multiply relevant Fourier modes
        out_ft = torch.zeros(batchsize, self.out_channels, num_vars, x.size(-2), x.size(-1) // 2 + 1,
                             dtype=torch.cfloat, device=x.device)
        out_ft[:, :, :, :self.modes1, :self.modes2] = \
            self.compl_mul2d(x_ft[:, :, :, :self.modes1, :self.modes2], self.weights1)
        out_ft[:, :, :, -self.modes1:, :self.modes2] = \
            self.compl_mul2d(x_ft[:, :, :, -self.modes1:, :self.modes2], self.weights2)

        # Return to physical space
        x = torch.fft.irfft2(out_ft, s=(x.size(-2), x.size(-1)))
        return x

class MLP(nn.Module):
    def __init__(self, in_channels, out_channels, mid_channels):
        super(MLP, self).__init__()
        self.mlp1 = nn.Conv3d(in_channels, mid_channels, 1)
        self.mlp2 = nn.Conv3d(mid_channels, out_channels, 1)

    def forward(self, x):
        x = self.mlp1(x)
        x = F.gelu(x)
        x = self.mlp2(x)
        return x


class FNO2d(nn.Module):
    def __init__(self, modes1, modes2, width):
        super(FNO2d, self).__init__()

        self.modes1 = modes1
        self.modes2 = modes2
        self.width = width

        self.conv = SpectralConv2d(self.width, self.width, self.modes1, self.modes2)
        self.mlp = MLP(self.width, self.width, self.width)
        self.w = nn.Conv3d(self.width, self.width, 1)
        self.b = nn.Conv3d(2, self.width, 1)

    def forward(self, x, grid):
        x1 = self.conv(x)
        x1 = self.mlp(x1)
        x2 = self.w(x)
        x3 = self.b(grid)
        x = x1 + x2 + x3
        x = F.gelu(x)
        return x

# %%



class FNO_multi(nn.Module):
    def __init__(self, modes1, modes2, width_vars, width_time):
        super(FNO_multi, self).__init__()

        """
        The overall network. It contains 4 layers of the Fourier layer.
        1. Lift the input to the desire channel dimension by self.fc0 .
        2. 4 layers of the integral operators u' = (W + K)(u).
            W defined by self.w; K defined by self.conv .
        3. Project from the channel space to the output space by self.fc1 and self.fc2 .

        input: the solution of the previous T_in timesteps + 2 locations (u(t-T_in, x, y), ..., u(t-1, x, y),  x, y)
        input shape: (batchsize, x=x_discretistion, y=y_discretisation, c=T_in)
        output: the solution of the next timestep
        output shape: (batchsize, x=x_discretisation, y=y_discretisatiob, c=step)
        """

        self.modes1 = modes1
        self.modes2 = modes2
        self.width_vars = width_vars
        self.width_time = width_time

        self.fc0_time = nn.Linear(T_in + 2, self.width_time)

        # self.padding = 8 # pad the domain if input is non-periodic

        self.f0 = FNO2d(self.modes1, self.modes2, self.width_time)
        self.f1 = FNO2d(self.modes1, self.modes2, self.width_time)
        self.f2 = FNO2d(self.modes1, self.modes2, self.width_time)
        self.f3 = FNO2d(self.modes1, self.modes2, self.width_time)
        self.f4 = FNO2d(self.modes1, self.modes2, self.width_time)
        self.f5 = FNO2d(self.modes1, self.modes2, self.width_time)

        # self.norm = nn.InstanceNorm2d(self.width)
        self.norm = nn.Identity()

        self.fc1_time = nn.Linear(self.width_time, 256)
        self.fc2_time = nn.Linear(256, step)

    def forward(self, x):
        grid = self.get_grid(x.shape, x.device)
        x = torch.cat((x, grid), dim=-1)

        x = self.fc0_time(x)
        x = x.permute(0, 4, 1, 2, 3)
        grid = grid.permute(0, 4, 1, 2, 3)
        # x = self.dropout(x)

        # x = F.pad(x, [0,self.padding, 0,self.padding]) # pad the domain if input is non-periodic

        x0 = self.f0(x, grid)
        x = self.f1(x0, grid)
        x = self.f2(x, grid) + x0
        x1 = self.f3(x, grid)
        x = self.f4(x1, grid)
        x = self.f5(x, grid) + x1
        # x = self.dropout(x)

        # x = x[..., :-self.padding, :-self.padding] # pad the domain if input is non-periodic

        x = x.permute(0, 2, 3, 4, 1)
        x = x

        x = self.fc1_time(x)
        x = F.gelu(x)
        # x = self.dropout(x)
        x = self.fc2_time(x)

        return x

    # Using x and y values from the simulation discretisation
    def get_grid(self, shape, device):
        batchsize, num_vars, size_x, size_y = shape[0], shape[1], shape[2], shape[3]
        gridx = torch.tensor(np.linspace(9.5, 10.5, size_x), dtype=torch.float)
        gridx = gridx.reshape(1, 1, size_x, 1, 1).repeat([batchsize, num_vars, 1, size_y, 1])
        gridy = torch.tensor(np.linspace(-0.5, 0.5, size_y), dtype=torch.float)
        gridy = gridy.reshape(1, 1, 1, size_y, 1).repeat([batchsize, num_vars, size_x, 1, 1])
        return torch.cat((gridx, gridy), dim=-1).to(device)

    ## Arbitrary grid discretisation
    # def get_grid(self, shape, device):
    #     batchsize, size_x, size_y = shape[0], shape[1], shape[2]
    #     gridx = torch.tensor(np.linspace(0, 1, size_x), dtype=torch.float)
    #     gridx = gridx.reshape(1, size_x, 1, 1).repeat([batchsize, 1, size_y, 1])
    #     gridy = torch.tensor(np.linspace(0, 1, size_y), dtype=torch.float)
    #     gridy = gridy.reshape(1, 1, size_y, 1).repeat([batchsize, size_x, 1, 1])
    #     return torch.cat((gridx, gridy), dim=-1).to(device)

    def count_params(self):
        c = 0
        for p in self.parameters():
            c += reduce(operator.mul, list(p.size()))

        return c

# %%

################################################################
# Loading Data
################################################################

# %%
data =  np.load(data_loc + '/Data/MHD_isothermal_blob.npz')

# %%
field = configuration['Field']
dims = ['rho', 'Phi']
num_vars = configuration['Variables']

u_sol = data['rho'][:,1:,:,:].astype(np.float32) / 1e20
v_sol = data['Phi'][:,1:,:,:].astype(np.float32) / 1e5
# p_sol = data['w'][:,1:,:,:].astype(np.float32) / 1e1

u_sol = np.nan_to_num(u_sol)
v_sol = np.nan_to_num(v_sol)
# p_sol = np.nan_to_num(p_sol)

u = torch.from_numpy(u_sol)
u = u.permute(0, 2, 3, 1)

v = torch.from_numpy(v_sol)
v = v.permute(0, 2, 3, 1)

# p = torch.from_numpy(p_sol)
# p = p.permute(0, 2, 3, 1)

t_res = configuration['Temporal Resolution']
x_res = configuration['Spatial Resolution']
uvp = torch.stack((u,v), dim=1)[:,::t_res]


x_grid = data['Rgrid'][0,:].astype(np.float32)
y_grid = data['Zgrid'][:,0].astype(np.float32)
t_grid = data['time'].astype(np.float32)

# #Padding Removed
# uvp = uvp[:, :, 3:-3, 3:-3, :]
# x_grid = x_grid[3:-3]
# y_grid = y_grid[3:-3]

ntrain = 100
ntest = 20
S = 100 #Grid Size
size_x = S
size_y = S

batch_size = configuration['Batch Size']

batch_size2 = batch_size

t1 = default_timer()
print(uvp.shape)

train_a = uvp[:ntrain, :, :, :, :T_in]
train_u = uvp[:ntrain, :, :, :, T_in:T + T_in]

test_a = uvp[-ntest:, :, :, :, :T_in]
test_u = uvp[-ntest:, :, :, :, T_in:T + T_in]

print(train_u.shape)
print(test_u.shape)

# %%
a_normalizer = MinMax_Normalizer(uvp[...,:T_in])

train_a = a_normalizer.encode(train_a)
test_a = a_normalizer.encode(test_a)

y_normalizer = MinMax_Normalizer(uvp[...,T_in:T+T_in])

train_u = y_normalizer.encode(train_u)
test_u_encoded = y_normalizer.encode(test_u)

# %%
train_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(train_a, train_u), batch_size=batch_size,
                                           shuffle=True)
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u_encoded), batch_size=batch_size,
                                          shuffle=False)

t2 = default_timer()
print('preprocessing finished, time used:', t2 - t1)

# %%

################################################################
# training and evaluation
################################################################
model = FNO_multi(modes, modes, width_vars, width_time)
model.to(device)

run.update_metadata({'Number of Params': int(model.count_params())})


print("Number of model params : " + str(model.count_params()))

optimizer = torch.optim.Adam(model.parameters(), lr=configuration['Learning Rate'], weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=configuration['Scheduler Step'],
                                            gamma=configuration['Scheduler Gamma'])

myloss = LpLoss(size_average=False)
# myloss = HsLoss(size_average=False)
# %%
epochs = configuration['Epochs']
if torch.cuda.is_available():
    y_normalizer.cuda()

# %%

max_grad_clip_norm = configuration['Gradient Clipping Norm']

start_time = time.time()
for ep in tqdm(range(epochs)):
    model.train()
    t1 = default_timer()
    train_l2_step = 0
    train_l2_full = 0
    for xx, yy in train_loader:
        optimizer.zero_grad()
        loss = 0
        xx = xx.to(device)
        yy = yy.to(device)
        y_old = xx[..., -step:]
        # xx = additive_noise(xx)

        for t in range(0, T, step):
            y = yy[..., t:t + step]
            im = model(xx)
            #Recon Loss
            loss += myloss(im.reshape(batch_size, -1), y.reshape(batch_size, -1))

            #Residual Loss
            pred_diff = im - xx[..., -step:]
            y_diff = y - y_old
            loss += myloss(pred_diff.reshape(batch_size*num_vars, S, S, step), y_diff.reshape(batch_size*num_vars, S, S, step))

            if t == 0:
                pred = im
            else:
                pred = torch.cat((pred, im), -1)

            xx = torch.cat((xx[..., step:], im), dim=-1)
            y_old = y

        train_l2_step += loss.item()
        l2_full = myloss(pred.reshape(batch_size*num_vars, S, S, T), yy.reshape(batch_size*num_vars, S, S, T))
        train_l2_full += l2_full.item()

        loss.backward()
        # torch.nn.utils.clip_grad_norm(parameters=model.parameters(), max_norm=max_grad_clip_norm, norm_type=2.0)

        # l2_full.backward()
        optimizer.step()

    train_loss = train_l2_full / ntrain /num_vars
    train_l2_step = train_l2_step / ntrain / (T / step) /num_vars

    # Validation Loop
    test_loss = 0
    with torch.no_grad():
        for xx, yy in test_loader:
            xx, yy = xx.to(device), yy.to(device)

            for t in range(0, T, step):
                y = yy[..., t:t + step]
                out = model(xx)

                if t == 0:
                    pred = out
                else:
                    pred = torch.cat((pred, out), -1)

                xx = torch.cat((xx[..., step:], out), dim=-1)
            test_loss += myloss(pred.reshape(batch_size*num_vars, S, S, T), yy.reshape(batch_size*num_vars, S, S, T)).item()
        test_loss = test_loss / ntest /num_vars

    t2 = default_timer()

    print('Epochs: %d, Time: %.2f, Train Loss per step: %.3e, Train Loss: %.3e, Test Loss: %.3e' % (
    ep, t2 - t1, train_l2_step, train_loss, test_loss))

    run.log_metrics({'Train Loss': train_loss,
                    'Test Loss': test_loss})

train_time = time.time() - start_time
# %%
#Saving the Model
model_loc = file_loc + '/Models/FNO_multi_blobs_' + run.name + '.pth'
torch.save(model.state_dict(),  model_loc)

# %%
# Testing
batch_size = 1
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u_encoded), batch_size=1,
                                          shuffle=False)
pred_set = torch.zeros(test_u.shape)
index = 0
with torch.no_grad():
    for xx, yy in tqdm(test_loader):
        loss = 0
        xx, yy = xx.to(device), yy.to(device)
        # xx = additive_noise(xx)
        t1 = default_timer()
        for t in range(0, T, step):
            y = yy[..., t:t + step]
            out = model(xx)
            # loss += myloss(out.reshape(batch_size, -1), y.reshape(batch_size, -1))

            if t == 0:
                pred = out
            else:
                pred = torch.cat((pred, out), -1)

            xx = torch.cat((xx[..., step:], out), dim=-1)

        t2 = default_timer()
        # pred = y_normalizer.decode(pred)
        pred_set[index] = pred
        index += 1
        print(t2 - t1)

# %%
print(pred_set.shape, test_u.shape)
# Logging Metrics
MSE_error = (pred_set - test_u_encoded).pow(2).mean()
MAE_error = torch.abs(pred_set - test_u_encoded).mean()
# LP_error = loss / (ntest * T / step)
# rel_error = torch.abs((pred_set - test_u_encoded)/test_u_encoded).mean() * 100 
# nmse = ((pred_set - test_u_encoded).pow(2).mean() / test_u_encoded.pow(2).mean())
# nrmse = torch.sqrt((pred_set - test_u_encoded).pow(2).mean()) / torch.std(test_u_encoded)

print('(MSE) Testing Error: %.3e' % (MSE_error))
print('(MAE) Testing Error: %.3e' % (MAE_error))
# print('(LP) Testing Error: %.3e' % (LP_error))
# print('(MAPE) Testing Error %.3e' % (rel_error))
# print('(NMSE) Testing Error %.3e' % (nmse))
# print('(NRMSE) Testing Error %.3e' % (nrmse))

# %% 
run.update_metadata({'Training Time': float(train_time),
                     'MSE': float(MSE_error),
                     'MAE': float(MAE_error)
                    })

pred_set = y_normalizer.decode(pred_set.to(device)).cpu()

pred_set_encoded = pred_set
pred_set = y_normalizer.decode(pred_set.to(device)).cpu()

nmse= 0 
for ii in range(num_vars):
    nmse += (pred_set[:,ii] - test_u[:,ii]).pow(2).mean() / test_u[:,ii].pow(2).mean()
    print(test_u[:,ii].pow(2).mean())
nmse = nmse/num_vars

print('(NMSE) Testing Error %.3e' % (nmse))
run.update_metadata({'NMSE': float(nmse)})
# %%
# Plotting the comparison plots

idx = np.random.randint(0, ntest)
idx = 3

if configuration['Log Normalisation'] == 'Yes':
    test_u = torch.exp(test_u)
    pred_set = torch.exp(pred_set)

# %%
output_plot = []
for dim in range(num_vars):
    u_field = test_u[idx]

    v_min_1 = torch.min(u_field[dim, :, :, 0])
    v_max_1 = torch.max(u_field[dim, :, :, 0])

    v_min_2 = torch.min(u_field[dim, :, :, int(T / 2)])
    v_max_2 = torch.max(u_field[dim, :, :, int(T / 2)])

    v_min_3 = torch.min(u_field[dim, :, :, -1])
    v_max_3 = torch.max(u_field[dim, :, :, -1])

    fig = plt.figure(figsize=plt.figaspect(0.5))
    ax = fig.add_subplot(2, 3, 1)
    pcm = ax.imshow(u_field[dim, :, :, 0], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_1, vmax=v_max_1)
    # ax.title.set_text('Initial')
    ax.title.set_text('t=' + str(T_in))
    ax.set_ylabel('Solution')
    fig.colorbar(pcm, pad=0.05)

    ax = fig.add_subplot(2, 3, 2)
    pcm = ax.imshow(u_field[dim, :, :, int(T / 2)], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_2,
                    vmax=v_max_2)
    # ax.title.set_text('Middle')
    ax.title.set_text('t=' + str(int((T + T_in) / 2)))
    ax.axes.xaxis.set_ticks([])
    ax.axes.yaxis.set_ticks([])
    fig.colorbar(pcm, pad=0.05)

    ax = fig.add_subplot(2, 3, 3)
    pcm = ax.imshow(u_field[dim, :, :, -1], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_3, vmax=v_max_3)
    # ax.title.set_text('Final')
    ax.title.set_text('t=' + str(T + T_in))
    ax.axes.xaxis.set_ticks([])
    ax.axes.yaxis.set_ticks([])
    fig.colorbar(pcm, pad=0.05)

    u_field = pred_set[idx]

    ax = fig.add_subplot(2, 3, 4)
    pcm = ax.imshow(u_field[dim, :, :, 0], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_1, vmax=v_max_1)
    ax.set_ylabel('FNO')

    fig.colorbar(pcm, pad=0.05)

    ax = fig.add_subplot(2, 3, 5)
    pcm = ax.imshow(u_field[dim, :, :, int(T / 2)], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_2,
                    vmax=v_max_2)
    ax.axes.xaxis.set_ticks([])
    ax.axes.yaxis.set_ticks([])
    fig.colorbar(pcm, pad=0.05)

    ax = fig.add_subplot(2, 3, 6)
    pcm = ax.imshow(u_field[dim, :, :, -1], cmap=cm.coolwarm, extent=[9.5, 10.5, -0.5, 0.5], vmin=v_min_3, vmax=v_max_3)
    ax.axes.xaxis.set_ticks([])
    ax.axes.yaxis.set_ticks([])
    fig.colorbar(pcm, pad=0.05)

    plt.title(dims[dim])

    output_plot.append(file_loc + '/Plots/MultiBlobs_' + dims[dim] + '_' + run.name + '.png')
    plt.savefig(output_plot[dim])

# %%


# %%

INPUTS = []
OUTPUTS = [model_loc, output_plot[0], output_plot[1]]


# Save input files
for input_file in INPUTS:
    if os.path.isfile(input_file):
        run.save(input_file, 'input')
    elif os.path.isdir(input_file):
        run.save_directory(input_file, 'input', 'text/plain', preserve_path=True)
    else:
        print('ERROR: input file %s does not exist' % input_file)


# Save output files
for output_file in OUTPUTS:
    if os.path.isfile(output_file):
        run.save(output_file, 'output')
    elif os.path.isdir(output_file):
        run.save_directory(output_file, 'output', 'text/plain', preserve_path=True)   
    else:
        print('ERROR: output file %s does not exist' % output_file)

run.close()

# %%
