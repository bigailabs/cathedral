"""Narrow Bittensor 8/10 compatibility boundary."""

from __future__ import annotations

from typing import Any


def bt_class(bt: Any, modern: str, legacy: str) -> Any:
    value = getattr(bt, modern, None) or getattr(bt, legacy, None)
    if value is None:
        raise RuntimeError(f"installed bittensor lacks {modern}/{legacy}")
    return value


def make_wallet(bt: Any, *, name: str, hotkey: str, path: str | None = None) -> Any:
    kwargs = {"name": name, "hotkey": hotkey}
    if path:
        kwargs["path"] = path
    return bt_class(bt, "Wallet", "wallet")(**kwargs)


def make_subtensor(bt: Any, *, network: str) -> Any:
    return bt_class(bt, "Subtensor", "subtensor")(network=network)


def make_dendrite(bt: Any, *, wallet: Any) -> Any:
    return bt_class(bt, "Dendrite", "dendrite")(wallet=wallet)


def make_axon(
    bt: Any,
    *,
    wallet: Any,
    port: int,
    external_ip: str | None,
    external_port: int | None,
    max_workers: int,
) -> Any:
    kwargs: dict[str, Any] = {
        "wallet": wallet,
        "port": port,
        "max_workers": max_workers,
    }
    if external_ip:
        kwargs["external_ip"] = external_ip
    if external_port:
        kwargs["external_port"] = external_port
    return bt_class(bt, "Axon", "axon")(**kwargs)


def listify(value: Any) -> list[Any]:
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def current_block(subtensor: Any) -> int:
    getter = getattr(subtensor, "get_current_block", None)
    if callable(getter):
        return int(getter())
    value = getattr(subtensor, "block", None)
    return int(value() if callable(value) else value)
