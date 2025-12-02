#!/usr/bin/env python3
"""
Deploy a persistent counter to Basilica in 25 lines.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 simple_deploy.py
"""
import os, requests

r = requests.post(
    "https://api.basilica.ai/deployments",
    headers={"Authorization": f"Bearer {os.environ['BASILICA_API_TOKEN']}"},
    json={
        "instance_name": "counter",
        "image": "python:3.11-alpine",
        "port": 8000,
        "replicas": 1,
        "public": True,
        "ttl_seconds": 600,
        "storage": {"persistent": {"enabled": True, "backend": "r2", "bucket": "", "mountPath": "/data"}},
        "command": ["python", "-c", """
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        f = Path('/data/count')
        n = int(f.read_text()) + 1 if f.exists() else 1
        f.write_text(str(n))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f'Visit #{n}'.encode())
HTTPServer(('', 8000), H).serve_forever()
"""],
    },
)
print(f"Live at: {r.json().get('url')}")
