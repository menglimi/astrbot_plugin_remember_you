const API = "/astrbot_plugin_remember_you/page";
const PAGE_ENDPOINT_PREFIX = "page";

const VIEWS = {
  objects: { title: "上下文管理", hint: "按私聊/群聊分别配置上下文压缩策略。" },
  film: { title: "群聊记忆", hint: "查看群聊范围内可召回、可管理的结构化记忆。" },
  microscope: { title: "记忆显微镜", hint: "输入一句话，模拟当前对象下的召回和过滤。" },
  relations: { title: "用户记忆", hint: "聚焦用户画像、偏好、称呼和关系声明。" },
  review: { title: "个人记忆", hint: "查看 Bot 自身的每日生活日程、当前状态和细化片段。" },
  archive: { title: "维护 / 迁移 / 配置", hint: "执行维护、迁移、清理和导入修复。" },
  maintain: { title: "私聊记忆", hint: "查看私聊范围内的对话、偏好、事实和稳定记忆。" },
};

const PERSONAL_MEMORY_VIEW = {
  available: {
    title: "个人记忆",
    hint: "查看 Bot 自身的每日生活日程、当前状态和细化片段。",
    small: "日程 · 细化",
  },
  unavailable: {
    title: "个人记忆不可用",
    hint: "需要安装并启用主动陪伴插件后，才能查看 Bot 自身的日程与细化。",
    small: "需要陪伴插件",
  },
};

const SECONDARY_NAV = {
  objects: [
    { id: "private", label: "私聊", sublabel: "上下文管理配置", badge: "私聊" },
    { id: "group", label: "群聊", sublabel: "上下文管理配置", badge: "群聊" },
    { id: "logs", label: "注入记录", sublabel: "最近召回与过滤", badge: "日志" },
  ],
  microscope: [
    { id: "query", label: "召回测试", sublabel: "输入一句话模拟检索", badge: "测试" },
    { id: "hits", label: "命中记忆", sublabel: "查看可注入结果", badge: "命中" },
    { id: "blocked", label: "过滤原因", sublabel: "查看被挡下的记忆", badge: "过滤" },
  ],
  relations: [
    { id: "all", label: "全部用户记忆", sublabel: "画像、偏好与关系", badge: "全部" },
    { id: "profile", label: "用户画像", sublabel: "稳定画像片段", badge: "画像" },
    { id: "preference", label: "偏好", sublabel: "喜好、习惯、倾向", badge: "偏好" },
    { id: "relationship", label: "关系声明", sublabel: "身份和关系线索", badge: "关系" },
    { id: "explicit", label: "明确记住", sublabel: "用户主动要求记住", badge: "记住" },
  ],
  archive: [
    { id: "config", label: "配置", sublabel: "当前记忆策略快照", badge: "配置" },
    { id: "maintenance", label: "维护", sublabel: "修复索引和数据状态", badge: "维护" },
    { id: "migration", label: "迁移", sublabel: "LivingMemory 预览与导入", badge: "迁移" },
    { id: "clear", label: "清理", sublabel: "清空全部记忆数据", badge: "清理" },
  ],
};

const state = {
  stats: {},
  buckets: [],
  activeView: "objects",
  activeBucketId: "all",
  activeMemoryId: "",
  secondaryNav: {},
  companionPersonalAvailable: null,
  personalDates: [],
  selectedPersonalDate: "",
  selectedScheduleIndex: "",
  animatePersonalDateRail: false,
  pendingPersonalFilmReveal: false,
  personalAlignTimer: 0,
};

const DEFAULT_THEME = "yuebai";
const THEME_OPTIONS = [
  "huangbaiyou", "tianpiao", "haitianxia", "yingying", "oubi", "qingming", "zipu",
  "shanlan", "qielan", "tuihong", "congqing", "yuebai", "mocan", "gupiao",
];
const THEME_ALIASES = {
  黄白游: "huangbaiyou",
  天缥: "tianpiao",
  海天霞: "haitianxia",
  盈盈: "yingying",
  欧碧: "oubi",
  青冥: "qingming",
  紫蒲: "zipu",
  山岚: "shanlan",
  窃蓝: "qielan",
  退红: "tuihong",
  葱倩: "congqing",
  月白: "yuebai",
  墨黪: "mocan",
  骨缥: "gupiao",
};

let railCoverflowFrame = 0;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function compact(value, fallback = "-") {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortId(value) {
  const text = compact(value, "");
  if (!text) return "-";
  if (text.length <= 14) return text;
  return `${text.slice(0, 8)}...${text.slice(-4)}`;
}

function reviewScopeLabel(item) {
  if (item.scope === "group") return `群聊 ${compact(item.group_id || item.object_id || item.session_id, "未知群聊")}`;
  if (item.scope === "private") return `私聊 ${compact(item.object_name || item.object_id || item.session_id, "未知对象")}`;
  if (item.visibility === "bot_self") return "Bot 自己";
  return compact(item.scope, "未识别范围");
}

function reviewSourceLabel(item) {
  const plugin = compact(item.source_plugin, "");
  const batch = shortId(item.import_batch_id);
  if (plugin && batch !== "-") return `${plugin} · ${batch}`;
  return plugin || compact(item.memory_type, "记忆记录");
}

function isNumericOnlyContent(value) {
  return /^[0-9]+$/.test(String(value ?? "").trim());
}

function reviewCard(item, index) {
  const content = compact(item.content, "(空内容)");
  const evidence = compact(item.evidence, "");
  const needsRepair = item.source_plugin === "livingmemory" && isNumericOnlyContent(content);
  const hasEvidence = evidence && evidence !== content;
  return `
    <article class="review-card">
      <div class="review-card-main">
        <div class="review-card-top">
          <span class="review-number">#${escapeHtml(index + 1)}</span>
          <span class="review-id">${escapeHtml(shortId(item.memory_id))}</span>
          <div class="badges">
            <span class="badge red">${escapeHtml(item.status || "pending")}</span>
            <span class="badge gold">${escapeHtml(item.reality_level || "待确认")}</span>
            <span class="badge blue">${escapeHtml(item.memory_type || "memory")}</span>
          </div>
        </div>
        <p class="review-content ${needsRepair ? "needs-repair" : ""}">${escapeHtml(needsRepair ? "这条 LivingMemory 导入内容只有编号，需先在维护工具中执行“修复 LivingMemory 内容”。" : content)}</p>
        ${hasEvidence ? `<p class="review-evidence">${escapeHtml(evidence)}</p>` : ""}
        <dl class="review-meta-grid">
          <div><dt>范围</dt><dd>${escapeHtml(reviewScopeLabel(item))}</dd></div>
          <div><dt>来源</dt><dd>${escapeHtml(reviewSourceLabel(item))}</dd></div>
          <div><dt>可见性</dt><dd>${escapeHtml(item.visibility || "-")}</dd></div>
          <div><dt>时间</dt><dd>${escapeHtml(formatTime(item.occurred_at || item.created_at))}</dd></div>
        </dl>
        <div class="review-reason"><b>审核原因</b><span>${escapeHtml(item.reason || "待人工确认")}</span></div>
      </div>
      <div class="review-actions">
        <button class="approve" data-review="auto" data-id="${escapeHtml(item.memory_id)}" type="button">通过</button>
        <button class="reject" data-review="rejected" data-id="${escapeHtml(item.memory_id)}" type="button">拒绝</button>
        <button class="ghost mini" data-review-open="${escapeHtml(item.memory_id)}" type="button">详情</button>
      </div>
    </article>
  `;
}

async function apiGet(path) {
  return apiRequest(path, { method: "GET" });
}

async function apiPost(path, payload = {}) {
  return apiRequest(path, { method: "POST", body: payload });
}

async function apiRequest(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const bridge = await waitForBridge();
  let data;
  if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
    data = await bridgeRequest(bridge, path, method, options.body);
  } else if (new URLSearchParams(window.location.search).get("debug_http") === "1") {
    data = await httpRequest(path, method, options.body);
  } else {
    throw new Error("未检测到 AstrBot 官方插件 Page 桥接，请从 AstrBot 后台的插件拓展页打开");
  }
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch (error) {
      throw new Error(data);
    }
  }
  if (!data || data.success === false) throw new Error(data?.error || "请求失败");
  return data.data ?? data;
}

async function waitForBridge() {
  for (let i = 0; i < 24; i += 1) {
    const bridge = getBridge();
    if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
      return bridge;
    }
    await sleep(80);
  }
  return null;
}

function getBridge() {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  try {
    if (window.parent && window.parent !== window && window.parent.AstrBotPluginPage) {
      return window.parent.AstrBotPluginPage;
    }
  } catch (error) {
    return null;
  }
  return null;
}

async function bridgeRequest(bridge, path, method, body) {
  const url = new URL(path, "https://astrbot-plugin-page.local/");
  const endpoint = `${PAGE_ENDPOINT_PREFIX}/${url.pathname.replace(/^\/+/, "")}`.replace(/\/+/g, "/");
  if (method === "GET") {
    const params = Object.fromEntries(url.searchParams.entries());
    return bridge.apiGet(endpoint, Object.keys(params).length ? params : undefined);
  }
  return bridge.apiPost(endpoint, body || {});
}

async function httpRequest(path, method, body) {
  const response = await fetch(`${API}${path}`, {
    method,
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  return response.json();
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function setMessage(text) {
  $("#subtitle").textContent = text;
}

function setBusy(active, text = "正在处理...") {
  const app = $("#app");
  const layer = $("#busyLayer");
  if (!app || !layer) return;
  app.classList.toggle("is-busy", active);
  layer.setAttribute("aria-hidden", active ? "false" : "true");
  $("#busyText").textContent = text;
}

function normalizeTheme(theme) {
  const value = String(theme || "").trim();
  return THEME_OPTIONS.includes(value) ? value : (THEME_ALIASES[value] || DEFAULT_THEME);
}

function applyTheme(theme) {
  const next = normalizeTheme(theme);
  document.documentElement.dataset.theme = next;
  $("#app")?.setAttribute("data-theme", next);
}

async function loadConfiguredTheme() {
  applyTheme(DEFAULT_THEME);
  try {
    const data = await apiGet("/context/config");
    applyTheme(data.appearance?.theme_key || data.appearance?.theme);
  } catch (error) {
    applyTheme(DEFAULT_THEME);
  }
}

function showToast(text, tone = "info") {
  const toast = $("#toast");
  if (!toast) return;
  toast.textContent = text;
  toast.dataset.tone = tone;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.classList.remove("is-visible");
  }, tone === "error" ? 4200 : 2400);
}

function loadingState(text = "正在读取胶片...") {
  return `<div class="loading-state"><span></span><b>${escapeHtml(text)}</b></div>`;
}

function panelError(error, retryLabel = "重试") {
  return `
    <div class="empty-state error-state">
      <b>读取失败</b>
      <span>${escapeHtml(error?.message || error || "未知错误")}</span>
      <button data-retry-active type="button">${escapeHtml(retryLabel)}</button>
    </div>
  `;
}

async function withBusy(text, task) {
  try {
    setBusy(true, text);
    return await task();
  } catch (error) {
    showToast(error.message || "操作失败", "error");
    return undefined;
  } finally {
    setBusy(false);
  }
}

async function withButton(button, text, task) {
  const original = button.textContent;
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = text;
  try {
    return await task();
  } catch (error) {
    showToast(error.message || "操作失败", "error");
    return undefined;
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = original;
  }
}

function activeBucket() {
  return state.buckets.find((bucket) => bucket.id === state.activeBucketId) || state.buckets[0];
}

function bucketLabel(bucket = activeBucket()) {
  return bucket?.label || "全部记忆";
}

function isWindowBucket(bucket) {
  return Boolean(bucket && ["group", "private"].includes(bucket.scope) && bucket.target_id);
}

function windowKindLabel(scope) {
  return scope === "group" ? "群聊" : "私聊";
}

function windowOptionValue(scope, id) {
  return `${scope}:${id}`;
}

function parseWindowOption(value) {
  const parts = String(value || "").split(":");
  const scope = parts.shift() || "";
  return { scope, id: parts.join(":") };
}

function bucketByWindow(scope, id) {
  return state.buckets.find((bucket) => bucket.scope === scope && bucket.target_id === id);
}

function windowLabel(scope, id) {
  const bucket = bucketByWindow(scope, id);
  return bucket?.label || `${windowKindLabel(scope)} ${id || "未知窗口"}`;
}

function permissionTargets(bucket) {
  return state.buckets
    .filter((item) => isWindowBucket(item) && !(item.scope === bucket.scope && item.target_id === bucket.target_id))
    .sort((a, b) => {
      if (a.scope !== b.scope) return a.scope === "group" ? -1 : 1;
      return a.label.localeCompare(b.label, "zh-CN");
    });
}

function secondaryNavItems(view = state.activeView) {
  return SECONDARY_NAV[view] || [];
}

function defaultSecondaryNav(view = state.activeView) {
  return secondaryNavItems(view)[0]?.id || "";
}

function activeSecondaryNav(view = state.activeView) {
  const items = secondaryNavItems(view);
  if (!items.length) return "";
  const active = state.secondaryNav[view];
  if (items.some((item) => item.id === active)) return active;
  state.secondaryNav[view] = items[0].id;
  return items[0].id;
}

function activeSecondaryItem(view = state.activeView) {
  const active = activeSecondaryNav(view);
  return secondaryNavItems(view).find((item) => item.id === active) || null;
}

function renderSecondaryNav(view = state.activeView, immediate = false) {
  if (view === "review") return;
  const items = secondaryNavItems(view);
  if (!items.length) {
    renderBuckets();
    return;
  }
  const rail = document.querySelector(".object-rail");
  const railTitle = document.querySelector(".rail-head b");
  const clearButton = $("#clearTargetBtn");
  const active = activeSecondaryNav(view);
  rail?.classList.remove("is-scoped-rail");
  rail?.classList.add("is-secondary-nav");
  if (railTitle) railTitle.textContent = "二级导航";
  if (clearButton) clearButton.textContent = "默认";
  $("#bucketList").innerHTML = items.map((item) => `
    <button class="bucket secondary-nav-item${item.id === active ? " is-active" : ""}" data-secondary-nav="${escapeHtml(item.id)}" type="button" aria-current="${item.id === active ? "true" : "false"}">
      <b>${escapeHtml(item.label)}</b>
      <small>${escapeHtml(item.sublabel)}</small>
      <div class="badges"><span class="badge blue">${escapeHtml(item.badge)}</span></div>
    </button>
  `).join("");
  $$("#bucketList [data-secondary-nav]").forEach((item) => {
    item.addEventListener("click", () => selectSecondaryNav(item.dataset.secondaryNav));
  });
  const activeItem = activeSecondaryItem(view);
  $("#activeTarget").textContent = activeItem ? `${VIEWS[view]?.title || "二级页"} · ${activeItem.label}` : (VIEWS[view]?.title || "二级页");
  resetRailCoverflow();
  requestAnimationFrame(() => moveSecondaryNavToStandard(immediate));
}

async function selectSecondaryNav(id) {
  const view = state.activeView;
  if (!secondaryNavItems(view).some((item) => item.id === id)) return;
  state.secondaryNav[view] = id;
  renderSecondaryNav(view);
  clearDetail();
  await loadActiveView();
}

function moveSecondaryNavToStandard(immediate = false) {
  if (state.activeView === "review") return;
  const app = $("#app");
  const list = $("#bucketList");
  const rail = document.querySelector(".object-rail.is-secondary-nav");
  if (!app || !list || !rail) return;
  const items = Array.from(list.querySelectorAll(".secondary-nav-item"));
  const active = list.querySelector(".secondary-nav-item.is-active");
  if (!active || !items.length) return;
  const currentShift = parseFloat(getComputedStyle(app).getPropertyValue("--secondary-nav-shift")) || 0;
  const standard = items[Math.min(1, items.length - 1)];
  const activeTop = active.getBoundingClientRect().top - currentShift;
  const standardTop = standard.getBoundingClientRect().top - currentShift;
  list.style.transition = immediate ? "none" : "";
  app.style.setProperty("--secondary-nav-shift", `${Math.round(standardTop - activeTop)}px`);
  if (immediate) {
    list.offsetHeight;
    list.style.transition = "";
  }
}

function contextParams(extra = {}) {
  const bucket = activeBucket();
  const params = new URLSearchParams();
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim()) {
      params.set(key, String(value).trim());
    }
  });
  if (!bucket || bucket.id === "all") return params;
  if (bucket.id === "self") {
    params.set("visibility", "bot_self");
    return params;
  }
  if (bucket.scope) params.set("scope", bucket.scope);
  if (bucket.scope === "private") {
    if (bucket.target_id) params.set("entity_id", bucket.target_id);
    if (bucket.session_id) params.set("session_id", bucket.session_id);
  } else if (bucket.scope === "group") {
    if (bucket.group_id) {
      params.set("group_id", bucket.group_id);
    } else if (bucket.target_id) {
      params.set("entity_id", bucket.target_id);
    }
    if (bucket.session_id) params.set("session_id", bucket.session_id);
  }
  return params;
}

