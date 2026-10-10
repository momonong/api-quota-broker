"""Fixed read-only root receipt query after the r1 transport's unknown result."""

import hashlib
import importlib.util
import shlex
import sys
from pathlib import Path

CANDIDATE_SHA = "4527a42c01b4d7c05300a75d8b54f1b0574ebe994214e1ed20f50e749f393d3a"
SPEC = importlib.util.spec_from_file_location(
    "aqb_transport_v2", Path(__file__).with_name("bootstrap_transport_v2.py")
)
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)

# This literal is the complete root program. It imports only stdlib and does
# not load installed/candidate project code or execute any other command.
READER = r"""
import os,stat,json,ctypes,resource
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
libc=ctypes.CDLL(None)
assert libc.prctl(4,0,0,0,0)==0
assert os.geteuid()==0 and os.uname().nodename=='asus-ubuntu2604-server'
base=Path('/var/lib/api-quota-broker-ops')
items={
 'claim':base/'maintenance-bootstrap-r1.claim.json',
 'result':base/'maintenance-bootstrap-r1.result.json',
 'backup':base/'maintenance-bootstrap-r1.backup',
 'maintenance':base/'maintenance',
}
output={'kind':'readonly_snapshot','presence':{},'receipt':None}
for name,path in items.items():
 try:meta=path.lstat()
 except FileNotFoundError:
  output['presence'][name]='absent';continue
 assert meta.st_uid==0 and not stat.S_ISLNK(meta.st_mode)
 if name in ('backup','maintenance'):
  assert stat.S_ISDIR(meta.st_mode) and stat.S_IMODE(meta.st_mode)==0o700
 else:
  assert stat.S_ISREG(meta.st_mode) and meta.st_nlink==1 and stat.S_IMODE(meta.st_mode)==0o600
 output['presence'][name]='present'
 if name=='result':
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  with os.fdopen(fd,'rb') as stream:raw=stream.read(16385)
  assert len(raw)<=16384
  obj=json.loads(raw)
  assert obj.get('status') in ('passed','blocked')
  assert obj.get('stage') in ('preflight','install','verify','complete','bootstrap_gate')
  output['receipt']={'status':obj['status'],'stage':obj['stage']}
  for key in ('rollback_verified','maintenance_installed','normal_v1_deployed'):
   if key in obj:
    assert type(obj[key]) is bool
    output['receipt'][key]=obj[key]
print(json.dumps(output),flush=True)
"""

VALIDATOR = """
def validate_snapshot(obj):
    need(type(obj) is dict and set(obj) == {"kind", "presence", "receipt"})
    need(obj["kind"] == "readonly_snapshot")
    need(type(obj["presence"]) is dict)
    need(set(obj["presence"]) == {"claim", "result", "backup", "maintenance"})
    need(all(v in ("absent", "present") for v in obj["presence"].values()))
    receipt = obj["receipt"]
    if receipt is not None:
        need(type(receipt) is dict)
        need(set(receipt) <= {"status", "stage", "rollback_verified", "maintenance_installed", "normal_v1_deployed"})
        need(receipt.get("status") in ("passed", "blocked"))
        need(receipt.get("stage") in ("preflight", "install", "verify", "complete", "bootstrap_gate"))
        need(all(type(v) is bool for k,v in receipt.items() if k not in ("status", "stage")))
    return obj
"""
exec(VALIDATOR, {"need": t.need}, validator_scope := {})  # noqa: S102 - fixed literal
validate_snapshot = validator_scope["validate_snapshot"]


def argv():
    args = t.remote_argv(CANDIDATE_SHA)
    remote = shlex.split(args[-1])
    source = remote[-1].removesuffix("sys.exit(remote_main())")
    remote[-1] = (
        source
        + VALIDATOR
        + "\nREADER = "
        + repr(READER)
        + r"""
def query_original_receipt():
    raw=bytearray()
    try:
        stage="local_guard";possible=False
        guard()
        stage="host_gate"
        command=target_command()
        command=command[:6]+['/usr/bin/python3.14','-I','-B','-S','-c',READER]
        def supply():
            emit(READY)
            return read_line(sys.stdin.buffer,MAX_SECRET,30,"input_write",False)
        stage="command_dispatch";possible=True
        child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,env=ENV,start_new_session=True)
        rc,raw,count=capture(child,32768,45,supply)
        stage="result_framing"
        if rc!=0: raise TransportFailure(stage,"child_exit",rc=rc,possible_dispatch=True)
        need(count in (0,1))
        emit({'kind':'diagnostic_result','state':'received','authentication_submissions':count,
              'snapshot':validate_snapshot(json.loads(raw))})
    except BaseException as error:
        emit({'kind':'diagnostic_result',**failure(error,stage,possible)})
    finally:
        erase(raw)
query_original_receipt()
"""
    )
    args[-1] = shlex.join(remote)
    return args


def query(fetch=t.fetch_secret):
    return t.exchange(
        argv(), fetch, 55, kind="diagnostic_result", field="snapshot", projection=validate_snapshot
    )


def main():
    out = {"state": "blocked", "automatic_retry": False}
    claimed = False
    root = Path(__file__).resolve().parents[2] / "docs/evidence/asus-maintenance"
    try:
        t.need(len(sys.argv) == 3 and sys.argv[1] == "--expected-sha256")
        t.need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == sys.argv[2])
        t.need(hashlib.sha256(Path(t.__file__).read_bytes()).hexdigest() == CANDIDATE_SHA)
        t.guard()
        t.check_secret_name()
        t.exclusive_json(root / "bootstrap-receipt-diagnostic-r1.claim.json", {"readonly": True})
        claimed = True
        out = {"state": "unknown", "automatic_retry": False}
        out.update(query())
    except BaseException as error:  # noqa: BLE001 - fixed safe diagnostic only
        out.update(t.failure(error, "local_guard", claimed))
    finally:
        if claimed:
            t.exclusive_json(root / "bootstrap-receipt-diagnostic-r1.result.json", out)
        t.emit(out)
    return 0 if out.get("state") == "received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
