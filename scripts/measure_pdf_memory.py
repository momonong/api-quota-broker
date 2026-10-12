"""Linux-only offline 8 MiB PDF smoke with a separately spawned Broker.

Run: uv run --locked python scripts/measure_pdf_memory.py
Default is the original whitespace-content fixture; --image-xobject selects
the alternative raw RGB image PDF. --stress-whitespace is an explicit default.
AQB_FIXTURE_CGROUP=/sys/fs/cgroup/<already delegated unit> moves only the spawned
Broker into that existing cgroup before application imports. The client and
sampler stay outside. The target must have CPU50%/High384MiB/Max512MiB/Swap0.
Uses the project's locked runtime dependencies (including pypdf), not accounts
or credentials. The real family transport encodes and pipes the complete request
to its child; only that child's HTTP implementation is replaced by a fixture.
RSS/PSS are sampled sums for the Broker process tree, excluding this HTTP client.
These sampled peaks are not a cgroup peak or ASUS deployment acceptance.
"""

import base64
import hashlib
import http.client
import io
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
PDF_BYTES = 8 * MIB
TOKEN = "offline-fixture-client-token-at-least-32-characters"
MODEL = "gemini-3.5-flash-lite"
SAMPLE_SECONDS = 0.005
POLL_SECONDS = 1.0

# Preserve config JSON parsing and body base64 decoding used by the real child.
# Body JSON parsing additionally verifies that the full PDF/prompt/schema arrived.
# The short hold makes this real child allocation observable to the sampler.
FIXTURE_CHILD = r"""
import base64,json,sys,time
fixture_count_generation = True
def no_network(event,args):
 if event == 'socket.connect': raise RuntimeError('offline fixture')
sys.addaudithook(no_network)
wire=sys.stdin.buffer.read()
pipe_bytes=len(wire)
config=json.loads(wire)
del wire
body=base64.b64decode(config['body'],validate=True)
payload=json.loads(body)
url=config['url']
if url.endswith(':countTokens'):
 payload=payload['generateContentRequest']
 assert payload['model']=='models/gemini-3.5-flash-lite'
 result={'totalTokens':300}
elif url.endswith(':generateContent'):
 result={'candidates':[{'content':{'parts':[{'text':'{"items":[{"description":"fixture item","quantity":1}]}'}]},'finishReason':'STOP'}],
         'usageMetadata':{'promptTokenCount':300,'candidatesTokenCount':16}}
else: raise RuntimeError('unsupported fixture endpoint')
assert payload['generationConfig']['maxOutputTokens']==2048
assert 'responseJsonSchema' in payload['generationConfig']
assert len(payload['contents'][0]['parts'][1]['inlineData']['data'])==11184812
time.sleep(0.05)
raw=json.dumps(result,separators=(',',':')).encode()
head={'status':200,'headers':{'Content-Length':str(len(raw)),
 'X-Fixture-Request-Bytes':str(len(body)),'X-Fixture-Pipe-Bytes':str(pipe_bytes)}}
sys.stdout.buffer.write(json.dumps(head,separators=(',',':')).encode()+b'\n'+raw)
"""


def _no_outbound_network(event: str, _args: tuple[Any, ...]) -> None:
    if event == "socket.connect":
        raise RuntimeError("offline Broker cannot connect to any network endpoint")


def _cgroup() -> str:
    return Path("/proc/self/cgroup").read_text().strip()


