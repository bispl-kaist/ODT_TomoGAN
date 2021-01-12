import torch
from torch import nn
from data.data_transforms import ifft2, complex_abs, chw_to_k_slice, nchw_to_kspace, fft2, exp_weighting


class TrainOutputSliceTransform:
    """
    Data transformer for outputs of k-space learning.
    This transform is to be used for training and validation data.

    Please note that this transform is under development and can be altered at any time.
    """
    def __init__(self, k_slice_fn=None, img_slice_fn=None, amplification_factor=1):  # Make as I go.
        """
        'k_slice_fn' should be a function which takes both the k-space slice and the target as inputs and
        outputs a different k-space slice. It exists to do processing on the k-space domain.
        Importantly, it should crop the padded k-space to the original dimensions.
        Recall that k-space is padded before being put into the CNN model.
        Currently, only padding removal is performed.
        Also, the output is expected to be in the k-slice format, not the (C, H, W) format.
        To summarize, the output must be ready to undergo ifft2d.

        'img_slice_fn' should be a a function which takes the output image and the target as inputs and outputs
        the post-processed images. Image domain transformations should be done here.
        Currently, images are simply amplified by a scalar to prevent numerical underflow on the loss.
        Later, clipping may be introduced to remove the spike at the middle caused by the ifft2D.

        Please note that amplification_factor has an effect only when scalar_amplify is used as img_slice_fn.

        """
        self.amplification_factor = amplification_factor  # Used only for scalar amplify

        self.k_slice_fn = k_slice_fn if k_slice_fn is not None else self.restore_orig_shape
        self.img_slice_fn = img_slice_fn if img_slice_fn is not None else self.scalar_amplify

    def __call__(self, k_slice, target_slice):
        """
        'k_slice' should be in the (C, H, W) format.
        'target_slice' should be (Coil, Height, Width). It should be all real values. C = 2 * Coil
        k_slice should be padded while target_slice should not be padded.
        Singlecoil and Multicoil data both have the same dimensions. Singlecoil has Coil = 1
        """
        assert k_slice.dim() == target_slice.dim() == 3
        assert k_slice.size(0) / 2 == target_slice.size(0)

        # Remove padding, etc.
        k_slice = self.k_slice_fn(k_slice, target_slice)

        # Convert to image domain.
        recon_slice = complex_abs(ifft2(k_slice))

        # Image domain post-processing.
        recon_slice = self.img_slice_fn(recon_slice, target_slice)

        assert recon_slice.size() == target_slice.size(), 'Shape conversion went wrong somewhere.'
        return recon_slice

    def scalar_amplify(self, recon_slice, target_slice):  # Has input format like this for API consistency.
        return recon_slice * self.amplification_factor

    @staticmethod
    def restore_orig_shape(k_slice, target_slice):
        left_pad = (k_slice.size(-1) - target_slice.size(-1)) // 2
        right_pad = (1 + k_slice.size(-1) - target_slice.size(-1)) // 2
        k_slice = k_slice[..., left_pad:-right_pad]
        return chw_to_k_slice(k_slice)


class TrainBatchTransform:
    """
    Data transformer for outputs of k-space learning.
    This transform is to be used for training and validation data.

    Please note that this transform is under development and can be altered at any time.
    """
    def __init__(self, k_slice_fn=None, img_slice_fn=None, amplification_factor=1):  # Make as I go.
        """
        'k_slice_fn' should be a function which takes both the k-space slice and the target as inputs and
        outputs a different k-space slice. It exists to do processing on the k-space domain.
        Importantly, it should crop the padded k-space to the original dimensions.
        Recall that k-space is padded before being put into the CNN model.
        Currently, only padding removal is performed.
        Also, the output is expected to be in the k-slice format, not the (C, H, W) format.
        To summarize, the output must be ready to undergo ifft2d.

        'img_slice_fn' should be a a function which takes the output image and the target as inputs and outputs
        the post-processed images. Image domain transformations should be done here.
        Currently, images are simply amplified by a scalar to prevent numerical underflow on the loss.
        Later, clipping may be introduced to remove the spike at the middle caused by the ifft2D.

        Please note that amplification_factor has an effect only when scalar_amplify is used as img_slice_fn.

        """
        self.amplification_factor = amplification_factor  # Used only for scalar amplify

        self.k_slice_fn = k_slice_fn if k_slice_fn is not None else self.restore_orig_shape
        self.img_slice_fn = img_slice_fn if img_slice_fn is not None else self.scalar_amplify

    def __call__(self, k_slice, target_slice):
        """
        'k_slice' should be in the (C, H, W) format.
        'target_slice' should be (Coil, Height, Width). It should be all real values. C = 2 * Coil
        k_slice should be padded while target_slice should not be padded.
        Singlecoil and Multicoil data both have the same dimensions. Singlecoil has Coil = 1
        """
        # assert k_slice.dim() == target_slice.dim() == 3
        # assert k_slice.size(0) / 2 == target_slice.size(0)

        # Remove padding, etc.
        k_slice = self.k_slice_fn(k_slice, target_slice)

        # Convert to image domain.
        recon_slice = complex_abs(ifft2(k_slice))

        # Image domain post-processing.
        recon_slice = self.img_slice_fn(recon_slice, target_slice)

        assert recon_slice.size() == target_slice.size(), 'Shape conversion went wrong somewhere.'
        return recon_slice

    def scalar_amplify(self, recon_slice, target_slice):  # Has input format like this for API consistency.
        return recon_slice * self.amplification_factor

    @staticmethod
    def restore_orig_shape(k_slice, target_slice):
        left_pad = (k_slice.size(-1) - target_slice.size(-1)) // 2
        right_pad = (1 + k_slice.size(-1) - target_slice.size(-1)) // 2
        k_slice = k_slice[..., left_pad:-right_pad]
        return nchw_to_kspace(k_slice)


