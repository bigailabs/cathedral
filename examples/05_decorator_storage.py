#!/usr/bin/env python3
"""
Decorator-based deployment with persistent storage.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 05_decorator_storage.py
"""
import cathedral

cache = cathedral.Volume.from_name("counter-cache", create_if_missing=True)


@cathedral.deployment(
    name="decorator-counter",
    port=8000,
    volumes={"/data": cache},
    ttl_seconds=600,
)
def serve():
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

        def log_message(self, *a):
            pass

    HTTPServer(('', 8000), Handler).serve_forever()


deployment = serve()
print(f"Live at: {deployment.url}")
print("Try refreshing - the counter persists!")
