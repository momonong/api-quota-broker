"""Root transaction control, source-matched wheel, and no replay on failures."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_normal_pool_limits import body, configured

from quota_broker.queue import DurableQueue


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


operator = load("deploy/asus/normal_pool_operator.py", "tested_normal_operator")


@pytest.mark.parametrize(
    "state",
    ["queued", "waiting", "running", "unknown", "processing", "scheduled", "completed", "foreign"],
)
def test_worker_activation_refuses_all_existing_work_without_mutation(tmp_path, state):
    gateway, _, _ = configured(tmp_path)
    db = tmp_path / "db.sqlite3"
    tmp_path.chmod(0o700)
    db.chmod(0o600)
    queue = DurableQueue(gateway, b"Q" * 32)
    queue.submit(body("existing-source-job"))
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE queue_jobs SET state=?,next_retry_at='2099-01-01T00:00:00+00:00'", (state,)
        )
    before = db.read_bytes()
    with sqlite3.connect(db) as con, pytest.raises(operator.Blocked, match="existing_queue_work"):
        operator.empty_queue_gate(con)
    assert db.read_bytes() == before


class Fixture:
    def __init__(self, fault=None, short=False):
        self.fault, self.short, self.events = fault, short, []
        self.changed = self.claimed = False
        self.budget = SimpleNamespace(restarts=0)

    def preflight(self):
        self.events.append("preflight")

    def claim(self, record):
        self.claimed = True

    def save(self, record):
        self.saved = json.loads(json.dumps(record))

    def stage(self):
        pass

    def install_client_and_worker(self):
        self.changed = True
        if self.fault == "install":
            raise OSError("public fixture fault")

    def configuration(self):
        return {"targets": []}

    def switch(self, config, original=False):
        self.budget.restarts += 1
        self.events.append("restore_selector" if original else "activate")

    def activation_changed(self):
        return "activate" in self.events

    def verify_client(self):
        self.events.append("client")

    def preservation(self):
        return {"historical_rows_preserved": True}

    def allocate_post(self):
        self.events.append("intent")

    def probe(self, body):
        self.events.append(body["request_key"])
        if self.fault == "unknown":
            raise operator.Blocked("queue_result_unknown")
        return {
            "execution_verified": True,
            "queued_auto": body["request_key"] == operator.QUEUE_KEY,
            "reported_output_tokens": 32 if self.short else 512,
            "long_answer_verified": False,
        }  # No 1200-char hard gate for each model.

    def restart_and_verify(self, record):
        self.budget.restarts += 1

    def verify_ops_pins(self):
        pass

    def remove_client_and_restore_units(self):
        self.events.append("cleanup_client")

    def original_config(self):
        return {"targets": []}

    def stop_broker(self):
        self.events.append("stop_broker")


def test_engineering_vs_long_auto_acceptance_and_budget():
    successful = Fixture()
    result = operator.transaction(successful)
    assert result["status"] == "passed" and result["provider_posts_intended"] == 3
    short = Fixture(short=True)
    assert operator.transaction(short)["status"] == "partial"
    assert short.events.count("intent") == 3
    assert (
        "restore_selector" not in short.events
    )  # Insufficient length isn't a unit/config failure.


def test_failure_cleanup_precedes_selector_restore_and_unknown_never_replayed():
    early = Fixture(fault="install")
    result = operator.transaction(early)
    assert result["original_restored"] and early.budget.restarts == 0
    unknown = Fixture(fault="unknown")
    result = operator.transaction(unknown)
    assert result["status"] == "blocked" and unknown.events.count("intent") == 1
    assert unknown.events.index("cleanup_client") < unknown.events.index("restore_selector")


FAULT_NAMESPACE = r"""
import importlib.util,json,os,shutil,tempfile,tarfile,io,sys,sqlite3
from pathlib import Path
def load(p,n):
 s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);sys.modules[n]=m;s.loader.exec_module(m);return m