function contextPayload(query) {
  const bucket = activeBucket();
  const payload = { query, top_k: 8, scope: "unknown" };
  if (!bucket || bucket.id === "all") return payload;
  if (bucket.id === "self") {
    payload.session_id = "bot_self";
    payload.scope = "unknown";
    return payload;
  }
  payload.scope = bucket.scope || "unknown";
  payload.session_id = bucket.session_id || "";
  if (bucket.scope === "private") {
    payload.user_id = bucket.target_id || "";
  }
  if (bucket.scope === "group") {
    payload.group_id = bucket.group_id || bucket.target_id || "";
  }
  return payload;
}

function renderStats(stats) {
  const items = [
    ["记忆", stats.total_memories],
    ["群聊记忆", stats.by_scope?.group ?? 0],
    ["私聊记忆", stats.by_scope?.private ?? 0],
    ["稳定记忆", stats.stable_memories ?? 0],
  ];
  $("#stats").innerHTML = items.map(([label, value]) => `
    <article class="stat"><b>${escapeHtml(value ?? 0)}</b><span>${escapeHtml(label)}</span></article>
  `).join("");
}

function normalizeBuckets(rawBuckets) {
  const normalized = [
    {
      id: "all",
      label: "全部记忆",
      sublabel: "不限定对象",
      memory_count: state.stats.total_memories || 0,
      pending_count: state.stats.pending_review || 0,
      latest_at: "",
    },
    {
      id: "self",
      label: "Bot 自己",
      sublabel: "行动、创作、搜索、阅读",
      memory_count: 0,
      pending_count: 0,
      latest_at: "",
    },
  ];
  for (const item of rawBuckets || []) {
    const scope = compact(item.scope, "unknown");
    const targetId = compact(item.target_id, "");
    if (!targetId) continue;
    const name = compact(item.target_name, "");
    const label = scope === "group"
      ? (name || `群聊 ${targetId}`)
      : (name || `私聊 ${targetId}`);
    normalized.push({
      id: `${scope}:${targetId}`,
      scope,
      target_id: targetId,
      group_id: item.sample_group_id || (scope === "group" ? targetId : ""),
      session_id: item.sample_session_id || "",
      label,
      sublabel: `${scope === "group" ? "群聊" : "私聊"} · ${targetId}`,
      memory_count: item.memory_count || 0,
      pending_count: item.pending_count || 0,
      archived_count: item.archived_count || 0,
      latest_at: item.latest_at || "",
    });
  }
  return normalized;
}

function bucketCard(bucket) {
  const active = bucket.id === state.activeBucketId ? " is-active" : "";
  const pending = bucket.pending_count ? `<span class="badge red">待审核 ${escapeHtml(bucket.pending_count)}</span>` : "";
  const canConfigure = isWindowBucket(bucket) && ["film", "maintain"].includes(state.activeView);
  const permission = canConfigure
    ? `<button class="bucket-acl-btn" data-acl-bucket="${escapeHtml(bucket.id)}" type="button" title="配置记忆权限" aria-label="配置 ${escapeHtml(bucket.label)} 记忆权限">权限</button>`
    : "";
  return `
    <article class="bucket${active}${canConfigure ? " has-acl" : ""}" data-bucket-id="${escapeHtml(bucket.id)}" role="button" tabindex="0" aria-current="${bucket.id === state.activeBucketId ? "true" : "false"}">
      ${permission}
      <b>${escapeHtml(bucket.label)}</b>
      <small>${escapeHtml(bucket.sublabel || "")}</small>
      <div class="badges">
        <span class="badge blue">${escapeHtml(bucket.memory_count || 0)} 条</span>
        ${pending}
      </div>
    </article>
  `;
}

function scopedRailConfig(scope) {
  if (scope === "group") {
    return { title: "群聊列表", clear: "全部群聊", label: "全部群聊", sublabel: "不限定群聊", badge: "群聊" };
  }
  return { title: "私聊用户", clear: "全部私聊", label: "全部私聊", sublabel: "不限定用户", badge: "私聊" };
}

function bindBucketListInteractions() {
  $$("#bucketList [data-bucket-id]").forEach((item) => {
    item.addEventListener("click", (event) => {
      if (event.target.closest("[data-acl-bucket]")) return;
      selectBucket(item.dataset.bucketId);
    });
    item.addEventListener("keydown", (event) => {
      if (event.target.closest("[data-acl-bucket]")) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectBucket(item.dataset.bucketId);
      }
    });
  });
  $$("#bucketList [data-acl-bucket]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openBucketPermissions(button.dataset.aclBucket);
    });
  });
}

function currentRailScope() {
  if (state.activeView === "film") return "group";
  if (state.activeView === "maintain") return "private";
  return "";
}

function renderScopedBucketRail(scope) {
  const rail = document.querySelector(".object-rail");
  rail?.classList.remove("is-secondary-nav");
  rail?.classList.add("is-scoped-rail");
  $("#app")?.style.removeProperty("--secondary-nav-shift");
  const config = scopedRailConfig(scope);
  const railTitle = document.querySelector(".rail-head b");
  const clearButton = $("#clearTargetBtn");
  if (railTitle) railTitle.textContent = config.title;
  if (clearButton) clearButton.textContent = config.clear;

  const scopedBuckets = state.buckets.filter((bucket) => bucket.scope === scope);
  if (state.activeBucketId !== "all" && !scopedBuckets.some((bucket) => bucket.id === state.activeBucketId)) {
    state.activeBucketId = "all";
  }
  const totalCount = scopedBuckets.reduce((sum, bucket) => sum + Number(bucket.memory_count || 0), 0);
  const allBucket = {
    id: "all",
    label: config.label,
    sublabel: config.sublabel,
    memory_count: totalCount,
    pending_count: scopedBuckets.reduce((sum, bucket) => sum + Number(bucket.pending_count || 0), 0),
  };
  $("#bucketList").innerHTML = [allBucket, ...scopedBuckets].map((bucket) => bucketCard(bucket)).join("");
  bindBucketListInteractions();
  $("#activeTarget").textContent = state.activeBucketId === "all" ? config.label : bucketLabel();
  requestRailCoverflow();
  centerActiveBucket();
}

function renderBuckets() {
  const rail = document.querySelector(".object-rail");
  rail?.classList.remove("is-secondary-nav");
  rail?.classList.remove("is-scoped-rail");
  $("#app")?.style.removeProperty("--secondary-nav-shift");
  const railTitle = document.querySelector(".rail-head b");
  const clearButton = $("#clearTargetBtn");
  if (railTitle) railTitle.textContent = "观察对象";
  if (clearButton) clearButton.textContent = "全部";
  $("#bucketList").innerHTML = state.buckets.map(bucketCard).join("");
  const objectCards = $("#objectCards");
  if (objectCards) {
    objectCards.innerHTML = state.buckets.map((bucket) => `
      <article class="object-card${bucket.id === state.activeBucketId ? " is-active" : ""}" data-bucket-id="${escapeHtml(bucket.id)}" role="button" tabindex="0" aria-current="${bucket.id === state.activeBucketId ? "true" : "false"}">
        <span class="item-title">${escapeHtml(bucket.label)}</span>
        <div class="item-meta">${escapeHtml(bucket.sublabel || "全局范围")} · 最近 ${escapeHtml(formatTime(bucket.latest_at))}</div>
        <div class="badges">
          <span class="badge blue">${escapeHtml(bucket.memory_count || 0)} 条记忆</span>
          <span class="badge ${bucket.pending_count ? "red" : "teal"}">待审核 ${escapeHtml(bucket.pending_count || 0)}</span>
        </div>
      </article>
    `).join("");
  }
  bindBucketListInteractions();
  $$("#objectCards [data-bucket-id]").forEach((item) => {
    item.addEventListener("click", () => selectBucket(item.dataset.bucketId));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectBucket(item.dataset.bucketId);
      }
    });
  });
  $("#activeTarget").textContent = bucketLabel();
  requestRailCoverflow();
  centerActiveBucket();
}

function renderPersonalDateRail(dates, selectedDate) {
  const app = $("#app");
  const list = $("#bucketList");
  const rail = document.querySelector(".object-rail");
  rail?.classList.remove("is-secondary-nav");
  rail?.classList.remove("is-scoped-rail");
  app?.style.removeProperty("--secondary-nav-shift");
  const animate = state.animatePersonalDateRail;
  if (app && list && !animate) {
    list.style.transition = "none";
    app.style.removeProperty("--personal-reel-shift");
    list.offsetHeight;
    list.style.transition = "";
  }
  state.personalDates = Array.isArray(dates) ? dates : [];
  state.selectedPersonalDate = selectedDate || state.personalDates[0] || "";
  const railTitle = document.querySelector(".rail-head b");
  const clearButton = $("#clearTargetBtn");
  if (railTitle) railTitle.textContent = "日期胶卷";
  if (clearButton) clearButton.textContent = "今天";
  $("#bucketList").innerHTML = state.personalDates.length ? state.personalDates.map((date) => {
    const active = date === state.selectedPersonalDate ? " is-active" : "";
    const label = date === todayKey() ? "今天" : formatDateLabel(date);
    return `
      <button class="bucket date-reel${active}" data-personal-date="${escapeHtml(date)}" type="button" aria-current="${date === state.selectedPersonalDate ? "true" : "false"}">
        <b>${escapeHtml(label)}</b>
        <small>${escapeHtml(date)}</small>
      </button>
    `;
  }).join("") : `<div class="empty-state">还没有可选择的日期。</div>`;
  $$("#bucketList [data-personal-date]").forEach((item) => {
    item.addEventListener("click", () => selectPersonalDate(item.dataset.personalDate));
  });
  $("#activeTarget").textContent = state.selectedPersonalDate ? `个人记忆 · ${state.selectedPersonalDate}` : "个人记忆";
  resetRailCoverflow();
  state.pendingPersonalFilmReveal = animate;
  state.animatePersonalDateRail = false;
  requestAnimationFrame(() => movePersonalDateToStandard(!animate));
}

