# Copyright (c) OpenMMLab. All rights reserved.
import argparse
import os
import os.path as osp
import cv2
from PIL import Image
import imgviz
import shutil
import time
import warnings
import numpy as np

import mmcv
import torch
from mmcv.parallel import MMDataParallel, MMDistributedDataParallel
from mmcv.runner import (get_dist_info, init_dist, load_checkpoint,
                         wrap_fp16_model)
from mmcv.utils import DictAction

from mmseg.apis import multi_gpu_test, single_gpu_test
from mmseg.datasets import build_dataloader, build_dataset
from mmseg.models import build_model, build_segmentor


def parse_args():
    parser = argparse.ArgumentParser(
        description='mmseg test (and eval) a model')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument(
        '--work-dir',
        help=('if specified, the evaluation metric results will be dumped'
              'into the directory as json'))
    parser.add_argument(
        '--aug-test', action='store_true', help='Use Flip and Multi scale aug')
    parser.add_argument('--out', help='output result file in pickle format')
    # NEW
    parser.add_argument('--output_dir', default='ddr', help='output dir')
    parser.add_argument('--source', default='refuge', help='gan source')
    parser.add_argument(
        '--vis', action='store_true', help='output the segmentation results')
    parser.add_argument(
        '--OD', action='store_true', help='evaluate the IDRiD OD performance')
    # NEW
    parser.add_argument(
        '--format-only',
        action='store_true',
        help='Format the output results without perform evaluation. It is'
             'useful when you want to format the result to a specific format and '
             'submit it to the test server')
    parser.add_argument(
        '--eval',
        type=str,
        nargs='+',
        help='evaluation metrics, which depends on the dataset, e.g., "mIoU"'
             ' for generic datasets, and "cityscapes" for Cityscapes')
    parser.add_argument('--show', action='store_true', help='show results')
    parser.add_argument(
        '--show-dir', help='directory where painted images will be saved')
    parser.add_argument(
        '--gpu-collect',
        action='store_true',
        help='whether to use gpu to collect results.')
    parser.add_argument(
        '--tmpdir',
        help='tmp directory used for collecting results from multiple '
             'workers, available when gpu_collect is not specified')
    parser.add_argument(
        '--options', nargs='+', action=DictAction, help='custom options')
    parser.add_argument(
        '--eval-options',
        nargs='+',
        action=DictAction,
        help='custom options for evaluation')
    parser.add_argument(
        '--launcher',
        choices=['none', 'pytorch', 'slurm', 'mpi'],
        default='none',
        help='job launcher')
    parser.add_argument(
        '--opacity',
        type=float,
        default=0.5,
        help='Opacity of painted segmentation map. In (0, 1] range.')
    parser.add_argument('--local_rank', type=int, default=0)
    args = parser.parse_args()
    if 'LOCAL_RANK' not in os.environ:
        os.environ['LOCAL_RANK'] = str(args.local_rank)
    return args


