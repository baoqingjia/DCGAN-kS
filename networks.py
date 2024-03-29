from torch import nn
from torch.autograd import Variable
import torch
import torch.nn.functional as F
import torchvision
from torch.nn.utils import weight_norm
from MetaAconC import MetaAconC
import numpy as np
try:
    from itertools import izip as zip
except ImportError: # will be 3.x series
    pass
import torch.fft as fft

##################################################################################
# Discriminator
##################################################################################

class MsImageDis(nn.Module):
    # Multi-scale discriminator architecture
    def __init__(self, input_dim, params):
        super(MsImageDis, self).__init__()
        self.n_layer = params['n_layer']
        self.gan_type = params['gan_type']
        self.dim = params['dim']
        self.norm = params['norm']
        self.activ = params['activ']
        self.num_scales = params['num_scales']
        self.pad_type = params['pad_type']
        self.input_dim = input_dim
        self.downsample = nn.AvgPool2d(3, stride=2, padding=[1, 1], count_include_pad=False)
        self.cnns = nn.ModuleList()
        for _ in range(self.num_scales):
            self.cnns.append(self._make_net())

    def _make_net(self):
        dim = self.dim
        cnn_x = []
        cnn_x += [Conv2dBlock(self.input_dim, dim, 4, 2, 1, norm='none', activation=self.activ, pad_type=self.pad_type)]
        for _ in range(self.n_layer - 1):
            cnn_x += [Conv2dBlock(dim, dim * 2, 4, 2, 1, norm=self.norm, activation=self.activ, pad_type=self.pad_type)]
            dim *= 2
        cnn_x += [nn.Conv2d(dim, 1, 1, 1, 0)]
        cnn_x = nn.Sequential(*cnn_x)
        return cnn_x

    def forward(self, x):
        outputs = []
        for model in self.cnns:
            outputs.append(model(x))
            x = self.downsample(x)
        return outputs

    def calc_dis_loss(self, input_fake, input_real):
        # calculate the loss to train D
        outs0 = self.forward(input_fake)
        outs1 = self.forward(input_real)
        loss = 0

        for it, (out0, out1) in enumerate(zip(outs0, outs1)):
            if self.gan_type == 'lsgan':
                loss += torch.mean((out0 - 0)**2) + torch.mean((out1 - 1)**2)
            elif self.gan_type == 'nsgan':
                all0 = Variable(torch.zeros_like(out0.data).cuda(), requires_grad=False)
                all1 = Variable(torch.ones_like(out1.data).cuda(), requires_grad=False)
                loss += torch.mean(F.binary_cross_entropy(F.sigmoid(out0), all0) +
                                   F.binary_cross_entropy(F.sigmoid(out1), all1))
            else:
                assert 0, "Unsupported GAN type: {}".format(self.gan_type)
        return loss

    def calc_gen_loss(self, input_fake):
        # calculate the loss to train G
        outs0 = self.forward(input_fake)
        loss = 0
        for it, (out0) in enumerate(outs0):
            if self.gan_type == 'lsgan':
                loss += torch.mean((out0 - 1)**2) # LSGAN
            elif self.gan_type == 'nsgan':
                all1 = Variable(torch.ones_like(out0.data).cuda(), requires_grad=False)
                loss += torch.mean(F.binary_cross_entropy(F.sigmoid(out0), all1))
            else:
                assert 0, "Unsupported GAN type: {}".format(self.gan_type)
        return loss

