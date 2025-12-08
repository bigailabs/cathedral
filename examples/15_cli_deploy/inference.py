import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import torch


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        info = {
            "cuda_available": torch.cuda.is_available(),
            "device_count": torch.cuda.device_count(),
            "device_name": (
                torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
            ),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(info).encode())


if __name__ == "__main__":
    HTTPServer(("", 8000), Handler).serve_forever()
