'use strict';
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const num = value => value === null || value === undefined ? '—' : new Intl.NumberFormat('zh-TW').format(value);
const date = value => value ? new Date(value).toLocaleString('zh-TW', {hour12:false}) : '未提供';
const metrics = {requests:'requests',input_tokens:'input tokens',output_tokens:'output tokens',total_tokens:'total tokens',tokens:'total tokens',neurons:'neurons',audio_seconds:'秒',images:'張圖片',pages:'頁',conversions:'次轉換'};
const windows = {rolling_minute:'滾動分鐘',rolling_hour:'滾動小時',day:'日視窗',month:'月視窗',budget:'預算視窗'};
const states = {ready:'基本檢查通過・待任務檢查',blocked:'需要檢查',unknown:'狀態未知'};
const credentials = {present:'名稱清單已確認',missing:'缺少名稱',unknown:'尚未確認'};
const reasonNames = {billing_enabled:'已啟用計費，禁止推薦',gateway_ineligible:'未通過 Gateway 任務條件',diagnostics_not_ready:'最新本機狀態尚未就緒',credential_inventory_not_current:'憑證名稱清單未知或已到期',free_evidence_not_current:'免費資格證據需要重新確認',free_eligibility_unverified:'免費資格尚未確認',free_evidence_missing:'缺少免費資格證據',credential_inventory_unknown:'憑證名稱清單未知',provider_constraint:'不符合供應商限制',model_constraint:'不符合模型限制',capability_mismatch:'不支援此任務能力',quota_evidence_missing:'缺少權威配額讀值',quota_evidence_not_current:'配額讀值缺少、過時或不完整',quota_insufficient:'已確認餘額不足',quota_cost_unknown:'此配額的成本未知',disabled:'配置停用',local_cap:'本機保護上限不足',observed_capacity:'觀測餘額不足',gateway_blocked:'Gateway 條件阻擋'};
function reason(value) { const key=value.split(':')[0]; return (reasonNames[key] || '需要核對路由條件') + (value.includes(':') ? '（'+value.split(':').slice(1).map(v=>metrics[v]||windows[v]||v).join(' / ')+'）' : ''); }
let pending=false;
let sessionVersion=0;
let taskVersion=0;
let hasAdvice=false;
function invalidateAdvice(){taskVersion++;if(!hasAdvice)return;$('advice-result').replaceChildren(node('p','任務或資料已變更，請重新檢查免費路由。','empty'));}
$('advice-form').addEventListener('input',invalidateAdvice);
async function api(path, body) {
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Dashboard-Request':'1'},body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store'});
  const data=await response.json();
  if(!response.ok){ const error=new Error(data.error || 'source_unavailable'); error.status=response.status; throw error; }
  return data;
}
function showLogin(){ sessionVersion++;pending=false;invalidateAdvice();$('error').hidden=true;$('login-panel').hidden=false; $('workspace').hidden=true; $('refresh').hidden=true; $('advice-jump').hidden=true; $('logout').hidden=true; }
function localCap(q){const block=node('div',undefined,'local-cap');const label=num(q.used)+' / '+num(q.limit)+' '+(metrics[q.metric]||q.metric)+' · '+(windows[q.window]||q.window)+(q.timezone?' · '+q.timezone:'');block.append(node('div',label,'small'));if(q.used!==null && q.limit!==null && q.limit>0){const progress=document.createElement('progress');progress.max=q.limit;progress.value=Math.min(q.used,q.limit);progress.setAttribute('aria-label','本機保護額度 '+label);block.append(progress);}return block;}
function labelPair(parent, label, value){ const p=node('p'); p.append(node('strong',label+'：'),document.createTextNode(value)); parent.append(p); }
function render(snapshot){
  $('login-panel').hidden=true; $('workspace').hidden=false; $('refresh').hidden=false; $('advice-jump').hidden=false; $('logout').hidden=false; $('error').hidden=true;
  $('source').textContent=snapshot.source.label+' · 快照 '+date(snapshot.source.as_of)+' · 取得 '+date(snapshot.source.fetched_at);
  const ready=snapshot.targets.filter(t=>t.state==='ready').length;
  $('pool-status').textContent=snapshot.targets.length+' 個路由 · '+ready+' 個基本檢查通過 · '+(snapshot.targets.length-ready)+' 個待核對';
  $('providers').replaceChildren();
  const oldProvider=$('provider').value;
  $('provider').replaceChildren(node('option','不限供應商')); $('provider').firstChild.value='';
  [...new Set(snapshot.targets.map(t=>t.provider))].forEach(p=>{const o=node('option',p);o.value=p;$('provider').append(o);}); $('provider').value=oldProvider;
  snapshot.targets.forEach(t=>{
    const row=node('article',undefined,'provider-row');
    const header=node('div',undefined,'provider-summary'); const identity=node('div'); identity.append(node('div',t.provider,'provider-name'),node('div',t.model,'model'));
    const evidenceKnown=t.local_caps.length>0 && t.local_caps.every(cap=>t.observations.some(q=>q.metric===cap.metric && q.window===cap.window && q.current && q.effective_remaining!==null)) && t.observations.every(q=>q.current && q.effective_remaining!==null);
    header.append(identity,node('span',t.state==='ready'&&!evidenceKnown?'餘額待確認':states[t.state]||'未知','state'+(t.state==='ready'&&evidenceKnown?'':' warning'))); row.append(header);
    const quota=node('div',undefined,'quota-line');
    t.observations.slice(0,3).forEach(q=>{const part=node('div',undefined,'quota-item');part.append(node('span',num(q.remaining),q.remaining===null||!q.current?'remaining unknown':'remaining'),document.createTextNode(' '+(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window)));quota.append(part);});
    if(!t.observations.length)quota.append(node('span','— 尚無供應商餘額讀值','small'));row.append(quota);
    if(t.local_caps.length){const local=node('div',undefined,'local-summary');local.append(node('span','本機保護額度（含保留）','small'),localCap(t.local_caps[0]));row.append(local);}
    const detail=node('details');detail.append(node('summary','查看證據、保留量與本機上限'));const grid=node('div',undefined,'details-grid');const eligibility=node('div');
    labelPair(eligibility,'免費資格',t.free_eligible?'已確認':'未知或未確認');labelPair(eligibility,'計費',t.billing_enabled?'已啟用・不允許推薦':'未啟用');labelPair(eligibility,'資格確認',date(t.free_verified_at));labelPair(eligibility,'資格證據期限',date(t.free_evidence_expires_at));
    const cred=node('div');labelPair(cred,'憑證清單',credentials[t.credentials.state]);labelPair(cred,'清單確認',date(t.credentials.verified_at));labelPair(cred,'清單期限',date(t.credentials.expires_at));labelPair(cred,'阻擋原因',t.reasons.map(reason).join('；')||'未見本機阻擋，仍需任務檢查');grid.append(eligibility,cred);
    const caps=node('div',undefined,'detail-wide');caps.append(node('strong','本機保護額度（當前視窗，含保留）'));t.local_caps.forEach(q=>{caps.append(localCap(q));caps.append(node('p','快照 '+date(q.as_of),'small'));});caps.append(node('p','本機帳本已用量不代表供應商剩餘額度；未知值不換算比例。'));grid.append(caps);
    const observations=node('div',undefined,'detail-wide');observations.append(node('strong','供應商餘額證據'));
    t.observations.forEach(q=>{const o=node('div',undefined,'observation');labelPair(o,(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window),'餘額 '+num(q.remaining)+' · 本機保留 '+num(q.local_observation_hold)+' · 保守可用 '+num(q.effective_remaining));labelPair(o,'觀測時間',date(q.as_of));labelPair(o,'觀測期限',date(q.valid_until)+(q.current?'':'（已過時或未知）'));labelPair(o,'配額重置',date(q.reset_at));labelPair(o,'證據種類',(q.provenance||'未知')+' / '+(q.confidence||'未知'));observations.append(o);});grid.append(observations);detail.append(grid);row.append(detail);$('providers').append(row);
  });
  $('usage').replaceChildren();
  snapshot.usage.forEach(u=>{const row=node('div',undefined,'activity');const who=node('div');who.append(node('p',u.provider),node('p',u.model,'small'));const data=node('div');data.append(node('p',num(u.requests)+' 次生成嘗試'),node('p','輸入 '+num(u.reported_input_tokens)+' / 輸出 '+num(u.reported_output_tokens)+' tokens','small'),node('p','帳本保留 '+num(u.ledger_held_count)+' · 結果未知 '+num(u.outcome_unknown_count),'small'));row.append(who,data);$('usage').append(row);});
  if(!snapshot.usage.length)$('usage').append(node('p','尚無已送出的生成嘗試。合成示範不會執行任務。','empty'));
  $('recent').replaceChildren();snapshot.recent.forEach(r=>{const row=node('div',undefined,'activity');const who=node('div');who.append(node('p',(r.provider||'未選路由')+' · '+(r.state||'未知')),node('p',r.model||'—','small'));row.append(who,node('div',date(r.created_at),'small'));$('recent').append(row);});
  if(!snapshot.recent.length)$('recent').append(node('p','尚無工作紀錄。檢查路由不會建立工作。','empty'));
}
async function refresh(){if(pending)return;invalidateAdvice();const version=sessionVersion;pending=true;$('refresh').disabled=true;$('refresh').textContent='讀取中…';try{const snapshot=await api('/api/snapshot');if(version!==sessionVersion)return;render(snapshot);}catch(error){if(version!==sessionVersion)return;if(error.status===401){showLogin();}else{$('workspace').hidden=true;$('error').hidden=false;$('source').textContent='資料來源無法使用 · 未顯示舊讀值';}}finally{if(version===sessionVersion){pending=false;$('refresh').disabled=false;$('refresh').textContent='重新整理';}}}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;sessionVersion++;pending=false;button.disabled=true;$('login-error').textContent='';try{await api('/api/login',{token:$('login-token').value});$('login-token').value='';await refresh();}catch(error){$('login-error').textContent=error.status===429?'登入嘗試過多，請稍後再試。':'登入失敗，請核對工作台權杖。';}finally{button.disabled=false;}});
$('refresh').addEventListener('click',refresh);
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{});invalidateAdvice();showLogin();$('source').textContent='已登出。';}catch(error){$('source').textContent='登出失敗，請稍後再試。';}});
$('advice-form').addEventListener('submit',async event=>{event.preventDefault();const version=++taskVersion;hasAdvice=true;const button=event.submitter;button.disabled=true;button.textContent='檢查中…';$('advice-result').replaceChildren(node('p','正在核對任務與證據…','empty'));try{const task={request_key:'advice-'+crypto.randomUUID(),capability:'text_generation',input:$('task-input').value,max_output_tokens:Number($('output').value)};if($('provider').value)task.provider=$('provider').value;if($('model').value.trim())task.model=$('model').value.trim();const data=await api('/api/advice',task);if(version!==taskVersion)return;const result=node('div',undefined,data.decision==='recommend'?'result-recommend':'result-blocked');result.append(node('h3',data.decision==='recommend'?'可建議此免費路由':'需要你確認'));if(data.selected_target){result.append(node('p',data.selected_target.provider+' · '+data.selected_target.model));result.append(node('p','本次已確認免費資格、憑證清單與足夠餘額。建議沒有送出任務。','small'));}else{data.candidates.filter(c=>c.reasons.length).forEach(c=>{const p=node('p',c.provider+'：'+c.reasons.map(reason).join('；'),'small');result.append(p);});}if(data.recheck_at)result.append(node('p','估計可重新核對：'+date(data.recheck_at)+'。不會自動排程，期限不保證配額已恢復。','small'));result.append(node('p','資料：'+data.source.label+' · '+date(data.source.as_of),'small'));$('advice-result').replaceChildren(result);}catch(error){if(version!==taskVersion)return;if(error.status===401)showLogin();$('advice-result').replaceChildren(node('p',error.message==='invalid_task'?'任務格式或輸出上限無效，請修正後再試。':'無法取得建議。資料來源未確認，請重新整理後再試。','result-error'));}finally{button.disabled=false;button.textContent='檢查免費路由';}});
refresh();