async function selectPersonalDate(date) {
  const nextDate = date || "";
  if (nextDate && nextDate !== state.selectedPersonalDate) {
    await retractScheduleFilmBeforeDateMove();
  }
  state.selectedPersonalDate = nextDate;
  state.selectedScheduleIndex = "";
  state.animatePersonalDateRail = true;
  clearDetail();
  await loadPersonalMemory();
  resetRailCoverflow();
}

function movePersonalDateToStandard(immediate = false) {
  if (state.activeView !== "review") return;
  const app = $("#app");
  const list = $("#bucketList");
  if (!app || !list) return;
  const reels = Array.from(list.querySelectorAll(".bucket.date-reel"));
  const active = list.querySelector(".bucket.date-reel.is-active");
  if (!active || !reels.length) return;
  const currentShift = parseFloat(getComputedStyle(app).getPropertyValue("--personal-reel-shift")) || 0;
  const standard = reels[Math.min(1, reels.length - 1)];
  const activeTop = active.getBoundingClientRect().top - currentShift;
  const standardTop = standard.getBoundingClientRect().top - currentShift;
  list.style.transition = immediate ? "none" : "";
  app.style.setProperty("--personal-reel-shift", `${Math.round(standardTop - activeTop)}px`);
  if (immediate) {
    list.offsetHeight;
    list.style.transition = "";
  }
  schedulePersonalScheduleAlign(immediate);
}

function schedulePersonalScheduleAlign(immediate = false) {
  const list = $("#bucketList");
  window.clearTimeout(state.personalAlignTimer);
  if (immediate || !list) {
    requestAnimationFrame(alignPersonalScheduleToReel);
    return;
  }
  const finish = (event) => {
    if (event.target !== list || event.propertyName !== "transform") return;
    window.clearTimeout(state.personalAlignTimer);
    alignPersonalScheduleToReel();
  };
  list.addEventListener("transitionend", finish, { once: true });
  state.personalAlignTimer = window.setTimeout(alignPersonalScheduleToReel, 780);
}

function todayKey() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDateLabel(date) {
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date || "-";
  return parsed.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function centerActiveBucket() {
  const active = $("#bucketList .bucket.is-active");
  if (!active || !$("#app").classList.contains("is-workspace") || state.activeView === "review" || document.querySelector(".object-rail")?.classList.contains("is-secondary-nav")) return;
  window.setTimeout(() => {
    if (state.activeView === "review" || document.querySelector(".object-rail")?.classList.contains("is-secondary-nav")) return;
    active.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    requestRailCoverflow();
  }, 40);
}

function requestRailCoverflow() {
  const rail = document.querySelector(".object-rail");
  if (
    state.activeView === "review"
    || rail?.classList.contains("is-secondary-nav")
    || rail?.classList.contains("is-scoped-rail")
  ) {
    resetRailCoverflow();
    return;
  }
  if (railCoverflowFrame) return;
  railCoverflowFrame = window.requestAnimationFrame(() => {
    railCoverflowFrame = 0;
    updateRailCoverflow();
  });
}

function resetRailCoverflow() {
  $("#bucketList")?.querySelectorAll(".bucket").forEach((bucket) => {
    ["--cf-rot", "--cf-scale", "--cf-z", "--cf-x", "--cf-opacity"].forEach((name) => {
      bucket.style.removeProperty(name);
    });
    bucket.style.removeProperty("z-index");
    bucket.classList.remove("is-cover-center");
  });
}

function updateRailCoverflow() {
  const list = $("#bucketList");
  if (!list || !$("#app").classList.contains("is-workspace") || state.activeView === "review") return;
  const listRect = list.getBoundingClientRect();
  const centerY = listRect.top + listRect.height / 2;
  const half = listRect.height / 2 || 1;
  list.querySelectorAll(".bucket").forEach((bucket) => {
    const rect = bucket.getBoundingClientRect();
    const bucketY = rect.top + rect.height / 2;
    const distance = Math.max(-1, Math.min(1, (bucketY - centerY) / half));
    const amount = Math.abs(distance);
    const rotate = Math.max(-58, Math.min(58, distance * 62));
    const scale = 1.08 - amount * 0.22;
    const depth = -amount * 110;
    const shift = -amount * 12;
    bucket.style.setProperty("--cf-rot", `${rotate.toFixed(2)}deg`);
    bucket.style.setProperty("--cf-scale", scale.toFixed(3));
    bucket.style.setProperty("--cf-z", `${depth.toFixed(1)}px`);
    bucket.style.setProperty("--cf-x", `${shift.toFixed(1)}px`);
    bucket.style.setProperty("--cf-opacity", String((1 - amount * 0.42).toFixed(3)));
    bucket.style.zIndex = String(1000 - Math.round(amount * 900));
    bucket.classList.toggle("is-cover-center", amount < 0.23);
  });
}

async function loadStats() {
  const data = await apiGet("/stats");
  state.stats = data.stats || {};
  renderStats(state.stats);
  setMessage("");
}

async function loadBuckets() {
  const data = await apiGet("/buckets?limit=180");
  state.buckets = normalizeBuckets(data.buckets || []);
  if (!state.buckets.some((bucket) => bucket.id === state.activeBucketId)) {
    state.activeBucketId = "all";
  }
  const scope = currentRailScope();
  if ($("#app")?.classList.contains("is-workspace") && scope) {
    renderScopedBucketRail(scope);
  } else if ($("#app")?.classList.contains("is-workspace") && state.activeView !== "review") {
    renderSecondaryNav(state.activeView, true);
  } else if (state.activeView !== "review") {
    renderBuckets();
  }
}

async function selectBucket(id) {
  state.activeBucketId = id || "all";
  const scope = currentRailScope();
  if (scope) {
    renderScopedBucketRail(scope);
  } else {
    renderBuckets();
  }
  clearDetail();
  await loadActiveView();
  requestRailCoverflow();
}

function openView(view) {
  const app = $("#app");
  if (view !== "review") removeRailMountedScheduleFilm();
  state.activeView = view;
  if (view !== "review") state.activeBucketId = "all";
  app.classList.add("is-workspace");
  app.classList.toggle("is-personal-memory", view === "review");
  app.dataset.workspaceView = view;
  $("#backHomeBtn").classList.remove("hidden");
  $$(".filmstrip").forEach((strip) => {
    strip.classList.toggle("is-locked", strip.dataset.view === view);
  });
  $$(".view").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `view-${view}`);
  });
  $("#workspaceTitle").textContent = VIEWS[view]?.title || "记忆面板";
  $("#workspaceHint").textContent = VIEWS[view]?.hint || "";
  if (view === "film") {
    renderScopedBucketRail("group");
  } else if (view === "maintain") {
    renderScopedBucketRail("private");
  } else if (view !== "review") {
    renderSecondaryNav(view, true);
  }
  loadActiveView();
  requestRailCoverflow();
}
  
function returnHome() {
  const app = $("#app");
  removeRailMountedScheduleFilm();
  app.classList.remove("is-workspace");
  app.classList.remove("is-personal-memory");
  app.style.removeProperty("--secondary-nav-shift");
  delete app.dataset.workspaceView;
  $("#backHomeBtn").classList.add("hidden");
  $$(".filmstrip").forEach((strip) => strip.classList.remove("is-locked"));
  requestRailCoverflow();
}

async function loadActiveView() {
  try {
    if (state.activeView === "objects") {
      await loadContextPanel();
    } else if (state.activeView === "film") {
      await loadScopedMemories("#groupMemoryList", "group", "正在读取群聊记忆...", "还没有群聊范围内的记忆。");
    } else if (state.activeView === "microscope") {
      applyMicroscopeView();
    } else if (state.activeView === "relations") {
      await loadUserMemory();
    } else if (state.activeView === "review") {
      await loadPersonalMemory();
    } else if (state.activeView === "maintain") {
      await loadScopedMemories("#privateMemoryList", "private", "正在读取私聊记忆...", "还没有私聊范围内的记忆。");
    } else if (state.activeView === "archive") {
      await loadArchive();
    }
  } catch (error) {
    renderViewError(error);
    showToast(error.message || "读取失败", "error");
  }
}

function renderViewError(error) {
  const targets = {
    objects: "#contextPanel",
    film: "#groupMemoryList",
    relations: "#relationList",
    review: "#personalMemoryList",
    maintain: "#privateMemoryList",
    archive: "#importResult",
  };
  const selector = targets[state.activeView];
  if (!selector) return;
  const target = $(selector);
  if (!target) return;
  target.innerHTML = panelError(error);
  const retry = target.querySelector("[data-retry-active]");
  if (retry) retry.addEventListener("click", () => loadActiveView());
}

function memoryRow(memory) {
  return `
    <article class="row-item memory-frame" data-memory-id="${escapeHtml(memory.id)}">
      <div class="memory-frame-time">
        <b>${escapeHtml(formatTime(memory.occurred_at || memory.created_at))}</b>
        <span>${escapeHtml(shortId(memory.id))}</span>
      </div>
      <div class="memory-frame-main">
        <span class="item-title">${escapeHtml(memory.content || "(空内容)")}</span>
        <div class="badges">
          <span class="badge teal">${escapeHtml(memory.memory_type)}</span>
          <span class="badge blue">${escapeHtml(memory.visibility)}</span>
          <span class="badge gold">${escapeHtml(memory.reality_level)}</span>
          <span class="badge ${memory.review_status === "pending" ? "red" : "violet"}">${escapeHtml(memory.review_status)}</span>
        </div>
      </div>
    </article>
  `;
}

async function loadMemories(extra = {}) {
  const query = $("#globalSearch").value.trim();
  const params = contextParams({ limit: extra.limit || 80, q: query, ...extra });
  const data = await apiGet(`/memories?${params.toString()}`);
  return data.memories || [];
}

function scopedMemoryParams(scope, extra = {}) {
  const query = $("#globalSearch").value.trim();
  const params = new URLSearchParams();
  params.set("limit", String(extra.limit || 100));
  if (query) params.set("q", query);
  if (scope) params.set("scope", scope);
  Object.entries(extra).forEach(([key, value]) => {
    if (key !== "limit" && value !== undefined && value !== null && String(value).trim()) {
      params.set(key, String(value).trim());
    }
  });

  const bucket = activeBucket();
  if (!bucket || bucket.id === "all") return { params, incompatible: false };
  if (bucket.id === "self") return { params, incompatible: Boolean(scope) };
  if (scope && bucket.scope && bucket.scope !== scope) return { params, incompatible: true };

  if (bucket.scope) params.set("scope", bucket.scope);
  if (bucket.scope === "private") {
    if (bucket.target_id) params.set("entity_id", bucket.target_id);
    if (bucket.session_id) params.set("session_id", bucket.session_id);
  } else if (bucket.scope === "group") {
    if (bucket.group_id) params.set("group_id", bucket.group_id);
    if (bucket.session_id) params.set("session_id", bucket.session_id);
  }
  return { params, incompatible: false };
}

