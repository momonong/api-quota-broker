"""The documented consumer runs outside the repository using a wheel-installed Python."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_encrypted_api import encrypted_fixture
from test_execution_policy import configure
from test_local_jobs import Backend

from quota_broker.access import AccessPolicyFile, ProjectAccess
from quota_broker.local_jobs import LocalJobs


def test_independent_documented_encrypted_consumer(tmp_path):
    consumer = tmp_path / "independent-consumer"
    consumer.mkdir()
    source = Path(__file__).resolve().parents[1] / "examples/project_client/client.py"
    shutil.copyfile(source, consumer / "client.py")
    python = os.environ.get("QUOTA_EXAMPLE_PYTHON", sys.executable)
    backend = Backend()
    with encrypted_fixture(tmp_path, backend=backend) as fixture:
        configure(fixture[6])
        from test_gateway_access import TOKENS

        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env.update(
            QUOTA_BASE_URL=fixture[0],
            QUOTA_PROJECT_TOKEN=TOKENS["a"],
            QUOTA_TRUST_PROFILE=str(tmp_path / "profile.json"),
            QUOTA_TRUST_STATE=str(consumer / "trust.sqlite"),
            QUOTA_CA_FILE=str(tmp_path / "tls.crt"),
        )

        def run(*args):
            proc = subprocess.run(
                [python, "-I", str(consumer / "client.py"), *args],
                cwd=consumer,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)

        assert any(row.get("execution_location") == "local" for row in run("catalog"))
        assert (
            run("explain", "--key", "documented-local", "--execution-policy", "local_only")[
                "execution_policy"
            ]
            == "local_only"
        )
        assert (
            run("submit", "--key", "documented-local", "--execution-policy", "local_only")["state"]
            == "queued"
        )
        local = LocalJobs(
            fixture[4], ProjectAccess(fixture[3], AccessPolicyFile(fixture[6])), backend
        )
        local.tick()
        next(iter(backend.jobs.values())).update(
            state="succeeded", result_state="available", ended=fixture[3].clock().timestamp()
        )
        local.tick()
        assert (
            run("wait", "--key", "documented-local", "--wait-timeout", "2")["result"]["text"]
            == "private local answer"
        )
        assert run("result", "--key", "documented-local")["execution_location"] == "local"
        assert run("status", "--key", "documented-local")["result_available"]
        run("submit", "--key", "cancel-before-dispatch", "--execution-policy", "local_only")
        assert run("cancel", "--key", "cancel-before-dispatch")["state"] == "cancelled"
        assert len(backend.posts) == 1
        assert fixture[5] == []
