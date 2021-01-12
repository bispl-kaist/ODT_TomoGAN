import torch
import torch.nn.functional as F

import numpy as np

from data.data_transforms import to_tensor, ifft2, complex_abs, apply_mask, k_slice_to_chw, log_weighting


# My transforms for data processing
class TrainInputSliceTransform:
    """
    Data Transformer for training and validating models.

    This transform is designed for a single slice of k-space input data, termed the 'k-slice'.
    """

    def __init__(self, mask_func, which_challenge, use_seed=True, divisor=1):
        """
        Args:
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.
            use_seed (bool): If true, this class computes a pseudo random number generator seed
                from the filename. This ensures that the same mask is used for all the slices of
                a given volume every time.
            divisor (int): An integer indicating the lowest common denominator necessary for padding.
                This parameter is necessary because phase encoding dimensions are different for all blocks
                and UNETs and other models require inputs to be divisible by some power of 2.
                Set to 1 if not necessary.
        """

        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.which_challenge = which_challenge
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        """
        Args:
            k_slice (numpy.array): Input k-space of shape (num_coils, height, width) for multi-coil
                data or (rows, cols) for single coil data.
            target (numpy.array): Target (320x320) image. May be None.
            attrs (dict): Acquisition related information stored in the HDF5 object.
            file_name (str): File name
            slice_num (int): Serial number of the slice.
        Returns:
            (tuple): tuple containing:
                data (torch.Tensor): kspace data converted to CHW format for CNNs, where C=(2*num_coils).
                    Also has padding in the width axis for auto-encoders, which have down-sampling regions.
                    This requires the data to be divisible by some number (usually 2**num_pooling_layers).
                    Otherwise, concatenation will not work in the decoder due to different sizes.
                    Only the width dimension is padded in this case due to the nature of the dataset.
                    The height is fixed at 640, while the width is variable.
                labels (torch.Tensor): Coil-wise ground truth images. Shape=(num_coils, H, W)
        """
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'
        assert k_slice.shape[-1] % 2 == 0, 'k-space data width must be even.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise TypeError('Invalid slice type')

        with torch.no_grad():  # Remove unnecessary gradient calculations.

            k_slice = to_tensor(k_slice)  # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            target_slice = complex_abs(ifft2(k_slice))  # I need cuda here!
            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(k_slice, self.mask_func, seed)

            data_slice = k_slice_to_chw(masked_kspace)
            left_pad = (self.divisor - (data_slice.shape[-1] % self.divisor)) // 2
            right_pad = (1 + self.divisor - (data_slice.shape[-1] % self.divisor)) // 2
            pad = [left_pad, right_pad]
            data_slice = F.pad(data_slice, pad=pad, value=0)  # This pads at the last dimension of a tensor.

            # Using the data acquisition method (fat suppression) may be useful later on.

        return data_slice, target_slice


class SubmitInputSliceTransform:
    """
    Data Transformer for generating submissions on the validation and test datasets.
    """

    def __init__(self, which_challenge, mask_func=None, divisor=1):
        """
        Args:
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
        """
        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.which_challenge = which_challenge
        self.mask_func = mask_func
        self.divisor = divisor

    def __call__(self, kspace, target, attrs, file_name, slice_num):
        """
        Args:
            kspace (numpy.Array): k-space measurements
            target (numpy.Array): Target image
            attrs (dict): Acquisition related information stored in the HDF5 object
            file_name (str): File name
            slice_num (int): Serial number of the slice
        Returns:
            (tuple): tuple containing:
                image (torch.Tensor): Normalized zero-filled input image
                mean (float): Mean of the zero-filled image
                std (float): Standard deviation of the zero-filled image
                file_name (str): File name
                slice_num (int): Serial number of the slice
        """
        kspace = to_tensor(kspace)
        if self.mask_func is not None:  # Validation set
            seed = tuple(map(ord, file_name))
            masked_kspace, _ = apply_mask(kspace, self.mask_func, seed)
        else:  # Test set
            masked_kspace = kspace

        data = k_slice_to_chw(masked_kspace)
        pad = (self.divisor - (data.shape[-1] % self.divisor)) // 2
        pad = [pad, pad]
        data = F.pad(data, pad=pad, value=0)  # This pads at the last dimension of a tensor.
        return data


