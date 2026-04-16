#!/usr/bin/env python3
"""
Decorator-based Hello World deployment.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 05_decorator_hello.py
"""
import cathedral


@cathedral.deployment(name="decorator-hello", port=8000, ttl_seconds=600)
def serve():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Hello from decorator!')

        def log_message(self, *a):
            pass

    HTTPServer(('', 8000), Handler).serve_forever()


deployment = serve()
print(f"Live at: {deployment.url}")
