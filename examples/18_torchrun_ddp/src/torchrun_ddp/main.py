import logging
import tempfile
import time

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP  # noqa: N817
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM

from .compression import dct_compression_hook
from .optimizer import create_optimizer
from .training_data import load_training_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHECKPOINT_PATH = tempfile.gettempdir() + "/model.checkpoint"


def cleanup():
    dist.destroy_process_group()


async def run(
    rank: int,
    world_size: int,
    local_rank: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    seq_length: int,
    accumulation_steps: int,
    dataset_name: str,
    *args,
    **kwargs,
):
    logger.info(f"Running with rank {rank}, world size {world_size}, local rank {local_rank}")

    tokenizer = AutoTokenizer.from_pretrained(dataset_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dist.init_process_group("gloo", rank=rank, world_size=world_size)
    logger.info(f"Initialized process group for rank {rank}")

    device = torch.device(f"cuda:{local_rank}")
    torch.cuda.set_device(device)

    def collate_fn(data):
        texts = []
        for d in data:
            texts.append(d["text"])
        return texts

    dataset = load_training_data(rank=rank, world_size=world_size)
    dataloader = DataLoader(
        dataset,  # type: ignore
        batch_size=batch_size,
        collate_fn=collate_fn,
    )

    model = AutoModelForCausalLM.from_pretrained(dataset_name).to(device)
    ddp_model = DDP(model, device_ids=[rank])

    compression_hook = dct_compression_hook()
    ddp_model.register_comm_hook(state=None, hook=compression_hook)
    logger.info("Registered DCT compression hook for gradient communication")

    optimizer, scheduler = create_optimizer(model, learning_rate)

    # Save the model state dict on rank 0
    if rank == 0:
        logger.info(f"Saving model state dict to {CHECKPOINT_PATH}")
        torch.save(ddp_model.state_dict(), CHECKPOINT_PATH)

    # Load the model state dict on all ranks
    dist.barrier()

    map_location = {"cuda:%d" % 0: "cuda:%d" % rank}
    ddp_model.load_state_dict(
        torch.load(CHECKPOINT_PATH, map_location=map_location, weights_only=True)
    )

    start_epoch = 0

    # Training loop
    for epoch in range(start_epoch, epochs):
        model.train()
        total_loss = 0.0
        start_time = time.time()

        for step, batch in enumerate(dataloader):
            tokenized_batch = tokenizer(
                batch,
                return_tensors="pt",
                padding="max_length",
                truncation=True,
                max_length=seq_length,
            )

            input_ids = tokenized_batch["input_ids"].to("cuda")
            attention_mask = tokenized_batch["attention_mask"].to("cuda")

            outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=input_ids)
            loss = outputs.loss / accumulation_steps  # Scale loss for accumulation

            loss.backward()

            batch_time = time.time() - start_time
            tokens_per_batch = input_ids.numel()
            throughput = tokens_per_batch / batch_time
            gpu_memory = torch.cuda.memory_allocated() / (1024**2)

            if (step + 1) % accumulation_steps == 0:
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

                if step % 10 == 0:
                    current_rank = dist.get_rank() if dist.is_initialized() else 0
                    if current_rank == 0:
                        logger.info(
                            f"Epoch: {epoch}/{epochs-1} | "
                            f"Step: {step} | "
                            f"Loss: {loss.item()*accumulation_steps:.4f} | "
                            f"LR: {scheduler.get_last_lr()[0]:.6f} | "
                            f"Throughput: {throughput:.2f} tokens/s | "
                            f"Memory: {gpu_memory:.2f}MB"
                        )

            total_loss += loss.item() * accumulation_steps
            start_time = time.time()

        num_steps = step + 1
        avg_loss = total_loss / num_steps if num_steps > 0 else 0.0
        epoch_time = time.time() - start_time

        logger.info(
            f"Epoch {epoch}/{epochs-1} completed in {epoch_time:.2f}s | "
            f"Steps: {num_steps} | Avg loss: {avg_loss:.4f}"
        )

    logger.info(f"Rank {rank} finished training step")

    # Synchronize all processes
    dist.barrier()

    torch.save(
        {
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "epoch": epoch,
        },
        CHECKPOINT_PATH,
    )

    if dist.is_initialized() and rank == 0:
        logger.info("Saving final model and tokenizer to /data/final_model")
        model.save_pretrained("/data/final_model")
        tokenizer.save_pretrained("/data/final_model")
        logger.info("Training complete!")

    cleanup()
