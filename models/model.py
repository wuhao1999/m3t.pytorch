# coding=utf-8
# Copyright 2020 Yuan-Hang Zhang.
#
from .dataset import AffWild2iBugSequenceDataset
from .backbone import *
from .utils import concordance_cc2, mse

from argparse import ArgumentParser

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

import pytorch_lightning as pl


class AffWild2VA(pl.LightningModule):
    
    def __init__(self, hparams):
        super(AffWild2VA, self).__init__()
        self.hparams = hparams
        if self.hparams.backbone == 'resnet':
            self.visual = VA_3DResNet(
                hiddenDim=self.hparams.num_hidden,
                frameLen=self.hparams.window,
                backend=self.hparams.backend,
                resnet_ver='v1'
            )
        elif self.hparams.backbone == 'v2p':
            self.visual = VA_3DVGGM(
                hiddenDim=self.hparams.num_hidden,
                frameLen=self.hparams.window,
                backend=self.hparams.backend,
                nClasses=9 if self.hparams.valence_loss == 'softmax' else 2
            )
        elif self.hparams.backbone == 'v2p_split':
            self.visual = VA_3DVGGM_Split(
                hiddenDim=self.hparams.num_hidden,
                frameLen=self.hparams.window,
                backend=self.hparams.backend,
                split_layer=self.hparams.split_layer
            )
        elif self.hparams.backbone == 'densenet':
            self.visual = VA_3DDenseNet(
                hiddenDim=self.hparams.num_hidden,
                frameLen=self.hparams.window,
                backend=self.hparams.backend
            )
        elif self.hparams.backbone == 'vggface':
            self.visual = VA_VGGFace(
                hiddenDim=self.hparams.num_hidden,
                frameLen=self.hparams.window,
                backend=self.hparams.backend
            )

    def forward(self, x):
        # normalize to [-1, 1]
        x = (x - 127.5) / 127.5
        return self.visual(x)
    
    def ccc_loss(self, y_hat, y):
        return 1 - concordance_cc2(y_hat.view(-1), y.view(-1), 'none').squeeze()
    
    def bce_loss(self, y_hat, y):
        return F.binary_cross_entropy_with_logits(y_hat.view(-1), (y.view(-1) > 0).float())
    
    def ce_loss(self, y_hat, y):
        return F.cross_entropy(y_hat.view(-1), y(-1))
    
    def mse_loss(self, y_hat, y):
        return F.mse_loss(y_hat, y)
    
    def training_step(self, batch, batch_idx):
        x = batch['video']
        arousal = batch['label_arousal']
        
        y_hat = self.forward(x)
        if self.params.valence_loss == 'softmax':
            valence_hat, arousal_hat = y_hat[..., :8], y_hat[..., -1]
            valence = batch['class_valence']
            loss_v = self.ce_loss(valence_hat, valence)
        elif self.params.valence_loss == 'ccc':
            valence_hat, arousal_hat = y_hat[..., 0], y_hat[..., 1]
            valence = batch['label_valence']
            loss_v = self.ccc_loss(valence_hat, valence)
        loss_a = self.ccc_loss(arousal_hat, arousal)
        loss = 0.5 * loss_v + 0.5 * loss_a
        return {
            'loss': loss,
            'progress_bar': {'loss_v': loss_v, 'loss_a': loss_a, 'loss': loss},
            'log': {'loss_v': loss_v, 'loss_a': loss_a, 'loss': loss}
        }

    def validation_step(self, batch, batch_idx):
        v, a, v_hat, a_hat = [], [], [], []
        
        x = batch['video']
        y_hat = self.forward(x).cpu()
        valence_hat, arousal_hat = y_hat[..., 0], y_hat[..., 1]
        lens = batch['length']

        bs = lens.size(0)
        v_hat.extend([valence_hat[i][: lens[i]] for i in range(bs)])
        a_hat.extend([arousal_hat[i][: lens[i]] for i in range(bs)])
        
        valence, arousal = batch['label_valence'].cpu(), batch['label_arousal'].cpu()
        v.extend([valence[i][: lens[i]] for i in range(bs)])
        a.extend([arousal[i][: lens[i]] for i in range(bs)])

        return {
            'v_gt': v, 'a_gt': a,
            'v_pred': v_hat, 'a_pred': a_hat,
            'vid_names': batch['vid_name'],
            'start_frames': batch['start'].cpu()
        }

    def validation_end(self, outputs):
        all_v_gt = torch.cat([torch.cat(x['v_gt']) for x in outputs])
        all_a_gt = torch.cat([torch.cat(x['a_gt']) for x in outputs])
        all_v_pred = torch.cat([torch.cat(x['v_pred']) for x in outputs])
        all_a_pred = torch.cat([torch.cat(x['a_pred']) for x in outputs])

        is_valid = (torch.abs(all_v_gt) <= 1) & (torch.abs(all_a_gt) <= 1)
        all_ccc_v = concordance_cc2(all_v_gt[is_valid], all_v_pred[is_valid])
        all_ccc_a = concordance_cc2(all_a_gt[is_valid], all_a_pred[is_valid])
        all_mse_v = mse(all_v_pred[is_valid], all_v_gt[is_valid])
        all_mse_a = mse(all_a_pred[is_valid], all_a_gt[is_valid])

        val_loss = 1 - 0.5 * (all_ccc_v + all_ccc_a)

        # save outputs for visualisation
        predictions = {}
        for x in outputs:
            # gather batch elements by file name
            for vid_name, st_frame, v_gt, a_gt, v_pred, a_pred in zip(x['vid_names'], x['start_frames'], x['v_gt'], x['a_gt'], x['v_pred'], x['a_pred']):
                if vid_name in predictions.keys():
                    predictions[vid_name].append((st_frame, v_gt, a_gt, v_pred, a_pred))
                else:
                    predictions[vid_name] = [(st_frame, v_gt, a_gt, v_pred, a_pred)]
        pred_v, pred_a, gt_v, gt_a = {}, {}, {}, {}
        for k, w in predictions.items():
            # sort segment predictions by start frame index
            sorted_preds = sorted(w)
            gt_v[k] = torch.cat([x[1] for x in sorted_preds])
            gt_a[k] = torch.cat([x[2] for x in sorted_preds])
            pred_v[k] = torch.cat([x[3] for x in sorted_preds])
            pred_a[k] = torch.cat([x[4] for x in sorted_preds])
        torch.save({
            'valence_gt': gt_v,
            'arousal_gt': gt_a,
            'valence_pred': pred_v,
            'arousal_pred': pred_a
        }, 'predictions_val.pt')

        return {
            'val_loss': val_loss,
            'progress_bar': {
                'val_ccc_v': all_ccc_v,
                'val_ccc_a': all_ccc_a
            },
            'log': {
                'val_ccc_v': all_ccc_v,
                'val_ccc_a': all_ccc_a,
                'val_mse_v': all_mse_v,
                'val_mse_a': all_mse_a,
                'val_loss': val_loss
            }
        }
    
    def test_step(self, batch, batch_idx):
        v_hat, a_hat = [], []
        
        x = batch['video']
        y_hat = self.forward(x).cpu()
        valence_hat, arousal_hat = y_hat[..., 0], y_hat[..., 1]
        lens = batch['length']

        bs = lens.size(0)
        v_hat.extend([valence_hat[i][: lens[i]] for i in range(bs)])
        a_hat.extend([arousal_hat[i][: lens[i]] for i in range(bs)])
        
        return {
            'v_pred': v_hat, 'a_pred': a_hat,
            'vid_names': batch['vid_name'],
            'start_frames': batch['start'].cpu()
        }

    def test_end(self, outputs):
        predictions = {}
        for x in outputs:
            # gather batch elements by file name
            for vid_name, st_frame, v, a in zip(x['vid_names'], x['start_frames'], x['v_pred'], x['a_pred']):
                if vid_name in predictions.keys():
                    predictions[vid_name].append((st_frame, v, a))
                else:
                    predictions[vid_name] = [(st_frame, v, a)]
        pred_v, pred_a = {}, {}
        for k, w in predictions.items():
            # sort segment predictions by start frame index
            sorted_preds = sorted(w)
            pred_v[k] = torch.cat([x[1] for x in sorted_preds])
            pred_a[k] = torch.cat([x[2] for x in sorted_preds])
        # save predictions for further ensembling
        torch.save({
            'valence_pred': pred_v,
            'arousal_pred': pred_a
        }, 'predictions_test.pt')
        
        return {}

    def configure_optimizers(self):
        if self.hparams.optimizer == 'adam':
            optimizer = torch.optim.Adam(self.parameters(),
                                         lr=self.hparams.learning_rate,
                                         weight_decay=1e-4)
            return optimizer
