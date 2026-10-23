"""Execute actual frontend code with controlled async transport; no browser dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_late_snapshot_cannot_reopen_logged_out_workspace(outcome):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the optional frontend async regression")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
class Element {
  constructor(){ this.hidden=false; this.value=''; this.children=[]; this.handlers={}; }
  append(...items){ this.children.push(...items); }
  replaceChildren(...items){ this.children=items; }
  get firstChild(){return this.children[0];}
  addEventListener(name,callback){this.handlers[name]=callback;}
  setAttribute(){}
}
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let resolveSnapshot, rejectSnapshot;
const response = value=>({ok:true,status:200,json:async()=>value});
const snapshot = new Promise((resolve,reject)=>{resolveSnapshot=resolve;rejectSnapshot=reject;});
const context={document:{getElementById:get,createElement:()=>new Element(),createTextNode:text=>text},
  fetch:async path=>path==='/api/snapshot'?snapshot:response({state:'signed_out'}),
  Intl,Date,crypto:{randomUUID:()=> 'synthetic'},console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
  await get('logout').handlers.click();
  assert.equal(get('workspace').hidden,true);
  if(process.argv[2]==='success') resolveSnapshot(response({source:{kind:'demo',label:'synthetic',as_of:'2026-10-12T00:00:00Z',fetched_at:'2026-10-12T00:00:00Z'},targets:[],usage:[],recent:[]}));
  else rejectSnapshot(new Error('synthetic transport failure'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('workspace').hidden,true);
  assert.equal(get('login-panel').hidden,false);
  assert.equal(get('source').textContent,'已登出。');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    asset = Path(__file__).resolve().parents[1] / "src/quota_broker/dashboard_assets/dashboard.js"
    result = subprocess.run(
        [node, "-e", script, str(asset), outcome],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
