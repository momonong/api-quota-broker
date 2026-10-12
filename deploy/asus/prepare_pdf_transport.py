"""One-shot private sudo transport for the fixed PDF prerequisite helper.

Reuses the audited Doppler/prompt/pipe machinery. Values never enter argv,
environment, files or returned diagnostics. Unknown receipts cannot be retried.
"""

import hashlib
import importlib.util
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/pdf-integration-2026-10-12"
TRANSPORT_SHA = "4527a42c01b4d7c05300a75d8b54f1b0574ebe994214e1ed20f50e749f393d3a"
spec = importlib.util.spec_from_file_location(
    "transport", Path(__file__).with_name("bootstrap_transport_v2.py")
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def projection(obj):
    t.need(isinstance(obj, dict) and obj.get("automatic_retry") is False)
    if obj.get("state") == "restored":
        t.need(set(obj) == {"state", "automatic_retry"})
    else:
        t.need(
            set(obj)
            == {
                "state",
                "service_restarts",
                "provider_posts",
                "database_writes",
                "files",
                "backup",
                "automatic_retry",
            }
        )
        t.need(
            obj["state"] == "passed"
            and obj["service_restarts"] == obj["provider_posts"] == obj["database_writes"] == 0
        )
        t.need(obj["backup"] == "/var/backups/api-quota-broker/pdf-integration-20261012-r1")
        t.need(set(obj["files"]) == {"manifest.json", "broker-v1.service", "release_verifier.py"})
        t.need(
            all(
                isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
                for v in obj["files"].values()
            )
        )
    return obj


def remote(source, *, restore=False):
    root = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=aqb-pdf-prerequisite-r1",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "LimitCORE=0",
        "-p",
        "PrivateNetwork=yes",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=yes",
        "-p",
        "RuntimeMaxSec=90",
        "-p",
        "UMask=0077",
        "-p",
        "ReadWritePaths=/usr/local/lib/api-quota-broker-ops /var/backups/api-quota-broker",
        "/usr/bin/python3.14",
        "-I",
        "-B",
        "-S",
        "-c",
        source.decode(),
    ]
    if restore:
        root[5] = "--unit=aqb-pdf-prerequisite-restore-r1"
        root.append("--restore")
    args = t.remote_argv(TRANSPORT_SHA)
    command = shlex.split(args[-1])
    command[-1] = (
        command[-1].removesuffix("sys.exit(remote_main())")
        + "\nROOT_ARGV="
        + repr(root)
        + "\nREADY="
        + repr(t.READY)
        + r"""
raw=bytearray()
stage="local_guard";possible=False
try:
    guard()
    stage="host_gate"
    need(os.getuid()==os.geteuid()==1000 and pwd.getpwuid(1000).pw_name=="morris" and os.uname().nodename==HOST)
    command=["/usr/bin/sudo","-S","-k","-p",PROMPT_FORMAT,"--",*ROOT_ARGV]
    def supply():
        emit(READY)
        return read_line(sys.stdin.buffer,MAX_SECRET,30,"input_write",False)
    stage="command_dispatch";possible=True
    child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,start_new_session=True)
    rc,raw,count=capture(child,32768,100,supply)
    stage="result_framing"
    if rc!=0: raise TransportFailure(stage,"child_exit",rc=rc,possible_dispatch=True)
    need(count in (0,1))
    emit({"kind":"pdf_preparation","state":"received","authentication_submissions":count,"preparation":json.loads(raw)})
except BaseException as error:
    emit({"kind":"pdf_preparation",**failure(error,stage,possible)})
finally:
    erase(raw)
"""
    )
    args[-1] = shlex.join(command)
    return args


def main():
    claimed = False
    restore = sys.argv[-1:] == ["--restore"]
    receipt = "restore-r1" if restore else "prepare-r1"
    result = {"state": "blocked", "automatic_retry": False}
    try:
        t.need(
            len(sys.argv) == (6 if restore else 5)
            and sys.argv[1] == "--expected-sha256"
            and sys.argv[3] == "--root-sha256"
        )
        t.need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == sys.argv[2])
        t.need(hashlib.sha256(Path(t.__file__).read_bytes()).hexdigest() == TRANSPORT_SHA)
        source = Path(__file__).with_name("prepare_pdf_maintenance.py").read_bytes()
        t.need(hashlib.sha256(source).hexdigest() == sys.argv[4])
        t.READY = {
            "kind": "ready",
            "host": t.HOST,
            "principal": "morris",
            "operation": "pdf-prerequisite-restore-r1" if restore else "pdf-prerequisite-r1",
            "root_sha256": sys.argv[4],
        }
        args = remote(source, restore=restore)
        t.guard()
        t.check_secret_name()
        EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
        t.exclusive_json(
            EVIDENCE / (receipt + ".claim.json"),
            {"root_sha256": sys.argv[4], "transport_sha256": sys.argv[2]},
        )
        claimed = True
        result.update(
            t.exchange(
                args,
                seconds=115,
                kind="pdf_preparation",
                field="preparation",
                projection=projection,
            )
        )
    except BaseException as error:  # noqa: BLE001 - only bounded non-secret error categories
        result.update(t.failure(error, "local_guard", claimed))
    finally:
        if claimed:
            t.exclusive_json(EVIDENCE / (receipt + ".result.json"), result)
        t.emit(result)
    return (
        0
        if result.get("state") == "received"
        and result.get("preparation", {}).get("state") == ("restored" if restore else "passed")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
