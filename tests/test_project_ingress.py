"""Exercise the proposed Caddy allowlist with an actual project facade."""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from test_gateway_access import request, running


def test_caddy_project_ingress_route_matrix(tmp_path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy is required for ingress candidate validation")
    with running(tmp_path, private=True) as (backend, _gateway, _queue, calls, _policy):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        snippet = (
            Path(__file__).resolve().parents[1] / "deploy/hp_quota_project_api.caddy"
        ).read_text()
        snippet = snippet.replace("127.0.0.1:18588", backend.removeprefix("http://"))
        config = tmp_path / "Caddyfile"
        config.write_text(
            "{\n admin off\n auto_https off\n}\n"
            + f"http://127.0.0.1:{port} {{\n"
            + snippet
            + '\nhandle {\n respond "not found" 404\n}\n}\n'
        )
        subprocess.run(
            [caddy, "validate", "--config", str(config), "--adapter", "caddyfile"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        process = subprocess.Popen(
            [caddy, "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            end = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert process.poll() is None and time.monotonic() < end
                    time.sleep(0.02)
            base = f"http://127.0.0.1:{port}"
            assert request(base, "a", "GET", "/quota/api/v1/catalog")[0] == 200
            assert request(base, "invalid", "GET", "/quota/api/v1/catalog")[0] == 401
            from test_gateway import task

            assert request(base, "a", "POST", "/quota/api/v1/routes/explain", task())[0] == 200
            assert request(base, "a", "POST", "/quota/api/v1/jobs", task("ingress-001"))[0] == 200
            assert request(base, "a", "GET", "/quota/api/v1/jobs/ingress-001")[0] == 200
            assert request(base, "b", "GET", "/quota/api/v1/jobs/ingress-001/result")[0] == 404
            assert (
                request(base, "a", "POST", "/quota/api/v1/jobs/ingress-001/cancel", {})[1]["state"]
                == "cancelled"
            )
            # Caddy's final fallback is plain text, so inspect only status here.
            import urllib.error
            import urllib.request

            for method, path in [
                ("POST", "jobs/tick"),
                ("GET", "jobs/tick"),
                ("POST", "tasks"),
                ("GET", "usage"),
                ("POST", "catalog"),
                ("GET", "admin/targets"),
                ("GET", "jobs/ingress-001/cancel"),
            ]:
                req = urllib.request.Request(base + "/quota/api/v1/" + path, method=method)
                with pytest.raises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req, timeout=2)
                assert exc.value.code == 404
            assert calls == []
        finally:
            process.terminate()
            process.wait(timeout=5)
