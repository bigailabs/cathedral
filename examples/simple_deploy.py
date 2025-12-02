#!/usr/bin/env python3
"""
Deploy a persistent counter to Basilica in 10 lines.

This example shows the simplest way to deploy an application using
the Basilica SDK's high-level deploy() method.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 simple_deploy.py
"""
from basilica import BasilicaClient

client = BasilicaClient()

deployment = client.deploy(
    name="counter",
    source="""
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        f = Path('/data/count')
        n = int(f.read_text()) + 1 if f.exists() else 1
        f.write_text(str(n))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f'Visit #{n}'.encode())

HTTPServer(('', 8000), Handler).serve_forever()
""",
    port=8000,
    storage=True,
    ttl_seconds=600,
)

print(f"Live at: {deployment.url}")
