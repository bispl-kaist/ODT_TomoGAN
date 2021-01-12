import torch
import numpy as np
import torch.nn.functional as F
from torch import nn
import random
import matplotlib.pyplot as plt

def to_tensor(data):
    """
    Convert numpy array to PyTorch tensor. For complex arrays, the real and imaginary parts
    are stacked along the last dimension.
    Args:
        data (np.array): Input numpy array
    Returns:
        torch.Tensor: PyTorch version of data
    """
    if np.iscomplexobj(data):
        data = np.stack((data.real, data.imag), axis=-1)
    data = data.astype(np.float32)
    return torch.from_numpy(data)


def apply_mask(data, mask_func, seed=None):

    shape = np.array(data.shape)
    shape[:-3] = 1
    mask, acceleration = mask_func(shape, seed)
    mask = mask.to(data.device)  # Changed this part here for Pre-loading on GPU.
    return data * mask, mask


def apply_info_mask(data, mask_func, seed=None):
    """
    Subsample given k-space by multiplying with a mask.
    Args:
        data (torch.Tensor): The input k-space data. This should have at least 3 dimensions, where
            dimensions -3 and -2 are the spatial dimensions, and the final dimension has size
            2 (for complex values).
        mask_func (callable): A function that takes a shape (tuple of ints) and a random
            number seed and returns a mask and a dictionary containing information about the masking.
        seed (int or 1-d array_like, optional): Seed for the random number generator.
    Returns:
        (tuple): tuple containing:
            masked data (torch.Tensor): Sub-sampled k-space data
            mask (torch.Tensor): The generated mask
            info (dict): A dictionary containing information about the mask.
    """
    shape = np.array(data.shape)
    shape[:-3] = 1
    mask, info = mask_func(shape, seed)
    mask = mask.to(data.device)
    # Checked that this version also removes negative 0 values as well.
    return torch.where(mask == 0, torch.tensor(0, dtype=data.dtype, device=data.device), data), mask, info


def apply_retro_mask(data, mask):
    return torch.where(mask == 0, torch.tensor(0, dtype=data.dtype, device=data.device), data)


def apply_PCmask(data, mask_func, seed=None):
    shape = np.array(data.shape)
    shape[:-3] = 1
    mask, acceleration, mask_holder = mask_func(shape, seed)
    mask = mask.to(data.device)  # Changed this part here for Pre-loading on GPU.
    return data * mask, mask, acceleration, mask_holder


def apply_random_mask(data, mask_func, seed=None):
    """
    Subsample given k-space by multiplying with a mask.
    Args:
        data (torch.Tensor): The input k-space data. This should have at least 3 dimensions, where
            dimensions -3 and -2 are the spatial dimensions, and the final dimension has size
            2 (for complex values).
        mask_func (callable): A function that takes a shape (tuple of ints) and a random
            number seed and returns a mask.
        seed (int or 1-d array_like, optional): Seed for the random number generator.
    Returns:
        (tuple): tuple containing:
            masked data (torch.Tensor): Subsampled k-space data
            mask (torch.Tensor): The generated mask
    """
    shape = np.array(data.shape)
    shape[:-3] = 1
    mask, type_choice = mask_func(shape, seed)
    mask = mask.to(data.device)  # Changed this part here for Pre-loading on GPU.
    return data * mask, mask, type_choice


def fft2(data):
    """
    Apply centered 2 dimensional Fast Fourier Transform.
    Args:
        data (torch.Tensor): Complex valued input data containing at least 3 dimensions: dimensions
            -3 & -2 are spatial dimensions and dimension -1 has size 2. All other dimensions are
            assumed to be batch dimensions.
    Returns:
        torch.Tensor: The FFT of the input.
    """
    assert data.size(-1) == 2
    data = ifftshift(data, dim=(-3, -2))
    data = torch.fft(data, 2, normalized=True)
    data = fftshift(data, dim=(-3, -2))
    return data


def ifft2(data):
    """
    Apply centered 2-dimensional Inverse Fast Fourier Transform.
    Args:
        data (torch.Tensor): Complex valued input data containing at least 3 dimensions: dimensions
            -3 & -2 are spatial dimensions and dimension -1 has size 2. All other dimensions are
            assumed to be batch dimensions.
    Returns:
        torch.Tensor: The IFFT of the input.
    """
    assert data.size(-1) == 2
    data = ifftshift(data, dim=(-3, -2))
    data = torch.ifft(data, 2, normalized=True)
    data = fftshift(data, dim=(-3, -2))
    return data


