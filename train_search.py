import argparse
import glob
import logging
import os
import sys
import time

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.utils
from scipy.stats import kendalltau
from torch.nn import functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader, SubsetRandomSampler
from torchvision.datasets import CIFAR10

import utils
from module.architect import Architect
from module.estimator.estimator import Estimator, PredictorForGraph
from module.estimator.gnn.decoder import LinearDecoder
from module.estimator.gnn.encoder import GINEncoder
from module.estimator.gnn.gae import GAEExtractor
from module.estimator.gnn.loss import ReconstructedLoss
from module.estimator.memory import Memory
from module.estimator.population import Population
from module.estimator.predictor import Predictor, weighted_loss
from module.estimator.utils import GraphPreprocessor
from utils import gumbel_like, gpu_usage

from module.resnet import resnet18, resnet34, resnet50, resnet110
from module.loss import LossFunc

CIFAR_CLASSES = 10


def main():
    if not torch.cuda.is_available():
        logging.info('no gpu device available')
        sys.exit(1)

    # enable GPU and set random seeds
    np.random.seed(args.seed)  # set random seed: numpy
    torch.cuda.set_device(args.gpu)

    # fast search
    cudnn.deterministic = False
    cudnn.benchmark = True

    torch.manual_seed(args.seed)  # set random seed: torch
    cudnn.enabled = True
    torch.cuda.manual_seed(args.seed)  # set random seed: torch.cuda
    logging.info('gpu device = %d' % args.gpu)
    logging.info("args = %s", args)
    if len(unknown_args) > 0:
        logging.warning('unknown_args: %s' % unknown_args)
    else:
        logging.info('unknown_args: %s' % unknown_args)
    # Loss Function Search


    # build the model with model_search.Network
    logging.info("init arch param")
    model = resnet50(num_classes=CIFAR_CLASSES)
    model = model.to('cuda')
    logging.info("model param size = %fMB", utils.count_parameters_in_MB(model))

    # use SGD to optimize the model (optimize model.parameters())
    optimizer = torch.optim.SGD(
        model.parameters(),
        args.learning_rate,
        momentum=args.momentum,
        weight_decay=args.weight_decay
    )

    # construct data transformer (including normalization, augmentation)
    train_transform, valid_transform = utils.data_transforms_cifar10(args)
    # load cifar10 data training set (train=True)
    train_data = CIFAR10(root=args.data, train=True, download=True, transform=train_transform)

    # generate data indices
    num_train = len(train_data)
    indices = list(range(num_train))
    split = int(np.floor(args.train_portion * num_train))

    # split training set and validation queue given indices
    # train queue:
    train_queue = DataLoader(
        train_data, batch_size=args.batch_size, sampler=SubsetRandomSampler(indices[:split]),
        pin_memory=True, num_workers=args.num_workers
    )

    # validation queue:
    valid_queue = DataLoader(
        train_data, batch_size=args.batch_size, sampler=SubsetRandomSampler(indices[split:num_train]),
        pin_memory=True, num_workers=args.num_workers
    )

    # learning rate scheduler (with cosine annealing)
    scheduler = CosineAnnealingLR(optimizer, int(args.epochs), eta_min=args.learning_rate_min)


    lfs = LossFunc(operator_size=args.operator_size,
                   model=model, momentum=args.momentum, weight_decay=args.weight_decay,
                   arch_learning_rate=args.arch_learning_rate, arch_weight_decay=args.arch_weight_decay,
                   predictor=predictor, pred_learning_rate=args.pred_learning_rate,
                   architecture_criterion=F.mse_loss, predictor_criterion=predictor_criterion,
                   is_gae=is_gae, reconstruct_criterion=reconstruct_criterion, preprocessor=preprocessor
                   )

    # construct architect with architect.Architect
    if args.predictor_type == 'lstm':
        is_gae = False
        # -- preprocessor --
        preprocessor = None
        # -- build model --
        predictor = Predictor(input_size=lfs.operation_size + lfs.operater_size,
                              hidden_size=args.predictor_hidden_state)
        predictor = predictor.to('cuda')
        reconstruct_criterion = None
    elif args.predictor_type == 'gae':
        is_gae = True
        # -- preprocessor --
        preprocessor = GraphPreprocessor(mode=args.preprocess_mode, lamb=args.preprocess_lamb)
        # -- build model --
        predictor = Estimator(
            extractor=GAEExtractor(
                encoder=GINEncoder(
                    input_dim=args.opt_num, hidden_dim=args.hidden_dim, latent_dim=args.latent_dim,
                    num_layers=args.num_layers, num_mlp_layers=args.num_mlp_layers
                ),
                decoder=LinearDecoder(
                    latent_dim=args.latent_dim, decode_dim=args.opt_num, dropout=args.dropout,
                    activation_adj=torch.sigmoid, activation_opt=torch.softmax
                )
            ),
            predictor=PredictorForGraph(in_features=args.latent_dim * 2, out_features=1)
        )
        predictor = predictor.to('cuda')
        reconstruct_criterion = ReconstructedLoss(
            loss_opt=torch.nn.BCELoss(), loss_adj=F.mse_loss, w_opt=1.0, w_adj=1.0
        )
    else:
        raise ValueError('unknown estimator type: %s' % args.predictor_type)
    logging.info("predictor param size = %fMB", utils.count_parameters_in_MB(predictor))

    if args.weighted_loss:
        logging.info('using weighted MSE loss for predictor')
        predictor_criterion = weighted_loss
    else:
        logging.info('using MSE loss for predictor')
        predictor_criterion = F.mse_loss

    architect = Architect(
        model=model, momentum=args.momentum, weight_decay=args.weight_decay,
        arch_learning_rate=args.arch_learning_rate, arch_weight_decay=args.arch_weight_decay,
        predictor=predictor, pred_learning_rate=args.pred_learning_rate,
        architecture_criterion=F.mse_loss, predictor_criterion=predictor_criterion,
        is_gae=is_gae, reconstruct_criterion=reconstruct_criterion, preprocessor=preprocessor
    )

    if args.evolution:
        memory = Population(batch_size=args.predictor_batch_size, tau=args.tau, is_gae=is_gae)
    else:
        memory = Memory(limit=args.memory_size, batch_size=args.predictor_batch_size, is_gae=is_gae)

    # --- Part 1: model warm-up and build memory---
    # 1.1 model warm-up
    if args.load_model is not None:
        # load from file
        logging.info('Load warm-up from %s', args.load_model)
        model.load_state_dict(torch.load(os.path.join(args.load_model, 'model-weights-warm-up.pt')))
        warm_up_gumbel = utils.pickle_load(os.path.join(args.load_model, 'gumbel-warm-up.pickle'))
    else:
        # 1.1.1 sample cells for warm-up
        warm_up_gumbel = []
        # assert args.warm_up_population >= args.predictor_batch_size
        for epoch in range(args.warm_up_population):
            g_operation = gumbel_like(lfs.alphas_operation)
            g_operator = gumbel_like(lfs.alphas_operator)
            warm_up_gumbel.append((g_operation, g_operator))
        utils.pickle_save(warm_up_gumbel, os.path.join(args.save, 'gumbel-warm-up.pickle'))
        # 1.1.2 warm up
        for epoch, gumbel in enumerate(warm_up_gumbel):
            logging.info('[warm-up model] epoch %d/%d', epoch + 1, args.warm_up_population)
            # warm-up
            lfs.g_operation, lfs.g_operator = gumbel
            objs, top1, top5 = model_train(train_queue, model, lfs, optimizer, name='warm-up model')
            logging.info('[warm-up model] epoch %d/%d overall loss=%.4f top1-acc=%.4f top5-acc=%.4f',
                         epoch + 1, args.warm_up_population, objs, top1, top5)
            # save weights
            utils.save(model, os.path.join(args.save, 'model-weights-warm-up.pt'))
            # gpu info
            gpu_usage()

    # 1.2 build memory (i.e. valid model)
    if args.load_memory is not None:
        logging.info('Load valid model from %s', args.load_model)
        model.load_state_dict(torch.load(os.path.join(args.load_memory, 'model-weights-valid.pt')))
        memory.load_state_dict(
            utils.pickle_load(
                os.path.join(args.load_memory, 'memory-warm-up.pickle')
            )
        )
    else:
        for epoch, gumbel in enumerate(warm_up_gumbel):
            # re-sample Gumbel distribution
            lfs.g_operation, lfs.g_operator = gumbel
            # train model for one step
            objs, top1, top5 = model_train(train_queue, model, lfs, optimizer, name='build memory')
            logging.info('[build memory] train model-%03d loss=%.4f top1-acc=%.4f',
                         epoch + 1, objs, top1)
            # valid model
            objs, top1, top5, ece, nll = model_valid(valid_queue, model, lfs, name='build memory')
            logging.info('[build memory] valid model-%03d loss=%.4f top1-acc=%.4f',
                         epoch + 1, objs, top1)
            # save to memory
            if args.evolution:
                memory.append(individual=[(model.alphas_operation.detach().clone(), lfs.g_operation.detach().clone()),
                                          (model.alphas_reduce.detach().clone(), lfs.g_operator.detach().clone())],
                              fitness=torch.tensor(objs, dtype=torch.float32).to('cuda'))
            else:
                memory.append(weights=[w.detach() for w in model.arch_weights(cat=False)],
                              nll=torch.tensor(nll, dtype=torch.float32).to('cuda'),
                              acc=torch.tensor(top1, dtype=torch.float32).to('cuda'),
                              ece=torch.tensor(ece, dtype=torch.float32).to('cuda'))
            # checkpoint: model, memory
            utils.save(model, os.path.join(args.save, 'model-weights-valid.pt'))
            utils.pickle_save(memory.state_dict(),
                              os.path.join(args.save, 'memory-warm-up.pickle'))

    logging.info('memory size=%d', len(memory))

    # --- Part 2 predictor warm-up ---
    if args.load_extractor is not None:
        logging.info('Load extractor from %s', args.load_extractor)
        architect.predictor.extractor.load_state_dict(torch.load(args.load_extractor)['weights'])

    predictor.train()
    for epoch in range(args.predictor_warm_up):
        epoch += 1
        # warm-up
        p_loss, p_true, p_pred = predictor_train(lfs, memory)
        if epoch % args.report_freq == 0 or epoch == args.predictor_warm_up:
            logging.info('[warm-up predictor] epoch %d/%d loss=%.4f', epoch, args.predictor_warm_up, p_loss)
            logging.info('\np-true: %s\np-pred: %s', p_true.data, p_pred.data)
            k_tau = kendalltau(p_true.detach().to('cpu'), p_pred.detach().to('cpu'))[0]
            logging.info('kendall\'s-tau=%.4f' % k_tau)
            # save predictor
            utils.save(architect.predictor, os.path.join(args.save, 'predictor-warm-up.pt'))
    # gpu info
    gpu_usage()
    # log genotype
    log_genotype(model)

    # --- Part 3 architecture search ---
    for epoch in range(args.epochs):
        # get current learning rate
        lr = scheduler.get_lr()[0]
        logging.info('[architecture search] epoch %d/%d lr %e', epoch + 1, args.epochs, lr)
        # search
        objs, top1, top5, objp = architecture_search(train_queue, valid_queue, model, architect,
                                                     lfs, optimizer, memory)
        # save weights
        utils.save(model, os.path.join(args.save, 'model-weights-search.pt'))
        # log genotype
        log_genotype(model)
        # update learning rate
        scheduler.step()
        # log
        logging.info('[architecture search] overall loss=%.4f top1-acc=%.4f top5-acc=%.4f predictor_loss=%.4f',
                     objs, top1, top5, objp)
        # gpu info
        gpu_usage()


