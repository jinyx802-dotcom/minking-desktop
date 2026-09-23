const $ = (id) => document.getElementById(id);
const appRootPath = document.querySelector("meta[name=app-root-path]")?.content.replace(/\/+$/, "") || "";
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const state = {csrf: "", page: "dashboard", callsPage: 1, callsTotal: 0, ledgerPage: 1, ledgerTotal: 0, usageRange: "day", keys: [], accounts: [], providers: [], quotas: new Map(), quotaExpanded: new Set()};
const pageTitles = {dashboard:"仪表盘",accounts:"上游账号",keys:"使用者 / API Key",calls:"调用明细",usage:"Token 统计",models:"可用模型",billing:"计费",cards:"卡密",ledger:"资金流水",errors:"错误日志",skills:"客户端技能",settings:"账号设置"};
const providerNames = {};
let pageAbort = null;
let pageGeneration = 0;
let accountOauth = {provider: "codex", state: "", timer: null, generation: 0, startedAt: 0, pollErrors: 0};
let quotaAbort = null;
let quotaGeneration = 0;
let quotaStatus = "idle";
let routeKeyId = "";
let routeSelected = "";
let routeSearch = "";

function setProviders(rows) {
  state.providers = Array.isArray(rows) ? rows : [];
  for (const provider of state.providers) providerNames[provider.id] = provider.display_name;
  const select = $("account-provider-filter");
  const selected = select.value;
  select.innerHTML = `<option value="">全部</option>${state.providers.map((provider) => `<option value="${esc(provider.id)}">${esc(provider.display_name)}</option>`).join("")}`;
  select.value = selected;
}

