"""Unit tests for GPU model name extraction."""

import pytest

from basilica import _extract_gpu_model_id


class TestGpuModelExtraction:
    """Tests for _extract_gpu_model_id function."""

    @pytest.mark.parametrize(
        "full_name,expected",
        [
            # Data center GPUs - Ampere, Hopper, Blackwell
            ("NVIDIA A100 80GB PCIe", "A100"),
            ("NVIDIA A100-SXM4-80GB", "A100"),
            ("NVIDIA H100 80GB HBM3", "H100"),
            ("NVIDIA H100 PCIe", "H100"),
            ("NVIDIA H200", "H200"),
            # Other data center GPUs
            ("Tesla V100-SXM2-16GB", "V100"),
            ("NVIDIA V100", "V100"),
            ("Tesla P100-PCIE-16GB", "P100"),
            ("NVIDIA T4", "T4"),
            ("NVIDIA A10", "A10"),
            ("NVIDIA A30", "A30"),
            ("NVIDIA A40", "A40"),
            ("NVIDIA L4", "L4"),
            ("NVIDIA L40", "L40"),
            ("NVIDIA L40S", "L40S"),
            # Consumer RTX GPUs
            ("NVIDIA GeForce RTX 4090", "RTX-4090"),
            ("NVIDIA GeForce RTX 3090", "RTX-3090"),
            ("NVIDIA GeForce RTX 3080", "RTX-3080"),
            ("GeForce RTX 4080", "RTX-4080"),
            # Consumer GTX GPUs
            ("NVIDIA GeForce GTX 1080", "GTX-1080"),
            ("GeForce GTX 1080 Ti", "GTX-1080"),
            # Edge cases
            ("", ""),
            ("Unknown GPU XYZ", "Unknown GPU XYZ"),
        ],
    )
    def test_extracts_model_id(self, full_name: str, expected: str):
        """Test that GPU model ID is correctly extracted from full NVML name."""
        result = _extract_gpu_model_id(full_name)
        assert result == expected, f"Expected '{expected}' for '{full_name}', got '{result}'"

    def test_handles_none_like_empty(self):
        """Test that empty string returns empty string."""
        assert _extract_gpu_model_id("") == ""

    def test_case_insensitive_matching(self):
        """Test that pattern matching is case-insensitive."""
        assert _extract_gpu_model_id("nvidia a100") == "A100"
        assert _extract_gpu_model_id("NVIDIA A100") == "A100"
        assert _extract_gpu_model_id("Nvidia A100") == "A100"