#             scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, 0.5)
#             return [optimizer], [scheduler]
        elif self.hparams.optimizer == 'sgd':
            optimizer = torch.optim.SGD(self.parameters(),
                                        lr=self.hparams.learning_rate,
                                        momentum=0.9, weight_decay=5e-4)
            scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[30,80], gamma=0.5)
            return [optimizer], [scheduler]
    

    @pl.data_loader
    def train_dataloader(self):
        if self.hparams.mode == 'video':
            dataset = AffWild2iBugSequenceDataset('train', self.hparams.dataset_path, self.hparams.window, self.hparams.windows_per_epoch, self.hparams.cutout)
        else:
            # TODO: implement framewise
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.workers, pin_memory=True)

    @pl.data_loader
    def val_dataloader(self):
        if self.hparams.mode == 'video':
            dataset = AffWild2iBugSequenceDataset('val', self.hparams.dataset_path, self.hparams.window)
        else:
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.workers, pin_memory=True)

    @pl.data_loader
    def test_dataloader(self):
        if self.hparams.mode == 'video':
            dataset = AffWild2iBugSequenceDataset('test', self.hparams.dataset_path, self.hparams.window)
        else:
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.workers, pin_memory=True)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument('--backbone', default='resnet', type=str)
        parser.add_argument('--backend', default='gru', type=str)

        parser.add_argument('--mode', default='video', type=str)
        parser.add_argument('--window', default=16, type=int)
        parser.add_argument('--windows_per_epoch', default=200, type=int)
        parser.add_argument('--learning_rate', default=0.0003, type=float)
        parser.add_argument('--batch_size', default=96, type=int)
        parser.add_argument('--optimizer', default='adam', type=str)

        parser.add_argument('--valence_loss', default='ccc', type=str)
        parser.add_argument('--num_hidden', default=512, type=int)
        parser.add_argument('--split_layer', default=5, type=int)
        parser.add_argument('--cutout', action='store_true', default=False)

        # training specific (for this model)
        parser.add_argument('--distributed', action='store_true', default=False)
        parser.add_argument('--dataset_path', default='/.data/zhangyuanhang/Aff-Wild2', type=str)
        parser.add_argument('--checkpoint_path', default='.', type=str)
        parser.add_argument('--workers', default=8, type=int)
        parser.add_argument('--max_nb_epochs', default=30, type=int)

        return parser