const accountLabel = (row) => {
  if (!row || row.account_id == null || row.account_id === "") return "—";
  const provider = providerNames[row.provider] || row.provider || "Codex";
  return `${provider} · ${row.label || row.account_id}`;
};
const capabilityText = (row) => {
  const caps = new Set(row.capabilities || []);
  const parts = [];
  if (caps.has("chat") || caps.has("responses")) parts.push("文本");
  if (caps.has("image") || caps.has("image_edit")) parts.push("图");
  if (caps.has("video")) parts.push("视频");
  return parts.join("/") || "—";
};
const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const formatTokens = (value) => {
  const tokens = Number(value || 0);
  if (!Number.isFinite(tokens) || tokens === 0) return "0k";
  const scaled = tokens / 1000;
  const maximumFractionDigits = Math.abs(scaled) < 0.1 ? 3 : Math.abs(scaled) < 10 ? 2 : 1;
  return `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits}).format(scaled)}k`;
};
const formatTime = (value) => { if(!value)return '—';const d=new Date(value);if(Number.isNaN(d.getTime()))return '—';const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
const statusText = (value) => ({active:"正常",disabled:"已禁用",invalid:"失效",deleted:"已删除",cooling:"冷却中"})[value] || value;

async function api(path, options = {}) {
  const headers = {"X-CSRF-Token": state.csrf, ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(`${appRootPath}${path}`, {credentials: "same-origin", cache: "no-store", ...options, headers});
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = {error: {message: text || response.statusText}}; }
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/auth/login")) showLogin();
    const error = new Error(body?.error?.message || body?.detail || response.statusText);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      /* HTTP admin pages are not a secure context; fall through. */
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  area.style.top = "0";
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, text.length);
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

function showLogin(message = "") {
  $("app-shell").classList.add("hidden");
  $("login-view").classList.remove("hidden");
  $("login-error").textContent = message;
  $("login-error").classList.toggle("hidden", !message);
  $("login-password").value = "";
}

function beginQuotaRequest() {
  quotaAbort?.abort();
  const abort = new AbortController();
  quotaAbort = abort;
  const generation = ++quotaGeneration;
  quotaStatus = "loading";
  return {signal: abort.signal, generation};
}

function applyQuotas(body, generation) {
  if (generation !== quotaGeneration) return false;
  state.quotas = new Map((body.data || []).map((item) => [item.account_id, item]));
  quotaStatus = "ok";
  return true;
}

function markQuotaFailed(generation) {
  if (generation !== quotaGeneration) return;
  quotaStatus = "failed";
}

function showApp(session) {
  state.csrf = session.csrf_token;
  $("session-user").textContent = session.username;
  $("login-view").classList.add("hidden");
  $("app-shell").classList.remove("hidden");
  const quota = beginQuotaRequest();
  api("/admin/api/accounts/quotas", {signal: quota.signal}).then((body) => {
    if (applyQuotas(body, quota.generation) && state.page === "accounts") renderAccountsTable();
  }).catch((error) => {
    if (error.name === "AbortError" || quota.signal.aborted) return;
    markQuotaFailed(quota.generation);
    if (state.page === "accounts") renderAccountsTable();
  });
  switchPage("dashboard");
}

function table(columns, rows) {
  if (!rows.length) return '<div class="empty">暂无数据</div>';
  const cell = (key, row) => {
    const value = typeof key === "function" ? key(row) : row[key];
    return value && typeof value === "object" && value.html !== undefined ? value.html : esc(value ?? "—");
  };
  return `<table><thead><tr>${columns.map(([, label]) => `<th>${esc(label)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map(([key]) => `<td>${cell(key, row)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

const chip = (value) => ({html:`<span class="chip ${esc(value)}">${esc(({success:"成功",failed:"失败",interrupted:"中断",active:"正常",disabled:"已禁用",invalid:"失效",cooling:"冷却中",revoked:"已吊销",deleted:"已删除",priority:"已开启",off:"关闭",primary:"主账号",failover:"故障转移",unconfigured:"未配置",unused:"未使用",redeemed:"已兑换",expired:"已过期",usage:"用量",grant:"赠送",redeem:"卡密",adjust:"调整",reversal:"冲正"})[value] || value)}</span>`});
const formatUsd = (value) => {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `$${number.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:8})}`;
};

function fillSelect(select, options, {placeholder = "全部", value, label, disabled = () => false, groupBy} = {}) {
  if (!select) return;
  const previous = select.value;
  select.replaceChildren(new Option(placeholder, ""));
  if (!groupBy) {
    for (const item of options) {
      const option = new Option(label(item), value(item));
      option.disabled = disabled(item);
      select.add(option);
    }
  } else {
    const groups = new Map();
    for (const item of options) {
      const name = groupBy(item) || "其他";
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(item);
    }
    for (const [name, items] of groups) {
      const group = document.createElement("optgroup");
      group.label = name;
      for (const item of items) {
        const option = new Option(label(item), value(item));
        option.disabled = disabled(item);
        group.append(option);
      }
      select.append(group);
    }
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

function errorTable(rows) {
  return table([[(row)=>formatTime(row.occurred_at),"时间"],[(row)=>row.level === "error" ? "错误" : "警告","级别"],["user_name","使用者"],["category","类别"],[(row)=>[row.method,row.path].filter(Boolean).join(" "),"接口"],["status","HTTP"],["code","错误码"],["message","错误信息"]], rows);
}

async function refreshDashboard(signal) {
  const body = await api("/admin/api/dashboard", {signal});
  if (signal?.aborted) return;
  const metrics = [
    ["今日调用", body.calls, false], ["成功", body.successes, false], ["失败 / 中断", body.failures, false],
    ["输入 Token", body.input_tokens, true], ["输出 Token", body.output_tokens, true],
    ["缓存 Token", body.cached_tokens, true], ["推理 Token", body.reasoning_tokens, true],
    ["总 Token", body.total_tokens, true], ["平均耗时", `${formatNumber(body.average_duration_ms)} ms`, false],
    ["健康账号", body.healthy_accounts, false],
  ];
  $("dashboard-metrics").innerHTML = metrics.map(([label, value, isToken], index) => `<article class="metric-card ${index === 7 ? "accent" : ""}"><span>${esc(label)}</span><strong>${isToken ? formatTokens(value) : typeof value === "number" ? formatNumber(value) : esc(value)}</strong></article>`).join("");
  $("dashboard-errors").innerHTML = errorTable(body.recent_errors);
}

function modelCell(row) {
  const requestModel = String(row.request_model || row.model || "").trim();
  const responseModel = String(row.response_model || "").trim();
  if (row.model_mismatch && requestModel && responseModel) {
    return {html:`<div class="model-pair model-mismatch"><span>${esc(requestModel)}</span><small>返回 ${esc(responseModel)}</small></div>`};
  }
  return requestModel || responseModel || "—";
}

function formQuery(form) {
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => { if (String(value).trim()) params.set(key, String(value)); });
  for(const key of ['start','end']){if(params.has(key)){const date=new Date(params.get(key));if(!Number.isNaN(date.getTime()))params.set(key,date.toISOString());}}
  return params;
}

async function refreshCalls(signal) {
  const params = formQuery($("call-filters"));
  params.set("page", state.callsPage); params.set("page_size", "25");
  const [body, keys, accounts] = await Promise.all([
    api(`/admin/api/calls?${params}`, {signal}), api("/admin/api/keys", {signal}), api("/admin/api/accounts", {signal}),
  ]);
  if (signal?.aborted) return;
  state.keys = keys.data; state.accounts = accounts.data; state.callsTotal = body.total;
  const keyOptions = new Map((body.key_options || []).map((row) => [row.key_id, row]));
  state.keys.forEach((row) => keyOptions.set(row.id, {key_id: row.id, name: row.name, status: row.status}));
  const accountOptions = new Map((body.account_options || []).map((row) => [row.account_id, row]));
  state.accounts.forEach((row) => accountOptions.set(row.account_id, row));
  fillSelect($("call-filters").elements.key_id, [...keyOptions.values()], {
    value: (row) => row.key_id,
    label: (row) => `${row.name}${row.status === "deleted" ? "（历史）" : ""}`,
  });
  fillSelect($("call-filters").elements.account_id, [...accountOptions.values()], {
    value: (row) => row.account_id,
    label: (row) => `${accountLabel(row)}${row.status === "deleted" ? "（历史）" : ""}`,
  });
  const callUserName = (row) => {
    const current = keyOptions.get(row.key_id);
    return (current && current.name) || row.key_name || "—";
  };
  const callTokenCell = (row, field) => row.usage_unknown ? "未知" : formatTokens(row[field]);
  $("calls").innerHTML = table([
    [(row)=>formatTime(row.started_at),"开始时间"],[(row)=>row.request_id.slice(0,12),"Request ID"],
    [callUserName,"使用者"],[(row)=>accountLabel(accountOptions.get(row.account_id) || {account_id: row.account_id}),"账号"],["endpoint","接口"],[modelCell,"模型"],
    [(row)=>row.is_stream ? "是" : "否","流式"],["attempts","尝试"],["http_status","HTTP"],["status","结果"],
    ["error_code","错误码"],[(row)=>callTokenCell(row,"input_tokens"),"输入 Token"],[(row)=>callTokenCell(row,"output_tokens"),"输出 Token"],[(row)=>callTokenCell(row,"cached_tokens"),"缓存 Token"],
    [(row)=>`${formatNumber(row.duration_ms)} ms`,"耗时"]
  ], body.data.map((row)=>({...row,status:chip(row.status)})));
  const pages = Math.max(1, Math.ceil(body.total / body.page_size));
  $("calls-page").textContent = `第 ${body.page} / ${pages} 页，共 ${body.total} 条`;
  $("calls-prev").disabled = body.page <= 1; $("calls-next").disabled = body.page >= pages;
}

async function refreshUsage(signal) {
  const params = formQuery($("usage-filters")); params.set("range", state.usageRange);
  const [summary, detail] = await Promise.all([api(`/admin/api/usage/summary?range=${state.usageRange}`, {signal}), api(`/admin/api/usage?${params}`, {signal})]);
  if (signal?.aborted) return;
  const rangeNames = {day:"日",week:"周",month:"月",all:"总量"};
  const dates = summary.start_date && summary.end_date ? `${summary.start_date} 至 ${summary.end_date}` : "暂无统计数据";
  $("usage-range-label").textContent = `${rangeNames[state.usageRange]} · ${dates}`;
  $("usage-summary").innerHTML = table([
    ["name","使用者"],["status","状态"],[(row)=>formatTokens(row.input_tokens),"输入"],[(row)=>formatTokens(row.output_tokens),"输出"],
    [(row)=>formatTokens(row.cached_tokens),"缓存"],[(row)=>formatTokens(row.reasoning_tokens),"推理"],[(row)=>formatTokens(row.total_tokens),"合计"]
  ], summary.data.map((row)=>({...row,status:chip(row.status)})));
  const keyNames = Object.fromEntries(summary.data.map((row)=>[row.key_id,row.name]));
  $("usage-detail").innerHTML = table([
    ["usage_date","日期"],[(row)=>keyNames[row.key_id] || row.key_id,"使用者"],["account_id","账号"],["model","模型"],
    [(row)=>formatTokens(row.input_tokens),"输入"],[(row)=>formatTokens(row.output_tokens),"输出"],[(row)=>formatTokens(row.cached_tokens),"缓存"],[(row)=>formatTokens(row.reasoning_tokens),"推理"],[(row)=>formatTokens(row.total_tokens),"合计"],["unknown_usage_count","未知次数"]
  ], detail.data);
}

async function refreshErrors(signal) {
  const body = await api("/admin/api/errors?limit=50", {signal});
  if (signal?.aborted) return;
  $("error-capacity").textContent = body.capacity;
  $("errors").innerHTML = errorTable(body.data);
}

async function refreshSkills(signal) {
  const body = await api("/admin/api/client-skills", {signal});
  if (signal?.aborted) return;
  const sourceName = {uploaded: "已上传", bundled: "内置"};
  $("client-skills").innerHTML = table([
    ["name", "名称"],
    [(row) => sourceName[row.source] || row.source || "内置", "来源"],
    [(row) => String(row.sha256 || "").slice(0, 12), "sha256"],
    [(row) => String((row.files || []).length), "文件"],
    [(row) => {
      const download = `<button class="mini quiet" data-download-skill="${esc(row.name)}">下载</button>`;
      const restore = row.uploaded
        ? `<button class="mini quiet" data-restore-skill="${esc(row.name)}">${row.bundled ? "恢复内置" : "删除"}</button>`
        : "";
      return {html: [download, restore].filter(Boolean).join(" ")};
    }, "操作"],
  ], body.skills || []);
  try {
    const pkg = await api("/admin/api/desktop-package", {signal});
    if (signal?.aborted) return;
    const status = $("desktop-package-status");
    if (!status) return;
    if (!pkg.available) {
      status.textContent = "网站上还没有安装包。";
      return;
    }
    const size = Math.round(Number(pkg.bytes || 0) / (1024 * 1024));
    status.textContent = `当前 ${pkg.name}，约 ${size} MB，sha256 ${String(pkg.sha256 || "").slice(0, 12)}。下载地址 /download/MinKingAI.exe`;
  } catch (error) {
    const status = $("desktop-package-status");
    if (status) status.textContent = error.message;
  }
}

const formatQuotaTime = (value) => value ? formatTime(Number(value) * 1000) : "—";
const formatWindowDuration = (minutes) => {
  const value = Number(minutes);
  if (!Number.isFinite(value) || value <= 0) return "额度窗口";
  if (value % 1440 === 0) return `${value / 1440} 天窗口`;
  if (value % 60 === 0) return `${value / 60} 小时窗口`;
  return `${value} 分钟窗口`;
};
const quotaWindowHtml = (window) => {
  const remaining = Math.max(0, Math.min(100, Number(window.remaining_percent || 0)));
  const label = window.window_label || formatWindowDuration(window.window_minutes);
  const amount = Number.isFinite(Number(window.remaining_amount)) && Number.isFinite(Number(window.total_amount))
    ? ` · ${formatNumber(window.remaining_amount)} / ${formatNumber(window.total_amount)}`
    : "";
  const reset = window.resets_at ? `${formatQuotaTime(window.resets_at)} 重置` : "按套餐周期刷新";
  return `<div class="quota-window"><div><span>${esc(label)}</span><strong class="quota-remaining">剩余 ${esc(remaining.toFixed(1))}%${esc(amount)}</strong></div><div class="quota-progress remaining"><i style="width:${remaining}%"></i></div><small>${esc(reset)}</small></div>`;
};
const quotaUsageCell = (quota, accountId) => {
  if (!quota) {
    if (quotaStatus === "failed") return {html:'<span class="quota-error" title="暂无上游额度数据">获取失败</span>'};
    return {html:'<span class="muted">正在获取…</span>'};
  }
  const windows = (quota.limits || []).flatMap((limit) => [limit.primary, limit.secondary].filter(Boolean));
  if (windows.length) {
    let visible = windows;
    let extra = "";
    if (quota.provider === "antigravity") {
      const gemini = windows.filter((window) => window.window_label === "Gemini");
      const claude = windows.filter((window) => window.window_label === "Claude");
      const expanded = state.quotaExpanded.has(accountId);
      visible = expanded ? gemini.concat(claude) : gemini;
      if (claude.length) {
        extra = `<button type="button" class="quota-more" data-quota-more="${esc(accountId)}">${expanded ? "收起" : "查看更多"}</button>`;
      }
    }
    return {html:`<div class="quota-stack">${visible.map(quotaWindowHtml).join("")}${extra}${quota.stale ? '<span class="quota-error">缓存数据，刷新失败</span>' : ""}</div>`};
  }
  if (quota.message) {
    return {html:`<span class="muted" title="${esc(quota.message)}">${esc(quota.message)}</span>`};
  }
  return {html:`<span class="quota-error" title="${esc(quota.error?.message || "暂无上游额度数据")}">获取失败</span>`};
};
const quotaResetCell = (quota, accountId, provider) => {
  if (provider && provider !== "codex") return {html:'<span class="muted">不可重置</span>'};
  if (!quota) return {html:'<span class="muted">—</span>'};
  const reset = quota.reset_credits || {};
  const cards = (reset.credits || []).filter((item) => item.status === "available");
  const count = Number(reset.available_count || 0);
  if (count <= 0) return {html:'<span class="muted">无可用重置卡</span>'};
  const card = cards[0];
  const expires = card?.expires_at ? `<small>有效期至 ${esc(formatTime(card.expires_at))}</small>` : "";
  return {html:`<div class="quota-reset"><strong>${esc(count)} 张可用</strong>${expires}<button class="mini quiet" data-quota-reset="${esc(accountId)}" data-credit-id="${esc(card?.id || "")}">重置额度</button></div>`};
};

function renderAccountsTable() {
  const filter = $("account-provider-filter")?.value || "";
  const rows = filter ? state.accounts.filter((row) => (row.provider || "codex") === filter) : state.accounts;
  $("account-count").textContent = rows.length;
  $("accounts").innerHTML = table([
    [(row)=>providerNames[row.provider] || row.provider || "Codex","供应商"],
    ["label","登录邮箱/标签"],
    [(row)=>capabilityText(row),"能力"],
    [(row)=>chip(row.status),"状态"],[(row)=>formatTime(row.expires_at),"过期时间"],
    ["current_concurrency","当前并发"],[(row)=>formatTokens(row.total_tokens),"历史 Token"],
    [(row)=>quotaUsageCell(state.quotas.get(row.account_id), row.account_id),"上游额度 / 重置时间"],
    [(row)=>quotaResetCell(state.quotas.get(row.account_id), row.account_id, row.provider),"重置卡"],
    [(row)=>({html:switchHtml({
      checked: row.status === "active",
      disabled: !["active", "disabled", "invalid"].includes(row.status),
      attrs: `data-account="${esc(row.account_id)}"`,
      label: row.status === "invalid" ? "置为有效" : "是否开启",
    })}),"是否开启"],
    [(row)=>({html:`${row.status === "invalid" ? `<button class="mini quiet" data-restore-account="${esc(row.account_id)}">置为有效</button> ` : ""}<button class="mini quiet" data-export-account="${esc(row.account_id)}">导出</button> <button class="mini danger" data-delete-account="${esc(row.account_id)}">删除</button>`}),"操作"]
  ], rows);
}

async function refreshAccountsList(signal) {
  const [body, providers] = await Promise.all([api("/admin/api/accounts", {signal}), api("/admin/api/providers", {signal})]);
  if (signal?.aborted) return;
  setProviders(providers.data);
  state.accounts = body.data;
  renderAccountsTable();
}

async function refreshAccounts(refreshQuota = false, signal) {
  await refreshAccountsList(signal);
  if (signal?.aborted) return;
  const quota = beginQuotaRequest();
  if (signal) signal.addEventListener("abort", () => quotaAbort.abort(), {once: true});
  try {
    const quotaBody = await api(`/admin/api/accounts/quotas${refreshQuota ? "?refresh=true" : ""}`, {signal: quota.signal});
    if (!applyQuotas(quotaBody, quota.generation)) return;
  } catch (error) {
    if (error.name === "AbortError" || quota.signal.aborted) return;
    markQuotaFailed(quota.generation);
  }
  if (state.page === "accounts") renderAccountsTable();
}

function switchHtml({checked, attrs, label, disabled = false}) {
  return `<label class="switch${disabled ? " is-disabled" : ""}" title="${esc(label)}"><input type="checkbox" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""} ${attrs} aria-label="${esc(label)}"><span class="switch-track"></span></label>`;
}

const routeProviders = () => state.providers;
const routeName = (id) => state.accounts.find((row) => row.account_id === id)?.label || id || "等待首次调用";
const routeFor = (key, provider) => (key.routes || []).find((route) => route.provider === provider) || {};

function routeSummary(row) {
  return {html:`<button type="button" class="mini quiet route-manage" data-route-key="${esc(row.id)}">管理路由</button>`};
}

function renderRouteDialog() {
  const key = state.keys.find((row) => row.id === routeKeyId);
  if (!key) { $("route-dialog").close(); return; }
  $("route-dialog-title").textContent = `${key.name} · 上游路由`;
  const providers = routeProviders();
  if (!providers.some((item) => item.id === routeSelected)) routeSelected = providers[0]?.id || "";
  const filtered = providers.filter((item) => `${item.display_name} ${item.id}`.toLowerCase().includes(routeSearch.toLowerCase()));
  const provider = routeSelected;
  const index = providers.findIndex((item) => item.id === provider);
  const detail = provider ? (() => {
    const route = routeFor(key, provider);
    const current = route.active_account_id;
    const available = state.accounts.filter((row) => (row.provider || "codex") === provider && row.status !== "deleted");
    const mode = route.mode === "manual" ? "手动指定" : "自动分配";
    const switched = route.route_status === "failover";
    const cooldowns = route.model_cooldowns || [];
    const options = available.map((row) => {
      const cooling = row.cooldown_until && new Date(row.cooldown_until) > new Date();
      const disabled = row.status !== "active" || cooling;
      const suffix = disabled ? `（${cooling ? "账号冷却中" : statusText(row.status)}）` : "";
      return `<option value="${esc(row.account_id)}" ${row.account_id === current ? "selected" : ""} ${disabled ? "disabled" : ""}>${esc(row.label || row.account_id)} ${esc(suffix)}</option>`;
    }).join("");
    return `<section class="route-card" aria-label="${esc(providerNames[provider])} 路由">
      <div class="route-card-top"><span class="route-glyph">${["✦","✧","✳","❖","◈","◇"][index % 6]}</span><div><span class="route-card-kicker">CHANNEL ${String(index + 1).padStart(2,"0")}</span><h3>${esc(providerNames[provider])}</h3></div><span class="route-mode ${switched ? "warn" : ""}">${switched ? "故障切换" : mode}</span></div>
      <div class="route-current"><span>当前连接</span><strong>${esc(routeName(current))}</strong><small>${current ? esc(current) : "首次调用时自动分配"}</small></div>
      <div class="route-meta"><span>连续失败 <b>${Number(route.consecutive_failures || 0)}/2</b></span><span>${cooldowns.length ? `${cooldowns.length} 个模型冷却中` : "模型状态正常"}</span></div>
      ${cooldowns.length ? `<p class="route-cooldown">${cooldowns.map((item) => `${esc(item.model)} 至 ${esc(formatTime(item.until))}`).join("<br>")}</p>` : ""}
      <label class="route-select-label">切换到指定账号<select data-route-select="${esc(provider)}"><option value="">选择一个健康账号</option>${options}</select></label>
      <div class="route-actions"><button type="button" data-route-save="${esc(provider)}" ${!available.some((row) => row.status === "active") ? "disabled" : ""}>设为当前账号</button><button type="button" class="quiet" data-route-auto="${esc(provider)}">恢复自动分配</button></div>
    </section>`;
  })() : '<p class="muted">暂无已注册供应商</p>';
  $("route-dialog-body").innerHTML = `<div class="route-browser"><div class="route-directory"><label class="route-search-label">查找供应商<input id="route-search" type="search" placeholder="名称或 ID" value="${esc(routeSearch)}"></label><div class="route-directory-list">${filtered.map((item) => {
    const route = routeFor(key, item.id);
    return `<button type="button" class="route-directory-item ${item.id === routeSelected ? "selected" : ""}" data-route-pick="${esc(item.id)}"><span class="route-pill-dot ${route.active_account_id ? "online" : ""}"></span><span><strong>${esc(item.display_name)}</strong><small>${esc(route.active_account_id ? routeName(route.active_account_id) : "自动分配 · 待调用")}</small></span><span>›</span></button>`;
  }).join("") || '<p class="muted">没有匹配的供应商</p>'}</div></div><div class="route-detail">${detail}</div></div>`;
}

async function refreshKeys(signal) {
  const [body, accounts, providers] = await Promise.all([api("/admin/api/keys", {signal}), api("/admin/api/accounts", {signal}), api("/admin/api/providers", {signal})]);
  if (signal?.aborted) return;
  setProviders(providers.data);
  state.keys = Array.isArray(body.data) ? body.data : [];
  state.accounts = Array.isArray(accounts.data) ? accounts.data : [];
  $("keys").innerHTML = table([
    [(row)=>row.name,"人员名称"],
    [(row)=>row.email || "—","邮箱"],
    [(row)=>({html:row.key
      ? `<code id="key-${esc(row.id)}">••••••••••••••••</code> <button class="mini quiet" data-reveal-key="${esc(row.id)}" data-full-key="${esc(row.key)}">显示</button> <button class="mini quiet" data-copy-key="${esc(row.key)}">复制</button>`
      : `<span class="muted">历史 Key 无法恢复</span> <button class="mini quiet" data-rotate-key="${esc(row.id)}">轮换</button>`}),"完整 API Key"],
    [(row)=>chip(row.status),"状态"],
    [(row)=>({html: row.status === "revoked" ? "—" : switchHtml({
      checked: row.status === "active",
      attrs: `data-key="${esc(row.id)}"`,
      label: "是否开启",
    })}),"是否开启"],
    [(row)=>({html:switchHtml({checked: Boolean(row.fast_enabled), attrs: `data-fast-key="${esc(row.id)}"`, label: "Fast"})}),"Fast"],
    [routeSummary,"上游路由"],
    [(row)=>formatUsd(row.usd_credit),"余额"],
    [(row)=>formatTime(row.last_used_at),"最后使用"],
    [(row)=>({html:`<button class="mini quiet" data-credit-key="${esc(row.id)}" data-credit-name="${esc(row.name)}">加美元</button> <button class="mini quiet" data-bundle-key="${esc(row.id)}" ${!row.recoverable || row.status !== "active" ? "disabled" : ""}>下载 ZIP</button> <button class="mini quiet" data-rotate-key="${esc(row.id)}">轮换</button> <button class="mini danger" data-delete-key="${esc(row.id)}">删除</button>`}),"操作"]
  ], state.keys);
  if ($("route-dialog").open) renderRouteDialog();
}

$("route-dialog-close").addEventListener("click", () => $("route-dialog").close());
$("route-dialog").addEventListener("click", (event) => {
  if (event.target === $("route-dialog")) $("route-dialog").close();
});
$("route-dialog-body").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (button?.dataset.routePick) { routeSelected = button.dataset.routePick; renderRouteDialog(); return; }
  if (!button || (!button.dataset.routeSave && !button.dataset.routeAuto)) return;
  const provider = button.dataset.routeSave || button.dataset.routeAuto;
  const accountId = button.dataset.routeSave
    ? $("route-dialog-body").querySelector(`[data-route-select="${provider}"]`)?.value : null;
  const message = $("route-dialog-message");
  if (button.dataset.routeSave && !accountId) {
    message.textContent = "请先选择一个健康账号。"; message.classList.add("error"); return;
  }
  button.disabled = true;
  try {
    await api(`/admin/api/keys/${encodeURIComponent(routeKeyId)}/route`, {
      method:"PUT", body:JSON.stringify({provider, preferred_account_id:accountId}),
    });
    message.textContent = accountId ? "已更新当前账号，后续请求将使用新路由。" : "已恢复自动分配，首次调用时选择健康账号。";
    message.classList.remove("error");
    await refreshKeys();
  } catch (error) {
    message.textContent = error.message; message.classList.add("error");
    button.disabled = false;
  }
});
$("route-dialog-body").addEventListener("input", (event) => {
  if (event.target.id !== "route-search") return;
  routeSearch = event.target.value;
  const cursor = event.target.selectionStart;
  renderRouteDialog();
  $("route-search").focus();
  $("route-search").setSelectionRange(cursor, cursor);
});

async function refreshModels(signal) {
  const body = await api("/admin/api/models", {signal});
  if (signal?.aborted) return;
  const sourceName = {builtin: "内置", upstream: "上游", manual: "手动"};
  $("models").innerHTML = table([
    ["id","模型"],
    [(row)=>providerNames[row.provider] || row.provider || "—","供应商"],
    [(row)=>row.type === "video" ? "视频" : row.type === "image" ? "图片" : "文本","类型"],
    [(row)=>row.capabilities.join("、"),"能力"],
    [(row)=>sourceName[row.source] || row.source || "内置","来源"],
    [(row)=>row.default ? "是" : "否","默认"],
    [(row)=>row.priced ? "已定价" : {html:'<span class="price-missing">未定价</span>'},"定价"],
    [(row)=>row.available ? (row.cooled ? chip("cooling") : chip("active")) : chip("disabled"),"状态"],
    [(row)=>{
      const restore = !row.available || row.cooled
        ? `<button class="mini quiet" data-restore-model="${esc(row.id)}" data-restore-provider="${esc(row.provider)}">置为有效</button>`
        : "";
      const remove = row.source && row.source !== "builtin"
        ? `<button class="mini quiet" data-remove-model="${esc(row.id)}" data-remove-provider="${esc(row.provider)}">移除</button>`
        : "";
      const html = [restore, remove].filter(Boolean).join(" ");
      return html ? {html} : "—";
    }, "操作"],
  ], body.data);
}

function priceLines(model, price) {
  const kind = price?.modality || model.type || "text";
  const official = price?.official || {};
  const sell = price?.sell || {};
  const rate = price?.multiplier_override || price?.multiplier || "全局";
  if (!model.priced && !price?.priced) return {kind, rate: "—", official: "未配置", sell: "—"};
  if (kind === "image") return {kind, rate, official: `${formatUsd(official.usd_per_image || price.official_usd_per_image)} / 张`, sell: `${formatUsd(sell.usd_per_image)} / 张`};
  if (kind === "video") return {kind, rate, official: `${formatUsd(official.usd_per_second || price.official_usd_per_second)} / 秒`, sell: `${formatUsd(sell.usd_per_second)} / 秒`};
  const officialText = `输入 ${formatUsd(official.input_usd_per_1m || price.official_input_usd_per_1m)} · 输出 ${formatUsd(official.output_usd_per_1m || price.official_output_usd_per_1m)}`;
  const sellText = `输入 ${formatUsd(sell.input_usd_per_1m)} · 输出 ${formatUsd(sell.output_usd_per_1m)}`;
  return {kind: "text", rate, official: officialText, sell: sellText};
}

async function refreshBilling(signal) {
  const [settingsBody, prices, models] = await Promise.all([
    api("/admin/api/billing/settings", {signal}),
    api("/admin/api/billing/prices", {signal}),
    api("/admin/api/models", {signal}),
  ]);
  if (signal?.aborted) return;
  $("billing-multiplier").value = settingsBody.price_multiplier || "0.12";
  $("billing-new-user").value = settingsBody.new_user_usd || "0.00";
  $("billing-budget").value = settingsBody.request_budget_usd || "0.50";
  const priceById = new Map((prices.data || []).map((row) => [row.model, row]));
  const rows = (models.data || []).filter((row) => !row.alias_of).map((model) => {
    const price = priceById.get(model.id) || {};
    return {...model, price, priced: Boolean(model.priced)};
  });
  const quoteModel = $("quote-model");
  const previous = quoteModel.value;
  quoteModel.replaceChildren();
  for (const row of rows.filter((item) => item.priced)) {
    quoteModel.append(new Option(row.id, row.id));
  }
  if ([...quoteModel.options].some((option) => option.value === previous)) quoteModel.value = previous;
  $("billing-prices").innerHTML = table([
    ["id", "模型"],
    [(row) => providerNames[row.provider] || row.provider, "供应商"],
    [(row) => ({text: "文本", image: "图片", video: "视频"}[priceLines(row, row.price).kind] || row.type), "类型"],
    [(row) => priceLines(row, row.price).official, "官方定价"],
    [(row) => priceLines(row, row.price).rate, "倍率"],
    [(row) => priceLines(row, row.price).sell, "售价"],
    [(row) => row.priced ? "已定价" : {html: '<span class="price-missing">未定价</span>'}, "状态"],
    [(row) => ({html: `<button class="mini quiet" data-price-model="${esc(row.id)}" data-price-provider="${esc(row.provider)}" data-price-type="${esc(row.type || "text")}" data-price-rate="${esc(row.price.multiplier_override || "")}" data-price-input="${esc(row.price.official_input_usd_per_1m || "")}" data-price-output="${esc(row.price.official_output_usd_per_1m || "")}" data-price-cached="${esc(row.price.official_cached_usd_per_1m || "")}" data-price-image="${esc(row.price.official_usd_per_image || "")}" data-price-second="${esc(row.price.official_usd_per_second || "")}">设置价格</button>`}), "操作"],
  ], rows);
}

async function refreshCards(signal) {
  const body = await api("/admin/api/cards?page=1&page_size=100", {signal});
  if (signal?.aborted) return;
  $("cards").innerHTML = table([
    ["prefix", "前缀"],
    [(row) => formatUsd(row.amount_usd), "面额"],
    [(row) => chip(row.status), "状态"],
    ["note", "备注"],
    ["batch_id", "批次"],
    [(row) => formatTime(row.created_at), "创建"],
    [(row) => formatTime(row.redeemed_at), "兑换"],
    [(row) => row.status === "unused" || row.status === "expired"
      ? ({html: `<button class="mini quiet" data-disable-card="${esc(row.id)}">禁用</button>`})
      : "—", "操作"],
  ], body.data || []);
}

async function refreshLedger(signal) {
  const params = formQuery($("ledger-filters"));
  params.set("page", String(state.ledgerPage));
  params.set("page_size", "25");
  const body = await api(`/admin/api/wallet/ledger?${params}`, {signal});
  if (signal?.aborted) return;
  state.ledgerTotal = Number(body.total || 0);
  $("ledger").innerHTML = table([
    [(row) => formatTime(row.created_at), "时间"],
    ["email", "邮箱"],
    [(row) => chip(row.kind), "类型"],
    [(row) => formatUsd(row.amount_usd), "金额"],
    [(row) => formatUsd(row.balance_after), "余额"],
    ["reason", "原因"],
    ["actor", "操作者"],
  ], body.data || []);
  const pages = Math.max(1, Math.ceil(state.ledgerTotal / 25));
  $("ledger-page").textContent = `第 ${body.page} / ${pages} 页，共 ${body.total} 条`;
  $("ledger-prev").disabled = body.page <= 1;
  $("ledger-next").disabled = body.page >= pages;
}

const refreshers = {
  dashboard: refreshDashboard,
  accounts: (signal) => refreshAccounts(false, signal),
  keys: refreshKeys,
  calls: refreshCalls,
  usage: refreshUsage,
  models: refreshModels,
  billing: refreshBilling,
  cards: refreshCards,
  ledger: refreshLedger,
  errors: refreshErrors,
  skills: refreshSkills,
};
async function refreshPage(page = state.page) {
  pageAbort?.abort();
  const abort = new AbortController();
  pageAbort = abort;
  const generation = ++pageGeneration;
  const signal = abort.signal;
  $("global-status").textContent = "加载中";
  $("global-status").style.color = "";
  try {
    await refreshers[page]?.(signal);
    if (generation !== pageGeneration || signal.aborted) return;
    $("global-status").textContent = "已连接";
  } catch (error) {
    if (signal.aborted || error.name === "AbortError" || generation !== pageGeneration) return;
    if (error.status !== 401) { $("global-status").textContent = error.message; $("global-status").style.color = "var(--danger)"; }
  }
}

function switchPage(page) {
  state.page = page; $("page-title").textContent = pageTitles[page] || page;
  document.querySelectorAll("[data-page-panel]").forEach((panel)=>panel.classList.toggle("active", panel.dataset.pagePanel === page));
  document.querySelectorAll(".nav-item[data-page]").forEach((button)=>button.classList.toggle("active", button.dataset.page === page));
  $("sidebar").classList.remove("open"); $("sidebar-backdrop").classList.add("hidden");
  refreshPage(page);
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-error").classList.add("hidden");
  try { const session = await api("/admin/api/auth/login", {method:"POST",body:JSON.stringify({username:$("login-username").value.trim(),password:$("login-password").value})}); showApp(session); }
  catch (error) { showLogin(error.message); }
});

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const data = new FormData(); for (const file of $("auth-files").files) data.append("files[]", file);
  data.append("provider", $("import-provider").value || "codex");
  try { const body = await api("/admin/api/accounts/import", {method:"POST",body:data}); $("import-result").innerHTML = `新增 ${body.created.length}，更新 ${body.updated.length}，失败 ${body.failed.length}<br>请到“使用者 / API Key”页面创建 Key。`; $("import-result").className = "notice"; await refreshPage("accounts"); }
  catch (error) { $("import-result").textContent = error.message; $("import-result").className = "notice error"; }
});

$("import-local-grok").addEventListener("click", async () => {
  try {
    const body = await api("/admin/api/accounts/import-local", {method:"POST",body:JSON.stringify({provider:"grok"})});
    $("import-result").innerHTML = `本机 Grok：新增 ${body.created.length}，更新 ${body.updated.length}，失败 ${body.failed.length}`;
    $("import-result").className = "notice";
    await refreshPage("accounts");
  } catch (error) {
    $("import-result").textContent = error.message;
    $("import-result").className = "notice error";
  }
});

$("import-local-antigravity").addEventListener("click", async () => {
  try {
    const body = await api("/admin/api/accounts/import-local", {method:"POST",body:JSON.stringify({provider:"antigravity"})});
    $("import-result").innerHTML = `本机 Antigravity：新增 ${body.created.length}，更新 ${body.updated.length}，失败 ${body.failed.length}`;
    $("import-result").className = "notice";
    await refreshPage("accounts");
  } catch (error) {
    $("import-result").textContent = error.message;
    $("import-result").className = "notice error";
  }
});

function stopAccountOauth() {
  accountOauth.generation += 1;
  if (accountOauth.timer) {
    clearInterval(accountOauth.timer);
    accountOauth.timer = null;
  }
  accountOauth.state = "";
  accountOauth.startedAt = 0;
  accountOauth.pollErrors = 0;
  $("oauth-panel").classList.add("hidden");
  $("oauth-callback-url").value = "";
  $("oauth-link").href = "#";
  $("oauth-user-code").textContent = "";
  $("oauth-user-code").classList.add("hidden");
  $("oauth-copy-wrap").classList.add("hidden");
  $("oauth-form").classList.remove("hidden");
}

function showImportCounts(prefix, body) {
  $("import-result").innerHTML = `${prefix}新增 ${body.created.length}，更新 ${body.updated.length}，失败 ${(body.failed || []).length}<br>请到“使用者 / API Key”页面创建 Key。`;
  $("import-result").className = "notice";
}

function oauthProviderLabel(provider) {
  return providerNames[provider] || provider || "上游";
}

async function pollAccountOauth(generation) {
  if (!accountOauth.state || generation !== accountOauth.generation) return;
  try {
    const body = await api(`/admin/api/accounts/oauth/status?state=${encodeURIComponent(accountOauth.state)}`);
    accountOauth.pollErrors = 0;
    if (body.status === "completed") {
      const provider = body.provider || accountOauth.provider;
      stopAccountOauth();
      $("account-provider-filter").value = "";
      showImportCounts(`${oauthProviderLabel(provider)} 登录：`, body);
      await refreshPage("accounts");
    } else if (body.status === "failed") {
      stopAccountOauth();
      $("import-result").textContent = body.error || "登录失败";
      $("import-result").className = "notice error";
    } else if (accountOauth.provider === "workbuddy" && Date.now() - accountOauth.startedAt > 30000) {
      $("oauth-hint").textContent = "仍在等待 WorkBuddy 授权。请在下方打开的专用登录页完成登录和授权；只登录官网首页不会绑定本次请求。";
    }
  } catch (error) {
    if (error.status === 410) {
      stopAccountOauth();
      $("import-result").textContent = "登录已过期，请重新打开登录页";
      $("import-result").className = "notice error";
    } else {
      accountOauth.pollErrors += 1;
      const terminal = accountOauth.pollErrors >= 3 || error.status === 422;
      const message = `查询登录结果失败：${error.message}。${terminal ? "请重新打开登录页。" : "正在重试。"}`;
      if (terminal) {
        stopAccountOauth();
        $("import-result").textContent = message;
        $("import-result").className = "notice error";
      } else $("oauth-hint").textContent = message;
    }
  }
}

function setAccountMode(mode) {
  const login = mode !== "files";
  document.querySelectorAll("[data-account-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.accountMode === (login ? "login" : "files"));
  });
  $("account-login-pane").classList.toggle("hidden", !login);
  $("account-files-pane").classList.toggle("hidden", login);
  if (!login) stopAccountOauth();
}

document.querySelectorAll("[data-account-mode]").forEach((button) => {
  button.addEventListener("click", () => setAccountMode(button.dataset.accountMode));
});

$("oauth-start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const provider = $("oauth-provider").value || "codex";
  // Open during the click itself; opening after the API await is blocked by some browsers.
  let loginTab = null;
  try {
    loginTab = window.open("about:blank", "_blank");
    if (loginTab) loginTab.opener = null;
  } catch (_) { /* The explicit link below remains available. */ }
  try {
    const realm = provider === "workbuddy" ? $("oauth-workbuddy-realm").value : "cn";
    const started = await api("/admin/api/accounts/oauth/start", {method:"POST",body:JSON.stringify({provider,realm})});
    if (accountOauth.timer) clearInterval(accountOauth.timer);
    accountOauth.generation += 1;
    accountOauth.provider = started.provider || provider;
    accountOauth.state = started.state;
    accountOauth.startedAt = Date.now();
    accountOauth.pollErrors = 0;
    $("oauth-panel").classList.remove("hidden");
    $("oauth-callback-url").value = "";
    $("oauth-link").href = started.auth_url;
    const name = oauthProviderLabel(accountOauth.provider);
    const device = started.login_mode === "device";
    const polled = started.login_mode === "poll";
    $("oauth-form").classList.toggle("hidden", device || polled);
    $("oauth-user-code").classList.toggle("hidden", !device || !started.user_code);
    $("oauth-copy-wrap").classList.toggle("hidden", !device || !started.user_code);
    $("oauth-user-code").textContent = device ? (started.user_code || "") : "";
    if (device) {
      $("oauth-hint").textContent = `已打开带代码 ${started.user_code || ""} 的 xAI 登录页。登录并允许后，账号会自动出现。也可复制短码手动输入。`;
    } else if (polled) {
      $("oauth-hint").textContent = `在 ${name} 专用登录页完成登录和授权，账号会自动出现在列表中。凭据保存在本服务器；不会修改电脑上的 workbuddy-desktop.info，可在账号列表导出。`;
    } else {
      $("oauth-hint").textContent = started.listen_bound
        ? `已打开 ${name} 登录页。登录完成后一般会自动回来；如果浏览器停在 localhost 打不开，把地址栏完整网址贴到下面。`
        : `已打开 ${name} 登录页。登录完成后浏览器会跳到 localhost（页面可能打不开），把地址栏完整网址贴到下面。`;
    }
    if (loginTab && !loginTab.closed) loginTab.location.replace(started.auth_url);
    else $("oauth-hint").textContent += " 浏览器拦截了弹窗，请点击下方“打开登录页”链接。";
    const generation = accountOauth.generation;
    accountOauth.timer = setInterval(() => pollAccountOauth(generation), device ? 5000 : polled ? 2500 : 1500);
  } catch (error) {
    if (loginTab && !loginTab.closed) loginTab.close();
    $("import-result").textContent = error.message;
    $("import-result").className = "notice error";
  }
});

$("oauth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const callbackUrl = $("oauth-callback-url").value.trim();
  if (!callbackUrl || !accountOauth.state) return;
  try {
    const body = await api("/admin/api/accounts/oauth/callback", {
      method: "POST",
      body: JSON.stringify({
        provider: accountOauth.provider,
        state: accountOauth.state,
        callback_url: callbackUrl,
      }),
    });
    stopAccountOauth();
    showImportCounts(`${oauthProviderLabel(body.provider || accountOauth.provider)} 登录：`, body);
    await refreshPage("accounts");
  } catch (error) {
    $("import-result").textContent = error.message;
    $("import-result").className = "notice error";
  }
});

$("oauth-copy-code").addEventListener("click", async () => {
  const value = $("oauth-user-code").textContent.trim();
  if (!value) return;
  try {
    await copyText(value);
    $("oauth-copy-code").textContent = "已复制";
  } catch {
    $("oauth-copy-code").textContent = "复制失败";
  }
});

$("oauth-cancel").addEventListener("click", () => stopAccountOauth());
$("oauth-provider").addEventListener("change", () => {
  $("oauth-workbuddy-realm-wrap").classList.toggle("hidden", $("oauth-provider").value !== "workbuddy");
});

$("account-provider-filter").addEventListener("change", () => renderAccountsTable());
$("import-provider").addEventListener("change", () => {
  const provider = $("import-provider").value;
  $("import-local-grok").classList.toggle("hidden", provider !== "grok");
  $("import-local-antigravity").classList.toggle("hidden", provider !== "antigravity");
});

$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const body = await api("/admin/api/keys", {method:"POST",body:JSON.stringify({name:$("key-name").value.trim(),fast_enabled:$("key-fast-enabled").checked})});
    $("new-key").innerHTML = `请立即保存：<code>${esc(body.key)}</code>`;
    $("new-key").className = "notice";
    event.target.reset();
    await refreshPage("keys");
  } catch (error) { $("new-key").textContent = error.message; $("new-key").className = "notice error"; }
});

$("call-filters").addEventListener("submit", (event)=>{event.preventDefault();state.callsPage=1;refreshPage("calls");});
$("usage-filters").addEventListener("submit", (event)=>{event.preventDefault();refreshPage("usage");});
document.querySelectorAll("[data-usage-range]").forEach((button)=>button.addEventListener("click",()=>{
  state.usageRange = button.dataset.usageRange;
  document.querySelectorAll("[data-usage-range]").forEach((item)=>item.classList.toggle("active", item === button));
  refreshPage("usage");
}));
$("calls-prev").addEventListener("click", ()=>{if(state.callsPage>1){state.callsPage--;refreshPage("calls");}});
$("calls-next").addEventListener("click", ()=>{if(state.callsPage*25<state.callsTotal){state.callsPage++;refreshPage("calls");}});

$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = new FormData(event.target); const current = form.get("current_password"); const next = form.get("new_password");
  if (next !== form.get("confirm_password")) { $("password-result").textContent = "两次输入的新密码不一致"; $("password-result").className = "notice error"; return; }
  try { const body = await api("/admin/api/auth/password", {method:"PUT",body:JSON.stringify({current_password:current,new_password:next})}); state.csrf = body.csrf_token; event.target.reset(); $("password-result").textContent = "密码已更新，其他管理会话已失效。"; $("password-result").className = "notice"; }
  catch (error) { $("password-result").textContent = error.message; $("password-result").className = "notice error"; }
});

$("skill-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData();
  const file = $("skill-zip").files[0];
  if (!file) { $("skill-upload-result").textContent = "请选择 zip"; $("skill-upload-result").className = "notice error"; return; }
  data.append("file", file);
  const name = $("skill-name").value.trim();
  if (name) data.append("name", name);
  try {
    const body = await api("/admin/api/client-skills", {method:"POST", body:data});
    $("skill-upload-result").textContent = `已覆盖 ${body.skill?.name || name || file.name}，客户端下次同步会拉到新版本。`;
    $("skill-upload-result").className = "notice";
    event.target.reset();
    await refreshPage("skills");
  } catch (error) {
    $("skill-upload-result").textContent = error.message;
    $("skill-upload-result").className = "notice error";
  }
});

$("desktop-package-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData();
  const file = $("desktop-package-file").files[0];
  if (!file) {
    $("desktop-package-result").textContent = "请选择安装包";
    $("desktop-package-result").className = "notice error";
    return;
  }
  data.append("file", file);
  try {
    const body = await api("/admin/api/desktop-package", {method:"POST", body:data});
    const digest = String(body.package?.sha256 || "").slice(0, 12);
    $("desktop-package-result").textContent = `已更新网站安装包 ${digest}。用户重新下载即可。`;
    $("desktop-package-result").className = "notice";
    event.target.reset();
    await refreshPage("skills");
  } catch (error) {
    $("desktop-package-result").textContent = error.message;
    $("desktop-package-result").className = "notice error";
  }
});

$("logout").addEventListener("click", async ()=>{try{await api("/admin/api/auth/logout",{method:"POST"});}finally{showLogin();}});
document.querySelectorAll(".nav-item[data-page]").forEach((button)=>button.addEventListener("click",()=>switchPage(button.dataset.page)));
document.querySelectorAll("[data-refresh]").forEach((button)=>button.addEventListener("click",()=>refreshPage(button.dataset.refresh)));
$("model-add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("model-add-message");
  const body = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    await api("/admin/api/models", {method:"POST", body:JSON.stringify(body)});
    message.textContent = "已添加。客户端下次打开模型列表就能看到。";
    message.classList.remove("error");
    await refreshModels();
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  }
});
$("models-pull").addEventListener("click", async () => {
  const message = $("model-add-message");
  try {
    const body = await api("/admin/api/models/refresh", {method:"POST"});
    const counts = body.counts || {};
    const version = body.codex_client_version ? ` 当前 Codex 客户端版本 ${body.codex_client_version}。` : "";
    message.textContent = `上游返回 Codex ${counts.codex || 0}、Grok ${counts.grok || 0}、Antigravity ${counts.antigravity || 0}、WorkBuddy ${counts.workbuddy || 0}。没有返回的上游保持上次的列表。${version}`;
    message.classList.remove("error");
    await refreshModels();
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  }
});
$("menu-toggle").addEventListener("click",()=>{$("sidebar").classList.add("open");$("sidebar-backdrop").classList.remove("hidden");});
$("sidebar-backdrop").addEventListener("click",()=>{$("sidebar").classList.remove("open");$("sidebar-backdrop").classList.add("hidden");});
$("refresh-quotas").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  pageAbort?.abort();
  const abort = new AbortController();
  pageAbort = abort;
  const generation = ++pageGeneration;
  $("global-status").textContent = "加载中";
  $("global-status").style.color = "";
  try {
    await refreshAccounts(true, abort.signal);
    if (generation !== pageGeneration || abort.signal.aborted) return;
    $("global-status").textContent = "额度已刷新";
  } catch (error) {
    if (abort.signal.aborted || error.name === "AbortError" || generation !== pageGeneration) return;
    $("global-status").textContent = error.message; $("global-status").style.color = "var(--danger)";
  }
  finally { event.currentTarget.disabled = false; }
});

const quotaResetDialog = $("quota-reset-dialog");
const quotaResetPhrase = $("quota-reset-phrase");
const quotaResetConfirm = $("quota-reset-confirm");
function clearQuotaResetDialog() {
  quotaResetDialog.dataset.accountId = "";
  quotaResetDialog.dataset.creditId = "";
  quotaResetPhrase.value = "";
  quotaResetConfirm.disabled = true;
}
quotaResetPhrase.addEventListener("input", () => {
  quotaResetConfirm.disabled = quotaResetPhrase.value !== "确定重置";
});
$("quota-reset-cancel").addEventListener("click", () => quotaResetDialog.close());
quotaResetDialog.addEventListener("close", clearQuotaResetDialog);
$("quota-reset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (quotaResetPhrase.value !== "确定重置" || !quotaResetDialog.dataset.accountId) return;
  quotaResetConfirm.disabled = true;
  const idempotencyKey = crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const result = await api(`/admin/api/accounts/${encodeURIComponent(quotaResetDialog.dataset.accountId)}/quota/reset`, {
      method:"POST",
      body:JSON.stringify({idempotency_key:idempotencyKey,credit_id:quotaResetDialog.dataset.creditId || null,confirmation_phrase:quotaResetPhrase.value}),
    });
    const messages = {reset:"额度已重置",nothing_to_reset:"当前没有需要重置的额度",no_credit:"没有可用重置卡",already_redeemed:"该重置卡已使用"};
    quotaResetDialog.close();
    pageAbort?.abort();
    const abort = new AbortController();
    pageAbort = abort;
    const generation = ++pageGeneration;
    $("global-status").textContent = "加载中";
    $("global-status").style.color = "";
    await refreshAccounts(true, abort.signal);
    if (generation === pageGeneration && !abort.signal.aborted) {
      $("global-status").textContent = messages[result.outcome] || "重置请求已完成";
    }
  } catch (error) {
    $("global-status").textContent = error.message;
    $("global-status").style.color = "var(--danger)";
    quotaResetConfirm.disabled = quotaResetPhrase.value !== "确定重置";
  }
});

document.addEventListener("change", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement) || target.type !== "checkbox" || target.id === "key-fast-enabled") return;
  const previous = !target.checked;
  try {
    if (target.dataset.account) {
      const account = state.accounts.find((row) => row.account_id === target.dataset.account);
      if (target.checked && account?.status === "invalid" && !confirm("该账号已被标为失效。确认恢复为有效？冷却和该账号上的模型失败记录会清除。")) {
        target.checked = false;
        return;
      }
      await api(`/admin/api/accounts/${encodeURIComponent(target.dataset.account)}`, {method:"PATCH",body:JSON.stringify({enabled:target.checked})});
      if (state.page === "accounts") { await refreshAccountsList(); return; }
    } else if (target.dataset.fastKey) {
      await api(`/admin/api/keys/${target.dataset.fastKey}`, {method:"PATCH",body:JSON.stringify({fast_enabled:target.checked})});
    } else if (target.dataset.key) {
      await api(`/admin/api/keys/${target.dataset.key}`, {method:"PATCH",body:JSON.stringify({enabled:target.checked})});
    } else return;
    $("global-status").textContent = "已连接";
    $("global-status").style.color = "";
    await refreshPage();
  } catch (error) {
    target.checked = previous;
    $("global-status").textContent = error.message;
    $("global-status").style.color = "var(--danger)";
  }
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button"); if (!target) return;
  try {
    if (target.dataset.downloadSkill) {
      const name = target.dataset.downloadSkill;
      const response = await fetch(`${appRootPath}/admin/api/client-skills/${encodeURIComponent(name)}`, {
        credentials: "same-origin",
        headers: {"X-CSRF-Token": state.csrf},
      });
      if (!response.ok) {
        const text = await response.text();
        let message = response.statusText;
        try { message = JSON.parse(text)?.error?.message || message; } catch {}
        throw new Error(message);
      }
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = `${name}.zip`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 30000);
      return;
    }
    if (target.dataset.restoreSkill) {
      const name = target.dataset.restoreSkill;
      if (!confirm(`确定恢复 ${name} 为内置版本吗？已上传的覆盖会被删掉。`)) return;
      await api(`/admin/api/client-skills/${encodeURIComponent(name)}`, {method:"DELETE"});
      await refreshPage("skills");
      return;
    }
    if (target.dataset.restoreModel) {
      if (!confirm("确认把该模型恢复为有效？将清除这个模型的冷却。如果该供应商没有健康账号，失效和已删除且仍有凭据的账号会一并恢复；手动禁用的账号保持不变。")) return;
      const body = await api("/admin/api/models/restore", {
        method:"POST",
        body:JSON.stringify({provider: target.dataset.restoreProvider, model_id: target.dataset.restoreModel}),
      });
      const message = $("model-add-message");
      message.textContent = body.available
        ? (body.revived_accounts ? `已恢复为有效，并重新启用 ${body.revived_accounts} 个账号。` : "该模型已恢复为有效。")
        : "该供应商没有可恢复的账号。请重新导入上游账号。";
      message.classList.toggle("error", !body.available);
      await refreshModels();
      return;
    }
    if (target.dataset.removeModel) {
      const params = new URLSearchParams({provider: target.dataset.removeProvider, model_id: target.dataset.removeModel});
      await api(`/admin/api/models?${params}`, {method:"DELETE"});
      await refreshModels();
      return;
    }
    if (target.dataset.restoreAccount) {
      if (!confirm("该账号已被标为失效。确认恢复为有效？冷却和该账号上的模型失败记录会清除。")) return;
      await api(`/admin/api/accounts/${encodeURIComponent(target.dataset.restoreAccount)}`, {method:"PATCH", body:JSON.stringify({enabled:true})});
      $("global-status").textContent = "账号已恢复为有效";
      $("global-status").style.color = "";
      if (state.page === "accounts") await refreshAccountsList();
      else await refreshPage();
      return;
    }
    if (target.dataset.routeKey) {
      routeKeyId = target.dataset.routeKey;
      routeSearch = "";
      routeSelected = state.providers[0]?.id || "";
      $("route-dialog-message").textContent = "";
      renderRouteDialog();
      $("route-dialog").showModal();
      return;
    }
    if (target.dataset.bundleKey) {
      const response = await fetch(`${appRootPath}/admin/api/keys/${encodeURIComponent(target.dataset.bundleKey)}/bundle`, {method:"POST",credentials:"same-origin",headers:{"X-CSRF-Token":state.csrf}});
      if (!response.ok) { const body = await response.json(); throw new Error(body.error?.message || response.statusText); }
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = "minking-api-codex.zip";
      link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 30000);
      return;
    }
    if (target.dataset.revealKey) {
      const code = $(`key-${target.dataset.revealKey}`); const showing = target.textContent === "隐藏";
      code.textContent = showing ? "••••••••••••••••" : target.dataset.fullKey;
      target.textContent = showing ? "显示" : "隐藏"; return;
    }
    if (target.dataset.copyKey) {
      try {
        await copyText(target.dataset.copyKey);
        target.textContent = "已复制";
      } catch {
        target.textContent = "复制失败";
      }
      return;
    }
    if (target.dataset.quotaMore) {
      const accountId = target.dataset.quotaMore;
      if (state.quotaExpanded.has(accountId)) state.quotaExpanded.delete(accountId);
      else state.quotaExpanded.add(accountId);
      renderAccountsTable();
      return;
    }
    if (target.dataset.quotaReset) {
      quotaResetDialog.dataset.accountId = target.dataset.quotaReset;
      quotaResetDialog.dataset.creditId = target.dataset.creditId || "";
      quotaResetDialog.showModal();
      quotaResetPhrase.focus();
      return;
    }
    if (target.dataset.exportAccount) {
      const accountId = target.dataset.exportAccount;
      const response = await fetch(`${appRootPath}/admin/api/accounts/${encodeURIComponent(accountId)}/export`, {
        credentials: "same-origin",
        headers: {"X-CSRF-Token": state.csrf},
      });
      if (!response.ok) {
        const text = await response.text();
        let message = response.statusText;
        try { message = JSON.parse(text)?.error?.message || message; } catch {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = match?.[1] || "auth.json";
      link.click();
      URL.revokeObjectURL(link.href);
      $("global-status").textContent = "账号文件已导出";
      $("global-status").style.color = "";
      return;
    }
    if (target.dataset.deleteAccount && confirm("确定删除该账号吗？再次导入同一账号可以恢复。")) {
      await api(`/admin/api/accounts/${encodeURIComponent(target.dataset.deleteAccount)}`, {method:"DELETE"});
      if (state.page === "accounts") { await refreshAccountsList(); return; }
    }
    else if (target.dataset.rotateKey && confirm("轮换后旧 Key 会立即失效，调用方必须改用新 Key。确定继续吗？")) {
      const rotated = await api(`/admin/api/keys/${target.dataset.rotateKey}/rotate`, {method:"POST"});
      $("new-key").innerHTML = `新 Key：<code>${esc(rotated.key)}</code>`; $("new-key").className = "notice";
    }
    else if (target.dataset.deleteKey && confirm("删除后该 Key 会立即失效且无法恢复。确定删除吗？")) await api(`/admin/api/keys/${target.dataset.deleteKey}`, {method:"DELETE"});
    else if (target.dataset.disableCard && confirm("确定禁用这张卡密吗？")) await api(`/admin/api/cards/${encodeURIComponent(target.dataset.disableCard)}/disable`, {method:"POST"});
    else if (target.dataset.creditKey) { openKeyCredit(target.dataset.creditKey, target.dataset.creditName || ""); return; }
    else if (target.dataset.priceModel) { openPriceEditor(target); return; }
    else return;
    await refreshPage();
  } catch (error) { $("global-status").textContent = error.message; $("global-status").style.color = "var(--danger)"; }
});

function openKeyCredit(keyId, name) {
  const body = `<p>${esc(name)}</p><form id="key-credit-form" class="stack-form"><label>金额 USD<input name="amount" required inputmode="decimal" min="0.01" step="0.01" placeholder="大于 0"></label><label>原因<input name="reason" maxlength="200" placeholder="可选"></label><button type="submit">记入这把 Key</button><p id="key-credit-message" role="status"></p></form>`;
  if (typeof openConsoleDrawer === "function") openConsoleDrawer("加美元", body);
  else document.body.insertAdjacentHTML("beforeend", `<dialog open class="console-drawer">${body}</dialog>`);
  $("key-credit-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector("button");
    const amount = form.elements.amount.value.trim();
    const parsed = Number(amount);
    if (!amount || !Number.isFinite(parsed) || parsed <= 0) {
      $("key-credit-message").textContent = "金额必须大于 0，不能为负数";
      return;
    }
    button.disabled = true;
    try {
      const result = await api(`/admin/api/keys/${encodeURIComponent(keyId)}/credit`, {
        method: "POST",
        body: JSON.stringify({amount, reason: form.elements.reason.value.trim()}),
      });
      $("key-credit-message").textContent = `已入账，余额 ${formatUsd(result.balance_after)}`;
      await refreshPage("keys");
    } catch (error) {
      $("key-credit-message").textContent = error.message;
      button.disabled = false;
    }
  };
}

function openPriceEditor(button) {
  const kind = button.dataset.priceType || "text";
  const fields = kind === "image"
    ? [["每张官方价 USD", "image", button.dataset.priceImage || ""]]
    : kind === "video"
      ? [["每秒官方价 USD", "second", button.dataset.priceSecond || ""]]
      : [["输入 / 百万 Token", "input", button.dataset.priceInput || ""], ["输出 / 百万 Token", "output", button.dataset.priceOutput || ""], ["缓存 / 百万 Token", "cached", button.dataset.priceCached || ""]];
  const body = `<p>${esc(button.dataset.priceModel)}</p><p class="hint">留空倍率表示跟随全局。保存后这行官方价不再被内置价目覆盖。</p><form id="price-editor-form" class="stack-form">${fields.map(([label, name, value]) => `<label>${label}<input name="${name}" value="${esc(value)}" inputmode="decimal"></label>`).join("")}<label>模型倍率<input name="multiplier" value="${esc(button.dataset.priceRate || "")}" placeholder="留空跟随全局" inputmode="decimal"></label><button type="submit">保存价格</button><p id="price-editor-message" role="status"></p></form>`;
  if (typeof openConsoleDrawer === "function") openConsoleDrawer("设置价格", body);
  $("price-editor-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {provider: button.dataset.priceProvider, modality: kind === "image" || kind === "video" ? kind : "text", multiplier_override: form.elements.multiplier.value.trim() || null};
    if (kind === "image") payload.official_usd_per_image = form.elements.image.value.trim();
    else if (kind === "video") payload.official_usd_per_second = form.elements.second.value.trim();
    else {
      payload.official_input_usd_per_1m = form.elements.input.value.trim();
      payload.official_output_usd_per_1m = form.elements.output.value.trim();
      payload.official_cached_usd_per_1m = form.elements.cached.value.trim() || null;
    }
    const submit = form.querySelector("button");
    submit.disabled = true;
    try {
      await api(`/admin/api/billing/prices/${encodeURIComponent(button.dataset.priceModel)}`, {method: "PUT", body: JSON.stringify(payload)});
      if (typeof consoleDrawer !== "undefined" && consoleDrawer.open) consoleDrawer.close();
      await refreshPage("billing");
    } catch (error) {
      $("price-editor-message").textContent = error.message;
      submit.disabled = false;
    }
  };
}

$("billing-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/admin/api/billing/settings", {
      method: "PUT",
      body: JSON.stringify({
        price_multiplier: $("billing-multiplier").value.trim(),
        new_user_usd: $("billing-new-user").value.trim(),
        request_budget_usd: $("billing-budget").value,
        enforced: true,
      }),
    });
    $("billing-settings-result").textContent = "计费设置已保存";
    $("billing-settings-result").className = "notice";
    await refreshPage("billing");
  } catch (error) {
    $("billing-settings-result").textContent = error.message;
    $("billing-settings-result").className = "notice error";
  }
});

$("billing-sync").addEventListener("click", async () => {
  try {
    const result = await api("/admin/api/billing/prices/sync", {method: "POST"});
    $("billing-sync-result").textContent = `补齐 ${result.inserted || 0} 条，更新 ${result.updated || 0} 条，保留人工价 ${result.skipped_manual || 0} 条`;
    await refreshPage("billing");
  } catch (error) {
    $("billing-sync-result").textContent = error.message;
  }
});

$("card-batch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const expires = $("card-expires").value;
  try {
    const body = await api("/admin/api/cards/batches", {
      method: "POST",
      body: JSON.stringify({
        amount_usd: $("card-amount").value.trim(),
        count: Number($("card-count").value || 1),
        expires_at: expires ? new Date(expires).toISOString() : null,
        note: $("card-note").value.trim() || null,
      }),
    });
    const codes = (body.codes || []).map((code) => `<code>${esc(code)}</code>`).join("<br>");
    $("card-result").innerHTML = `已生成 ${body.count} 张，请立即复制：<br>${codes}`;
    $("card-result").className = "notice";
    await refreshPage("cards");
  } catch (error) {
    $("card-result").textContent = error.message;
    $("card-result").className = "notice error";
  }
});

$("wallet-credit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const amount = $("wallet-credit-amount").value.trim();
  const parsed = Number(amount);
  if (!amount || !Number.isFinite(parsed) || parsed <= 0) {
    $("wallet-credit-result").textContent = "金额必须大于 0，不能为负数";
    $("wallet-credit-result").className = "notice error";
    return;
  }
  try {
    const body = await api("/admin/api/wallet/credit", {
      method: "POST",
      body: JSON.stringify({
        email: $("wallet-credit-email").value.trim(),
        amount: $("wallet-credit-amount").value.trim(),
        reason: $("wallet-credit-reason").value.trim(),
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}`,
      }),
    });
    $("wallet-credit-result").textContent = `已入账 ${body.amount_usd}，余额 ${body.balance_after}`;
    $("wallet-credit-result").className = "notice";
    event.target.reset();
    await refreshPage("ledger");
  } catch (error) {
    $("wallet-credit-result").textContent = error.message;
    $("wallet-credit-result").className = "notice error";
  }
});

$("ledger-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.ledgerPage = 1;
  refreshPage("ledger");
});
$("ledger-prev").addEventListener("click", () => {
  if (state.ledgerPage > 1) {
    state.ledgerPage -= 1;
    refreshPage("ledger");
  }
});
$("ledger-next").addEventListener("click", () => {
  if (state.ledgerPage * 25 < state.ledgerTotal) {
    state.ledgerPage += 1;
    refreshPage("ledger");
  }
});

async function bootstrap() { try { showApp(await api("/admin/api/auth/session")); } catch (error) { showLogin(); } }
bootstrap();
setInterval(()=>{if(!$("app-shell").classList.contains("hidden") && document.visibilityState === "visible" && ["errors","dashboard"].includes(state.page)) refreshPage(state.page);},15000);
