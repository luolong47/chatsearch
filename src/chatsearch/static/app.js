(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const q = $("q");
  const resultsEl = $("results");
  const resultList = $("result-list");
  const emptyEl = $("empty");
  const hintEl = $("result-hint");
  const statusEl = $("index-status");
  const drawer = $("drawer");
  const drawerMask = $("drawer-mask");

  let currentSource = "all";
  let currentArchived = "all";   // all / active / archived
  let currentCwd = "";           // 空 = 全部工作目录
  let debounceTimer = null;
  let lastQuery = "";
  let allCwds = [];        // /api/filters 的全部工作区（浏览态侧边栏用）
  let lastAllHits = null;  // 最近一次搜索的全量 hits（侧边栏聚合 + 点击工作区时前端过滤用）
  let navCleanup = null;   // 消息导航条的滚动监听清理函数
  let selectMode = false;  // 批量选择模式
  const selected = new Set();  // 已选会话 key
  const busyKeys = new Set();  // 进行中的会话 key（不可归档 / 不可同步）

  const SOURCE_LABEL = { workbuddy: "WorkBuddy", dsh: "DeepSeek Harness" };

  // ---------- 工具 ----------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // 与后端 _query_terms 保持一致：空格 + 中英文/数字边界都切分
  function queryTerms(query) {
    const out = [];
    for (const chunk of (query || "").trim().split(/\s+/)) {
      const parts = chunk.match(/[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+|[A-Za-z0-9_]+/g) || [];
      if (!parts.length) continue;
      if (parts.length === 1 && parts[0].length === 1) out.push(parts[0]);
      else out.push(...parts.filter((p) => p.length >= 2));
    }
    return [...new Set(out.map((w) => w.toLowerCase()))];
  }

  // 高亮词精选：搜索拆词求召回，但高亮求精准。
  // 只取最长的少数几个词——长词更有区分度，避免长查询被拆成十几个词后满屏黄底。
  const MAX_HIGHLIGHT_WORDS = 3;
  const MAX_MARKS_PER_BLOCK = 10;
  function highlightTerms(query) {
    const terms = queryTerms(query);
    if (!terms.length) return [];
    return [...terms].sort((a, b) => b.length - a.length).slice(0, MAX_HIGHLIGHT_WORDS);
  }
  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlight(text, query) {
    const safe = escapeHtml(text);
    const words = highlightTerms(query);
    if (!words.length) return safe;
    try {
      const re = new RegExp(`(${words.map(escapeRe).join("|")})`, "gi");
      let n = 0;
      return safe.replace(re, (m) => (++n <= MAX_MARKS_PER_BLOCK ? `<mark>${m}</mark>` : m));
    } catch {
      return safe;
    }
  }

  // ---------- Markdown 渲染 ----------
  if (window.marked) {
    marked.setOptions({ gfm: true, breaks: true });
  }

  // 把 markdown 渲染成 HTML，再在文本节点上高亮关键词。
  // 直接在 HTML 字符串上做替换会破坏标签/代码块，故用 DOM TreeWalker。
  function renderMd(text, query) {
    const src = String(text == null ? "" : text);
    if (!window.marked) return highlight(src, query);
    const html = marked.parse(src);
    const tpl = document.createElement("template");
    tpl.innerHTML = html;

    // 代码语法高亮必须在关键词高亮之前：hljs 会重写 <code> 内部 DOM，
    // 若先插 <mark> 会被覆盖。github-markdown-dark + github-dark 主题配套。
    if (window.hljs) {
      tpl.content.querySelectorAll("pre code").forEach((el) => {
        try { hljs.highlightElement(el); } catch (_) { /* 语言不支持时保留原文 */ }
      });
    }

    const words = highlightTerms(query);
    if (words.length) {
      const re = new RegExp(`(${words.map(escapeRe).join("|")})`, "gi");
      const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      let marked = 0;  // 整条消息高亮总数上限，防止长文里同一词刷屏
      for (const node of nodes) {
        if (!node.nodeValue || marked >= MAX_MARKS_PER_BLOCK) continue;
        const frag = document.createDocumentFragment();
        let last = 0;
        let m;
        re.lastIndex = 0;
        while ((m = re.exec(node.nodeValue)) !== null) {
          if (m.index > last) frag.appendChild(document.createTextNode(node.nodeValue.slice(last, m.index)));
          if (marked >= MAX_MARKS_PER_BLOCK) {
            frag.appendChild(document.createTextNode(node.nodeValue.slice(m.index)));
            last = node.nodeValue.length;
            break;
          }
          const mark = document.createElement("mark");
          mark.textContent = m[0];
          frag.appendChild(mark);
          marked++;
          last = m.index + m[0].length;
        }
        if (last < node.nodeValue.length) frag.appendChild(document.createTextNode(node.nodeValue.slice(last)));
        if (last > 0) node.parentNode.replaceChild(frag, node);
      }
    }
    // marked 在块级元素间插入纯换行文本节点；若容器是 pre-wrap，
    // 这些 "\n" 会被渲染成可见空行，短消息气泡因此撑出大片空白。清掉它们。
    for (const node of Array.from(tpl.content.childNodes)) {
      if (node.nodeType === Node.TEXT_NODE && !node.nodeValue.trim()) {
        node.parentNode.removeChild(node);
      }
    }
    return tpl.innerHTML;
  }

  function fmtTime(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  async function api(url, options) {
    const r = await fetch(url, options);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || data.message || `HTTP ${r.status}`);
    return data;
  }

  // ---------- 索引状态 ----------
  async function loadStatus() {
    try {
      const s = await api("/api/status");
      if (!s.exists) {
        statusEl.textContent = "索引未构建 · 正在准备…";
      } else {
        const wm = s.watcher && s.watcher.mode;
        // 实时监听/定时检查模式下会自动同步，"待更新"只在无监听时提示
        const autoSync = wm === "watchdog" || wm === "poll";
        const watchTag = wm === "watchdog" ? " · 🟢 实时监听"
          : wm === "poll" ? " · 🟡 定时检查" : "";
        const staleTag = (s.stale && !autoSync) ? " · 待更新" : "";
        statusEl.textContent = `${s.sessions} 会话 · ${s.messages} 消息`
          + watchTag + staleTag;
      }
    } catch {
      statusEl.textContent = "索引状态未知";
    }
  }

  // ---------- 筛选选项 ----------
  async function loadFilters() {
    try {
      // 工作区列表跟随来源/归档筛选：选 DSH 时只联想、展示 DSH 的工作区
      let url = "/api/filters";
      const params = [];
      if (currentSource !== "all") params.push(`source=${encodeURIComponent(currentSource)}`);
      if (currentArchived !== "all") params.push(`archived=${encodeURIComponent(currentArchived)}`);
      if (params.length) url += `?${params.join("&")}`;
      const data = await api(url);
      allCwds = data.cwds || [];
      // 当前选中的工作区若不在新范围内，重置为「全部」，避免侧栏无高亮且结果为空
      if (currentCwd && !allCwds.some((c) => c.cwd === currentCwd)) {
        currentCwd = "";
        $("cwd-filter").value = "";
      }
      const dl = $("cwd-options");
      dl.innerHTML = allCwds.map((c) => {
        const label = `${c.cwd}（${c.total} 会话${c.archived ? ` · ${c.archived} 归档` : ""}）`;
        return `<option value="${escapeHtml(c.cwd)}" label="${escapeHtml(label)}"></option>`;
      }).join("");
      // 标题提示归档统计
      const chipAll = document.querySelector('[data-archived="all"]');
      if (chipAll && data.archived_counts) {
        chipAll.title = `全部 ${data.archived_counts.all} · 未归档 ${data.archived_counts.active} · 已归档 ${data.archived_counts.archived}`;
      }
      // 浏览态（未搜索）侧边栏展示全部工作区
      if (!q.value.trim()) renderSidebar(allCwds.map((c) => ({ cwd: c.cwd, count: c.total })));
    } catch {
      /* 筛选选项加载失败不影响搜索 */
    }
  }

  // 侧边栏工作区列表。items: [{cwd, count}]，按 count 降序；点选联动 currentCwd。
  function renderSidebar(items) {
    const list = $("workspace-list");
    $("ws-count").textContent = items.length ? `${items.length} 个` : "";
    const allActive = !currentCwd ? " active" : "";
    const rows = items.map((it) => {
      const active = currentCwd === it.cwd ? " active" : "";
      return `<button class="ws-item${active}" data-cwd="${escapeHtml(it.cwd)}" title="${escapeHtml(it.cwd)}">
        <span class="ws-name">${escapeHtml(cwdLabel(it.cwd))}</span>
        <span class="ws-num">${it.count}</span>
      </button>`;
    }).join("");
    list.innerHTML =
      `<button class="ws-item ws-all${allActive}" data-cwd="">
        <span class="ws-name">全部工作区</span>
        <span class="ws-num">${items.reduce((a, b) => a + b.count, 0)}</span>
      </button>` + rows;
  }

  // 工作目录展示名：Windows 路径取末段，slug 去掉 c- 前缀，过长截断交给 CSS
  function cwdLabel(cwd) {
    if (!cwd) return "（无工作目录）";
    let s = String(cwd).replace(/\\/g, "/");
    const seg = s.split("/").filter(Boolean);
    s = seg.length ? seg[seg.length - 1] : s;
    s = s.replace(/^c-(prjs|Users-LuoLong|ctf|Users)[-_]/i, "");
    return s || cwd;
  }

  // ---------- 搜索 ----------
  async function doSearch() {
    const query = q.value.trim();
    lastQuery = query;
    if (!query) { browse(); return; }
    emptyEl.classList.add("hidden");
    resultsEl.classList.remove("hidden");
    resultList.innerHTML = '<div class="loading">搜索中…</div>';
    hintEl.textContent = "";

    try {
      // 请求全量（不带 cwd），侧边栏才能展示所有命中工作区；cwd 在前端过滤
      let url = `/api/search?q=${encodeURIComponent(query)}`;
      if (currentSource !== "all") url += `&source=${encodeURIComponent(currentSource)}`;
      if (currentArchived !== "all") url += `&archived=${encodeURIComponent(currentArchived)}`;
      const data = await api(url);
      lastAllHits = data.hits;

      // 左侧工作区按命中结果筛选（count = 命中会话数）
      const cwdCount = new Map();
      for (const h of data.hits) cwdCount.set(h.cwd || "", (cwdCount.get(h.cwd || "") || 0) + 1);
      const sidebarItems = [...cwdCount.entries()]
        .map(([cwd, count]) => ({ cwd, count }))
        .sort((a, b) => b.count - a.count);
      renderSidebar(sidebarItems);

      // 右侧：按当前选中的工作区前端过滤
      const shown = currentCwd
        ? data.hits.filter((h) => (h.cwd || "") === currentCwd)
        : data.hits;
      const parts = [currentCwd ? `${shown.length} 个会话命中` : `找到 ${data.total_sessions} 个会话`];
      if (currentCwd) parts.push(`目录 ${cwdLabel(currentCwd)}`);
      if (currentArchived !== "all") parts.push(currentArchived === "archived" ? "仅已归档" : "仅未归档");
      hintEl.textContent = parts.join(" · ");
      if (!shown.length) {
        resultList.innerHTML = `<div class="no-result"><div class="big">⌕</div>${currentCwd ? "该工作区没有命中" : "没有找到包含"}「${escapeHtml(query)}」${currentCwd ? "" : "的会话或消息"}</div>`;
        pruneSelection();
        return;
      }
      resultList.innerHTML = shown.map(renderCard).join("");
      pruneSelection();
    } catch (e) {
      resultList.innerHTML = `<div class="no-result">搜索失败：${escapeHtml(e.message)}</div>`;
    }
  }

  // 浏览态：未搜索时按选中工作区列出全部会话；未选工作区则显示引导空状态
  async function browse() {
    lastAllHits = null;
    resultsEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    renderSidebar(allCwds.map((c) => ({ cwd: c.cwd, count: c.total })));
    hintEl.textContent = "";
    if (!currentCwd) return;

    emptyEl.classList.add("hidden");
    resultsEl.classList.remove("hidden");
    resultList.innerHTML = '<div class="loading">加载会话…</div>';
    try {
      let url = `/api/sessions?cwd=${encodeURIComponent(currentCwd)}`;
      if (currentSource !== "all") url += `&source=${encodeURIComponent(currentSource)}`;
      if (currentArchived !== "all") url += `&archived=${encodeURIComponent(currentArchived)}`;
      const data = await api(url);
      hintEl.textContent = `${cwdLabel(currentCwd)} · ${data.sessions.length} 个会话`;
      if (!data.sessions.length) {
        resultList.innerHTML = '<div class="no-result"><div class="big">⌕</div>该工作区暂无会话</div>';
        pruneSelection();
        return;
      }
      resultList.innerHTML = data.sessions.map(renderBrowseCard).join("");
      pruneSelection();
    } catch (e) {
      resultList.innerHTML = `<div class="no-result">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- 批量选择 ----------
  // 进行中的会话（后端判定：App 正在写入）不允许归档 / 同步，卡片上禁用勾选。
  function cardClass(key, inProgress) {
    return "result-card"
      + (selected.has(key) ? " selected" : "")
      + (inProgress ? " in-progress" : "");
  }

  function checkHtml(key, inProgress) {
    if (!selectMode) return "";
    const on = selected.has(key);
    const cls = "rc-check" + (inProgress ? " disabled" : "") + (on ? " on" : "");
    const tip = inProgress ? "会话进行中，不能归档或同步" : (on ? "取消选择" : "选择该会话");
    return `<span class="${cls}" data-check="${escapeHtml(key)}" title="${tip}">${on ? "✓" : ""}</span>`;
  }

  function busyBadge(inProgress) {
    return inProgress
      ? '<span class="busy-badge" title="会话仍在进行中：不可归档、不可同步">进行中</span>'
      : "";
  }

  function refreshList() {
    if (q.value.trim()) doSearch();
    else browse();
  }

  function updateBatchBar() {
    if (!selectMode) return;
    const countEl = $("batch-count");
    if (countEl) countEl.textContent = `已选 ${selected.size} 条`;
    const cards = Array.from(document.querySelectorAll("#result-list .result-card"));
    const pickable = cards.filter((c) => !c.classList.contains("in-progress"));
    const none = selected.size === 0;
    document.querySelectorAll("#batch-bar [data-batch]").forEach((b) => {
      const act = b.dataset.batch;
      if (act === "archive" || act === "unarchive" || act === "migrate") b.disabled = none;
    });
    const selAll = document.querySelector('#batch-bar [data-batch="select-all"]');
    if (selAll) {
      selAll.disabled = pickable.length === 0;
      selAll.textContent = (pickable.length && selected.size >= pickable.length) ? "取消全选" : "全选";
    }
  }

  function setSelectMode(on) {
    selectMode = on;
    if (!on) selected.clear();
    $("select-toggle").classList.toggle("active", on);
    $("batch-bar").classList.toggle("hidden", !on);
    document.body.classList.toggle("select-mode", on);
    refreshList();
    updateBatchBar();
  }

  // 只保留当前列表里还看得见的选中项，避免切换筛选后"已选 N 条"里混着不可见的 key
  function pruneSelection() {
    const visible = new Set(
      Array.from(document.querySelectorAll("#result-list .result-card")).map((c) => c.dataset.key)
    );
    let changed = false;
    for (const k of Array.from(selected)) {
      if (!visible.has(k)) { selected.delete(k); changed = true; }
    }
    if (changed) updateBatchBar();
  }

  function toggleSelect(card) {
    const key = card.dataset.key;
    if (card.classList.contains("in-progress")) {
      toast("会话进行中，不能归档或同步");
      return;
    }
    if (selected.has(key)) selected.delete(key);
    else selected.add(key);
    const on = selected.has(key);
    card.classList.toggle("selected", on);
    const box = card.querySelector(".rc-check");
    if (box) {
      box.textContent = on ? "✓" : "";
      box.classList.toggle("on", on);
      box.title = on ? "取消选择" : "选择该会话";
    }
    updateBatchBar();
  }

  async function runBatch(action, btn) {
    if (!selected.size) return;
    const keys = Array.from(selected);
    const n = keys.length;
    const tips = {
      archive: `归档 ${n} 条会话？\n\n会写入 WorkBuddy 数据库或 DSH 配置（写前自动备份）。\n进行中的会话会被自动跳过。`,
      unarchive: `取消归档 ${n} 条会话？\n\n会写回原生 App（写前自动备份）。\nDSH 侧需要重启应用才能看到变化。`,
      migrate: `把 ${n} 条会话同步到另一端？\n\nWorkBuddy → DSH，DSH → WorkBuddy。只搬文本对话（提问 + 回复），\n不含工具调用、推理和附件；原会话保持不变，目标端生成新会话。\n进行中的会话会被自动跳过。`,
    };
    if (!confirm(tips[action] || "确定执行？")) return;

    const raw = btn ? btn.textContent : "";
    if (btn) { btn.textContent = "处理中…"; btn.disabled = true; }
    document.querySelectorAll("#batch-bar [data-batch]").forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, keys }),
      });
      toast(r.message || "已完成", 7000);
      if (r.changed && r.changed.length) selected.clear();
      await loadStatus();
      await loadFilters();
      refreshList();
    } catch (err) {
      toast(`操作失败：${err.message}`, 7000);
    } finally {
      if (btn) btn.textContent = raw;
      updateBatchBar();
    }
  }

  // 浏览态会话卡片（无命中高亮，展示首条用户消息作摘要）
  function renderBrowseCard(s) {
    const badge = `<span class="src-badge badge-${s.source}">${SOURCE_LABEL[s.source] || s.source}</span>`;
    const arcBadge = s.archived ? '<span class="arc-badge">已归档</span>' : "";
    return `
      <div class="${cardClass(s.key, s.in_progress)}" data-key="${escapeHtml(s.key)}">
        <div class="rc-head">
          ${checkHtml(s.key, s.in_progress)}
          ${badge}${arcBadge}${busyBadge(s.in_progress)}
          <span class="rc-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
        </div>
        <div class="rc-foot">
          <span>◷ ${fmtTime(s.updated_at)}</span>
          <span>💬 ${s.msg_count} 条消息</span>
          <span>↗ 点击查看完整会话</span>
        </div>
      </div>`;
  }

  function renderCard(h) {
    const badge = `<span class="src-badge badge-${h.source}">${SOURCE_LABEL[h.source] || h.source}</span>`;
    const arcBadge = h.archived ? '<span class="arc-badge">已归档</span>' : "";
    const snippets = h.snippets.map((s) => `
      <div class="snippet">
        <span class="role ${s.role}">${s.role === "user" ? "我" : s.role === "title" ? "标题" : "AI"}</span>
        <span class="txt md-inline markdown-body">${renderMd(s.text, lastQuery)}</span>
      </div>`).join("");
    return `
      <div class="${cardClass(h.session_key, h.in_progress)}" data-key="${escapeHtml(h.session_key)}">
        <div class="rc-head">
          ${checkHtml(h.session_key, h.in_progress)}
          ${badge}
          ${arcBadge}${busyBadge(h.in_progress)}
          <span class="rc-title" title="${escapeHtml(h.title)}">${escapeHtml(h.title)}</span>
          <span class="rc-count">${h.match_count} 处命中</span>
        </div>
        <div class="rc-snippets">${snippets}</div>
        <div class="rc-foot">
          <span>◷ ${fmtTime(h.updated_at)}</span>
          <span>💬 ${h.msg_count} 条消息</span>
          <span>↗ 点击查看完整会话</span>
        </div>
      </div>`;
  }

  let currentCwdReal = "";   // 当前会话的真实工作目录（供复制/打开）
  let currentCwdExists = false;

  // ---------- 会话抽屉 ----------
  async function openSession(key) {
    if (navCleanup) { navCleanup(); navCleanup = null; }
    drawer.classList.remove("hidden");
    drawerMask.classList.remove("hidden");
    $("drawer-body").innerHTML = '<div class="loading">加载会话…</div>';
    $("drawer-title").textContent = "加载中…";
    $("drawer-source").textContent = "";
    $("drawer-meta").innerHTML = "";
    currentCwdReal = "";
    currentCwdExists = false;
    try {
      const s = await api(`/api/session?key=${encodeURIComponent(key)}`);
      $("drawer-title").textContent = s.title;
      const src = $("drawer-source");
      src.textContent = SOURCE_LABEL[s.source] || s.source;
      src.className = `src-badge badge-${s.source}`;

      // 第一行：统计；第二行：工作目录（单独一行，可复制 + 打开）
      // 进行中的会话不能归档 / 同步（App 正在往会话里写数据）
      const busy = !!s.in_progress;
      const busyTag = busy
        ? ' <span class="busy-badge" title="会话仍在进行中：不可归档、不可同步">进行中</span>'
        : "";
      const unarchiveBtn = s.archived
        ? ` <button class="unarchive-btn" data-act="unarchive" title="从原生应用恢复为未归档（写前自动备份）">取消归档</button>`
        : ` <button class="archive-btn" data-act="archive"${busy ? " disabled" : ""}`
          + ` title="${busy ? "会话进行中，不可归档" : "在原生应用里归档（写前自动备份）"}">归档</button>`;
      // 跨 App 同步：把这条会话复制到另一端的原生存储
      const otherLabel = s.source === "workbuddy" ? "DSH" : "WorkBuddy";
      const migrateTarget = s.source === "workbuddy" ? "dsh" : "workbuddy";
      const migrateBtn = ` <button class="migrate-btn" data-act="migrate" data-target="${migrateTarget}"`
        + (busy ? " disabled" : "")
        + ` title="${busy ? "会话进行中，不可同步" : `把这条会话复制到 ${otherLabel}（只搬文本对话，写前自动备份）`}">同步到 ${otherLabel}</button>`;
      const stat = `共 ${s.messages.length} 条消息 · ${fmtTime(s.created_at)} — ${fmtTime(s.updated_at)}` +
        (s.archived ? ' <span class="arc-badge">已归档</span>' : "") + busyTag + unarchiveBtn + migrateBtn;
      const cwdLine = s.cwd
        ? `<div class="cwd-line" id="cwd-line" title="点击复制完整路径">
             <span class="cwd-label">工作目录</span>
             <span class="cwd-path">${escapeHtml(s.cwd)}</span>
             <button class="cwd-action" data-act="copy-cwd" title="复制完整路径" aria-label="复制路径">⧉</button>
             <button class="cwd-action" data-act="open-cwd" id="open-cwd-btn" title="在文件管理器中打开" aria-label="打开目录">📁</button>
           </div>`
        : "";
      $("drawer-meta").innerHTML =
        `<div class="drawer-stat">${stat}</div>${cwdLine}`;
      $("drawer-meta").dataset.key = key;

      // 异步解析真实路径：复制用它，打开按钮根据存在性启用/禁用
      if (s.cwd) {
        try {
          const r = await api(`/api/cwd?key=${encodeURIComponent(key)}`);
          currentCwdReal = r.cwd || "";
          currentCwdExists = !!r.exists;
          const pathEl = $("drawer-meta").querySelector(".cwd-path");
          if (pathEl) {
            pathEl.textContent = r.cwd || s.cwd;
            pathEl.title = r.cwd || s.cwd;
          }
          const openBtn = $("open-cwd-btn");
          if (openBtn && !currentCwdExists) {
            openBtn.disabled = true;
            openBtn.title = "目录不存在或已移动";
          }
        } catch (_) { /* 解析失败时保留 slug，复制仍可用 */ }
      }

      const body = $("drawer-body");
      body.innerHTML = s.messages.map((m, i) => `
        <div class="msg ${m.role}" data-midx="${i}">
          <div class="msg-head">
            <span><span class="msg-idx">#${i + 1}</span>${m.role === "user" ? "我" : "AI 回复"}</span>
            <span class="msg-time">${fmtTime(m.ts)}</span>
          </div>
          <div class="msg-body md-body markdown-body">${renderMd(m.text, lastQuery)}</div>
        </div>`).join("");

      // 消息导航条：消息数 > 3 才显示（少量消息无需定位）
      setupMsgNav(s.messages, lastQuery);
    } catch (e) {
      $("drawer-body").innerHTML = `<div class="no-result">加载失败：${escapeHtml(e.message)}</div>`;
      $("msg-nav").classList.add("hidden");
    }
  }

  // 消息导航条：一根刻度对应一条消息，点击定位，滚动时高亮当前
  function setupMsgNav(messages, query) {
    if (navCleanup) { navCleanup(); navCleanup = null; }
    const nav = $("msg-nav");
    const body = $("drawer-body");
    if (!nav || messages.length <= 3) { nav.classList.add("hidden"); return; }
    nav.classList.remove("hidden");

    const terms = queryTerms(query);
    const preview = (t) => String(t || "").replace(/\s+/g, " ").slice(0, 60);
    nav.innerHTML = messages.map((m, i) => {
      const hit = terms.some((w) => m.text.toLowerCase().includes(w)) ? " hit" : "";
      const tip = `#${i + 1} ${m.role === "user" ? "我" : "AI"} · ${preview(m.text)}`;
      return `<button class="nav-tick ${m.role}${hit}" data-idx="${i}" data-tip="${escapeHtml(tip)}" aria-label="跳到第 ${i + 1} 条消息"></button>`;
    }).join("");
    const ticks = Array.from(nav.querySelectorAll(".nav-tick"));
    const msgs = Array.from(body.querySelectorAll(".msg"));

    let activeIdx = -1;
    function setActive(i) {
      if (i === activeIdx) return;
      if (activeIdx >= 0 && ticks[activeIdx]) ticks[activeIdx].classList.remove("active");
      activeIdx = i;
      const tick = ticks[i];
      if (!tick) return;
      tick.classList.add("active");
      // 保持当前刻度在导航条可视区内（用视口坐标，避免 offsetTop 参照系错位）
      const navRect = nav.getBoundingClientRect();
      const tickRect = tick.getBoundingClientRect();
      if (tickRect.top < navRect.top) nav.scrollTop -= (navRect.top - tickRect.top) + 8;
      else if (tickRect.bottom > navRect.bottom) nav.scrollTop += (tickRect.bottom - navRect.bottom) + 8;
    }

    const onTick = (e) => {
      const tick = e.target.closest(".nav-tick");
      if (!tick) return;
      const i = Number(tick.dataset.idx);
      const msg = msgs[i];
      if (msg) msg.scrollIntoView({ behavior: "smooth", block: "start" });
      setActive(i);
    };
    nav.addEventListener("click", onTick);

    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        // 用视口坐标算当前消息，避免 offsetTop 相对定位祖先导致的坐标错位。
        // 取正文可视区顶部往下 30% 处的水平线，命中的消息即为"当前"。
        const bodyTop = body.getBoundingClientRect().top;
        const line = bodyTop + body.clientHeight * 0.3;
        let cur = 0;
        for (let i = 0; i < msgs.length; i++) {
          if (msgs[i].getBoundingClientRect().top <= line) cur = i; else break;
        }
        setActive(cur);
      });
    };
    body.addEventListener("scroll", onScroll, { passive: true });
    setActive(0);

    navCleanup = () => {
      nav.removeEventListener("click", onTick);
      body.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
      nav.classList.add("hidden");
    };
  }

  function closeDrawer() {
    drawer.classList.add("hidden");
    drawerMask.classList.add("hidden");
    if (navCleanup) { navCleanup(); navCleanup = null; }
  }

  // ---------- 事件绑定 ----------
  // 主题切换：data-theme 驱动变量；四张主题 CSS 靠 disabled 切换；持久化偏好
  $("theme-toggle").addEventListener("click", () => {
    const light = document.documentElement.getAttribute("data-theme") !== "light";
    const t = light ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    const set = (id, disabled) => { const e = document.getElementById(id); if (e) e.disabled = disabled; };
    set("md-dark", light); set("hl-dark", light);
    set("md-light", !light); set("hl-light", !light);
    try { localStorage.setItem("chatsearch-theme", t); } catch (_) {}
  });

  // 工作目录：点击整行或复制按钮复制完整路径；文件夹按钮打开系统文件管理器
  $("drawer-meta").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    const line = e.target.closest(".cwd-line");
    if (!btn && !line) return;
    const act = btn ? btn.dataset.act : "copy-cwd";
    if (act === "unarchive") {
      await doUnarchive(btn);
      return;
    }
    if (act === "archive") {
      await doArchive(btn);
      return;
    }
    if (act === "migrate") {
      await doMigrate(btn);
      return;
    }
    const path = currentCwdReal;
    if (!path) { toast("路径不可用"); return; }
    if (act === "copy-cwd") {
      try {
        await navigator.clipboard.writeText(path);
        toast("已复制路径");
      } catch (_) {
        // 降级：execCommand（http 非安全上下文下 clipboard API 可能不可用）
        const ta = document.createElement("textarea");
        ta.value = path; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); toast("已复制路径"); }
        catch (_) { toast("复制失败"); }
        document.body.removeChild(ta);
      }
    } else if (act === "open-cwd") {
      if (!currentCwdExists) { toast("目录不存在或已移动"); return; }
      const key = $("drawer-meta").dataset.key;
      try {
        const r = await api(`/api/open-cwd?key=${encodeURIComponent(key)}`);
        if (r.ok) toast("已打开文件夹");
        else toast(r.error || "打开失败");
      } catch (err) { toast(err.message || "打开失败"); }
    }
  });

  let toastTimer = null;
  function toast(msg, ms = 1800) {
    let el = $("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast"; el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), ms);
  }

  // 取消归档：写回原生存储（后端先备份），成功后刷新抽屉与列表
  async function doUnarchive(btn) {
    const key = $("drawer-meta").dataset.key;
    if (!key) return;
    const ok = confirm(
      "确定取消归档吗？\n\n" +
      "会直接写入 WorkBuddy 数据库或 DSH 配置（写前自动备份）。\n" +
      "WorkBuddy 会自动重载界面；DSH 会在它自己的界面里完成取消归档，状态立即生效。"
    );
    if (!ok) return;
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
    try {
      const r = await api(`/api/unarchive?key=${encodeURIComponent(key)}`, { method: "POST" });
      toast(r.message || "已取消归档", 5200);
      await loadFilters();
      if (q.value.trim()) doSearch(); else browse();
      await openSession(key);
    } catch (err) {
      toast(`取消归档失败：${err.message}`, 5200);
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  }

  // 归档：把当前会话在原生 App 里置为归档（单条也走批量接口，写前自动备份）
  async function doArchive(btn) {
    const key = $("drawer-meta").dataset.key;
    if (!key) return;
    const ok = confirm(
      "确定归档这条会话吗？\n\n" +
      "会写入 WorkBuddy 数据库或 DSH 配置（写前自动备份）。\n" +
      "DSH 侧需要重启应用才能看到变化。"
    );
    if (!ok) return;
    const raw = btn.textContent;
    btn.disabled = true;
    btn.textContent = "处理中…";
    try {
      const r = await api("/api/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "archive", keys: [key] }),
      });
      toast(r.message || "已归档", 6000);
      await loadStatus();
      await loadFilters();
      refreshList();
      await openSession(key);
    } catch (err) {
      toast(`归档失败：${err.message}`, 6000);
      btn.disabled = false;
      btn.textContent = raw;
    }
  }

  // 跨 App 同步：把当前会话复制到另一端原生存储（只搬文本对话，写前自动备份）
  async function doMigrate(btn) {
    const key = $("drawer-meta").dataset.key;
    if (!key) return;
    const target = btn.dataset.target;
    const label = target === "dsh" ? "DeepSeek Harness" : "WorkBuddy";
    const ok = confirm(
      `把这条会话复制到 ${label}？\n\n` +
      "只搬文本对话（你的提问 + AI 回复），不含工具调用、推理过程和附件。\n" +
      `会写入 ${label} 的原生存储（写前自动备份）；原会话保持不变。`
    );
    if (!ok) return;
    const raw = btn.textContent;
    btn.disabled = true;
    btn.textContent = "同步中…";
    try {
      const r = await api(
        `/api/migrate?key=${encodeURIComponent(key)}&target=${encodeURIComponent(target)}`,
        { method: "POST" }
      );
      let msg;
      if (target === "dsh") {
        msg = `已同步到 DSH：${r.messages} 条消息。重启 DSH 后可在会话列表看到它。`;
      } else {
        const refreshed = r.ui_refresh && r.ui_refresh.ok;
        msg = `已同步到 WorkBuddy：${r.messages} 条消息。` +
          (refreshed ? "已重载它的界面，侧边栏「最近」顶部可见。" : "重开 WorkBuddy 窗口即可看到。");
      }
      toast(msg, 7000);
      await loadStatus();
      await loadFilters();
      if (q.value.trim()) doSearch(); else if (currentCwd) browse(); else browse();
    } catch (err) {
      toast(`同步失败：${err.message}`, 7000);
    } finally {
      btn.disabled = false;
      btn.textContent = raw;
    }
  }

  q.addEventListener("input", () => {    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(doSearch, 280);
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      clearTimeout(debounceTimer);
      doSearch();
    }
  });
  $("clear-btn").addEventListener("click", () => {
    q.value = "";
    doSearch();
    q.focus();
  });

  // 统一选择工作区：同步输入框 + 侧边栏高亮 + 触发搜索/浏览
  function selectCwd(cwd) {
    currentCwd = cwd;
    $("cwd-filter").value = cwd;
    if (q.value.trim()) {
      doSearch();  // 搜索态：前端过滤全量 hits，无需重新请求
    } else {
      browse();    // 浏览态：加载该工作区全部会话
    }
  }

  // 侧边栏点击
  $("workspace-list").addEventListener("click", (e) => {
    const item = e.target.closest(".ws-item");
    if (!item) return;
    selectCwd(item.dataset.cwd || "");
  });

  // 来源筛选（只切换 data-source 的同组）
  document.querySelectorAll(".chip[data-source]").forEach((chip) => {
    chip.addEventListener("click", async () => {
      document.querySelectorAll(".chip[data-source]").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      currentSource = chip.dataset.source;
      await loadFilters();
      if (q.value.trim()) doSearch(); else browse();
    });
  });

  // 归档筛选（只切换 data-archived 的同组）
  document.querySelectorAll(".chip[data-archived]").forEach((chip) => {
    chip.addEventListener("click", async () => {
      document.querySelectorAll(".chip[data-archived]").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      currentArchived = chip.dataset.archived;
      await loadFilters();
      if (q.value.trim()) doSearch(); else browse();
    });
  });

  // 工作目录筛选（datalist 联想 + 手动输入）
  const cwdInput = $("cwd-filter");
  cwdInput.addEventListener("change", () => selectCwd(cwdInput.value.trim()));
  cwdInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { selectCwd(cwdInput.value.trim()); }
  });
  $("cwd-clear").addEventListener("click", () => {
    selectCwd("");
    cwdInput.focus();
  });

  resultList.addEventListener("click", (e) => {
    const card = e.target.closest(".result-card");
    if (!card) return;
    if (selectMode) { toggleSelect(card); return; }
    openSession(card.dataset.key);
  });

  // ---------- 批量模式 ----------
  $("select-toggle").addEventListener("click", () => setSelectMode(!selectMode));

  // 只更新卡片勾选态，不重新拉数据（切换全选时避免闪一下）
  function syncCardChecks() {
    document.querySelectorAll("#result-list .result-card").forEach((c) => {
      const on = selected.has(c.dataset.key);
      c.classList.toggle("selected", on);
      const box = c.querySelector(".rc-check");
      if (box) {
        box.textContent = on ? "✓" : "";
        box.classList.toggle("on", on);
      }
    });
    updateBatchBar();
  }

  $("batch-bar").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-batch]");
    if (!btn) return;
    const act = btn.dataset.batch;
    if (act === "exit") { setSelectMode(false); return; }
    if (act === "clear") { selected.clear(); syncCardChecks(); return; }
    if (act === "select-all") {
      const cards = Array.from(document.querySelectorAll("#result-list .result-card:not(.in-progress)"));
      const allPicked = cards.length > 0 && cards.every((c) => selected.has(c.dataset.key));
      if (allPicked) selected.clear();
      else cards.forEach((c) => selected.add(c.dataset.key));
      syncCardChecks();
      return;
    }
    runBatch(act, btn);
  });

  $("drawer-close").addEventListener("click", closeDrawer);
  drawerMask.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !drawer.classList.contains("hidden")) closeDrawer();
    if (e.key === "/" && document.activeElement !== q) {
      e.preventDefault();
      q.focus();
    }
  });

  $("rebuild-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "更新中…";
    statusEl.textContent = "正在增量更新索引…";
    try {
      const r = await api("/api/rebuild");
      await loadStatus();
      statusEl.textContent = r.updated
        ? `已更新 ${r.updated} 个会话变更 · ${r.sessions} 会话`
        : `索引已是最新 · ${r.sessions} 会话`;
      if (q.value.trim()) doSearch();
      else { await loadFilters(); if (currentCwd) browse(); }
    } catch (err) {
      statusEl.textContent = "更新失败：" + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "更新索引";
    }
  });

  // ---------- 初始化 ----------
  loadStatus();
  loadFilters();
  q.focus();
})();