function renderMemoryList(selector, memories, emptyText) {
  const target = $(selector);
  if (!target) return;
  target.innerHTML = memories.length
    ? memories.map(memoryRow).join("")
    : `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
  target.querySelectorAll("[data-memory-id]").forEach((row) => {
    row.addEventListener("click", () => showMemory(row.dataset.memoryId));
  });
}

function relationTypesForSecondary() {
  const active = activeSecondaryNav("relations");
  const map = {
    profile: ["user_profile"],
    preference: ["user_preference"],
    relationship: ["relationship_claim"],
    explicit: ["explicit_memory"],
  };
  return map[active] || ["user_profile", "user_preference", "explicit_memory", "relationship_claim"];
}

async function loadScopedMemories(selector, scope, loadingText, emptyText, extra = {}) {
  const target = $(selector);
  if (!target) return;
  target.innerHTML = loadingState(loadingText);
  const { params, incompatible } = scopedMemoryParams(scope, { limit: 120, ...extra });
  if (incompatible) {
    const label = scope === "group" ? "群聊" : "私聊";
    target.innerHTML = `<div class="empty-state">当前观察对象不属于${escapeHtml(label)}范围。请选择${escapeHtml(label)}对象或切回全部。</div>`;
    return;
  }
  const data = await apiGet(`/memories?${params.toString()}`);
  renderMemoryList(selector, data.memories || [], emptyText);
}

function hasMemoryType(memory, types) {
  return types.includes(memory.memory_type);
}

function isUserMemory(memory) {
  return hasMemoryType(memory, ["user_profile", "user_preference", "explicit_memory", "relationship_claim"]);
}

function isPersonalMemory(memory) {
  return memory.visibility === "bot_self"
    && (
      hasMemoryType(memory, ["schedule_fragment", "persona_life"])
      || memory.source_plugin === "private_companion"
      || (memory.tags || []).includes("schedule")
      || (memory.tags || []).includes("persona_life")
    );
}

async function loadContextPanel() {
  const target = $("#contextPanel");
  if (!target) return;
  target.className = "page-panel-stack";
  const section = activeSecondaryNav("objects");
  target.innerHTML = loadingState(section === "logs" ? "正在读取注入记录..." : "正在读取上下文配置...");
  const params = contextParams({ limit: 8 });
  if (section === "logs") {
    const logData = await apiGet(`/logs?${params.toString()}`);
    target.innerHTML = renderContextLogs(logData.items || []);
  } else {
    const config = await apiGet("/context/config");
    target.innerHTML = renderContextConfig(config, section === "group" ? "group" : "private");
    bindContextConfigForm(target);
  }
  target.querySelectorAll("[data-raw]").forEach((row) => {
    row.addEventListener("click", () => showGenericDetail("注入日志", JSON.parse(row.dataset.raw || "{}")));
  });
}

function boolLabel(value) {
  return value ? "开启" : "关闭";
}

function queryModeLabel(mode) {
  if (mode === "guarded_companion") return "受保护陪伴";
  if (mode === "companion_augmented") return "增强检索";
  return "当前消息";
}

function queryModeNote(mode) {
  if (mode === "guarded_companion") return "线索与当前消息重叠时才扩展检索";
  if (mode === "companion_augmented") return "直接拼接陪伴线索，适合强联动场景";
  return "只用当前用户消息检索，记忆作为附加资料";
}

function queryModeTone(mode) {
  if (mode === "companion_augmented") return "gold";
  if (mode === "guarded_companion") return "teal";
  return "blue";
}

function configCard(title, value, note, tone = "blue", badge = "配置") {
  return `
    <article class="config-card">
      <div class="config-card-top">
        <span class="item-title">${escapeHtml(title)}</span>
        <span class="badge ${escapeHtml(tone)}">${escapeHtml(badge)}</span>
      </div>
      <b>${escapeHtml(value)}</b>
      <small>${escapeHtml(note)}</small>
    </article>
  `;
}

function renderContextConfig(config, scope = "private") {
  const profile = (config.context_profiles || {})[scope] || config.context_management || {};
  const providerOptions = Array.isArray(config.provider_options) ? config.provider_options : [];
  const title = scope === "group" ? "群聊上下文感知(原聊天记忆增强)" : "私聊上下文感知(原聊天记忆增强)";
  const scopeLabel = scope === "group" ? "群聊" : "私聊";
  const subtitle = scope === "group"
    ? "当前保存范围：群聊窗口"
    : "当前保存范围：私聊窗口";
  return `
    <section class="context-settings-panel" data-context-scope="${escapeHtml(scope)}">
      <div class="context-settings-title">
        <h4>${escapeHtml(title)}</h4>
        <p>按 AstrBot 原版上下文感知表单整理，保存后仅作用于本插件的${escapeHtml(scopeLabel)}策略。</p>
      </div>
      <form id="contextConfigForm" class="context-form" autocomplete="off">
        <input type="hidden" name="scope" value="${escapeHtml(scope)}" />
        ${contextField({
          label: `启用${scopeLabel}上下文感知`,
          control: contextSwitch("enabled", Boolean(profile.enabled)),
        })}
        ${contextField({
          label: "最大消息数量",
          control: `<input name="max_events" type="number" min="0" step="1" value="${escapeHtml(profile.max_events ?? 300)}" />`,
        })}
        ${contextField({
          label: "自动理解图片",
          hint: `需要设置${scopeLabel}图片转述模型。`,
          control: contextSwitch("auto_understand_images", Boolean(profile.auto_understand_images)),
        })}
        ${contextField({
          label: "主动回复",
          control: contextSwitch("proactive_reply_enabled", Boolean(profile.proactive_reply_enabled)),
        })}
        <details class="context-advanced">
          <summary>高级上下文压缩</summary>
          ${contextField({
            label: "轮次超限时一次丢弃轮数",
            hint: "当历史超过最大消息数量且无法使用 LLM 压缩时，一次丢弃多少条旧消息；0 表示按长度自动裁剪。",
            control: `<input name="drop_events" type="number" min="0" step="1" value="${escapeHtml(profile.drop_events ?? 0)}" />`,
          })}
          ${contextField({
            label: "历史超限或上下文接近上限时的处理方式",
            hint: "接近上下文窗口上限时使用同一策略保护本次请求。",
            control: `
              <select name="overflow_strategy">
                <option value="drop"${profile.overflow_strategy === "drop" ? " selected" : ""}>按对话轮数截断</option>
                <option value="summarize"${profile.overflow_strategy !== "drop" ? " selected" : ""}>由 LLM 压缩上下文</option>
              </select>
            `,
            wide: true,
          })}
          ${contextField({
            label: "上下文压缩提示词",
            hint: "留空时使用默认提示词。可用 {older_context}、{recent_context}、{max_chars}、{session_label}。",
            control: `<textarea name="summary_prompt" rows="5">${escapeHtml(profile.summary_prompt || "")}</textarea>`,
            wide: true,
          })}
          ${contextField({
            label: "压缩时保留最近上下文比例",
            hint: "范围 0-1。0.15 表示保留最近 15%；比例大于 0 时至少保留最后一轮。",
            control: `
              <div class="context-range">
                <input name="retain_recent_ratio_range" type="range" min="0" max="1" step="0.01" value="${escapeHtml(profile.retain_recent_ratio ?? 0.15)}" />
                <input name="retain_recent_ratio" type="number" min="0" max="1" step="0.01" value="${escapeHtml(profile.retain_recent_ratio ?? 0.15)}" />
              </div>
            `,
          })}
          ${contextField({
            label: "用于上下文压缩的模型提供商 ID",
            hint: "留空时不调用 LLM 压缩；模型不可用或压缩失败时回退为截断。",
            control: `
              <div class="provider-inline">
                <input name="summary_provider_id" type="text" list="contextProviderOptions-${escapeHtml(scope)}" value="${escapeHtml(profile.summary_provider_id || "")}" placeholder="Provider ID" />
                <datalist id="contextProviderOptions-${escapeHtml(scope)}">
                  ${providerOptions.map((option) => `<option value="${escapeHtml(option.id || "")}" label="${escapeHtml(option.label || option.id || "")}"></option>`).join("")}
                </datalist>
              </div>
            `,
          })}
          ${contextField({
            label: "上下文窗口兜底值",
            hint: "当模型窗口无法识别时使用。0 表示使用默认兜底。",
            control: `<input name="model_context_tokens" type="number" min="0" step="1" value="${escapeHtml(profile.model_context_tokens ?? 0)}" />`,
          })}
          ${contextField({
            label: "RememberYou 短期上下文注入字数",
            hint: "插件自身注入 short_context 时使用的字符上限，不等同于模型 token 窗口。",
            control: `<input name="max_chars" type="number" min="200" step="50" value="${escapeHtml(profile.max_chars ?? 1200)}" />`,
          })}
          ${contextField({
            label: "压缩摘要字数",
            hint: "LLM 压缩较早上下文时生成的摘要最大字数。",
            control: `<input name="summary_max_chars" type="number" min="80" step="20" value="${escapeHtml(profile.summary_max_chars ?? 360)}" />`,
          })}
          ${contextField({
            label: "异步预压缩触发比例",
            hint: "接近上下文上限时提前在后台压缩，默认贴近 AstrBot 的 82%。",
            control: `<input name="precompress_threshold_percent" type="number" min="1" max="100" step="1" value="${escapeHtml(profile.precompress_threshold_percent ?? 82)}" />`,
          })}
          ${contextField({
            label: "压缩模型名覆盖",
            hint: "可选。留空则使用 Provider 自己配置的默认模型。",
            control: `<input name="summary_model" type="text" value="${escapeHtml(profile.summary_model || "")}" />`,
          })}
        </details>
        <div class="context-form-actions">
          <span>${escapeHtml(subtitle)} · <a href="https://docs.astrbot.app/use/context-compress.html" target="_blank" rel="noreferrer">AstrBot 文档</a></span>
          <button id="saveContextConfigBtn" type="submit">保存${scope === "group" ? "群聊" : "私聊"}配置</button>
        </div>
      </form>
    </section>
  `;
}

function contextSwitch(name, checked = false) {
  return `
    <label class="context-switch">
      <input name="${escapeHtml(name)}" type="checkbox"${checked ? " checked" : ""} />
      <span></span>
    </label>
  `;
}

function contextField({ label, hint, control, wide = false }) {
  return `
    <div class="context-form-row${wide ? " is-wide" : ""}">
      <span>
        <b>${escapeHtml(label)}</b>
        <small>${escapeHtml(hint)}</small>
      </span>
      <div class="context-control">${control}</div>
    </div>
  `;
}

function bindContextConfigForm(root) {
  const form = root.querySelector("#contextConfigForm");
  if (!form) return;
  const ratioRange = form.querySelector("[name='retain_recent_ratio_range']");
  const ratioInput = form.querySelector("[name='retain_recent_ratio']");
  if (ratioRange && ratioInput) {
    ratioRange.addEventListener("input", () => {
      ratioInput.value = ratioRange.value;
    });
    ratioInput.addEventListener("input", () => {
      ratioRange.value = ratioInput.value;
    });
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    withButton(form.querySelector("#saveContextConfigBtn"), "保存中", () => saveContextConfig(form));
  });
}

async function saveContextConfig(form) {
  const payload = contextFormPayload(form);
  const data = await apiPost("/context/config/update", payload);
  showToast(`${payload.scope === "group" ? "群聊" : "私聊"}上下文配置已保存`);
  const scope = data.scope || payload.scope;
  $("#contextPanel").innerHTML = renderContextConfig(
    {
      context_profiles: { [scope]: data.context },
      provider_options: data.provider_options || [],
    },
    scope,
  );
  bindContextConfigForm($("#contextPanel"));
}

function contextFormPayload(form) {
  const data = new FormData(form);
  const num = (name, fallback = 0) => {
    const value = Number(data.get(name));
    return Number.isFinite(value) ? value : fallback;
  };
  const maxEvents = Math.max(0, Math.round(num("max_events", 0)));
  return {
    scope: String(data.get("scope") || "private"),
    context: {
      enabled: data.has("enabled"),
      max_events: maxEvents,
      drop_events: Math.max(0, Math.round(num("drop_events", 0))),
      overflow_strategy: String(data.get("overflow_strategy") || "drop"),
      summary_prompt: String(data.get("summary_prompt") || ""),
      retain_recent_ratio: Math.max(0, Math.min(1, num("retain_recent_ratio", 0.15))),
      auto_understand_images: data.has("auto_understand_images"),
      proactive_reply_enabled: data.has("proactive_reply_enabled"),
      summary_provider_id: String(data.get("summary_provider_id") || ""),
      summary_model: String(data.get("summary_model") || ""),
      summary_fallback_provider_id: "",
      summary_fallback_model: "",
      summary_max_chars: Math.max(80, Math.round(num("summary_max_chars", 360))),
      model_context_tokens: Math.max(0, Math.round(num("model_context_tokens", 0))),
      max_chars: Math.max(200, Math.round(num("max_chars", 1200))),
      async_precompress_enabled: true,
      precompress_threshold_percent: Math.max(1, Math.min(100, Math.round(num("precompress_threshold_percent", 82)))),
      allow_sync_compression: false,
      sync_compression_timeout_ms: 0,
      manage_astrbot_history_enabled: false,
      astrbot_history_mode: "keep",
      keep_recent_messages: 0,
    },
  };
}

function renderContextLogs(logs) {
  return `
    <section class="context-section context-logs film-panel">
      <div class="personal-zone-head">
        <h4>最近注入记录</h4>
        <span>${escapeHtml(logs.length)} Frames</span>
      </div>
      <div class="row-list">
        ${logs.length ? logs.map((item) => `
          <article class="row-item memory-frame" data-raw="${escapeHtml(JSON.stringify(item))}">
            <div class="memory-frame-time">
              <b>${escapeHtml(formatTime(item.created_at))}</b>
              <span>${escapeHtml(item.scope || "unknown")}</span>
            </div>
            <div class="memory-frame-main">
              <span class="item-title">${escapeHtml(item.query || "未记录查询文本")}</span>
              <div class="badges">
                <span class="badge blue">选中 ${escapeHtml((item.selected_memory_ids || []).length)} 条</span>
                <span class="badge teal">过滤 ${escapeHtml((item.blocked_reasons || []).length)} 条</span>
                <span class="badge gold">${escapeHtml(shortId(item.session_id || "-"))}</span>
              </div>
            </div>
          </article>
        `).join("") : `<div class="empty-state">当前范围还没有注入日志。</div>`}
      </div>
    </section>
  `;
}

async function loadUserMemory() {
  const target = $("#relationList");
  if (!target) return;
  target.innerHTML = loadingState("正在读取用户记忆...");
  const types = relationTypesForSecondary();
  const memories = (await loadMemories({ limit: 160 })).filter((memory) => hasMemoryType(memory, types));
  renderMemoryList("#relationList", memories, "当前范围还没有用户画像、偏好或关系声明。");
}

async function loadPersonalMemory() {
  const target = $("#personalMemoryList");
  if (!target) return;
  removeRailMountedScheduleFilm();
  if (state.companionPersonalAvailable === false) {
    updatePersonalMemoryAvailability(false);
    target.innerHTML = renderPersonalMemoryUnavailable("未检测到已加载的主动陪伴插件");
    return;
  }
  target.innerHTML = loadingState("正在读取个人记忆...");
  const query = $("#globalSearch").value.trim();
  const params = new URLSearchParams({ limit: "80" });
  if (query) params.set("q", query);
  if (state.selectedPersonalDate) params.set("date", state.selectedPersonalDate);
  const data = await apiGet(`/companion/personal-memory?${params.toString()}`);
  updatePersonalMemoryAvailability(Boolean(data.available));
  if (!data.available) {
    target.innerHTML = renderPersonalMemoryUnavailable(data.reason || "未检测到已加载的主动陪伴插件");
    return;
  }
  state.selectedPersonalDate = data.selected_date || state.selectedPersonalDate || "";
  renderPersonalDateRail(data.dates || [], state.selectedPersonalDate);
  target.innerHTML = renderPersonalMemoryWorkspace(data.snapshot || {}, data);
  bindPersonalMemoryWorkspace(target, data.snapshot || {}, data);
}

function bindPersonalMemoryWorkspace(target, snapshot, data) {
  target.querySelectorAll("[data-memory-id]").forEach((row) => {
    row.addEventListener("click", () => showMemory(row.dataset.memoryId));
  });
  const film = target.querySelector("[data-schedule-film]");
  const selectSchedule = (index, options = {}) => {
    state.selectedScheduleIndex = index || "";
    target.querySelectorAll(".schedule-frame").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.scheduleIndex === state.selectedScheduleIndex);
    });
    updateScheduleSummary(target, snapshot, { animate: true });
    showPersonalScheduleDetail(snapshot, data, { animate: true });
    if (!options.preserveOffset) centerScheduleFrame(film, state.selectedScheduleIndex);
  };
  target.querySelectorAll("[data-schedule-index]").forEach((row) => {
    row.addEventListener("click", () => {
      if (row.closest("[data-schedule-film]")?.dataset.draggingClick === "1") return;
      selectSchedule(row.dataset.scheduleIndex);
    });
  });
  setupScheduleFilmDrag(film, selectSchedule);
  mountScheduleFilmToRail(film);
  const shouldRevealAfterReel = state.pendingPersonalFilmReveal;
  state.pendingPersonalFilmReveal = false;
  if (shouldRevealAfterReel) {
    prepareScheduleFilmPeek(film);
    revealScheduleFilmAfterReel(film);
  } else {
    requestAnimationFrame(() => applyScheduleFilmOffset(film, 0, true));
    requestAnimationFrame(alignPersonalScheduleToReel);
  }
  updateScheduleSummary(target, snapshot);
  showPersonalScheduleDetail(snapshot, data);
}

function mountScheduleFilmToRail(film) {
  const rail = document.querySelector(".object-rail");
  if (!film || !rail || film.classList.contains("is-rail-mounted")) return;
  film.classList.add("is-rail-mounted");
  rail.appendChild(film);
}

function removeRailMountedScheduleFilm() {
  document.querySelector(".schedule-film.is-rail-mounted")?.remove();
}

function prepareScheduleFilmPeek(film) {
  if (!film) return;
  film.classList.add("is-peeking");
  film.style.transition = "none";
  film.style.minWidth = "0px";
  film.style.width = "0px";
  film.offsetHeight;
  film.style.transition = "";
}

function retractScheduleFilmBeforeDateMove() {
  const film = document.querySelector(".schedule-film.is-rail-mounted");
  if (!film || film.classList.contains("is-peeking")) return Promise.resolve();
  const currentWidth = Math.round(film.getBoundingClientRect().width);
  if (currentWidth <= 2) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      window.clearTimeout(film._retractTimer);
      film.removeEventListener("transitionend", onEnd);
      film.style.transition = "";
      resolve();
    };
    const onEnd = (event) => {
      if (event.target !== film || event.propertyName !== "width") return;
      finish();
    };
    window.clearTimeout(film._retractTimer);
    film.classList.add("is-peeking", "is-retracting");
    film.style.minWidth = "0px";
    film.style.width = `${currentWidth}px`;
    film.style.transition = "width .38s cubic-bezier(.34,.02,.18,1)";
    film.offsetHeight;
    film.addEventListener("transitionend", onEnd);
    requestAnimationFrame(() => {
      film.style.width = "0px";
    });
    film._retractTimer = window.setTimeout(finish, 460);
  });
}

function revealScheduleFilmAfterReel(film) {
  if (!film) return;
  const list = $("#bucketList");
  let done = false;
  const reveal = () => {
    if (done) return;
    done = true;
    window.clearTimeout(film?._revealTimer);
    alignPersonalScheduleToReel();
    requestAnimationFrame(() => revealScheduleFilmPeek(film));
  };
  requestAnimationFrame(() => {
    list?.addEventListener("transitionend", (event) => {
      if (event.target === list && event.propertyName === "transform") reveal();
    }, { once: true });
    film._revealTimer = window.setTimeout(reveal, 820);
  });
}

function revealScheduleFilmPeek(film) {
  if (!film) return;
  applyScheduleFilmOffset(film, 0, false);
  window.clearTimeout(film._peekTimer);
  film._peekTimer = window.setTimeout(() => {
    film.classList.remove("is-peeking");
    film.style.minWidth = "";
  }, 760);
}

function alignPersonalScheduleToReel() {
  const app = $("#app");
  const film = document.querySelector("[data-schedule-film]");
  const reel = document.querySelector(".bucket.date-reel.is-active");
  if (!app || !film || !reel || state.activeView !== "review") {
    app?.style.removeProperty("--personal-film-lift");
    app?.style.removeProperty("--personal-film-shift");
    app?.style.removeProperty("--personal-detail-offset");
    app?.style.removeProperty("--personal-main-height");
    return;
  }
  const styles = getComputedStyle(app);
  const currentLift = parseFloat(styles.getPropertyValue("--personal-film-lift")) || 0;
  const currentShift = parseFloat(styles.getPropertyValue("--personal-film-shift")) || 0;
  const filmRect = film.getBoundingClientRect();
  const reelRect = reel.getBoundingClientRect();
  if (film.classList.contains("is-rail-mounted")) {
    const rail = document.querySelector(".object-rail");
    const railRect = rail?.getBoundingClientRect();
    if (!railRect) return;
    app.style.removeProperty("--personal-film-lift");
    app.style.removeProperty("--personal-film-shift");
    if (getComputedStyle(film).position !== "absolute") {
      alignPersonalPanelsAroundFilm(film.getBoundingClientRect().top, film.getBoundingClientRect().bottom);
      return;
    }
    const top = reelRect.top + reelRect.height / 2 - filmRect.height / 2 - railRect.top;
    const left = reelRect.left + 48 - railRect.left;
    film.style.setProperty("--personal-film-top", `${Math.round(top)}px`);
    film.style.setProperty("--personal-film-left", `${Math.round(left)}px`);
    alignPersonalPanelsAroundFilm(railRect.top + top, railRect.top + top + filmRect.height);
    return;
  }
  const unshiftedTop = filmRect.top - currentLift;
  const unshiftedLeft = filmRect.left - currentShift;
  const targetTop = reelRect.top + reelRect.height / 2 - filmRect.height / 2;
  const targetLeft = reelRect.left + 48;
  const lift = Math.round(targetTop - unshiftedTop);
  const shift = Math.round(targetLeft - unshiftedLeft);
  app.style.setProperty("--personal-film-lift", `${lift}px`);
  app.style.setProperty("--personal-film-shift", `${shift}px`);
  const filmTop = reelRect.top + reelRect.height / 2 - filmRect.height / 2;
  alignPersonalPanelsAroundFilm(filmTop, filmTop + filmRect.height);
}

function alignPersonalPanelsAroundFilm(filmTop, filmBottom) {
  alignPersonalSummaryAboveFilm(filmTop);
  document.querySelector(".workspace-main")?.offsetHeight;
  alignPersonalDetailBelowFilm(filmBottom);
}

function alignPersonalSummaryAboveFilm(filmTop) {
  const app = $("#app");
  const main = document.querySelector(".workspace-main");
  if (!app || !main || state.activeView !== "review") return;
  if (window.matchMedia("(max-width: 1080px)").matches) {
    app.style.removeProperty("--personal-main-height");
    return;
  }
  const mainRect = main.getBoundingClientRect();
  const height = Math.max(154, Math.round(filmTop - mainRect.top));
  app.style.setProperty("--personal-main-height", `${height}px`);
}

function alignPersonalDetailBelowFilm(filmBottom) {
  const app = $("#app");
  const detail = $("#detailDrawer");
  if (!app || !detail || state.activeView !== "review") return;
  if (app.style.getPropertyValue("--personal-detail-offset")) return;
  const currentOffset = parseFloat(getComputedStyle(app).getPropertyValue("--personal-detail-offset")) || 0;
  const detailRect = detail.getBoundingClientRect();
  const unshiftedTop = detailRect.top - currentOffset;
  const targetTop = Math.round(filmBottom + 16);
  const offset = Math.round(targetTop - unshiftedTop);
  const next = window.matchMedia("(max-width: 1080px)").matches ? Math.max(0, offset) : offset;
  app.style.setProperty("--personal-detail-offset", `${next}px`);
}

function setupScheduleFilmDrag(film, selectSchedule) {
  if (!film) return;
  const track = film.querySelector("[data-schedule-track]");
  if (!track) return;
  let isDown = false;
  let startX = 0;
  let startOffset = 0;
  let moved = 0;
  let lastSelected = "";
  const selectNearestAtMarker = () => {
    const nearest = nearestScheduleFrame(film);
    if (!nearest || nearest.dataset.scheduleIndex === lastSelected) return;
    lastSelected = nearest.dataset.scheduleIndex;
    selectSchedule(lastSelected, { preserveOffset: true });
  };
  applyScheduleFilmOffset(film, Number(film.dataset.offset || 0), true);
  selectNearestAtMarker();
  film.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    isDown = true;
    moved = 0;
    startX = event.clientX;
    startOffset = Number(film.dataset.offset || 0);
    film.classList.add("is-dragging");
    film.setPointerCapture?.(event.pointerId);
  });
  film.addEventListener("pointermove", (event) => {
    if (!isDown) return;
    event.preventDefault();
    const dx = event.clientX - startX;
    moved = Math.max(moved, Math.abs(dx));
    applyScheduleFilmOffset(film, startOffset + dx);
    selectNearestAtMarker();
  });
  const finish = (event) => {
    if (!isDown) return;
    isDown = false;
    const nearest = moved > 8 ? nearestScheduleFrame(film) : null;
    film.classList.remove("is-dragging");
    film.releasePointerCapture?.(event.pointerId);
    if (moved > 8) {
      film.dataset.draggingClick = "1";
      if (nearest) selectSchedule(nearest.dataset.scheduleIndex, { preserveOffset: true });
      window.setTimeout(() => {
        delete film.dataset.draggingClick;
      }, 80);
    }
  };
  film.addEventListener("pointerup", finish);
  film.addEventListener("pointercancel", finish);
  film.addEventListener("mouseleave", (event) => {
    if (isDown) finish(event);
  });
  film.addEventListener("wheel", (event) => {
    event.preventDefault();
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    applyScheduleFilmOffset(film, Number(film.dataset.offset || 0) + delta);
    selectNearestAtMarker();
    window.clearTimeout(film._snapTimer);
    film._snapTimer = window.setTimeout(() => {
      const nearest = nearestScheduleFrame(film);
      if (nearest) selectSchedule(nearest.dataset.scheduleIndex, { preserveOffset: true });
    }, 160);
  }, { passive: false });
  window.addEventListener("resize", () => {
    applyScheduleFilmOffset(film, Number(film.dataset.offset || 0), true);
    alignPersonalScheduleToReel();
  });
}

function nearestScheduleFrame(film) {
  const frames = Array.from(film.querySelectorAll(".schedule-frame"));
  if (!frames.length) return null;
  const filmRect = film.getBoundingClientRect();
  const focus = filmRect.left + scheduleFilmMarkerX(film);
  return frames.reduce((nearest, frame) => {
    const rect = frame.getBoundingClientRect();
    const distance = Math.abs(rect.right - focus);
    if (!nearest || distance < nearest.distance) return { frame, distance };
    return nearest;
  }, null)?.frame || frames[0];
}

function scheduleFilmMaxOffset(film) {
  const frames = Array.from(film?.querySelectorAll(".schedule-frame") || []);
  if (!film || !frames.length) return 0;
  const latest = frames[0];
  const earliest = frames[frames.length - 1];
  const frameWidth = latest.offsetWidth || 260;
  const latestAlignOffset = earliest.offsetLeft - latest.offsetLeft;
  const max = latestAlignOffset + frameWidth;
  return Math.max(0, max);
}

function applyScheduleFilmOffset(film, offset, immediate = false) {
  const track = film?.querySelector("[data-schedule-track]");
  if (!film || !track) return;
  const next = Math.max(0, Math.min(scheduleFilmMaxOffset(film), Number(offset) || 0));
  const base = scheduleFilmBaseWidth(film);
  const visibleWidth = base + next;
  const frames = Array.from(film.querySelectorAll(".schedule-frame"));
  const frameWidth = frames[0]?.offsetWidth || 260;
  const earliest = frames[frames.length - 1];
  const earliestEdge = earliest ? earliest.offsetLeft + frameWidth : track.scrollWidth;
  const trackShift = scheduleFilmMarkerX(film) - earliestEdge + next;
  film.dataset.offset = String(next);
  film.dataset.dragOffset = String(next);
  film.style.setProperty("--schedule-film-pull", `${next}px`);
  film.style.setProperty("--schedule-film-drag", "0px");
  film.style.transition = immediate ? "none" : "";
  film.style.width = `${visibleWidth}px`;
  film.classList.toggle("is-extended", next > 8);
  track.style.transition = immediate ? "none" : "";
  track.style.transform = `translate3d(${trackShift}px,0,0)`;
  if (immediate) {
    track.offsetHeight;
    film.style.transition = "";
    track.style.transition = "";
  }
}

function scheduleFilmBaseWidth(film) {
  const raw = getComputedStyle(film).getPropertyValue("--schedule-film-base");
  return parseFloat(raw) || 150;
}

function scheduleFilmMarkerX(film) {
  const raw = getComputedStyle(film).getPropertyValue("--schedule-marker-x");
  return parseFloat(raw) || scheduleFilmBaseWidth(film) / 2;
}

function centerScheduleFrame(film, index, immediate = false) {
  if (!film || !index) return;
  const frame = Array.from(film.querySelectorAll("[data-schedule-index]"))
    .find((item) => item.dataset.scheduleIndex === String(index));
  if (!frame) return;
  const frames = Array.from(film.querySelectorAll(".schedule-frame"));
  const earliest = frames[frames.length - 1];
  const target = earliest ? earliest.offsetLeft - frame.offsetLeft : 0;
  applyScheduleFilmOffset(film, target, immediate);
}

async function loadCompanionAvailability() {
  try {
    const data = await apiGet("/companion/personal-memory?limit=0");
    updatePersonalMemoryAvailability(Boolean(data.available));
  } catch (error) {
    updatePersonalMemoryAvailability(false);
  }
}

function updatePersonalMemoryAvailability(available) {
  state.companionPersonalAvailable = available;
  const view = available ? PERSONAL_MEMORY_VIEW.available : PERSONAL_MEMORY_VIEW.unavailable;
  VIEWS.review.title = view.title;
  VIEWS.review.hint = view.hint;
  const strip = document.querySelector('[data-view="review"]');
  if (strip) {
    strip.dataset.tipTitle = view.title;
    strip.dataset.tipSub = view.hint;
    const label = strip.querySelector(".strip-label b");
    const small = strip.querySelector(".strip-label small");
    if (label) label.textContent = view.title;
    if (small) small.textContent = view.small;
  }
  const head = $("#view-review .section-head");
  if (head) {
    const title = head.querySelector("h3");
    const hint = head.querySelector("p");
    if (title) title.textContent = view.title;
    if (hint) hint.textContent = view.hint;
  }
  if (state.activeView === "review") {
    $("#workspaceTitle").textContent = view.title;
    $("#workspaceHint").textContent = view.hint;
  }
}

function renderPersonalMemoryUnavailable(reason) {
  return `
    <div class="empty-state unavailable-state">
      <b>个人记忆不可用</b>
      <span>${escapeHtml(reason)}</span>
      <p>这里用于联动主动陪伴插件，展示 Bot 自身的每日生活日程、当前状态、日程细化片段和由陪伴插件写入的个人记忆。</p>
    </div>
  `;
}

function renderPersonalMemoryWorkspace(snapshot, status) {
  return `
    <section class="personal-memory-workspace">
      ${renderCompanionSchedulePanel(snapshot, status)}
    </section>
  `;
}

function renderCompanionSchedulePanel(snapshot, status) {
  const plan = snapshot.plan || {};
  const items = Array.isArray(plan.items) ? plan.items : [];
  const visualItems = items.map((item, index) => ({ item, index })).reverse();
  const activeIndex = activeScheduleIndex(items);
  const active = selectedScheduleItem(items, activeIndex);
  return `
    <section class="personal-zone companion-overview">
      <div class="personal-zone-head">
        <h4>时间胶片</h4>
        <span>${escapeHtml(snapshot.bot_name || "Bot")} · ${escapeHtml(plan.date || status.selected_date || "-")}</span>
      </div>
      <div class="schedule-summary" data-schedule-summary>
        ${renderScheduleSummary(active.item, items, active.index)}
      </div>
      <div class="schedule-film" data-schedule-film>
        <div class="schedule-film-track" data-schedule-track>
        ${visualItems.length ? visualItems.map(({ item, index }) => {
          const itemIndex = scheduleIndex(item, index);
          return `
          <button class="schedule-frame${itemIndex === activeIndex ? " is-active" : ""}" data-schedule-index="${escapeHtml(itemIndex)}" type="button">
            <span>${escapeHtml(scheduleRange(items, index))}</span>
          </button>
        `}).join("") : `<div class="empty-state">这一天没有日程。</div>`}
        </div>
        <div class="schedule-marker" aria-hidden="true"></div>
      </div>
    </section>
  `;
}

function updateScheduleSummary(target, snapshot, options = {}) {
  const summary = target.querySelector("[data-schedule-summary]");
  if (!summary) return;
  const plan = snapshot.plan || {};
  const items = Array.isArray(plan.items) ? plan.items : [];
  const active = selectedScheduleItem(items, activeScheduleIndex(items));
  const render = () => {
    summary.innerHTML = renderScheduleSummary(active.item, items, active.index);
  };
  if (options.animate) {
    swapPanelContent(summary, render);
  } else {
    render();
  }
}

function swapPanelContent(element, render) {
  if (!element) return;
  window.clearTimeout(element._swapTimer);
  window.clearTimeout(element._swapDoneTimer);
  element.classList.add("is-switching");
  element._swapTimer = window.setTimeout(() => {
    render();
    element.classList.add("is-switching");
    element.offsetHeight;
    requestAnimationFrame(() => {
      element.classList.remove("is-switching");
      element.classList.add("is-switch-settling");
      element._swapDoneTimer = window.setTimeout(() => {
        element.classList.remove("is-switch-settling");
      }, 220);
    });
  }, 120);
}

function selectedScheduleItem(items, selectedIndex) {
  const index = items.findIndex((item, fallback) => scheduleIndex(item, fallback) === String(selectedIndex));
  return {
    item: index >= 0 ? items[index] : null,
    index,
  };
}

function renderScheduleSummary(item, items, index) {
  if (!item) {
    return `<b>未选择时段</b><span>拖动胶片选择一段日程。</span>`;
  }
  const range = index >= 0 ? scheduleRange(items, index) : (item.time || "");
  const meta = [item.mood, item.message_seed].filter(Boolean).join(" · ");
  return `
    <b>${escapeHtml(range)}</b>
    <span>${escapeHtml(item.activity || "未命名日程")}</span>
    ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
  `;
}

function scheduleIndex(item, fallback) {
  const value = item?.index;
  if (value !== undefined && value !== null && String(value) !== "") return String(value);
  return String(fallback);
}

function activeScheduleIndex(items) {
  if (!items.length) return "";
  const available = items.map((item, index) => scheduleIndex(item, index));
  if (available.includes(String(state.selectedScheduleIndex))) return String(state.selectedScheduleIndex);
  state.selectedScheduleIndex = available[0] || "";
  return state.selectedScheduleIndex;
}

function detailForSchedule(details, item, selectedIndex) {
  if (!item || !selectedIndex) return null;
  return details.find((detail) => String(detail.index) === String(selectedIndex))
    || details.find((detail) => String(detail.key || "").includes(`:${selectedIndex}:`))
    || details.find((detail) => item.time && String(detail.key || "").includes(`:${item.time}`))
    || null;
}

function scheduleRange(items, index) {
  const item = items[index] || {};
  const start = item.time || "--:--";
  const next = items[index + 1]?.time || "";
  return next ? `${start} - ${next}` : `${start} 后`;
}

function showPersonalScheduleDetail(snapshot, status, options = {}) {
  if (state.activeView !== "review") return;
  const plan = snapshot.plan || {};
  const items = Array.isArray(plan.items) ? plan.items : [];
  const details = Array.isArray(snapshot.details) ? snapshot.details : [];
  const selectedIndex = activeScheduleIndex(items);
  const selectedItem = items.find((item, index) => scheduleIndex(item, index) === selectedIndex) || null;
  const selectedDetail = detailForSchedule(details, selectedItem, selectedIndex);
  const drawer = $("#detailDrawer");
  const render = () => {
    drawer.className = selectedItem ? "detail-drawer" : "detail-drawer empty";
    drawer.innerHTML = `<div class="personal-detail-content">${renderSelectedDetail(selectedItem, selectedDetail, items)}</div>`;
  };
  if (options.animate) {
    swapPanelContent(drawer, render);
  } else {
    render();
  }
}

function renderSelectedDetail(item, detail, items) {
  if (!item) {
    return `
      <div class="detail-empty">
        <b>选择日程段</b>
        <span>点击上方日程表里的时间段，在这里查看对应细化。</span>
      </div>
    `;
  }
  const index = items.findIndex((candidate, fallback) => scheduleIndex(candidate, fallback) === String(state.selectedScheduleIndex));
  const range = index >= 0 ? scheduleRange(items, index) : (item.time || "");
  const detailTime = detail?.time ? detail.time : "";
  if (!detail) {
    return `
      <div class="empty-state">这个时间段还没有细化。</div>
    `;
  }
  return `
    <article class="selected-detail">
      ${detailTime ? `<span class="detail-time">${escapeHtml(detailTime)}</span>` : ""}
      ${detail.summary ? `<b class="detail-summary">${escapeHtml(detail.summary)}</b>` : ""}
      ${renderDetailLines(detail)}
    </article>
  `;
}

function renderDetailLines(item) {
  const lines = [
    ...(item.today_events || []),
    ...(item.proactive_events || []),
    ...(item.state_variables || []),
  ].slice(0, 4);
  if (!lines.length) return "";
  return `<ul class="detail-lines">${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`;
}

function applyMicroscopeView() {
  const active = activeSecondaryNav("microscope");
  const box = $("#view-microscope .microscope-box");
  const result = $("#searchResult");
  if (!box || !result) return;
  box.classList.toggle("hidden", active !== "query");
  result.dataset.microscopeSection = active;
  if (active !== "query" && !result.innerHTML.trim()) {
    result.innerHTML = `<div class="empty-state">先在“召回测试”里运行一次检索。</div>`;
  }
}

async function runSearch() {
  const query = $("#searchQuery").value.trim();
  if (!query) {
    $("#searchResult").innerHTML = `<div class="empty-state">先输入一句要测试的话。</div>`;
    return;
  }
  $("#searchResult").innerHTML = loadingState("正在模拟召回...");
  const data = await apiPost("/search", contextPayload(query));
  const results = data.results || [];
  const blocked = data.blocked || [];
  $("#searchResult").innerHTML = `
    <section class="result-section film-panel" data-result-section="hits">
      <div class="personal-zone-head">
        <h4>命中记忆</h4>
        <span>${escapeHtml(results.length)} Hits</span>
      </div>
      ${results.length ? results.map((item) => `
        <article class="search-card" data-memory-id="${escapeHtml(item.id)}">
          <span class="item-title">${escapeHtml(item.content)}</span>
          <div class="item-meta">score ${escapeHtml(item.score)} · ${escapeHtml(item.reason || "")}</div>
          <div class="badges">
            <span class="badge teal">${escapeHtml(item.memory_type)}</span>
            <span class="badge blue">${escapeHtml(item.visibility)}</span>
          </div>
        </article>
      `).join("") : `<div class="empty-state">没有命中可注入记忆。</div>`}
    </section>
    <section class="result-section film-panel" data-result-section="blocked">
      <div class="personal-zone-head">
        <h4>过滤原因</h4>
        <span>${escapeHtml(blocked.length)} Blocked</span>
      </div>
      ${blocked.length ? blocked.map((item) => `
        <article class="search-card">
          <span class="item-title">${escapeHtml(item.memory_id || item.id || "blocked")}</span>
          <div class="item-meta">${escapeHtml(item.reason || JSON.stringify(item))}</div>
        </article>
      `).join("") : `<div class="empty-state">没有过滤记录。</div>`}
    </section>
  `;
  $$("#searchResult [data-memory-id]").forEach((card) => {
    card.addEventListener("click", () => showMemory(card.dataset.memoryId));
  });
  applyMicroscopeView();
}

async function loadArchive() {
  $("#selfMemoryList").innerHTML = loadingState("正在读取配置快照...");
  const config = await apiGet("/context/config");
  $("#selfMemoryList").innerHTML = renderArchiveConfig(config);
  setArchiveSection(activeSecondaryNav("archive"));
}

function setArchiveSection(section) {
  $$("#view-archive [data-archive-section]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.archiveSection === section);
  });
}

function renderArchiveConfig(config) {
  const ctx = config.context_management || {};
  const injection = config.memory_injection || {};
  const orchestration = config.context_orchestration || {};
  const summary = config.memory_summary || {};
  const tools = config.memory_tools || {};
  const visibility = config.visibility || {};
  const maintenance = config.maintenance || {};
  const sleep = config.sleep_maintenance || {};
  const sleepDecay = sleep.decay || {};
  const sleepRaw = sleep.raw_retention || {};
  const historyNote = ctx.manage_astrbot_history_enabled
    ? `保留 ${ctx.keep_recent_messages ?? 0} 条原始历史`
    : "保持 AstrBot 原生上下文";
  const sleepNote = sleep.ran_at
    ? `衰减总结 ${sleepDecay.summaries ?? 0} · 衰减归档 ${sleepDecay.archived ?? 0} · 原始归档 ${sleepRaw.archived ?? 0}`
    : (sleep.message || "记录最近一次维护状态");
  return `
    <div class="config-grid single">
      ${configCard("检索架构", queryModeLabel(orchestration.query_mode), queryModeNote(orchestration.query_mode), queryModeTone(orchestration.query_mode), "Mode")}
      ${configCard("上下文管理", boolLabel(ctx.enabled), ctx.enabled ? `最多 ${ctx.max_events ?? "-"} 条事件，${ctx.max_chars ?? "-"} 字` : "只追加长期记忆，不替换短期上下文", ctx.enabled ? "teal" : "gold", "Context")}
      ${configCard("异步压缩", boolLabel(ctx.async_precompress_enabled), `阈值 ${ctx.precompress_threshold_percent ?? 85}% · 同步兜底 ${boolLabel(ctx.allow_sync_compression)}`, ctx.async_precompress_enabled ? "teal" : "gold", "Speed")}
      ${configCard("低信息保护", boolLabel(ctx.low_information_guard_enabled), `过期 ${ctx.low_information_gap_minutes ?? 20} 分钟 · 长期召回 ${ctx.suppress_memory_on_low_information ? "禁用" : "保留"}`, ctx.low_information_guard_enabled ? "teal" : "gold", "Guard")}
      ${configCard("新话题隔离", boolLabel(ctx.topic_shift_guard_enabled), `比较最近 ${ctx.topic_shift_guard_recent_events ?? 6} 条事件`, ctx.topic_shift_guard_enabled ? "teal" : "gold", "Shift")}
      ${configCard("阶段性总结", boolLabel(summary.enabled), `满 ${summary.trigger_event_count ?? "-"} 条或 ${summary.trigger_interval_minutes ?? "-"} 分钟，重试 ${summary.max_retries ?? 3} 次`, summary.enabled ? "teal" : "red", "Summary")}
      ${configCard("原始历史策略", ctx.manage_astrbot_history_enabled ? (ctx.astrbot_history_mode || "-") : "原生", historyNote, ctx.manage_astrbot_history_enabled ? "blue" : "teal", "AstrBot")}
      ${configCard("溢出处理", ctx.overflow_strategy || "-", ctx.overflow_strategy === "drop" ? "丢弃更早上下文" : "压缩更早上下文", "gold", "Overflow")}
      ${configCard("记忆注入", boolLabel(injection.enabled), `Top ${injection.top_k ?? "-"}，上限 ${injection.max_chars ?? "-"} 字`, injection.enabled ? "violet" : "red", "Memory")}
      ${configCard("注入调试日志", boolLabel(injection.debug_log_injection_enabled), `日志上限 ${injection.debug_log_max_chars ?? 12000} 字 · 数据库日志 ${boolLabel(injection.enable_injection_logs)}`, injection.debug_log_injection_enabled ? "red" : "gold", "Debug")}
      ${configCard("原始事件", boolLabel(injection.include_raw_events), injection.include_raw_events ? "允许对话事件参与召回" : "只注入总结和稳定记忆", injection.include_raw_events ? "teal" : "gold", "Raw")}
      ${configCard("卡片权限", boolLabel(visibility.enable_acl_rules), visibility.enable_acl_rules ? "私聊/群聊卡片可配置跨窗口读取" : "仅使用默认隔离边界", visibility.enable_acl_rules ? "teal" : "gold", "ACL")}
      ${configCard("自然衰减", boolLabel(maintenance.memory_decay_enabled), `满 ${maintenance.memory_decay_after_days ?? 180} 天且闲置 ${maintenance.memory_decay_idle_days ?? 90} 天，候选 ${maintenance.memory_decay_max_candidates ?? 120}`, maintenance.memory_decay_enabled ? "teal" : "gold", "Decay")}
      ${configCard("衰减保护", `重要度 ≤ ${maintenance.memory_decay_max_importance_percent ?? 74}%`, `召回 ≤ ${maintenance.memory_decay_max_access_count ?? 2} 次 · 每摘要 ${maintenance.memory_decay_min_items_per_summary ?? 4}-${maintenance.memory_decay_max_items_per_summary ?? 24} 条`, "blue", "Sleep")}
      ${configCard("原始保留", `${maintenance.retention_raw_event_days ?? 7} 天`, `单次归档上限 ${maintenance.retention_raw_event_limit ?? 1000} 条`, "gold", "Raw")}
      ${configCard("主动工具", boolLabel(tools.enable_recall_tool || tools.enable_remember_tool || tools.enable_note_tools), `回忆 ${boolLabel(tools.enable_recall_tool)} · 记忆 ${boolLabel(tools.enable_remember_tool)} · 笔记 ${boolLabel(tools.enable_note_tools)}`, "teal", "Tools")}
      ${configCard("主动记忆审核", tools.auto_approve_tool_memories ? "自动通过" : "待审核", tools.auto_approve_tool_memories ? "工具写入会直接进入稳定记忆" : "工具写入默认进入审核队列", tools.auto_approve_tool_memories ? "red" : "gold", "Guard")}
      ${configCard("睡眠维护", sleep.ran_at || "未运行", sleep.backup ? `最近备份 ${sleep.backup}` : sleepNote, sleep.ran_at ? "blue" : "gold", "Sleep")}
    </div>
  `;
}

async function openBucketPermissions(bucketId) {
  const bucket = state.buckets.find((item) => item.id === bucketId);
  if (!isWindowBucket(bucket)) {
    showToast("只有私聊/群聊卡片可以配置权限", "error");
    return;
  }
  $("#detailDrawer").className = "detail-drawer";
  $("#detailDrawer").innerHTML = loadingState("正在读取权限...");
  try {
    if (state.activeBucketId !== bucket.id) {
      state.activeBucketId = bucket.id;
      const scope = currentRailScope();
      if (scope) {
        renderScopedBucketRail(scope);
      } else {
        renderBuckets();
      }
      await loadActiveView();
    }
    await showBucketPermissions(bucket.id);
  } catch (error) {
    $("#detailDrawer").innerHTML = panelError(error, "重新读取权限");
    const retry = $("#detailDrawer [data-retry-active]");
    if (retry) retry.addEventListener("click", () => openBucketPermissions(bucketId));
    showToast(error.message || "权限读取失败", "error");
  }
}

async function showBucketPermissions(bucketId) {
  const bucket = state.buckets.find((item) => item.id === bucketId);
  if (!isWindowBucket(bucket)) return;
  const detail = $("#detailDrawer");
  detail.className = "detail-drawer permission-drawer";
  detail.innerHTML = loadingState("正在读取权限...");
  const params = new URLSearchParams({ scope: bucket.scope, id: bucket.target_id });
  const data = await apiGet(`/acl?${params.toString()}`);
  detail.classList.remove("empty");
  detail.innerHTML = renderBucketPermissionPanel(bucket, data);
  bindBucketPermissionPanel(bucket);
}

function renderBucketPermissionPanel(bucket, data) {
  const canRead = data.can_read || [];
  const canBeReadBy = data.can_be_read_by || [];
  const policy = data.policy || {};
  const readMode = normalizeAclMode(policy.read_mode);
  const shareMode = normalizeAclMode(policy.share_mode);
  const targets = permissionTargets(bucket);
  return `
    <h3>${escapeHtml(bucket.label)} · 记忆权限</h3>
    <div class="badges">
      <span class="badge blue">${escapeHtml(windowKindLabel(bucket.scope))}</span>
      <span class="badge teal">${escapeHtml(bucket.target_id)}</span>
      <span class="badge gold">读取 ${escapeHtml(aclModeLabel(readMode))}</span>
      <span class="badge violet">被读 ${escapeHtml(aclModeLabel(shareMode))}</span>
    </div>
    ${bucket.scope === "private" ? `<div class="privacy-note">隐私保护：私聊记忆流向群聊必须显式加入白名单，黑名单默认放行不会自动开放给群聊。</div>` : ""}
    <section class="permission-panel">
      ${renderAclSection("can_read", "当前窗口可读", canRead, targets, readMode)}
      ${renderAclSection("can_be_read_by", "可读取当前窗口", canBeReadBy, targets, shareMode)}
    </section>
  `;
}

function normalizeAclMode(value) {
  return value === "blacklist" ? "blacklist" : "whitelist";
}

function aclModeLabel(mode) {
  return normalizeAclMode(mode) === "blacklist" ? "黑名单" : "白名单";
}

function aclEffectForMode(mode) {
  return normalizeAclMode(mode) === "blacklist" ? "deny" : "allow";
}

function aclPolicyField(sectionMode) {
  return sectionMode === "can_read" ? "read_mode" : "share_mode";
}

function aclSectionNote(sectionMode, listMode) {
  if (sectionMode === "can_read") {
    return listMode === "blacklist" ? "名单内不可读" : "只读名单内";
  }
  return listMode === "blacklist" ? "名单内不可读当前" : "名单内可读当前";
}

function renderAclModeSwitch(sectionMode, listMode) {
  const field = aclPolicyField(sectionMode);
  return `
    <div class="permission-mode" role="group" aria-label="${escapeHtml(aclModeLabel(listMode))}">
      <button class="${listMode === "whitelist" ? "is-active" : ""}" data-acl-policy="${escapeHtml(field)}" data-acl-policy-value="whitelist" type="button">白名单</button>
      <button class="${listMode === "blacklist" ? "is-active" : ""}" data-acl-policy="${escapeHtml(field)}" data-acl-policy-value="blacklist" type="button">黑名单</button>
    </div>
  `;
}

function renderAclSection(mode, title, rules, targets, listMode = "whitelist") {
  const normalizedMode = normalizeAclMode(listMode);
  const effect = aclEffectForMode(normalizedMode);
  const visibleRules = rules.filter((rule) => (rule.effect || "allow") === effect);
  const disabled = targets.length ? "" : " disabled";
  const rows = visibleRules.length ? visibleRules.map((rule) => renderAclRuleRow(rule, mode)).join("") : `
    <div class="empty-state compact">${normalizedMode === "blacklist" ? "暂无阻止项。" : "暂无允许项。"}</div>
  `;
  return `
    <section class="permission-section">
      <div class="personal-zone-head">
        <h4>${escapeHtml(title)}</h4>
        <span>${escapeHtml(aclSectionNote(mode, normalizedMode))}</span>
      </div>
      ${renderAclModeSwitch(mode, normalizedMode)}
      <div class="permission-add">
        <select data-acl-select="${escapeHtml(mode)}"${disabled}>
          ${targets.map((target) => `
            <option value="${escapeHtml(windowOptionValue(target.scope, target.target_id))}">
              ${escapeHtml(windowKindLabel(target.scope))} · ${escapeHtml(target.label)}
            </option>
          `).join("")}
        </select>
        <button data-acl-add="${escapeHtml(mode)}" data-acl-effect="${escapeHtml(effect)}" type="button"${disabled}>${normalizedMode === "blacklist" ? "加入黑名单" : "加入白名单"}</button>
      </div>
      <div class="permission-list">${rows}</div>
    </section>
  `;
}

function renderAclRuleRow(rule, mode) {
  const scope = mode === "can_read" ? rule.owner_scope : rule.reader_scope;
  const id = mode === "can_read" ? rule.owner_id : rule.reader_id;
  return `
    <article class="permission-row">
      <div>
        <b>${escapeHtml(windowLabel(scope, id))}</b>
        <small>${escapeHtml(windowKindLabel(scope))} · ${escapeHtml(id)}</small>
      </div>
      <button class="ghost mini" data-acl-delete="${escapeHtml(rule.id)}" type="button">移除</button>
    </article>
  `;
}

function bindBucketPermissionPanel(bucket) {
  $$("#detailDrawer [data-acl-add]").forEach((button) => {
    button.addEventListener("click", () => addBucketAclRule(bucket, button.dataset.aclAdd, button));
  });
  $$("#detailDrawer [data-acl-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteBucketAclRule(bucket, button.dataset.aclDelete, button));
  });
  $$("#detailDrawer [data-acl-policy]").forEach((button) => {
    button.addEventListener("click", () => updateBucketAclPolicy(bucket, button.dataset.aclPolicy, button.dataset.aclPolicyValue, button));
  });
}

async function addBucketAclRule(bucket, mode, button) {
  const select = $(`#detailDrawer [data-acl-select="${mode}"]`);
  const target = parseWindowOption(select?.value || "");
  if (!target.scope || !target.id) {
    showToast("没有可添加的目标窗口", "error");
    return;
  }
  const current = { scope: bucket.scope, id: bucket.target_id };
  const payload = mode === "can_read"
    ? { owner_scope: target.scope, owner_id: target.id, reader_scope: current.scope, reader_id: current.id }
    : { owner_scope: current.scope, owner_id: current.id, reader_scope: target.scope, reader_id: target.id };
  await withButton(button, "保存中", async () => {
    await apiPost("/acl/upsert", { ...payload, effect: button.dataset.aclEffect || "allow", enabled: true });
    showToast(button.dataset.aclEffect === "deny" ? "黑名单已更新" : "白名单已更新");
    await showBucketPermissions(bucket.id);
  });
}

async function updateBucketAclPolicy(bucket, field, value, button) {
  const payload = { scope: bucket.scope, id: bucket.target_id };
  payload[field] = value;
  await withButton(button, "切换中", async () => {
    await apiPost("/acl/policy", payload);
    showToast("名单模式已更新");
    await showBucketPermissions(bucket.id);
  });
}

async function deleteBucketAclRule(bucket, ruleId, button) {
  await withButton(button, "移除中", async () => {
    await apiPost("/acl/delete", { id: ruleId });
    showToast("权限已移除");
    await showBucketPermissions(bucket.id);
  });
}

async function showMemory(id) {
  state.activeMemoryId = id;
  $("#detailDrawer").className = "detail-drawer";
  $("#detailDrawer").innerHTML = loadingState("正在展开详情...");
  let memory;
  try {
    const data = await apiGet(`/memory?id=${encodeURIComponent(id)}`);
    memory = data.memory;
  } catch (error) {
    $("#detailDrawer").innerHTML = panelError(error, "重新读取");
    const retry = $("#detailDrawer [data-retry-active]");
    if (retry) retry.addEventListener("click", () => showMemory(id));
    showToast(error.message || "详情读取失败", "error");
    return;
  }
  $("#detailDrawer").classList.remove("empty");
  $("#detailDrawer").innerHTML = `
    <div class="memory-manage-head">
      <div>
        <h3>${escapeHtml(memory.memory_type)}</h3>
        <p class="item-meta">${escapeHtml(memory.id)} · ${escapeHtml(formatTime(memory.occurred_at || memory.created_at))}</p>
      </div>
      <div class="badges">
        <span class="badge teal">${escapeHtml(memory.visibility)}</span>
        <span class="badge blue">${escapeHtml(memory.reality_level)}</span>
        <span class="badge gold">${escapeHtml(memory.lifecycle)}</span>
        <span class="badge ${memory.review_status === "pending" ? "red" : "violet"}">${escapeHtml(memory.review_status)}</span>
      </div>
    </div>
    ${memory.source_plugin === "livingmemory" && isNumericOnlyContent(memory.content) ? `<div class="empty-state error-state"><b>导入内容未修复</b><span>这条记录目前只有旧库编号，请先在维护工具中执行“修复 LivingMemory 内容”。</span></div>` : ""}
    <form id="memoryManageForm" class="memory-manage-form" autocomplete="off">
      <label>
        <span>记忆类型</span>
        <input name="memory_type" type="text" value="${escapeHtml(memory.memory_type || "")}" />
      </label>
      <label>
        <span>内容</span>
        <textarea name="content" rows="6">${escapeHtml(memory.content || "")}</textarea>
      </label>
      <label>
        <span>证据</span>
        <textarea name="evidence" rows="4">${escapeHtml(memory.evidence || "")}</textarea>
      </label>
      <div class="memory-manage-grid">
        <label>
          <span>可见性</span>
          <select name="visibility">
            ${memoryOption("private_pair", "私聊可见", memory.visibility)}
            ${memoryOption("group_public", "群聊可见", memory.visibility)}
            ${memoryOption("bot_self", "自我档案", memory.visibility)}
            ${memoryOption("internal", "内部", memory.visibility)}
            ${memoryOption("shareable", "可共享", memory.visibility)}
          </select>
        </label>
        <label>
          <span>生命周期</span>
          <select name="lifecycle">
            ${memoryOption("stable_memory", "稳定记忆", memory.lifecycle)}
            ${memoryOption("raw_event", "原始事件", memory.lifecycle)}
            ${memoryOption("archived", "归档", memory.lifecycle)}
          </select>
        </label>
        <label>
          <span>重要度</span>
          <input name="importance" type="number" min="0" max="1" step="0.01" value="${escapeHtml(memory.importance ?? 0.3)}" />
        </label>
        <label>
          <span>置信度</span>
          <input name="confidence" type="number" min="0" max="1" step="0.01" value="${escapeHtml(memory.confidence ?? 0.5)}" />
        </label>
      </div>
      <div class="memory-manage-actions">
        <button id="saveMemoryBtn" type="submit">保存这条记忆</button>
        <button class="approve" data-review-status="auto" type="button">通过审核</button>
        <button class="reject" data-review-status="rejected" type="button">拒绝并归档</button>
        <button class="danger" data-delete="1" type="button">删除</button>
      </div>
    </form>
    <details class="memory-raw-detail">
      <summary>归属与元数据</summary>
      <h4>归属</h4>
      <pre>${escapeHtml(JSON.stringify({ subject: memory.subject, object: memory.object, scope: memory.scope, session_id: memory.session_id, group_id: memory.group_id }, null, 2))}</pre>
      <h4>元数据</h4>
      <pre>${escapeHtml(JSON.stringify(memory.metadata || {}, null, 2))}</pre>
    </details>
  `;
  const form = $("#memoryManageForm");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    withButton($("#saveMemoryBtn"), "保存中", () => saveMemoryManagement(id, form));
  });
  $$("#detailDrawer [data-review-status]").forEach((button) => {
    button.addEventListener("click", async () => {
      await withButton(button, "保存中", async () => {
        await apiPost("/review/update", { id, status: button.dataset.reviewStatus });
        showToast(button.dataset.reviewStatus === "rejected" ? "已拒绝并归档" : "已通过审核");
        await refreshAll();
        await showMemory(id);
      });
    });
  });
  $("#detailDrawer [data-delete]").addEventListener("click", async () => {
    if (!confirm("确认删除这条记忆？")) return;
    await apiPost("/memory/delete", { id });
    clearDetail();
    await refreshAll();
  });
}