def _join_fixture_cgroup() -> dict[str, Any]:
    before = _cgroup()
    metadata: dict[str, Any] = {"pid": os.getpid(), "cgroup_before": before, "delegated": False}
    requested = os.environ.get("AQB_FIXTURE_CGROUP")
    if requested:
        directory = Path(requested).resolve(strict=True)
        root = Path("/sys/fs/cgroup")
        if directory == root or not directory.is_relative_to(root):
            raise ValueError("fixture cgroup must be under the unified cgroup mount")
        limits = {
            name: (directory / name).read_text().strip()
            for name in ("cpu.max", "memory.high", "memory.max", "memory.swap.max", "pids.max")
        }
        quota, period = limits["cpu.max"].split()
        if (
            quota == "max"
            or int(quota) <= 0
            or int(period) != 2 * int(quota)
            or limits["memory.high"] != str(384 * MIB)
            or limits["memory.max"] != str(512 * MIB)
            or limits["memory.swap.max"] != "0"
        ):
            raise ValueError("fixture cgroup resource limits differ")
        fd = os.open(directory / "cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW)
        try:
            raw = (str(os.getpid()) + "\n").encode()
            if os.write(fd, raw) != len(raw):
                raise OSError("incomplete cgroup migration")
        finally:
            os.close(fd)
        expected = "0::/" + str(directory.relative_to(root))
        if _cgroup() != expected:
            raise ValueError("fixture cgroup migration not confirmed")
        # Moving a process does not inherit the unit holder's per-process rlimit.
        import resource

        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        metadata.update(
            delegated=True,
            requested_directory=str(directory),
            limits=limits,
            nofile_limit=list(resource.getrlimit(resource.RLIMIT_NOFILE)),
        )
    metadata["cgroup_after"] = _cgroup()
    return metadata


def _broker(directory: str, connection: Any, stop: Any) -> None:
    # Spawn starts without the client's PDF, JSON, pypdf writer, or heap.
    try:
        cgroup = _join_fixture_cgroup()
    except Exception as error:  # noqa: BLE001 - safe setup classification only
        connection.send(
            {
                "broker_error_type": type(error).__name__,
                "broker_process": {"pid": os.getpid(), "cgroup_after": _cgroup()},
            }
        )
        connection.close()
        return
    sys.addaudithook(_no_outbound_network)
    import resource

    from quota_broker import families, family_transport
    from quota_broker.config import Quota, Target
    from quota_broker.families import MediaLimits
    from quota_broker.gateway import Gateway
    from quota_broker.gateway_server import make_gateway_server
    from quota_broker.queue import DurableQueue

    family_transport._HTTP_CHILD = FIXTURE_CHILD
    now = datetime.now(UTC)
    target = Target(
        id="offline-google-pdf",
        provider="google",
        model=MODEL,
        account_id="offline-fixture-account",
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        quotas=(
            Quota("fixture-rpm", "requests", 3, "rolling_minute"),
            Quota("fixture-rpd", "requests", 100, "day", "America/Los_Angeles"),
            Quota("fixture-tpm", "input_tokens", 131328, "rolling_minute"),
        ),
        concurrency_limit=1,
        max_output_tokens=8192,
        priority=0,
        source="offline fixture, not account evidence",
        shared_concurrency_scope="offline-fixture-google",
        shared_concurrency_limit=1,
        secret_ref="OFFLINE_FIXTURE_KEY",
        quota_basis="local_safety_cap",
    )
    metrics: dict[str, Any] = {
        "countTokens_posts": 0,
        "generation_posts": 0,
        "transport": [],
        "pdf_parser_events": [],
        "broker_process": cgroup,
    }
    run = families.subprocess.run

    def observed_run(command: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(command, (list, tuple)) or families._PDF_PROGRAM not in command:
            return run(command, *args, **kwargs)
        started = time.monotonic()
        cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        event: dict[str, Any] = {
            "returncode": None,
            "elapsed_seconds": None,
            "timeout_expired": False,
        }
        try:
            completed = run(command, *args, **kwargs)
            event["returncode"] = completed.returncode
            return completed
        except families.subprocess.TimeoutExpired:
            event["timeout_expired"] = True
            raise
        finally:
            event["elapsed_seconds"] = round(time.monotonic() - started, 3)
            cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            event["child_user_cpu_seconds"] = round(cpu_after.ru_utime - cpu_before.ru_utime, 3)
            event["child_system_cpu_seconds"] = round(cpu_after.ru_stime - cpu_before.ru_stime, 3)
            metrics["pdf_parser_events"].append(event)

    # Observe only the exact installed PDF program. All limits and code stay intact.
    families.subprocess.run = observed_run  # type: ignore[assignment]

    def transport(request: Any, timeout: float, response_bound: int) -> Any:
        kind = "countTokens" if request.url.endswith(":countTokens") else "generation"
        metrics[kind + "_posts"] += 1
        status, headers, raw = family_transport.send_family_request(
            request, timeout, response_bound
        )
        metrics["transport"].append(
            {
                "kind": kind,
                "request_bytes": int(headers["X-Fixture-Request-Bytes"]),
                "pipe_config_bytes": int(headers["X-Fixture-Pipe-Bytes"]),
                "response_bytes": len(raw),
            }
        )
        return status, headers, raw

    def forbidden_legacy(*_args: Any) -> Any:
        raise RuntimeError("legacy provider transport is forbidden in this fixture")

    database = Path(directory) / "broker.sqlite"
    database.touch(mode=0o600)
    gateway = Gateway(
        database,
        (target,),
        b"offline-fixture-hmac-key".ljust(32, b"!"),
        lambda _ref: "offline-fixture-provider-credential",
        forbidden_legacy,
        family_transport=transport,
        media_limits=MediaLimits(PDF_BYTES, 16 * MIB, 4 * MIB),
        google_pdf_count_tokens=True,
    )
    queue = DurableQueue(gateway, b"offline-fixture-queue-key".ljust(32, b"!"))
    server = make_gateway_server(
        gateway, TOKEN, port=0, queue=queue, worker=True, serialize_work=True
    )
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    connection.send({"port": server.server_port, "broker_process": cgroup})
    try:
        if not stop.wait(timeout=90):
            raise TimeoutError("offline smoke exceeded its lifetime")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        worker_thread = getattr(server, "worker_thread", None)
        if worker_thread is not None:
            worker_thread.join(timeout=7)
        connection.send(metrics)
        connection.close()


def _pdf(*, stress_whitespace: bool = True) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    stream = DecodedStreamObject()
    page[NameObject("/Contents")] = writer._add_object(stream)
    content = b""
    padding = PDF_BYTES - 600 if stress_whitespace else 0
    if not stress_whitespace:
        image = DecodedStreamObject()
        image.set_data(b"\xff" * (1024 * 2730 * 3))
        image.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1024),
                NameObject("/Height"): NumberObject(2730),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(image)}
                )
            }
        )
        content = b"q\n200 0 0 200 0 0 cm\n/Im0 Do\nQ\n"
    for _attempt in range(5):
        stream.set_data(content + b" " * padding)
        output = io.BytesIO()
        writer.write(output)
        raw = output.getvalue()
        if len(raw) == PDF_BYTES:
            assert len(PdfReader(io.BytesIO(raw), strict=True).pages) == 1
            return raw
        padding += PDF_BYTES - len(raw)
    raise ValueError("could not construct an exact 8 MiB PDF")