class NewTrainInputSliceTransform:
    """
    Data Transformer for training and validating models.

    This transform is designed for a single slice of k-space input data, termed the 'k-slice'.

    This transform corrects several serious flaws of the previous transformer class.

    First, it includes scalar amplification for solving the numerical underflow problem.
    Second, it sends the data to GPU before sending it to DataLoader.
    Note that the settings should be set to pin_memory=False. Tensors in GPU cannot be sent to pin memory and
    pin_memory=True will cause errors.

    As long as batch size = 1, there should be no issues with sending each slice to GPU individually.
    If batching is ever implemented, it will be better to send the data to GPU as a single batch to reduce overhead.

    I have found several issues with sending to GPU in the transform.
    First, multi-processing is impossible if there is a call to the GPU.
    See https://discuss.pytorch.org/t/pin-memory-vs-sending-direct-to-gpu-from-dataset/33891 for what I mean.
    This means that the number of workers in the data loader must be 0 if data is sent to GPU inside the transform.
    Otherwise, there will be an error.
    Second, I have found no significant difference in data processing speed by sending data to the GPU in the
    transform. I should note that this is for the batch size = 1 case.
    """

    def __init__(self, mask_func, which_challenge, device, use_seed=True, amp_fac=1E4, divisor=1):
        """
        Args:
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.

            device (torch.device): The device to send the data to.
                This will work well with single batch inputs since all data is being sent in at once.
                This may cause problems with multi-GPU models and for batch sizes greater than 1.
                Multi-GPU models are not planned and batch sizes greater than 1 can have sending to GPU implemented
                in the collate function of the DataLoader to reduce data sending overhead.

            use_seed (bool): If true, this class computes a pseudo random number generator seed
                from the filename. This ensures that the same mask is used for all the slices of
                a given volume every time.

            amp_fac (float): Amplification factor for k-space data. This is designed to prevent numerical underflow
                from causing nan outputs. This can be multiplied to k-space because
                the Fourier Transform is a linear transform. Scalar multiplication in k-space is the same as
                scalar multiplication in image space. This is a form of data manipulation specific to this transform.
                Other data pre-processing methods may be better. Unlike the others,
                there is no strong theoretical justification for this, it is just a hack.

            divisor (int): An integer indicating the lowest common denominator necessary for padding.
                This parameter is necessary because phase encoding dimensions are different for all blocks
                and UNETs and other models require inputs to be divisible by some power of 2.
                Set to 1 if not necessary.
        """

        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.which_challenge = which_challenge
        self.use_seed = use_seed
        self.divisor = divisor
        self.device = device
        self.amp_fac = amp_fac

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        """
        Args:
            k_slice (numpy.array): Input k-space of shape (num_coils, height, width) for multi-coil
                data or (rows, cols) for single coil data.
            target (numpy.array): Target (320x320) image. May be None.
            attrs (dict): Acquisition related information stored in the HDF5 object.
            file_name (str): File name
            slice_num (int): Serial number of the slice.
        Returns:
            (tuple): tuple containing:
                data (torch.Tensor): kspace data converted to CHW format for CNNs, where C=(2*num_coils).
                    Also has padding in the width axis for auto-encoders, which have down-sampling regions.
                    This requires the data to be divisible by some number (usually 2**num_pooling_layers).
                    Otherwise, concatenation will not work in the decoder due to different sizes.
                    Only the width dimension is padded in this case due to the nature of the dataset.
                    The height is fixed at 640, while the width is variable.
                labels (torch.Tensor): Coil-wise ground truth images. Shape=(num_coils, H, W)
        """
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'
        assert k_slice.shape[-1] % 2 == 0, 'k-space data width must be even.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise TypeError('Invalid slice type')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            # The data is in the GPU and has been amplified by the amplification factor.
            k_slice = to_tensor(k_slice).to(device=self.device) * self.amp_fac
            # k_slice = to_tensor(k_slice).cuda(self.device) * self.amp_fac
            target_slice = complex_abs(ifft2(k_slice))  # I need cuda here!
            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(k_slice, self.mask_func, seed)

            data_slice = k_slice_to_chw(masked_kspace)
            # assert data_slice.size(-1) % 2 == 0

            margin = (data_slice.shape[-1] % self.divisor)

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix.
                pad = [0, 0]
            # right_pad = self.divisor - left_pad
            # pad = [pad, pad]
            data_slice = F.pad(data_slice, pad=pad, value=0)  # This pads at the last dimension of a tensor.

            # Using the data acquisition method (fat suppression) may be useful later on.
        # print(1, data_slice.size())
        # print(2, target_slice.size())
        return data_slice, target_slice