####################################################################
#--------------------- Spectral Normalization ---------------------
#  This part of code is copied from pytorch master branch (0.5.0)
####################################################################
class SpectralNorm(object):
    def __init__(self, name='weight', n_power_iterations=1, dim=0, eps=1e-12):
        self.name = name
        self.dim = dim
        if n_power_iterations <= 0:
            raise ValueError('Expected n_power_iterations to be positive, but '
                       'got n_power_iterations={}'.format(n_power_iterations))
        self.n_power_iterations = n_power_iterations
        self.eps = eps
    def compute_weight(self, module):
        weight = getattr(module, self.name + '_orig')
        u = getattr(module, self.name + '_u')
        weight_mat = weight
        if self.dim != 0:
        # permute dim to front
            weight_mat = weight_mat.permute(self.dim,
                                            *[d for d in range(weight_mat.dim()) if d != self.dim])
        height = weight_mat.size(0)
        weight_mat = weight_mat.reshape(height, -1)
        with torch.no_grad():
            for _ in range(self.n_power_iterations):
                v = F.normalize(torch.matmul(weight_mat.t(), u), dim=0, eps=self.eps)
                u = F.normalize(torch.matmul(weight_mat, v), dim=0, eps=self.eps)
                sigma = torch.dot(u, torch.matmul(weight_mat, v))
                weight = weight / sigma
            return weight, u
    def remove(self, module):
        weight = getattr(module, self.name)
        delattr(module, self.name)
        delattr(module, self.name + '_u')
        delattr(module, self.name + '_orig')
        module.register_parameter(self.name, torch.nn.Parameter(weight))
    def __call__(self, module, inputs):
        if module.training:
            weight, u = self.compute_weight(module)
            setattr(module, self.name, weight)
            setattr(module, self.name + '_u', u)
        else:
            r_g = getattr(module, self.name + '_orig').requires_grad
            getattr(module, self.name).detach_().requires_grad_(r_g)
    @staticmethod
    def apply(module, name, n_power_iterations, dim, eps):
        fn = SpectralNorm(name, n_power_iterations, dim, eps)
        weight = module._parameters[name]
        height = weight.size(dim)
        u = F.normalize(weight.new_empty(height).normal_(0, 1), dim=0, eps=fn.eps)
        delattr(module, fn.name)
        module.register_parameter(fn.name + "_orig", weight)
        module.register_buffer(fn.name, weight.data)
        module.register_buffer(fn.name + "_u", u)
        module.register_forward_pre_hook(fn)
        return fn
def spectral_norm(module, name='weight', n_power_iterations=1, eps=1e-12, dim=None):
    if dim is None:
        if isinstance(module, (torch.nn.ConvTranspose1d,
                           torch.nn.ConvTranspose2d,
                           torch.nn.ConvTranspose3d)):
            dim = 1
        else:
            dim = 0
    SpectralNorm.apply(module, name, n_power_iterations, dim, eps)
    return module

class LeakyReLUConv2d(nn.Module):
    def __init__(self, n_in, n_out, kernel_size, stride, padding=0, norm='None', sn=False):
        super(LeakyReLUConv2d, self).__init__()
        model = []
        model += [nn.ReflectionPad2d(padding)]
        if sn:
            model += [spectral_norm(nn.Conv2d(n_in, n_out, kernel_size=kernel_size, stride=stride, padding=0, bias=True))]
        else:
            model += [nn.Conv2d(n_in, n_out, kernel_size=kernel_size, stride=stride, padding=0, bias=True)]
        if 'norm' == 'Instance':
            model += [nn.InstanceNorm2d(n_out, affine=False)]
        model += [nn.LeakyReLU(inplace=True)]
        self.model = nn.Sequential(*model)
        # self.model.apply(gaussian_weights_init)
        #elif == 'Group'
    def forward(self, x):
        return self.model(x)
####################################################################
#------------------------- Discriminators --------------------------
####################################################################
class Dis_content(nn.Module):
    def __init__(self):
        super(Dis_content, self).__init__()
        model = []
        model += [LeakyReLUConv2d(256, 256, kernel_size=3, stride=2, padding=1, norm='None')] # Instance
        model += [LeakyReLUConv2d(256, 256, kernel_size=3, stride=2, padding=1, norm='None')]
        model += [LeakyReLUConv2d(256, 256, kernel_size=3, stride=2, padding=1, norm='None')]
        model += [LeakyReLUConv2d(256, 256, kernel_size=3, stride=2, padding=1, norm='None')]
        # model += [LeakyReLUConv2d(256, 256, kernel_size=4, stride=1, padding=0)]
        model += [nn.AdaptiveAvgPool2d(1)]
        model += [nn.Conv2d(256, 1, kernel_size=1, stride=1, padding=0)]
        self.model = nn.Sequential(*model)

    def forward(self, x):
        out = self.model(x)
        out = out.view(-1)
        return out