function memoryOption(value, label, current) {
  return `<option value="${escapeHtml(value)}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`;
}

async function saveMemoryManagement(id, form) {
  const data = new FormData(form);
  const num = (name, fallback) => {
    const value = Number(data.get(name));
    return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : fallback;
  };
  await apiPost("/memory/update", {
    id,
    memory_type: String(data.get("memory_type") || ""),
    content: String(data.get("content") || ""),
    evidence: String(data.get("evidence") || ""),
    importance: num("importance", 0.3),
    confidence: num("confidence", 0.5),
  });
  await apiPost("/memory/visibility", {
    id,
    visibility: String(data.get("visibility") || "internal"),
  });
  await apiPost("/memory/lifecycle", {
    id,
    lifecycle: String(data.get("lifecycle") || "stable_memory"),
  });
  showToast("这条记忆已保存");
  await refreshAll();
  await showMemory(id);
}

function showGenericDetail(title, payload) {
  $("#detailDrawer").classList.remove("empty");
  $("#detailDrawer").innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <pre>${escapeHtml(JSON.stringify(payload || {}, null, 2))}</pre>
  `;
}

function clearDetail() {
  $("#detailDrawer").className = "detail-drawer empty";
  $("#detailDrawer").innerHTML = `
    <div class="detail-empty">
      <b>等待选片</b>
      <span>选择左侧二级导航，再点一条记忆或记录查看详情。</span>
    </div>
  `;
}

async function runMaintenance() {
  const data = await apiPost("/maintenance");
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.result, null, 2))}</pre>`;
  await refreshAll();
  showToast("维护已完成");
}