class KInputSliceTransform:
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'
        # assert k_slice.shape[-1] % 2 == 0, 'k-space data width must be even.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            k_slice = to_tensor(k_slice).to(device=self.device)
            scaling = torch.std(k_slice)  # Pseudo-standard deviation for normalization.
            target_slice = complex_abs(ifft2(k_slice))  # Labels are not standardized.
            k_slice *= (torch.ones(()) / scaling)  # Standardization of CNN inputs.
            # Using weird multiplication because multiplication is much faster than division.
            # Multiplying the whole tensor by 1/scaling is faster than dividing the whole tensor by scaling.

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(k_slice, self.mask_func, seed)

            data_slice = k_slice_to_chw(masked_kspace)

            margin = data_slice.size(-1) % self.divisor

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
                pad = [0, 0]

            data_slice = F.pad(data_slice, pad=pad, value=0)  # This pads at the last dimension of a tensor with 0.

        return data_slice, target_slice, scaling  # This has a different output API.


class InputSliceTransformK2K:
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            kspace_target = to_tensor(k_slice).to(device=self.device)

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace_target, self.mask_func, seed)
            # Multiplying the whole tensor by 1/scaling is faster than dividing the whole tensor by scaling.

            scaling = torch.std(masked_kspace)  # Pseudo-standard deviation for normalization.
            masked_kspace *= (torch.tensor(1) / scaling)  # Standardization of CNN inputs.
            masked_kspace = k_slice_to_chw(masked_kspace)

            margin = masked_kspace.size(-1) % self.divisor

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
                pad = [0, 0]

            # This pads at the last dimension of a tensor with 0.
            masked_kspace = F.pad(masked_kspace, pad=pad, value=0)

        return masked_kspace, kspace_target, (scaling, mask)


class WeightedInputSliceK2K:
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1, log_amp_scale=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        assert callable(mask_func)

        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor
        self.log_amp_scale = torch.as_tensor(log_amp_scale, dtype=torch.float32)

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            kspace_target = to_tensor(k_slice).to(device=self.device)

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace_target, self.mask_func, seed)
            # Multiplying the whole tensor by 1/scaling is faster than dividing the whole tensor by scaling.

            # Maybe call this scaling k_scale or something. Using scaling every time is confusing.
            k_scale = torch.std(masked_kspace)  # Pseudo-standard deviation for normalization.
            masked_kspace *= (torch.as_tensor(1) / k_scale)  # Standardization of CNN inputs.

            # Performing log weighting for smoother inputs. It can be before padding since 0 will be 0 after weighting.
            masked_kspace = log_weighting(k_slice_to_chw(masked_kspace), scale=self.log_amp_scale)

            margin = masked_kspace.size(-1) % self.divisor

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
                pad = [0, 0]

            # This pads at the last dimension of a tensor with 0.
            masked_kspace = F.pad(masked_kspace, pad=pad, value=0)

        return masked_kspace, kspace_target, (k_scale, mask)


