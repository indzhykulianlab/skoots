from functools import partial
from numbers import Number
from typing import List, Tuple, Optional

import bism.backends
import bism.modules
import torch
import torch.nn as nn
from bism.models.spatial_embedding import SpatialEmbedding
from torch import Tensor
from yacs.config import CfgNode


def cfg_to_bism_model(cfg: CfgNode) -> nn.Module:
    """utility function to get a bism model from cfg"""

    _valid_model_constructors = {
        "bism_unext": bism.backends.unext.UNeXT_3D,
        "bism_unet": bism.backends.unet.UNet_3D,
    }

    _valid_model_blocks = {"block3d": bism.modules.convnext_block.Block3D}

    _valid_upsample_layers = {
        "upsamplelayer3d": bism.modules.upsample_layer.UpSampleLayer3D
    }

    _valid_normalization = {
        "layernorm": partial(
            bism.modules.layer_norm.LayerNorm, data_format="channels_first"
        )
    }

    _valid_activations = {
        "gelu": torch.nn.GELU,
        "relu": torch.nn.ReLU,
        "silu": torch.nn.SiLU,
        "selu": torch.nn.SELU,
    }

    _valid_concat_blocks = {"concatconv3d": bism.modules.concat.ConcatConv3D}

    model_config = [
        cfg.MODEL.DIMS,
        cfg.MODEL.DEPTHS,
        cfg.MODEL.KERNEL_SIZE,
        cfg.MODEL.DROP_PATH_RATE,
        cfg.MODEL.LAYER_SCALE_INIT_VALUE,
        cfg.MODEL.ACTIVATION,
        cfg.MODEL.BLOCK,
        cfg.MODEL.CONCAT_BLOCK,
        cfg.MODEL.UPSAMPLE_BLOCK,
        cfg.MODEL.NORMALIZATION,
    ]

    model_kwargs = [
        "dims",
        "depths",
        "kernel_size",
        "drop_path_rate",
        "layer_scale_init_value",
        "activation",
        "block",
        "concat_conv",
        "upsample_layer",
        "normalization",
    ]

    valid_dicts = [
        None,
        None,
        None,
        None,
        None,
        _valid_activations,
        _valid_model_blocks,
        _valid_concat_blocks,
        _valid_upsample_layers,
        _valid_normalization,
    ]

    kwarg = {}
    for kw, config, vd in zip(model_kwargs, model_config, valid_dicts):
        if vd is not None:
            if config in vd:
                kwarg[kw] = vd[config]
            else:
                raise RuntimeError(
                    f"{config} is not a valid config option for {kw}. Valid options are: {vd.keys()}"
                )
        else:
            kwarg[kw] = config

    if cfg.MODEL.ARCHITECTURE in _valid_model_constructors:
        backbone = _valid_model_constructors[cfg.MODEL.ARCHITECTURE]
    else:
        raise RuntimeError(
            f"{cfg.MODEL.ARCHITECTURE} is not a valid model constructor, valid options are: {_valid_model_constructors.keys()}"
        )

    backbone = backbone(cfg.MODEL.IN_CHANNELS, cfg.MODEL.OUT_CHANNELS, **kwarg)
    model = SpatialEmbedding(backbone)

    return model


def calculate_indexes(
    pad_size: int, eval_image_size: int, image_shape: int, padded_image_shape: int
) -> List[List[int]]:
    """
    This calculates indexes for the complete evaluation of an arbitrarily large image by unet.
    each index is offset by eval_image_size, but has a width of eval_image_size + pad_size * 2.
    Unet needs padding on each side of the evaluation to ensure only full convolutions are used
    in generation of the final mask. If the algorithm cannot evenly create indexes for
    padded_image_shape, an additional index is added at the end of equal size.



    :param pad_size: int corresponding to the amount of padding on each side of the
                     padded image
    :param eval_image_size: int corresponding to the shape of the image to be used for
                            the final mask
    :param image_shape: int Shape of image before padding is applied
    :param padded_image_shape: int Shape of image after padding is applied

    :return: List of lists corresponding to the indexes
    """

    # We want to account for when the eval image size is super big, just return index for the whole image.
    if eval_image_size + (2 * pad_size) > image_shape:
        return [[0, image_shape - 1]]

    try:
        ind_list = torch.arange(0, image_shape, eval_image_size)
    except RuntimeError:
        raise RuntimeError(
            f"Calculate_indexes has incorrect values {pad_size} | {image_shape} | {eval_image_size}:\n"
            f"You are likely trying to have a chunk smaller than the set evaluation image size. "
            "Please decrease number of chunks."
        )
    ind = []
    for i, z in enumerate(ind_list):
        if i == 0:
            continue
        z1 = int(ind_list[i - 1])
        z2 = int(z - 1) + (2 * pad_size)
        if z2 < padded_image_shape:
            ind.append([z1, z2])
        else:
            break
    if (
        not ind
    ):  # Sometimes z is so small the first part doesnt work. Check if z_ind is empty, if it is do this!!!
        z1 = 0
        z2 = eval_image_size + pad_size * 2
        ind.append([z1, z2])
        ind.append(
            [padded_image_shape - (eval_image_size + pad_size * 2), padded_image_shape]
        )
    else:  # we always add at the end to ensure that the whole thing is covered.
        z1 = padded_image_shape - (eval_image_size + pad_size * 2)
        z2 = padded_image_shape - 1
        ind.append([z1, z2])
    return ind