##################################################################################
# Generator
##################################################################################
class VAEGen(nn.Module):
    # VAE architecture
    def __init__(self, input_dim, params):
        super(VAEGen, self).__init__()
        dim = params['dim']
        n_downsample = params['n_downsample']
        n_res = params['n_res']
        activ = params['activ']
        pad_type = params['pad_type']

        # content encoder
        # Replace traditional instance normalization layer (IN) for image translation with batch normalization (BN). 
        self.enc = ContentEncoder(n_downsample, n_res, input_dim, dim, 'bn', activ, pad_type=pad_type) # replace 'in' with 'bn'
        self.styc = NoiseEncoder(n_downsample, input_dim, dim, self.enc.output_dim, 'bn', activ, pad_type = pad_type) # use similar codes with style encoder
        self.dec_cont = Decoder(n_downsample, n_res, self.enc.output_dim, input_dim, res_norm='bn', activ=activ, pad_type=pad_type) # 'in'
        self.dec_recs = Decoder(n_downsample, n_res, 2 * self.enc.output_dim, input_dim, res_norm='bn', activ=activ, pad_type=pad_type) # 'in'

    def encode_cont(self, images):
        hiddens = self.enc(images)
        return hiddens

    def encode_sty(self, images):
        styhiddens = self.styc(images)
        return styhiddens
    
    def decode_cont(self, hiddens):
        images = self.dec_cont(hiddens)
        return images

    def decode_recs(self, hiddens):
        images = self.dec_recs(hiddens)
        return images

##################################################################################
# Encoder and Decoders
##################################################################################

class NoiseEncoder(nn.Module):
    def __init__(self, n_downsample, input_dim, dim, style_dim, norm, activ, pad_type):
        super(NoiseEncoder, self).__init__()
        self.model = []
        self.model += [Conv2dBlock(input_dim, dim, 7, 1, 3, norm=norm, activation=activ, pad_type=pad_type)]
        for _ in range(2):
            self.model += [Conv2dBlock(dim, 2 * dim, 4, 2, 1, norm=norm, activation=activ, pad_type=pad_type)]
            dim *= 2
        for i in range(n_downsample - 2):
            self.model += [Conv2dBlock(dim, dim, 4, 2, 1, norm=norm, activation=activ, pad_type=pad_type)]
        # self.model += [nn.AdaptiveAvgPool2d(1)] # global average pooling
        self.model += [nn.Conv2d(dim, style_dim, 1, 1, 0)]
        self.model = nn.Sequential(*self.model)
        self.output_dim = dim

    def forward(self, x):
        return self.model(x)

class ContentEncoder(nn.Module):
    def __init__(self, n_downsample, n_res, input_dim, dim, norm, activ, pad_type):
        super(ContentEncoder, self).__init__()
        self.model = []
        self.model += [Conv2dBlock(input_dim, dim, 7, 1, 3, norm=norm, activation=activ, pad_type=pad_type)]
        # downsampling blocks
        for _ in range(n_downsample):
            self.model += [Conv2dBlock(dim, 2 * dim, 4, 2, 1, norm=norm, activation=activ, pad_type=pad_type)]
            dim *= 2
        # residual blocks
        self.model += [ResBlocks(n_res, dim, norm=norm, activation=activ, pad_type=pad_type)]
        self.model = nn.Sequential(*self.model)
        self.output_dim = dim

    def forward(self, x):
        return self.model(x)
    


