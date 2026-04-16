#!/usr/bin/env python3
"""
Hello World - Simplest Cathedral deployment.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 01_hello_world.py
"""
from cathedral import CathedralClient

client = CathedralClient()

deployment = client.deploy(
    name="hello",
    source="""
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Hello from Cathedral!')

HTTPServer(('', 8000), Handler).serve_forever()
""",
    port=8000,
    ttl_seconds=600,
)

print(f"Live at: {deployment.url}")