def log_genotype(lfs):
    # log genotype (i.e. alpha)
    genotype = lfs.genotype()
    logging.info('genotype = %s', genotype)
    logging.info('alphas_normal: %s\n%s', torch.argmax(lfs.alphas_operation, dim=-1), lfs.alphas_operation)
    logging.info('alphas_reduce: %s\n%s', torch.argmax(lfs.alphas_operator, dim=-1), lfs.alphas_operator)


def model_train(train_queue, model, lfs, optimizer, name):
    # set model to training model
    model.train()
    # create metrics
    objs = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()
    # training loop
    total_steps = len(train_queue)
    for step, (x, target) in enumerate(train_queue):
        n = x.size(0)
        # data to CUDA
        x = x.to('cuda').requires_grad_(False)
        target = target.to('cuda', non_blocking=True).requires_grad_(False)
        # update model weight
        # forward
        optimizer.zero_grad()
        logits = model(x)
        loss = lfs(logits, target)
        # backward
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
        optimizer.step()
        prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
        # update metrics
        objs.update(loss.data.item(), n)
        top1.update(prec1.data.item(), n)
        top5.update(prec5.data.item(), n)
        if step % args.report_freq == 0:
            logging.info('[%s] train model %03d/%03d loss=%.4f top1-acc=%.4f top5-acc=%.4f',
                         name, step, total_steps, objs.avg, top1.avg, top5.avg)
    # return average metrics
    return objs.avg, top1.avg, top5.avg