class SubmitOutputSliceTransform:
    def __init__(self):  # Make as I go.
        pass

    def __call__(self, k_slice, *args, **kwargs):
        pass


class OutputBatchTransformK2K(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, kspace_output, kspace_target, extra_params):
        if not kspace_output.size(0) == 1:
            raise NotImplementedError('Only single batch for now.')

        scaling, mask = extra_params

        # For removing width dimension padding. Recall that k-space form has 2 as last dim size.
        left = (kspace_output.size(-1) - kspace_target.size(-2)) // 2
        right = left + kspace_target.size(-2)

        # Cropping width dimension by pad. Multiply by scales to restore the original scaling.
        k_output = kspace_output[..., left:right] * scaling

        # Processing to k-space form. This is where the batch_size == 1 is important.
        kspace_recon = nchw_to_kspace(k_output)

        assert kspace_recon.size() == kspace_target.size(), 'Reconstruction and target sizes do not match.'

        return kspace_recon


class OutputBatchReplaceTransformK2K(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, kspace_output, kspace_target, extra_params):
        if not kspace_output.size(0) == 1:
            raise NotImplementedError('Only single batch for now.')

        scaling, mask = extra_params

        # For removing width dimension padding. Recall that k-space form has 2 as last dim size.
        left = (kspace_output.size(-1) - kspace_target.size(-2)) // 2
        right = left + kspace_target.size(-2)

        # Cropping width dimension by pad. Multiply by scales to restore the original scaling.
        k_output = kspace_output[..., left:right] * scaling

        # Processing to k-space form. This is where the batch_size == 1 is important.
        kspace_recon = nchw_to_kspace(k_output)

        assert kspace_recon.size() == kspace_target.size(), 'Reconstruction and target sizes do not match.'

        kspace_recon = kspace_recon * (1 - mask) + kspace_target * mask

        return kspace_recon


class WeightedOutputReplaceK2K(nn.Module):
    def __init__(self, log_amp_scale=1):
        super().__init__()
        self.log_amp_scale = log_amp_scale

    def forward(self, kspace_output, kspace_target, extra_params):
        k_scale, mask = extra_params

        # For removing width dimension padding. Recall that k-space form has 2 as last dim size.
        left = (kspace_output.size(-1) - kspace_target.size(-2)) // 2
        right = left + kspace_target.size(-2)

        # Processing to k-space form. This is where the batch_size == 1 is important.
        # 1. Crop padding. 2. Reshape to kspace shape. 3. Unweight k-space values. 4. Rescale to original scale.
        kspace_recon = exp_weighting(nchw_to_kspace(kspace_output[..., left:right]), scale=self.log_amp_scale) * k_scale

        assert kspace_recon.size() == kspace_target.size(), 'Reconstruction and target sizes do not match.'
        kspace_recon = kspace_recon * (1 - mask) + kspace_target * mask
        return kspace_recon


class OutputReplaceTransformK2C(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, kspace_output, c_img_target, extra_params):
        kspace_target, scaling, mask = extra_params

        # For removing width dimension padding. Recall that k-space form has 2 as last dim size.
        left = (kspace_output.size(-1) - kspace_target.size(-2)) // 2
        right = left + kspace_target.size(-2)

        # Cropping width dimension by pad. Multiply by scales to restore the original scaling.
        k_output = kspace_output[..., left:right] * scaling

        # Processing to k-space form. This is where the batch_size == 1 is important.
        kspace_recon = nchw_to_kspace(k_output)

        assert kspace_recon.size() == kspace_target.size(), 'Reconstruction and target sizes do not match.'

        kspace_recon = kspace_recon * (1 - mask) + kspace_target * mask

        c_img_recons = ifft2(kspace_recon)

        return c_img_recons


class WeightedOutputReplaceK2C(nn.Module):
    def __init__(self, log_amp_scale=1):
        super().__init__()
        self.log_amp_scale = log_amp_scale

    def forward(self, kspace_output, c_img_target, extra_params):
        kspace_target, k_scale, mask = extra_params

        # For removing width dimension padding. Recall that k-space form has 2 as last dim size.
        left = (kspace_output.size(-1) - kspace_target.size(-2)) // 2
        right = left + kspace_target.size(-2)

        # Cropping width dimension by pad. Multiply by scales to restore the original scaling.
        kspace_recon = exp_weighting(nchw_to_kspace(kspace_output[..., left:right]), scale=self.log_amp_scale) * k_scale
        assert kspace_recon.size() == kspace_target.size(), 'Reconstruction and target sizes do not match.'

        kspace_recon = kspace_recon * (1 - mask) + kspace_target * mask
        c_img_recons = ifft2(kspace_recon)

        return c_img_recons


class OutputTransformC2C(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, c_img_output, c_img_target, extra_params):
        c_scale, c_bias = extra_params
        # Bias is expected to be subtracted after standardization in pre-processing, hence the current ordering.
        c_img_output = (c_img_output + c_bias) * c_scale
        assert c_img_output.size() == c_img_target.size()
        return c_img_output
