from collections import OrderedDict
from typing import Tuple, Union
from itertools import repeat
import collections.abc

import math
import logging
import numpy as np
import torch
import torch.nn.functional as F
import timm
from torch import nn
from torch.utils.checkpoint import checkpoint

import importlib.util

if importlib.util.find_spec('flash_attn'):
    FlashMHA = importlib.import_module('flash_attn.flash_attention').FlashMHA

from RET_CLIP.clip import _tokenizer
from RET_CLIP.clip.configuration_bert import BertConfig
from RET_CLIP.clip.modeling_bert import BertModel


class RestNetBasicBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride):
        super(RestNetBasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=stride, padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        output = self.conv1(x)
        output = F.relu(self.bn1(output))
        output = self.conv2(output)
        output = self.bn2(output)
        return F.relu(x + output)


class RestNetDownBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride):
        super(RestNetDownBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride[0], padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=stride[1], padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)
        self.extra = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride[0], padding=0),
            nn.BatchNorm2d(out_channels)
        )

    def forward(self, x):
        extra_x = self.extra(x)
        output = self.conv1(x)
        out = F.relu(self.bn1(output))

        out = self.conv2(out)
        out = self.bn2(out)
        return F.relu(extra_x + out)


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1):
        super().__init__()

        # all conv layers have stride 1. an avgpool is performed after the second convolution when stride > 1
        self.conv1 = nn.Conv2d(inplanes, planes, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)

        self.conv2 = nn.Conv2d(planes, planes, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.avgpool = nn.AvgPool2d(stride) if stride > 1 else nn.Identity()

        self.conv3 = nn.Conv2d(planes, planes * self.expansion, 1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)

        self.relu = nn.ReLU(inplace=True)
        self.downsample = None
        self.stride = stride

        if stride > 1 or inplanes != planes * Bottleneck.expansion:
            # downsampling layer is prepended with an avgpool, and the subsequent convolution has stride 1
            self.downsample = nn.Sequential(OrderedDict([
                ("-1", nn.AvgPool2d(stride)),
                ("0", nn.Conv2d(inplanes, planes * self.expansion, 1, stride=1, bias=False)),
                ("1", nn.BatchNorm2d(planes * self.expansion))
            ]))

    def forward(self, x: torch.Tensor):
        identity = x

        out = self.relu(self.bn1(self.conv1(x)))
        out = self.relu(self.bn2(self.conv2(out)))
        out = self.avgpool(out)
        out = self.bn3(self.conv3(out))

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)
        return out


class AttentionPool2d(nn.Module):
    def __init__(self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int = None):
        super().__init__()
        self.positional_embedding = nn.Parameter(torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5)
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
        self.num_heads = num_heads

    def forward(self, x):
        x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3]).permute(2, 0, 1)  # NCHW -> (HW)NC
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)  # (HW+1)NC
        x = x + self.positional_embedding[:, None, :].to(x.dtype)  # (HW+1)NC
        x, _ = F.multi_head_attention_forward(
            query=x, key=x, value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False
        )

        return x[0]