def model_valid(valid_queue, model, lfs, name):
    # set model to evaluation model
    model.eval()
    # create metrics
    objs = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()
    # validation loop
    total_steps = len(valid_queue)
    for step, (x, target) in enumerate(valid_queue):
        n = x.size(0)
        # data to CUDA
        x = x.to('cuda').requires_grad_(False)
        target = target.to('cuda', non_blocking=True).requires_grad_(False)
        # valid model
        logits = model(x)
        loss = lfs(logits, target)
        prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
        # update metrics
        objs.update(loss.data.item(), n)
        top1.update(prec1.data.item(), n)
        top5.update(prec5.data.item(), n)
        # log
        if step % args.report_freq == 0:
            logging.info('[%s] valid model %03d/%03d loss=%.4f top1-acc=%.4f top5-acc=%.4f',
                         name, step, total_steps, objs.avg, top1.avg, top5.avg)
    return objs.avg, top1.avg, top5.avg


def predictor_train(architect, memory, unsupervised=False):
    # TODO: add support for gae predictor training
    objs = utils.AverageMeter()
    batch = memory.get_batch()
    all_y = []
    all_p = []
    for x, y in batch:
        n = y.size(0)
        pred, loss = architect.predictor_step(x, y, unsupervised=unsupervised)
        objs.update(loss.data.item(), n)
        all_y.append(y)
        all_p.append(pred)
    return objs.avg, torch.cat(all_y), torch.cat(all_p)