class InputSliceTransformK2C:
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            kspace_target = to_tensor(k_slice).to(device=self.device)
            c_img_target = ifft2(kspace_target)

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace_target, self.mask_func, seed)

            # Multiplying the whole tensor by 1/scaling is faster than dividing the whole tensor by scaling.
            k_scale = torch.std(masked_kspace)  # Pseudo-standard deviation for normalization.
            masked_kspace *= (torch.tensor(1) / k_scale)  # Standardization of CNN inputs.
            masked_kspace = k_slice_to_chw(masked_kspace)
            margin = masked_kspace.size(-1) % self.divisor

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
                pad = [0, 0]

            # This pads at the last dimension of a tensor with 0.
            masked_kspace = F.pad(masked_kspace, pad=pad, value=0)

        return masked_kspace, c_img_target, (kspace_target.unsqueeze(dim=0), k_scale, mask)


class WeightedInputSliceK2C:
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1, log_amp_scale=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')

        assert callable(mask_func)

        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor
        self.log_amp_scale = log_amp_scale

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            kspace_target = to_tensor(k_slice).to(device=self.device)
            c_img_target = ifft2(kspace_target)

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace_target, self.mask_func, seed)

            # Multiplying the whole tensor by 1/scaling is faster than dividing the whole tensor by scaling.
            k_scale = torch.std(masked_kspace)  # Pseudo-standard deviation for normalization.
            masked_kspace *= (torch.tensor(1) / k_scale)  # Standardization of CNN inputs.
            # Weighting is performed here since it is shape independent and inputs of 0 result in outputs of 0.
            masked_kspace = log_weighting(k_slice_to_chw(masked_kspace), scale=self.log_amp_scale)
            margin = masked_kspace.size(-1) % self.divisor

            if margin > 0:
                pad = [(self.divisor - margin) // 2, (1 + self.divisor - margin) // 2]
            else:  # This is a temporary fix to prevent padding by half the divisor when margin=0.
                pad = [0, 0]

            # This pads at the last dimension of a tensor with 0.
            masked_kspace = F.pad(masked_kspace, pad=pad, value=0)

        return masked_kspace, c_img_target, (kspace_target.unsqueeze(dim=0), k_scale, mask)


class TrainInputBatchTransform:
    """
    Data Transformer for training and validating models.

    This transform is designed for a single slice of k-space input data, termed the 'k-slice'.

    This transform corrects several serious flaws of the previous transformer class.

    First, it includes scalar amplification for solving the numerical underflow problem.
    Second, it sends the data to GPU before sending it to DataLoader.
    Note that the settings should be set to pin_memory=False. Tensors in GPU cannot be sent to pin memory and
    pin_memory=True will cause errors.

    As long as batch size = 1, there should be no issues with sending each slice to GPU individually.
    If batching is ever implemented, it will be better to send the data to GPU as a single batch to reduce overhead.

    I have found several issues with sending to GPU in the transform.
    First, multi-processing is impossible if there is a call to the GPU.
    See https://discuss.pytorch.org/t/pin-memory-vs-sending-direct-to-gpu-from-dataset/33891 for what I mean.
    This means that the number of workers in the data loader must be 0 if data is sent to GPU inside the transform.
    Otherwise, there will be an error.
    Second, I have found no significant difference in data processing speed by sending data to the GPU in the
    transform. I should note that this is for the batch size = 1 case.
    """

    def __init__(self, mask_func, which_challenge, device, use_seed=True, amp_fac=1E4, divisor=1):
        """
        Args:
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.

            device (torch.device): The device to send the data to.
                This will work well with single batch inputs since all data is being sent in at once.
                This may cause problems with multi-GPU models and for batch sizes greater than 1.
                Multi-GPU models are not planned and batch sizes greater than 1 can have sending to GPU implemented
                in the collate function of the DataLoader to reduce data sending overhead.

            use_seed (bool): If true, this class computes a pseudo random number generator seed
                from the filename. This ensures that the same mask is used for all the slices of
                a given volume every time.

            amp_fac (float): Amplification factor for k-space data. This is designed to prevent numerical underflow
                from causing nan outputs. This can be multiplied to k-space because
                the Fourier Transform is a linear transform. Scalar multiplication in k-space is the same as
                scalar multiplication in image space. This is a form of data manipulation specific to this transform.
                Other data pre-processing methods may be better. Unlike the others,
                there is no strong theoretical justification for this, it is just a hack.

            divisor (int): An integer indicating the lowest common denominator necessary for padding.
                This parameter is necessary because phase encoding dimensions are different for all blocks
                and UNETs and other models require inputs to be divisible by some power of 2.
                Set to 1 if not necessary.
        """

        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.which_challenge = which_challenge
        self.use_seed = use_seed
        self.divisor = divisor
        self.device = device
        self.amp_fac = amp_fac

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        """
        Args:
            k_slice (numpy.array): Input k-space of shape (num_coils, height, width) for multi-coil
                data or (rows, cols) for single coil data.
            target (numpy.array): Target (320x320) image. May be None.
            attrs (dict): Acquisition related information stored in the HDF5 object.
            file_name (str): File name
            slice_num (int): Serial number of the slice.
        Returns:
            (tuple): tuple containing:
                data (torch.Tensor): kspace data converted to CHW format for CNNs, where C=(2*num_coils).
                    Also has padding in the width axis for auto-encoders, which have down-sampling regions.
                    This requires the data to be divisible by some number (usually 2**num_pooling_layers).
                    Otherwise, concatenation will not work in the decoder due to different sizes.
                    Only the width dimension is padded in this case due to the nature of the dataset.
                    The height is fixed at 640, while the width is variable.
                labels (torch.Tensor): Coil-wise ground truth images. Shape=(num_coils, H, W)
        """
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'
        assert k_slice.shape[-1] % 2 == 0, 'k-space data width must be even.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise TypeError('Invalid slice type')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            # The data is in the GPU and has been amplified by the amplification factor.
            k_slice = to_tensor(k_slice).to(self.device, non_blocking=True) * self.amp_fac
            target_slice = complex_abs(ifft2(k_slice))  # I need cuda here!
            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(k_slice, self.mask_func, seed)

            data_slice = k_slice_to_chw(masked_kspace)
            left_pad = (self.divisor - (data_slice.shape[-1] % self.divisor)) // 2
            right_pad = (1 + self.divisor - (data_slice.shape[-1] % self.divisor)) // 2
            pad = [left_pad, right_pad]
            data_slice = F.pad(data_slice, pad=pad, value=0)  # This pads at the last dimension of a tensor.

            # Using the data acquisition method (fat suppression) may be useful later on.

        return data_slice, target_slice


class InputSliceTransformC2C:  # Assumes only Complex to Complex will be calculated.
    def __init__(self, mask_func, challenge, device, use_seed=True, divisor=1):
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')

        assert callable(mask_func)

        self.mask_func = mask_func
        self.challenge = challenge
        self.device = device
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, k_slice, target, attrs, file_name, slice_num):
        assert np.iscomplexobj(k_slice), 'kspace must be complex.'

        if k_slice.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            k_slice = np.expand_dims(k_slice, axis=0)
        elif k_slice.ndim != 3:  # Prevents possible errors.
            raise RuntimeError('Invalid slice shape. Please check input shape.')

        with torch.no_grad():  # Remove unnecessary gradient calculations.
            # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            kspace_target = to_tensor(k_slice).to(device=self.device)
            c_img_target = k_slice_to_chw(ifft2(kspace_target))  # Assumes only C2C will be calculated.

            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace_target, self.mask_func, seed)

            c_img_input = k_slice_to_chw(ifft2(masked_kspace))
            c_scale = torch.std(c_img_input)
            c_img_input *= (torch.tensor(1) / c_scale)
            c_bias = torch.mean(c_img_input)
            c_img_input -= c_bias

            margin = c_img_input.size(-1) % self.divisor

            if margin > 0:  # Cut off instead of adding padding.
                left = margin // 2
                right = (margin + 1) // 2
                assert c_img_input.size() == c_img_target.size()
                c_img_input = c_img_input[..., left:-right]
                c_img_target = c_img_target[..., left:-right]

        assert c_img_input.size() == c_img_target.size()

        return c_img_input, c_img_target, (c_scale, c_bias)