def fft1(data, direction):
    """
    Apply centered, normalized 1 dimensional Fast Fourier Transform along the height axis.
    Super-inefficient implementation where the Inverse Fourier Transform is applied to the last (width) axis again.
    This is because there is no Pytorch native implementation for controlling FFT axes.
    Also, this is (probably) faster than permuting the tensor repeatedly.
    Args:
        data (torch.Tensor): Complex valued input data containing at least 3 dimensions: dimensions
            -3 & -2 are spatial dimensions and dimension -1 has size 2. All other dimensions are
            assumed to be batch dimensions.
        direction (str): Direction that the FFT is to be performed.
            Not using `dim` or `axis` as keyword to reduce confusion.
            Unfortunately, Pytorch has no complex number data type for fft, so axis dims are different.
    Returns:
        torch.Tensor: The FFT of the input.
    """
    assert data.size(-1) == 2
    assert direction in ('height', 'width'), 'direction must be either height or width.'

    # Push height dimension to last meaningful axis for FFT.
    if direction == 'height':
        data = data.transpose(dim0=-3, dim1=-2)

    data = ifftshift(data, dim=-2)
    data = torch.fft(data, signal_ndim=1, normalized=True)
    data = fftshift(data, dim=-2)

    # Restore height dimension to its original location.
    if direction == 'height':
        data = data.transpose(dim0=-3, dim1=-2)

    return data


def ifft1(data, direction):
    """
    Apply centered, normalized 1 dimensional Inverse Fast Fourier Transform along the height axis.
    Super-inefficient implementation where the Fourier Transform is applied to the last (width) axis again.
    This is because there is no Pytorch native implementation for controlling IFFT axes.
    Also, this is (probably) faster than permuting the tensor repeatedly.
    Args:
        data (torch.Tensor): Complex valued input data containing at least 3 dimensions: dimensions
            -3 & -2 are spatial dimensions and dimension -1 has size 2. All other dimensions are
            assumed to be batch dimensions.
        direction (str): Direction that the IFFT is to be performed.
            Not using `dim` or `axis` as keyword to reduce confusion.
            Unfortunately, Pytorch has no complex number data type for fft, so axis dims are different.
    Returns:
        torch.Tensor: The IFFT of the input.
    """
    assert data.size(-1) == 2
    assert direction in ('height', 'width'), 'direction must be either height or width.'

    if direction == 'height':  # Push height dimension to last meaningful axis for IFFT.
        data = data.transpose(dim0=-3, dim1=-2)

    data = ifftshift(data, dim=-2)
    data = torch.ifft(data, signal_ndim=1, normalized=True)
    data = fftshift(data, dim=-2)

    if direction == 'height':  # Restore height dimension to its original location.
        data = data.transpose(dim0=-3, dim1=-2)

    return data


def complex_abs(data):
    """
    Compute the absolute value of a complex valued input tensor.
    Args:
        data (torch.Tensor): A complex valued tensor, where the size of the final dimension
            should be 2.
    Returns:
        torch.Tensor: Absolute value of data
    """
    assert data.size(-1) == 2
    return (data ** 2).sum(dim=-1).sqrt()


def fake_input_gen(data, mask):
    """
    From reconstructed image, mask k-space again for k-space fidelity term
    :param
        data: reconstructed image data
        mask: mask to undersample k-space data
    :return:
        under_im
    """
    full_k = kspace_to_nchw(fft2(nchw_to_kspace(data)))
    under_k = apply_retro_mask(full_k, mask)
    under_im = kspace_to_nchw(ifft2(nchw_to_kspace(under_k)))

    return under_im


def fake_input_gen_rss(data, mask):
    """
    From reconstructed image, mask k-space again for k-space fidelity term
    :param
        data: reconstructed image data
        mask: mask to undersample k-space data
    :return:
        under_im
    """
    full_k = kspace_to_nchw(fft2(nchw_to_kspace(data)))
    under_k = apply_retro_mask(full_k, mask)
    under_im = kspace_to_nchw(ifft2(nchw_to_kspace(under_k)))
    rss_under_im = root_sum_of_squares(under_im, dim=1).squeeze()

    return rss_under_im, under_k