async function approveLivingMemoryImports() {
  if (!confirm("确认批量通过所有 LivingMemory 导入的待审核记忆？这不会处理其它来源的待审核内容。")) return;
  const data = await apiPost("/review/approve_livingmemory");
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.result, null, 2))}</pre>`;
  await refreshAll();
  showToast(`已通过 ${data.result?.updated || 0} 条 LivingMemory 导入记忆`);
}

async function repairLivingMemoryContent() {
  const path = $("#livingmemoryPath").value.trim();
  const data = await apiPost("/maintenance/repair_livingmemory_content", { path });
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.result, null, 2))}</pre>`;
  await refreshAll();
  showToast(`已修复 ${data.result?.updated || 0} 条 LivingMemory 内容`);
}

async function clearAllMemoryData() {
  const box = $("#importResult");
  const warning = "这会清空全部记忆、权限规则、审核队列、关系、时间线、身份、注入日志和导入批次。执行前会自动备份数据库。";
  if (!box) return;
  box.innerHTML = `
    <div class="clear-confirm">
      <b>确认清空全部记忆</b>
      <p>${escapeHtml(warning)}</p>
      <input id="clearAllConfirmText" type="text" placeholder="输入 清空 后执行" autocomplete="off" />
      <div class="inline-actions">
        <button id="executeClearAllMemoryBtn" class="danger" type="button" disabled>执行清空</button>
        <button id="cancelClearAllMemoryBtn" type="button">取消</button>
      </div>
    </div>
  `;
  const input = $("#clearAllConfirmText");
  const execute = $("#executeClearAllMemoryBtn");
  const cancel = $("#cancelClearAllMemoryBtn");
  const update = () => {
    execute.disabled = input.value.trim() !== "清空";
  };
  input.addEventListener("input", update);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !execute.disabled) {
      withBusy("正在清空全部记忆...", executeClearAllMemoryData);
    }
  });
  execute.addEventListener("click", () => withBusy("正在清空全部记忆...", executeClearAllMemoryData));
  cancel.addEventListener("click", () => {
    box.innerHTML = "";
    showToast("已取消清空操作");
  });
  input.focus();
  showToast("请在下方输入“清空”确认");
}

