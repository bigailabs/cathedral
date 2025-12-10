import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, LinearLR, SequentialLR


def create_optimizer(model, learning_rate):
    """
    Create optimizer and learning rate scheduler.

    Args:
        model: The model to optimize
        learning_rate: Base learning rate

    Returns:
        Tuple of (optimizer, scheduler)
    """
    optimizer = optim.AdamW(
        model.parameters(), lr=learning_rate, betas=(0.9, 0.999), eps=1e-8, weight_decay=0.01
    )

    # Linear warmup for 250 steps
    warmup_scheduler = LinearLR(optimizer, start_factor=0.1, end_factor=1.0, total_iters=250)

    # Cosine annealing with restarts
    cosine_scheduler = CosineAnnealingWarmRestarts(
        optimizer, T_0=1000, T_mult=2, eta_min=learning_rate * 0.1
    )

    # Combine schedules
    scheduler = SequentialLR(
        optimizer, schedulers=[warmup_scheduler, cosine_scheduler], milestones=[250]
    )

    return optimizer, scheduler
