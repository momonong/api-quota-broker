'use strict';
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const num = value => value === null || value === undefined ? '—' : new Intl.NumberFormat('zh-TW').format(value);
const date = value => value ? new Date(value).toLocaleString('zh-TW', {hour12:false}) : '未提供';
const metrics = {requests:'requests',input_tokens:'input tokens',output_tokens:'output tokens',total_tokens:'total tokens',tokens:'total tokens',neurons:'neurons',audio_seconds:'秒',images:'張圖片',pages:'頁',conversions:'次轉換'};
const usageUnknownFields = {reported_input_tokens:'input_unknown_count',reported_output_tokens:'output_unknown_count',reported_neurons:'neurons_unknown_count'};
const windows = {rolling_minute:'滾動分鐘',rolling_hour:'滾動小時',day:'日視窗',month:'月視窗',budget:'預算視窗'};
const states = {ready:'基本檢查通過・待任務檢查',blocked:'需要檢查',unknown:'狀態未知'};
const credentials = {present:'名稱清單已確認',missing:'缺少名稱',unknown:'尚未確認'};
const taskNames = {text_generation:'文字生成',translation:'翻譯',ocr:'文字辨識',embedding:'向量嵌入',rerank:'排序',speech_to_text:'語音轉文字',text_to_speech:'文字轉語音',image_generation:'圖片生成',audio_transcription:'語音轉文字',audio_generation:'音訊生成',document_conversion:'文件轉換'};
const usageTaskTypes = ['unknown','text_generation','translation','ocr','vision','code_completion','audio_transcription','audio_translation','tts','image_generation','embedding','rerank','classification','moderation'];
Object.assign(taskNames,{unknown:'類別未記錄',vision:'圖片理解',code_completion:'程式補全',audio_translation:'音訊翻譯',tts:'文字轉語音',classification:'分類',moderation:'內容審查'});
const modalityNames = {text:'文字',image:'圖片',document:'文件',audio:'音訊',video:'影片',embedding:'向量',json:'JSON',scores:'分數',labels:'標籤'};
const catalogTaskNames={...taskNames,unknown:'能力未知',realtime:'即時互動',realtime_audio_transcription:'即時轉錄',image_editing:'圖片編輯',video_generation:'影片生成',music_generation:'音樂生成',decisions:'結構化決策',extraction:'資料抽取',document_extraction:'文件抽取',code_generation:'程式生成',reward_scoring:'回覆評分',object_detection:'物件偵測',image_classification:'圖片分類',turn_detection:'語音輪次偵測',batch:'批次工作',agents:'代理與工具',files:'檔案與快取',model_discovery:'模型查詢',management:'用量與管理',specialized:'專門領域推論',robotics_reasoning:'機器人推理',computer_use:'電腦操作'};
const catalogStatus={listed:'已列出',preview:'預覽',restricted:'存取受限',deprecated:'舊版／待退場',retired:'已退場',self_hosted:'開放權重',unknown:'狀態未核對'};
const catalogPageSize=24;
const reasonNames = {credit_expired:'已確認點數到期，停止使用',billing_state_unknown:'計費狀態未核對',billing_enabled:'已啟用計費，禁止推薦',gateway_ineligible:'未通過 Gateway 任務條件',diagnostics_not_ready:'最新本機狀態尚未就緒',credential_inventory_not_current:'憑證名稱清單未知或已到期',free_evidence_not_current:'免費資格證據需要重新確認',free_eligibility_unverified:'免費資格尚未確認',free_evidence_missing:'缺少免費資格證據',credential_inventory_unknown:'憑證名稱清單未知',provider_constraint:'不符合供應商限制',model_constraint:'不符合模型限制',capability_mismatch:'不支援此任務能力',features_mismatch:'不符合功能需求',quota_evidence_missing:'缺少權威配額讀值',quota_evidence_not_current:'配額讀值缺少、過時或不完整',quota_insufficient:'已確認餘額不足',quota_cost_unknown:'此配額的成本未知',disabled:'配置停用',local_cap:'本機保護上限不足',observed_capacity:'觀測餘額不足',gateway_blocked:'Gateway 條件阻擋'};
function reason(value) { const key=value.split(':')[0]; return (reasonNames[key] || '需要核對路由條件') + (value.includes(':') ? '（'+value.split(':').slice(1).map(v=>metrics[v]||windows[v]||v).join(' / ')+'）' : ''); }
let pending=false, sessionVersion=0, taskVersion=0, usageVersion=0, hasAdvice=false;
let currentSnapshot=null, activeTab='models', usageLoaded=false;
let catalogPage=0, catalogOpen=false, catalogSourceScope=null;
function invalidateAdvice(){taskVersion++;invalidateMatch();if(hasAdvice)$('advice-result').replaceChildren(node('p','任務或資料已變更，請重新檢查免費路由。','empty'));}
async function api(path, body) {
  const response=await fetch('.'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Dashboard-Request':'1'},body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store'});
  const data=await response.json();
  if(!response.ok){ const error=new Error(data.error || 'source_unavailable'); error.status=response.status; throw error; }
  return data;
}
function options(id,values,label,names={}){
  const control=$(id),old=control.value;
  control.replaceChildren(node('option',label));control.firstChild.value='';
  [...new Set(values)].sort().forEach(value=>{const option=node('option',names[value]||value);option.value=value;control.append(option);});
  control.value=values.includes(old)?old:'';
}
function clearWorkspace(){
  currentSnapshot=null;usageLoaded=false;usageVersion++;
  catalogPage=0;catalogSourceScope=null;showCatalog(false);$('catalog-search').value='';$('catalog-status').value='current';
  $('catalog-advanced').open=false;$('catalog-source-panel').open=false;
  ['catalog-summary','catalog-sources','catalog-count','catalog-models','catalog-page'].forEach(id=>$(id).replaceChildren());
  ['catalog-provider','catalog-task','catalog-input','catalog-output'].forEach(id=>$(id).replaceChildren());
  ['accounts','providers','usage','usage-totals','usage-coverage','usage-status','recent','time-overview','time-preview','source-detail','evidence-brief','activity-source','pool-status','model-count','advice-compatibility'].forEach(id=>$(id).replaceChildren());
  options('provider',[],'不限供應商');options('model',[],'不限模型');options('model-provider',[],'全部供應商');options('model-task',[],'全部任務');options('model-input',[],'全部輸入');options('usage-provider',[],'全部供應商');options('usage-model',[],'全部模型');
  ['task-input','match-text','login-token','usage-from','usage-to'].forEach(id=>$(id).value='');
  ['match-task','match-input','match-output','match-provider'].forEach(id=>$(id).replaceChildren());
  $('output').value='128';$('task-kind').value='text_generation';$('input-modality').value='text';$('output-modality').value='text';$('usage-period').value='all';$('usage-task').value='';$('usage-dates').hidden=true;$('translation-fields').hidden=true;
  $('advice-result').replaceChildren(node('p','只評估，不送出任務。','empty'));$('advice-submit').disabled=false;$('advice-submit').textContent='檢查免費路由';hasAdvice=false;
}
function showLogin(){sessionVersion++;pending=false;invalidateAdvice();clearWorkspace();$('source').textContent='請登入後查看資料。';$('error').hidden=true;$('login-panel').hidden=false;$('workspace').hidden=true;$('refresh').hidden=true;$('logout').hidden=true;selectTab('models');}
function selectTab(name,focus=false){
  activeTab=name;
  ['models','usage','advice'].forEach(key=>{const selected=key===name;const tab=$('tab-'+key);tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;$('view-'+key).hidden=!selected;});
  if(focus)$('tab-'+name).focus();
  if(name==='usage'&&currentSnapshot&&!usageLoaded)loadUsage();
}
function localCap(q){const block=node('div',undefined,'local-cap');const label=num(q.used)+' / '+num(q.limit)+' '+(metrics[q.metric]||q.metric)+' · '+(windows[q.window]||q.window)+(q.timezone?' · '+q.timezone:'');block.append(node('div',label,'small'));if(q.used!==null && q.limit!==null && q.limit>0){const progress=document.createElement('progress');progress.max=q.limit;progress.value=Math.min(q.used,q.limit);progress.setAttribute('aria-label','本機保護額度 '+label);block.append(progress);}return block;}
function labelPair(parent, label, value){ const p=node('p'); p.append(node('strong',label+'：'),document.createTextNode(value)); parent.append(p); }
const qualificationNames={account_plan:'已登入帳戶／方案證據',public_conditions:'公開免費條件適用・尚未驗證 API 接受請求',unverified:'資格未核對'};
const resetNames={pacific_midnight_project:'Pacific 午夜・專案共用',utc_midnight:'UTC 午夜',calendar_month_day_1:'每曆月第 1 日',unknown:'未知'};
const evidenceNames={official:'官方公開資訊',observed:'人工核對帳戶頁面',estimated:'推算',unknown:'未知'};
const periodNames={per_second:'每秒',calendar_month:'曆月・每月第 1 日重置',this_month:'本月歷史',last_month:'上月歷史', '28_days':'28 天歷史',account:'帳戶快照',current_day:'當日歷史'};
const factNames={requests_per_second:'請求速率（僅顯示）',monthly_allowance_usd:'每月內含額度（非精確剩餘）',used_allowance_usd:'畫面已用金額',credits_usd:'帳戶 credits',historical_requests:'歷史請求',historical_tokens:'歷史 total tokens',historical_cost_usd:'歷史估算費用',purchases:'購買交易筆數'};
function reviewedDetails(grid,t){
  const account=t.reviewed_account;
  if(account){
    const block=node('div',undefined,'detail-wide');block.append(node('strong','人工核對帳戶證據'));
    labelPair(block,'資格依據',qualificationNames[account.qualification_basis]||'未知');
    labelPair(block,'方案',account.plan==='free'?'Free 方案證據':'未核對');
    labelPair(block,'核對檔保護','本機核對檔不執行任務；不是線上 Broker 開關。');labelPair(block,'Broker 啟用配置',runtimeState(t));labelPair(block,'配置觀測',date(t.runtime_configuration?.observed_at));
    account.notes.forEach(text=>block.append(node('p',text,'small')));
    account.display_facts.forEach(f=>{let amount=num(f.value);if(f.kind==='requests_per_second')amount+=' requests/second・不換算每分鐘';else if(f.kind.endsWith('_usd'))amount+=' USD';labelPair(block,factNames[f.kind]||f.kind,amount+' · '+(periodNames[f.period]||f.period)+(f.precision==='rounded'?' · 畫面四捨五入，不倒算剩餘':''));});
    account.sources.forEach(source=>labelPair(block,'核對來源',source));grid.append(block);
  }
  if(t.credit_expires_at){const expiry=node('div',undefined,'detail-wide');labelPair(expiry,'已核對的實際點數到期',date(t.credit_expires_at));grid.append(expiry);}
  const limits=node('div',undefined,'detail-wide');limits.append(node('strong','供應商上限（與剩餘分開）'));
  (t.provider_limits||[]).forEach(q=>{const item=node('div',undefined,'observation');labelPair(item,(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window),num(q.value)+' · '+(q.basis==='account'?'帳戶／專案限額':q.basis==='public_plan'?'公開方案限制・非帳戶特例':'來源範圍待核對'));labelPair(item,'證據種類',evidenceNames[q.provenance]||'未知');labelPair(item,'核對時段',date(q.as_of));labelPair(item,'本機再核對期限',date(q.valid_until)+'（非點數到期）'+(q.current?'':'・已過時或未知'));labelPair(item,'視窗重置',resetNames[q.reset]||'未知');if(q.source)labelPair(item,'上限來源',q.source);limits.append(item);});
  if(!(t.provider_limits||[]).length)limits.append(node('p','尚無已核對的供應商上限。','small'));
  grid.append(limits);
}
const money = value => value === null || value === undefined ? '—（未知）' : value+' USD';
const preciseDate = value => !value ? '未提供' : value.precision === 'date' ? value.value+'（僅日期・時間與時區未知）' : date(value.value);
function evidenceComplete(t){
  const observations=t.observations||[],caps=t.local_caps||[];
  return t.state==='ready' && t.enabled!==false && t.free_eligible===true && t.billing_enabled===false && t.billing_state!=='unknown' && t.credentials.state==='present' && caps.length>0 && observations.length>0 &&
    caps.every(cap=>observations.some(q=>q.metric===cap.metric && q.window===cap.window && q.current && q.effective_remaining!==null && q.effective_remaining!==undefined)) &&
    observations.every(q=>q.current && q.effective_remaining!==null && q.effective_remaining!==undefined);
}
function grantExpiryText(account){
  if(!account || !account.grants.length)return '未提供';
  return account.grants.map((g,i)=>(account.grants.length>1?'第 '+(i+1)+'/'+account.grants.length+' 筆 ':'')+preciseDate(g.expires)).join('；');
}
function renderAccounts(snapshot){
  $('accounts').replaceChildren();
  const accounts=snapshot.accounts||[];
  accounts.forEach((account,index)=>{
    const row=node('article',undefined,'credit-account');
    const heading=node('div',undefined,'credit-heading');
    const identity=node('div');identity.append(node('h3',account.provider+' API credits'+(accounts.length>1?' · 帳戶 '+(index+1):'')),node('p','獨立監看 · 不加入可執行路由','small'));
    const current=account.evidence.current && account.current_balance!==null && account.current_balance!==undefined;
    const value=node('div');const balance=node('p',undefined,'credit-balance'+(current?'':' unknown'));
    balance.append(node('span',snapshot.source.kind==='demo'?'合成示例餘額':current?'有效餘額':'目前有效餘額','credit-label'),document.createTextNode(current?money(account.current_balance):'未知'));
    value.append(balance);
    if(!current && account.balance!==null && account.balance!==undefined)value.append(node('p','歷史快照 '+money(account.balance)+' · 證據已過時','history-value'));
    heading.append(identity,value);row.append(heading);
    const brief=node('div',undefined,'account-brief');
    brief.append(node('p','觀測 '+date(account.evidence.as_of)),node('p','餘額證據至 '+date(account.evidence.valid_until)));
    if(account.grants.length)brief.append(node('p','實際點數到期 '+grantExpiryText(account)));
    row.append(brief);
    const details=node('details');details.append(node('summary','補充條件、限制與來源'));
    const body=node('div',undefined,'details-grid');
    const renewal=node('div');labelPair(renewal,'下次訂閱補充',date(account.benefit.renews_at));labelPair(renewal,'下次金額',money(account.benefit.renewal_amount));renewal.append(node('p',account.benefit.recurrence==='conditional_on_eligible_subscription_renewal'?'符合資格的訂閱續期後才可能補充；不是每月第1日固定重置。':'補充規則未核對。','small'));
    labelPair(renewal,'結轉',account.benefit.rollover===false?'不結轉':'未知');body.append(renewal);
    const grants=node('div');account.grants.forEach(grant=>{labelPair(grants,'實際點數到期',preciseDate(grant.expires));labelPair(grants,'該筆核發／剩餘',money(grant.amount)+' / '+money(grant.remaining));labelPair(grants,'收到日期',preciseDate(grant.received));});if(!account.grants.length)labelPair(grants,'實際點數到期','未提供');body.append(grants);
    const qualification=node('div',undefined,'detail-wide');labelPair(qualification,'資格',account.qualification==='linked_chatgpt_subscription_benefit'?'已連結 ChatGPT 訂閱福利':'未核對');labelPair(qualification,'計費',account.billing==='not_started_no_payment_method'?'帳戶尚未啟用計費・無付款方式':'未知');qualification.append(node('p','僅監看帳戶；沒有 OpenAI 可執行路由，正餘額不代表任務可用。','small'));body.append(qualification);
    const usage=account.usage_page;
    if(usage)body.append(node('p',(usage.state==='no_data'?'官方用量頁：無用量資料':'官方用量頁：狀態未知')+' · '+usage.from_date+' 至 '+usage.through_date+' · 全部 API sources／projects／keys（不把摘要零值視為已確認零消耗）','detail-wide small'));
    account.model_limits.forEach(limit=>{const item=node('div',undefined,'detail-wide observation');item.append(node('strong',limit.model+' · 帳戶觀測上限，非剩餘'));labelPair(item,'一般速率',num(limit.requests_per_minute)+' requests/min · '+num(limit.tokens_per_minute)+' tokens/min · '+num(limit.images_per_minute)+' images/min');labelPair(item,'Batch queue',num(limit.batch_queue_tokens_per_day)+' tokens/day（批次佇列維度，非一般每日剩餘）');body.append(item);});
    const sources=node('div',undefined,'detail-wide');account.evidence.sources.forEach(source=>labelPair(sources,'核對來源',source));account.notes.forEach(note=>sources.append(node('p',note,'small')));sources.append(node('p','重新讀取只重讀本機證據，不取得官方新餘額，也不延長證據有效期。','small'));body.append(sources);details.append(body);row.append(details);$('accounts').append(row);
  });
  if(!accounts.length)$('accounts').append(node('p','尚無獨立帳戶點數證據。下方供應商的資格與餘額仍須各自核對。','empty'));
  const metadata=snapshot.provider_metadata;
  if(metadata){
    const counter=metadata.openrouter;
    const block=node('article',undefined,'credit-account');
    block.append(node('h3','OpenRouter 免費日計數'));
    const value=counter?.daily;
    labelPair(block,'本次查詢金鑰',value?num(value.remaining)+' 次剩餘 · 已用 '+num(value.used)+' / '+num(value.limit):counter?.state==='stale'?'證據已過期 · 剩餘未知':'剩餘未知');
    if(counter){
      labelPair(block,'觀測時間',date(counter.checked_at));
      labelPair(block,'證據有效至',date(counter.valid_until));
      labelPair(block,'日計數重置',date(counter.reset_at));
    }
    block.append(node('p','供應商回報的方案計數。實際執行資格與帳戶美元點數另行核對。','small'));
    $('accounts').append(block);
  }
  const activity=snapshot.activity_source;
  $('activity-source').textContent=activity?activity.label+' · '+(activity.state==='available'?'本機讀取 '+date(activity.as_of):'未接入／不可用，用量未知'):'用量來源未知';
}
function providerRow(t){
  const row=node('details',undefined,'provider-row');
  const summary=node('summary');const header=node('div',undefined,'provider-summary');const identity=node('div',undefined,'provider-identity');
  identity.append(node('div',t.provider,'provider-name'),node('div',t.model,'model'));
  const complete=evidenceComplete(t);
  const stale=t.observations.some(q=>q.remaining!==null && q.remaining!==undefined && !q.current);
  const status=t.enabled===false?(stale?'停用・餘額過時':'配置停用'):complete?'可做任務檢查':stale?'餘額證據已過時':t.state==='ready'?'餘額待確認':states[t.state]||'狀態未知';
  header.append(identity,node('span',status,'state'+(complete?'':' warning')));summary.append(header);
  const quota=node('div',undefined,'quota-line');
  t.observations.slice(0,3).forEach(q=>{
    const part=node('div',undefined,'quota-item');const current=q.current && q.effective_remaining!==null && q.effective_remaining!==undefined;
    part.append(document.createTextNode('保守可用 '),node('span',current?num(q.effective_remaining):'—',current?'remaining':'unknown'),document.createTextNode(' '+(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window)));
    quota.append(part);
  });
  if(!t.observations.length)quota.append(node('span','剩餘未知 · 尚無供應商餘額讀值','small'));
  summary.append(quota);
  const first=t.observations[0];
  if(first){
    const historical=first.remaining!==null && first.remaining!==undefined;
    const dimension=(metrics[first.metric]||first.metric)+' / '+(windows[first.window]||first.window);
    const more=t.observations.length>1?' · 另 '+(t.observations.length-1)+' 份見詳情':'';
    summary.append(node('p','首筆 '+dimension+' · '+(historical?(first.current?'觀測 ':'歷史觀測 ')+num(first.remaining)+' · 保留 '+num(first.local_observation_hold)+(first.provenance==='estimated'?' · 推算':'')+' · '+date(first.as_of):'無讀值；上限不是剩餘')+more,'capacity-meta'));
  }
  row.append(summary);
  const grid=node('div',undefined,'details-grid');const eligibility=node('div');
  labelPair(eligibility,'免費資格',t.reviewed_account?(qualificationNames[t.reviewed_account.qualification_basis]||'未核對'):(t.free_eligible?'已確認':'未知或未確認'));
  labelPair(eligibility,'計費',t.billing_state==='unknown'?'計費狀態未核對':t.billing_enabled?'已啟用・不允許推薦':'已核對未啟用');
  labelPair(eligibility,'資格核對時段',date(t.free_verified_at));labelPair(eligibility,'資格本機再核對期限',date(t.free_evidence_expires_at)+'（非點數到期）');
  const cred=node('div');labelPair(cred,'憑證清單',credentials[t.credentials.state]||'尚未確認');labelPair(cred,'清單確認',date(t.credentials.verified_at));labelPair(cred,'清單期限',date(t.credentials.expires_at));labelPair(cred,'阻擋原因',t.reasons.map(reason).join('；')||'未見本機阻擋，仍需任務檢查');grid.append(eligibility,cred);reviewedDetails(grid,t);
  const caps=node('div',undefined,'detail-wide');caps.append(node('strong','本機保護額度（當前視窗，含保留）'));t.local_caps.forEach(q=>{caps.append(localCap(q));caps.append(node('p','快照 '+date(q.as_of),'small'));});caps.append(node('p','本機帳本已用量不代表供應商剩餘額度；未知值不換算比例。'));grid.append(caps);
  const observations=node('div',undefined,'detail-wide');observations.append(node('strong','供應商餘額證據'));
  t.observations.forEach(q=>{const o=node('div',undefined,'observation');labelPair(o,(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window),'快照餘額 '+num(q.remaining)+(q.provenance==='estimated'?'（推算，非直接回報）':'')+' · 本機保留 '+num(q.local_observation_hold)+' · 目前保守可用 '+(q.current?num(q.effective_remaining):q.remaining===null||!q.as_of?'—（剩餘未知）':'—（快照已過時）'));labelPair(o,'觀測時間',date(q.as_of));labelPair(o,'觀測期限',date(q.valid_until)+(q.current?'':!q.as_of?'（無觀測讀值）':'（已過時或期限未知）'));labelPair(o,'配額重置',date(q.reset_at));labelPair(o,'證據種類',(evidenceNames[q.provenance]||'未知')+' / '+(q.confidence||'未知'));if(q.remaining_basis==='derived_from_used_and_limit')labelPair(o,'推算依據','上限減當時已用；歷史推算值不隨重新整理續期');observations.append(o);});
  if(!t.observations.length)observations.append(node('p','沒有已核對的餘額；不從上限或帳本倒算。','small'));
  grid.append(observations);row.append(grid);return row;
}
function renderTimeOverview(snapshot){
  const box=$('time-overview');box.replaceChildren();
  const accounts=snapshot.accounts||[],targets=snapshot.targets||[];
  const all=targets.flatMap(t=>t.observations.map(q=>({provider:t.provider,...q})));
  const earliest=(items,key)=>items.filter(x=>x[key] && Number.isFinite(Date.parse(x[key]))).sort((a,b)=>Date.parse(a[key])-Date.parse(b[key]))[0];
  const reset=earliest(all,'reset_at');
  const account=accounts[0],grant=account?.grants[0];
  const accountScope=account?(accounts.length>1?account.provider+' · 首個帳戶（共 '+accounts.length+' 個）':account.provider):'未知帳戶';
  const reviewTarget=earliest(targets,'free_evidence_expires_at');
  const review=snapshot.source.review_due_at || reviewTarget?.free_evidence_expires_at;
  const evidence=earliest([...all,...accounts.map((a,i)=>({provider:a.provider+(accounts.length>1?' · 帳戶 '+(i+1):''),...a.evidence}))],'valid_until');
  $('time-preview').textContent=accounts.length>1?accounts.length+' 個帳戶 · 期限見詳情':account?.grants.length>1?account.grants.length+' 筆點數期限':grant?.expires?'點數 '+grant.expires.value+(grant.expires.precision==='date'?' · 僅日期':''):'展開五類時間';
  const rows=[
    ['配額重置',reset?date(reset.reset_at):'未提供',reset?reset.provider+' · '+(windows[reset.window]||reset.window):'依供應商視窗核對'],
    ['訂閱補充',account?date(account.benefit.renews_at):'未提供',accountScope+' · 取決於訂閱資格與續期'],
    ['點數實際到期',grantExpiryText(account),accountScope+(accounts.length>1?' · 其餘帳戶詳情各自展開':' · 逐筆 grant 詳情見帳戶')],
    ['資格複核',date(review),(snapshot.source.review_due_at?'共用來源':reviewTarget?.provider||'未知來源')+' · 本機期限，非點數到期'],
    ['餘額證據期限',evidence?date(evidence.valid_until):'未提供',(evidence?.provider||'未知來源')+' · '+(evidence?.current===false?'已過時；':'')+'非配額重置']
  ];
  rows.forEach(([label,value,detail])=>{const r=node('div',undefined,'time-item');const body=node('div');body.append(node('p',value),node('span',detail,'small'));r.append(node('p',label,'time-label'),body);box.append(r);});
}
function inventory(snapshot){
  if(Array.isArray(snapshot.models)&&snapshot.model_inventory?.state!=='unavailable')return snapshot.models;
  return snapshot.targets.map(t=>({...t,model_scope:'configured_gateway_target'}));
}
function showCatalog(show){
  catalogOpen=show;
  $('model-view-broker').setAttribute('aria-pressed',String(!show));
  $('model-view-catalog').setAttribute('aria-pressed',String(show));
  $('broker-models').hidden=show;$('provider-catalog').hidden=!show;
  $('pool-status').hidden=show;
  if(show)renderCatalog();
}
function sourceLink(url,label){
  const link=node('a',label);
  if(typeof url==='string'&&/^https:\/\//i.test(url))link.href=url;
  link.target='_blank';link.rel='noopener noreferrer';
  return link;
}
function renderCatalog(){
  const data=currentSnapshot?.provider_inventory;
  const providers=data?.providers||[];
  $('catalog-controls').hidden=!providers.length;
  if(!providers.length){
    $('catalog-summary').textContent='';$('catalog-count').textContent='';$('catalog-sources').replaceChildren();$('catalog-pages').hidden=true;
    $('catalog-models').replaceChildren(node('p',currentSnapshot?.source.kind==='demo'?'合成預覽僅顯示 Broker 範例。':'此版本的供應商型錄暫無資料。','empty panel'));
    return;
  }
  const variants=providers.flatMap(p=>p.catalogs.flatMap(c=>c.models.flatMap(m=>m.variants)));
  options('catalog-provider',providers.map(p=>p.id),'全部供應商',Object.fromEntries(providers.map(p=>[p.id,p.name])));
  options('catalog-task',[...variants.map(v=>v.task),...providers.flatMap(p=>p.apis.flatMap(a=>a.tasks))],'全部任務',catalogTaskNames);
  options('catalog-input',variants.flatMap(v=>v.inputs),'全部輸入',modalityNames);
  options('catalog-output',variants.flatMap(v=>v.outputs),'全部輸出',modalityNames);
  $('catalog-summary').textContent=providers.length+' 家供應商 · 盤點日期 '+data.audited_on;
  const selected=$('catalog-provider').value, task=$('catalog-task').value, input=$('catalog-input').value, output=$('catalog-output').value;
  const lifecycle=$('catalog-status').value||'current';
  const query=$('catalog-search').value.trim().toLowerCase();
  const scope=providers.filter(p=>!selected||p.id===selected);
  const sourceScope=selected+'\n'+task;
  if(catalogSourceScope!==sourceScope){renderCatalogSources(scope,Boolean(selected),task);catalogSourceScope=sourceScope;}
  const rows=[];
  for(const p of scope)for(const c of p.catalogs)for(const m of c.models){
    if(lifecycle==='current'&&['retired','deprecated','self_hosted'].includes(m.status))continue;
    if(lifecycle==='retired'&&m.status!=='retired')continue;
    const matching=m.variants.filter(v=>(!task||v.task===task)&&(!input||v.inputs.includes(input))&&(!output||v.outputs.includes(output)));
    const searchTerms=[m.id,p.name,p.id,...matching.flatMap(v=>[v.task,catalogTaskNames[v.task]])];
    if(matching.length&&searchTerms.join(' ').toLowerCase().includes(query))rows.push({p,c,m,matching});
  }
  const pages=Math.ceil(rows.length/catalogPageSize);
  catalogPage=Math.max(0,Math.min(catalogPage,pages-1));
  const start=catalogPage*catalogPageSize;
  $('catalog-count').textContent=rows.length?num(rows.length)+' 個型錄項目 · 顯示 '+num(start+1)+' 至 '+num(Math.min(rows.length,start+catalogPageSize)):'沒有符合條件的模型';
  $('catalog-pages').hidden=pages<=1;$('catalog-page').textContent='第 '+(catalogPage+1)+' / '+pages+' 頁';
  $('catalog-prev').disabled=catalogPage===0;$('catalog-next').disabled=catalogPage>=pages-1;
  if(!rows.length){$('catalog-models').replaceChildren(node('p','試著縮短搜尋詞或放寬篩選。各供應商的 API 與來源仍可查看。','empty panel'));return;}
  const table=node('div',undefined,'catalog-list');
  const heading=node('div',undefined,'catalog-row catalog-row-label');
  ['供應商／模型 ID','任務與模態','帳戶與 Broker'].forEach(label=>heading.append(node('span',label)));
  table.append(heading);
  const modelKey=(p,m)=>p+'\n'+m;
  const targets=new Map();
  for(const t of currentSnapshot.targets){const key=modelKey(t.provider,t.model);if(!targets.has(key))targets.set(key,[]);targets.get(key).push(t);}
  const registered=new Set(inventory(currentSnapshot).map(m=>modelKey(m.provider,m.model)));
  const openai=(currentSnapshot.accounts||[]).find(a=>a.provider==='openai');
  const openaiLimits=new Set((openai?.model_limits||[]).map(m=>m.model));
  rows.slice(start,start+catalogPageSize).forEach(({p,c,m,matching})=>{
    const row=node('article',undefined,'catalog-row');
    const identity=node('div');identity.append(node('p',p.name+' · '+(catalogStatus[m.status]||'已列出'),'small'),node('h4',m.id));
    if(m.note)identity.append(node('p',m.note,'small'));
    const capability=node('div');matching.forEach(v=>{
      const line=node('p',undefined,'catalog-task-line');
      line.append(node('strong',catalogTaskNames[v.task]||v.task),node('span',catalogModalities(v.inputs)+' → '+catalogModalities(v.outputs),'small'));capability.append(line);
    });
    const key=modelKey(p.id,m.id), routes=targets.get(key)||[];
    let account='帳戶存取未核對';
    if(c.basis==='key_catalog')account='金鑰清單曾列出 · 執行權限未驗證';
    if(p.id==='openai'&&openaiLimits.has(m.id))account='帳戶頁曾列模型上限 · 執行權限未驗證';
    const status=node('div');status.append(node('p',account,'small'));
    status.append(node('p',routes.length?[...new Set(routes.map(routeStatus))].join('、'):registered.has(key)?'Broker 已登錄 · 未配置路由':currentSnapshot.model_inventory?.state==='unavailable'?'Broker 登錄狀態未知':'Broker 未登錄','small'));
    status.append(sourceLink(m.source||c.source,c.label));
    row.append(identity,capability,status);table.append(row);
  });
  $('catalog-models').replaceChildren(table);
}
function catalogModalities(items){return items.map(value=>modalityNames[value]||value).join('、')||'未知';}
function renderCatalogSources(providers,open,task){
  $('catalog-sources').replaceChildren();
  providers.forEach(p=>{
    const detail=node('details',undefined,'catalog-source');detail.open=open;
    const apis=p.apis.filter(a=>!task||a.tasks.includes(task));
    detail.append(node('summary',p.name+' · '+apis.length+' 種 API／介面'));
    p.catalogs.forEach(c=>{
      const checked=c.checked_at.length>10?date(c.checked_at):c.checked_at;
      const line=node('p',undefined,'small');line.append(sourceLink(c.source,c.label),document.createTextNode(' · '+checked+' · '+c.coverage));detail.append(line);
    });
    const list=node('ul',undefined,'catalog-apis');apis.forEach(a=>{
      const item=node('li');item.append(sourceLink(a.source,a.name),node('code',a.interface));
      item.append(node('p',a.tasks.map(t=>catalogTaskNames[t]||t).join('、')+(a.note?' · '+a.note:''),'small'));list.append(item);
    });if(apis.length)detail.append(list);$('catalog-sources').append(detail);
  });
}
function runtimeState(t){
  const config=t.runtime_configuration;
  if(config?.state==='available')return config.enabled===true?'Broker 已啟用（不代表額度已確認）':config.enabled===false?'Broker 配置停用':'Broker 啟用狀態未知';
  if(config?.state==='not_configured')return 'Broker 未配置此模型';
  if(config?.state==='ambiguous')return '多條 Broker 配置，需指定帳戶';
  return 'Broker 啟用狀態未知';
}
function routeStatus(t){
  if(!t)return '未配置路由';
  if(currentSnapshot?.source.kind==='reviewed')return runtimeState(t);
  if(t.enabled===false)return '本機配置停用';
  if(evidenceComplete(t))return '可做任務檢查';
  if(t.observations.some(q=>q.current===false&&q.remaining!==null))return '餘額證據過時';
  return '餘額／資格待核對';
}
function modelRow(model,targets){
  const row=node('details',undefined,'model-row');
  const summary=node('summary',undefined,'model-row-head');
  const matching=targets.filter(t=>t.provider===model.provider&&t.model===model.model);
  const identity=node('div',undefined,'model-identity');identity.append(node('h3',model.model),node('p',matching.length?(currentSnapshot?.source.kind==='reviewed'?matching.length+' 份資格核對項目':'已配置 '+matching.length+' 條路由'):'僅登錄 · 未配置路由','small'));
  if(model.provider==='google'&&model.model==='gemini-2.5-flash-lite'){
    identity.append(node('p','舊版 · Google 限曾活躍使用的帳戶','small'));
  }
  const capability=node('div',undefined,'model-capabilities');capability.append(node('p',(model.capabilities||[]).map(c=>taskNames[c]||c).join('、')||'任務能力未知'),node('p','輸入 '+((model.input_modalities||[]).map(m=>modalityNames[m]||m).join('、')||'未知')+' → 輸出 '+((model.output_modalities||[]).map(m=>modalityNames[m]||m).join('、')||'未知'),'small'));
  const quota=node('div',undefined,'model-quota');
  const observationRows=matching.flatMap(t=>(t.observations||[]));
  if(!observationRows.length)quota.append(node('p','— 剩餘未知','unknown'));
  observationRows.forEach(q=>{const current=q.current&&q.effective_remaining!==null&&q.effective_remaining!==undefined;quota.append(node('p',(current?num(q.effective_remaining):'—')+' '+(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window),current?'':'unknown'));if(!current&&q.remaining!==null&&q.remaining!==undefined)quota.append(node('p','歷史觀測 '+num(q.remaining)+' · 已過時','small'));});
  const limits=node('div',undefined,'model-limits');
  const limitRows=matching.flatMap(t=>t.provider_limits||[]);
  if(!limitRows.length)limits.append(node('p','供應商上限未知','small'));
  limitRows.forEach(q=>limits.append(node('p',num(q.value)+' '+(metrics[q.metric]||q.metric)+' / '+(windows[q.window]||q.window)+(q.current?'':' · 已過時或未知'),'small')));
  const deadline=observationRows.map(q=>q.valid_until).filter(Boolean).sort()[0];
  limits.append(node('p','餘額證據至 '+date(deadline),'small'));
  const state=node('span',matching.map(routeStatus).join('；')||'未配置路由','state'+(matching.some(evidenceComplete)?'':' warning'));
  summary.append(identity,capability,quota,limits,state);row.append(summary);
  const details=node('div',undefined,'model-detail');
  const catalogStates={listed:'精確 ID 已列出',inactive:'清單標為停用或退役',not_listed:'完整清單未列出此 ID',unknown:'清單不完整，狀態未知',documented:'官方文件列出'};
  matching.forEach(t=>{
    const observation=t.catalog_observation;
    if(observation)labelPair(details,'供應商模型清單',(observation.current?catalogStates[observation.status]:'觀測已過期')+' · '+date(observation.checked_at));
  });
  details.append(node('p','能力來源：此 Gateway 的 Registry 登錄；不是供應商完整型錄或帳戶存取保證。','small'));
  details.append(node('p','Context '+num(model.context_tokens)+' tokens · 模型輸出上限 '+num(model.max_output_tokens)+' tokens · 功能 '+((model.features||[]).join('、')||'未登錄'),'small'));
  if(!matching.length)details.append(node('p','此模型未配置執行路由；帳戶資格、剩餘、上限與期限未知。不沿用同廠商其他模型的額度。','empty'));
  matching.forEach(t=>{const evidence=providerRow(t);details.append(evidence.children[1]);});
  row.append(details);return row;
}
function renderModels(){
  if(!currentSnapshot)return;
  const all=inventory(currentSnapshot),provider=$('model-provider').value,task=$('model-task').value,input=$('model-input').value;
  const filtered=all.filter(m=>(!provider||m.provider===provider)&&(!task||(m.capabilities||[]).includes(task))&&(!input||(m.input_modalities||[]).includes(input)));
  $('providers').replaceChildren();
  const providers=[...new Set(filtered.map(m=>m.provider))];
  providers.forEach((provider,index)=>{
    const models=filtered.filter(m=>m.provider===provider),targets=currentSnapshot.targets.filter(t=>t.provider===provider);
    const group=node('details',undefined,'provider-group');group.open=index===0||Boolean($('model-provider').value||task||input);
    const summary=node('summary',undefined,'provider-group-heading');
    const configured=models.filter(m=>m.model_scope==='configured_gateway_target');
    const summaryModels=configured.length?configured:models;
    const title=node('div');title.append(node('h3',provider),node('p',summaryModels.slice(0,2).map(m=>m.model).join('、')+(summaryModels.length>2?' …':'')+(configured.length?'':' · 僅登錄'),'small'));
    const scope=node('div',undefined,'provider-scope');scope.append(node('p',models.length+(currentSnapshot.model_inventory?.state==='unavailable'?' 個已配置模型 · ':' 個登錄模型 · ')+targets.length+(currentSnapshot.source.kind==='reviewed'?' 份資格核對項目':' 條已配置路由')));
    const now=Date.parse(currentSnapshot.source.fetched_at);
    const eligible=targets.filter(t=>t.free_eligible===true&&t.billing_enabled===false&&t.billing_state!=='unknown'&&Date.parse(t.free_verified_at)<=now&&now<Date.parse(t.free_evidence_expires_at)).length;
    scope.append(node('p','帳戶資格：'+(targets.length?eligible+' / '+targets.length+' 條有有效免費資格證據':'尚無路由可核對')+' · 配置與資格分開','small'));summary.append(title,scope);group.append(summary);
    const heading=node('div',undefined,'model-table-heading');['模型／配置','任務與輸入 → 輸出','保守剩餘（分視窗）','供應商上限／證據期限','目前狀態'].forEach(text=>heading.append(node('span',text)));
    group.append(heading);
    [...configured,...models.filter(m=>m.model_scope!=='configured_gateway_target')].forEach(m=>group.append(modelRow(m,currentSnapshot.targets)));
    $('providers').append(group);
  });
  const available=currentSnapshot.model_inventory?.state!=='unavailable';
  $('model-count').textContent=filtered.length+' / '+all.length+(available?' 個登錄模型':' 個已配置模型 · 完整登錄清單不可用');
  if(!filtered.length)$('providers').append(node('p',available?'沒有符合條件的登錄模型；不代表供應商沒有其他模型。':'模型清單來源不可用；只能核對已配置路由。','empty panel'));
}
function populateModelChoices(){
  if(!currentSnapshot)return;
  const provider=$('provider').value;
  options('model',currentSnapshot.targets.filter(t=>!provider||t.provider===provider).map(t=>t.model),'不限模型');
  updateAdviceCompatibility();
}
function updateAdviceCompatibility(){
  const translation=$('task-kind').value==='translation';$('translation-fields').hidden=!translation;
  const supported=$('input-modality').value==='text'&&($('output-modality').value==='text'||(!translation&&$('output-modality').value==='json'));
  $('advice-submit').disabled=!supported;
  if(!supported){
    const input=$('input-modality').value,output=$('output-modality').value;
    const matches=currentSnapshot?inventory(currentSnapshot).filter(m=>(m.input_modalities||[]).includes(input)&&(m.output_modalities||[]).includes(output)):[];
    $('advice-compatibility').textContent='此表單只評估文字生成／翻譯；媒體需求需使用 Gateway typed Task API。'+(matches.length?'登錄清單有 '+matches.length+' 個模態相容模型；這不是可執行推薦。':'目前沒有已確認相容的登錄模型。');
  }else $('advice-compatibility').textContent=$('output-modality').value==='json'?'JSON 以真實 json_output 功能需求交給 Broker 檢查。':'送出後只取得 Broker 建議，不會呼叫模型。';
}
function renderRecent(snapshot){
  const names={completed:'已完成',completed_usage_unknown:'已完成・用量未知',unknown:'結果未知',pending:'等待中',running:'執行中',failed:'失敗',cancelled:'已取消'};
  $('recent').replaceChildren();(snapshot.recent||[]).forEach(r=>{const row=node('div',undefined,'activity');const who=node('div');who.append(node('p',(r.provider||'未選路由')+' · '+(names[r.state]||r.state||'未知')),node('p',r.model||'—','model'));const timing=node('div');timing.append(node('p',date(r.created_at),'small'));if(r.completed_at)timing.append(node('p','完成 '+date(r.completed_at),'small'));if(r.latency_ms!==null&&r.latency_ms!==undefined)timing.append(node('p',num(r.latency_ms)+' ms','small'));row.append(who,timing);$('recent').append(row);});
  if(snapshot.recent===null)$('recent').append(node('p','工作紀錄來源不可用；目前狀態未知。','empty'));
  else if(!snapshot.recent.length)$('recent').append(node('p','尚無工作紀錄。檢查路由不會建立工作。','empty'));
}
function renderUsage(report){
  $('usage-filter-note').textContent=report.task_metadata?.state==='available_for_new_records'?'新紀錄依實際任務能力分類；歷史未記錄類別的嘗試保留為「類別未記錄」。':'此來源的歷史任務類別未記錄；指定任務可能無法統計，未知不視為零用量。';
  $('usage').replaceChildren();$('usage-totals').replaceChildren();
  $('activity-source').textContent=report.source.label+' · '+(report.source.state==='available'?'本機讀取 '+date(report.source.as_of):'來源未接入或所選條件無法統計');
  $('usage-coverage').textContent='送出時間：'+(report.filters?.from?date(report.filters.from):'最早紀錄')+' 至 '+(report.filters?.to?date(report.filters.to)+'（不含）':'最新紀錄')+' · 僅此帳本已記錄的嘗試';
  const rows=report.rows;
  if(rows===null||!Array.isArray(rows)){$('usage-status').textContent='用量未知';$('usage').append(node('p','用量來源未接入、無法讀取或任務類別未記錄；實際帳戶消耗未知，不顯示為零。','empty'));return;}
  $('usage-status').textContent=rows.length+' 組供應商／模型／任務';
  if(!rows.length){$('usage').append(node('p','所選條件沒有本機生成嘗試；不代表整個供應商帳戶用量為零。','empty'));return;}
  const measures=[['requests','生成嘗試','requests'],['reported_input_tokens','已回報輸入','input tokens'],['reported_output_tokens','已回報輸出','output tokens'],['reported_total_tokens','已回報總 tokens','total tokens'],['reported_neurons','已回報運算量','neurons'],['reported_audio_seconds','已回報音訊','秒'],['reported_images','已回報圖片','張圖片'],['reported_pages','已回報文件','頁'],['reported_conversions','已回報轉換','次轉換']];
  measures.forEach(([key,label,unit])=>{const known=rows.every(r=>r[key]!==null&&r[key]!==undefined),unknownField=usageUnknownFields[key],complete=known&&(!unknownField||rows.every(r=>r[unknownField]===0)),value=complete?rows.reduce((sum,r)=>sum+r[key],0):null;const item=node('div');item.append(node('span',label,'small'),node('p',num(value),'total-value'),node('span',unit+(!known?' · 含未回報值':!complete?' · 部分回報或完整性未知':''),'small'));$('usage-totals').append(item);});
  const table=node('table',undefined,'usage-table');table.setAttribute('aria-label','篩選範圍的本機用量');
  const head=node('thead'),header=node('tr');['供應商／模型','任務',...measures.map(m=>m[0]==='requests'?m[2]:'已回報 '+m[2]),'結果未知'].forEach((text,i)=>header.append(node('th',text,i>1?'num':'')));head.append(header);table.append(head);
  const body=node('tbody');rows.forEach(r=>{const row=node('tr');const who=node('td');who.append(node('p',r.provider),node('p',r.model,'model'));row.append(who,node('td',taskNames[r.task_type]||'類別未記錄','small'));[...measures.map(m=>m[0]),'outcome_unknown_count'].forEach(key=>{const unknownField=usageUnknownFields[key],present=r[key]!==null&&r[key]!==undefined;const suffix=!unknownField||!present||r[unknownField]===0?'':r[unknownField]>0?'（部分）':'（完整性未知）';row.append(node('td',num(r[key])+suffix,'num'));});body.append(row);});table.append(body);
  const scroll=node('div',undefined,'table-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label','用量明細，可水平捲動');scroll.append(table);$('usage').append(scroll);
}
function populateUsageModels(){
  const provider=$('usage-provider').value;
  const models=[...inventory(currentSnapshot),...(currentSnapshot.usage||[])];
  options('usage-model',models.filter(row=>!provider||row.provider===provider).map(row=>row.model),'全部模型');
}
function usageQuery(){
  const values={},period=$('usage-period').value,now=new Date();
  if(period==='custom'){
    const start=$('usage-from').value,end=$('usage-to').value;
    if(!start||!end||start>end)throw new Error('invalid_dates');
    const from=new Date(start+'T00:00:00'),to=new Date(end+'T00:00:00');to.setDate(to.getDate()+1);values.from=from.toISOString();values.to=to.toISOString();
  }else if(period!=='all'){
    const from=new Date(now);if(period==='today')from.setHours(0,0,0,0);else from.setDate(from.getDate()-Number(period));values.from=from.toISOString();values.to=now.toISOString();
  }
  if($('usage-provider').value)values.provider=$('usage-provider').value;
  if($('usage-model').value)values.model=$('usage-model').value;
  if($('usage-task').value)values.task=$('usage-task').value;
  return Object.entries(values).map(([key,value])=>encodeURIComponent(key)+'='+encodeURIComponent(value)).join('&');
}
async function loadUsage(){
  if(!currentSnapshot)return;
  let query;try{query=usageQuery();}catch(error){$('usage-status').textContent='請選有效的起訖日期';return;}
  const version=++usageVersion,session=sessionVersion;usageLoaded=true;$('usage-status').textContent='讀取完整本機帳本…';$('usage').replaceChildren(node('p','正在依篩選條件統計…','empty'));$('usage-totals').replaceChildren();
  try{const report=await api('/api/usage'+(query?'?'+query:''));if(version!==usageVersion||session!==sessionVersion)return;renderUsage(report);}catch(error){if(version!==usageVersion||session!==sessionVersion)return;if(error.status===401){showLogin();return;}$('usage-status').textContent='用量未知';$('usage').replaceChildren(node('p','無法取得本機用量，請重試。未使用最近工作推算完整歷史。','empty'));}
}
function render(snapshot){
  invalidateMatch();
  currentSnapshot=snapshot;usageVersion++;usageLoaded=false;
  renderAccounts(snapshot);renderRecent(snapshot);renderTimeOverview(snapshot);
  $('login-panel').hidden=true;$('workspace').hidden=false;$('refresh').hidden=false;$('logout').hidden=false;$('error').hidden=true;
  $('demo-notice').hidden=snapshot.source.kind!=='demo';
  $('source').textContent=snapshot.source.label+' · 本機重讀 '+date(snapshot.source.fetched_at);
  const detail=$('source-detail');detail.replaceChildren();labelPair(detail,'來源',snapshot.source.label);labelPair(detail,'資料快照',date(snapshot.source.as_of));
  if(snapshot.source.kind==='reviewed'){labelPair(detail,'人工核對時段',date(snapshot.source.audit_session_at)+'（共用稽核時鐘，非各帳戶精確觀測時間）');labelPair(detail,'資格本機複核期限',date(snapshot.source.review_due_at)+'（非點數到期）');}
  labelPair(detail,'本機取得時間',date(snapshot.source.fetched_at));detail.append(node('p','重讀本機資料不會查詢供應商或延長原始證據期限。'));
  const ready=snapshot.targets.filter(evidenceComplete).length;
  $('evidence-brief').replaceChildren(node('span',String(ready)),node('div',undefined));const brief=$('evidence-brief').children[1];brief.append(node('p',ready?'條路由的餘額證據完整':'目前沒有完整的路由餘額證據'),node('span','配置、能力、帳戶資格與剩餘額度各自核對。','small'));
  $('usage-totals').replaceChildren();$('usage-coverage').textContent='';$('usage-status').textContent='尚未套用篩選';$('usage').replaceChildren(node('p',snapshot.usage===null?'用量來源未接入或無法讀取；實際帳戶消耗未知。':'開啟總用量後，依完整本機帳本統計；不以最近工作推算。','empty'));
  if(snapshot.source.kind==='reviewed')brief.append(node('span','本機核對快照不執行；線上服務的啟用狀態未在此查核。','small'));
  const models=inventory(snapshot);$('pool-status').textContent=models.length+(snapshot.model_inventory?.state==='unavailable'?' 個已配置模型 · 登錄清單不可用 · ':' 個登錄模型 · ')+snapshot.targets.length+(snapshot.source.kind==='reviewed'?' 份資格核對項目':' 條配置路由');
  options('provider',snapshot.targets.map(t=>t.provider),'不限供應商');options('usage-task',usageTaskTypes,'全部類別',taskNames);options('usage-provider',[...snapshot.targets.map(t=>t.provider),...(snapshot.usage||[]).map(u=>u.provider)],'全部供應商');
  options('model-provider',models.map(m=>m.provider),'全部供應商');options('model-task',models.flatMap(m=>m.capabilities||[]),'全部任務',taskNames);options('model-input',models.flatMap(m=>m.input_modalities||[]),'全部輸入',modalityNames);
  populateUsageModels();populateModelChoices();renderModels();selectTab(activeTab);
  catalogSourceScope=null;if(catalogOpen)renderCatalog();
  populateMatchChoices();
}
async function refresh(){
  if(pending)return;invalidateAdvice();const version=sessionVersion;pending=true;$('refresh').disabled=true;$('refresh').textContent='讀取中…';
  try{const snapshot=await api('/api/snapshot');if(version!==sessionVersion)return;render(snapshot);}catch(error){if(version!==sessionVersion)return;if(error.status===401)showLogin();else{clearWorkspace();$('workspace').hidden=true;$('login-panel').hidden=true;$('refresh').hidden=false;$('error').hidden=false;$('source').textContent='資料來源無法使用 · 未顯示舊讀值';}}finally{if(version===sessionVersion){pending=false;$('refresh').disabled=false;$('refresh').textContent='重讀本機資料';}}
}
['models','usage','advice'].forEach((name,index)=>{
  $('tab-'+name).addEventListener('click',()=>selectTab(name));
  $('tab-'+name).addEventListener('keydown',event=>{const names=['models','usage','advice'];let next;if(event.key==='ArrowRight')next=names[(index+1)%3];if(event.key==='ArrowLeft')next=names[(index+2)%3];if(event.key==='Home')next=names[0];if(event.key==='End')next=names[2];if(next){event.preventDefault();selectTab(next,true);}});
});
['model-provider','model-task','model-input'].forEach(id=>$(id).addEventListener('change',renderModels));
$('model-view-broker').addEventListener('click',()=>showCatalog(false));
$('model-view-catalog').addEventListener('click',()=>showCatalog(true));
['catalog-provider','catalog-task','catalog-input','catalog-output','catalog-status','catalog-search'].forEach(id=>$(id).addEventListener(id==='catalog-search'?'input':'change',()=>{catalogPage=0;renderCatalog();}));
$('catalog-prev').addEventListener('click',()=>{catalogPage--;renderCatalog();});
$('catalog-next').addEventListener('click',()=>{catalogPage++;renderCatalog();});
$('provider').addEventListener('change',populateModelChoices);
$('advice-form').addEventListener('input',()=>{invalidateAdvice();updateAdviceCompatibility();});
$('advice-form').addEventListener('change',()=>{invalidateAdvice();updateAdviceCompatibility();});
$('usage-provider').addEventListener('change',populateUsageModels);
$('usage-period').addEventListener('change',()=>{$('usage-dates').hidden=$('usage-period').value!=='custom';});
$('usage-form').addEventListener('submit',event=>{event.preventDefault();loadUsage();});
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;const version=++sessionVersion;pending=false;button.disabled=true;$('login-error').textContent='';try{await api('/api/login',{token:$('login-token').value});if(version!==sessionVersion)return;$('login-token').value='';await refresh();}catch(error){if(version!==sessionVersion)return;$('login-error').textContent=error.status===429?'登入嘗試過多，請稍後再試。':'登入失敗，請核對 Quota 網站密碼。';}finally{button.disabled=false;}});
$('refresh').addEventListener('click',refresh);
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{});showLogin();$('source').textContent='已登出。';}catch(error){$('source').textContent='登出失敗，請稍後再試。';}});
$('advice-form').addEventListener('submit',async event=>{
  event.preventDefault();if($('advice-submit').disabled)return;
  const version=++taskVersion;hasAdvice=true;const button=event.submitter;button.disabled=true;button.textContent='檢查中…';$('advice-result').replaceChildren(node('p','正在核對任務與證據…','empty'));
  try{
    const task={request_key:'advice-'+crypto.randomUUID(),capability:$('task-kind').value,input:$('task-input').value,max_output_tokens:Number($('output').value)};
    if(task.capability==='translation'){task.source_language=$('source-language').value;task.target_language=$('target-language').value;}
    if($('output-modality').value==='json'){task.input={messages:[{role:'user',content:$('task-input').value}]};task.options={response_format:'json_object'};task.requirements={features:['json_output']};}
    if($('provider').value)task.provider=$('provider').value;if($('model').value)task.model=$('model').value;
    const data=await api('/api/advice',task);if(version!==taskVersion)return;
    const result=node('div',undefined,data.decision==='recommend'?'result-recommend':'result-blocked');result.append(node('h3',data.decision==='recommend'?'可建議此免費路由':'目前沒有可建議的免費路由'));
    if(data.selected_target){result.append(node('p',data.selected_target.provider+' · '+data.selected_target.model),node('p','本次任務通過免費資格、憑證清單與額度檢查；建議沒有送出任務。','small'));}
    (data.candidates||[]).forEach(candidate=>{const item=node('div',undefined,'candidate');item.append(node('p',candidate.provider+' · '+candidate.model));item.append(node('p',candidate.reasons.length?candidate.reasons.map(reason).join('；'):data.selected_target?.target_id===candidate.target_id?'本次建議路由':'符合檢查，依 Broker 順序候選','small'));if(candidate.ranking){const rank=candidate.ranking;if(rank.value_loss_basis==='credit_expiry')item.append(node('p','排序因素：已核對 credit 較早到期；約 '+num(rank.value_loss_seconds)+' 秒後到期。','small'));else if(rank.value_loss_basis==='quota_reset')item.append(node('p','排序因素：已核對下次額度重置；約 '+num(rank.value_loss_seconds)+' 秒後重置。','small'));if(rank.observed_headroom!==null && rank.observed_headroom!==undefined)item.append(node('p','觀測額度最小可用比例 '+new Intl.NumberFormat('zh-TW',{style:'percent',maximumFractionDigits:1}).format(rank.observed_headroom)+'；相同時間因素再比較餘裕、健康度與負載。','small'));}result.append(item);});
    if(data.recheck_at)result.append(node('p','估計可重新核對：'+date(data.recheck_at)+'。不會自動排程，也不保證配額已恢復。','small'));
    result.append(node('p','資料：'+data.source.label+' · '+date(data.source.as_of),'small'));$('advice-result').replaceChildren(result);
  }catch(error){if(version!==taskVersion)return;if(error.status===401){showLogin();return;}$('advice-result').replaceChildren(node('p',error.message==='invalid_task'?'任務格式、功能或輸出上限不符目前契約，請修正後再試。':'無法取得建議。資料來源未確認，請重新讀取後再試。','result-error'));}finally{if(version===taskVersion){button.textContent='檢查免費路由';updateAdviceCompatibility();}}
});
let matchVersion=0,matchResult=null,matchPage=0;
const matchReasons={route_not_configured:'尚未配置此模型的執行路由',task_cost_not_checked:'本次任務成本尚未核對',quota_not_verified_for_task:'本次任務的免費餘額尚未核對',features_unverified:'所需功能尚未核對',free_route_not_verified:'免費路由未通過完整檢查',context_limit:'這次輸入超過模型上下文上限',output_limit:'這次輸出需求超過模型上限',adapter_admission:'這次任務不符合 API 格式或限制'};
function invalidateMatch(){
  matchVersion++;matchResult=null;matchPage=0;
  $('match-result').replaceChildren(node('p','需求或資料已變更，請重新配對。','empty'));
  $('match-submit').disabled=false;$('match-submit').textContent='配對模型與額度';
}
function populateMatchChoices(){
  const providers=currentSnapshot?.provider_inventory?.providers||[],models=inventory(currentSnapshot);
  const variants=providers.flatMap(p=>p.catalogs.flatMap(c=>c.models.flatMap(m=>m.variants)));
  options('match-task',[...variants.map(v=>v.task),...models.flatMap(m=>m.capabilities||[])].filter(v=>v!=='unknown'),'選擇任務',{...taskNames,...catalogTaskNames});
  options('match-input',[...variants.flatMap(v=>v.inputs),...models.flatMap(m=>m.input_modalities||[])],'選擇輸入',modalityNames);
  options('match-output',[...variants.flatMap(v=>v.outputs),...models.flatMap(m=>m.output_modalities||[]),'json'],'選擇輸出',modalityNames);
  options('match-provider',[...providers.map(p=>p.id),...models.map(m=>m.provider)],'不限供應商');
  for(const [id,value] of [['match-task','text_generation'],['match-input','text'],['match-output','text']])if(!$(id).value)$(id).value=value;
  syncMatchModalities();
}
function syncMatchModalities(){
  const provider=$('match-provider').value,task=$('match-task').value;
  const variants=(currentSnapshot?.provider_inventory?.providers||[]).filter(p=>!provider||p.id===provider)
    .flatMap(p=>p.catalogs.flatMap(c=>c.models.filter(m=>!['retired','deprecated','self_hosted'].includes(m.status)).flatMap(m=>m.variants)));
  variants.push(...inventory(currentSnapshot).filter(m=>!provider||m.provider===provider).flatMap(m=>m.variants||[]));
  if(!variants.length)return;
  const matching=variants.filter(v=>v.task===task);
  const inputs=[...new Set(matching.flatMap(v=>v.inputs))].sort();
  options('match-input',inputs,'選擇輸入',modalityNames);
  if(!$('match-input').value)$('match-input').value=inputs.includes('text')?'text':inputs[0]||'';
  const outputs=[...new Set(matching.filter(v=>v.inputs.includes($('match-input').value)).flatMap(v=>v.outputs))].sort();
  if(task==='text_generation'&&outputs.includes('text')&&!outputs.includes('json'))outputs.push('json');
  options('match-output',outputs,'選擇輸出',modalityNames);
  if(!$('match-output').value)$('match-output').value=outputs.includes('text')?'text':outputs[0]||'';
}
for(const id of ['match-task','match-input','match-provider'])$(id).addEventListener('change',syncMatchModalities);
function matchCandidate(candidate,compact=false){
  const item=node('div',undefined,'candidate');
  item.append(node('p',candidate.provider+' · '+candidate.model,'model'));
  item.append(node('p',candidate.capability_verified===false?'此任務的額外需求未通過檢查':candidate.access_requires_confirmation?'能力相符，模型存取資格需申請':'適合此任務 · 輸入／輸出能力相符','small'));
  if(candidate.capability_blockers?.length)item.append(node('p',candidate.capability_blockers.map(r=>matchReasons[r]||reason(r)).join('；'),'small'));
  if(candidate.safe)item.append(node('p','本次任務的免費資格與額度已通過檢查。','small'));
  else if(candidate.free_plan_verified)item.append(node('p',candidate.free_plan_basis==='account_plan'?'帳戶免費方案有有效核對證據；免費剩餘仍需確認。':'有有效免費資格證據；免費剩餘仍需確認。','small'));
  else item.append(node('p','免費資格或證據時效待確認；不代表模型不適合此任務。','small'));
  const rank=candidate.ranking||{};
  if(rank.value_loss_seconds!==null&&rank.value_loss_seconds!==undefined)item.append(node('p',(rank.value_loss_basis==='quota_reset'?'已核對重置：約 ':'已核對到期：約 ')+num(Math.ceil(rank.value_loss_seconds/60))+' 分鐘後','small'));
  if(rank.observed_headroom!==null&&rank.observed_headroom!==undefined)item.append(node('p','各額度中最低可用比例：'+new Intl.NumberFormat('zh-TW',{style:'percent',maximumFractionDigits:1}).format(rank.observed_headroom),'small'));
  if(!candidate.remaining?.length)item.append(node('p','免費剩餘：未知 · 本次可用額度：未知','small'));
  else for(const q of candidate.remaining)item.append(node('p',(metrics[q.metric]||q.metric)+' · '+(windows[q.window]||q.window)+'：'+(q.current&&q.effective_remaining!==null?num(q.effective_remaining):'未知'),'small'));
  if(candidate.documentation_url)item.append(sourceLink(candidate.documentation_url,'官方 API 文件 ↗'));
  if(!compact){
    const details=node('details'),summary=node('summary','自動執行前還需要什麼？');details.append(summary);
    if(candidate.execution_status==='reviewed_snapshot')details.append(node('p','目前讀取的是帳戶核對快照，並非線上 Broker 的啟用狀態；需先接上即時資格與額度證據。','small'));
    const reasons=(candidate.reasons||[]).filter(r=>!(candidate.execution_status==='reviewed_snapshot'&&['disabled','gateway_blocked'].includes(r)));
    if(reasons.length)details.append(node('p',reasons.map(r=>matchReasons[r]||reason(r)).join('；'),'small'));
    if(reasons.includes('route_not_configured'))details.append(node('p','可先依官方 API 文件選定模型，再配置對應路由；不會因為選用就啟用付費。','small'));
    if(reasons.includes('task_cost_not_checked'))details.append(node('p','若要核對單次文字任務額度，可展開上方「加入實際文字」；留空仍可選模型。','small'));
    details.append(node('p','未知或不足時不會自動呼叫，也不會自動付費。','small'));item.append(details);
  }
  return item;
}
function renderMatchResult(){
  if(!matchResult)return;
  const data=matchResult,root=$('match-result');root.replaceChildren();
  const chosen=data.recommended_model||data.selected_target;
  root.append(node('h3',chosen?'優先考慮：'+chosen.provider+' · '+chosen.model:'目前沒有已確認符合完整需求的模型'));
  root.append(node('p',num(data.matched_models)+' 個模型符合任務與模態。先比較任務能力，優先列出已有免費方案證據的模型；已確認可用的免費路由，再依到期／重置時間與餘額排序。','section-note'));
  root.append(node('p',data.selected_target?'已有通過本次免費額度檢查的路由；此頁仍只提供建議，不會自動呼叫。':'你可以先選模型；目前沒有通過完整免費額度檢查的路由，不會自動呼叫或付費。','section-note'));
  if(!data.candidates?.length){root.append(node('p','目前型錄沒有同時符合這些需求的模型；可調整任務或輸入／輸出。','empty'));return;}
  if(data.shortlist?.length){
    const shortlist=node('section',undefined,'match-shortlist');shortlist.append(node('h4','先比較這幾個選項'));
    data.shortlist.forEach(candidate=>shortlist.append(matchCandidate(candidate,true)));root.append(shortlist);
  }
  root.append(node('h4','全部相符模型與接入條件'));
  const rows=data.candidates.slice(matchPage*20,(matchPage+1)*20);
  for(const candidate of rows)root.append(matchCandidate(candidate));
  if(data.candidates.length>20){
    const paging=node('div',undefined,'catalog-pagination'),previous=node('button','上一頁'),next=node('button','下一頁');
    previous.type=next.type='button';previous.disabled=matchPage===0;next.disabled=(matchPage+1)*20>=data.candidates.length;
    previous.addEventListener('click',()=>{matchPage--;renderMatchResult();});next.addEventListener('click',()=>{matchPage++;renderMatchResult();});
    paging.append(previous,node('span',(matchPage+1)+' / '+Math.ceil(data.candidates.length/20)),next);root.append(paging);
  }
  root.append(node('p','推薦依能力與證據完整度排序，不是模型品質評測。資料：'+data.source.label+' · '+date(data.source.as_of),'section-note'));
}
for(const event of ['input','change'])$('match-form').addEventListener(event,()=>{
  invalidateMatch();$('match-text-options').hidden=!($('match-task').value==='text_generation'&&$('match-input').value==='text'&&['text','json'].includes($('match-output').value));
});
$('match-form').addEventListener('submit',async event=>{
  event.preventDefault();const version=++matchVersion,session=sessionVersion;
  $('match-submit').disabled=true;$('match-submit').textContent='配對中…';$('match-result').replaceChildren(node('p','正在比對能力與免費額度證據…','empty'));
  try{
    const data={mode:'match',task:$('match-task').value,inputs:[$('match-input').value],outputs:[$('match-output').value]};
    if($('match-provider').value)data.provider=$('match-provider').value;
    if(data.task==='text_generation'&&data.inputs[0]==='text'&&['text','json'].includes(data.outputs[0])&&$('match-text').value.trim()){
      data.route_task={request_key:'match-'+crypto.randomUUID(),capability:'text_generation',input:$('match-text').value,max_output_tokens:Number($('match-limit').value)};
      if(data.provider)data.route_task.provider=data.provider;
      if(data.outputs[0]==='json'){
        data.outputs=['json'];data.features=['json_output'];
        data.route_task.input={messages:[{role:'user',content:$('match-text').value}]};data.route_task.options={response_format:'json_object'};data.route_task.requirements={features:['json_output']};
      }
    }
    const result=await api('/api/advice',data);if(version!==matchVersion||session!==sessionVersion)return;
    matchResult=result;matchPage=0;renderMatchResult();
  }catch(error){
    if(version!==matchVersion||session!==sessionVersion)return;
    if(error.status===401){showLogin();return;}
    $('match-result').replaceChildren(node('p',error.message==='invalid_task'?'請核對任務、輸入／輸出與 token 上限；尚未取得推薦。':'配對資料無法取得，已停止推薦。請重讀本機資料。','result-error'));
  }finally{if(version===matchVersion&&session===sessionVersion){$('match-submit').disabled=false;$('match-submit').textContent='配對模型與額度';}}
});
refresh();
