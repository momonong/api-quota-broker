"""The acceptance client never sends credentials or valid model requests."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "verify_quota_public", Path(__file__).parents[1] / "scripts/verify_quota_public.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_default_plan_does_not_open_connections():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network in plan")

    result = check.verify(connection=forbidden)
    assert result["status"] == "planned"
    assert result["requests"] == result["provider_calls"] == 0


def test_fixed_anonymous_checks_are_bounded_and_do_not_follow_redirects(tmp_path):
    calls = []
    for name in check.ASSETS.values():
        (tmp_path / name).write_bytes(b"synthetic public asset")

    class Client:
        def __init__(self, host, **kwargs):
            assert host == "momonong.me"
            assert kwargs["timeout"] == 8

        def request(self, method, path, body, headers):
            assert set(headers) == {"Host", "Origin", "Content-Type", "X-Dashboard-Request"}
            assert body == (b"{}" if method == "POST" else None)
            self.path = path
            calls.append((method, path))

        def getresponse(self):
            path = self.path

            class Response:
                status = next(s for m, p, s in check.CHECKS if p == path)

                def read(self, bound):
                    assert bound == check.LIMIT + 1
                    return b"synthetic public asset" if path in check.ASSETS else b"{}"

                def getheader(self, name):
                    return "/quota/" if name == "Location" else "no-store"

            return Response()

        def close(self):
            pass

    result = check.verify(apply=True, assets=tmp_path, connection=Client)
    assert result["status"] == "passed"
    assert len(calls) == result["requests"] == len(check.CHECKS)
    assert result["provider_calls"] == 0


def test_remote_failure_is_not_reflected_or_retried():
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(1)
        raise ValueError("synthetic-secret-not-for-output")

    result = check.verify(apply=True, connection=unavailable)
    assert result["status"] == "failed"
    assert len(calls) == len(check.CHECKS)
    assert "synthetic-secret" not in str(result)