def proj_mask(data, mask):
    under_k = apply_retro_mask(data, mask)
    return under_k


def fake_input_gen_hc(data, mask):
    """
    From reconstructed image, mask k-space again for k-space fidelity term
    :param
        data: reconstructed k-space data
        mask: mask to undersample k-space data
    :return:
        under_im
    """
    under_k = apply_retro_mask(data, mask)
    full_under_im = ifft2(under_k)
    hc_under_im = complex_height_crop(full_under_im, 320)
    under_im = kspace_to_nchw(hc_under_im)

    return under_im


def root_sum_of_squares(data, dim=0):
    """
    Compute the Root Sum of Squares (RSS) transform along a given dimension of a tensor.
    Args:
        data (torch.Tensor): The input tensor
        dim (int): The dimensions along which to apply the RSS transform
    Returns:
        torch.Tensor: The RSS value
    """
    return torch.sqrt((data ** 2).sum(dim))


def Ssos_mip(data, dim=0, slice=3):
    """
    Compute Ssos (RSS) two the multislice data, and compute MIP in the stacked ssos images
    Args:
        data (torch.Tensor): The input tensor
        dim (int): The dimensions along which to apply ssos
        slice (int): Number of slices that were concatenated to form a 2.5D slice
    Returns:
        rss_data_stack (torch.Tensor) : ssos images stacked into the specified dimension
        mip_data (torch.Tensor) : A single mip image with mip held in the specified dimension
    """
    ch = data.shape[1] // slice
    split_data = torch.split(data, ch, dim=dim)
    rss_data_list = list()
    for i in range(slice):
        rss_data = root_sum_of_squares(split_data[i], dim=dim).unsqueeze(dim=dim)
        rss_data_list.append(rss_data)
    rss_data_stack = torch.cat(rss_data_list, dim=dim)
    mip_data = torch.max(rss_data_stack, dim=dim)

    return rss_data_stack, mip_data.values


def pre_RSS(image_recons, image_targets):
    assert image_recons.size() == image_targets.size()

    image_recons = root_sum_of_squares(image_recons)
    image_targets = root_sum_of_squares(image_targets)

    return image_recons, image_targets


def center_crop(data, shape):
    """
    Apply a center crop to the input real image or batch of real images.
    Args:
        data (torch.Tensor): The input tensor to be center cropped. It should have at
            least 2 dimensions and the cropping is applied along the last two dimensions.
        shape (int, int): The output shape. The shape should be smaller than the
            corresponding dimensions of data.
    Returns:
        torch.Tensor: The center cropped image
    """
    assert 0 < shape[0] <= data.shape[-2]
    assert 0 < shape[1] <= data.shape[-1]
    w_from = (data.shape[-2] - shape[0]) // 2
    h_from = (data.shape[-1] - shape[1]) // 2
    w_to = w_from + shape[0]
    h_to = h_from + shape[1]
    return data[..., w_from:w_to, h_from:h_to]


def complex_center_crop(data, shape):
    """
    Apply a center crop to the input image or batch of complex images.
    Args:
        data (torch.Tensor): The complex input tensor to be center cropped. It should
            have at least 3 dimensions and the cropping is applied along dimensions
            -3 and -2 and the last dimensions should have a size of 2.
        shape (int, int): The output shape. The shape should be smaller than the
            corresponding dimensions of data.
    Returns:
        torch.Tensor: The center cropped image
    """
    assert 0 < shape[0] <= data.shape[-3]
    assert 0 < shape[1] <= data.shape[-2]
    w_from = (data.shape[-3] - shape[0]) // 2
    h_from = (data.shape[-2] - shape[1]) // 2
    w_to = w_from + shape[0]
    h_to = h_from + shape[1]
    return data[..., w_from:w_to, h_from:h_to, :]


def complex_height_crop(data, height_shape):

    assert 0 < height_shape <= data.shape[-3]
    h_from = (data.shape[-3] - height_shape) // 2
    h_to = h_from + height_shape
    return data[..., h_from:h_to, :, :]


def complex_width_crop(data, width_shape):

    assert 0 < width_shape <= data.shape[-2]
    w_from = (data.shape[-2] - width_shape) // 2
    w_to = w_from + width_shape
    return data[..., w_from:w_to, :]


