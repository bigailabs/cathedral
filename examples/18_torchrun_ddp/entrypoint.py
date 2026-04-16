#!/usr/bin/env python3
"""
Entrypoint for torchrun-ddp Cathedral deployment.

Runs a health server for Cathedral health checks while executing
the distributed training job.
"""
import sys
print(f"[entrypoint] Starting, cwd={__import__('os').getcwd()}", flush=True)

import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class TrainingState:
    """Thread-safe training state tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "pending"
        self._message = "Training not started"
        self._exit_code: Optional[int] = None
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None

    def set_running(self) -> None:
        with self._lock:
            self._status = "running"
            self._message = "Training in progress"
            self._start_time = time.time()

    def set_completed(self, exit_code: int) -> None:
        with self._lock:
            self._end_time = time.time()
            self._exit_code = exit_code
            if exit_code == 0:
                self._status = "completed"
                self._message = "Training completed successfully"
            else:
                self._status = "failed"
                self._message = f"Training failed with exit code {exit_code}"

    def get_state(self) -> dict:
        with self._lock:
            state = {
                "status": self._status,
                "message": self._message,
                "exit_code": self._exit_code,
            }
            if self._start_time:
                state["start_time"] = self._start_time
                elapsed = (self._end_time or time.time()) - self._start_time
                state["elapsed_seconds"] = round(elapsed, 2)
            return state


training_state = TrainingState()


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and status reporting."""

    def log_message(self, format: str, *args) -> None:
        logger.debug("HTTP: %s", format % args)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"status": "healthy"})
        elif self.path == "/status":
            self._send_json(training_state.get_state())
        elif self.path == "/":
            state = training_state.get_state()
            self._send_json({
                "service": "torchrun-ddp",
                "training": state,
            })
        else:
            self._send_json({"error": "not found"}, 404)


def run_health_server(port: int) -> None:
    """Run the health check HTTP server."""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()


def run_training() -> int:
    """Run the torchrun training process."""
    nproc = os.getenv("NPROC_PER_NODE", "1")

    cmd = [
        "torchrun",
        "--standalone",
        f"--nproc_per_node={nproc}",
        "train.py",
    ]

    logger.info(f"Starting training: {' '.join(cmd)}")
    training_state.set_running()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in process.stdout:
        print(line, end="", flush=True)

    process.wait()
    return process.returncode


def main() -> None:
    port = int(os.getenv("PORT", "8000"))

    health_thread = threading.Thread(target=run_health_server, args=(port,), daemon=True)
    health_thread.start()

    time.sleep(1)

    try:
        exit_code = run_training()
        training_state.set_completed(exit_code)
        logger.info(f"Training finished with exit code {exit_code}")
    except Exception as e:
        logger.exception(f"Training failed with exception: {e}")
        training_state.set_completed(1)

    logger.info("Keeping container alive for status queries...")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