class ModifiedResNet(nn.Module):
    """
    A ResNet class that is similar to torchvision's but contains the following changes:
    - There are now 3 "stem" convolutions as opposed to 1, with an average pool instead of a max pool.
    - Performs anti-aliasing strided convolutions, where an avgpool is prepended to convolutions with stride > 1
    - The final pooling layer is a QKV attention instead of an average pool
    """

    def __init__(self, layers, output_dim, heads, input_resolution=224, width=64):
        super().__init__()
        self.output_dim = output_dim
        self.input_resolution = input_resolution

        # the 3-layer stem
        self.conv1 = nn.Conv2d(3, width // 2, kernel_size=3, stride=2, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(width // 2)
        self.conv2 = nn.Conv2d(width // 2, width // 2, kernel_size=3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(width // 2)
        self.conv3 = nn.Conv2d(width // 2, width, kernel_size=3, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(width)
        self.avgpool = nn.AvgPool2d(2)
        self.relu = nn.ReLU(inplace=True)

        # residual layers
        self._inplanes = width  # this is a *mutable* variable used during construction
        self.layer1 = self._make_layer(width, layers[0])
        self.layer2 = self._make_layer(width * 2, layers[1], stride=2)
        self.layer3 = self._make_layer(width * 4, layers[2], stride=2)
        self.layer4 = self._make_layer(width * 8, layers[3], stride=2)

        embed_dim = width * 32  # the ResNet feature dimension
        self.attnpool = AttentionPool2d(input_resolution // 32, embed_dim, heads, output_dim)

    def _make_layer(self, planes, blocks, stride=1):
        layers = [Bottleneck(self._inplanes, planes, stride)]

        self._inplanes = planes * Bottleneck.expansion
        for _ in range(1, blocks):
            layers.append(Bottleneck(self._inplanes, planes))

        return nn.Sequential(*layers)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        # FIXME support for non-transformer
        pass

    def forward(self, x):
        def stem(x):
            for conv, bn in [(self.conv1, self.bn1), (self.conv2, self.bn2), (self.conv3, self.bn3)]:
                x = self.relu(bn(conv(x)))
            x = self.avgpool(x)
            return x

        x = x.type(self.conv1.weight.dtype)
        x = stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.attnpool(x)

        return x


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)


class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None, use_flash_attention: bool = False):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head) if not use_flash_attention else FlashMHA(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.attn_mask = attn_mask
        self.use_flash_attention = use_flash_attention

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        if self.use_flash_attention:
            # Batch first is needed for FlashAttention. See https://github.com/HazyResearch/flash-attention/issues/84 for more information.
            return self.attn(x.transpose(1, 0))[0].transpose(1, 0)
        else:
            return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, x: torch.Tensor):
        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None,
                 use_flash_attention: bool = False):
        super().__init__()
        self.width = width
        self.layers = layers
        self.grad_checkpointing = False
        self.resblocks = nn.Sequential(
            *[ResidualAttentionBlock(width, heads, attn_mask, use_flash_attention) for _ in range(layers)])
        print('transformer int finished')

    def forward(self, x: torch.Tensor):
        if self.grad_checkpointing and not torch.jit.is_scripting():
            for r in self.resblocks:
                x = checkpoint(r, x)
            return x
        return self.resblocks(x)


class VisualTransformer(nn.Module):
    def __init__(self, input_resolution: int, patch_size: int, width: int, layers: int, heads: int, output_dim: int,
                 use_flash_attention: bool = False, use_rn_for_embed: bool = False):
        super().__init__()
        self.input_resolution = input_resolution
        self.grid_size = (self.input_resolution // patch_size, self.input_resolution // patch_size)
        self.output_dim = output_dim
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
        self.ln_pre = LayerNorm(width)

        self.transformer = Transformer(width, layers, heads, use_flash_attention=use_flash_attention)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))
        print('vit int finished')

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.transformer.grad_checkpointing = enable

    def random_masking(self, x, mask_ratio):
        N, L, D = x.shape  # batch, length, dim
        len_keep = int((L - 1) * (1 - mask_ratio))

        noise = torch.rand(N, L - 1, device=x.device)
        ids_shuffle = torch.argsort(noise, dim=1) + torch.ones(N, L - 1, device=x.device,
                                                               dtype=int)
        ids_keep = ids_shuffle[:, :len_keep]

        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        x0 = x[:, 0, :]
        x0 = x0.reshape(N, 1, D)
        x_masked_add = torch.cat([x0, x_masked], axis=1)
        return x_masked_add

    def forward(self, x: torch.Tensor, mask_ratio: float = 0.0, return_all_features=False):
        x = self.conv1(x)  # shape = [*, width, grid, grid]
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
             x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        x = x + self.positional_embedding.to(x.dtype)
        if mask_ratio != 0:
            x = self.random_masking(x, mask_ratio)
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        # Optionally return all features before projection
        if return_all_features:
            x=self.ln_post(x)
            x = x.permute(1, 0, 2)  # LND -> NLD
            return x
            
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_post(x[:, 0, :])  # Only CLS token
        if self.proj is not None:
            x = x @ self.proj
        return x


class Squeeze(nn.Module):
    def __init__(self, dim=None):
        super(Squeeze, self).__init__()
        self.dim = dim

    def forward(self, x):
        return torch.squeeze(x, dim=self.dim)


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = nn.ReLU()
        self.fc2 = nn.Linear(hidden_features, out_features)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.fc2(x)
        return x


