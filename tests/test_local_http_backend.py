"""Real authenticated TLS worker fixture, including an accepted POST with no reply."""

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_encrypted_api import encrypted_fixture
from test_local_jobs import Backend, local_setup, submit

from quota_broker.local_jobs import LocalHTTPBackend, LocalTarget


def test_tls_worker_drop_and_exact_read_only_recovery(tmp_path):
    fake = Backend()
    worker_token = "synthetic-private-worker-token-at-least-32-characters"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.handle_request(None)

        def do_POST(self):
            self.handle_request(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def handle_request(self, body):
            assert self.headers["Authorization"] == "Bearer " + worker_token
            received.append((self.command, self.path))
            if self.command == "POST":
                fake.submit(self.headers["Idempotency-Key"], body)
                self.close_connection = True
                return  # Simulate durable acceptance followed by a dropped response.
            if self.path.startswith("/v1/jobs/by-idempotency/"):
                reply = fake.lookup(self.path.rsplit("/", 1)[1])
            elif self.path.endswith("/result"):
                reply = fake.result(self.path.split("/")[3])
            else:
                reply = fake.status(self.path.rsplit("/", 1)[1])
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with encrypted_fixture(tmp_path) as fixture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp_path / "tls.crt", tmp_path / "tls.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            token = tmp_path / "worker.token"
            token.write_text(worker_token)
            token.chmod(0o600)
            target = LocalTarget(
                "fixture4090",
                f"https://127.0.0.1:{server.server_port}",
                fake.target.deployment_id,
                fake.target.model,
            )
            backend = LocalHTTPBackend(
                target, token, ca_file=str(tmp_path / "tls.crt"), exact_lookup=True
            )
            local, access = local_setup(fixture[3], fixture[4], fixture[6], backend)
            internal, _ = submit(local, access, fixture[6])
            local.tick()
            assert local.status(internal)["state"] == "unknown"
            local.tick()
            assert local.status(internal)["state"] == "queued"
            next(iter(fake.jobs.values())).update(
                state="succeeded", result_state="available", ended=fixture[3].clock().timestamp()
            )
            local.tick()
            assert local.result(internal)["result"]["text"] == "private local answer"
            assert sum(method == "POST" for method, _path in received) == 1
            assert fixture[5] == []
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
