# Copyright (c) OpenMMLab. All rights reserved.
import copy
import warnings
from typing import List, Tuple, Union

import torch
from torch import Tensor
import torch.nn as nn
from mmdet.registry import MODELS
from mmdet.structures import SampleList
from mmdet.utils import ConfigType, OptConfigType, OptMultiConfig
from mmdet.models.detectors.base import BaseDetector
from mmdet.apis import init_backbone_neck
from projects.BAANet.baanet.modules.baa_gate import DWConv
from mmengine.runner import load_checkpoint
from mmengine.model.utils import revert_sync_batchnorm
from mmengine.config import Config



@MODELS.register_module()
class TwoStageFGDThermalRPNFstDetector(BaseDetector):
    """Base class for two-stage detectors.

    Two-stage detectors typically consisting of a region proposal network and a
    task-specific regression head.
    """

    def __init__(self,
                 backbone: ConfigType,
                 neck: OptConfigType = None,
                 distill_cfg: OptConfigType = None,
                 rpn_head: OptConfigType = None,
                 roi_head: OptConfigType = None,
                 train_cfg: OptConfigType = None,
                 test_cfg: OptConfigType = None,
                 teacher_cfg: OptConfigType = None,
                 teacher_pretrained: OptConfigType = None,
                 data_preprocessor: OptConfigType = None,
                 init_cfg: OptMultiConfig = None) -> None:
        super().__init__(
            data_preprocessor=data_preprocessor, init_cfg=init_cfg)
        self.backbone = MODELS.build(backbone)
        self.backbone_ir = MODELS.build(backbone)
        self.fusion_conv = DWConv(in_channels=256 * 2, out_channels=256, ksize=3).cuda()
        if neck is not None:
            self.neck = MODELS.build(neck)
            self.neck_ir = MODELS.build(neck)

        if rpn_head is not None:
            rpn_train_cfg = train_cfg.rpn if train_cfg is not None else None
            rpn_head_ = rpn_head.copy()
            rpn_head_.update(train_cfg=rpn_train_cfg, test_cfg=test_cfg.rpn)
            rpn_head_num_classes = rpn_head_.get('num_classes', None)
            if rpn_head_num_classes is None:
                rpn_head_.update(num_classes=1)
            else:
                if rpn_head_num_classes != 1:
                    warnings.warn(
                        'The `num_classes` should be 1 in RPN, but get '
                        f'{rpn_head_num_classes}, please set '
                        'rpn_head.num_classes = 1 in your config file.')
                    rpn_head_.update(num_classes=1)
            self.rpn_head = MODELS.build(rpn_head_)
            # self.rpn_head_rgb = MODELS.build(rpn_head_)
            # self.rpn_head_fusion = MODELS.build(rpn_head_)

        if roi_head is not None:
            # update train and test cfg here for now
            # TODO: refactor assigner & sampler
            rcnn_train_cfg = train_cfg.rcnn if train_cfg is not None else None
            roi_head.update(train_cfg=rcnn_train_cfg)
            roi_head.update(test_cfg=test_cfg.rcnn)
            self.roi_head = MODELS.build(roi_head)
            # self.roi_head_rgb = MODELS.build(roi_head)
            # self.roi_head_ir = MODELS.build(roi_head)

        self.distill = True
        if teacher_cfg == None and teacher_pretrained == None:
            self.distill = False
        
        self.train_cfg = train_cfg
        self.test_cfg = test_cfg
        self.__set_eval = False
        self.teacher_pretrained = teacher_pretrained
        if self.distill:
            self.teacher_backbone, self.teacher_backbone_ir, self.teacher_neck, self.teacher_neck_ir, self.teacher_fusion_conv, self.teacher_fusion_module \
                = self._load_distilled_weights(teacher_cfg,teacher_pretrained)
            
            self.distill_losses = nn.ModuleDict()
            self.distill_cfg = distill_cfg

        
            for item_loc in distill_cfg:

                for item_loss in item_loc.methods:
                    loss_name = item_loss.name
                    self.distill_losses[loss_name] = MODELS.build(item_loss)

        # import pdb
        # pdb.set_trace()
        # prinResNet(  (conv1): Conv2d(3, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)  (bn1): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)  (relu): ReLU(inplace=True)  (maxpool): MaxPool2d(kernel_size=3, stride=2, padding=1, dilation=1, ceil_mode=False)  (layer1): ResLayer(    (0): Bottleneck(      (conv1): Conv2d(64, 64, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)      (downsample): Sequential(        (0): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)        (1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      )    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (1): Bottleneck(      (conv1): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (2): Bottleneck(      (conv1): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}  )  (layer2): ResLayer(    (0): Bottleneck(      (conv1): Conv2d(256, 128, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(128, 128, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)      (downsample): Sequential(        (0): Conv2d(256, 512, kernel_size=(1, 1), stride=(2, 2), bias=False)        (1): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      )    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (1): Bottleneck(      (conv1): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (2): Bottleneck(      (conv1): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (3): Bottleneck(      (conv1): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}  )  (layer3): ResLayer(    (0): Bottleneck(      (conv1): Conv2d(512, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)      (downsample): Sequential(        (0): Conv2d(512, 1024, kernel_size=(1, 1), stride=(2, 2), bias=False)        (1): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      )    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (1): Bottleneck(      (conv1): Conv2d(1024, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (2): Bottleneck(      (conv1): Conv2d(1024, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (3): Bottleneck(      (conv1): Conv2d(1024, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (4): Bottleneck(      (conv1): Conv2d(1024, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (5): Bottleneck(      (conv1): Conv2d(1024, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(256, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(256, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(1024, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}  )  (layer4): ResLayer(    (0): Bottleneck(      (conv1): Conv2d(1024, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(512, 512, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)      (downsample): Sequential(        (0): Conv2d(1024, 2048, kernel_size=(1, 1), stride=(2, 2), bias=False)        (1): BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      )    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (1): Bottleneck(      (conv1): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}    (2): Bottleneck(      (conv1): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn1): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv2): Conv2d(512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)      (bn2): BatchNorm2d(512, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (conv3): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False)      (bn3): BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)      (relu): ReLU(inplace=True)    )    init_cfg={'type': 'Constant', 'val': 0, 'override': {'name': 'norm3'}}  ))init_cfg=[{'type': 'Kaiming', 'layer': 'Conv2d'}, {'type': 'Constant', 'val': 1, 'layer': ['_BatchNorm', 'GroupNorm']}]t(self.teacher_backbone.training)
        # checkpoint = torch.load(self.teacher_pretrained)
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'] == dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'].to('cpu'))
        # # print(dict(self.named_parameters()).keys())
        # print(dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'])
        

    def _load_from_state_dict(self, state_dict: dict, prefix: str,
                              local_metadata: dict, strict: bool,
                              missing_keys: Union[List[str], str],
                              unexpected_keys: Union[List[str], str],
                              error_msgs: Union[List[str], str]) -> None:
        """Exchange bbox_head key to rpn_head key when loading single-stage
        weights into two-stage model."""
        bbox_head_prefix = prefix + '.bbox_head' if prefix else 'bbox_head'
        bbox_head_keys = [
            k for k in state_dict.keys() if k.startswith(bbox_head_prefix)
        ]
        rpn_head_prefix = prefix + '.rpn_head' if prefix else 'rpn_head'
        rpn_head_keys = [
            k for k in state_dict.keys() if k.startswith(rpn_head_prefix)
        ]
        if len(bbox_head_keys) != 0 and len(rpn_head_keys) == 0:
            for bbox_head_key in bbox_head_keys:
                rpn_head_key = rpn_head_prefix + \
                               bbox_head_key[len(bbox_head_prefix):]
                state_dict[rpn_head_key] = state_dict.pop(bbox_head_key)
        def is_substring_of_key(dict_obj, sub_str):
            return any(sub_str in key for key in dict_obj.keys())
        if not is_substring_of_key(state_dict, 'backbone_ir'):
            ori_dict = copy.deepcopy(state_dict)
            for k, v in ori_dict.items():
                if k.split('.')[0] == 'backbone':
                    state_dict[k.replace('backbone', 'backbone_ir')] = v
                if k.split('.')[0] == 'neck':
                    state_dict[k.replace('neck', 'neck_ir')] = v
                if k.split('.')[0] == 'rpn_head':
                    state_dict[k.replace('rpn_head', 'rpn_head_fusion')] = v
                if k.split('.')[0] == 'roi_head':
                    state_dict[k.replace('roi_head', 'roi_head_rgb')] = v
                    state_dict[k.replace('roi_head', 'roi_head_ir')] = v
            del ori_dict
        super()._load_from_state_dict(state_dict, prefix, local_metadata,
                                      strict, missing_keys, unexpected_keys,
                                      error_msgs)

    def _load_distilled_weights(self,teacher_cfg,teacher_pretrained,device: str = 'cuda:0'):
        config = Config.fromfile(teacher_cfg)
        # import pdb 
        # pdb.set_trace()

        # checkpoint = torch.load(teacher_pretrained)
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'])
        teacher_backbone = MODELS.build(config.model.backbone)
        teacher_backbone_ir = MODELS.build(config.model.backbone)
        teacher_neck = MODELS.build(config.model.neck)
        teacher_neck_ir = MODELS.build(config.model.neck)
        teacher_fusion_conv = DWConv(in_channels=256 * 2, out_channels=256, ksize=3).cuda()
        teacher_fusion_module = MODELS.build(config.model.fusion_module)


        teacher_neck = revert_sync_batchnorm(teacher_neck)
        teacher_backbone = revert_sync_batchnorm(teacher_backbone)
        teacher_neck_ir = revert_sync_batchnorm(teacher_neck_ir)
        teacher_backbone_ir = revert_sync_batchnorm(teacher_backbone_ir)

        load_checkpoint(teacher_backbone, teacher_pretrained, map_location='cuda:0', revise_keys=[(r'^backbone\.', '')])
        load_checkpoint(teacher_neck, teacher_pretrained, map_location='cuda:0',revise_keys=[(r'^neck\.', '')])
        load_checkpoint(teacher_backbone_ir, teacher_pretrained, map_location='cuda:0',revise_keys=[(r'^backbone_ir\.', '')])
        load_checkpoint(teacher_neck_ir, teacher_pretrained, map_location='cuda:0',revise_keys=[(r'^neck_ir\.', '')])
        load_checkpoint(teacher_fusion_module, teacher_pretrained, map_location='cuda:0',revise_keys=[(r'^fusion_module\.', '')])
        load_checkpoint(teacher_fusion_conv, teacher_pretrained, map_location='cuda:0',revise_keys=[(r'^fusion_conv\.', '')])

        

        return teacher_backbone, teacher_backbone_ir, teacher_neck, teacher_neck_ir, teacher_fusion_conv, teacher_fusion_module

    def _set_distilled_module_eval(self):

        self.teacher_backbone.eval()
        self.teacher_backbone_ir.eval()
        self.teacher_neck.eval()
        self.teacher_neck_ir.eval()
        self.teacher_fusion_conv.eval()
        self.teacher_fusion_module.eval()

    @property
    def with_rpn(self) -> bool:
        """bool: whether the detector has RPN"""
        return hasattr(self, 'rpn_head') and self.rpn_head is not None

    @property
    def with_roi_head(self) -> bool:
        """bool: whether the detector has a RoI head"""
        return hasattr(self, 'roi_head') and self.roi_head is not None

    def extract_feat(self, batch_inputs: Tensor) -> Tuple[Tensor]:
        """Extract features.

        Args:
            batch_inputs (Tensor): Image tensor with shape (N, C, H ,W).

        Returns:
            tuple[Tensor]: Multi-level features that may have
            different resolutions.
        """
        x_rgb = self.backbone(batch_inputs[:, :3, :, :])
        x_ir = self.backbone_ir(batch_inputs[:, 3:, :, :])
        
        if self.with_neck:
            x_rgb = self.neck(x_rgb)
            x_ir = self.neck_ir(x_ir)
            # print(buffer_dict.keys())
        return x_rgb, x_ir
    
    def extract_teacher_feat(self, batch_inputs: Tensor) -> Tuple[Tensor]:
        """Extract features.

        Args:
            batch_inputs (Tensor): Image tensor with shape (N, C, H ,W).

        Returns:
            tuple[Tensor]: Multi-level features that may have
            different resolutions.
        """
        x_t = self.teacher_backbone(batch_inputs[:, :3, :, :])
        x_t_ir = self.teacher_backbone_ir(batch_inputs[:, 3:, :, :])
        if self.with_neck:
            x_t = self.teacher_neck(x_t)
            x_t_ir = self.teacher_neck_ir(x_t_ir)
        
        # fused_feature_maps = []
        # for i in [0, 1, 2]:
        #     fused_feature_maps.append(self.teacher_fusion_conv(torch.cat([x_t[i], x_t_ir[i]], dim=1)))

        # for i in [3, 4]:
        #     fused_feature_maps.append(self.teacher_fusion_module[i](x_t[i], x_t_ir[i]))
        # return fused_feature_maps
        return x_t , x_t_ir

    def _forward(self, batch_inputs: Tensor,
                 batch_data_samples: SampleList) -> tuple:
        """Network forward process. Usually includes backbone, neck and head
        forward without any post-processing.

        Args:
            batch_inputs (Tensor): Inputs with shape (N, C, H, W).
            batch_data_samples (list[:obj:`DetDataSample`]): Each item contains
                the meta information of each image and corresponding
                annotations.

        Returns:
            tuple: A tuple of features from ``rpn_head`` and ``roi_head``
            forward.
        """
        results = ()
        x, x_ir = self.extract_feat(batch_inputs)
        # if self.distill:
        #     x_t,x_t_ir = self.extract_teacher_feat(batch_inputs)

        if self.with_rpn:
            rpn_results_list = self.rpn_head.predict(
                x_ir, batch_data_samples, rescale=False)
        else:
            assert batch_data_samples[0].get('proposals', None) is not None
            rpn_results_list = [
                data_sample.proposals for data_sample in batch_data_samples
            ]

        

        fused_feature_maps = []
        for i in [0, 1, 2, 3, 4]:
            fused_feature_maps.append(self.fusion_conv(torch.cat([x[i], x_ir[i]], dim=1)))

        
        roi_outs = self.roi_head.forward(fused_feature_maps, rpn_results_list,
                                         batch_data_samples)
        results = results + (roi_outs, )
        return results

    def loss(self, batch_inputs: Tensor,
             batch_data_samples: SampleList) -> dict:
        """Calculate losses from a batch of inputs and data samples.

        Args:
            batch_inputs (Tensor): Input images of shape (N, C, H, W).
                These should usually be mean centered and std scaled.
            batch_data_samples (List[:obj:`DetDataSample`]): The batch
                data samples. It usually includes information such
                as `gt_instance` or `gt_panoptic_seg` or `gt_sem_seg`.

        Returns:
            dict: A dictionary of loss components
        """


        x,x_ir = self.extract_feat(batch_inputs)
        
        if self.distill:
            with torch.no_grad():
                if not self.__set_eval:
                    self._set_distilled_module_eval()
                    x_t, x_t_ir = self.extract_teacher_feat(batch_inputs)

        # import pdb 
        # pdb.set_trace()
        # checkpoint = torch.load(self.teacher_pretrained)
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'] == dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'].to('cpu'))
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'])
        # print(dict(checkpoint['state_dict']).keys())
        # print(dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'].to('cpu'))


        
        losses = dict()
        # import pdb 
        # pdb.set_trace()
        # checkpoint = torch.load(self.teacher_pretrained)
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'] == dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'].to('cpu'))
        # print(dict(checkpoint['state_dict'])['backbone.layer1.1.conv2.weight'])
        # print(dict(self.named_parameters())['teacher_backbone.layer1.1.conv2.weight'].to('cpu'))
        # RPN forward and loss
        if self.with_rpn:
            proposal_cfg = self.train_cfg.get('rpn_proposal',
                                              self.test_cfg.rpn)
            rpn_data_samples = copy.deepcopy(batch_data_samples)
            # set cat_id of gt_labels to 0 in RPN
            for data_sample in rpn_data_samples:
                data_sample.gt_instances.labels = \
                    torch.zeros_like(data_sample.gt_instances.labels)

            rpn_losses, rpn_results_list = self.rpn_head.loss_and_predict(
                x_ir, rpn_data_samples, proposal_cfg=proposal_cfg)
            # avoid get same name with roi_head loss
            keys = rpn_losses.keys()
            for key in list(keys):
                if 'loss' in key and 'rpn' not in key:
                    rpn_losses[f'ir_rpn_{key}'] = rpn_losses.pop(key)
            losses.update(rpn_losses)
        else:
            assert batch_data_samples[0].get('proposals', None) is not None
            # use pre-defined proposals in InstanceData for the second stage
            # to extract ROI features.
            rpn_results_list = [
                data_sample.proposals for data_sample in batch_data_samples
            ]


        fused_feature_maps = []
        for i in [0, 1, 2, 3, 4]:
            fused_feature_maps.append(self.fusion_conv(torch.cat([x[i], x_ir[i]], dim=1)))

        # rpn_results_list_rgb = copy.deepcopy(rpn_results_list)
        # rpn_results_list_ir = copy.deepcopy(rpn_results_list)

        # roi_losses_rgb = self.roi_head_rgb.loss(x, rpn_results_list_rgb,
        #                                 batch_data_samples)
        # keys = roi_losses_rgb.keys()
        # for key in list(keys):
        #     if 'loss' in key:
        #         roi_losses_rgb[f'rgb_roi_{key}'] = roi_losses_rgb.pop(key)
        # # if self.if_double_loss:
        # #     for k, v in roi_losses_rgb.items():
        # #         roi_losses_rgb[k] = v * 1.5
        # losses.update(roi_losses_rgb)
        # # ir
        # batch_data_samples_ir = copy.deepcopy(batch_data_samples)

        
        # roi_losses_ir = self.roi_head_ir.loss(x_ir, rpn_results_list_ir,
        #                                 batch_data_samples_ir)
        # keys = roi_losses_ir.keys()
        # for key in list(keys):
        #     if 'loss' in key:
        #         roi_losses_ir[f'ir_roi_{key}'] = roi_losses_ir.pop(key)
        # losses.update(roi_losses_ir)

        roi_losses = self.roi_head.loss(fused_feature_maps, rpn_results_list,
                                        batch_data_samples)
        losses.update(roi_losses)
        if self.distill:
            distill_losses = dict()

            # buffer_dict = dict(self.named_buffers())
            # print([x for x in buffer_dict.keys() if x.startswith('teacher_neck')])
            for item_loc in self.distill_cfg:
                
                # student_module = 'student_' + item_loc.student_module.replace('.','_')
                # teacher_module = 'teacher_' + item_loc.teacher_module.replace('.','_')
                
                # student_feat = buffer_dict[student_module]
                # teacher_feat = buffer_dict[teacher_module]

                
                for item_loss in item_loc.methods:
                    loss_name = item_loss.name
                    if 'ir' in loss_name:
                        distill_losses[loss_name] = self.distill_losses[loss_name](x_ir[int(loss_name[-1])],x_t_ir[int(loss_name[-1])],batch_data_samples)
                    else:
                        distill_losses[loss_name] = self.distill_losses[loss_name](x[int(loss_name[-1])],x_t[int(loss_name[-1])],batch_data_samples)
            losses.update(distill_losses)
        return losses

    def predict(self,
                batch_inputs: Tensor,
                batch_data_samples: SampleList,
                rescale: bool = True) -> SampleList:
        """Predict results from a batch of inputs and data samples with post-
        processing.

        Args:
            batch_inputs (Tensor): Inputs with shape (N, C, H, W).
            batch_data_samples (List[:obj:`DetDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instance`, `gt_panoptic_seg` and `gt_sem_seg`.
            rescale (bool): Whether to rescale the results.
                Defaults to True.

        Returns:
            list[:obj:`DetDataSample`]: Return the detection results of the
            input images. The returns value is DetDataSample,
            which usually contain 'pred_instances'. And the
            ``pred_instances`` usually contains following keys.

                - scores (Tensor): Classification scores, has a shape
                    (num_instance, )
                - labels (Tensor): Labels of bboxes, has a shape
                    (num_instances, ).
                - bboxes (Tensor): Has a shape (num_instances, 4),
                    the last dimension 4 arrange as (x1, y1, x2, y2).
                - masks (Tensor): Has a shape (num_instances, H, W).
        """

        assert self.with_bbox, 'Bbox head must be implemented.'
        x,x_ir = self.extract_feat(batch_inputs)

        # If there are no pre-defined proposals, use RPN to get proposals
        if batch_data_samples[0].get('proposals', None) is None:
            rpn_results_list = self.rpn_head.predict(
                x_ir, batch_data_samples, rescale=False)
        else:
            rpn_results_list = [
                data_sample.proposals for data_sample in batch_data_samples
            ]
        fused_feature_maps = []
        for i in [0, 1, 2, 3, 4]:
            fused_feature_maps.append(self.fusion_conv(torch.cat([x[i], x_ir[i]], dim=1)))

        
        
        results_list = self.roi_head.predict(
            fused_feature_maps, rpn_results_list, batch_data_samples, rescale=rescale)

        batch_data_samples = self.add_pred_to_datasample(
            batch_data_samples, results_list)
        return batch_data_samples