class CLIP(nn.Module):
    def __init__(self,
                 embed_dim: int,
                 # vision
                 image_resolution: int,
                 vision_layers: Union[Tuple[int, int, int, int], int],
                 vision_width: int,
                 vision_patch_size: int,

                 # text
                 vocab_size: int,
                 text_attention_probs_dropout_prob: float,
                 text_hidden_act: str,
                 text_hidden_dropout_prob: float,
                 text_hidden_size: int,
                 text_initializer_range: float,
                 text_intermediate_size: int,
                 text_max_position_embeddings: int,
                 text_num_attention_heads: int,
                 text_num_hidden_layers: int,
                 text_type_vocab_size: int,
                 tokenizer=_tokenizer,
                 # vision head width, added this param for ViT-H
                 vision_head_width: int = 64,
                 use_flash_attention: bool = False,
                 ):
        super().__init__()

        if isinstance(vision_layers, (tuple, list)):
            vision_heads = vision_width * 32 // vision_head_width
            self.visual = ModifiedResNet(
                layers=vision_layers,
                output_dim=embed_dim,
                heads=vision_heads,
                input_resolution=image_resolution,
                width=vision_width
            )
        else:
            vision_heads = vision_width // vision_head_width
            self.visual = VisualTransformer(
                input_resolution=image_resolution,
                patch_size=vision_patch_size,
                width=vision_width,
                layers=vision_layers,
                heads=vision_heads,
                output_dim=embed_dim,
                use_flash_attention=use_flash_attention,
            )

        self.bert_config = BertConfig(
            vocab_size_or_config_json_file=vocab_size,
            hidden_size=text_hidden_size,
            num_hidden_layers=text_num_hidden_layers,
            num_attention_heads=text_num_attention_heads,
            intermediate_size=text_intermediate_size,
            hidden_act=text_hidden_act,
            hidden_dropout_prob=text_hidden_dropout_prob,
            attention_probs_dropout_prob=text_attention_probs_dropout_prob,
            max_position_embeddings=text_max_position_embeddings,
            type_vocab_size=text_type_vocab_size,
            initializer_range=text_initializer_range,
            layer_norm_eps=1e-12,
            use_flash_attention=use_flash_attention
        )
        self.bert = BertModel(self.bert_config)

        self.text_projection = nn.Sequential(nn.Linear(text_hidden_size, text_hidden_size),
                                             nn.ReLU(),
                                             nn.Linear(text_hidden_size, embed_dim))
        self.text_projection_left = nn.Sequential(nn.Linear(text_hidden_size, text_hidden_size),
                                                  nn.ReLU(),
                                                  nn.Linear(text_hidden_size, embed_dim))
        self.text_projection_right = nn.Sequential(nn.Linear(text_hidden_size, text_hidden_size),
                                                   nn.ReLU(),
                                                   nn.Linear(text_hidden_size, embed_dim))

        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(90))
        self.logit_scale_left = nn.Parameter(torch.ones([]) * np.log(90))
        self.logit_scale_right = nn.Parameter(torch.ones([]) * np.log(90))

        self.global_feature_mapping = nn.Linear(2 * embed_dim, embed_dim, bias=False)
        self.left_feature_mapping = nn.Linear(embed_dim, embed_dim, bias=False)
        self.right_feature_mapping = nn.Linear(embed_dim, embed_dim, bias=False)

        self.tokenizer = tokenizer

        self.initialize_parameters()

    def initialize_parameters(self):
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(90))
        self.logit_scale_left = nn.Parameter(torch.ones([]) * np.log(90))
        self.logit_scale_right = nn.Parameter(torch.ones([]) * np.log(90))

        if isinstance(self.visual, ModifiedResNet):
            if self.visual.attnpool is not None:
                std = self.visual.attnpool.c_proj.in_features ** -0.5
                nn.init.normal_(self.visual.attnpool.q_proj.weight, std=std)
                nn.init.normal_(self.visual.attnpool.k_proj.weight, std=std)
                nn.init.normal_(self.visual.attnpool.v_proj.weight, std=std)
                nn.init.normal_(self.visual.attnpool.c_proj.weight, std=std)

            for resnet_block in [self.visual.layer1, self.visual.layer2, self.visual.layer3, self.visual.layer4]:
                for name, param in resnet_block.named_parameters():
                    if name.endswith("bn3.weight"):
                        nn.init.zeros_(param)

        if self.text_projection is not None:
            for module in self.text_projection.modules():
                if isinstance(module, nn.Linear):
                    nn.init.normal_(module.weight, std=self.bert_config.hidden_size ** -0.5)

        if self.text_projection_left is not None:
            for module in self.text_projection_left.modules():
                if isinstance(module, nn.Linear):
                    nn.init.normal_(module.weight, std=self.bert_config.hidden_size ** -0.5)

        if self.text_projection_right is not None:
            for module in self.text_projection_right.modules():
                if isinstance(module, nn.Linear):
                    nn.init.normal_(module.weight, std=self.bert_config.hidden_size ** -0.5)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.visual.set_grad_checkpointing(enable)
        self.bert.set_grad_checkpointing(enable)

    @property
    def dtype(self):
        return self.visual.conv1.weight.dtype

    def encode_image(self, img_l, img_r, mask_ratio=0, return_all_features=False):
        if img_r is None:
            if isinstance(self.visual, ModifiedResNet):
                # mask_ratio > 0 (FLIP strategy) is currently only implemented for VisualTransformer.
                vision_feature = self.visual(img_l.type(self.dtype))
                return vision_feature
            vision_feature = self.visual(img_l.type(self.dtype), mask_ratio, return_all_features=True)
            return vision_feature
        if img_l is None:
            if isinstance(self.visual, ModifiedResNet):
                # mask_ratio > 0 (FLIP strategy) is currently only implemented for VisualTransformer.
                vision_feature = self.visual(img_r.type(self.dtype))
                return vision_feature
            vision_feature = self.visual(img_r.type(self.dtype), mask_ratio, return_all_features=True)
            return vision_feature
        if isinstance(self.visual, ModifiedResNet):
            # mask_ratio > 0 (FLIP strategy) is currently only implemented for VisualTransformer.
            left_feature = self.visual(img_l.type(self.dtype))
            right_feature = self.visual(img_r.type(self.dtype))
            vision_feature = torch.cat(
                (left_feature, right_feature), dim=1)

            return self.global_feature_mapping(vision_feature), self.single_feature_mapping(
                left_feature), self.single_feature_mapping(right_feature)
        left_feature = self.visual(img_l.type(self.dtype), mask_ratio, return_all_features=True)
        right_feature = self.visual(img_r.type(self.dtype), mask_ratio, return_all_features=True)
        vision_feature = torch.cat(
            (left_feature, right_feature), dim=1)

        return self.global_feature_mapping(vision_feature), self.left_feature_mapping(
            left_feature), self.right_feature_mapping(right_feature)

    def encode_text(self, text):
        pad_index = self.tokenizer.vocab['[PAD]']
        attn_mask = text.ne(pad_index).type(self.dtype)
        x = self.bert(text, attention_mask=attn_mask)[0].type(self.dtype)  # [batch_size, seq_length, hidden_size]

        text = self.text_projection(x[:, 0, :])
        text_left = self.text_projection_left(x[:, 0, :])
        text_right = self.text_projection_right(x[:, 0, :])
        return text, text_left, text_right

    def forward(self, img_l, img_r, text, mask_ratio=0):
        assert img_l is not None or img_r is not None or text is not None, "text and images cannot all be None!"

        if img_l is None and img_r is None:
            return self.encode_text(text)
        elif text is None and img_r is None:
            return self.encode_image(img_l=img_l, img_r=None)
        elif text is None and img_l is None:
            return self.encode_image(img_l=None, img_r=img_r)
        elif text is None:
            return self.encode_image(img_l, img_r)
        assert img_l is not None and img_r is not None, "both images is required!"

        image_features, left_features, right_features = self.encode_image(img_l, img_r, mask_ratio)
        text_features, text_features_left, text_features_right = self.encode_text(text)

        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        left_features = left_features / left_features.norm(dim=-1, keepdim=True)
        right_features = right_features / right_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        text_features_left = text_features_left / text_features_left.norm(dim=-1, keepdim=True)
        text_features_right = text_features_right / text_features_right.norm(dim=-1, keepdim=True)

        return image_features, text_features, text_features_left, text_features_right, left_features, right_features, self.logit_scale.exp(), self.logit_scale_left.exp(), self.logit_scale_right.exp()

    def get_similarity(self, img_l, img_r, text):
        image_features, _ = self.encode_image(img_l, img_r)
        text_features = self.encode_text(text)

        # normalized features
        image_features = image_features / image_features.norm(dim=1, keepdim=True)
        text_features = text_features / text_features.norm(dim=1, keepdim=True)

        # cosine similarity as logits
        logit_scale = self.logit_scale.exp()
        logits_per_image = logit_scale * image_features @ text_features.t()
        logits_per_text = logits_per_image.t()

        # shape = [global_batch_size, global_batch_size]
        return logits_per_image, logits_per_text


