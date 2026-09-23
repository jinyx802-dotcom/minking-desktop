const $ = (id) => document.getElementById(id);
let workspace = 'cloud';
async function setWorkspace(mode, persist=true) {
  workspace = mode === 'local' ? 'local' : 'cloud';
  document.body.dataset.workspace = workspace;
  for (const name of ['cloud', 'local']) $(name+'-workspace').setAttribute('aria-pressed', String(name === workspace));
  const frame = $('local-workspace-frame');
  frame.hidden = workspace !== 'local';
  if (workspace === 'local') {
    $('guide-root').classList.add('hidden');
    if (!frame.getAttribute('src')) frame.src = 'local/index.html?embedded=1';
  }
  if (persist) await api().set_workspace(workspace);
  if (workspace === 'cloud') await render();
}
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-jump]').forEach(button => button.addEventListener('click', () => navTo(button.dataset.jump)));
  for (const mode of ['cloud','local']) $(mode+'-workspace').addEventListener('click', () => {
    setWorkspace(mode).catch(error => setMessage($('login-message'),error.message,true));
  });
});
const MODE = { cloud: "云", official: "官方", unknown: "未知" };
const PAGE_SIZE = 20;
let challengeId = "";
const CAPTCHA_REFRESH_MS = 4 * 60 * 1000;
let captchaLoadedAt = 0;
let captchaRequest = 0;
let captchaTimer = 0;
let booted = false;
let authMode = "login";
let currentPane = "dashboard";
let lastState = null;
let callsPage = 1;
let ledgerPage = 1;
let pendingConfirm = null;
let dashboardPeriods = {};
let activePeriod = "";
let guideStep = -1;
let guideDone = false;
const MODEL_PICKER = { codex: true, grok: true, workbuddy: true, zcode: true };
const GUIDE_STEPS = [
  {
    title: "第一次使用",
    text: "先打开「接入工具」。把本机软件接到云端，写入前会备份，并让你勾选模型。",
    next: "去接入工具",
    target: () => document.querySelector('[data-pane="tools"]'),
    run: () => navTo("tools", true),
  },
  {
    title: "一键接入",
    text: "点这里写入配置。默认会勾选全部模型，不需要的可以取消。",
    next: "知道了",
    target: () => document.querySelector("[data-guide='apply-cloud']") || document.querySelector(".mini.cloud"),
  },
];

function setAuthMode(mode) {
  authMode = mode === "register" ? "register" : "login";
  const register = authMode === "register";
  $("tab-login").classList.toggle("active", !register);
  $("tab-register").classList.toggle("active", register);
  $("name-field").classList.toggle("hidden", !register);
  $("name").required = register;
  $("auth-sub").textContent = register
    ? "新用户填写姓名和邮箱。新人奖励每台电脑只能领取一次，重复注册会提示并且不再发放。"
    : "已有账号用邮箱验证码登录。这里只切换 Codex / Grok / Claude Code / WorkBuddy 的配置，不是聊天窗口。";
  $("send-code-btn").innerHTML = register ? "发送注册验证码 <span>↗</span>" : "发送登录验证码 <span>↗</span>";
}

function apiReady(bridge) {
  return Boolean(
    bridge
    && typeof bridge.state === "function"
    && typeof bridge.get_captcha === "function"
  );
}

function api() {
  const bridge = window.pywebview && window.pywebview.api;
  if (!apiReady(bridge)) {
    throw new Error("本机接口还没就绪");
  }
  return bridge;
}

async function waitForApi(timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const bridge = window.pywebview && window.pywebview.api;
    if (apiReady(bridge)) {
      return bridge;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("本机接口没有连上，请关闭窗口后重新打开客户端");
}

function setMessage(node, text, error) {
  node.textContent = text || "";
  node.classList.toggle("error", Boolean(error && text));
}

function showBanner(id, text, error) {
  const node = $(id);
  if (!text) {
    node.classList.add("hidden");
    node.textContent = "";
    return;
  }
  node.classList.remove("hidden");
  node.textContent = text;
  node.classList.toggle("error", Boolean(error));
}

function show(id) {
  $("login-shell").classList.toggle("hidden", id !== "login");
  $("app-shell").classList.toggle("hidden", id !== "main");
  $("header-user").classList.toggle("hidden", id !== "main");
}

function navTo(pane, reload) {
  currentPane = pane;
  if (pane === 'models' || pane === 'playground') {
    const frame = document.querySelector(`#pane-${pane} iframe`);
    if (!frame.getAttribute('src')) frame.src = `local/index.html?embedded=1&source=cloud&view=${pane}`;
  }
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.pane === pane);
  });
  document.querySelectorAll(".pane").forEach((el) => {
    el.classList.toggle("hidden", el.id !== `pane-${pane}`);
  });
  if (reload === false) {
    return;
  }
  if (pane === "dashboard") loadDashboard();
  if (pane === "calls") loadCalls();
  if (pane === "ledger") loadLedger();
  if (pane === "redeem") loadWallet();
}

function formatNumber(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString("zh-CN");
}

function trimCompact(number) {
  const text = number.toFixed(1);
  return text.endsWith(".0") ? text.slice(0, -2) : text;
}

function formatTokens(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const abs = Math.abs(number);
  if (abs >= 1000000) return `${trimCompact(number / 1000000)}M`;
  if (abs >= 1000) return `${trimCompact(number / 1000)}K`;
  return String(Math.trunc(number) === number ? number : number);
}

function formatRate(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `${number}%`;
}

