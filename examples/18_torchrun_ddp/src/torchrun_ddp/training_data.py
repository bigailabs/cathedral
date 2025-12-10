import logging

import datasets
from datasets import Dataset
from datasets.distributed import split_dataset_by_node

logger = logging.getLogger(__name__)


def load_training_data(rank: int, world_size: int, sample_size: int = 1000) -> Dataset:
    """
    Load training data from the C4 dataset.

    Args:
        sample_size: Number of samples to use (for testing)

    Returns:
        List of text samples for training
    """
    logger.info(f"Loading {sample_size} samples from C4 dataset")
    c4 = datasets.load_dataset("allenai/c4", "en", split="train", streaming=True).take(sample_size)  # type: ignore

    dataset = split_dataset_by_node(c4, rank=rank, world_size=world_size)  # type: ignore
    dataset.with_format("torch")

    return dataset