def convert_models_to_fp32(model):
    for p in model.parameters():
        p.data = p.data.float()
        if p.grad:
            p.grad.data = p.grad.data.float()


def convert_weights(model: nn.Module):
    """Convert applicable model parameters to fp16"""

    def _convert_weights_to_fp16(l):
        if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
            l.weight.data = l.weight.data.half()
            if l.bias is not None:
                l.bias.data = l.bias.data.half()

        if isinstance(l, nn.MultiheadAttention):
            for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
                tensor = getattr(l, attr)
                if tensor is not None:
                    tensor.data = tensor.data.half()

        if isinstance(l, BertModel):
            l.to(torch.half)

        for name in ["proj"]:
            if hasattr(l, name):
                attr = getattr(l, name)
                # if isinstance(attr, (nn.Conv1d, nn.Conv2d, nn.Linear)):
                #     attr.weight.data = attr.weight.data.half()
                #     if attr.bias is not None:
                #         attr.bias.data = attr.bias.data.half()
                if attr is not None:
                    attr.data = attr.data.half()  # 2023.10.14 should be 'attr.data'
        if isinstance(l, nn.Sequential):
            l.half()

        # for name in ["text_projection", "proj"]:
        #     if hasattr(l, name):
        #         attr = getattr(l, name)
        #         # if isinstance(attr, (nn.Conv1d, nn.Conv2d, nn.Linear)):
        #         #     attr.weight.data = attr.weight.data.half()
        #         #     if attr.bias is not None:
        #         #         attr.bias.data = attr.bias.data.half()
        #         if attr is not None:
        #             attr.data = attr.data.half()  # 2023.10.14 should be 'attr.data'

    # model.apply(_convert_weights_to_fp16)
    for param in model.parameters():
        param.data = param.data.half()
        if param.grad:
            param.grad.data = param.grad.data.float()
    # model.half()


