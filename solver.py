# -*- coding: utf-8 -*-
"""
Created on Tue Apr 14 21:20:37 2020

Created on 2018/12
Author: Kaituo XU

2020/09 Edited by: sanghyu  


"""

import os
import time

import torch
import numpy as np

from pit_criterion import cal_loss

class TransformerOptimizer(object):
    """A simple wrapper class for learning rate scheduling"""

    def __init__(self, optimizer, k, d_model, warmup_steps=4000):
        self.optimizer = optimizer
        self.k = k
        self.init_lr = d_model ** (-0.5)
        self.warmup_steps = warmup_steps
        self.step_num = 0
        self.epoch = 0
        self.visdom_lr = None

    def zero_grad(self):
        self.optimizer.zero_grad()

    def step(self, epoch):
        self._update_lr(epoch)
        # self._visdom()
        self.optimizer.step()

    def _update_lr(self, epoch):
        self.step_num += 1
        if self.step_num <= self.warmup_steps:
            lr = self.k * self.init_lr * min(self.step_num ** (-0.5),
                                             self.step_num * (self.warmup_steps ** (-1.5)))
        else:
            lr = 0.0004 * (0.98 ** ((epoch-1)//2))

        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr

    def load_state_dict(self, state_dict):
        self.optimizer.load_state_dict(state_dict)

    def state_dict(self):
        return self.optimizer.state_dict()

    def set_k(self, k):
        self.k = k

    def set_visdom(self, visdom_lr, vis):
        self.visdom_lr = visdom_lr  # Turn on/off visdom of learning rate
        self.vis = vis  # visdom enviroment
        self.vis_opts = dict(title='Learning Rate',
                             ylabel='Leanring Rate', xlabel='step')
        self.vis_window = None
        self.x_axis = torch.LongTensor()
        self.y_axis = torch.FloatTensor()

    def _visdom(self):
        if self.visdom_lr is not None:
            self.x_axis = torch.cat(
                [self.x_axis, torch.LongTensor([self.step_num])])
            self.y_axis = torch.cat(
                [self.y_axis, torch.FloatTensor([self.optimizer.param_groups[0]['lr']])])
            if self.vis_window is None:
                self.vis_window = self.vis.line(X=self.x_axis, Y=self.y_axis,
                                                opts=self.vis_opts)
            else:
                self.vis.line(X=self.x_axis, Y=self.y_axis, win=self.vis_window,
                              update='replace')
                
class Solver(object):
    
    def __init__(self, data, model, optimizer, args):
        self.tr_loader = data['tr_loader']
        self.cv_loader = data['cv_loader']
        self.model = model
        self.optimizer =  TransformerOptimizer(optimizer, 0.2, args.N)

        # Training config
        self.use_cuda = args.use_cuda
        self.epochs = args.epochs
        self.half_lr = args.half_lr
        self.early_stop = args.early_stop
        self.max_norm = args.max_norm
        # save and load model
        self.save_folder = args.save_folder
        self.checkpoint = args.checkpoint
        self.continue_from = args.continue_from
        # logging
        self.print_freq = args.print_freq
        # loss
        self.tr_loss = torch.Tensor(self.epochs)
        self.cv_loss = torch.Tensor(self.epochs)

        self._reset()

    def _reset(self):
        # Reset
        if self.continue_from:
            print('Loading checkpoint model %s' % self.continue_from)
            cont = torch.load(self.continue_from)
            self.start_epoch = cont['epoch']
            self.model.load_state_dict(cont['model_state_dict'])
            self.optimizer.load_state_dict(cont['optimizer_state'])
            torch.set_rng_state(cont['trandom_state'])
            np.random.set_state(cont['nrandom_state'])
        else:
            self.start_epoch = 0
        # Create save folder
        os.makedirs(self.save_folder, exist_ok=True)
        self.best_val_loss = float("inf")
        self.halving = False
        self.val_no_impv = 0

    def train(self):
        # Train model multi-epoches
        for epoch in range(self.start_epoch, self.epochs):
            print("Training...")
            self.model.train()
            start = time.time()
            # Train one epoch
            tr_avg_loss = self._run_one_epoch(epoch)
            print('-' * 85)
            print('Train Summary | End of Epoch {0:5d} | Time {1:.2f}s | '
                  'Train Loss {2:.3f}'.format(
                      epoch + 1, time.time() - start, tr_avg_loss))
            print('-' * 85)

            # Save model each epoch
            if self.checkpoint:
                file_path = os.path.join(
                    self.save_folder, 'epoch%d.pth' % (epoch + 1))
                torch.save(self.model.state_dict(), file_path)
                print('Saving checkpoint model to %s' % file_path)

            
            # Cross validation
            print('Cross validation...')
            self.model.eval()
            with torch.no_grad():
                val_loss = self._run_one_epoch(epoch, cross_valid=True)
            print('-' * 85)
            print('Valid Summary | End of Epoch {0} | Time {1:.2f}s | '
                  'Valid Loss {2:.3f}'.format(
                      epoch + 1, time.time() - start, val_loss))
            print('-' * 85)

            # Adjust learning rate (halving)
            if self.half_lr:
                if val_loss >= self.best_val_loss:
                    self.val_no_impv += 1
                    if self.val_no_impv >= 3:
                        self.halving = True
                    if self.val_no_impv >= 10 and self.early_stop:
                        print("No imporvement for 10 epochs, early stopping.")
                        break
                else:
                    self.val_no_impv = 0
            if self.halving:
                optim_state = self.optimizer.state_dict()
                optim_state['param_groups'][0]['lr'] = \
                    optim_state['param_groups'][0]['lr'] / 2.0
                self.optimizer.load_state_dict(optim_state)
                print('Learning rate adjusted to: {lr:.6f}'.format(
                    lr=optim_state['param_groups'][0]['lr']))
                self.halving = False

            # Save the best model
            self.tr_loss[epoch] = tr_avg_loss
            self.cv_loss[epoch] = val_loss
            if val_loss < self.best_val_loss:
                self.best_val_loss = val_loss
                best_file_path = os.path.join(
                    self.save_folder, 'temp_best.pth')
                torch.save(self.model.state_dict(), best_file_path)
                print("Find better validated model, saving to %s" % best_file_path)


    def _run_one_epoch(self, epoch, cross_valid=False):
        start = time.time()
        total_loss = 0

        data_loader = self.tr_loader if not cross_valid else self.cv_loader

        for i, data in enumerate(data_loader):
            mix_batch, src_batch, pad_mask = data
            
            if self.use_cuda:
                mix_batch = mix_batch.cuda()
                src_batch = src_batch.cuda()
                pad_mask = pad_mask.cuda()
            estimate_source = self.model(mix_batch)
            loss, max_snr, estimate_source, reorder_estimate_source = \
                cal_loss(src_batch, estimate_source, pad_mask)
            if not cross_valid:
                self.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(),
                                               self.max_norm)
                self.optimizer.step(epoch)

            total_loss += loss.item()

            if i % self.print_freq == 0:
                print('Epoch {0:3d} | Iter {1:5d} | Average Loss {2:3.3f} | '
                      'Current Loss {3:3.6f} | {4:5.1f} ms/batch'.format(
                          epoch + 1, i + 1, total_loss / (i + 1),
                          loss.item(), 1000 * (time.time() - start) / (i + 1)),
                      flush=True)

        return total_loss / (i + 1)
