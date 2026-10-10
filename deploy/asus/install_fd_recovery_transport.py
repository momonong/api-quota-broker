"""Sealed one-shot transport for the reviewed same-ID maintenance repair."""

import hashlib
import importlib.util
import json
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
def validate_probe(obj):
    need(type(obj) is dict and set(obj)=={"state","stage","request_id","service_changes","database_writes","provider_posts","rollback_verified"})
    need(obj["state"] in ("passed","blocked","unknown"))
    need(obj["stage"] in ("unclassified","backup","install","installed","rollback_unverified"))
    need(obj["request_id"]=="e1028cade84c4fc09fa0cbba85d19be0")
    need(type(obj["provider_posts"]) is int and obj["provider_posts"]==0)
    need(type(obj["rollback_verified"]) is bool)
    need(obj["service_changes"]==0 and obj["database_writes"]==0)
    return obj
"""

exec(VALIDATOR, {"need": t.need}, scope := {})  # noqa: S102 - fixed reviewed literal
validate_probe = scope["validate_probe"]


def argv(plan):
    source = (EVIDENCE / "fd-recovery-r1/root-command.py").read_bytes()
    t.need(hashlib.sha256(source).hexdigest() == plan["root_command_sha256"])
    root = ["/usr/bin/python3.14", "-I", "-B", "-S", "-c", source.decode()]
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
    rc,raw,count=capture(child,32768,160,supply)
    stage="result_framing"
    if rc!=0: raise TransportFailure(stage,"child_exit",rc=rc,possible_dispatch=True)
    need(count in (0,1))
    emit({"kind":"repair_result","state":"received","authentication_submissions":count,"probe":validate_probe(json.loads(raw))})
except BaseException as error:
    emit({"kind":"repair_result",**failure(error,stage,possible)})
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
        plan = json.loads((EVIDENCE / "fd-recovery-r1/plan.json").read_text())
        t.need(plan["runner_sha256"] == sys.argv[2])
        args = argv(plan)
        t.guard()
        t.check_secret_name()
        t.exclusive_json(
            EVIDENCE / "fd-recovery-r1.local-claim.json", {"fixed_repair": True, "plan": plan}
        )
        claimed = True
        out.update(
            t.exchange(
                args,
                seconds=170,
                kind="repair_result",
                field="probe",
                projection=validate_probe,
            )
        )
    except BaseException as error:  # noqa: BLE001 - closed failure only
        out.update(t.failure(error, "local_guard", claimed))
    finally:
        if claimed:
            t.exclusive_json(EVIDENCE / "fd-recovery-r1.local-result.json", out)
        t.emit(out)
    return 0 if out.get("state") == "received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
