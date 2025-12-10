import asyncio
import logging
import os

import torchrun_ddp
from dotenv import load_dotenv
from huggingface_hub import login

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

HF_TOKEN = os.getenv("HF_TOKEN", "")

RANK = int(os.getenv("RANK", 0))
WORLD_SIZE = int(os.getenv("WORLD_SIZE", 0))
LOCAL_RANK = int(os.getenv("LOCAL_RANK", 0))
GROUP_RANK = int(os.getenv("GROUP_RANK", 0))
LOCAL_WORLD_SIZE = int(os.getenv("LOCAL_WORLD_SIZE", 0))
ROLE_WORLD_SIZE = int(os.getenv("ROLE_WORLD_SIZE", 0))
MASTER_ADDR = os.getenv("MASTER_ADDR", "localhost")
MASTER_PORT = int(os.getenv("MASTER_PORT", 29400))
TORCHELASTIC_RESTART_COUNT = int(os.getenv("TORCHELASTIC_RESTART_COUNT", 0))
TORCHELASTIC_MAX_RESTARTS = int(os.getenv("TORCHELASTIC_MAX_RESTARTS", 0))
TORCHELASTIC_RUN_ID = os.getenv("TORCHELASTIC_RUN_ID", "")
PYTHON_EXEC = os.getenv("PYTHON_EXEC", "python3")

login(token=HF_TOKEN)


async def main() -> None:
    logger.info(
        f"Hello from torchrun-ddp! (rank {RANK}) (world size {WORLD_SIZE}) (local rank {LOCAL_RANK})"
    )
    await torchrun_ddp.run(
        rank=RANK,
        world_size=WORLD_SIZE,
        local_rank=LOCAL_RANK,
        group_rank=GROUP_RANK,
        local_world_size=LOCAL_WORLD_SIZE,
        role_world_size=ROLE_WORLD_SIZE,
        master_addr=MASTER_ADDR,
        master_port=MASTER_PORT,
        torchelastic_restart_count=TORCHELASTIC_RESTART_COUNT,
        torchelastic_max_restarts=TORCHELASTIC_MAX_RESTARTS,
        torchelastic_run_id=TORCHELASTIC_RUN_ID,
        python_exec=PYTHON_EXEC,
        dataset_name="gpt2",
        learning_rate=1e-5,
        seq_length=128,
        accumulation_steps=1,
        batch_size=4,
        epochs=1,
    )


if __name__ == "__main__":
    asyncio.run(main())
