"""Copy the documented examples to another directory and exercise real HTTP.

QUOTA_EXAMPLE_PYTHON can point at an isolated, wheel-installed Python to prove
the consumer does not depend on this checkout or an editable installation.
"""

import http.client
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_family_adapters import GOOGLE
from test_gateway_access import TOKENS, running
from test_project_client import fixture_worker

from quota_broker.client import LegacyProjectClient as ProjectClient
from quota_broker.gateway import GatewayError, validate_task

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("consumer", ["sdk", "http_example"])
def test_consumers_identify_the_application_in_requests(monkeypatch, capsys, consumer):
    from quota_broker import __version__

    seen = []

    class Capture:
        def open(self, request, **_kwargs):
            seen.append(request.get_header("User-agent"))
            return io.BytesIO(b"[]")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Capture())
    if consumer == "sdk":
        assert ProjectClient("https://momonong.me/quota", TOKENS["a"]).catalog() == []
        assert seen == [f"api-quota-broker/{__version__}"]
    else:
        spec = importlib.util.spec_from_file_location(
            "http_example_identity", ROOT / "examples/project_client/http_client.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setenv("QUOTA_PROJECT_TOKEN", TOKENS["a"])
        monkeypatch.setattr(sys, "argv", ["http_client.py", "catalog"])
        assert module.main() == 0
        assert json.loads(capsys.readouterr().out) == []
        assert seen == ["quota-http-example/1.0"]


def test_openapi_priority_boundaries_match_gateway():
    spec = json.loads((ROOT / "docs/openapi.json").read_text())
    task = spec["components"]["schemas"]["Task"]["example"]
    bounds = spec["components"]["schemas"]["Task"]["properties"]["priority"]
    for name, delta in (("minimum", -1), ("maximum", 1)):
        assert validate_task({**task, "priority": bounds[name]})["priority"] == bounds[name]
        with pytest.raises(GatewayError):
            validate_task({**task, "priority": bounds[name] + delta})


def test_plain_http_example_handles_truncated_response(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "http_example", ROOT / "examples/project_client/http_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Broken:
        def open(self, *_args, **_kwargs):
            raise http.client.IncompleteRead(b"{", 100)

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: Broken())
    monkeypatch.setenv("QUOTA_PROJECT_TOKEN", TOKENS["a"])
    monkeypatch.setattr(sys, "argv", ["http_client.py", "submit", "--key", "http-saved"])
    assert module.main() == 1
    assert json.loads(capsys.readouterr().err)["request_key"] == "http-saved"


def test_documented_examples_from_independent_directory(tmp_path):
    consumer = tmp_path / "independent-consumer"
    consumer.mkdir()
    for name in ("client.py", "http_client.py"):
        shutil.copyfile(ROOT / "examples/project_client" / name, consumer / name)
    python = os.environ.get("QUOTA_EXAMPLE_PYTHON", sys.executable)
    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):

        def transport(request, *_args):
            calls.append(request)
            return 200, {}, json.dumps(GOOGLE).encode()

        gateway.family_transport = transport
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env.update(QUOTA_BASE_URL=base + "/quota/api/v1", QUOTA_PROJECT_TOKEN=TOKENS["a"])

        def run(script, *args, who="a", code=0):
            result = subprocess.run(
                [
                    python,
                    str(consumer / script),
                    *(["--legacy"] if script == "client.py" else []),
                    *args,
                ],
                cwd=consumer,
                env={**env, "QUOTA_PROJECT_TOKEN": TOKENS[who]},
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
            assert result.returncode == code, result.stderr
            return json.loads(result.stdout if code == 0 else result.stderr)

        for script in ("client.py", "http_client.py"):
            assert run(script, "catalog")[0]["provider"] == "google"
            plan = run(script, "explain", "--key", "preview")
            assert plan["selected_target_id"] == "google"
        assert not calls

        with fixture_worker(queue):
            outcome = run("client.py", "run", "--key", "sdk-001", "--wait-timeout", "5")
            assert outcome["result"]["text"] == "answer"
            assert run("client.py", "result", "--key", "sdk-001") == outcome
            assert run("http_client.py", "result", "--key", "sdk-001") == outcome
            assert (
                run("http_client.py", "result", "--key", "sdk-001", who="b", code=1)["error"]
                == "not_found"
            )
            assert len(calls) == 1
            job = run("http_client.py", "submit", "--key", "http-001")
            assert job["request_key"] == "http-001"
            ProjectClient(base, TOKENS["a"]).wait("http-001", wait_timeout=5, poll_interval=0.01)
            assert run("http_client.py", "status", "--key", "http-001")["result_available"]
            assert (
                run("http_client.py", "result", "--key", "http-001")["result"]["text"] == "answer"
            )
            assert len(calls) == 2

        run("http_client.py", "submit", "--key", "cancel-001")
        assert run("http_client.py", "cancel", "--key", "cancel-001")["state"] == "cancelled"
        assert len(calls) == 2


def test_openapi_example_uses_the_real_project_contract(tmp_path):
    spec = json.loads((ROOT / "docs/openapi.json").read_text())
    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == {
        "/catalog",
        "/crypto-config",
        "/encrypted",
        "/routes/explain",
        "/jobs",
        "/jobs/{request_key}",
        "/jobs/{request_key}/result",
        "/jobs/{request_key}/cancel",
    }
    with running(tmp_path, private=True) as (base, _gateway, _queue, calls, _policy):
        client = ProjectClient(base, TOKENS["a"])
        task = spec["components"]["schemas"]["Task"]["example"]
        assert client.explain(task)["selected_target_id"] == "google"
        assert client.submit(task)["request_key"] == task["request_key"]
        assert client.cancel(task["request_key"])["state"] == "cancelled"
        assert not calls
