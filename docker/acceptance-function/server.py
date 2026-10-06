"""Small CPU acceptance fixture; reports credential hashes, never plaintext values."""

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

BOOT = uuid4().hex
VALUE = hashlib.sha256(os.environ.get("TEST_CREDENTIAL", "").encode()).hexdigest()
# Keep readiness healthy while injecting candidate-only request failures.
RESPONSE_STATUS = int(os.environ.get("TEST_RESPONSE_STATUS", "200"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length > 65536:
            self.send_response(413)
            self.end_headers()
            return
        self.rfile.read(length)
        result = json.dumps({"boot_id": BOOT, "credential_sha256": VALUE}).encode()
        self.send_response(RESPONSE_STATUS)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
