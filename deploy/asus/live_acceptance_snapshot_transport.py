"""Sealed one-shot transport for the fixed read-only syscall reduction."""

import hashlib
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
EVIDENCE = BASE / "docs/evidence/asus-maintenance"
TRANSPORT_SHA = "4527a42c01b4d7c05300a75d8b54f1b0574ebe994214e1ed20f50e749f393d3a"
spec = importlib.util.spec_from_file_location(
    "transport", Path(__file__).with_name("bootstrap_transport_v2.py")
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

VALIDATOR = """
import re
def validate_probe(obj):
    need(type(obj) is dict and set(obj)=={"queue","tasks","attempts","reservations","counts","provider_posts","database_writes","query_only"})
    need(obj["provider_posts"]==0 and obj["database_writes"]==0 and obj["query_only"] is True)
    keys={"asus-normal-v1-2026-10-08-r1-"+s for s in ("queued-auto-a1","google-long-a1","cloudflare-long-a1")}
    states={"queued","waiting","running","completed","completed_usage_unknown","failed","unknown","cancelled","expired","quota_rejected","quota_exhausted","rejected","preparing","dispatched","reserved","settled","released"}
    allowed={"request_key","state","attempt_count","max_attempts","run_started","execution_key","has_lease","has_execution_deadline","has_result","provider","http_status","dispatched","completed","attempt_no"}
    for group in ("queue","tasks","attempts","reservations"):
        need(type(obj[group]) is list and len(obj[group])<=12)
        for row in obj[group]:
            need(type(row) is dict and set(row)<=allowed)
            need(row["request_key"] in keys or bool(re.fullmatch("q-[a-f0-9]{32}",row["request_key"])))
            need(row["state"] in states)
            for k,v in row.items():
                if k=="execution_key":need(v is None or bool(re.fullmatch("q-[a-f0-9]{32}",v)))
                elif k=="provider":need(v in {None,"nvidia","google","groq","mistral","cloudflare","openrouter","ocrspace"})
                elif k not in {"request_key","state"}:need(v is None or type(v) is int and 0<=v<=1000000)
    need(type(obj["counts"]) is dict and set(obj["counts"])=={"gateway_tasks","gateway_attempts","reservations","charges","execution_completion","queue_jobs","queue_attempts"})
    need(all(type(v) is int and 0<=v<=1000000 for v in obj["counts"].values()))
    return obj
"""

exec(VALIDATOR, {"need": t.need, "re": re}, scope := {})  # noqa: S102 - fixed reviewed literal
validate_probe = scope["validate_probe"]


def argv(plan):
    source = Path(__file__).with_name("live_acceptance_snapshot.py").read_bytes()
    t.need(hashlib.sha256(source).hexdigest() == plan["probe_sha256"])
    t.need(plan["unit"] == "api-quota-broker-live-snapshot-r2.service")
    root = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit",
        plan["unit"],
    ]
    for prop in plan["properties"]:
        root += ["-p", prop]
    root += ["/usr/bin/python3.14", "-I", "-B", "-S", "-c", source.decode()]
    args = t.remote_argv(TRANSPORT_SHA)
    remote = shlex.split(args[-1])
    remote[-1] = (
        remote[-1].removesuffix("sys.exit(remote_main())")
        + VALIDATOR
        + "\nROOT_ARGV="
        + repr(root)
        + r"""
raw=bytearray()
stage="local_guard";possible=False
try:
    guard()
    stage="host_gate"
    command=target_command()[:6]+ROOT_ARGV
    def supply():
        emit(READY)
        return read_line(sys.stdin.buffer,MAX_SECRET,30,"input_write",False)
    stage="command_dispatch";possible=True
    child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,start_new_session=True)
    rc,raw,count=capture(child,32768,85,supply)
    stage="result_framing"
    if rc!=0: raise TransportFailure(stage,"child_exit",rc=rc,possible_dispatch=True)
    need(count in (0,1))
    emit({"kind":"stage_probe_result","state":"received","authentication_submissions":count,"probe":validate_probe(json.loads(raw))})
except BaseException as error:
    emit({"kind":"stage_probe_result",**failure(error,stage,possible)})
finally:
    erase(raw)
"""
    )
    args[-1] = shlex.join(remote)
    return args


def main():
    claimed = False
    out = {"state": "blocked", "automatic_retry": False}
    try:
        t.need(len(sys.argv) == 3 and sys.argv[1] == "--expected-sha256")
        t.need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == sys.argv[2])
        t.need(hashlib.sha256(Path(t.__file__).read_bytes()).hexdigest() == TRANSPORT_SHA)
        plan = json.loads((EVIDENCE / "live-snapshot-plan-r2.json").read_text())
        t.need(plan["runner_sha256"] == sys.argv[2])
        args = argv(plan)
        t.guard()
        t.check_secret_name()
        t.exclusive_json(EVIDENCE / "live-snapshot-r2.claim.json", {"readonly": True, "plan": plan})
        claimed = True
        out.update(
            t.exchange(
                args,
                seconds=95,
                kind="stage_probe_result",
                field="probe",
                projection=validate_probe,
            )
        )
    except BaseException as error:  # noqa: BLE001 - closed failure only
        out.update(t.failure(error, "local_guard", claimed))
    finally:
        if claimed:
            t.exclusive_json(EVIDENCE / "live-snapshot-r2.result.json", out)
        t.emit(out)
    return 0 if out.get("state") == "received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