def get_dtype_offset(dtype: str = "uint16", image_max: Optional[Number] = None) -> int:
    """
    Returns the scaling factor such that
    such that :math:`\frac{image}{f} \in [0, ..., 1]`

    Supports: uint16, uint8, uint12, float64

    :param dtype: String representation of the data type.
    :param image_max: Returns image max if the dtype is not supported.
    :return: Integer scale factor
    """

    encoding = {
        "uint16": 2**16,
        "uint8": 2**8,
        "uint12": 2**12,
        "float64": 1,
    }
    dtype = str(dtype)
    if dtype in encoding:
        scale = encoding[dtype]
    else:
        print(
            f"\x1b[1;31;40m"
            + f"ERROR: Unsupported dtype: {dtype}. Currently support: {[k for k in encoding]}"
            + "\x1b[0m"
        )
        scale = None
        if image_max:
            print(f"Appropriate Scale Factor inferred from image maximum: {image_max}")
            if image_max <= 256:
                scale = 256
            else:
                scale = image_max
    return scale


@torch.jit.script
def _crop3d(img: Tensor, x: int, y: int, z: int, w: int, h: int, d: int) -> Tensor:
    """
    torch scriptable function which crops an image

    :param img: torch.Tensor image of shape [C, X, Y, Z]
    :param x: x coord of crop box
    :param y: y coord of crop box
    :param z: z coord of crop box
    :param w: width of crop box
    :param h: height of crop box
    :param d: depth of crop box
    :return:
    """
    return img[..., x : x + w, y : y + h, z : z + d]


@torch.jit.script
def _crop2d(img: Tensor, x: int, y: int, w: int, h: int) -> Tensor:
    """
    torch scriptable function which crops an image

    :param img: torch.Tensor image of shape [C, X, Y, Z]
    :param x: x coord of crop box
    :param y: y coord of crop box
    :param z: z coord of crop box
    :param w: width of crop box
    :param h: height of crop box
    :param d: depth of crop box
    :return:
    """

    return img[..., x : x + w, y : y + h]


@torch.jit.script
def crop_to_identical_size(a: Tensor, b: Tensor) -> Tuple[Tensor, Tensor]:
    """
    Crops Tensor a to the shape of Tensor b, then crops Tensor b to the shape of Tensor a.

    :param a: torch.
    :param b:
    :return:
    """
    if a.ndim < 3:
        raise RuntimeError(
            "Only supports tensors with minimum 3dimmensions and shape [..., X, Y, Z]"
        )

    a = _crop3d(a, x=0, y=0, z=0, w=b.shape[-3], h=b.shape[-2], d=b.shape[-1])
    b = _crop3d(b, x=0, y=0, z=0, w=a.shape[-3], h=a.shape[-2], d=a.shape[-1])
    return a, b


@torch.jit.script
def cantor2(a: Tensor, b: Tensor) -> Tensor:
    "Hashes two combination of tensors together"
    return 0.5 * (a + b) * (a + b + 1) + b


@torch.jit.script
def cantor3(a: Tensor, b: Tensor, c: Tensor) -> Tensor:
    "Hashes three combination of tensors together"
    return (0.5 * (cantor2(a, b) + c) * (cantor2(a, b) + c + 1) + c).int()


@torch.jit.script
def identical_rows(a: Tensor, b: Tensor) -> Tensor:
    """
    Given two matrices of identical size, determines the indices of identical rows.

    :param a: [N, 3] torch.Tensor
    :param b: [N, 3] torch.Tensor

    :return: Indicies of identical rows
    """
    a = cantor3(a[:, 0], a[:, 1], a[:, 2])
    b = cantor3(b[:, 0], b[:, 1], b[:, 2])

    _inv = torch.reciprocal(b)

    ind = torch.sum(a.unsqueeze(-1) @ _inv.unsqueeze(0), dim=0).gt(0)

    return ind
