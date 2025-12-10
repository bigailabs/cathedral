import torch
import torch.distributed as dist


def dct_compression_hook():
    """
    Create a gradient compression hook using Discrete Cosine Transform.

    This hook reduces communication bandwidth by ~50% during training
    by transforming gradients to frequency domain and discarding
    high-frequency components.

    Returns:
        A communication hook function for DDP
    """

    def dct_compress_hook(
        state: object, bucket: dist.GradBucket
    ) -> torch.futures.Future[torch.Tensor]:
        tensor = bucket.buffer()

        compressed_tensor = torch.fft.dct(tensor, norm="ortho")[: tensor.size(0) // 2]

        fut = dist.all_reduce(compressed_tensor, op=dist.ReduceOp.SUM, async_op=True).get_future()

        def decompress(fut_result):
            decompressed_tensor = torch.zeros_like(tensor)
            decompressed_tensor[: tensor.size(0) // 2] = fut_result.value()

            return torch.fft.idct(decompressed_tensor, norm="ortho")

        return fut.then(decompress)

    return dct_compress_hook