repo=Path.cwd();r2=repo/"deploy/asus"
op=load(repo/"deploy/asus/normal_pool_operator.py","n")
original=(repo/"tests/fixtures/asus-history/seven-r2-broker.service").read_bytes()
assert op.hashlib.sha256(original).hexdigest()==op.OLD_UNIT_SHA
root=Path(tempfile.mkdtemp(prefix="aqb-normal-public-fault-"))
try:
 for fault in ("before_client","partial_client","partial_wrapper","unit_sync","enable"):
  test=root/fault;test.mkdir();candidate=test/"release";directory=candidate/"deploy/asus";directory.mkdir(parents=True)
  for name in ("api-quota-broker-client.service","api-quota-broker-v1.service","aqb"):(directory/name).write_bytes((repo/"deploy/asus"/name).read_bytes())
  op.CLIENT_UNIT_PATH=test/"client.service";op.WRAPPER_PATH=test/"aqb";op.BROKER_UNIT_PATH=test/"broker.service"
  op.BROKER_UNIT_PATH.write_bytes(original);op.BROKER_UNIT_PATH.chmod(0o644)
  modules={name:load(r2/source,fault+name.replace('.','_')) for name,source in (("pool_base.py","enable_provider_pool.py"),("pool_base_plan.py","provider_pool_plan.py"),("seven_pool_plan.py","seven_pool_plan.py"),("seven_pool_operator.py","seven_pool_operator.py"))}
  # Existing source matches the reviewed r2 bytes; retain that exact identity gate.
  historical_sha={"pool_base.py":"f4ca903754e44b252948ee4a5ea4a85e63fbb8a4609b3a6bd013964c47fdbb3c","pool_base_plan.py":"aab70f473bf383788107e2210a7711f3035a2dec8d4a73161d6a1e4dca6238d7","seven_pool_plan.py":"1941713a8f1f5fd6b140b7548476ee49d57d9131283d69ec64e266ef59f7443d","seven_pool_operator.py":"ffc24a77f8189c67fa822c53664699c6ef66ae592acfc7f69880946c13b1e12b"}
  assert all(op.hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()==historical_sha[name] for name,module in modules.items())
  modules["build_asus_release.py"]=None
  native=op.make_native(test,{},modules);native.new_release=candidate
  native.installed_client=False;native.installed_paths={};native.client_loaded=False
  native.broker_unit_original=original;native.unit_expected_sha=op.OLD_UNIT_SHA
  events=[]
  def command(argv):
   events.append(argv[1:])
   if argv[1]=="enable" and fault=="enable":raise OSError("public fixture")
   return b""
  native.command=command
  original_open=os.open;original_fdopen=os.fdopen;original_sync=modules["pool_base.py"].sync_directory
  def opened(path,flags,*a,**kw):
   if Path(path)==op.CLIENT_UNIT_PATH and flags&os.O_CREAT and fault=="before_client":raise OSError("public fixture")
   return original_open(path,flags,*a,**kw)
  def fdopen(fd,mode,*a,**kw):
   stream=original_fdopen(fd,mode,*a,**kw)
   name=Path(os.readlink('/proc/self/fd/'+str(fd)))
   partial=(fault=="partial_client" and name==op.CLIENT_UNIT_PATH) or (fault=="partial_wrapper" and name==op.WRAPPER_PATH)
   if not partial or mode!='wb':return stream
   class Broken:
    def __enter__(self):return self
    def __exit__(self,*args):return stream.__exit__(*args)
    def fileno(self):return stream.fileno()
    def write(self,raw):stream.write(raw[:max(1,len(raw)//2)]);stream.flush();raise OSError("public partial fixture")
   return Broken()
  def sync(path):
   if fault=="unit_sync":raise OSError("public fixture")
   return original_sync(path)
  os.open=opened;os.fdopen=fdopen;modules["pool_base.py"].sync_directory=sync
  try:native.install_client_and_worker()
  except OSError:pass
  else:raise AssertionError("fault not exercised")
  finally:os.open=original_open;os.fdopen=original_fdopen;modules["pool_base.py"].sync_directory=original_sync
  native.remove_client_and_restore_units()
  assert op.BROKER_UNIT_PATH.read_bytes()==original
  assert not op.CLIENT_UNIT_PATH.exists() and not op.WRAPPER_PATH.exists()
  assert not any(e[0]=='restart' for e in events)
 # Genuine sealed Ops.connect with actual namespace UID995/GID982 metadata.
 db=root/"readonly.sqlite3"
 with sqlite3.connect(db) as con:
  con.executescript("CREATE TABLE queue_jobs(request_key TEXT,state TEXT,next_retry_at TEXT,deadline TEXT,lease_owner TEXT,lease_token TEXT,lease_until TEXT,execution_until TEXT,execution_key TEXT,run_started INTEGER,payload BLOB);CREATE TABLE queue_attempts(request_key TEXT);CREATE TABLE queue_settings(id INTEGER,verifier BLOB);INSERT INTO queue_settings VALUES(1,x'5055424c4943');")
 db.chmod(0o600);os.chown(db,995,982)
 base=modules["pool_base.py"];base.DB=db
 reader=base.Ops(root,{},None,None)
 con=reader.connect()
 try:
  con.execute('PRAGMA query_only=ON');assert op.empty_queue_gate(con)['jobs']==0
  try:con.execute('CREATE TABLE forbidden(x)')
  except sqlite3.OperationalError:pass
  else:raise AssertionError('readonly connection allowed write')
 finally:con.close()
 print(json.dumps({"status":"passed","fault_cases":5,"actual_namespace_inode_ownership":True,"genuine_sealed_connect_readonly":True,"no_unnecessary_broker_restart":True,"systemd_calls":0,"provider_calls":0}))
finally:shutil.rmtree(root)
"""


def test_native_partial_writes_use_owned_inodes_and_preserve_existing_unit():
    if sys.platform != "linux":
        pytest.skip("Linux namespace acceptance")
    result = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-auto",
            "--map-root-user",
            sys.executable,
            "-c",
            FAULT_NAMESPACE,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["fault_cases"] == 5