def main():
    args = parse_args()

    cfg = mmcv.Config.fromfile(args.config)
    if args.options is not None:
        cfg.merge_from_dict(args.options)
    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = True
    if args.aug_test:
        # hard code index
        cfg.data.test.pipeline[1].img_ratios = [
            0.5, 0.75, 1.0, 1.25, 1.5, 1.75
        ]
        cfg.data.test.pipeline[1].flip = True
    # cfg.data.test.test_mode = True

    # init distributed env first, since logger depends on the dist info.
    if args.launcher == 'none':
        distributed = False
    else:
        distributed = True
        init_dist(args.launcher, **cfg.dist_params)

    rank, _ = get_dist_info()
    # allows not to create
    if args.work_dir is not None and rank == 0:
        mmcv.mkdir_or_exist(osp.abspath(args.work_dir))
        timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
        json_file = osp.join(args.work_dir, f'eval_{timestamp}.json')

    # build the dataloader
    # TODO: support multiple images per gpu (only minor changes are needed)
    dataset = build_dataset(cfg.data.test)
    data_loader = build_dataloader(
        dataset,
        samples_per_gpu=1,
        workers_per_gpu=cfg.data.workers_per_gpu,
        dist=distributed,
        shuffle=False)

    # build the model and load checkpoint
    cfg.model.train_cfg = None
    model = build_model(cfg.model, test_cfg=cfg.get('test_cfg'))
    fp16_cfg = cfg.get('fp16', None)
    if fp16_cfg is not None:
        wrap_fp16_model(model)
    checkpoint = load_checkpoint(model, args.checkpoint, map_location='cpu')

    # clean gpu memory when starting a new evaluation.
    torch.cuda.empty_cache()
    eval_kwargs = {} if args.eval_options is None else args.eval_options

    # Deprecated
    efficient_test = eval_kwargs.get('efficient_test', False)
    if efficient_test:
        warnings.warn(
            '``efficient_test=True`` does not have effect in tools/test.py, '
            'the evaluation and format results are CPU memory efficient by '
            'default')

    eval_on_format_results = (
            args.eval is not None and 'cityscapes' in args.eval)
    if eval_on_format_results:
        assert len(args.eval) == 1, 'eval on format results is not ' \
                                    'applicable for metrics other than ' \
                                    'cityscapes'
    if args.format_only or eval_on_format_results:
        if 'imgfile_prefix' in eval_kwargs:
            tmpdir = eval_kwargs['imgfile_prefix']
        else:
            tmpdir = '.format_cityscapes'
            eval_kwargs.setdefault('imgfile_prefix', tmpdir)
        mmcv.mkdir_or_exist(tmpdir)
    else:
        tmpdir = None

    if args.vis:
        flag = True
    else:
        flag = False

    if not distributed:
        model = MMDataParallel(model, device_ids=[0])
        results = single_gpu_test(
            model,
            data_loader,
            args.show,
            args.show_dir,
            False,
            args.opacity,
            # pre_eval=args.eval is not None and not eval_on_format_results,
            format_only=args.format_only or eval_on_format_results,
            format_args=eval_kwargs)
    else:
        model = MMDistributedDataParallel(
            model.cuda(),
            device_ids=[torch.cuda.current_device()],
            broadcast_buffers=False)
        results = multi_gpu_test(
            model,
            data_loader,
            args.tmpdir,
            args.gpu_collect,
            False,
            # pre_eval=args.eval is not None and not eval_on_format_results,
            format_only=args.format_only or eval_on_format_results,
            format_args=eval_kwargs,
            test_mode=flag)

    rank, _ = get_dist_info()
    if rank == 0:
        if args.vis:
            os.makedirs(args.output_dir, exist_ok=True)
            if args.source == 'refuge':
                for res in results:
                    pred, filename = res
                    if isinstance(pred, list):
                        for p in pred:
                            if p.shape[0] == 2:
                                pred = p
                    # pred[pred == 1] = 128
                    # pred[pred == 2] = 255
                    tmp = np.zeros(pred.shape[1:])
                    tmp[pred[0] == 1] = 1
                    tmp[pred[1] == 1] = 2
                    label_pil = Image.fromarray(tmp.astype(np.uint8), mode="P")
                    colormap = imgviz.label_colormap()
                    label_pil.putpalette(colormap)
                    label_pil.save(os.path.join(args.output_dir, filename + '.png'))
            elif args.source == 'ddr':
                for res in results:
                    pred, filename = res
                    if isinstance(pred, list):
                        for p in pred:
                            if p.shape[0] == 4:
                                pred = p
                    # label = pred
                    if pred.shape[0] == 5 or pred.shape[0] == 7:
                        label = np.argmax(pred, axis=0)
                    else:
                        label = np.argmax(pred, axis=0) + 1
                        pred = pred > 0.5
                        label[(pred == 0).all(axis=0)] = 0
                    # cv2.imwrite(os.path.join(args.output_dir, filename + '.png'), label)
                    label_pil = Image.fromarray(label.astype(np.uint8), mode="P")
                    colormap = imgviz.label_colormap()
                    label_pil.putpalette(colormap)
                    label_pil.save(os.path.join(args.output_dir, filename + '.png'))
            else:
                for res in results:
                    pred, filename = res
                    if isinstance(pred, list):
                        for p in pred:
                            if p.shape[0] == 1:
                                pred = p
                    pred = (pred.squeeze(0) > 0.5).astype(np.uint8)
                    label_pil = Image.fromarray(pred.astype(np.uint8), mode="P")
                    colormap = imgviz.label_colormap()
                    label_pil.putpalette(colormap)
                    label_pil.save(os.path.join(args.output_dir, filename + '.png'))
        elif args.eval:
            eval_kwargs.update(metric=args.eval)
            if not args.OD:
                metric = dataset.evaluate(results, **eval_kwargs)
                metric_dict = dict(config=args.config, metric=metric)
                if args.work_dir is not None and rank == 0:
                    mmcv.dump(metric_dict, json_file, indent=4)
                if tmpdir is not None and eval_on_format_results:
                    # remove tmp dir when cityscapes evaluation
                    shutil.rmtree(tmpdir)
            else:
                label_dir = 'data/FOVCrop-padding/IDRiD-FOVCrop-padding/test/OD'
                gt_seg_maps = []
                for img_info in dataset.img_infos:
                    img = np.array(Image.open(os.path.join(label_dir, img_info['filename'].replace('.jpg', '.png'))))
                    gt_seg_maps.append(img)
                num_classes = 2
                pred = []
                for res in results:
                    img = res[2]
                    # img[img > 0] = 1
                    img = img[0]
                    pred.append(img)
                iou, dice = total_iou(pred, gt_seg_maps, num_classes)
                print(iou, dice)


def eval_iou(pred_label, label, num_classes):
    tp = pred_label[pred_label == label]

    area_p, _ = np.histogram(pred_label, bins=np.arange(num_classes + 1))
    area_tp, _ = np.histogram(tp, bins=np.arange(num_classes + 1))
    area_gt, _ = np.histogram(label, bins=np.arange(num_classes + 1))
    area_union = area_p + area_gt - area_tp
    return area_tp, area_union, area_p, area_gt


def total_iou(results, gt_seg_maps, num_classes):
    total_pred = np.zeros((num_classes,), dtype=np.float)
    total_tp = np.zeros((num_classes,), dtype=np.float)
    total_label = np.zeros((num_classes,), dtype=np.float)
    total_union = np.zeros((num_classes,), dtype=np.float)

    for result, gt_seg_map in zip(results, gt_seg_maps):
        tp, union, pred, label = eval_iou(result, gt_seg_map, num_classes)
        total_tp += tp
        total_union += union
        total_pred += pred
        total_label += label

    iou = total_tp / total_union
    dice = 2 * total_tp / (total_pred + total_label)
    return iou, dice


if __name__ == '__main__':
    main()