def _process_tree(root: int) -> set[int]:
    pending, found = [root], set()
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            # PDF and transport children can be created by any server thread.
            for task in (Path("/proc") / str(pid) / "task").iterdir():
                try:
                    pending.extend(int(value) for value in (task / "children").read_text().split())
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def _sample(root: int, stop: threading.Event, metrics: dict[str, Any]) -> None:
    seen_pdf, seen_transport = set(), set()
    while not stop.is_set():
        rss = pss = active = 0
        for pid in _process_tree(root):
            proc = Path("/proc") / str(pid)
            try:
                readings = {}
                for line in (proc / "smaps_rollup").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key in {"Rss", "Pss"}:
                        readings[key] = int(value.split()[0])
                rss += readings["Rss"]
                pss += readings["Pss"]
                active += 1
                command = (proc / "cmdline").read_bytes()
                if pid != root and b"from pypdf import PdfReader" in command:
                    seen_pdf.add(pid)
                if pid != root and b"fixture_count_generation" in command:
                    seen_transport.add(pid)
            except (FileNotFoundError, ProcessLookupError):
                pass
        metrics["rss_peak_kib"] = max(metrics["rss_peak_kib"], rss)
        metrics["pss_peak_kib"] = max(metrics["pss_peak_kib"], pss)
        metrics["max_processes"] = max(metrics["max_processes"], active)
        metrics["samples"] += 1
        metrics["pdf_children_seen"] = len(seen_pdf)
        metrics["transport_children_seen"] = len(seen_transport)
        stop.wait(SAMPLE_SECONDS)


