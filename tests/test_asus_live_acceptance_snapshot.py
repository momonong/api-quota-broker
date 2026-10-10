import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "snapshot_transport", ROOT / "deploy/asus/live_acceptance_snapshot_transport.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def snapshot():
    return {
        "queue": [
            {
                "request_key": "asus-normal-v1-2026-10-08-r1-queued-auto-a1",
                "state": "running",
                "execution_key": "q-" + "a" * 32,
            }
        ],
        "tasks": [
            {
                "request_key": "q-" + "a" * 32,
                "state": "dispatched",
                "provider": "groq",
                "http_status": None,
                "dispatched": 1,
                "completed": 0,
            }
        ],
        "attempts": [],
        "reservations": [],
        "counts": {
            k: 0
            for k in (
                "gateway_tasks",
                "gateway_attempts",
                "reservations",
                "charges",
                "execution_completion",
                "queue_jobs",
                "queue_attempts",
            )
        },
        "provider_posts": 0,
        "database_writes": 0,
        "query_only": True,
    }


def test_actual_remote_prefix_resolves_validator_dependencies():
    namespace = {"__name__": "fixture", "__file__": str(Path(m.t.__file__).resolve())}
    prefix = Path(m.t.__file__).read_text().rsplit('if __name__ == "__main__":', 1)[0]
    exec(compile(prefix + m.VALIDATOR, "remote-validator", "exec"), namespace)  # noqa: S102 - exact reviewed public adapter
    assert namespace["validate_probe"](snapshot()) == snapshot()


@pytest.mark.parametrize("fault", ["raw", "foreign_key", "bad_provider", "string_number"])
def test_snapshot_validator_rejects_unreviewed_output(fault):
    obj = snapshot()
    if fault == "raw":
        obj["raw"] = "private fixture"
    elif fault == "foreign_key":
        obj["tasks"][0]["request_key"] = "foreign"
    elif fault == "bad_provider":
        obj["tasks"][0]["provider"] = "private fixture"
    else:
        obj["tasks"][0]["http_status"] = "private fixture"
    with pytest.raises(m.t.Denied):
        m.validate_probe(obj)
