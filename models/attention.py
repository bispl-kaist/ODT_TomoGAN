import torch
from torch import nn


# class GlobalMaxPooling2D(nn.Module):  # This tried to find why GMP was so much slower than GAP.
#     def __init__(self,):  # I think GMP is intrinsically slower than GAP for some reason.
#         super().__init__()
#
#     def forward(self, tensor):
#         assert isinstance(tensor, torch.Tensor)
#         assert tensor.dim() == 4
#         return F.max_pool2d(tensor, kernel_size=(tensor.size(-2), tensor.size(-1)))


class GlobalMaxPooling2d(nn.Module):
    """
    I have designed a custom Global Maximum Pooling Layer because of a weird bug in Adaptive Average Pooling
    that slows the system down enormously. Maximum pooling with a big kernel also fails for some reason.
    I have yet to find the cause of the bug but the success of this layer shows that it is not due to my design logic.
    I have verified that this layer is the same as AdaptiveMaxPool2d(1).
    Maybe create a unit-test as well later.
    """
    def __init__(self):
        super().__init__()

    def forward(self, tensor):
        return torch.max(torch.max(tensor, dim=-1, keepdim=True)[0], dim=-2, keepdim=True)[0]


class ChannelAttention(nn.Module):
    def __init__(self, num_chans, reduction=16, use_gap=True, use_gmp=True):
        super().__init__()
        self.gap = nn.AdaptiveAvgPool2d(1)  # Global Average Pooling.
        # self.gmp = nn.AdaptiveMaxPool2d(1)  # Global Maximum Pooling.
        self.gmp = GlobalMaxPooling2d()

        self.use_gap = use_gap
        self.use_gmp = use_gmp

        self.layer = nn.Sequential(
            nn.Linear(in_features=num_chans, out_features=num_chans // reduction),
            nn.ReLU(),
            nn.Linear(in_features=num_chans // reduction, out_features=num_chans)
        )

        self.sigmoid = nn.Sigmoid()

    def forward(self, tensor):
        if not (self.use_gap or self.use_gmp):
            return tensor

        batch, chans, _, _ = tensor.shape
        if self.use_gap and self.use_gmp:
            gap = self.gap(tensor).view(batch, chans)
            gmp = self.gmp(tensor).view(batch, chans)
            features = self.layer(gap) + self.layer(gmp)

        elif self.use_gap:
            gap = self.gap(tensor).view(batch, chans)
            features = self.layer(gap)

        elif self.use_gmp:
            gmp = self.gmp(tensor).view(batch, chans)
            features = self.layer(gmp)

        else:
            raise RuntimeError('Impossible logic. Please check for errors.')

        att = self.sigmoid(features).view(batch, chans, 1, 1)

        return tensor * att


class ChannelAvgPool(nn.Module):
    """
    Performs average pooling of tensors along the channel axis.
    """
    def __init__(self):
        super().__init__()

    def forward(self, tensor):
        return torch.mean(tensor, dim=1, keepdim=True)


class ChannelMaxPool(nn.Module):
    """
    Performs maximum pooling of tensors along the channel axis.
    """
    def __init__(self):
        super().__init__()

    def forward(self, tensor):
        return torch.max(tensor, dim=1, keepdim=True)[0]


class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7, dilation=1, use_cap=True, use_cmp=True):
        super().__init__()
        assert kernel_size % 2, 'The kernel is expected to have an odd size.'
        self.cap = ChannelAvgPool()
        self.cmp = ChannelMaxPool()

        self.use_cap = use_cap
        self.use_cmp = use_cmp

        padding = dilation * (kernel_size - 1) // 2
        self.conv = \
            nn.Conv2d(in_channels=2, out_channels=1, kernel_size=kernel_size, padding=padding, dilation=dilation)

        self.sigmoid = nn.Sigmoid()

    def forward(self, tensor):
        if not (self.use_cap or self.use_cmp):
            return tensor

        if self.use_cap and self.use_cmp:
            features = torch.cat([self.cap(tensor), self.cmp(tensor)], dim=1)
        elif self.use_cap:
            features = self.cap(tensor).expand(-1, 2, -1, -1)
        elif self.use_cmp:
            features = self.cmp(tensor).expand(-1, 2, -1, -1)
        else:
            raise RuntimeError('Impossible settings. Check for logic errors.')

        att = self.sigmoid(self.conv(features))
        return tensor * att.expand(1, 1, -1, -1)