def _http(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    metrics: dict[str, int] | None = None,
) -> tuple[int, dict]:
    for attempt in range(4):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        upload_error = None
        try:
            try:
                client.request(
                    method,
                    path,
                    body=body,
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Content-Type": "application/json",
                    },
                )
            except BrokenPipeError as error:
                upload_error = error
            try:
                response = client.getresponse()
                raw = response.read()
                content_lengths = [
                    v for k, v in response.getheaders() if k.lower() == "content-length"
                ]
                busy = (
                    response.status == 503
                    and raw == b'{"error":"work_busy"}'
                    and content_lengths == [str(len(raw))]
                    and response.getheader("Content-Type") == "application/json"
                )
                if upload_error is not None and not busy:
                    raise upload_error
                data = json.loads(raw)
            except Exception:
                # An unreadable or different response does not prove non-admission.
                if upload_error is not None:
                    raise upload_error from None
                raise
            if upload_error is not None and metrics is not None:
                metrics["broken_pipe_busy_responses"] += 1
            if busy and method == "POST" and attempt < 3:
                if metrics is not None:
                    metrics["post_busy_retries"] += 1
            else:
                return response.status, data
        finally:
            client.close()
        # Only a framed, exact non-admission receipt reaches this same-body retry.
        time.sleep(0.2)
    raise AssertionError("unreachable retry state")


