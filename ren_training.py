#!/usr/bin/env python
import copy
import torch

import torch.nn as nn
import matplotlib.pyplot as plt

from torch.utils.tensorboard import SummaryWriter

from IL.ren_discrete import REN
from datetime import datetime
from IL.cli import argument_parser

from dataset import lasa_expert, polynomial_expert, linear_expert


# main entry
if __name__ == '__main__':
    # TODO: neural ode layer instead of consecutive rollouts

    # parse and set experiment arguments
    args = argument_parser()

    # experiment and REN configs
    device = args.device

    ren_horizon = args.horizon
    ren_dim_x = args.dim_x
    ren_dim_in = args.dim_in
    ren_dim_out = args.dim_out
    ren_dim_v = args.dim_v

    total_epochs = args.total_epochs
    patience_epoch = (args.total_epochs // 5) if args.patience_epoch is None else args.patience_epoch
    log_epoch = (args.total_epochs // 10) if args.log_epoch is None else args.log_epoch
    ren_lr = args.lr
    ren_lr_start_factor = args.lr_start_factor
    ren_lr_end_factor = args.lr_end_factor

    expert = args.expert
    batch_size = args.batch_size
    lasa_motion_shape = args.motion_shape

    load_model = args.load_model
    experiment_dir = args.experiment_dir

    # set expert traj (equal to ren horizon for now)
    if expert == "poly":
        expert_trajectory = polynomial_expert(ren_horizon, device)
        y_init = 1.0 * torch.ones((1, 1, ren_dim_out), device=device)

    elif expert == "lin":
        expert_trajectory = linear_expert(ren_horizon, device)
        y_init = 1.0 * torch.ones((1, 1, ren_dim_out), device=device)

    elif expert == "lasa":
        expert_trajectory, dataloader = lasa_expert(lasa_motion_shape, ren_horizon, device, n_dems=batch_size)
        y_init = torch.Tensor(expert_trajectory[:, 0, :]).unsqueeze(1)
        y_init = y_init.to(device)


    # input is set to zero
    u_in = torch.zeros((batch_size, 1, 2), device=device)

    # define REN
    ren_module = REN(dim_in=ren_dim_in, dim_out=ren_dim_out, dim_x=ren_dim_x, dim_v=ren_dim_v, initialization_std=0.1, linear_output=True,
                     contraction_rate_lb=1.0, batch_size=batch_size, device=device)
    ren_module.to(device=device)

    # optimizer
    optimizer = torch.optim.Adam(ren_module.parameters(), lr=ren_lr)

    # loss
    criterion = nn.MSELoss()

    # temps
    trajectories: list = []
    best_model_stat_dict = None
    best_loss = torch.tensor(float('inf'))
    best_train_epoch = 0

    # experiment log setup
    timestamp = datetime.now().strftime('%d-%H%M')
    experiment_name = f'{expert}-{lasa_motion_shape}-h{ren_horizon}-x{ren_dim_x}-l{ren_dim_v}-e{total_epochs}-t{timestamp}'
    writer_dir = f'{experiment_dir}/ren-training-{experiment_name}'
    writer = SummaryWriter(writer_dir)

    # lr scheduler
    scheduler = torch.optim.lr_scheduler.LinearLR(optimizer, start_factor=ren_lr_start_factor,
                            end_factor=ren_lr_end_factor, total_iters=total_epochs)

    # time the operation
    start_time = datetime.now()

    # training epochs
    for epoch in range(total_epochs):

        # zero grad
        optimizer.zero_grad()

        # forward pass
        # TODO: Noise for u to get robust results: u_noise = torch.randn(u_in.shape, device=device) * noise_ratio
        out = ren_module.forward_trajectory(u_in, y_init, ren_horizon)

        # loss
        loss = criterion(out, expert_trajectory)

        # best model
        if loss < best_loss:
            best_model_stat_dict = copy.deepcopy(ren_module.state_dict())
            best_loss = loss
            best_train_epoch = epoch

        # check no progress
        if epoch - best_train_epoch > patience_epoch:
            print(f'No significant progress in a while, aborting training')
            break

        # backward and steps
        loss.backward()
        optimizer.step()
        scheduler.step()
        ren_module.update_model_param()

        # logs
        if epoch % log_epoch == 0:
            print(f'Epoch: {epoch}/{total_epochs} | Best Loss: {best_loss:.8f} | Best Epoch: {best_train_epoch} | LR: {scheduler.get_last_lr()[0]:.6f}')
            trajectories.append(out.detach().cpu().numpy())

        # tensorboard
        writer.add_scalars('Training Loss', {'Training' : loss.item()}, epoch + 1)
        writer.flush()

    # training time and best results
    training_time = datetime.now() - start_time
    print(f'Training Concluded in {training_time}| Best Loss: {best_loss:.8f} | Best Epoch: {best_train_epoch}')

    # save the best model
    best_state = {
        'model_state_dict': best_model_stat_dict,
    }
    file = f'{writer_dir}/best_model.pth'
    torch.save(best_state, file)

    # load the best model for plotting
    ren_module.load_state_dict(best_model_stat_dict)
    ren_module.update_model_param()

    # TODO: move plots to plot tools
    # plot the training trajectories
    expert_trajectory = expert_trajectory.cpu().numpy()

    fig = plt.figure(figsize=(10, 10), dpi=120)
    for idx, tr in enumerate(trajectories):
        plt.plot(tr[0, :, 0], tr[0, :, 1], linewidth=idx * 0.05, c='blue')
    plt.plot(expert_trajectory[0, :, 0], expert_trajectory[0, :, 1], linewidth=1, linestyle='dashed', c='green')
    plt.xlabel('dim0')
    plt.ylabel('dim1')
    plt.savefig(f'{writer_dir}/ren-training-motion-{experiment_name}.png')

    # generate rollouts std
    rollouts = []
    rollouts_horizon = ren_horizon
    num_rollouts = 10
    y_init_std = 0.2

    for _ in range(num_rollouts):
        y_init_rollout = y_init + y_init_std * (2 * torch.rand(*y_init.shape, device=device) - 1)
        rollouts.append(ren_module.forward_trajectory(u_in, y_init_rollout, rollouts_horizon).detach().cpu().numpy())

    fig = plt.figure(figsize=(10, 10), dpi=120)
    for idx, tr in enumerate(rollouts):
        plt.plot(tr[0, :, 0], tr[0, :, 1], linewidth=0.5, c='blue')
    plt.plot(expert_trajectory[0, :, 0], expert_trajectory[0, :, 1], linewidth=1, linestyle='dashed', c='green')
    plt.xlabel('dim0')
    plt.ylabel('dim1')
    plt.savefig(f'{writer_dir}/ren-rollouts-std-motion-{experiment_name}.png')

    # generate rollouts
    rollouts = []
    rollouts_horizon = ren_horizon
    num_rollouts = 10
    y_init_rollout = y_init

    for _ in range(num_rollouts):
        rollouts.append(ren_module.forward_trajectory(u_in, y_init_rollout, rollouts_horizon).detach().cpu().numpy())

    fig = plt.figure(figsize=(10, 10), dpi=120)
    for idx, tr in enumerate(rollouts):
        plt.plot(tr[0, :, 0], tr[0, :, 1], linewidth=0.5, c='blue')
    plt.plot(expert_trajectory[0, :, 0], expert_trajectory[0, :, 1], linewidth=1, linestyle='dashed', c='green')
    plt.xlabel('dim0')
    plt.ylabel('dim1')
    plt.savefig(f'{writer_dir}/ren-rollouts-motion-{experiment_name}.png')