async function executeClearAllMemoryData() {
  const confirmText = $("#clearAllConfirmText")?.value.trim();
  if (confirmText !== "清空") {
    showToast("请输入“清空”后再执行", "error");
    return;
  }
  const data = await apiPost("/maintenance/clear_all", { confirm: "清空" });
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.result, null, 2))}</pre>`;
  state.activeBucketId = "all";
  clearDetail();
  await refreshAll();
  showToast("全部记忆已清空");
}

async function previewImport() {
  const path = $("#livingmemoryPath").value.trim();
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const data = await apiGet(`/import/livingmemory/preview?${params.toString()}`);
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.report, null, 2))}</pre>`;
  showToast("预览已生成");
}

async function runImport() {
  const path = $("#livingmemoryPath").value.trim();
  if (!confirm("确认开始导入？导入内容默认会按保守策略处理。")) return;
  const data = await apiPost("/import/livingmemory/run", { path });
  $("#importResult").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.result, null, 2))}</pre>`;
  await refreshAll();
  showToast("导入已完成");
}

async function refreshAll() {
  await loadCompanionAvailability();
  await loadStats();
  await loadBuckets();
  await loadActiveView();
}

function bindActions() {
  const stage = document.querySelector(".projection-stage");
  $$(".filmstrip").forEach((strip) => {
    const style = strip.getAttribute("style") || "";
    const ang = parseFloat((style.match(/--a:\s*(-?[\d.]+)deg/) || [0,0])[1]);
    const off = parseFloat((style.match(/--off:\s*(-?[\d.]+)px/) || [0,0])[1]);
    const initialAxis = parseFloat((style.match(/--tx:\s*(-?[\d.]+)px/) || [0,0])[1]);
    const a   = ang * Math.PI / 180;
    const label = strip.querySelector(".strip-label");
    let baseAxisOffset = 0;
    let labelBaseShift = 0;
    const measureStrip = () => {
      const r = stage.getBoundingClientRect();
      const s = strip.getBoundingClientRect();
      const cx = s.left + s.width / 2 - r.left - r.width / 2;
      const cy = s.top + s.height / 2 - r.top - r.height / 2;
      baseAxisOffset = cx * Math.cos(a) + cy * Math.sin(a);
      const labelCenter = label ? label.offsetLeft + label.offsetWidth / 2 : strip.clientWidth / 2;
      labelBaseShift = strip.clientWidth / 2 - labelCenter;
    };
    measureStrip();
    const setStripTransform = (offset, axis = initialAxis, duration = ".86s") => {
      strip.style.transition = `transform ${duration} cubic-bezier(.16,.72,.18,1)`;
      strip.style.transform = `rotate(${ang}deg) translateY(${offset}px) translateX(${axis}px)`;
    };
    strip.addEventListener("mouseenter", measureStrip);
    strip.addEventListener("mousemove", (e) => {
      const r = stage.getBoundingClientRect();
      const dx = e.clientX - r.left - r.width/2;
      const dy = e.clientY - r.top  - r.height/2;
      // 仅沿胶卷轴向移动，并把胶卷标签带到鼠标投影位置。
      const raw = dx * Math.cos(a) + dy * Math.sin(a) - baseAxisOffset;
      const axis = initialAxis + raw + labelBaseShift;
      setStripTransform(off, axis, ".86s");
    });
    strip.addEventListener("mouseleave", () => {
      strip.style.transition = "transform .95s cubic-bezier(.18,.88,.22,1)";
      strip.style.transform  = `rotate(${ang}deg) translateY(${off}px) translateX(${initialAxis}px)`;
    });
    strip.addEventListener("click", () => openView(strip.dataset.view));
  });
  $("#backHomeBtn").addEventListener("click", returnHome);
  $("#refreshBtn").addEventListener("click", () => withBusy("正在刷新放映馆...", refreshAll));
  $("#loadActiveBtn").addEventListener("click", () => withBusy("正在重载当前帧...", loadActiveView));
  $("#clearTargetBtn").addEventListener("click", () => {
    if (state.activeView === "review") {
      selectPersonalDate(todayKey());
    } else if (currentRailScope()) {
      selectBucket("all");
    } else if (secondaryNavItems(state.activeView).length) {
      selectSecondaryNav(defaultSecondaryNav(state.activeView));
    } else {
      selectBucket("all");
    }
  });
  $("#runSearchBtn").addEventListener("click", (event) => withButton(event.currentTarget, "检索中", runSearch));
  $("#maintenanceBtn").addEventListener("click", () => withBusy("正在运行维护...", runMaintenance));
  $("#repairLivingMemoryBtn").addEventListener("click", () => withBusy("正在修复 LivingMemory 内容...", repairLivingMemoryContent));
  $("#approveLivingMemoryBtn").addEventListener("click", () => withBusy("正在批量通过 LivingMemory 导入...", approveLivingMemoryImports));
  $("#clearAllMemoryBtn").addEventListener("click", clearAllMemoryData);
  $("#previewImportBtn").addEventListener("click", () => withBusy("正在扫描 LivingMemory...", previewImport));
  $("#runImportBtn").addEventListener("click", () => withBusy("正在导入 LivingMemory...", runImport));
  $("#globalSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadActiveView();
  });
  $("#bucketList").addEventListener("scroll", requestRailCoverflow, { passive: true });
  $("#bucketList").addEventListener("pointermove", requestRailCoverflow, { passive: true });
  window.addEventListener("resize", requestRailCoverflow);
}

async function init() {
  bindActions();
  await loadConfiguredTheme();
  try {
    await loadCompanionAvailability();
    await loadStats();
    await loadBuckets();
    renderBuckets();
  } catch (error) {
    setMessage(`页面 API 暂不可用：${error.message}`);
    state.buckets = normalizeBuckets([]);
    renderBuckets();
  }
}

init();