def main() -> int:
    if not sys.platform.startswith("linux") or not Path("/proc/self/smaps_rollup").is_file():
        raise SystemExit("Linux /proc smaps_rollup is required")
    context = multiprocessing.get_context("spawn")
    memory = {
        "rss_peak_kib": 0,
        "pss_peak_kib": 0,
        "max_processes": 0,
        "samples": 0,
        "pdf_children_seen": 0,
        "transport_children_seen": 0,
    }
    started = time.monotonic()
    if sys.argv[1:] not in ([], ["--stress-whitespace"], ["--image-xobject"]):
        raise SystemExit("only --stress-whitespace or --image-xobject is supported")
    stress_whitespace = sys.argv[1:] != ["--image-xobject"]
    failure: BaseException | None = None
    stage = "broker_start"
    metadata: dict[str, Any] = {}
    result: dict[str, Any] = {}
    transport: dict[str, Any] = {}
    broker_metadata: dict[str, Any] = {}
    broker_error_type = None
    client_cgroup = _cgroup()
    client_metrics = {"post_busy_retries": 0, "broken_pipe_busy_responses": 0}
    input_bytes = http_bytes = None
    pdf_sha256 = None
    with tempfile.TemporaryDirectory(prefix="broker-pdf-memory-") as directory:
        os.chmod(directory, 0o700)
        parent, child = context.Pipe(duplex=False)
        stop = context.Event()
        broker = context.Process(target=_broker, args=(directory, child, stop))
        broker.start()
        child.close()
        sampling_stop = threading.Event()
        sampler = threading.Thread(target=_sample, args=(broker.pid, sampling_stop, memory))
        sampler.start()
        try:
            if not parent.poll(10):
                raise TimeoutError("offline Broker did not start")
            ready = parent.recv()
            broker_metadata = ready.get("broker_process", {})
            broker_error_type = ready.get("broker_error_type")
            if broker_error_type is not None:
                raise RuntimeError("Broker cgroup setup failed")
            port = ready["port"]
            if os.environ.get("AQB_FIXTURE_CGROUP"):
                assert (
                    broker_metadata["delegated"]
                    and broker_metadata["cgroup_after"] != client_cgroup
                )
            stage = "pdf_create"
            pdf = _pdf(stress_whitespace=stress_whitespace)
            input_bytes = len(pdf)
            pdf_sha256 = hashlib.sha256(pdf).hexdigest()
            document: dict[str, Any] = {
                "request_key": "offline-8mib-pdf",
                "capability": "text_generation",
                "provider": "google",
                "model": MODEL,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Extract the fixture item. Reply in JSON.",
                                },
                                {
                                    "type": "document",
                                    "mime_type": "application/pdf",
                                    "data": base64.b64encode(pdf).decode(),
                                },
                            ],
                        }
                    ]
                },
                "options": {
                    "json_schema": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string"},
                                        "quantity": {"type": "integer"},
                                    },
                                },
                            }
                        },
                        "required": ["items"],
                    }
                },
                "requirements": {"features": ["document_input", "structured_output"]},
                "max_output_tokens": 2048,
                "max_attempts": 1,
                "wait_policy": "reject",
            }
            body = json.dumps(document, separators=(",", ":")).encode()
            http_bytes = len(body)
            stage = "queue_submit"
            status, submitted = _http(port, "POST", "/v1/queue", body, metrics=client_metrics)
            assert status == 200 and submitted["state"] == "queued", submitted
            stage = "queue_wait"
            path = "/v1/queue/" + document["request_key"]
            deadline = time.monotonic() + 60
            while True:
                status, metadata = _http(port, "GET", path)
                assert status == 200, metadata
                if metadata["state"] in {"completed", "failed", "unknown", "expired"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("offline queue did not finish")
                time.sleep(POLL_SECONDS)
            assert metadata["state"] == "completed", metadata
            stage = "result_get"
            # A result GET can coincide with an idle tick holding the work gate.
            while True:
                status, result = _http(port, "GET", path + "/result")
                if status != 503 or result != {"error": "work_busy"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("offline result remained busy")
                time.sleep(0.02)
            assert status == 200 and result["state"] == "completed", result
            assert result["result"]["text"] == (
                '{"items":[{"description":"fixture item","quantity":1}]}'
            )
        except BaseException as error:  # noqa: BLE001 - output classification, never error text
            failure = error
        finally:
            stop.set()
            broker.join(timeout=10)
            if broker.is_alive():
                broker.terminate()
                broker.join(timeout=5)
            sampling_stop.set()
            sampler.join(timeout=5)
        try:
            if parent.poll(2):
                transport = parent.recv()
            if failure is None:
                stage = "resource_check"
                assert broker.exitcode == 0, "offline Broker failed"
                assert transport["countTokens_posts"] == transport["generation_posts"] == 1
                assert memory["pdf_children_seen"] >= 1 and memory["transport_children_seen"] == 2
        except BaseException as error:  # noqa: BLE001 - preserve available failure metrics
            if failure is None:
                failure = error
        finally:
            parent.close()
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "passed" if failure is None else "failed",
                    "failure": None
                    if failure is None
                    else {"stage": stage, "type": type(failure).__name__},
                    "mode": "offline_fixture",
                    "pdf_fixture": "whitespace_content_stream"
                    if stress_whitespace
                    else "raw_rgb_image_xobject",
                    "no_provider_network": True,
                    "provider_transport": "real_send_family_request_with_fixed_fixture_child",
                    "process_start_method": "spawn",
                    "client_excluded_from_memory": True,
                    "client_process": {"pid": os.getpid(), "cgroup": client_cgroup},
                    "sampler_pid": os.getpid(),
                    "broker_process": broker_metadata,
                    "broker_error_type": broker_error_type,
                    "serialize_work": True,
                    "google_pdf_count_tokens": True,
                    "pdf_bytes": input_bytes,
                    "pdf_sha256": pdf_sha256,
                    "http_request_bytes": http_bytes,
                    "limits_bytes": {"media": PDF_BYTES, "request": 16 * MIB, "result": 4 * MIB},
                    "normal_max_output_tokens": 8192,
                    "smoke_max_output_tokens": 2048,
                    "normal_local_caps": {
                        "requests_minute": 3,
                        "requests_day": 100,
                        "input_tokens_minute": 131328,
                    },
                    "shared_concurrency": 1,
                    "counted_tokens": (result.get("token_preflight") or {}).get("counted_tokens"),
                    "estimated_input_tokens": result.get("estimated_input_tokens"),
                    "queue_state": metadata.get("state"),
                    "queue_error_code": metadata.get("error_code"),
                    "client": client_metrics,
                    "sample_interval_ms": int(SAMPLE_SECONDS * 1000),
                    "metadata_poll_interval_seconds": POLL_SECONDS,
                    "sampled_broker_tree": memory,
                    **transport,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "python": sys.version.split()[0],
                    "limitations": [
                        "sampled_not_cgroup_peak",
                        "offline_not_ASUS",
                        "fixture_child_parses_full_body_JSON_and_holds_50ms",
                    ],
                },
                sort_keys=True,
            )
        )
    return 0 if failure is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
