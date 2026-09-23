'use strict';
let state = {accounts: [], service_running:false, config: {enabled_providers: []}};
let bridge, callMode='text', lastText='', lastVideoId='';
let modelFilter='all';
const cloudMode=new URLSearchParams(location.search).get('source')==='cloud';
const $ = id => document.getElementById(id);
const statuses = {missing:'未找到登录', detected:'凭据待验证', expired:'登录已过期', verified:'官方验证成功', rejected:'官方拒绝授权', unverified:'尚未验证'};
const sources = {local_config:'本机配置', adapter_catalog:'适配器候选', official_api:'官方接口',cloud_api:'云端接口'};
const availability = {unverified:'目录候选 · 尚未调用', listed:'官方已列出', call_verified:'调用成功',cloud_listed:'云端已列出',unavailable:'官方接口拒绝调用'};
function notice(message, error=false) { $('notice').textContent = message; $('notice').classList.toggle('error',error); }
async function api(path, data, method='POST') {
  const result = await bridge.request(path,data??null,method);
  if (!result.ok) throw new Error(result.error || '接口调用失败');
  return result.data;
}
function element(tag, text, className) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(className) node.className=className; return node; }
async function copy(text) { const result=await bridge.copy(text);notice(result.ok?'已复制。':result.error,!result.ok); }
function snippet() {
  const model=$('test-model').value || $('default-model').value || 'grok/官方模型ID';
  $('snippet').textContent=`model_provider = "minking_local"\nmodel = ${JSON.stringify(model)}\n\n[model_providers.minking_local]\nname = "MinKing Local"\nbase_url = ${JSON.stringify(state.base_url || '')}\nwire_api = "responses"\nenv_key = "MINKING_LOCAL_API_KEY"`;
}
function renderAccounts() {
  if(cloudMode)return;
  $('account-grid').replaceChildren();
  for (const a of state.accounts) {
    const card=element('article',undefined,'account');
    const top=element('div',undefined,'account-top'); const brand=element('div',undefined,'account-brand');
    const icon=element('img',undefined,'platform-icon');icon.src='../icons/'+({codex:'openai',grok:'grok',claude_code:'claude',workbuddy:'codebuddy',antigravity:'antigravity'}[a.id])+'.svg';icon.alt='';brand.append(icon,element('h3',a.name));
    const toggle=element('label',undefined,'switch-label'); const check=element('input');check.type='checkbox';check.checked=state.config.enabled_providers.includes(a.id);check.setAttribute('aria-label',`启用 ${a.name}`);
    check.addEventListener('change',()=>{state.config.enabled_providers=state.config.enabled_providers.filter(p=>p!==a.id);if(check.checked)state.config.enabled_providers.push(a.id);notice('平台选择已变更，点击「保存本地配置」生效。');});
    toggle.append(check,document.createTextNode('启用'));top.append(brand,toggle);
    const bottom=element('div',undefined,'account-bottom');bottom.append(element('span',a.detected?'已检测到本机资料':'未检测到本机资料'));
    const button=element('button','重试同步','probe');button.hidden=a.status==='verified';button.disabled=!state.service_running||a.status==='missing'||a.status==='expired';
    button.addEventListener('click',async()=>{button.disabled=true;button.textContent='验证中…';try {const result=await api(`/api/providers/${a.id}/probe`,{});state.accounts=state.accounts.map(v=>v.id===a.id?result:v);notice(`${a.name} 官方状态验证成功。`);}catch(e){notice(e.message,true);await reload(false);}finally{render();}});
    bottom.append(button);card.append(top,element('span',statuses[a.status]||a.status,`status ${a.status}`),element('p',a.detail,'account-detail'),bottom);if(a.verified_at)card.append(element('p','同步时间 '+window.parent.formatTime(a.verified_at),'small muted'));$('account-grid').append(card);
  }
  $('account-count').textContent=`${state.accounts.filter(a=>a.status==='verified').length} 个已验证 · ${state.accounts.length} 个平台`;
}
function pricingCells(pricing, kind) {
  const sell = (pricing && pricing.sell) || {};
  const official = (pricing && pricing.official) || {};
  const rate = (pricing && pricing.multiplier) || '—';
  if (kind === 'image') return [rate, official.usd_per_image ? `$${official.usd_per_image} / 张` : '—', sell.usd_per_image ? `$${sell.usd_per_image} / 张` : '—'];
  if (kind === 'video') return [rate, official.usd_per_second ? `$${official.usd_per_second} / 秒` : '—', sell.usd_per_second ? `$${sell.usd_per_second} / 秒` : '—'];
  const officialText = official.input_usd_per_1m ? `输入 $${official.input_usd_per_1m} · 输出 $${official.output_usd_per_1m || '—'}` : '—';
  const sellText = sell.input_usd_per_1m ? `输入 $${sell.input_usd_per_1m} · 输出 $${sell.output_usd_per_1m || '—'}` : '—';
  return [rate, officialText, sellText];
}
function renderModels() {
  const query=$('search').value.toLowerCase();const rows=state.accounts.flatMap(a=>a.models.map(m=>({...m,name:a.name}))).filter(m=>(m.id+m.name).toLowerCase().includes(query)&&(modelFilter==='all'||modelKind(m)===modelFilter));
  const head=$('models').querySelector('thead tr');
  if(cloudMode) head.innerHTML='<th>模型 ID</th><th>来源平台</th><th>倍率</th><th>官方定价</th><th>售价</th><th>操作</th>';
  $('model-rows').replaceChildren();
  document.dispatchEvent(new Event('modelsupdated'));
  for(const m of rows){
    const tr=element('tr');
    const kind=modelKind(m);
    const texts=cloudMode?[m.id,m.name,...pricingCells(m.pricing,kind)]:[m.id,m.name,sources[m.source]||m.source,availability[m.availability]||m.availability];
    for(const text of texts)tr.append(element('td',text));
    const cell=element('td');const button=element('button','调用');button.addEventListener('click',()=>{setMode(kind);$('test-model').value=m.id;snippet();showView('playground');});cell.append(button);tr.append(cell);$('model-rows').append(tr);
  }
  $('model-empty').hidden=rows.length>0;
}
function render(){
  renderAccounts();renderModels();snippet();$('network-mode').textContent=state.network==='system_proxy'?'系统本地代理':'直接连接';
  $('base-url').value=state.base_url||'';$('api-key').value=state.api_key||'';$('port').value=state.port||18787;$('app-version').textContent=`v${state.version||''}`;
  $('service-status').textContent=state.service_running?'HTTP 服务运行中':'HTTP 服务已停止';$('service-dot').classList.toggle('stopped',!state.service_running);
  $('service-control').textContent=state.service_running?'暂停服务':'启动服务';$('service-control').disabled=false;$('scan').disabled=!state.service_running;$('send').disabled=!state.service_running;
}
async function reload(resetConfig=true){const result=await bridge.state();if(!resetConfig)result.config=state.config;state=result;$('default-model').value=state.config.default_model||'';render();}
function modelKind(m){if(['text','image','video'].includes(m.type))return m.type;if(/video/i.test(m.official_id))return 'video';if(/image/i.test(m.official_id))return 'image';return 'text';}
function setMode(mode){callMode=mode;document.querySelectorAll('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===mode);b.setAttribute('aria-pressed',String(b.dataset.mode===mode));});$('endpoint').value={text:'responses',image:'images/generations',video:'videos'}[mode];$('prompt').placeholder={text:'例如：用一句话介绍你自己。',image:'描述你想生成的图片…',video:'描述你想生成的视频…'}[mode];renderModels();}
$('scan').textContent='一键同步';
$('scan').addEventListener('click',async()=>{const b=$('scan');b.disabled=true;b.textContent='同步并验证中…';try{const r=await api('/api/scan',{});state.accounts=r.accounts;render();const verified=state.accounts.filter(a=>a.status==='verified').length;notice(`同步完成，${verified} 个平台通过验证。未通过的平台已显示具体状态；媒体模型目录不代表已实际生成。`);}catch(e){notice(e.message,true);}finally{b.disabled=false;b.textContent='一键同步';}});
$('copy-url').addEventListener('click',()=>copy(state.base_url));$('copy-key').addEventListener('click',()=>copy(state.api_key));
$('show-key').addEventListener('click',()=>{const visible=$('api-key').type==='password';$('api-key').type=visible?'text':'password';$('show-key').textContent=visible?'隐藏':'显示';});
$('copy-config').addEventListener('click',()=>copy($('snippet').textContent));$('search').addEventListener('input',renderModels);$('test-model').addEventListener('input',snippet);
$('settings-form').addEventListener('submit',async e=>{e.preventDefault();try{state.config=await api('/api/config',{default_model:$('default-model').value.trim(),enabled_providers:state.config.enabled_providers},'PUT');snippet();notice('本地配置已保存。');}catch(err){notice(err.message,true);}});
$('test-form').addEventListener('submit',async e=>{
  e.preventDefault();const endpoint=$('endpoint').value,text=$('prompt').value,model=$('test-model').value.trim(),payload={model,stream:false};
  if(endpoint==='responses')payload.input=[{role:'user',content:[{type:'input_text',text}]}];else if(['messages','chat/completions'].includes(endpoint)){payload.messages=[{role:'user',content:text}];if(endpoint==='messages')payload.max_tokens=1024;}else{payload.prompt=text;delete payload.stream;}
  $('send').disabled=true;$('send').textContent='调用中…';$('result').textContent='正在等待官方响应…';$('raw-result').textContent='';$('result-meta').textContent='请求已发送';lastVideoId='';const start=performance.now();
  try{const result=await api('/v1/'+endpoint,payload);showResult(result);const usage=result.usage||{};$('result-meta').textContent=`${((performance.now()-start)/1000).toFixed(1)} 秒 · ${model}${usage.total_tokens?' · '+usage.total_tokens+' tokens':''}`;notice('官方调用完成。');}catch(err){$('result').textContent=err.message;$('result-meta').textContent='调用未完成';notice(err.message,true);if(!cloudMode)await reload(false);}finally{$('send').disabled=!state.service_running;$('send').textContent='发送请求 ↗';}
});
function textOf(result){
  if(result.error)return result.error.message||'官方返回错误';
  if(Array.isArray(result.output))return result.output.flatMap(x=>(x.content||[]).map(p=>p.text||'')).filter(Boolean).join('\n')||'调用完成，详细输出见原始 JSON。';
  if(Array.isArray(result.choices))return result.choices.map(x=>x.message?.content||'').join('\n');
  if(Array.isArray(result.content))return result.content.map(x=>x.text||'').join('\n');
  return result.status?`任务状态：${result.status}`:'调用完成。';
}
function showResult(result){
  $('result').replaceChildren();lastText=textOf(result);$('result').append(element('div',lastText));
  for(const item of result.data||[]){const source=item.b64_json?`data:image/png;base64,${item.b64_json}`:item.url;if(typeof source==='string'&&(source.startsWith('https://')||source.startsWith('data:image/'))){const img=element('img');img.src=source;img.alt='官方模型生成的图片';img.className='generated-image';img.referrerPolicy='no-referrer';$('result').append(img);}}
  lastVideoId=callMode==='video'?result.request_id||result.id||lastVideoId:'';
  if(lastVideoId){const check=element('button','刷新视频任务状态','secondary');check.addEventListener('click',async()=>{check.disabled=true;try{showResult(await api('/v1/videos/'+encodeURIComponent(lastVideoId),undefined,'GET'));}catch(e){notice(e.message,true);check.disabled=false;}});$('result').append(check);}
  const videoUrl=result.video?.url||result.url;
  if(callMode==='video'&&typeof videoUrl==='string'&&videoUrl.startsWith('https://')){const video=element('video');video.controls=true;video.src=videoUrl;video.className='generated-image';$('result').append(video);}
  const raw=JSON.stringify(result,null,2);$('raw-result').textContent=raw.length>30000?raw.slice(0,30000)+'\n…（响应较长，已截断显示）':raw;$('copy-result').disabled=false;
}
$('service-control').addEventListener('click',async()=>{const b=$('service-control');b.disabled=true;try{const r=await bridge.control(state.service_running?'stop':'start');if(!r.ok)throw new Error(r.error);state=r.data;render();notice(state.service_running?'本地 HTTP 服务已启动。':'服务已暂停，其他客户端暂时无法调用。');}catch(e){notice(e.message,true);b.disabled=false;}});
$('port-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await bridge.control('port',Number($('port').value));if(!r.ok)throw new Error(r.error);state=r.data;render();notice('端口已更新，请同步修改其他客户端的 Base URL。');}catch(err){notice(err.message,true);}});
$('copy-result').addEventListener('click',()=>copy(lastText));
document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.mode)));
function showView(view){
  const tools=$('local-tools');if(tools)tools.hidden=view!=='tools';
  if(view==='tools')loadLocalTools();
  $('connection').hidden=view!=='connection';$('connection-help').hidden=view!=='connection';$('accounts').hidden=view!=='accounts';$('models').hidden=view!=='models';$('call-view').hidden=view!=='playground';
  document.querySelectorAll('[data-view]').forEach(a=>{a.classList.toggle('active',a.dataset.view===view);if(a.dataset.view===view)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  const titles={connection:['你的官方账号，一个本地接口。','复制地址和密钥，让其他客户端调用本机的官方模型。'],accounts:['保留官方登录，统一管理。','发现本机凭据，一键同步登录状态和模型目录。'],models:['找到你要调用的官方模型。','区分本机配置、适配器候选和官方已列出的模型。'],playground:['选好模型，直接试一次。','在客户端内发送请求、查看结果，确认接入是否正常。']};
  titles.tools=['把常用工具，接到本机。','使用本地地址和独立密钥接入，写入前备份配置，随时回退。'];
  document.querySelector('.intro h1').textContent=cloudMode?(view==='models'?'云端模型目录':'选好模型，直接试一次。'):titles[view][0];document.querySelector('.intro .muted').textContent=cloudMode?'使用当前云端账号调用，费用计入云端账单。':titles[view][1];window.scrollTo(0,0);
}
document.querySelectorAll('[data-view]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();showView(a.dataset.view);}));
let started=false;
async function initialize(){
  const embedded=window.parent!==window;
  const native=(embedded?window.parent:window).pywebview?.api;
  if(started||!native)return;
  started=true;
  document.body.classList.toggle('embedded',embedded);
  bridge=embedded?{state:()=>native.local_state(),request:(...args)=>native.local_request(...args),control:(...args)=>native.local_control(...args),copy:value=>native.local_copy(value)}:native;
  if(cloudMode){
    document.body.classList.add('cloud-mode');
    bridge={state:()=>native.cloud_models_state(),request:(...args)=>native.cloud_model_request(...args),copy:value=>native.copy_model_text(value)};
    showView(new URLSearchParams(location.search).get('view')==='models'?'models':'playground');
    document.querySelector('#playground .form-footer .muted').textContent='使用云端额度 · 可在调用明细查看用量';
    document.querySelector('footer').textContent='MinKing AI · 云端服务';
    document.querySelector('.intro .eyebrow').textContent='CLOUD MODELS · API';
    document.querySelector('#models h2').textContent='云端模型目录';
    $('test-model').closest('label').firstChild.textContent='选择模型';
    $('test-model').placeholder='从云端模型目录选择，或输入模型 ID';
    $('result').textContent='选择一个云端模型，发送第一条消息。';
  }
  try{await reload();if(!state.service_running)notice('HTTP 服务尚未启动。若端口被占用，可在此更换端口。',true);}catch(e){notice(e.message||'客户端初始化失败',true);}
}
window.addEventListener('pywebviewready',initialize);initialize();

const categoryTabs=element('div',undefined,'call-tabs model-categories');
for(const [kind,label] of [['all','全部'],['text','文本'],['image','图片'],['video','视频']]){
  const button=element('button',label,kind==='all'?'active':'');button.type='button';button.setAttribute('aria-pressed',String(kind==='all'));
  button.addEventListener('click',()=>{modelFilter=kind;for(const child of categoryTabs.children){child.classList.toggle('active',child===button);child.setAttribute('aria-pressed',String(child===button));}renderModels();});categoryTabs.append(button);
}
$('models').querySelector('.section-head').after(categoryTabs);
if(!cloudMode){
  const nav=element('a','接入工具');nav.href='#tools';nav.dataset.view='tools';nav.addEventListener('click',e=>{e.preventDefault();showView('tools');});document.querySelector('.sidebar nav').append(nav);
  const panel=element('section');panel.id='local-tools';panel.hidden=true;document.querySelector('main>footer').before(panel);
}
async function loadLocalTools(){
  const panel=$('local-tools');panel.replaceChildren(element('p','正在发现本机工具…','muted'));
  try{
    const native=window.parent.pywebview.api;const result=await native.local_tools('list');if(!result.ok)throw new Error(result.error);
    const grid=element('div',undefined,'harness-list');
    const context={base_url:state.base_url,api_key:state.api_key,copy,notice,refresh:loadLocalTools};
    for(const item of result.items)grid.append(window.parent.card(item,context));
    panel.replaceChildren(grid);
  }catch(error){panel.replaceChildren(element('p',error.message,'error'));}
}
