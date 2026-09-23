const $ = id => document.getElementById(id);
const root = document.querySelector('meta[name="app-root-path"]')?.content.replace(/\/+$/, "") || "";
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = value => new Intl.NumberFormat('zh-CN').format(Number(value || 0));
const money = value => `$${Number(value || 0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:8})}`;
const formatTime = value => {if(!value)return '—';const d=new Date(value);if(Number.isNaN(d.getTime()))return '—';const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;};
const statusLabel = {success:'成功',failed:'失败',interrupted:'中断',in_progress:'进行中',usage:'扣费',grant:'加款',redeem:'卡密',adjust:'调整'};
let csrf = "", captchaId = "", challengeId = "", activeEmail = "", authMode = "login";
let callsPage = 1, ledgerPage = 1;

function setAuthMode(mode) {
  authMode = mode === "register" ? "register" : "login";
  const register = authMode === "register";
  $("tab-login").classList.toggle("active", !register);
  $("tab-register").classList.toggle("active", register);
  $("tab-login").setAttribute("aria-selected", register ? "false" : "true");
  $("tab-register").setAttribute("aria-selected", register ? "true" : "false");
  $("name-field").classList.toggle("hidden", !register);
  $("name").required = register;
  $("auth-sub").textContent = register ? "新用户填写姓名和邮箱。在 Windows 客户端注册时，每台电脑只能领取一次新人奖励；重复注册会提示，并且不再发放。" : "已有账号用邮箱验证码登录。";
  $("send-code-btn").innerHTML = register ? "发送注册验证码 <span>↗</span>" : "发送登录验证码 <span>↗</span>";
}