function formatMoney(value) {
  if (value == null || value === "") return "—";
  return String(value);
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = v => String(v).padStart(2,'0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function callStatus(value) {
  const label = {success:'成功',failed:'失败',pending:'等待中',running:'进行中',cancelled:'已取消',interrupted:'已中断',in_progress:'进行中'};
  const node=document.createElement('span');node.className='call-status '+(value==='success'?'success':value==='failed'?'failed':'pending');node.textContent=label[value]||'未知';return node;
}

function pick(obj, keys) {
  if (!obj || typeof obj !== "object") return undefined;
  for (const key of keys) {
    if (obj[key] != null && obj[key] !== "") return obj[key];
  }
  return undefined;
}

function rowsFrom(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  for (const key of ["data", "items", "records", "calls", "ledger", "entries"]) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  return [];
}

function pageMeta(payload, page, pageSize) {
  const rows = rowsFrom(payload);
  const paging = payload && payload.pagination && typeof payload.pagination === "object" ? payload.pagination : {};
  const total = Number(pick(payload, ["total", "count"]) ?? paging.total ?? rows.length) || 0;
  const current = Number(pick(payload, ["page"]) ?? paging.page ?? page) || page;
  const size = Number(pick(payload, ["page_size", "pageSize"]) ?? paging.page_size ?? pageSize) || pageSize;
  return { rows, total, page: current, pageSize: size };
}

function periodFrom(src) {
  if (!src || typeof src !== "object" || Array.isArray(src)) return null;
  const calls = pick(src, ["calls", "count"]);
  const successes = pick(src, ["successes", "success_count"]);
  const failures = pick(src, ["failures", "failed", "fail_count"]);
  const callCount = Number(calls);
  const successCount = Number(successes);
  const failCount = failures != null
    ? Number(failures)
    : (Number.isFinite(callCount) && Number.isFinite(successCount) ? callCount - successCount : null);
  let successRate = pick(src, ["success_rate", "successRate"]);
  let failureRate = pick(src, ["failure_rate", "failureRate"]);
  if (successRate == null && Number.isFinite(callCount) && callCount > 0 && Number.isFinite(successCount)) {
    successRate = Math.round((1000 * successCount) / callCount) / 10;
  }
  if (failureRate == null && Number.isFinite(callCount) && callCount > 0 && Number.isFinite(failCount)) {
    failureRate = Math.round((1000 * failCount) / callCount) / 10;
  }
  if (failureRate == null && successRate != null) {
    const rate = Number(successRate);
    if (Number.isFinite(rate)) failureRate = Math.round((1000 * (100 - rate)) / 10) / 100;
  }
  return {
    calls: calls,
    successRate,
    failureRate,
    inputTokens: pick(src, ["input_tokens", "prompt_tokens", "inputTokens"]),
    outputTokens: pick(src, ["output_tokens", "completion_tokens", "outputTokens"]),
    cachedTokens: pick(src, ["cached_tokens", "cache_tokens", "cachedTokens"]),
  };
}

function collectPeriods(data) {
  const metrics = data.metrics && typeof data.metrics === "object" ? data.metrics : {};
  const buckets = data.periods && typeof data.periods === "object" ? data.periods : (
    data.ranges && typeof data.ranges === "object" ? data.ranges : {}
  );
  const named = [
    ["day", ["day", "today"]],
    ["week", ["week", "this_week"]],
    ["month", ["month", "this_month"]],
  ];
  const found = {};
  named.forEach(([id, keys]) => {
    for (const key of keys) {
      const block = periodFrom(data[key]) || periodFrom(metrics[key]) || periodFrom(buckets[key]);
      if (block) {
        found[id] = block;
        break;
      }
    }
  });
  found.overall = periodFrom({
    ...metrics,
    input_tokens: pick(metrics, ["input_tokens"]) ?? pick(data, ["input_tokens"]),
    output_tokens: pick(metrics, ["output_tokens"]) ?? pick(data, ["output_tokens"]),
    cached_tokens: pick(metrics, ["cached_tokens"]) ?? pick(data, ["cached_tokens"]),
    failure_rate: pick(metrics, ["failure_rate"]) ?? pick(data, ["failure_rate"]),
  }) || {};
  return found;
}

function paintStats(block, credit) {
  $("dash-credit").textContent = formatMoney(credit);
  $("dash-calls").textContent = formatNumber(block && block.calls);
  $("dash-success").textContent = formatRate(block && block.successRate);
  $("dash-fail").textContent = formatRate(block && block.failureRate);
  $("dash-in").textContent = formatTokens(block && block.inputTokens);
  $("dash-out").textContent = formatTokens(block && block.outputTokens);
  $("dash-cached").textContent = formatTokens(block && block.cachedTokens);
}

async function loadDashboard() {
  showBanner("dash-note", "");
  const credit = lastState && lastState.user ? lastState.user.usd_credit : "—";
  paintStats({}, credit);
  try {
    const result = await api().dashboard();
    if (!result || result.ok === false) {
      showBanner("dash-note", (result && result.error) || "无法加载工作台", true);
      return;
    }
    const periods = collectPeriods(result);
    dashboardPeriods = periods;
    const labels = [
      ["day", "今日"],
      ["week", "本周"],
      ["month", "本月"],
    ].filter(([id]) => periods[id]);
    const tabs = $("period-tabs");
    tabs.innerHTML = "";
    if (labels.length) {
      tabs.classList.remove("hidden");
      if (!labels.some(([id]) => id === activePeriod)) {
        activePeriod = labels[0][0];
      }
      labels.forEach(([id, label]) => {
        const btn = button("period-tab", label);
        btn.classList.toggle("active", id === activePeriod);
        btn.addEventListener("click", () => {
          activePeriod = id;
          paintStats(dashboardPeriods[id], pick(result.user || {}, ["usd_credit"]) ?? pick(result, ["usd_credit"]) ?? credit);
          tabs.querySelectorAll(".period-tab").forEach((el) => el.classList.remove("active"));
          btn.classList.add("active");
        });
        tabs.appendChild(btn);
      });
    } else {
      tabs.classList.add("hidden");
      activePeriod = "overall";
    }
    const usd = pick(result.user || {}, ["usd_credit"]) ?? pick(result, ["usd_credit", "balance"]) ?? credit;
    $("side-credit").textContent = `额度 ${formatMoney(usd)}`;
    paintStats(periods[activePeriod] || periods.overall, usd);
  } catch (error) {
    showBanner("dash-note", error.message || "无法加载工作台", true);
  }
}

async function loadCalls() {
  showBanner("calls-note", "加载中…", false);
  try {
    const result = await api().calls(callsPage, PAGE_SIZE);
    if (!result || result.ok === false) {
      $("calls-body").innerHTML = "";
      showBanner("calls-note", (result && result.error) || "无法加载调用明细", true);
      return;
    }
    const meta = pageMeta(result, callsPage, PAGE_SIZE);
    callsPage = meta.page;
    renderRows($("calls-body"), meta.rows, (row) => [
      formatTime(pick(row, ["started_at", "created_at", "time"])),
      pick(row, ["request_model", "model", "response_model"]) || "—",
      callStatus(row.status),
      `输入 ${formatTokens(pick(row,['input_tokens','prompt_tokens']))} · 输出 ${formatTokens(pick(row,['output_tokens','completion_tokens']))} · 缓存 ${formatTokens(row.cached_tokens)}`,
      formatTokens(pick(row, ["total_tokens", "tokens"])),
      row.usd_charged == null ? '未结算' : '$'+Number(row.usd_charged).toFixed(6),
    ], 6);
    $("calls-page").textContent = meta.total
      ? `第 ${meta.page} 页 · 共 ${meta.total} 条`
      : `第 ${meta.page} 页`;
    $("calls-prev").disabled = meta.page <= 1;
    $("calls-next").disabled = meta.rows.length < meta.pageSize && meta.page * meta.pageSize >= meta.total;
    showBanner("calls-note", meta.rows.length ? "" : "暂无调用记录", false);
  } catch (error) {
    $("calls-body").innerHTML = "";
    showBanner("calls-note", error.message || "无法加载调用明细", true);
  }
}

async function loadLedger() {
  showBanner("ledger-note", "加载中…", false);
  try {
    const result = await api().wallet_ledger(ledgerPage, PAGE_SIZE);
    if (!result || result.ok === false) {
      $("ledger-body").innerHTML = "";
      showBanner("ledger-note", (result && result.error) || "无法加载资金流水", true);
      return;
    }
    const meta = pageMeta(result, ledgerPage, PAGE_SIZE);
    ledgerPage = meta.page;
    const ledgerKind = { usage: "扣费", grant: "加款", redeem: "卡密", adjust: "调整", reversal: "冲正" };
    renderRows($("ledger-body"), meta.rows, (row) => [
      formatTime(pick(row, ["created_at", "time", "started_at"])),
      ledgerKind[pick(row, ["type", "kind", "action"])] || pick(row, ["type", "kind", "action"]) || "—",
      formatMoney(pick(row, ["amount", "delta", "usd"])),
      formatMoney(pick(row, ["balance", "usd_credit", "after"])),
      pick(row, ["note", "remark", "memo", "description"]) || "—",
    ], 5);
    $("ledger-page").textContent = meta.total
      ? `第 ${meta.page} 页 · 共 ${meta.total} 条`
      : `第 ${meta.page} 页`;
    $("ledger-prev").disabled = meta.page <= 1;
    $("ledger-next").disabled = meta.rows.length < meta.pageSize && meta.page * meta.pageSize >= meta.total;
    showBanner("ledger-note", meta.rows.length ? "" : "暂无资金流水", false);
  } catch (error) {
    $("ledger-body").innerHTML = "";
    showBanner("ledger-note", error.message || "无法加载资金流水", true);
  }
}

async function loadWallet() {
  showBanner("redeem-note", "");
  $("wallet-credit").textContent = `当前额度 ${formatMoney(lastState && lastState.user && lastState.user.usd_credit)}`;
  try {
    const result = await api().wallet();
    if (!result || result.ok === false) {
      showBanner("redeem-note", (result && result.error) || "无法加载钱包", true);
      return;
    }
    const usd = pick(result.user || {}, ["usd_credit"]) ?? pick(result, ["usd_credit", "balance", "credit"]);
    if (usd != null) {
      $("wallet-credit").textContent = `当前额度 ${formatMoney(usd)}`;
      $("side-credit").textContent = `额度 ${formatMoney(usd)}`;
    }
  } catch (error) {
    showBanner("redeem-note", error.message || "无法加载钱包", true);
  }
}

function renderRows(tbody, rows, cells, colspan) {
  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="empty" colspan="${colspan}">暂无数据</td>`;
    tbody.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    cells(row).forEach((value) => {
      const td = document.createElement("td");
      if (value instanceof Node) td.appendChild(value);
      else td.textContent = value == null ? "—" : String(value);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

async function render() {
  const state = await api().state();
  lastState = state;
  $("base-url").value = state.settings.public_base_url || "";
  $("settings-url").value = state.settings.public_base_url || "";
  $("settings-version").textContent = state.settings.version ? `客户端版本 ${state.settings.version}` : "";
  if (!state.logged_in) {
    document.querySelectorAll('.cloud-model-frame').forEach(frame => frame.removeAttribute('src'));
    show("login");
    $("guide-root").classList.add("hidden");
    clearGuideTarget();
    if (!$("captcha-image").dataset.id || captchaIsStale()) {
      await refreshCaptcha($("captcha-image").dataset.id ? "expired" : "load");
    }
    if (state.error) setMessage($("login-message"), state.error, true);
    return;
  }
  show("main");
  const email = (state.user && state.user.email) || state.settings.email || "—";
  const credit = (state.user && state.user.usd_credit) || "0";
  $("header-user").textContent = email;
  $("side-credit").textContent = `额度 ${credit}`;
  if (state.deep_link) setMessage($("main-message"), "已从浏览器协议打开 MinKing 桌面端。", false);
  else if (state.error) setMessage($("main-message"), state.error, true);
  $("harness-list").innerHTML = "";
  (state.harnesses || []).forEach((item) => $("harness-list").appendChild(card(item)));
  navTo(currentPane, true);
  maybeStartGuide(state);
}

function card(item, local = null) {
  const el = document.createElement("article");
  el.className = "harness";
  const installed = item.installed ? "已安装" : "未检测到";
  const mode = local && item.mode === 'cloud' ? '本地服务' : MODE[item.mode] || "未知";
  el.innerHTML = `
    <div class="harness-head">
      <h3></h3>
      <span class="pill ${item.mode || "unknown"}">${mode}</span>
    </div>
    <p></p>
  `;
  el.querySelector("h3").textContent = item.display_name;
  const iconName = {codex:"openai",grok:"grok",workbuddy:"codebuddy",claude_code:"claude",antigravity:"antigravity"}[item.id];
  const icon = document.createElement(iconName ? "img" : "span");
  icon.className = "tool-icon";
  if (iconName) { icon.src = new URL(`icons/${iconName}.svg`, location.href).href; icon.alt = ""; }
  else { icon.textContent = (item.display_name || "工具").slice(0,1); icon.setAttribute("aria-hidden","true"); }
  el.querySelector("h3").prepend(icon);
  el.querySelector("p").textContent = installed + (item.has_snapshot ? " · 已保存配置版本" : "");
  const actions = document.createElement("div");
  actions.className = "row-actions";
  if (item.one_click) {
    const cloud = button("mini cloud", local ? "接入本地服务" : "接入云端服务");
    cloud.dataset.guide = "apply-cloud";
    cloud.addEventListener("click", () => beginApply(item.id, local));
    const official = button("mini official", "回退配置");
    official.disabled = !item.has_snapshot;
    official.title = item.has_snapshot ? "从已保存的配置版本中选择一份恢复，不执行账号登录" : "首次接入时会自动保存配置";
    official.addEventListener("click", () => beginRestore(item.id, local));
    actions.append(cloud, official);
    if (item.id === "codex") {
      const sync = button("mini", "一键同步会话");
      sync.addEventListener("click", () => beginSyncSessions(local));
      actions.append(sync);
    }
  } else {
    const copyUrl = button("mini", "复制接口地址");
    copyUrl.addEventListener("click", () => local ? local.copy(local.base_url) : act(() => api().copy_value("base_url", item.id), "已复制接口地址"));
    const reveal = button("mini", "显示密钥");
    reveal.textContent = local ? '复制密钥' : '显示密钥';
    reveal.addEventListener("click", () => local ? local.copy(local.api_key) : revealKey(el, actions, item.id));
    actions.append(copyUrl, reveal);
  }
  el.appendChild(actions);
  return el;
}

function button(className, label) {
  const node = document.createElement("button");
  node.className = className;
  node.type = "button";
  node.textContent = label;
  return node;
}

function fillConfirmFiles(files) {
  const list = $("confirm-files");
  list.innerHTML = "";
  (files || []).forEach((item) => {
    const li = document.createElement("li");
    const path = item.path || item.name || "";
    li.textContent = item.exists === false ? `${path}（当前不存在，将新建）` : path;
    if (item.exists === false) li.classList.add("missing");
    list.appendChild(li);
  });
  if (!(files || []).length) {
    const li = document.createElement("li");
    li.textContent = "将按该工具的默认配置文件写入。";
    list.appendChild(li);
  }
}

function closeConfirm() {
  pendingConfirm = null;
  $("confirm-dialog").classList.add("hidden");
  $("confirm-dialog").classList.remove("compare-open");
  $("confirm-dialog").classList.remove("model-open");
  $("confirm-check").checked = false;
  $("confirm-go").disabled = true;
  $("confirm-go").textContent = "确认写入";
  $("confirm-models-wrap").classList.add("hidden");
  $("confirm-models").innerHTML = "";
  $("confirm-versions-wrap").classList.add("hidden");
  $("confirm-versions").innerHTML = "";
  $("confirm-auth-compare").classList.add("hidden");
  $("auth-diff").classList.add("hidden");
  $("auth-diff").replaceChildren();
  setMessage($("confirm-message"), "", false);
}

function authSide(compare, versionId) {
  if (!compare || !compare.versions) return null;
  return compare.versions[versionId] || null;
}

function authVerdict(current, selected) {
  const cur = current.auth || {};
  const sel = selected.auth || {};
  const curRoute = current.route || {};
  const selRoute = selected.route || {};
  const curRelay = Boolean(cur.relay || curRoute.relay);
  const selRelay = Boolean(sel.relay || selRoute.relay);
  if (!sel.present) {
    return { kind: "missing", text: "这份备份里没有 auth.json。确认后，当前 auth.json 会被删除。" };
  }
  if (cur.mode === "apikey" && sel.mode === "chatgpt" && !selRoute.relay) {
    return { kind: "leave", text: "回退后会离开中转站，改回 ChatGPT 登录。" };
  }
  if (curRelay && selRelay) {
    return { kind: "stay", text: "这份备份也是中转站配置，回退不会回到官方账号。" };
  }
  if (cur.mode === sel.mode && curRoute.provider === selRoute.provider) {
    return { kind: "same", text: "两边的登录方式和接口一致，回退不会改变登录。" };
  }
  return { kind: "change", text: `回退后登录方式会改成「${sel.label || "所选备份"}」。` };
}

function fillAuthCard(node, eyebrow, side) {
  node.replaceChildren();
  const who = document.createElement("p");
  who.className = "who";
  who.textContent = eyebrow;
  const title = document.createElement("h3");
  title.textContent = (side.auth && side.auth.label) || "未读取";
  const cred = document.createElement("p");
  cred.className = "cred";
  cred.textContent = (side.auth && side.auth.credential) || "";
  const route = document.createElement("p");
  route.className = "route";
  route.textContent = (side.route && side.route.label) || "";
  node.append(who, title, cred, route);
}

function fillAuthDiff(current, selected) {
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["字段", "当前", "选中备份"].forEach((text) => {
    const cell = document.createElement("th");
    cell.textContent = text;
    head.appendChild(cell);
  });
  table.appendChild(head);
  const left = new Map((current.auth.fields || []).map((item) => [item.name, item.value]));
  const right = new Map((selected.auth.fields || []).map((item) => [item.name, item.value]));
  const names = [...new Set([...left.keys(), ...right.keys()])].sort();
  if (!names.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "两边都没有可对比的字段。";
    row.appendChild(cell);
    table.appendChild(row);
  }
  names.forEach((name) => {
    const row = document.createElement("tr");
    const a = left.has(name) ? left.get(name) : "（无）";
    const b = right.has(name) ? right.get(name) : "（无）";
    if (a !== b) row.className = "diff";
    [name, a, b].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    table.appendChild(row);
  });
  $("auth-diff").replaceChildren(table);
}

function renderAuthCompare() {
  const wrap = $("confirm-auth-compare");
  const compare = pendingConfirm && pendingConfirm.authCompare;
  const versionId = $("confirm-versions").value;
  const selected = authSide(compare, versionId);
  if (!compare || !compare.current || !selected) {
    wrap.classList.add("hidden");
    $("confirm-dialog").classList.remove("compare-open");
    if (pendingConfirm && pendingConfirm.kind === "restore") $("confirm-go").textContent = "确认恢复";
    return;
  }
  wrap.classList.remove("hidden");
  $("confirm-dialog").classList.add("compare-open");
  fillAuthCard($("auth-current"), "当前正在使用", compare.current);
  fillAuthCard($("auth-selected"), "选中的这份备份", selected);
  $("auth-selected").classList.add("selected");
  const verdict = authVerdict(compare.current, selected);
  const note = $("auth-verdict");
  note.className = `auth-verdict ${verdict.kind}`;
  note.textContent = verdict.text;
  $("confirm-go").textContent = verdict.kind === "stay" ? "仍然恢复这份中转站配置" : "确认恢复";
  fillAuthDiff(compare.current, selected);
}

function modelProvider(slug) {
  const head = String(slug || "").split("/")[0];
  const known = { codex: "Codex", grok: "Grok", workbuddy: "WorkBuddy", antigravity: "Antigravity" };
  if (known[head]) return { id: head, label: known[head] };
  return { id: "catalog", label: "目录" };
}

function priceChips(pricing) {
  if (!pricing) return [];
  const sell = pricing.sell || {};
  const chips = [];
  if (pricing.multiplier) chips.push(["倍率", pricing.multiplier]);
  if (sell.input_usd_per_1m || sell.output_usd_per_1m) {
    chips.push(["入", sell.input_usd_per_1m || "—"]);
    chips.push(["出", sell.output_usd_per_1m || "—"]);
  } else if (sell.usd_per_image) {
    chips.push(["每张", sell.usd_per_image]);
  } else if (sell.usd_per_second) {
    chips.push(["每秒", sell.usd_per_second]);
  }
  return chips;
}

function applyModelFilter() {
  const wrap = $("confirm-models-wrap");
  const query = (wrap.querySelector(".model-search")?.value || "").trim().toLowerCase();
  const provider = wrap.querySelector(".model-filters button.active")?.dataset.provider || "all";
  wrap.querySelectorAll(".model-item").forEach((card) => {
    const blob = `${card.dataset.name || ""} ${card.dataset.slug || ""}`.toLowerCase();
    const matched = (!query || blob.includes(query)) && (provider === "all" || card.dataset.provider === provider);
    card.classList.toggle("filtered", !matched);
  });
}

function fillConfirmModels(models, harnessId, local = false) {
  const wrap = $("confirm-models-wrap");
  const list = $("confirm-models");
  wrap.querySelector(".model-selection-actions")?.remove();
  wrap.querySelector(".model-toolbar")?.remove();
  list.replaceChildren();
  const show = Boolean((local || MODEL_PICKER[harnessId]) && models && models.length);
  wrap.classList.toggle("hidden", !show);
  $("confirm-dialog").classList.toggle("model-open", show);
  if (!show) return;
  const providers = new Map();
  models.forEach((item) => {
    const slug = item.slug || item.id || "";
    if (!slug) return;
    const provider = modelProvider(slug);
    providers.set(provider.id, provider.label);
    const label = document.createElement("label");
    label.className = "model-item";
    label.dataset.slug = slug;
    label.dataset.name = item.display_name || slug;
    label.dataset.provider = provider.id;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = true;
    input.value = slug;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = item.display_name || slug;
    text.appendChild(title);
    if (item.display_name && item.display_name !== slug) {
      const small = document.createElement("small");
      small.textContent = slug;
      text.appendChild(small);
    }
    const chips = priceChips(item.pricing);
    if (chips.length) {
      const prices = document.createElement("span");
      prices.className = "model-prices";
      chips.forEach(([name, value]) => {
        const chip = document.createElement("i");
        chip.textContent = `${name} ${value}`;
        prices.appendChild(chip);
      });
      text.appendChild(prices);
    }
    label.append(input, text);
    list.appendChild(label);
  });
  const toolbar = document.createElement("div");
  toolbar.className = "model-toolbar";
  const search = document.createElement("input");
  search.className = "model-search";
  search.type = "search";
  search.placeholder = "搜索名称或 ID";
  search.addEventListener("input", applyModelFilter);
  const filters = document.createElement("div");
  filters.className = "model-filters";
  const addFilter = (id, text) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.provider = id;
    button.textContent = text;
    button.className = id === "all" ? "active" : "";
    button.addEventListener("click", () => {
      filters.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node === button));
      applyModelFilter();
    });
    filters.appendChild(button);
  };
  addFilter("all", "全部");
  if (providers.size > 1) providers.forEach((label, id) => addFilter(id, label));
  toolbar.append(search, filters);
  list.before(toolbar);
  list.before(window.modelSelectionControls(list));
}

function selectedModels() {
  return [...document.querySelectorAll("#confirm-models input:checked")].map((node) => node.value);
}

async function beginApply(harnessId, local = null) {
  setMessage($("main-message"), "正在准备备份…", false);
  const preview = local ? await api().local_tools('preview', harnessId) : await api().preview_apply(harnessId);
  if (!preview || preview.ok === false) {
    if (local) local.notice(preview?.error || '无法预览配置', true);
    setMessage($("main-message"), (preview && preview.error) || "无法预览写入", true);
    return;
  }
  const backup = local ? await api().local_tools('backup', harnessId) : await api().backup_apply(harnessId);
  if (!backup || backup.ok === false) {
    if (local) local.notice(backup?.error || '备份失败，已取消写入', true);
    setMessage($("main-message"), (backup && backup.error) || "备份失败，已取消写入", true);
    return;
  }
  pendingConfirm = {
    kind: "apply",
    local,
    id: harnessId,
    backupPath: backup.backup_path || "",
    backupDir: backup.backup_dir || backup.backup_path || "",
  };
  $("confirm-title").textContent = `${local ? '接入本地服务' : '接入云端服务'} · ${preview.display_name || "工具配置"}`;
  $("confirm-sub").textContent = "以下文件将被修改。已先做一份本地备份，确认前请先打开或导出。";
  const codexNote = harnessId === "codex"
    ? "确认后会同时把现有会话的模型提供方和模型改成当前登录，会话文件不移动。请先完全退出 Codex（含托盘）。"
    : "";
  $("confirm-connection").textContent = `接口地址：${preview.base_url || local?.base_url || ''}。${harnessId === 'zcode' ? '同步工具接入配置。' : '同步 MinKing 媒体 skill 与工具接入配置。'}${codexNote}`;
  $("confirm-check-label").textContent = harnessId === "codex"
    ? "我已备份，并已完全退出 Codex（含托盘），确认写入并同步会话"
    : "我已备份，确认写入 MinKing";
  $("confirm-go").textContent = harnessId === "codex" ? "确认写入并同步会话" : "确认写入";
  $("confirm-backup-actions").classList.remove("hidden");
  $("confirm-versions-wrap").classList.add("hidden");
  $("confirm-auth-compare").classList.add("hidden");
  $("confirm-dialog").classList.remove("compare-open");
  fillConfirmFiles(preview.files || backup.files);
  fillConfirmModels(preview.models || (lastState && lastState.model_choices) || [], harnessId, Boolean(local));
  const path = backup.backup_path || backup.backup_dir || "";
  $("confirm-backup-path").textContent = backup.created
    ? `备份目录：${path}`
    : (backup.message || "当前没有可备份的旧文件，写入时会创建新文件。");
  $("confirm-dialog").classList.remove("hidden");
  setMessage($("main-message"), "", false);
}

async function beginSyncSessions(local = null) {
  $("confirm-check").checked = false;
  $("confirm-go").disabled = true;
  pendingConfirm = { kind: "sync-sessions", local, id: "codex", backupPath: "", backupDir: "" };
  $("confirm-title").textContent = "一键同步会话 · Codex";
  $("confirm-sub").textContent = "按当前登录改写现有会话里的模型提供方和模型，会话文件仍留在原处。ChatGPT 登录使用官方模型提供方和官方模型；API Key 使用配置里已经写好的模型提供方和模型列表。接口地址留在 config.toml，不写入会话文件。";
  $("confirm-connection").textContent = "请先完全退出 Codex（含托盘）。";
  $("confirm-check-label").textContent = "我已完全退出 Codex（含托盘），确认同步会话";
  $("confirm-go").textContent = "同步会话";
  $("confirm-backup-actions").classList.add("hidden");
  $("confirm-versions-wrap").classList.add("hidden");
  $("confirm-auth-compare").classList.add("hidden");
  $("confirm-dialog").classList.remove("compare-open");
  fillConfirmModels([], "");
  const list = $("confirm-files");
  list.innerHTML = "";
  const item = document.createElement("li");
  item.textContent = "只改写现有 Codex 会话里的模型提供方和模型，不移动会话文件，也不改接口配置和 auth.json。";
  list.appendChild(item);
  $("confirm-backup-path").textContent = "";
  $("confirm-dialog").classList.remove("hidden");
  setMessage($("main-message"), "", false);
}

function fillConfirmVersions(versions) {
  const wrap = $("confirm-versions-wrap");
  const select = $("confirm-versions");
  select.innerHTML = "";
  const items = Array.isArray(versions) ? versions : [];
  wrap.classList.toggle("hidden", items.length === 0);
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id || "";
    option.textContent = item.label || item.id || "";
    select.appendChild(option);
  });
  if (items.length) select.value = items[0].id || "";
  renderAuthCompare();
}

async function beginRestore(harnessId, local = null) {
  const preview = local ? await api().local_tools('preview_restore', harnessId) : await api().preview_restore(harnessId);
  if (!preview || preview.ok === false) {
    if (local) local.notice(preview?.error || '无法预览恢复', true);
    setMessage($("main-message"), (preview && preview.error) || "无法预览恢复", true);
    return;
  }
  pendingConfirm = {
    kind: "restore",
    local,
    id: harnessId,
    backupPath: "",
    backupDir: preview.snapshot_dir || "",
    authCompare: harnessId === "codex" ? preview.auth_compare : null,
  };
  $("confirm-connection").textContent = '';
  $("confirm-title").textContent = `回退 ${preview.display_name || "工具配置"}`;
  $("confirm-sub").textContent = harnessId === "codex"
    ? "选择一份已保存的配置恢复。恢复配置和 auth.json，不移动会话文件。要让现有会话跟上当前登录，请再点「一键同步会话」。此操作不会登录或注销官方账号。"
    : "选择一份已保存的配置恢复，覆盖下列文件。此操作不会登录或注销官方账号。";
  $("confirm-check-label").textContent = "确认恢复所选版本";
  $("confirm-go").textContent = "确认恢复";
  $("confirm-backup-actions").classList.add("hidden");
  fillConfirmModels([], "");
  fillConfirmVersions(preview.versions || []);
  fillConfirmFiles(preview.files);
  $("confirm-backup-path").textContent = "";
  $("confirm-dialog").classList.remove("hidden");
}

async function revealKey(el, actions, harnessId) {
  const result = await api().reveal_key();
  if (!result.ok) {
    setMessage($("main-message"), result.error || "无法读取密钥", true);
    return;
  }
  let box = el.querySelector(".key-box");
  if (!box) {
    box = document.createElement("div");
    box.className = "key-box";
    el.appendChild(box);
  }
  box.textContent = result.key;
  if (![...actions.querySelectorAll("button")].some((node) => node.dataset.kind === "copy-key")) {
    const copyKey = button("mini", "复制密钥");
    copyKey.dataset.kind = "copy-key";
    copyKey.addEventListener("click", () => act(() => api().copy_value("key", harnessId), "已复制密钥"));
    actions.appendChild(copyKey);
  }
}

async function act(fn, okText) {
  setMessage($("main-message"), "处理中…", false);
  const result = await fn();
  if (!result || result.ok === false) {
    setMessage($("main-message"), (result && result.error) || "操作失败", true);
    return;
  }
  setMessage($("main-message"), (result && result.message) || okText || "已完成", false);
  await render();
}

async function savePublicUrl(value) {
  const result = await api().save_base_url(value);
  if (!result.ok) {
    setMessage($("main-message"), result.error || "保存失败", true);
    return false;
  }
  $("settings-dialog").classList.add("hidden");
  await render();
  return true;
}

async function boot() {
  if (booted) {
    return;
  }
  booted = true;
  $("captcha-refresh").addEventListener("click", () => refreshCaptcha("manual"));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshCaptchaIfStale();
  });
  window.addEventListener("focus", refreshCaptchaIfStale);
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.addEventListener("click", () => navTo(el.dataset.pane, true));
  });
  $("mail-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!$("captcha-image").dataset.id || captchaIsStale()) {
      await refreshCaptcha("expired");
      return;
    }
    const captchaId = $("captcha-image").dataset.id;
    const name = authMode === "register" ? $("name").value.trim() : "";
    if (authMode === "register" && !name) {
      setMessage($("login-message"), "注册请填写姓名", true);
      return;
    }
    const result = await api().send_code(name, $("email").value, captchaId, $("captcha-answer").value);
    if (!result.ok) {
      setMessage($("login-message"), result.error || "发送失败", true);
      await refreshCaptcha();
      return;
    }
    challengeId = result.challenge_id;
    $("verify-email").textContent = $("email").value.trim();
    $("mail-form").classList.add("hidden");
    $("verify-form").classList.remove("hidden");
    setMessage($("login-message"), "验证码已发送，10 分钟内有效。", false);
  });
  $("verify-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = await api().verify(challengeId, $("email").value, $("email-code").value);
    if (!result.ok) {
      setMessage($("login-message"), result.error || "验证失败", true);
      return;
    }
    const rewardNotice = result.reward_notice || "";
    setMessage($("login-message"), rewardNotice, false);
    currentPane = "dashboard";
    await render();
    if (rewardNotice) {
      showBanner("dash-note", rewardNotice, false);
      setMessage($("main-message"), rewardNotice, false);
    }
  });
  $("back-to-email").addEventListener("click", async () => {
    $("verify-form").classList.add("hidden");
    $("mail-form").classList.remove("hidden");
    $("email-code").value = "";
    challengeId = "";
    await refreshCaptcha();
  });
  $("logout").addEventListener("click", () => act(() => api().logout()));
  $("settings-btn").addEventListener("click", () => {
    if (lastState && lastState.logged_in) {
      navTo("settings", false);
      return;
    }
    $("settings-dialog").classList.remove("hidden");
  });
  $("close-settings").addEventListener("click", () => $("settings-dialog").classList.add("hidden"));
  $("save-settings").addEventListener("click", () => savePublicUrl($("base-url").value));
  $("save-console-settings").addEventListener("click", () => savePublicUrl($("settings-url").value));
  $("calls-prev").addEventListener("click", () => {
    if (callsPage > 1) {
      callsPage -= 1;
      loadCalls();
    }
  });
  $("calls-next").addEventListener("click", () => {
    callsPage += 1;
    loadCalls();
  });
  $("ledger-prev").addEventListener("click", () => {
    if (ledgerPage > 1) {
      ledgerPage -= 1;
      loadLedger();
    }
  });
  $("ledger-next").addEventListener("click", () => {
    ledgerPage += 1;
    loadLedger();
  });
  $("redeem-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("redeem-code").value.trim();
    if (!code) {
      showBanner("redeem-note", "请输入卡密", true);
      return;
    }
    const result = await api().redeem(code);
    if (!result || result.ok === false) {
      showBanner("redeem-note", (result && result.error) || "兑换失败", true);
      return;
    }
    $("redeem-code").value = "";
    const usd = pick(result, ["usd_credit", "balance"]) ?? (result.user && result.user.usd_credit);
    showBanner("redeem-note", usd != null ? `兑换成功，当前额度 ${usd}` : "兑换成功", false);
    await render();
  });
  $("confirm-check").addEventListener("change", () => {
    $("confirm-go").disabled = !$("confirm-check").checked;
  });
  $("confirm-versions").addEventListener("change", renderAuthCompare);
  $("auth-diff-toggle").addEventListener("click", () => {
    $("auth-diff").classList.toggle("hidden");
  });
  $("confirm-cancel").addEventListener("click", closeConfirm);
  const openContact = () => $("contact-dialog").classList.remove("hidden");
  const closeContact = () => $("contact-dialog").classList.add("hidden");
  $("login-contact").addEventListener("click", openContact);
  $("sidebar-contact").addEventListener("click", openContact);
  $("contact-close").addEventListener("click", closeContact);
  $("contact-dialog").addEventListener("click", (event) => {
    if (event.target === $("contact-dialog")) closeContact();
  });
  $("confirm-open-backup").addEventListener("click", async () => {
    if (!pendingConfirm) return;
    const result = await api().open_backup_folder(pendingConfirm.backupPath || pendingConfirm.backupDir || "");
    if (!result || result.ok === false) {
      setMessage($("confirm-message"), (result && (result.hint || result.error)) || "无法打开备份目录", true);
    }
  });
  $("confirm-export-backup").addEventListener("click", async () => {
    if (!pendingConfirm) return;
    const result = await api().export_backup(pendingConfirm.backupPath || pendingConfirm.backupDir || "");
    if (!result || result.ok === false) {
      setMessage($("confirm-message"), (result && (result.hint || result.error)) || "导出失败，请手动复制备份目录", true);
      return;
    }
    setMessage($("confirm-message"), `已导出到 ${result.path}`, false);
  });
  $("confirm-go").addEventListener("click", async () => {
    if (!$("confirm-check").checked || !pendingConfirm) {
      return;
    }
    const action = pendingConfirm;
    if (action.kind === "sync-sessions") {
      $("confirm-go").disabled = true;
      try {
        const result = action.local
          ? await api().local_tools("sync_sessions", "codex")
          : await api().sync_codex_sessions();
        if (!result || result.ok === false) {
          setMessage($("confirm-message"), (result && result.error) || "同步失败", true);
          return;
        }
        closeConfirm();
        const text = result.message || "已同步会话";
        if (action.local) action.local.notice(text);
        else setMessage($("main-message"), text, false);
      } finally {
        $("confirm-go").disabled = !$("confirm-check").checked;
      }
      return;
    }
    if (action.local) {
      const models = selectedModels();
      if (action.kind === 'apply' && !models.length) {
        setMessage($("confirm-message"), '请至少选择一个模型', true);return;
      }
      $("confirm-go").disabled = true;
      try {
        const version = action.kind === "restore" ? $("confirm-versions").value : "";
        const result = await api().local_tools(action.kind, action.id, models, version);
        if (!result.ok) {setMessage($("confirm-message"), result.error || '操作失败', true);return;}
        closeConfirm();
        await action.local.refresh();
        const fallback = action.kind === 'restore' ? '已回退接入前配置。' : '已写入本地服务配置，重启调用方工具后生效。';
        action.local.notice(result.message || fallback);
      } finally {$("confirm-go").disabled = !$("confirm-check").checked;}
      return;
    }
    if (action.kind === "restore") {
      const version = $("confirm-versions").value;
      closeConfirm();
      await act(() => api().restore_official(action.id, version), "已恢复所选配置");
      return;
    }
    const models = selectedModels();
    const picking = !$("confirm-models-wrap").classList.contains("hidden");
    if (picking && !models.length) {
      setMessage($("confirm-message"), "请至少选择一个模型", true);
      return;
    }
    closeConfirm();
    await act(() => api().apply_cloud(action.id, picking ? models : null), "已写入 MinKing 配置");
  });
  $("guide-next").addEventListener("click", () => advanceGuide());
  $("guide-skip").addEventListener("click", () => finishGuide());
  window.addEventListener("resize", () => {
    if (guideStep >= 0) layoutGuide();
  });
  try {
    await waitForApi(8000);
    const initial = await api().workspace_state();
    await setWorkspace(initial.workspace, false);
  } catch (error) {
    show("login");
    setMessage($("login-message"), error.message || "本机接口没有连上", true);
  }
}

function captchaIsStale() {
  return !captchaLoadedAt || Date.now() - captchaLoadedAt >= CAPTCHA_REFRESH_MS;
}

function loginFormVisible() {
  return !$("login-shell").classList.contains("hidden") && !$("mail-form").classList.contains("hidden");
}

function scheduleCaptchaRefresh() {
  window.clearTimeout(captchaTimer);
  captchaTimer = window.setTimeout(() => {
    if (loginFormVisible()) refreshCaptcha("expired");
  }, CAPTCHA_REFRESH_MS);
}

function refreshCaptchaIfStale() {
  if (loginFormVisible() && captchaIsStale()) refreshCaptcha("expired");
}

async function refreshCaptcha(reason) {
  const ticket = ++captchaRequest;
  const button = $("captcha-refresh");
  if (button) button.disabled = true;
  try {
    const result = await api().get_captcha();
    if (ticket !== captchaRequest) return false;
    if (!result.ok) {
      setMessage($("login-message"), result.error || "无法加载验证码", true);
      return false;
    }
    $("captcha-image").src = result.image;
    $("captcha-image").dataset.id = result.id;
    $("captcha-answer").value = "";
    captchaLoadedAt = Date.now();
    scheduleCaptchaRefresh();
    if (reason === "expired") {
      setMessage($("login-message"), "图片验证码已更新，请按新图重新填写。", false);
    }
    return true;
  } finally {
    if (ticket === captchaRequest && button) button.disabled = false;
  }
}

function clearGuideTarget() {
  document.querySelectorAll(".guide-target").forEach((el) => el.classList.remove("guide-target"));
}

function layoutGuide() {
  const step = GUIDE_STEPS[guideStep];
  if (!step) return;
  const target = step.target && step.target();
  const hole = $("guide-hole");
  const finger = $("guide-finger");
  const card = $("guide-card");
  const pad = 8;
  let rect;
  if (target) {
    rect = target.getBoundingClientRect();
  } else {
    rect = { left: window.innerWidth / 2 - 80, top: window.innerHeight / 2 - 40, width: 160, height: 80 };
  }
  hole.style.left = `${Math.max(8, rect.left - pad)}px`;
  hole.style.top = `${Math.max(8, rect.top - pad)}px`;
  hole.style.width = `${rect.width + pad * 2}px`;
  hole.style.height = `${rect.height + pad * 2}px`;
  const fingerLeft = rect.left + rect.width - 8;
  const fingerTop = rect.top + rect.height - 4;
  finger.style.left = `${Math.min(window.innerWidth - 72, Math.max(12, fingerLeft))}px`;
  finger.style.top = `${Math.min(window.innerHeight - 72, Math.max(12, fingerTop))}px`;
  const cardWidth = Math.min(320, window.innerWidth - 32);
  let cardLeft = rect.left;
  if (cardLeft + cardWidth > window.innerWidth - 16) {
    cardLeft = window.innerWidth - cardWidth - 16;
  }
  let cardTop = rect.bottom + 56;
  if (cardTop + 180 > window.innerHeight) {
    cardTop = Math.max(16, rect.top - 188);
  }
  card.style.left = `${Math.max(16, cardLeft)}px`;
  card.style.top = `${cardTop}px`;
}

function showGuideStep(index) {
  guideStep = index;
  const step = GUIDE_STEPS[index];
  if (!step) {
    finishGuide();
    return;
  }
  clearGuideTarget();
  $("guide-root").classList.remove("hidden");
  $("guide-step").textContent = `第 ${index + 1} 步`;
  $("guide-title").textContent = step.title;
  $("guide-text").textContent = step.text;
  $("guide-next").innerHTML = `${step.next || "下一步"} <span>↗</span>`;
  const target = step.target && step.target();
  if (target) {
    target.classList.add("guide-target");
    const onTarget = () => {
      target.removeEventListener("click", onTarget);
      if (guideStep === index) advanceGuide();
    };
    target.addEventListener("click", onTarget);
  }
  layoutGuide();
}

function advanceGuide() {
  const step = GUIDE_STEPS[guideStep];
  if (step && typeof step.run === "function") {
    step.run();
    window.setTimeout(() => {
      if (guideStep + 1 >= GUIDE_STEPS.length) {
        finishGuide();
        return;
      }
      showGuideStep(guideStep + 1);
    }, 80);
    return;
  }
  if (guideStep + 1 >= GUIDE_STEPS.length) {
    finishGuide();
    return;
  }
  showGuideStep(guideStep + 1);
}

async function finishGuide() {
  guideDone = true;
  guideStep = -1;
  clearGuideTarget();
  $("guide-root").classList.add("hidden");
  try {
    if (apiReady(window.pywebview && window.pywebview.api)) {
      await api().complete_guide();
    }
  } catch (_error) {
    /* ignore */
  }
}

function maybeStartGuide(state) {
  if (state && state.settings && state.settings.guide_done) {
    guideDone = true;
  }
  if (!state || !state.logged_in || guideDone || guideStep >= 0) {
    return;
  }
  window.setTimeout(() => showGuideStep(0), 240);
}

window.addEventListener("pywebviewready", () => {
  boot();
});
if (apiReady(window.pywebview && window.pywebview.api)) {
  boot();
} else {
  show("login");
  setMessage($("login-message"), "正在连接本机接口…", false);
  boot();
}