def width_crop(data, width_shape):
    w_from = (data.shape[-1] - width_shape) // 2
    w_to = w_from + width_shape
    return data[..., w_from:w_to]


def normalize(data, mean, stddev, eps=0.):
    """
    Normalize the given tensor using:
        (data - mean) / (stddev + eps)
    Args:
        data (torch.Tensor): Input data to be normalized
        mean (float): Mean value
        stddev (float): Standard deviation
        eps (float): Added to stddev to prevent dividing by zero
    Returns:
        torch.Tensor: Normalized tensor
    """
    return (data - mean) / (stddev + eps)


def normalize_instance(data, eps=0.):
    """
        Normalize the given tensor using:
            (data - mean) / (stddev + eps)
        where mean and stddev are computed from the data itself.
        Args:
            data (torch.Tensor): Input data to be normalized
            eps (float): Added to stddev to prevent dividing by zero
        Returns:
            torch.Tensor: Normalized tensor
        """
    mean = data.mean()
    std = data.std()
    return normalize(data, mean, std, eps), mean, std


# Helper functions

def roll(x, shift, dim):
    """
    Similar to np.roll but applies to PyTorch Tensors
    """
    if isinstance(shift, (tuple, list)):
        assert len(shift) == len(dim)
        for s, d in zip(shift, dim):
            x = roll(x, s, d)
        return x
    shift = shift % x.size(dim)
    if shift == 0:
        return x
    left = x.narrow(dim, 0, x.size(dim) - shift)
    right = x.narrow(dim, x.size(dim) - shift, shift)
    return torch.cat((right, left), dim=dim)


