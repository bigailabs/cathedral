from http.server import HTTPServer, BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from Basilica!")


if __name__ == "__main__":
    HTTPServer(("", 8000), Handler).serve_forever()
