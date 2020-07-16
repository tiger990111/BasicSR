import importlib
from collections import Counter
from copy import deepcopy
from os import path as osp

import mmcv
import torch
from torch import distributed as dist

from basicsr.models.sr_model import SRModel
from basicsr.utils import ProgressBar, get_root_logger, tensor2img

metric_module = importlib.import_module('basicsr.metrics')


class VideoBaseModel(SRModel):
    """Base video SR model.
    """

    def dist_validation(self, dataloader, current_iter, tb_logger, save_img):
        dataset = dataloader.dataset
        dataset_name = dataset.opt['name']
        with_metrics = self.opt['val']['metrics'] is not None
        # initialize self.metric_results
        # It is a dict: {
        #    'folder1': tensor (num_frame x len(metrics)),
        #    'folder2': tensor (num_frame x len(metrics))
        # }
        if with_metrics and not hasattr(self, 'metric_results'):
            self.metric_results = {}
            num_frame_each_folder = Counter(dataset.data_info['folder'])
            for folder, num_frame in num_frame_each_folder.items():
                self.metric_results[folder] = torch.zeros(
                    num_frame,
                    len(self.opt['val']['metrics']),
                    dtype=torch.float32,
                    device='cuda')

        world_size = dist.get_world_size()
        rank = dist.get_rank()
        for _, tensor in self.metric_results.items():
            tensor.zero_()
        # record all frames (border and center frames)
        if rank == 0:
            pbar = ProgressBar(len(dataset))
        for idx in range(rank, len(dataset), world_size):
            val_data = dataset[idx]
            val_data['lq'].unsqueeze_(0)
            val_data['gt'].unsqueeze_(0)
            folder = val_data['folder']
            frame_idx, max_idx = val_data['idx'].split('/')
            lq_path = val_data['lq_path']

            self.feed_data(val_data)
            self.test()
            visuals = self.get_current_visuals()
            rlt_img = tensor2img([visuals['rlt']])
            if 'gt' in visuals:
                gt_img = tensor2img([visuals['gt']])
                del self.gt

            # tentative for out of GPU memory
            del self.lq
            del self.output
            torch.cuda.empty_cache()

            if save_img:
                if self.opt['is_train']:
                    raise NotImplementedError(
                        'saving image is not supported during training.')
                else:
                    if 'vimeo' in dataset_name.lower():
                        split_rlt = lq_path.split('/')
                        img_name = (f'{split_rlt[-3]}_{split_rlt[-2]}_'
                                    f'{split_rlt[-1].split(".")[0]}')
                    else:
                        img_name = osp.splitext(osp.basename(lq_path))[0]

                    if self.opt['val']['suffix']:
                        save_img_path = osp.join(
                            self.opt['path']['visualization'], dataset_name,
                            folder,
                            f'{img_name}_{self.opt["val"]["suffix"]}.png')
                    else:
                        save_img_path = osp.join(
                            self.opt['path']['visualization'], dataset_name,
                            folder, f'{img_name}_{self.opt["name"]}.png')
                mmcv.imwrite(rlt_img, save_img_path)

            if with_metrics:
                # calculate metrics
                opt_metric = deepcopy(self.opt['val']['metrics'])
                for metric_idx, opt_ in enumerate(opt_metric.values()):
                    metric_type = opt_.pop('type')
                    rlt = getattr(metric_module, metric_type)(rlt_img, gt_img,
                                                              **opt_)
                    self.metric_results[folder][int(frame_idx),
                                                metric_idx] += rlt

            # progress bar
            if rank == 0:
                for _ in range(world_size):
                    pbar.update(f'Test {folder} - '
                                f'{int(frame_idx) + world_size}/{max_idx}')

        if with_metrics:
            # collect data among GPUs
            for _, tensor in self.metric_results.items():
                dist.reduce(tensor, 0)
            dist.barrier()

            if rank == 0:
                self._log_validation_metric_values(current_iter, dataset_name,
                                                   tb_logger)

    def nondist_validation(self, dataloader, current_iter, tb_logger,
                           save_img):
        raise NotImplementedError('nondist_validation is not implemented.')

    def _log_validation_metric_values(self, current_iter, dataset_name,
                                      tb_logger):
        # average all frames for each sub-folder
        # metric_results_avg is a dict:{
        #    'folder1': tensor (len(metrics)),
        #    'folder2': tensor (len(metrics))
        # }
        metric_results_avg = {
            folder: torch.mean(tensor, dim=0).cpu()
            for (folder, tensor) in self.metric_results.items()
        }
        # total_avg_results is a dict: {
        #    'metric1': float,
        #    'metric2': float
        # }
        total_avg_results = {
            metric: 0
            for metric in self.opt['val']['metrics'].keys()
        }
        for folder, tensor in metric_results_avg.items():
            for idx, metric in enumerate(total_avg_results.keys()):
                total_avg_results[metric] += metric_results_avg[folder][
                    idx].item()
        # average among folders
        for metric in total_avg_results.keys():
            total_avg_results[metric] /= len(metric_results_avg)

        log_str = f'Validation {dataset_name}\n'
        for metric_idx, (metric,
                         value) in enumerate(total_avg_results.items()):
            log_str += f'\t # {metric}: {value:.4f}'
            for folder, tensor in metric_results_avg.items():
                log_str += f'\t # {folder}: {tensor[metric_idx].item():.4f}'
            log_str += '\n'

        logger = get_root_logger()
        logger.info(log_str)
        if tb_logger:
            for metric_idx, (metric,
                             value) in enumerate(total_avg_results.items()):
                tb_logger.add_scalar(f'metrics/{metric}', value, current_iter)
                for folder, tensor in metric_results_avg.items():
                    tb_logger.add_scalar(f'metrics/{metric}/{folder}',
                                         tensor[metric_idx].item(),
                                         current_iter)