import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from quota_broker.gateway import Gateway as RealGateway

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("v1_diagnose_once", scripts / "v1_diagnose_once.py")
assert spec is not None and spec.loader is not None
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)


def test_diagnostic_plan_has_no_token_or_provider_request(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_diagnose_once.py"])
    monkeypatch.setattr(diagnose, "service_token", lambda _: 1 / 0)
    assert diagnose.main() == 0
    output = capsys.readouterr().out
    assert "1 Google models GET" in output
    assert "4 provider POSTs maximum" in output
    assert output.count("max_output_tokens=64") == 4


def test_diagnostic_fixture_has_one_get_four_posts_and_never_replays(tmp_path, monkeypatch, capsys):
    db = tmp_path / "v1-diagnose-2026-10-02.sqlite"
    prior = [tmp_path / "first.sqlite", tmp_path / "remaining.sqlite"]
    for path, providers in zip(
        prior,
        (("nvidia", "google"), ("mistral", "cloudflare")),
        strict=True,
    ):
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT, dispatched_at TEXT)")
            con.executemany(
                "INSERT INTO gateway_attempts VALUES(?, '2026-10-02T00:00:00Z')",
                [(provider,) for provider in providers],
            )
    monkeypatch.setattr(diagnose, "DIAGNOSE_DB", db)
    monkeypatch.setattr(diagnose, "PRIOR_DBS", tuple(prior))
    monkeypatch.setattr(sys, "argv", ["v1_diagnose_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(diagnose, "cli", lambda: "fixture-cli")
    monkeypatch.setattr(
        diagnose,
        "metadata_names",
        lambda _: {
            "NVIDIA_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_ACCOUNT_ID",
        },
    )
    token_calls = []

    def token(_):
        assert db.is_file()
        token_calls.append(1)
        return "fixture-token-never-print"

    monkeypatch.setattr(diagnose, "service_token", token)
    monkeypatch.setattr(
        diagnose,
        "doppler_resolver_from_token",
        lambda *_: (
            lambda ref: "fixture-account" if ref == "CLOUDFLARE_ACCOUNT_ID" else "fixture-secret"
        ),
    )
    get_calls = []

    class ModelList:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "models/gemini-3.5-flash-lite",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            ).encode()

    class ModelOpener:
        def open(self, request, timeout):
            assert request.full_url == diagnose.GOOGLE_MODELS_URL
            assert timeout == 15
            get_calls.append(1)
            return ModelList()

    monkeypatch.setattr(diagnose.urllib.request, "build_opener", lambda *_: ModelOpener())
    post_calls = []

    def transport(url, _headers, _payload, _timeout):
        post_calls.append(url)
        if "googleapis" in url:
            body = {
                "candidates": [{"content": {"parts": [{"text": "READY"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
            }
        elif "cloudflare" in url:
            body = {
                "result": {
                    "response": "READY",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "neurons": 2},
                }
            }
        else:
            body = {
                "choices": [{"message": {"content": "READY"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }
        return 200, {}, json.dumps(body).encode()

    monkeypatch.setattr(
        diagnose,
        "Gateway",
        lambda path, targets, key, resolver: RealGateway(
            path, targets, key, resolver, transport=transport
        ),
    )
    assert diagnose.main() == 0
    output = capsys.readouterr().out
    assert len(get_calls) == 1 and len(post_calls) == 4 and len(token_calls) == 1
    assert db.stat().st_mode & 0o777 == 0o600
    assert "fixture-token" not in output and "fixture-secret" not in output
    assert "READY" not in output
    with sqlite3.connect(db) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM diagnostic_gets WHERE dispatched_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert (
            con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
            ).fetchone()[0]
            == 4
        )
        assert (
            con.execute("SELECT state FROM gateway_tasks WHERE provider='nvidia'").fetchone()[0]
            == "completed"
        )
    try:
        diagnose.main()
    except RuntimeError as exc:
        assert "never replay" in str(exc)
    else:
        raise AssertionError("same diagnostic receipt must refuse re-run")
    assert len(get_calls) == 1 and len(post_calls) == 4