class Decoder(nn.Module):
    def __init__(self, n_upsample, n_res, dim, output_dim, res_norm='adain', activ='relu', pad_type='zero'):
        super(Decoder, self).__init__()

        self.model = []
        # AdaIN residual blocks
        self.model += [ResBlocks(n_res, dim, res_norm, activ, pad_type=pad_type)]
        # upsampling blocks
        for _ in range(n_upsample):
            self.model += [nn.Upsample(scale_factor=2),
                           Conv2dBlock(dim, dim // 2, 5, 1, 2, norm='ln', activation=activ, pad_type=pad_type)]
            dim //= 2
        # use reflection padding in the last conv layer
        self.model += [Conv2dBlock(dim, output_dim, 7, 1, 3, norm='none', activation='sigmoid', pad_type=pad_type)] # tanh
        # self.model += [Conv2dBlock(dim, output_dim, 7, 1, 3, norm='none', activation='tanh', pad_type=pad_type)] # tanh
        self.model = nn.Sequential(*self.model)

    def forward(self, x):
        return self.model(x)

##################################################################################
# Sequential Models
##################################################################################
class ResBlocks(nn.Module):
    def __init__(self, num_blocks, dim, norm='in', activation='relu', pad_type='zero'):
        super(ResBlocks, self).__init__()
        self.model = []
        for _ in range(num_blocks):
            self.model += [ResBlock(dim, norm=norm, activation=activation, pad_type=pad_type)]
        self.model = nn.Sequential(*self.model)

    def forward(self, x):
        return self.model(x)

class MLP(nn.Module):
    def __init__(self, input_dim, output_dim, dim, n_blk, norm='none', activ='relu'):

        super(MLP, self).__init__()
        self.model = []
        self.model += [LinearBlock(input_dim, dim, norm=norm, activation=activ)]
        for _ in range(n_blk - 2):
            self.model += [LinearBlock(dim, dim, norm=norm, activation=activ)]
        self.model += [LinearBlock(dim, output_dim, norm='none', activation='none')] # no output activations
        self.model = nn.Sequential(*self.model)

    def forward(self, x):
        return self.model(x.view(x.size(0), -1))

##################################################################################
# Basic Blocks
##################################################################################
class ResBlock(nn.Module):
    def __init__(self, dim, norm='in', activation='relu', pad_type='zero'):
        super(ResBlock, self).__init__()

        model = []
        model += [Conv2dBlock(dim ,dim, 3, 1, 1, norm=norm, activation=activation, pad_type=pad_type)]
        model += [Conv2dBlock(dim ,dim, 3, 1, 1, norm=norm, activation='none', pad_type=pad_type)]
        self.model = nn.Sequential(*model)

    def forward(self, x):
        residual = x
        out = self.model(x)
        out += residual
        return out

class Conv2dBlock(nn.Module):
    def __init__(self, input_dim , output_dim, kernel_size, stride,
                 padding=0, norm='none', activation='relu', pad_type='zero'):
        super(Conv2dBlock, self).__init__()
        self.use_bias = True
        # initialize convolution
        self.conv = nn.Conv2d(input_dim, output_dim, kernel_size, stride, bias=self.use_bias)
        # initialize padding
        if pad_type == 'reflect':
            self.pad = nn.ReflectionPad2d(padding)
        elif pad_type == 'replicate':
            self.pad = nn.ReplicationPad2d(padding)
        elif pad_type == 'zero':
            self.pad = nn.ZeroPad2d(padding)
        else:
            assert 0, "Unsupported padding type: {}".format(pad_type)
        self.norm_type = norm
        # initialize normalization
        norm_dim = output_dim
        if norm == 'bn':
            self.norm = nn.BatchNorm2d(norm_dim)
        elif norm == 'in':
            #self.norm = nn.InstanceNorm2d(norm_dim, track_running_stats=True)
            self.norm = nn.InstanceNorm2d(norm_dim)
        elif norm == 'ln':
            self.norm = LayerNorm(norm_dim)
        elif norm == 'adain':
            self.norm = AdaptiveInstanceNorm2d(norm_dim)
        elif norm == 'wn':
            self.conv = weight_norm(self.conv)
        elif norm == 'none':
            self.norm = None
        else:
            assert 0, "Unsupported normalization: {}".format(norm)

        # self.acon = MetaAconC(width = output_dim)

        # initialize activation
        if activation == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif activation == 'lrelu':
            self.activation = nn.LeakyReLU(0.2, inplace=True)
        elif activation == 'prelu':
            self.activation = nn.PReLU()
        elif activation == 'selu':
            self.activation = nn.SELU(inplace=True)
        elif activation == 'tanh':
            self.activation = nn.Tanh()
        elif activation == 'sigmoid':
            self.activation = nn.Sigmoid()
        elif activation == 'none':
            self.activation = None
        else:
            assert 0, "Unsupported activation: {}".format(activation)

    def forward(self, x):
        x = self.conv(self.pad(x))
        if self.norm_type != 'wn' and self.norm != None:
            x = self.norm(x)

        if self.activation:
            x = self.activation(x)
            # x = self.acon(x)
        return x

class LinearBlock(nn.Module):
    def __init__(self, input_dim, output_dim, norm='none', activation='relu'):
        super(LinearBlock, self).__init__()
        use_bias = True
        # initialize fully connected layer
        self.fc = nn.Linear(input_dim, output_dim, bias=use_bias)

        # initialize normalization
        norm_dim = output_dim
        if norm == 'bn':
            self.norm = nn.BatchNorm1d(norm_dim)
        elif norm == 'in':
            self.norm = nn.InstanceNorm1d(norm_dim)
        elif norm == 'ln':
            self.norm = LayerNorm(norm_dim)
        elif norm == 'none':
            self.norm = None
        else:
            assert 0, "Unsupported normalization: {}".format(norm)

        # initialize activation
        if activation == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif activation == 'lrelu':
            self.activation = nn.LeakyReLU(0.2, inplace=True)
        elif activation == 'prelu':
            self.activation = nn.PReLU()
        elif activation == 'selu':
            self.activation = nn.SELU(inplace=True)
        elif activation == 'tanh':
            self.activation = nn.Tanh()
        elif activation == 'none':
            self.activation = None
        else:
            assert 0, "Unsupported activation: {}".format(activation)

    def forward(self, x):
        out = self.fc(x)
        if self.norm:
            out = self.norm(out)
        if self.activation:
            out = self.activation(out)
        return out



class vgg_19(nn.Module):
    def __init__(self):
        super(vgg_19, self).__init__()
        vgg_model = torchvision.models.vgg19(pretrained=True)
        self.feature_ext = nn.Sequential(*list(vgg_model.features.children())[:20])
    def forward(self, x):
        if x.size(1) == 1:
            x = torch.cat((x, x, x), 1)
        out = self.feature_ext(x)
        return out

##################################################################################
# Normalization layers
##################################################################################
class AdaptiveInstanceNorm2d(nn.Module):
    def __init__(self, num_features, eps=1e-5, momentum=0.1):
        super(AdaptiveInstanceNorm2d, self).__init__()
        self.num_features = num_features
        self.eps = eps
        self.momentum = momentum
        # weight and bias are dynamically assigned
        self.weight = None
        self.bias = None
        # just dummy buffers, not used
        self.register_buffer('running_mean', torch.zeros(num_features))
        self.register_buffer('running_var', torch.ones(num_features))

    def forward(self, x):
        assert self.weight is not None and self.bias is not None, "Please assign weight and bias before calling AdaIN!"
        b, c = x.size(0), x.size(1)
        running_mean = self.running_mean.repeat(b)
        running_var = self.running_var.repeat(b)

        # Apply instance norm
        x_reshaped = x.contiguous().view(1, b * c, *x.size()[2:])

        out = F.batch_norm(
            x_reshaped, running_mean, running_var, self.weight, self.bias,
            True, self.momentum, self.eps)

        return out.view(b, c, *x.size()[2:])

    def __repr__(self):
        return self.__class__.__name__ + '(' + str(self.num_features) + ')'


class LayerNorm(nn.Module):
    def __init__(self, num_features, eps=1e-5, affine=True):
        super(LayerNorm, self).__init__()
        self.num_features = num_features
        self.affine = affine
        self.eps = eps

        if self.affine:
            self.gamma = nn.Parameter(torch.Tensor(num_features).uniform_())
            self.beta = nn.Parameter(torch.zeros(num_features))

    def forward(self, x):
        shape = [-1] + [1] * (x.dim() - 1)
        # if x.size(0) == 1:
        #     # These two lines run much faster in pytorch 0.4 than the two lines listed below.
        #     mean = x.view(-1).mean().view(*shape)
        #     std = x.view(-1).std().view(*shape)
        # else:
        mean = x.view(x.size(0), -1).mean(1).view(*shape)
        std = x.view(x.size(0), -1).std(1).view(*shape)

        x = (x - mean) / (std + self.eps)

        if self.affine:
            shape = [1, -1] + [1] * (x.dim() - 2)
            x = x * self.gamma.view(*shape) + self.beta.view(*shape)
        return x

# diff: add random downsampling

##################################################################################
# Mask Generator
##################################################################################
class GenMask(nn.Module):
    def __init__(self, dim, output_dim=4, res_norm='adain', activ='relu', pad_type='zero'):
        super(GenMask, self).__init__()

        self.model = []
        # AdaIN residual blocks
        # use reflection padding in the last conv layer
        for _ in range(2):
            self.model += [
                Conv2dBlock(dim, output_dim, 5, 1, 2, norm='none', activation='relu', pad_type=pad_type),
                nn.MaxPool2d(2)]  # tanh
            dim = output_dim
            output_dim *= 4

        output_dim //= 16

        self.model += [nn.Upsample(scale_factor=2),
            Conv2dBlock(dim, output_dim, 5, 1, 2, norm='none', activation='relu', pad_type=pad_type)]  # tanh

        dim = output_dim
        output_dim //= 4
        self.model += [nn.Upsample(scale_factor=2),
                      Conv2dBlock(dim, output_dim, 5, 1, 2, norm='none', activation='sigmoid', pad_type=pad_type)]  # tanh
        self.model = nn.Sequential(*self.model)

    # 1: max1/3 and (4/9,5/9)
    # def forward(self, x):
    #     x = self.model(x)
    #     mask = torch.zeros_like(x)
    #     x = x.mean(3).view(x.shape[0], x.shape[1], x.shape[2], 1)
    #
    #     # find 1/3 lines and make them 1
    #     num = x.shape[2] *1//3
    #     values, indices = torch.topk(x, num, dim=2, largest=True)
    #     mask[:,:,indices,:] = 1
    #
    #     # make mid 1/9 lines 1
    #     start = x.shape[2] * 4 // 9
    #     end = x.shape[2] * 5 // 9
    #     mask[:,:, start:end, :] = 1
    #     # mask = torch.repeat_interleave(x, 128, dim=3)
    #     return mask.to(torch.complex64)

    # 1: x>0.5 and (4/9,5/9)
    def forward(self, x):
        x = self.model(x)
        mask = torch.zeros_like(x)
        x = x.mean(3).view(x.shape[0], x.shape[1], x.shape[2], 1)

        # normolize
        for i in range(x.shape[0]):
            for j in range(x.shape[1]):
                x_min = torch.min(x[i,j,:,:])
                x_max = torch.max(x[i,j,:,:])
                x[i,j,:,:] = (x[i,j,:,:] - x_min) / (x_max - x_min)

        start = x.shape[2] * 4 // 9
        end = x.shape[2] * 5 // 9
        for i in range(x.shape[0]):
            for j in range(x.shape[1]):
                for k in range(x.shape[2]):
                    if x[i,j,k,0]>0.5 or start<k<end:
                        mask[i,j,k,:] = 1

        # make mid 1/9 lines 1
        # mask[:, :, start:end, :] = 1
        # mask = torch.repeat_interleave(x, 128, dim=3)
        return mask.to(torch.complex64)

def make_mask(inp, R, gpuid):
    """
    Make subsampling mask (1D Cartesian trajectory, Gaussian random sampling)
    :param inp: 3D (HWC) output numpy array
    :param R: integer, downsampling rate
    :return: 3D (HWC) output numpy array
    """
    nY = inp.shape[2]
    nX = inp.shape[3]
    nC = inp.shape[1]
    nB = inp.shape[0]
    mask = np.zeros((nY, nX), dtype=np.complex64)
    masks = np.zeros(inp.shape, dtype=np.complex64)
    # masks = np.ones(inp.shape, dtype=np.complex64)

    for i in range(nB):
        for j in range(nC):
            nACS = round(nY / (R ** 2))
            ACS_s = round((nY - nACS) / 2)
            ACS_e = ACS_s + nACS
            mask[ACS_s:ACS_e, :] = 1

            nSamples = int(nY / R)
            r = np.floor(np.random.normal(nY / 2, 70, nSamples))
            r = np.clip(r.astype(int), 0, nY - 1)
            mask[r.tolist(), :] = 1
            masks[i, j, :, :] = mask



    masks = torch.from_numpy(masks)

    return masks.to(device=gpuid)

# downsampling
def downsampling(img, mask):
    h = img.shape[2]
    w = img.shape[3]
    k_full = fft.fftn(img, dim=(2,3))
    k_full = torch.roll(k_full, (h//2, w//2), dims=(2,3))   # make zero frequency center
    k_down = torch.multiply(k_full, mask)
    k_down = torch.roll(k_down, (h//2, w//2), dims=(2,3))
    img_down = fft.ifftn(k_down, dim=(2,3))

    return img_down.to(torch.float32)

# fft
def img2k(img):
    h = img.shape[2]
    w = img.shape[3]
    k_full = fft.fftn(img, dim=(2, 3))
    k_full = torch.roll(k_full, (h // 2, w // 2), dims=(2, 3))  # make zero frequency center

    return k_full

# ifft
def k2img(k_down):
    h = k_down.shape[2]
    w = k_down.shape[3]
    k_down = torch.roll(k_down, (h // 2, w // 2), dims=(2, 3))
    img_down = fft.ifftn(k_down, dim=(2, 3))

    return img_down.to(torch.float32)