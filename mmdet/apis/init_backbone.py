# Copyright (c) OpenMMLab. All rights reserved.
import copy
import warnings
from pathlib import Path
from typing import Optional, Sequence, Union
import logging
import numpy as np
import torch
import torch.nn as nn
from mmcv.ops import RoIPool
from mmcv.transforms import Compose
from mmengine.config import Config
from mmengine.dataset import default_collate
from mmengine.model.utils import revert_sync_batchnorm
from mmengine.registry import init_default_scope
from mmengine.runner import load_checkpoint
from mmengine.logging import print_log

from mmdet.registry import DATASETS
from mmdet.utils import ConfigType
from mmdet.evaluation import get_classes
from mmdet.registry import MODELS
from mmdet.structures import DetDataSample, SampleList
from mmdet.utils import get_test_pipeline_cfg


def init_backbone_neck(
    config: Union[str, Path, Config],
    checkpoint: Optional[str] = None,
    palette: str = 'none',
    device: str = 'cuda:0',
    cfg_options: Optional[dict] = None,
) -> nn.Module:
    """Initialize a detector from config file.

    Args:
        config (str, :obj:`Path`, or :obj:`mmengine.Config`): Config file path,
            :obj:`Path`, or the config object.
        checkpoint (str, optional): Checkpoint path. If left as None, the model
            will not load any weights.
        palette (str): Color palette used for visualization. If palette
            is stored in checkpoint, use checkpoint's palette first, otherwise
            use externally passed palette. Currently, supports 'coco', 'voc',
            'citys' and 'random'. Defaults to none.
        device (str): The device where the anchors will be put on.
            Defaults to cuda:0.
        cfg_options (dict, optional): Options to override some settings in
            the used config.

    Returns:
        nn.Module: The constructed detector.
    """
    if isinstance(config, (str, Path)):
        config = Config.fromfile(config)
    elif not isinstance(config, Config):
        raise TypeError('config must be a filename or Config object, '
                        f'but got {type(config)}')
    if cfg_options is not None:
        config.merge_from_dict(cfg_options)
    elif 'init_cfg' in config.model.backbone:
        config.model.backbone.init_cfg = None

    scope = config.get('default_scope', 'mmdet')
    if scope is not None:
        init_default_scope(config.get('default_scope', 'mmdet'))

    model_backbone = MODELS.build(config.model.backbone)
    model_neck = MODELS.build(config.model.neck)
    model_neck = revert_sync_batchnorm(model_neck)
    model_backbone = revert_sync_batchnorm(model_backbone)
    if checkpoint is None:
        warnings.simplefilter('once')
        warnings.warn('checkpoint is None, use COCO classes by default.')
        model_neck.dataset_meta = {'classes': get_classes('coco')}
        model_backbone.dataset_meta = {'classes': get_classes('coco')}
    else:
        load_checkpoint(model_backbone, checkpoint, map_location='cpu',revise_keys=[(r'^backbone\.', '')])
        checkpoint = load_checkpoint(model_neck, checkpoint, map_location='cpu',revise_keys=[(r'^neck\.', '')])
        # Weights converted from elsewhere may not have meta fields.
        checkpoint_meta = checkpoint.get('meta', {})

        # # save the dataset_meta in the model for convenience
        # if 'dataset_meta' in checkpoint_meta:
        #     # mmdet 3.x, all keys should be lowercase
        #     model_backbone.dataset_meta = {
        #         k.lower(): v
        #         for k, v in checkpoint_meta['dataset_meta'].items()
        #     }
        #     model_neck.dataset_meta = {
        #         k.lower(): v
        #         for k, v in checkpoint_meta['dataset_meta'].items()
        #     }
        # elif 'CLASSES' in checkpoint_meta:
        #     # < mmdet 3.x
        #     classes = checkpoint_meta['CLASSES']
        #     model_backbone.dataset_meta = {'classes': classes}
        #     model_neck.dataset_meta = {'classes': classes}
        # else:
        #     warnings.simplefilter('once')
        #     warnings.warn(
        #         'dataset_meta or class names are not saved in the '
        #         'checkpoint\'s meta data, use COCO classes by default.')
        #     model_backbone.dataset_meta = {'classes': get_classes('coco')}
        #     model_neck.dataset_meta = {'classes': get_classes('coco')}

    # Priority:  args.palette -> config -> checkpoint
    # if palette != 'none':
    #     model_backbone.dataset_meta['palette'] = palette
    #     model_neck.dataset_meta['palette'] = palette
    # else:
    #     test_dataset_cfg = copy.deepcopy(config.test_dataloader.dataset)
    #     # lazy init. We only need the metainfo.
    #     test_dataset_cfg['lazy_init'] = True
    #     metainfo = DATASETS.build(test_dataset_cfg).metainfo
    #     cfg_palette = metainfo.get('palette', None)
    #     if cfg_palette is not None:
    #         model_backbone.dataset_meta['palette'] = cfg_palette
    #         model_neck.dataset_meta['palette'] = cfg_palette
    #     else:
    #         if 'palette' not in model_backbone.dataset_meta:
    #             warnings.warn(
    #                 'palette does not exist, random is used by default. '
    #                 'You can also set the palette to customize.')
    #             model_backbone.dataset_meta['palette'] = 'random'
 # save the config in the model for convenience
    model_backbone.to(device)
    model_backbone.eval()

    model_neck.to(device)
    model_neck.eval()


    print_log('Successfully load the dicts of the backbone and neck of the two-stream network!!!',
                    level=logging.INFO)
    return model_backbone , model_neck