def architecture_search(train_queue, valid_queue, model, architect, lfs, optimizer, memory):
    # -- train model --
    gsw_normal, gsw_reduce = 1., 1.  # gumbel sampling weight
    lfs.g_operation = gumbel_like(model.alphas_operation) * gsw_normal
    lfs.g_operator = gumbel_like(model.alphas_reduce) * gsw_reduce
    # train model for one step
    model_train(train_queue, model, lfs, optimizer, name='build memory')
    # -- valid model --
    objs, top1, top5 = model_valid(valid_queue, model, lfs, name='build memory')
    # save validation to memory
    logging.info('[architecture search] append memory objs=%.4f top1-acc=%.4f top5-acc=%.4f', objs, top1, top5)
    if args.evolution:
        memory.append(individual=[(model.alphas_operation.detach().clone(), lfs.g_operation.detach().clone()),
                                  (model.alphas_reduce.detach().clone(), lfs.g_operator.detach().clone())],
                      fitness=torch.tensor(objs, dtype=torch.float32).to('cuda'))
        index = memory.remove('highest')
        logging.info('[evolution] %d is removed (population size: %d).' % (index, len(memory)))
    else:
        memory.append(weights=[w.detach() for w in model.arch_weights(cat=False)],
                      loss=torch.tensor(objs, dtype=torch.float32).to('cuda'))
    utils.pickle_save(memory.state_dict(),
                      os.path.join(args.save, 'memory-search.pickle'))

    # -- predictor train --
    architect.predictor.train()
    # use memory to train predictor
    p_loss, p_true, p_pred = None, None, None
    k_tau = -float('inf')
    for _ in range(args.predictor_warm_up):
        p_loss, p_true, p_pred = predictor_train(architect, memory)
        k_tau = kendalltau(p_true.detach().to('cpu'), p_pred.detach().to('cpu'))[0]
        if k_tau > 0.95: break
    logging.info('[architecture search] train predictor p_loss=%.4f\np-true: %s\np-pred: %s',
                 p_loss, p_true.data, p_pred.data)
    logging.info('kendall\'s-tau=%.4f' % k_tau)

    architect.step()
    # log
    logging.info('[architecture search] update architecture')

    return objs, top1, top5, p_loss