async function request(path, options = {}) {
  const apiBase = window.location.pathname.includes("/admin/portal") ? `${root}/admin/portal/api` : `${root}/portal/api`;
  const response = await fetch(`${apiBase}${path}`, {credentials:'same-origin',...options,headers:{...(options.body ? {'Content-Type':'application/json'} : {}),'X-CSRF-Token':csrf,...options.headers}});
  if (!response.ok) {
    let body = {}; try { body = await response.json(); } catch {}
    throw new Error(body.error?.message || `请求失败 (${response.status})`);
  }
  return response;
}
function message(text, error=false, dashboard=false) {
  const node = $(dashboard ? 'dashboard-message' : 'login-message');
  node.textContent = text; node.classList.toggle('error',error);
}
async function captcha() {
  try {
    const body = await (await request('/captcha')).json();
    captchaId = body.id; $('captcha-image').src = body.image;
  } catch (error) { message(error.message,true); }
}
function showLogin() { $('login-view').classList.remove('hidden'); $('dashboard-view').classList.add('hidden'); captcha(); }
function showDashboard() { $('login-view').classList.add('hidden'); $('dashboard-view').classList.remove('hidden'); }
function downloadBlob(blob) {
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href=url;link.download='minking-api-codex.zip';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),30000);
}
async function refreshDashboard() {
  const body = await (await request('/dashboard')).json();
  $('user-name').textContent=body.user.name;
  $('user-email').textContent=body.user.email;
  $('metric-calls').textContent=number(body.metrics.calls);
  $('metric-rate').textContent=body.metrics.success_rate === null ? '—' : `${body.metrics.success_rate}%`;
  $('metric-tokens').textContent=number(body.metrics.tokens);
  $('metric-downloads').textContent=number(body.metrics.downloads);
  $('usd-credit').textContent=money(body.user.usd_credit || body.balance);
  $('key-prefix').textContent=`${body.key.prefix}••••••••`;
  $('key-status').textContent=body.key.status === 'active' ? '● 已启用' : '○ 已暂停';
  $('key-created').textContent=`创建于 ${formatTime(body.key.created_at)}`;
  $('download').disabled=body.key.status !== 'active';
  const importUrl = body.desktop && typeof body.desktop.import_url === 'string' ? body.desktop.import_url : '';
  const openDesktop = $('open-desktop');
  if (openDesktop) {
    openDesktop.disabled = !importUrl.startsWith('minking://');
    openDesktop.dataset.href = importUrl.startsWith('minking://') ? importUrl : '';
  }
  const days=body.daily.slice(-45), peak=Math.max(1,...days.map(item=>Number(item.calls)));
  $('chart').innerHTML=days.length ? days.map(item=>`<span class="chart-bar" style="height:${Math.max(2,Number(item.calls)/peak*98)}%" title="${escapeHtml(item.day)} · ${Number(item.calls)} 次"></span>`).join('') : '<p class="muted">还没有调用记录。下载配置包后开始使用吧。</p>';
  await Promise.all([loadCalls(), loadLedger()]);
  showDashboard();
}
async function loadCalls() {
  const filters=$('portal-call-filters');const params=filters?new URLSearchParams(new FormData(filters)):new URLSearchParams();for(const key of [...params.keys()])if(!params.get(key))params.delete(key);for(const key of ['start','end'])if(params.has(key))params.set(key,new Date(params.get(key)).toISOString());
  const body = await (await request(`/calls?page=${callsPage}&page_size=12&${params}`)).json();
  const rows = body.data || [];
  callsPage = Number(body.page || callsPage);
  const total = Number(body.total || 0);
  const size = Number(body.page_size || 12);
  $('recent-calls').innerHTML = rows.length ? rows.map(row => {
    const ok = row.status === 'success';
    return `<tr><td>${escapeHtml(formatTime(row.started_at))}</td><td>${escapeHtml(row.model || '—')}</td><td>${escapeHtml(row.endpoint || '—')}</td><td>${number(row.input_tokens)}</td><td>${number(row.output_tokens)}</td><td>${row.usd_charged == null ? '—' : money(row.usd_charged)}</td><td class="${ok?'status-ok':'status-fail'}">${escapeHtml(statusLabel[row.status] || row.status || '—')}</td></tr>`;
  }).join('') : '<tr><td colspan="7">尚无调用记录</td></tr>';
  $('calls-page').textContent = total ? `第 ${callsPage} 页 · 共 ${total} 条` : `第 ${callsPage} 页`;
  $('calls-prev').disabled = callsPage <= 1;
  $('calls-next').disabled = callsPage * size >= total || rows.length === 0;
}
async function loadLedger() {
  const body = await (await request(`/wallet/ledger?page=${ledgerPage}&page_size=12`)).json();
  const rows = body.data || [];
  ledgerPage = Number(body.page || ledgerPage);
  const total = Number(body.total || 0);
  const size = Number(body.page_size || 12);
  $('wallet-ledger').innerHTML = rows.length ? rows.map(row => {
    const kind = row.kind || row.type || '';
    const reasonLabels={success:'调用成功',failed:'调用失败（按可信用量结算）',interrupted:'调用中断（按可信用量结算）',card_redeem:'卡密兑换',new_user:'新用户赠送'};
    return `<tr><td>${escapeHtml(formatTime(row.created_at))}</td><td>${escapeHtml(kind==='reversal'?'账单冲正':statusLabel[kind] || kind || '—')}</td><td>${money(row.amount_usd ?? row.amount)}</td><td>${money(row.balance_after ?? row.balance)}</td><td>${escapeHtml(reasonLabels[row.reason] || row.reason || row.note || '—')}</td></tr>`;
  }).join('') : '<tr><td colspan="5">暂无资金流水</td></tr>';
  $('ledger-page').textContent = total ? `第 ${ledgerPage} 页 · 共 ${total} 条` : `第 ${ledgerPage} 页`;
  $('ledger-prev').disabled = ledgerPage <= 1;
  $('ledger-next').disabled = ledgerPage * size >= total || rows.length === 0;
}
async function initialize() {
  try { const user=await (await request('/auth/session')).json(); csrf=user.csrf_token; await refreshDashboard(); }
  catch { showLogin(); }
}
$('captcha-refresh').addEventListener('click',captcha);
$('tab-login').addEventListener('click',()=>setAuthMode('login'));
$('tab-register').addEventListener('click',()=>setAuthMode('register'));
$('mail-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.target.querySelector('button[type="submit"]');button.disabled=true;message('正在发送验证码…');
  try {
    activeEmail=$('email').value.trim().toLowerCase();
    const name = authMode === "register" ? $('name').value.trim() : "";
    if (authMode === "register" && !name) { throw new Error("注册请填写姓名"); }
    const body=await (await request('/auth/send-code',{method:'POST',body:JSON.stringify({name,email:activeEmail,captcha_id:captchaId,captcha:$('captcha-answer').value})})).json();
    challengeId=body.challenge_id;$('verify-email').textContent=activeEmail;
    $('mail-form').classList.add('hidden');$('verify-form').classList.remove('hidden');message('验证码已发送，请查看邮箱。');
  } catch(error) { message(error.message,true);$('captcha-answer').value='';await captcha(); }
  finally { button.disabled=false; }
});
$('back-to-email').addEventListener('click',()=>{$('verify-form').classList.add('hidden');$('mail-form').classList.remove('hidden');$('email-code').value='';message('');captcha();});
$('verify-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.target.querySelector('button[type="submit"]');button.disabled=true;message('正在验证…');
  try { const body=await (await request('/auth/verify',{method:'POST',body:JSON.stringify({email:activeEmail,challenge_id:challengeId,code:$('email-code').value})})).json();csrf=body.csrf_token;message('');callsPage=1;ledgerPage=1;await refreshDashboard();if(body.reward_notice)message(body.reward_notice,false,true); }
  catch(error){message(error.message,true);}finally{button.disabled=false;}
});
$('open-desktop').addEventListener('click',()=>{
  const href = $('open-desktop').dataset.href || '';
  if (!href.startsWith('minking://')) {
    message('未找到 MinKing 客户端导入链接。',true,true);
    return;
  }
  message('正在打开 MinKing 客户端…',false,true);
  window.location.href = href;
});
$('download').addEventListener('click',async()=>{
  const button=$('download');button.disabled=true;message('正在生成你的配置包…',false,true);
  try { downloadBlob(await (await request('/bundle',{method:'POST'})).blob());message('下载已开始。',false,true);await refreshDashboard(); }
  catch(error){message(error.message,true,true);}finally{button.disabled=false;}
});
$('rotate').addEventListener('click',async()=>{
  if (!confirm('轮换后旧 API Key 将立即失效。确定继续吗？')) return;
  try { await request('/key/rotate',{method:'POST'});await refreshDashboard();message('已轮换密钥，请重新下载配置包。',false,true); }
  catch(error){message(error.message,true,true);}
});
$('logout').addEventListener('click',async()=>{try{await request('/auth/logout',{method:'POST'});}finally{csrf='';callsPage=1;ledgerPage=1;$('verify-form').classList.add('hidden');$('mail-form').classList.remove('hidden');showLogin();}});
$('calls-prev').addEventListener('click',async()=>{ if (callsPage <= 1) return; callsPage -= 1; try { await loadCalls(); } catch (error) { message(error.message,true,true); } });
$('calls-next').addEventListener('click',async()=>{ callsPage += 1; try { await loadCalls(); } catch (error) { callsPage -= 1; message(error.message,true,true); } });
$('ledger-prev').addEventListener('click',async()=>{ if (ledgerPage <= 1) return; ledgerPage -= 1; try { await loadLedger(); } catch (error) { message(error.message,true,true); } });
$('ledger-next').addEventListener('click',async()=>{ ledgerPage += 1; try { await loadLedger(); } catch (error) { ledgerPage -= 1; message(error.message,true,true); } });
$('redeem-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const code = $('redeem-code').value.trim();
  if (!code) { message('请输入卡密。',true,true); return; }
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await request('/wallet/redeem',{method:'POST',body:JSON.stringify({code})});
    $('redeem-code').value = '';
    callsPage = 1; ledgerPage = 1;
    await refreshDashboard();
    message('卡密已兑换到账。',false,true);
  } catch (error) { message(error.message,true,true); }
  finally { button.disabled = false; }
});
initialize();
