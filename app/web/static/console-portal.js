const portalDashboard=$('dashboard-view');
const portalNav=document.createElement('nav');portalNav.className='portal-tabs';portalNav.setAttribute('aria-label','个人工作台');
const portalPages={overview:'工作台',calls:'调用明细',models:'模型目录',try:'调用测试',wallet:'资金流水',redeem:'卡密兑换',tools:'接入工具'};
const portalPanes={};
for(const [key,title] of Object.entries(portalPages)){const button=document.createElement('button');button.type='button';button.textContent=title;button.dataset.portalPage=key;portalNav.append(button);const pane=document.createElement('section');pane.className='portal-pane';pane.id=`portal-${key}`;pane.setAttribute('aria-label',title);portalPanes[key]=pane;portalDashboard.append(pane);}
portalDashboard.querySelector('.dashboard-top').after(portalNav);
portalPanes.overview.append(portalDashboard.querySelector('.metrics'),portalDashboard.querySelector('.trend-panel'));
portalPanes.tools.append(portalDashboard.querySelector('.key-panel'));
portalPanes.calls.append($('recent-calls').closest('article'));
portalPanes.calls.insertAdjacentHTML('afterbegin','<form id="portal-call-filters" class="range-bar"><label>模型<input name="model" placeholder="全部模型"></label><label>状态<select name="status"><option value="">全部</option><option value="success">成功</option><option value="failed">失败</option><option value="interrupted">中断</option></select></label><label>开始<input name="start" type="datetime-local"></label><label>结束<input name="end" type="datetime-local"></label><button class="primary">筛选</button></form>');
$('portal-call-filters').onsubmit=e=>{e.preventDefault();callsPage=1;loadCalls().catch(error=>message(error.message,true,true));};
portalPanes.redeem.append(portalDashboard.querySelector('.credit-panel'));
portalPanes.wallet.append($('wallet-ledger').closest('article'));
portalDashboard.querySelectorAll('.workspace-grid').forEach(grid=>{if(!grid.children.length)grid.remove();});
const metricNames=['调用次数','成功率','输入令牌','输出令牌'];
portalPanes.overview.querySelectorAll('.metric').forEach((card,index)=>{card.querySelector('span').textContent=metricNames[index];card.querySelector('small').textContent='所选范围';});
portalPanes.overview.querySelector('.metrics').insertAdjacentHTML('beforeend','<article class="metric"><span>失败率</span><strong id="metric-fail">—</strong><small>所选范围</small></article><article class="metric"><span>缓存令牌</span><strong id="metric-cached">—</strong><small>所选范围</small></article><article class="metric"><span>额度（美元）</span><strong id="metric-credit">—</strong><small id="metric-credit-note">当前余额</small></article>');
portalPanes.overview.insertAdjacentHTML('afterbegin','<div class="range-bar" id="portal-periods"><div class="segmented"><button type="button" class="quiet active" data-portal-period="day">今日</button><button type="button" class="quiet" data-portal-period="week">7 天</button><button type="button" class="quiet" data-portal-period="month">30 天</button></div></div>');
const creditText=portalPanes.redeem.querySelector('.credit-panel > p');creditText.id='wallet-policy';creditText.textContent='正在读取计费状态…';
portalPanes.wallet.insertAdjacentHTML('beforeend',`<article class="panel"><div class="panel-heading"><div><span class="eyebrow">REQUEST BUDGET</span><h2>控制单次费用</h2></div></div><p>请求开始前预占预算，结束后只扣实际用量。超出授权预算的差额由平台承担。</p><form id="portal-budget-form" class="budget-inline"><label>单次上限 USD<input id="portal-budget" type="number" step="0.00000001" min="0.00000001" required></label><button class="primary">保存上限</button></form><p id="portal-budget-message" role="status"></p></article><article class="panel"><div class="panel-heading"><div><span class="eyebrow">BILLING STATUS</span><h2>预占与结算明细</h2></div><button id="portal-bills-refresh" class="outline">刷新</button></div><div id="portal-bills" class="table-scroll"></div><div class="pager"><button id="portal-bills-prev" class="text-button">上一页</button><span id="portal-bills-page"></span><button id="portal-bills-next" class="text-button">下一页</button></div></article>`);
portalPanes.models.innerHTML=`<article class="panel"><div class="panel-heading"><div><span class="eyebrow">MODEL CATALOG</span><h2>模型目录</h2><p>文本按百万 Token，图片按张，视频按秒。实际计价以请求开始时的价格为准。</p></div></div><label>搜索模型<input id="portal-price-search" type="search" placeholder="模型或供应商"></label><div id="portal-prices" class="table-scroll"></div></article>`;
portalPanes.try.innerHTML=`<article class="panel"><div class="panel-heading"><div><span class="eyebrow">TRY A CALL</span><h2>调用测试</h2><p>使用当前账号的密钥发起一次调用，费用记入这把密钥。网页不会显示完整密钥。</p></div></div><form id="portal-try-form" class="stack-form"><label>类型<select id="portal-try-mode"><option value="text">文本对话</option><option value="image">图片生成</option><option value="video">视频生成</option></select></label><label>模型<input id="portal-try-model" required placeholder="例如 gpt-5.6-sol"></label><label>内容<textarea id="portal-try-prompt" rows="4" required maxlength="2000" placeholder="用一句话介绍你自己。"></textarea></label><button class="primary" type="submit">发送</button></form><div id="portal-try-result" class="try-result"></div></article>`;
portalPanes.tools.insertAdjacentHTML('beforeend','<div id="portal-harnesses" class="harness-grid"></div>');
// Keep credential configuration detail discoverable without overwhelming the overview.
const connect=portalPanes.tools.querySelector('.key-panel');const notes=[...connect.querySelectorAll('.fineprint')];
notes.forEach((note,i)=>{const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=i===0?'手动配置：下载后如何接入':'客户端导入：连接信息如何传递';note.before(details);details.append(summary,note);});
connect.insertAdjacentHTML('afterbegin','<p class="tag">01 选择接入方式 · 02 导入配置 · 03 开始调用</p>');
let portalPage='overview',portalBillPage=1,portalPriceRows=[],portalPeriod='day',portalDash=null;
function portalSwitch(page){portalPage=page;Object.entries(portalPanes).forEach(([key,pane])=>pane.hidden=key!==page);portalNav.querySelectorAll('button').forEach(b=>{b.classList.toggle('active',b.dataset.portalPage===page);b.setAttribute('aria-current',b.dataset.portalPage===page?'page':'false');});if(page==='models')loadPortalPrices().catch(e=>message(e.message,true,true));if(page==='wallet'||page==='redeem')loadPortalBills().catch(e=>message(e.message,true,true));if(page==='tools')loadPortalHarnesses().catch(e=>message(e.message,true,true));}
portalNav.onclick=e=>{const b=e.target.closest('[data-portal-page]');if(b)portalSwitch(b.dataset.portalPage);};portalSwitch('overview');
function paintPortalPeriod(){
  const block=(portalDash&&portalDash.periods&&portalDash.periods[portalPeriod])||{};
  const calls=Number(block.calls||0);
  const successes=Number(block.successes||0);
  const failures=Number(block.failures||0);
  $('metric-calls').textContent=number(calls);
  $('metric-rate').textContent=calls?`${(100*successes/calls).toFixed(1)}%`:'—';
  $('metric-tokens').textContent=number(block.input_tokens||0);
  $('metric-downloads').textContent=number(block.output_tokens||0);
  $('metric-fail').textContent=calls?`${(100*failures/calls).toFixed(1)}%`:'—';
  $('metric-cached').textContent=number(block.cached_tokens||0);
}
async function refreshPortalWallet(){
  const [wallet,dash]=await Promise.all([request('/wallet').then(r=>r.json()),request('/dashboard').then(r=>r.json())]);
  portalDash=dash;
  paintPortalPeriod();
  $('metric-credit').textContent=money(wallet.usd_credit);
  $('metric-credit-note').textContent=`可用 ${money(wallet.available_usd)}`;
  $('usd-credit').textContent=money(wallet.usd_credit);$('wallet-policy').textContent=wallet.enforced?`严格预付费 · 可用 ${money(wallet.available_usd)}，预占 ${money(wallet.reserved_usd)}。中断请求按已核验用量收费。`:'当前未启用强制预付费，余额不足不会拦截调用。';
  if(document.activeElement!==$('portal-budget'))$('portal-budget').value=wallet.request_budget_usd;
}
$('portal-periods').onclick=event=>{const button=event.target.closest('[data-portal-period]');if(!button)return;portalPeriod=button.dataset.portalPeriod;$('portal-periods').querySelectorAll('button').forEach(item=>item.classList.toggle('active',item===button));paintPortalPeriod();};
const originalPortalDashboard=refreshDashboard;refreshDashboard=async function(){await originalPortalDashboard();await refreshPortalWallet();if(portalPage==='wallet')await loadPortalBills();};
const portalBillNames={reserved:'预占中',pending:'待核对',settled:'已结算',released:'已释放',reversed:'已冲正'};
async function loadPortalBills(){const body=await (await request(`/wallet/requests?page=${portalBillPage}`)).json();$('portal-bills').innerHTML=body.data.length?`<table><thead><tr><th>时间 / 模型</th><th>状态</th><th>扣费</th><th>详情</th></tr></thead><tbody>${body.data.map(r=>`<tr><td>${escapeHtml(formatTime(r.created_at))}<br>${escapeHtml(r.model)}</td><td>${portalBillNames[r.state]||escapeHtml(r.state)}</td><td>${money(r.charged_usd)}</td><td><details><summary>费用明细</summary><p>预占 ${money(r.reserved_usd)}<br>实际费用 ${r.actual_usd==null?'待核对':money(r.actual_usd)}<br>平台承担 ${money(r.absorbed_usd)}<br>价格版本 ${escapeHtml(r.price_version)}<br>请求 ${escapeHtml(r.request_id)}</p></details></td></tr>`).join('')}</tbody></table>`:'<p class="empty">还没有账单，首次调用后可在这里查看结算过程。</p>';$('portal-bills-page').textContent=`第 ${body.page} 页 · ${body.total} 笔`;$('portal-bills-prev').disabled=body.page<=1;$('portal-bills-next').disabled=body.page*50>=body.total;}
$('portal-bills-refresh').onclick=()=>Promise.all([loadPortalBills(),refreshPortalWallet()]).catch(e=>message(e.message,true,true));$('portal-bills-prev').onclick=()=>{portalBillPage--;loadPortalBills().catch(e=>message(e.message,true,true));};$('portal-bills-next').onclick=()=>{portalBillPage++;loadPortalBills().catch(e=>message(e.message,true,true));};
$('portal-budget-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await request('/wallet/budget',{method:'PUT',body:JSON.stringify({request_budget_usd:$('portal-budget').value})});$('portal-budget-message').textContent='单次预算已保存';await refreshPortalWallet();}catch(error){$('portal-budget-message').textContent=error.message;}finally{button.disabled=false;}};
async function loadPortalPrices(){const body=await (await request('/pricing')).json();portalPriceRows=body.data||body.models||[];renderPortalPrices();}
function portalPriceText(row, bucket) {
  const source = row[bucket] || {};
  if (row.modality === 'image') return source.usd_per_image == null ? '—' : `${money(source.usd_per_image)} / 张`;
  if (row.modality === 'video') return source.usd_per_second == null ? '—' : `${money(source.usd_per_second)} / 秒`;
  const input = source.input_usd_per_1m == null ? '—' : money(source.input_usd_per_1m);
  const output = source.output_usd_per_1m == null ? '—' : money(source.output_usd_per_1m);
  return `输入 ${input} · 输出 ${output}`;
}
function renderPortalPrices(){const query=$('portal-price-search').value.toLowerCase();const rows=portalPriceRows.filter(r=>`${r.model} ${r.provider}`.toLowerCase().includes(query));$('portal-prices').innerHTML=rows.length?`<table><thead><tr><th>模型</th><th>倍率</th><th>官方定价</th><th>售价</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${escapeHtml(r.model)}<br><small>${escapeHtml(r.provider)}</small></td><td>${escapeHtml(r.multiplier||'—')}</td><td>${portalPriceText(r,'official')}</td><td>${portalPriceText(r,'sell')}</td></tr>`).join('')}</tbody></table>`:'<p class="empty">没有匹配的已定价模型</p>';}
$('portal-price-search').oninput=renderPortalPrices;
async function loadPortalHarnesses(){const body=await (await request('/harnesses')).json();const rows=body.data||[];$('portal-harnesses').innerHTML=rows.map(item=>`<article class="panel harness-card"><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.summary)}</p><button type="button" class="outline portal-open-tool">在客户端中接入</button></article>`).join('')||'<p class="empty">暂时没有可接入的工具。</p>';}
$('portal-harnesses').onclick=event=>{if(!event.target.closest('.portal-open-tool'))return;const opener=$('open-desktop');if(opener)opener.click();};
$('portal-try-form').onsubmit=async event=>{event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;$('portal-try-result').textContent='正在调用…';try{const body=await (await request('/playground',{method:'POST',body:JSON.stringify({mode:$('portal-try-mode').value,model:$('portal-try-model').value.trim(),prompt:$('portal-try-prompt').value.trim()})})).json();if(typeof body.image==='string'&&body.image.startsWith('data:image/')){const img=document.createElement('img');img.alt='生成的图片';img.src=body.image;$('portal-try-result').replaceChildren(img);}else{$('portal-try-result').textContent=body.text||body.message||(body.ok?'调用已完成':'调用失败');}}catch(error){$('portal-try-result').textContent=error.message;}finally{button.disabled=false;}};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!portalDashboard.classList.contains('hidden'))refreshPortalWallet().catch(()=>{});});