def restore_model(model, clip_state_dict: dict, bert_state_dict: dict, use_flash_attention: bool):
    merged_state_dict = {}

    # use clip_state_dict to initialize the image encoder & logit scale
    if clip_state_dict is not None:
        for k, v in clip_state_dict.items():
            if k.startswith("visual") or k == "logit_scale":
                merged_state_dict[k] = v

    # use bert_state_dict to initialize the text encoder
    if bert_state_dict is not None:
        for k, v in bert_state_dict.items():
            if k.startswith("bert") and "bert.pooler" not in k:
                merged_state_dict[k] = v

    # adapt flash attention
    if use_flash_attention:
        merged_state_dict = convert_state_dict(merged_state_dict)

    convert_weights(model)
    resize_pos_embed(merged_state_dict, model)
    model.load_state_dict(merged_state_dict, strict=False)
    return model.eval()


def convert_state_dict(state_dict):
    """Adapt to Flash Attention"""
    if not state_dict:
        return state_dict

    prefix = 'module.' if list(state_dict.keys())[0].startswith('module') else ''

    if f'{prefix}visual.transformer.resblocks.0.attn.in_proj_weight' in state_dict:
        for k in list(state_dict.keys()):
            if 'attn.in_proj_weight' in k:
                state_dict[k.replace('attn.in_proj_weight', 'attn.Wqkv.weight')] = state_dict.pop(k)
            elif 'attn.in_proj_bias' in k:
                state_dict[k.replace('attn.in_proj_bias', 'attn.Wqkv.bias')] = state_dict.pop(k)
    elif f'{prefix}visual.transformer.resblocks.0.attn.Wqkv.weight' in state_dict:
        for k in list(state_dict.keys()):
            if 'attn.Wqkv.weight' in k:
                state_dict[k.replace('attn.Wqkv.weight', 'attn.in_proj_weight')] = state_dict.pop(k)
            elif 'attn.Wqkv.bias' in k:
                state_dict[k.replace('attn.Wqkv.bias', 'attn.in_proj_bias')] = state_dict.pop(k)

    if f'{prefix}bert.encoder.layer.0.attention.self.query.weight' in state_dict:
        i = 0
        while f'{prefix}bert.encoder.layer.{i}.attention.self.query.weight' in state_dict:
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.Wqkv.weight'] = torch.cat(
                (state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.query.weight'),
                 state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.key.weight'),
                 state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.value.weight'))
            )
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.Wqkv.bias'] = torch.cat(
                (state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.query.bias'),
                 state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.key.bias'),
                 state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.value.bias'))
            )
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.out_proj.weight'] = \
                state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.output.dense.weight')
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.out_proj.bias'] = \
                state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.output.dense.bias')
            i += 1
    elif f'{prefix}bert.encoder.layer.0.attention.self.Wqkv.weight' in state_dict:
        i = 0
        while f'{prefix}bert.encoder.layer.{i}.attention.self.Wqkv.weight' in state_dict:
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.query.weight'], \
                state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.key.weight'], \
                state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.value.weight'] = \
                torch.chunk(state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.Wqkv.weight'), chunks=3)
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.query.bias'], \
                state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.key.bias'], \
                state_dict[f'{prefix}bert.encoder.layer.{i}.attention.self.value.bias'] = \
                torch.chunk(state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.Wqkv.bias'), chunks=3)
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.output.dense.weight'] = \
                state_dict.pop(f'{prefix}bert.encoder.layer.{i}.attention.self.out_proj.weight')
            state_dict[f'{prefix}bert.encoder.layer.{i}.attention.output.dense.bias'] = \
                state_dict.pop(f'module.bert.encoder.layer.{i}.attention.self.out_proj.bias')
            i += 1

    return state_dict


def resize_pos_embed(state_dict, model, interpolation: str = 'bicubic', seq_dim=1, prefix=""):
    # Rescale the grid of position embeddings when loading from state_dict
    old_pos_embed = state_dict.get(prefix + 'visual.positional_embedding', None)
    model = model.module if hasattr(model, 'module') else model
    if old_pos_embed is None or not hasattr(model.visual, 'grid_size'):
        return
    grid_size = to_2tuple(model.visual.grid_size)
    extra_tokens = 1  # FIXME detect different token configs (ie no class token, or more)
    new_seq_len = grid_size[0] * grid_size[1] + extra_tokens
    if new_seq_len == old_pos_embed.shape[0]:
        return

    if extra_tokens:
        pos_emb_tok, pos_emb_img = old_pos_embed[:extra_tokens], old_pos_embed[extra_tokens:]
    else:
        pos_emb_tok, pos_emb_img = None, old_pos_embed
    old_grid_size = to_2tuple(int(math.sqrt(len(pos_emb_img))))

    logging.info('Resizing position embedding grid-size from %s to %s', old_grid_size, grid_size)
    pos_emb_img = pos_emb_img.reshape(1, old_grid_size[0], old_grid_size[1], -1).permute(0, 3, 1, 2)
    pos_emb_img = F.interpolate(
        pos_emb_img,
        size=grid_size,
        mode=interpolation,
        align_corners=True,
    )
    pos_emb_img = pos_emb_img.permute(0, 2, 3, 1).reshape(1, grid_size[0] * grid_size[1], -1)[0]
    if pos_emb_tok is not None:
        new_pos_embed = torch.cat([pos_emb_tok, pos_emb_img], dim=0)
    else:
        new_pos_embed = pos_emb_img
    state_dict[prefix + 'visual.positional_embedding'] = new_pos_embed


# From PyTorch internals
def _ntuple(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable):
            return x
        return tuple(repeat(x, n))

    return parse


to_1tuple = _ntuple(1)
to_2tuple = _ntuple(2)
to_3tuple = _ntuple(3)
to_4tuple = _ntuple(4)
to_ntuple = lambda n, x: _ntuple(n)(x)