if __name__ == '__main__':
    parser = argparse.ArgumentParser("cifar")
    # data
    parser.add_argument('--data', type=str, default='data', help='location of the data corpus')
    parser.add_argument('--train_portion', type=float, default=0.5, help='portion of training data')
    parser.add_argument('--num_workers', type=int, default=4, help='number of data loader workers')
    parser.add_argument('--cutout', action='store_true', default=False, help='use cutout')
    parser.add_argument('--cutout_length', type=int, default=16, help='cutout length')
    # save
    parser.add_argument('--save', type=str, default='EXP', help='experiment name')
    parser.add_argument('--model_path', type=str, default='saved_models', help='path to save the model')
    # training setting
    parser.add_argument('--batch_size', type=int, default=64, help='batch size')
    parser.add_argument('--learning_rate', type=float, default=0.025, help='init learning rate')
    parser.add_argument('--learning_rate_min', type=float, default=0.001, help='min learning rate')
    parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
    parser.add_argument('--weight_decay', type=float, default=3e-4, help='weight decay')
    parser.add_argument('--report_freq', type=float, default=50, help='report frequency')
    parser.add_argument('--epochs', type=int, default=200, help='num of training epochs')
    parser.add_argument('--drop_path_prob', type=float, default=0.3, help='drop path probability')
    parser.add_argument('--grad_clip', type=float, default=5, help='gradient clipping')
    # search setting
    parser.add_argument('--arch_learning_rate', type=float, default=3e-4, help='learning rate for arch encoding')
    parser.add_argument('--arch_weight_decay', type=float, default=1e-3, help='weight decay for arch encoding')
    parser.add_argument('--memory_size', type=int, default=100, help='size of memory to train predictor')
    parser.add_argument('--warm_up_population', type=int, default=100, help='warm_up_population')
    parser.add_argument('--load_model', type=str, default=None, help='load model weights from file')
    parser.add_argument('--load_memory', type=str, default=None, help='load memory from file')
    parser.add_argument('--tau', type=float, default=0.1, help='tau')
    parser.add_argument('--evolution', action='store_true', default=False, help='use weighted loss')
    parser.add_argument('--diw', action='store_true', default=False, help='dimension importance aware')
    # predictor setting
    parser.add_argument('--predictor_type', type=str, default='lstm')
    parser.add_argument('--predictor_warm_up', type=int, default=500, help='predictor warm-up steps')
    parser.add_argument('--predictor_hidden_state', type=int, default=16, help='predictor hidden state')
    parser.add_argument('--predictor_batch_size', type=int, default=64, help='predictor batch size')
    parser.add_argument('--pred_learning_rate', type=float, default=1e-3, help='predictor learning rate')
    parser.add_argument('--weighted_loss', action='store_true', default=False, help='use weighted loss')
    parser.add_argument('--load_extractor', type=str, default=None, help='load memory from file')
    # model setting
    parser.add_argument('--init_channels', type=int, default=16, help='num of init channels')
    parser.add_argument('--layers', type=int, default=8, help='total number of layers')
    # others
    parser.add_argument('--gpu', type=int, default=0, help='gpu device id')
    parser.add_argument('--seed', type=int, default=1, help='random seed')
    parser.add_argument('--debug', action='store_true', default=False, help='set logging level to debug')
    # GAE related
    parser.add_argument('--opt_num', type=int, default=11)
    parser.add_argument('--hidden_dim', type=int, default=128)
    parser.add_argument('--latent_dim', type=int, default=16)
    parser.add_argument('--num_layers', type=int, default=5)
    parser.add_argument('--num_mlp_layers', type=int, default=2)
    parser.add_argument('--dropout', type=float, default=0.3)

    # data
    parser.add_argument('--preprocess_mode', type=int, default=4)
    parser.add_argument('--preprocess_lamb', type=float, default=0.)

    # loss function search
    parser.add_argument('--operator_size', type=int, default=8)

    args, unknown_args = parser.parse_known_args()

    args.save = 'checkpoints/search{}-{}-{}'.format(
        '-ea' if args.evolution else '', args.save, time.strftime("%Y%m%d-%H%M%S")
    )
    utils.create_exp_dir(
        path=args.save,
        scripts_to_save=glob.glob('*.py') + glob.glob('module/**/*.py', recursive=True)
    )

    log_format = '%(asctime)s %(levelname)s %(message)s'
    logging_level = logging.INFO if not args.debug else logging.DEBUG
    logging.basicConfig(stream=sys.stdout, level=logging_level,
                        format=log_format, datefmt='%m/%d %I:%M:%S %p')
    fh = logging.FileHandler(os.path.join(args.save, 'log.txt'))
    fh.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(fh)

    main()