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
  fetch:async path=>path==='./api/snapshot'?snapshot:response({state:'signed_out'}),
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


UI_HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),calls=[];
class Element {
 constructor(tag='div'){this.tagName=tag;this.hidden=false;this.value='';this.children=[];this.handlers={};this.attrs={};this._text='';}
 append(...xs){this.children.push(...xs);}
 replaceChildren(...xs){this.children=xs;this._text='';}
 get firstChild(){return this.children[0];}
 addEventListener(name,callback){this.handlers[name]=callback;}
 setAttribute(k,v){this.attrs[k]=v;}
 focus(){this.focused=true;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join(' ');}
}
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const response=value=>({ok:true,status:200,json:async()=>value});
const stamp='2026-11-01T00:00:00Z';
const target={provider:'synthetic',model:'text-fixture',enabled:true,state:'ready',capabilities:['text_generation','translation'],features:['json_output'],input_modalities:['text'],output_modalities:['text'],free_eligible:true,billing_enabled:false,billing_state:'disabled',credentials:{state:'present'},reasons:[],local_caps:[{metric:'requests',window:'day',used:0,limit:20}],observations:[{metric:'requests',window:'day',remaining:10,effective_remaining:10,current:true,as_of:stamp,valid_until:stamp}],provider_limits:[]};
const unconfigured={...target,model:'vision-fixture',model_scope:'registered_unconfigured',input_modalities:['text','image'],output_modalities:['text'],configured_target_ids:[]};
const snapshot={source:{kind:'demo',label:'合成測試',as_of:stamp,fetched_at:stamp},targets:[target],models:[unconfigured,{...target,model_scope:'configured_gateway_target'}],model_inventory:{state:'available'},accounts:[],usage:[],recent:[{provider:'synthetic',model:'text-fixture',state:'completed',created_at:stamp}]};
const report={source:{state:'available',label:'合成完整帳本',as_of:stamp},filters:{},rows:[{provider:'synthetic',model:'text-fixture',task_type:'unknown',requests:12,reported_input_tokens:100,reported_output_tokens:40,reported_total_tokens:140,reported_neurons:null,outcome_unknown_count:1}]};
get('task-kind').value='text_generation';get('input-modality').value='text';get('output-modality').value='text';get('output').value='128';get('usage-period').value='all';
let fetchHandler=path=>response(path==='./api/snapshot'?structuredClone(snapshot):structuredClone(report));
const context={document:{getElementById:get,createElement:tag=>new Element(tag),createTextNode:text=>String(text)},fetch:async(path,options)=>{calls.push({path,options});return fetchHandler(path,options);},Intl,Date,crypto:{randomUUID:()=> 'synthetic'},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
"""


def run_ui_script(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for frontend behavior regression")
    asset = Path(__file__).resolve().parents[1] / "src/quota_broker/dashboard_assets/dashboard.js"
    result = subprocess.run(
        [node, "-e", UI_HARNESS + script, str(asset)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_model_matching_sends_requirements_and_never_fabricates_media_task():
    run_ui_script(r"""
(async()=>{
 await tick();
 get('match-task').value='image_generation';get('match-input').value='text';get('match-output').value='image';
 get('match-text').value='This must not be sent as a text generation task';
 fetchHandler=()=>response({source:snapshot.source,decision:'ask_user',matched_models:1,candidates:[
   {provider:'synthetic',model:'image-fixture',safe:false,reasons:['task_cost_not_checked'],ranking:{},remaining:null}],selected_target:null});
 await get('match-form').handlers.submit({preventDefault(){},submitter:get('match-submit')});
 const sent=JSON.parse(calls.at(-1).options.body);
 assert.equal(sent.mode,'match');assert.equal(sent.task,'image_generation');assert.equal(sent.route_task,undefined);
 assert.match(get('match-result').textContent,/image-fixture/);assert.match(get('match-result').textContent,/尚未核對/);
 get('match-form').handlers.input();assert.doesNotMatch(get('match-result').textContent,/image-fixture/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_match_paging_zero_unknown_and_refresh_invalidate_late_response():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data={source:snapshot.source,decision:'ask_user',matched_models:21,selected_target:null,candidates:Array.from({length:21},(_,i)=>({provider:'synthetic',model:'model-'+i,safe:false,reasons:[],ranking:{},remaining:i===0?[{metric:'requests',window:'day',current:true,effective_remaining:0}]:null}))};
 fetchHandler=()=>response(data);
 await get('match-form').handlers.submit({preventDefault(){},submitter:get('match-submit')});
 assert.match(get('match-result').textContent,/日視窗：0/);assert.match(get('match-result').textContent,/本次可用額度：未知/);
 assert.doesNotMatch(get('match-result').textContent,/model-20/);
 const pager=get('match-result').children.find(e=>e.className==='catalog-pagination');pager.children[2].handlers.click();
 assert.match(get('match-result').textContent,/model-20/);assert.doesNotMatch(get('match-result').textContent,/model-19/);
 let release;fetchHandler=()=>new Promise(resolve=>{release=resolve;});
 const pending=get('match-form').handlers.submit({preventDefault(){},submitter:get('match-submit')});
 context.render(structuredClone(snapshot));
 release(response(data));await pending;
 assert.doesNotMatch(get('match-result').textContent,/model-20/);
 assert.match(get('match-result').textContent,/重新配對/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_tabs_model_filters_full_usage_and_media_advice_boundary():
    run_ui_script(r"""
(async()=>{
 await tick();
 assert.equal(get('workspace').hidden,false);
 assert.equal(get('demo-notice').hidden,false);
 assert.equal(get('view-models').hidden,false);
 assert.match(get('providers').textContent,/text-fixture/);
 assert.match(get('providers').textContent,/vision-fixture/);
 assert.doesNotMatch(get('providers').children[0].children[0].textContent,/vision-fixture/,'unconfigured models are omitted from the configured model summary');
 assert.match(get('providers').textContent,/未配置執行路由/);
 get('model-input').value='image';get('model-input').handlers.change();
 assert.match(get('providers').textContent,/vision-fixture/);
 assert.doesNotMatch(get('providers').textContent,/text-fixture/);
 assert.doesNotMatch(get('providers').textContent,/10 requests/,'unconfigured model must not inherit quota');
 get('tab-models').handlers.keydown({key:'ArrowRight',preventDefault(){}});await tick();
 assert.equal(get('view-models').hidden,true);assert.equal(get('view-usage').hidden,false);
 assert.equal(get('tab-usage').attrs['aria-selected'],'true');assert.equal(get('tab-usage').focused,true);
 assert.match(get('usage-totals').textContent,/12/,'full report count, not one recent item');
 assert.match(get('usage-totals').textContent,/neurons · 含未回報值/);
 get('usage-period').value='custom';get('usage-from').value='2026-10-20';get('usage-to').value='2026-10-31';get('usage-provider').value='synthetic';get('usage-task').value='unknown';get('usage-model').value='text-fixture';
 await get('usage-form').handlers.submit({preventDefault(){}});await tick();
 const query=calls.at(-1).path;
 assert.match(query,/^\.\/api\/usage\?/);assert.match(query,/provider=synthetic/);assert.match(query,/model=text-fixture/);assert.match(query,/task=unknown/);assert.match(query,/from=/);assert.match(query,/to=/);
 get('tab-advice').handlers.click();assert.equal(get('view-advice').hidden,false);
 get('input-modality').value='image';get('advice-form').handlers.change();assert.equal(get('advice-submit').disabled,true);
 assert.match(get('advice-compatibility').textContent,/不是可執行推薦/);
 const count=calls.length;await get('advice-form').handlers.submit({preventDefault(){},submitter:get('advice-submit')});assert.equal(calls.length,count,'media form must not send fake text payload');
 get('input-modality').value='text';get('output-modality').value='json';get('task-input').value='合成內容';get('advice-form').handlers.change();
 fetchHandler=(path,options)=>{const body=JSON.parse(options.body);assert.equal(body.requirements.features[0],'json_output');assert.equal(body.options.response_format,'json_object');assert.equal(body.input.messages[0].content,'合成內容');return response({decision:'recommend',selected_target:{provider:'synthetic',model:'text-fixture',target_id:'x'},candidates:[{provider:'synthetic',model:'text-fixture',target_id:'x',reasons:[],ranking:{observed_headroom:0.6,value_loss_basis:null}}],source:snapshot.source});};
 await get('advice-form').handlers.submit({preventDefault(){},submitter:get('advice-submit')});
 assert.match(get('advice-result').textContent,/可建議此免費路由/);assert.match(get('advice-result').textContent,/沒有送出任務/);assert.match(get('advice-result').textContent,/60%/);
 assert.ok(calls.every(c=>c.options.credentials==='same-origin'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_reviewed_fixture_disabled_is_not_presented_as_live_service_disabled():
    run_ui_script(r"""
(async()=>{
 await tick();
 snapshot.source.kind='reviewed';snapshot.targets[0].enabled=false;
 snapshot.targets[0].runtime_configuration={state:'available',enabled:true,observed_at:stamp};
 await get('refresh').handlers.click();
 assert.match(get('providers').textContent,/Broker 已啟用/);
 assert.match(get('providers').textContent,/份資格核對項目/);
 assert.doesNotMatch(get('providers').textContent,/核對快照停用/);
 snapshot.targets[0].runtime_configuration={state:'unknown',enabled:null,observed_at:null};
 await get('refresh').handlers.click();
 assert.match(get('providers').textContent,/Broker 啟用狀態未知/);
 assert.doesNotMatch(get('providers').textContent,/Broker 已啟用/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_provider_inventory_filters_the_same_task_variant_and_never_enters_advice():
    run_ui_script(r"""
(async()=>{
 await tick();
 const variant=(task,inputs,outputs)=>({task,inputs,outputs});
 snapshot.provider_inventory={audited_on:'2026-11-06',providers:[{id:'other',name:'Other Provider',apis:[{name:'Speech API',interface:'/v1/speech',tasks:['tts'],source:'https://example.invalid/speech'}],catalogs:[{label:'Project list',basis:'key_catalog',checked_at:stamp,source:'https://example.invalid/models',coverage:'Fixture',models:[
  {id:'multi-model',status:'listed',variants:[variant('text_generation',['image'],['text']),variant('tts',['text'],['audio'])]},
  {id:'old-model',status:'retired',variants:[variant('tts',['text'],['audio'])]}
 ]}]}]};
 context.render(snapshot);
 get('model-view-catalog').handlers.click();
 assert.equal(get('provider-catalog').hidden,false);assert.equal(get('broker-models').hidden,true);
 assert.match(get('catalog-models').textContent,/multi-model/);
 assert.doesNotMatch(get('catalog-models').textContent,/old-model/);
 assert.match(get('catalog-models').textContent,/執行權限未驗證/);
 assert.doesNotMatch(get('provider').textContent,/other/);
 get('catalog-input').value='image';get('catalog-output').value='audio';get('catalog-output').handlers.change();
 assert.doesNotMatch(get('catalog-models').textContent,/multi-model/);
 get('catalog-input').value='text';get('catalog-input').handlers.change();
 assert.match(get('catalog-models').textContent,/multi-model/);
 get('catalog-status').value='retired';get('catalog-status').handlers.change();
 assert.match(get('catalog-models').textContent,/old-model/);
 assert.doesNotMatch(get('catalog-models').textContent,/multi-model/);
 assert.equal(calls.length,1,'catalog uses authenticated snapshot and no new requests');
 await get('logout').handlers.click();
 assert.equal(get('catalog-models').textContent,'');assert.equal(get('provider-catalog').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


@pytest.mark.parametrize("kind", ["usage", "advice", "match"])
def test_late_usage_or_advice_cannot_restore_private_dom_after_logout(kind):
    run_ui_script(
        r"""
(async()=>{
 await tick();
 let release;
 fetchHandler=path=>path==='./api/logout'?response({state:'signed_out'}):new Promise(resolve=>{release=resolve;});
 const kind='"""
        + kind
        + r"""';
 if(kind==='usage')get('tab-usage').handlers.click();
 else if(kind==='match'){get('match-text').value='private synthetic text';get('match-form').handlers.submit({preventDefault(){},submitter:get('match-submit')});}
 else{get('task-input').value='private synthetic text';get('advice-form').handlers.submit({preventDefault(){},submitter:get('advice-submit')});}
 await tick();await get('logout').handlers.click();
 release(response(kind==='usage'?report:{decision:'recommend',selected_target:{provider:'private',model:'private-model'},candidates:[],source:snapshot.source}));
 await tick();
 assert.equal(get('workspace').hidden,true);assert.equal(get('login-panel').hidden,false);
 for(const id of ['providers','accounts','usage','usage-totals','recent','time-overview','pool-status'])assert.equal(get(id).textContent,'',id+' cleared');
 assert.doesNotMatch(get('advice-result').textContent,/private/);assert.equal(get('task-input').value,'');assert.equal(get('model').value,'');
 assert.doesNotMatch(get('match-result').textContent,/private/);assert.equal(get('match-text').value,'');
 assert.equal(get('source').textContent,'已登出。');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )


def test_unknown_stale_zero_and_task_metadata_unavailable_remain_distinct():
    run_ui_script(r"""
(async()=>{
 await tick();
 const stale=structuredClone(snapshot);stale.targets[0].observations[0].current=false;stale.targets[0].observations[0].effective_remaining=null;context.render(stale);
 assert.match(get('providers').textContent,/歷史觀測 10/);assert.match(get('providers').textContent,/餘額證據過時/);
 const zero=structuredClone(snapshot);zero.targets[0].observations[0].effective_remaining=0;context.render(zero);assert.match(get('providers').textContent,/0 requests/);
 const unknown=structuredClone(snapshot);unknown.targets[0].observations=[];context.render(unknown);assert.match(get('providers').textContent,/剩餘未知/);
 context.renderUsage({...report,rows:null,source:{state:'unavailable',label:'任務類別未記錄'}});
 assert.match(get('usage').textContent,/不顯示為零/);assert.equal(get('usage-totals').textContent,'');
 context.renderUsage({...report,rows:[]});assert.match(get('usage').textContent,/不代表整個供應商帳戶用量為零/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_unavailable_inventory_keeps_configured_models_and_media_units_separate():
    run_ui_script(r"""
(async()=>{
 await tick();
 context.render({...snapshot,models:[],model_inventory:{state:'unavailable'}});
 assert.match(get('providers').textContent,/text-fixture/);
 assert.match(get('model-count').textContent,/完整登錄清單不可用/);
 assert.doesNotMatch(get('providers').textContent,/vision-fixture/);
 context.renderUsage({...report,rows:[{...report.rows[0],reported_audio_seconds:2.5,reported_images:2,reported_pages:3,reported_conversions:0}]});
 assert.match(get('usage-totals').textContent,/音訊 2.5 秒/);
 assert.match(get('usage-totals').textContent,/圖片 2 張圖片/);
 assert.match(get('usage-totals').textContent,/文件 3 頁/);
 assert.match(get('usage-totals').textContent,/轉換 0 次轉換/);
 assert.match(get('usage-totals').textContent,/總 tokens 140 total tokens/);
 context.renderUsage({...report,rows:[{...report.rows[0],reported_audio_seconds:null,reported_images:0,reported_pages:null,reported_conversions:null}]});
 assert.match(get('usage-totals').textContent,/音訊 — 秒 · 含未回報值/);
 assert.match(get('usage-totals').textContent,/圖片 0 張圖片/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_new_task_metadata_filters_and_legacy_unknown_are_explained():
    run_ui_script(r"""
(async()=>{
 await tick();
 const values=get('usage-task').children.map(option=>option.value);
 for(const type of ['unknown','text_generation','translation','ocr','vision','code_completion','audio_transcription','audio_translation','tts','image_generation','embedding','rerank','classification','moderation'])assert.ok(values.includes(type),type);
 context.renderUsage({...report,task_metadata:{state:'available_for_new_records'},rows:[{...report.rows[0],task_type:'translation'}]});
 assert.match(get('usage-filter-note').textContent,/新紀錄依實際任務能力分類/);
 assert.match(get('usage').textContent,/翻譯/);
 context.renderUsage({...report,task_metadata:{state:'available_for_new_records'},rows:[]});
 assert.match(get('usage').textContent,/沒有本機生成嘗試/);
 context.renderUsage({...report,task_metadata:{state:'unavailable'},rows:null});
 assert.match(get('usage-filter-note').textContent,/未知不視為零/);
 assert.match(get('usage').textContent,/不顯示為零/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_partial_reported_tokens_are_not_presented_as_complete_usage():
    run_ui_script(r"""
(async()=>{
 await tick();
 context.renderUsage({...report,rows:[{...report.rows[0],reported_input_tokens:123,input_unknown_count:1,output_unknown_count:0,neurons_unknown_count:0}]});
 assert.match(get('usage-totals').textContent,/已回報輸入 — input tokens/);
 assert.match(get('usage-totals').textContent,/部分.*完整性/);
 assert.match(get('usage').textContent,/123（部分）/);
 assert.match(get('usage-totals').textContent,/生成嘗試 12 requests/);
 context.renderUsage({...report,rows:[{...report.rows[0],reported_input_tokens:123}]});
 assert.match(get('usage').textContent,/123（完整性未知）/);
 assert.match(get('usage-totals').textContent,/已回報輸入 — input tokens/);
 context.renderUsage({...report,rows:[{...report.rows[0],reported_input_tokens:123,input_unknown_count:0}]});
 assert.match(get('usage-totals').textContent,/已回報輸入 123 input tokens/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_match_json_output_controls_exact_task_without_second_output_selector():
    run_ui_script(r"""
(async()=>{
 await tick();
 get('match-task').value='text_generation';get('match-input').value='text';get('match-output').value='json';
 get('match-text').value='Return a small JSON object';get('match-limit').value='64';
 fetchHandler=()=>response({source:snapshot.source,decision:'ask_user',matched_models:0,candidates:[],selected_target:null});
 await get('match-form').handlers.submit({preventDefault(){},submitter:get('match-submit')});
 const sent=JSON.parse(calls.at(-1).options.body);
 assert.deepEqual(sent.outputs,['json']);assert.equal(sent.route_task.options.response_format,'json_object');
 assert.ok(sent.route_task.requirements.features.includes('json_output'));
 assert.equal(sent.route_task.max_output_tokens,64);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_capability_shortlist_is_usable_when_free_execution_is_unknown():
    run_ui_script(r"""
(async()=>{
 await tick();
 const candidate={provider:'synthetic',model:'model-choice',capability_verified:true,free_plan_verified:true,
   documentation_url:'https://example.com/model',safe:false,remaining:null,ranking:{},
   execution_status:'reviewed_snapshot',reasons:['disabled','quota_not_verified_for_task']};
 fetchHandler=()=>response({source:snapshot.source,decision:'ask_user',matched_models:1,capability_matches:1,
   candidates:[candidate],shortlist:[candidate],recommended_model:candidate,selected_target:null});
 get('match-task').value='text_generation';get('match-input').value='text';get('match-output').value='text';
 await get('match-form').handlers.submit({preventDefault(){}});
 const text=get('match-result').textContent;
 assert.match(text,/優先考慮.*model-choice/);
 assert.match(text,/適合此任務/);
 assert.match(text,/免費剩餘.*未知/);
 assert.match(text,/官方 API 文件/);
 assert.match(text,/不會自動呼叫/);
 assert.doesNotMatch(text,/暫不可推薦|先停下來|配置停用/);
 assert.equal(JSON.parse(calls.at(-1).options.body).route_task,undefined);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_changing_task_selects_compatible_modalities_instead_of_empty_matches():
    run_ui_script(r"""
(async()=>{
 await tick();
 const s=structuredClone(snapshot);s.provider_inventory={providers:[{id:'images',catalogs:[{models:[
   {id:'paint',status:'listed',variants:[{task:'image_generation',inputs:['text'],outputs:['image']}]}
 ]}]}]};context.render(s);
 get('match-task').value='image_generation';get('match-output').value='text';
 get('match-task').handlers.change();
 assert.equal(get('match-input').value,'text');assert.equal(get('match-output').value,'image');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_quota_overview_separates_current_zero_history_and_public_limits():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data=structuredClone(snapshot);
 data.targets=[{...target,provider:'groq',provider_limits:[{metric:'requests',window:'day',value:500,current:true,reset:'utc_midnight'}],observations:[]}];
 data.provider_metadata={state:'available',openrouter:{state:'current',daily:{used:50,limit:50,remaining:0},checked_at:stamp,reset_at:stamp},refresh:{attempted_at:stamp,providers:{openrouter:{status:'updated',method:'api'},groq:{status:'console_required',reason:'headers_require_inference'}}}};
 data.accounts=[{provider:'openai',evidence:{current:false,as_of:stamp,sources:[]},balance:'17',current_balance:null,grants:[],benefit:{},model_limits:[],notes:[]}];
 context.render(data);
 const text=get('quota-overview').textContent;
 assert.match(text,/0 次/);assert.match(text,/最後觀測 17 USD/);assert.match(text,/500 requests/);
 assert.match(text,/餘額未知/);assert.doesNotMatch(text,/剩餘 500/);
 get('quota-filter').value='current';get('quota-filter').handlers.change();
 assert.doesNotMatch(get('quota-overview').textContent,/groq/);
 assert.match(get('quota-overview').textContent,/0 次/);
 await get('logout').handlers.click();assert.equal(get('quota-overview').textContent,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_task_candidate_shows_provider_observation_without_granting_model_quota():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data=structuredClone(snapshot);
 data.provider_metadata={openrouter:{daily:{remaining:50,limit:50,used:0},checked_at:stamp}};
 context.render(data);
 const row=context.matchCandidate({provider:'openrouter',model:'synthetic-free-model',safe:false,reasons:[],remaining:null},true);
 assert.match(row.textContent,/日計數最後觀測：50 次/);
 assert.match(row.textContent,/此模型適用範圍與本次可用額度仍待核對/);
 assert.match(row.textContent,/本次可用額度：未知/);
 assert.doesNotMatch(row.textContent,/免費資格與額度已通過/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_quota_kpi_priority_provider_usage_and_match_quota_summary():
    run_ui_script(r"""
(async()=>{
 await tick();
 assert.match(get('quota-kpi').textContent,/已納管供應商/);
 assert.match(get('quota-priority').textContent,/目前可檢查的免費路由/);
 assert.match(get('quota-priority').textContent,/text-fixture/);
 assert.ok(!get('quota-priority').textContent.includes('200.00 USD'));
 assert.ok(!get('quota-priority').textContent.includes('gemini-2.5'));
 context.renderUsage({...report,provider_usage:{state:'available',local_ledger_state:'unavailable',providers:[
   {provider:'openrouter',name:'OpenRouter',state:'reported',as_of:stamp,source:'https://openrouter.ai/api/v1/key',reported_metrics:[{metric:'free_model_daily_requests',label:'免費模型日計數',used:0,limit:50,remaining:50,unit:'requests',window:'day',current:true,window_matched:true}],includes_external_usage:true,external_usage_scope:'金鑰層級統計',local_recorded_generation_attempts:null,ledger_held_count:0}
 ]}});
 assert.match(get('provider-usage').textContent,/供應商回報／帳戶核對用量/);
 assert.match(get('provider-usage').textContent,/OpenRouter/);
 assert.match(get('provider-usage').textContent,/免費模型日計數：已用 0 \/ 50 requests/);
 const cand=context.matchCandidate({
   provider:'openai',model:'gpt-5-mini',safe:false,free_plan_verified:true,free_plan_basis:'credit_grant',
   access_stage:'account_grant_verified',
   quota_summary:{state:'observed_credit_grant',headline:'帳戶贈送點數最後觀測：200.00 USD',limits_text:'帳戶模型上限：500 RPM',reset_or_expiry:'實際點數到期：2026-12-03',sharing_scope:'帳戶共用贈送點數池'},
   reasons:['route_not_configured'],remaining:null
 });
 assert.match(cand.textContent,/帳戶贈送 Credits 與專案上限已核對/);
 assert.match(cand.textContent,/額度依據：帳戶贈送點數最後觀測：200\.00 USD/);
 assert.match(cand.textContent,/重置／到期：實際點數到期：2026-12-03/);
 await get('logout').handlers.click();
 assert.equal(get('quota-kpi').textContent,'');
 assert.equal(get('quota-priority').textContent,'');
 assert.equal(get('provider-usage').textContent,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_credit_route_uses_explicit_capacity_and_formats_micros_as_usd():
    run_ui_script(r"""
(async()=>{
 await tick();
 const t={...target,provider:'openai',required_capacity:[{metric:'credits_usd_micros',window:'budget'}],
 observations:[{metric:'credits_usd_micros',window:'budget',remaining:200000000,effective_remaining:199999900,local_observation_hold:100,current:true,as_of:stamp,valid_until:stamp}]};
 assert.equal(context.evidenceComplete(t),true);
 const row=context.providerRow(t);
 assert.match(row.textContent,/199\.9999\s+USD/);
 assert.match(row.textContent,/觀測 200/);
 assert.ok(!row.textContent.includes('199,999,900'));
 snapshot.targets=[t];snapshot.accounts=[{provider:'openai',qualification:'free_trial',billing:'not_started_no_payment_method',balance:'200.00',current_balance:'200.00',grants:[],benefit:{},model_limits:[],notes:[],evidence:{current:true,kind:'owner_reported_credit_account',sources:[],as_of:stamp,valid_until:stamp}}];
 context.renderAccounts(snapshot);
 assert.match(get('accounts').textContent,/Free trial/);
 assert.match(get('accounts').textContent,/使用者回報/);
 assert.match(get('accounts').textContent,/已接入 Broker/);
 assert.ok(!get('accounts').textContent.includes('沒有 OpenAI 可執行路由'));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_historical_grant_never_fabricates_zero_period_spending_or_fixed_ttl():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data=structuredClone(snapshot);
 data.accounts=[{provider:'openai',balance:'200.00',current_balance:null,balance_state:'stale',
   evidence:{current:false,as_of:stamp,valid_until:stamp,sources:[]},
   grants:[{amount:'200.00',remaining:'197.50',expires:{value:'2026-12-03',precision:'date'}}],
   benefit:{},model_limits:[],notes:[]}];
 context.render(data);
 const text=get('quota-overview').textContent;
 assert.match(text,/最後觀測剩餘 197\.5(?:0)? USD/);
 assert.match(text,/實際期間扣款未回報/);
 assert.match(text,/讀值已過時/);
 assert.doesNotMatch(text,/期間已用 0\.00 USD|逾 15 分鐘/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_provider_cost_and_counter_show_their_own_observation_times():
    run_ui_script(r"""
(async()=>{
 await tick();
 context.renderProviderUsage({state:'available',providers:[{provider:'openrouter',name:'OpenRouter',
  state:'reported',as_of:stamp,source:'https://openrouter.ai/api/v1/key',reported_metrics:[
   {metric:'free_model_daily_requests',label:'免費模型日計數',used:1,limit:50,remaining:49,
    unit:'requests',as_of:stamp,current:true},
   {metric:'total_usd',label:'累計金鑰費用',used:'1.23',unit:'USD',
    as_of:'2026-10-30T00:00:00Z',current:false}]}]});
 const text=get('provider-usage').textContent;
 assert.match(text,/此讀值觀測：/);
 assert.match(text,/2026\/10\/30/);
 assert.match(text,/讀值已過時/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_known_zero_account_credits_has_a_headline_and_missing_value_stays_unknown():
    run_ui_script(r"""
(async()=>{
 await tick();
 const walk=n=>[n,...(n.children||[]).filter(x=>typeof x!=='string').flatMap(walk)];
 const data=structuredClone(snapshot);data.source.kind='reviewed';data.source.audit_session_at=stamp;
 data.targets=[{...target,provider:'huggingface',enabled:false,free_eligible:false,observations:[],
   provider_limits:[],reviewed_account:{display_facts:[{kind:'credits_usd',value:0,period:'account',precision:'exact'}]}},
  {...target,provider:'sambanova',enabled:false,free_eligible:false,observations:[],provider_limits:[],
   reviewed_account:{display_facts:[]}}];
 context.render(data);
 const tableRows=walk(get('quota-overview')).filter(n=>n.tagName==='tr');
 const hf=tableRows.find(n=>n.textContent.includes('huggingface'));
 const samba=tableRows.find(n=>n.textContent.includes('sambanova'));
 assert.match(hf.textContent,/最後觀測帳戶點數 0 USD/);
 assert.match(hf.textContent,/帳戶核對/);
 assert.equal(walk(hf).some(n=>n.className==='quota-number unknown'),false);
 assert.equal(walk(samba).some(n=>n.className==='quota-number unknown'),true);
 assert.doesNotMatch(samba.textContent,/帳戶點數 0 USD/);
 assert.doesNotMatch(hf.textContent,/即時點數 0|剩餘 10 USD/);
 data.targets[0].reviewed_account.display_facts=[{kind:'credits_usd',value:null,period:'account'}];
 context.render(data);
 const missing=walk(get('quota-overview')).filter(n=>n.tagName==='tr').find(n=>n.textContent.includes('huggingface'));
 assert.equal(walk(missing).some(n=>n.className==='quota-number unknown'),true);
 assert.doesNotMatch(missing.textContent,/帳戶點數 0 USD/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_mistral_monthly_allowance_is_separate_from_rounded_usage_and_zero_prepaid():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data=structuredClone(snapshot);data.source.kind='reviewed';data.source.audit_session_at=stamp;
 data.targets=[{...target,provider:'mistral',observations:[],provider_limits:[],
  reviewed_account:{display_facts:[
   {kind:'monthly_allowance_usd',value:10,period:'calendar_month',precision:'exact'},
   {kind:'used_allowance_usd',value:0,period:'calendar_month',precision:'rounded'},
   {kind:'credits_usd',value:0,period:'account',precision:'exact'}]}}];
 context.render(data);
 const text=get('quota-overview').textContent;
 assert.match(text,/方案內含：10 USD/);
 assert.match(text,/每曆月1日（時區未提供）/);
 assert.match(text,/最後觀測已用：0 USD（約）/);
 assert.match(text,/最後觀測帳戶點數 0 USD/);
 assert.doesNotMatch(text,/剩餘 10 USD|即時.*10 USD/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_live_overlay_facts_show_their_own_audit_time_instead_of_runtime_asof():
    run_ui_script(r"""
(async()=>{
 await tick();
 const data=structuredClone(snapshot);data.source.kind='gateway';data.source.as_of=stamp;
 data.targets=[{...target,provider:'huggingface',observations:[],provider_limits:[],
  reviewed_account:{audit_session_at:'2026-10-12T00:00:00Z',display_only:true,
   display_facts:[{kind:'credits_usd',value:0,period:'account',precision:'exact'}]}}];
 context.render(data);
 const text=get('quota-overview').textContent;
 assert.match(text,/帳戶核對 2026\/10\/12/);
 assert.doesNotMatch(text,/帳戶核對 2026\/11\/1/);
 assert.match(text,/最後觀測帳戶點數 0 USD/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
