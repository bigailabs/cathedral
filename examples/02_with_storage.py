#!/usr/bin/env python3
"""
Persistent storage - Data survives container restarts.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 02_with_storage.py
"""
from cathedral import CathedralClient

client = CathedralClient()

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
    storage=True,  # Mounts persistent storage at /data
    ttl_seconds=600,
)

print(f"Live at: {deployment.url}")
print("Try refreshing - the counter persists!")