def fftshift(x, dim=None):
    """
    Similar to np.fft.fftshift but applies to PyTorch Tensors
    """
    if dim is None:
        dim = tuple(range(x.dim()))
        shift = [dim // 2 for dim in x.shape]
    elif isinstance(dim, int):
        shift = x.shape[dim] // 2
    else:
        shift = [x.shape[i] // 2 for i in dim]
    return roll(x, shift, dim)


def ifftshift(x, dim=None):
    """
    Similar to np.fft.ifftshift but applies to PyTorch Tensors
    """
    if dim is None:
        dim = tuple(range(x.dim()))
        shift = [(dim + 1) // 2 for dim in x.shape]
    elif isinstance(dim, int):
        shift = (x.shape[dim] + 1) // 2
    else:
        shift = [(x.shape[i] + 1) // 2 for i in dim]
    return roll(x, shift, dim)


def tensor_to_complex_np(data):
    """
    Converts a complex torch tensor to numpy array.
    Args:
        data (torch.Tensor): Input data to be converted to numpy.
    Returns:
        np.array: Complex numpy version of data
    """
    data = data.numpy()
    return data[..., 0] + 1j * data[..., 1]


# My k-space transforms
def k_slice_to_chw(tensor):
    """
    Convert torch tensor in (Coil, Height, Width, Complex) 4D k-slice format to
    (C, H, W) 3D format for processing by 2D CNNs.

    `Complex` indicates (real, imag) as 2 channels, the complex data format for Pytorch.

    C is the coils interleaved with real and imaginary values as separate channels.
    C is therefore always 2 * Coil.

    Singlecoil data is assumed to be in the 4D format with Coil = 1

    Args:
        tensor (torch.Tensor): Input data in 4D k-slice tensor format.
    Returns:
        tensor (torch.Tensor): tensor in 3D CHW format to be fed into a CNN.
    """
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 4
    s = tensor.shape
    assert s[-1] == 2
    tensor = tensor.permute(dims=(0, 3, 1, 2)).reshape(shape=(2 * s[0], s[1], s[2]))
    return tensor


def ej_kslice_to_chw(tensor):
    import ipdb; ipdb.set_trace()
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 4
    s = tensor.shape
    assert s[-1] == 2
    tensor = tensor.permute(dims=(0, 3, 1, 2)).reshape(shape=(2 * s[0], s[0], s[1]))
    return tensor


def ej_permute(tensor):
    """
    Converts eunju dataset given as (H, W, C, 2) format to (C, H, W, 2)
    """
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 4
    s = tensor.shape
    assert s[-1] == 2
    tensor = tensor.permute(dims=(2, 0, 1, 3))

    return tensor


def ej_permute_bchw(tensor):
    """
    Converts eunju dataset given as (H, W, C, 2) format to (C, H, W, 2)
    """
    assert isinstance(tensor, torch.Tensor), 'input must be type torch tensor'
    assert tensor.dim() == 5, 'Dimension must be BxCxHxWx2'
    s = tensor.shape
    assert s[-1] == 2
    tensor = tensor.permute(dims=(0, 3, 1, 2, 4))

    return tensor
    


def chw_to_k_slice(tensor):
    """
    Convert a torch tensor in (C, H, W) format to the (Coil, Height, Width, Complex) format.

    This assumes that the real and imaginary values of a coil are always adjacent to one another in C.
    """
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 3
    s = tensor.shape
    assert s[0] % 2 == 0
    tensor = tensor.view(size=(s[0] // 2, 2, s[1], s[2])).permute(dims=(0, 2, 3, 1))
    return tensor


def kspace_to_nchw(tensor):
    """
    Convert torch tensor in (Slice, Coil, Height, Width, Complex) 5D format to
    (N, C, H, W) 4D format for processing by 2D CNNs.

    Complex indicates (real, imag) as 2 channels, the complex data format for Pytorch.

    C is the coils interleaved with real and imaginary values as separate channels.
    C is therefore always 2 * Coil.

    Singlecoil data is assumed to be in the 5D format with Coil = 1

    Args:
        tensor (torch.Tensor): Input data in 5D kspace tensor format.
    Returns:
        tensor (torch.Tensor): tensor in 4D NCHW format to be fed into a CNN.
    """
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 5
    s = tensor.shape
    assert s[-1] == 2
    tensor = tensor.permute(dims=(0, 1, 4, 2, 3)).reshape(shape=(s[0], 2 * s[1], s[2], s[3]))
    return tensor


def nchw_to_kspace(tensor):
    """
    Convert a torch tensor in (N, C, H, W) format to the (Slice, Coil, Height, Width, Complex) format.

    This function assumes that the real and imaginary values of a coil are always adjacent to one another in C.
    """
    assert isinstance(tensor, torch.Tensor)
    assert tensor.dim() == 4
    s = tensor.shape
    assert s[1] % 2 == 0
    tensor = tensor.view(size=(s[0], s[1] // 2, 2, s[2], s[3])).permute(dims=(0, 1, 3, 4, 2))
    return tensor


def split_four_cols(tensor):
    b, c, h, w, ri = tensor.shape

    holder0 = torch.zeros([b, c, h, int(w / 4), ri]).to(device='cuda:0')
    holder1 = torch.zeros([b, c, h, int(w / 4), ri]).to(device='cuda:0')
    holder2 = torch.zeros([b, c, h, int(w / 4), ri]).to(device='cuda:0')
    holder3 = torch.zeros([b, c, h, int(w / 4), ri]).to(device='cuda:0')

    for i in range(w):
        if i % 4 == 0:
            holder0[:, :, :, i // 4, :] = tensor[:, :, :, i, :]
        if i % 4 == 1:
            holder1[:, :, :, i // 4, :] = tensor[:, :, :, i, :]
        if i % 4 == 2:
            holder2[:, :, :, i // 4, :] = tensor[:, :, :, i, :]
        if i % 4 == 3:
            holder3[:, :, :, i // 4, :] = tensor[:, :, :, i, :]

    return holder0, holder1, holder2, holder3


def stack_for_vis(tensor):

    b, c4, h, w_4 = tensor.shape
    c = int(c4/4)
    w = w_4 * 4

    chunked_tensor = torch.chunk(tensor, chunks=4, dim=1)

    holder = torch.zeors([b, c, h, w])
    for i in range(w):
        if i % 4 == 0:
            holder[..., i] = chunked_tensor[0][..., i]
        elif i % 4 == 1:
            holder[..., i] = chunked_tensor[1][..., i]
        elif i % 4 == 2:
            holder[..., i] = chunked_tensor[2][..., i]
        elif i % 4 == 3:
            holder[..., i] = chunked_tensor[3][..., i]

    return holder


def stack_for_vis_all(img_inputs, img_recons, img_targets):

    s_img_inputs = stack_for_vis(img_inputs)
    s_img_recons = stack_for_vis(img_recons)
    s_img_targets = stack_for_vis(img_targets)

    return s_img_inputs, s_img_recons, s_img_targets


def log_weighting(tensor, scale=1):
    assert scale > 0, '`scale` must be a positive value.'
    return scale * torch.sign(tensor) * torch.log1p(torch.abs(tensor))


def exp_weighting(tensor, scale=1):
    assert scale > 0, '`scale` must be a positive value.'
    return torch.sign(tensor) * torch.expm1(torch.abs(tensor) * (1 / scale))


def pad_FCF(tensor, divisor=32):

    margin = tensor.size(-2) % divisor
    if margin > 0:
        pad = [(divisor - margin) // 2, (1 + divisor - margin) // 2]
        pad2 = [0, 0, (divisor - margin) // 2, (1 + divisor - margin) // 2]
    else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
        pad = [0, 0]
        pad2 = [0, 0, 0, 0]

    outputs = F.pad(tensor, pad=pad2, value=0)

    return outputs

# normalize tensor so that we can save it as an image or visualize it on tensorboard
def normalize_im(tensor):
    large = torch.max(tensor).cpu().data
    small = torch.min(tensor).cpu().data
    diff = large - small

    normalized_tensor = (tensor.clamp(min=small, max=large) - small) * (torch.tensor(1) / diff)

    return normalized_tensor


def visualize_im(tensor):
    normalized_tensor = normalize_im(tensor.squeeze())
    tensor_np = normalized_tensor.cpu().detach().numpy()

    plt.imshow(tensor_np, cmap='gray')
    plt.show()


def kspace_transform(inputs, outputs):
    assert inputs.shape == outputs.shape, "Input and Output shape should be the same"
    k_inputs = kspace_to_nchw(fft2(nchw_to_kspace(inputs)))
    k_outputs = kspace_to_nchw(fft2(nchw_to_kspace(outputs)))
    return k_inputs, k_outputs


def kspace_transform_single(inputs):
    k_inputs = kspace_to_nchw(fft2(nchw_to_kspace(inputs)))
    return k_inputs

def split_slices(inputs, slice):
    inputs_list = list()
    ch = inputs.shape[1] // slice
    for i in range(slice):
        inputs_list.append(inputs[:, i*ch:(i+1)*ch, :, :])
    return inputs_list

def fft2_single(inputs):
    k_inputs = kspace_to_nchw(fft2(nchw_to_kspace(inputs)))
    return k_inputs


def ifft2_single(inputs):
    k_inputs = kspace_to_nchw(ifft2(nchw_to_kspace(inputs)))
    return k_inputs

# Given input and target are images, not k-space
# Consequently, we use fft to change domains
def Hard_DC_img(input, target, mask):
    DC = fft2_single(input)  # consistent part where only the sampled part are non-zero
    NDC = fft2_single(target) * (1 - mask)  # non-consistent part where k-space is interpolated
    output_k = DC + NDC
    output = ifft2_single(output_k)
    return output


class Adaptive_DC(nn.Module):

    def __init__(self):
        super().__init__()
        self.lambda_DC = nn.Parameter(torch.tensor(0.0, dtype=torch.float)) # Initialize with 0

    def forward(self, input, recon, mask):
        DC = fft2_single(input)
        # The k-space part that was interpolated with NN
        NDC = fft2_single(recon) * (1 - mask)
        # The k-space part that was changed while interpolation process
        new_DC = DC * self.lambda_DC + (fft2_single(recon) * mask) * (1 - self.lambda_DC)
        output_k = new_DC + NDC
        return ifft2_single(output_k)


def add_gaussian_noise(input, device, std_ratio=0.05):
    mean = input.mean()
    std = input.std()

    return input + mean + (std * std_ratio) * torch.randn(input.shape, device=device)


def extract_patch(inputs, targets, patch_size=128):
    assert inputs.dim() == 4, "Tensor should be batched and have dimension 4"
    assert isinstance(inputs, torch.Tensor), "Input data should be torch tensor"
    assert isinstance(targets, torch.Tensor), "Input data should be torch tensor"

    with torch.no_grad():
        h = inputs.shape[-2]
        w = inputs.shape[-1]

        start_h = random.randint(0, h - (patch_size + 1))
        start_w = random.randint(0, w - (patch_size + 1))

        patch_inputs = inputs[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]
        patch_targets = targets[:, start_h:start_h + patch_size, start_w:start_w + patch_size]

    assert patch_inputs.shape[-1] == patch_size
    assert patch_inputs.shape[-1] == patch_size

    return patch_inputs, patch_targets


def extract_patch_transform_single(inputs, patch_size=128):
    assert isinstance(inputs, torch.Tensor), "Input data should be torch tensor"

    with torch.no_grad():
        h = inputs.shape[-2]
        w = inputs.shape[-1]

        start_h = random.randint(0, h - (patch_size + 1))
        start_w = random.randint(0, w - (patch_size + 1))

        patch_inputs = inputs[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]

    return patch_inputs


# Use this function when you want to extract patch inside the input_transform function
def extract_patch_transform(inputs, targets, patch_size=128):
    assert isinstance(inputs, torch.Tensor), "Input data should be torch tensor"
    assert isinstance(targets, torch.Tensor), "Input data should be torch tensor"

    with torch.no_grad():
        h = inputs.shape[-2]
        w = inputs.shape[-1]

        start_h = random.randint(0, h - (patch_size + 1))
        start_w = random.randint(0, w - (patch_size + 1))

        patch_inputs = inputs[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]
        patch_targets = targets[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]

    return patch_inputs, patch_targets


def extract_patch_transform_proj(inputs, targets, proj, patch_size=128):
    assert isinstance(inputs, torch.Tensor), "Input data should be torch tensor"
    assert isinstance(targets, torch.Tensor), "Input data should be torch tensor"

    with torch.no_grad():
        h = inputs.shape[-2]
        w = inputs.shape[-1]

        start_h = random.randint(0, h - (patch_size + 1))
        start_w = random.randint(0, w - (patch_size + 1))

        patch_inputs = inputs[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]
        patch_targets = targets[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]
        patch_proj = proj[:, :, start_h:start_h + patch_size, start_w:start_w + patch_size]

    return patch_inputs, patch_targets, patch_proj


def extract_patch_transform_inference(inputs, dims=2, patch_size=128, stride=64):
    assert isinstance(inputs, torch.Tensor), "Input data should be torch tensor"
    ps = patch_size
    s = stride
    with torch.no_grad():
        if dims == 2:
            h = inputs.shape[-2]
            w = inputs.shape[-1]

            hs = ((h - ps) // s) + 1
            ws = ((w - ps) // s) + 1

            inputs_list = list()
            # if np.mod(h, stride) == 0:
            #     for i in range(hs):
            #         for j in range(ws):
            #             inputs_list.append(inputs[:, :, i*s:i*s+ps, j*s:j*s+ps].squeeze(dim=0))
            # else:
            for i in range(hs+1):
                if (i == hs):
                    st_i = h - ps
                else:
                    st_i = i * s
                for j in range(ws+1):
                    if (j == ws):
                        st_j = w - ps
                    else:
                        st_j = j * s
                    inputs_list.append(inputs[:, :, st_i:st_i+ps, st_j:st_j+ps].squeeze(dim=0))
            batched_inputs = torch.stack(inputs_list, dim=0)

    return batched_inputs


def extract_patch_trasnform_inference_gather(batched_outputs, FOV, dims=2, patch_size=128, stride=64):
    assert isinstance(batched_outputs, torch.Tensor)
    wgt = torch.zeros_like(FOV)
    ps = patch_size
    s = stride

    if dims == 2:
        b, c, h, w = FOV.shape
        hs = ((h - ps) // s) + 1
        ws = ((w - ps) // s) + 1
        flg = 0
        for i in range(hs + 1):
            if (i == hs):
                st_i = h - ps
            else:
                st_i = i * s
            for j in range(ws + 1):
                if (j == ws):
                    st_j = w - ps
                else:
                    st_j = j * s
                FOV[:, :, st_i:st_i+ps, st_j:st_j+ps] += batched_outputs[flg, :, :, :]
                wgt[:, :, st_i:st_i+ps, st_j:st_j+ps] += 1
                flg += 1

        FOV /= wgt
    else:
        NotImplementedError("Only dim=2 implemented")

    return FOV.squeeze()
