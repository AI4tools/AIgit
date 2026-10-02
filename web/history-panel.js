/* ═══════════════════════════════════════════════════════════════════════
 * aigit —— 历史 / 危险操作面板
 *
 * 宿主契约（照 web/index.html 里那段「面板契约」注释实现,不是照派单猜的）：
 *   window.AigitPanels.register({
 *     id: 'history', title: '历史',
 *     mount(el, ctx[, restore]) {},
 *     unmount() {}      // 返回值被宿主记住,下次 mount 的 restore 原样送回
 *   })
 *   ctx = { api, esc, toast, noteHtml, paneNote, fail, refresh,
 *           openModal, closeModal, confirmDanger, virtualList,
 *           status, onStatus, isActive, setPill, setTitle }
 *
 *   本面板对 ctx 的依赖是**分层**的：
 *     必需 —— api / esc。esc 缺了就拒绝启动（本面板绝不自己写一个顶上）。
 *     有就用 —— status / onStatus / isActive / confirmDanger。
 *     没有也能跑 —— 没有 confirmDanger 时退回面板内自带的确认抽屉。
 *   这样它既能挂在正式宿主上，也能单独开一个临时页自测（那两条线的差集）。
 *   setPill **故意不调**（理由见 mount 里那段注释：宿主会把它抹掉）。
 *
 *   mount 的第三个参数 restore 是宿主存的 unmount() 返回值；本面板只回
 *   { tab, selected, stashView } —— **不含 resetMode**，理由见 mount 里那段。
 *   ⚠ 面板文件被宿主当 ES module 加载，所以顶层 var/let 不挂 window ——
 *   本文件**全部**状态都关在下面的 IIFE 里，导出只靠 window.AigitPanels。
 *
 * ── 后端契约（本文件全部字段都是**打真 server 探出来的**，不是照源码猜的）──
 *
 *   查询（GET）
 *     /api/log?limit=N                    → {ok, log:[{sha(8位),author,date,subject}]}
 *     /api/branches                       → {ok, branches:[{name,current}]}
 *     /api/status                         → {ok, branch, root, staged, unstaged, conflicted, hasStaged}
 *                                          ← **没有 head sha**，所以 status 变了只能看出
 *                                            「换了分支」，看不出「多了提交」。
 *     /api/branch  (POST)                 → {ok, branch:<字符串>, ...status}
 *                                          ← branch 是字符串不是对象（建分支/切分支）
 *     /api/history/stashes?limit=N        → [ {index,ref,name,subject,raw} ]   ← 裸数组
 *     /api/history/stash?index=N&stat=1   → {ok,index,ref,name,text}   ← ok 可能是 false 而 HTTP 200
 *     /api/history/reflog?limit=N         → [ {index,sha,short,selector,author,date,action} ] ← 裸数组
 *     /api/history/tags?limit=N           → [ {name,sha,type,subject,date,annotated} ] ← 裸数组
 *     /api/history/plan/<op>?…            → {op,safe,warning,blocked,executable,requires_confirmation,…}
 *       可用的 <op> 段只有这九个（op 段的名单与响应里 op 字段的值**不是一回事**，见下）：
 *         stash / stash-drop / amend / undo-last-commit / reflog-restore /
 *         revert / branch-rename / branch-delete / tag-delete
 *       其余一律 400 {"ok":false,"error":"没有这个预演:<op>"}
 *
 *   执行（POST）
 *     /api/history/stash          {message?,include_untracked?,keep_index?}            安全
 *     /api/history/stash/apply    {index?}                                            安全
 *     /api/history/stash/pop      {index?}                                            安全
 *     /api/history/stash/drop     {index,confirmed}                                   破坏性
 *     /api/history/amend          {message?,confirmed}                                破坏性
 *     /api/history/undo-last-commit{drop_changes,confirmed}                           破坏性（soft/hard）
 *     /api/history/reflog/restore {index,mode,confirmed}                             破坏性
 *     /api/history/revert         {sha,mainline?,no_edit?}                            安全（无需 confirmed）
 *     /api/history/revert/abort   {}                                                  安全
 *     /api/history/branch/rename  {old,new,confirmed}                                 破坏性
 *     /api/history/branch/delete  {name,force,confirmed}                              破坏性
 *     /api/history/tag/create     {name,sha?,message?,force?}                         安全
 *     /api/history/tag/delete     {name,confirmed}                                    破坏性
 *
 *   两步协议是真的：上面 6 条带 confirmed 的 POST,**不带 confirmed:true 一律 400**
 *   （HistoryError code=needs_confirmation，文案「「amend」是不可逆操作…」）。
 *   所以本面板**永远**是「先 GET plan 给人看，再让用户点确认，最后才 POST」。
 *
 *   注意服务端**没有** /api/history/plan/reset —— reset 的三个模式是这么来的：
 *     soft  → /api/history/plan/undo-last-commit            + POST undo-last-commit{drop_changes:false}
 *     hard  → /api/history/plan/undo-last-commit?drop_changes=1 + POST undo-last-commit{drop_changes:true}
 *     mixed → 后端**没有** HEAD~1 的 mixed 重置。只能经 reflog/restore{mode:"mixed"} 落到
 *             某条 reflog 记录上。本面板用 /api/log?limit=2 取到 HEAD~1 的短 hash，
 *             再到 reflog 里找它的下标，找到才给 mixed，找不到就如实说不行。
 *
 * ── 本文件的三条硬纪律 ──
 *   1. mount() **不预取任何数据**：只搭骨架 + 挂一个事件委托。要数据点「刷新」。
 *   2. **没有任何 setInterval / setTimeout 轮询**。请求只在用户点击时发出；
 *      isLive() 为假（面板不可见 / 标签页隐藏 / 已卸载）时**跳过请求并丢弃结果**。
 *      唯一的自动请求是 onStatus 里的**分支切换**——那是用户自己切了分支，
 *      已经在看的页面留着旧分支的数据才是骗人。
 *   3. **零内联 onclick**，全部事件委托 + data-* 属性。HTML 里每一个插值的值都过
 *      ctx.esc()（它转义引号，所以拼进属性也是安全的）。
 * ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ── 小工具 ───────────────────────────────────────────────────────── */

  function uniq(arr) {
    var seen = {}, out = [];
    for (var i = 0; i < arr.length; i++) {
      if (!seen[arr[i]]) { seen[arr[i]] = 1; out.push(arr[i]); }
    }
    return out;
  }

  function asArray(v) { return Array.isArray(v) ? v : []; }

  function shortSha(s) { return String(s || "").slice(0, 8); }

  function prettyJson(v) {
    try { return JSON.stringify(v, null, 1); } catch (e) { return String(v); }
  }

  /* ── 面板状态 ─────────────────────────────────────────────────────── */

  var S = {
    el: null,
    esc: null,
    ctx: null,
    tab: "log",          // log | refs | stash | reflog | danger
    bodyEl: null,
    barEl: null,
    gen: 0,              // 代：unmount / 切 tab / 开新抽屉时递增，令旧响应作废
    inflight: 0,
    loaded: {},          // tab -> true（只在用户点过之后才为真）
    data: {},            // 各 tab 的最近一次数据
    selected: null,      // 时间线上选中的提交 {sha,author,date,subject}
    stashView: null,     // 展开的 stash 补丁 {index,text}
    pending: null,       // 待确认的破坏性/两步操作
    resetMode: "",       // "" 表示**一个都没选**（破坏性操作不得有默认值）
    branchSeen: "",      // 上一次见到的分支名，用来判断 status 变化该不该作废缓存
    busy: false
  };

  /* ── 可用性闸：不满足就既不发请求也不写 DOM ──────────────────────── */

  function isLive() {
    if (!S.el || !S.el.isConnected) return false;
    /* 宿主已经知道自己什么时候把面板藏起来了（切 tab）—— 直接问它，
       比自己猜 offsetParent 可靠。没实现 isActive 的宿主就退回几何判据。 */
    if (S.ctx && typeof S.ctx.isActive === "function") {
      try { if (S.ctx.isActive() === false) return false; } catch (e) { /* 问不出来就往下走 */ }
    }
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      return false;
    }
    /* offsetParent 为 null 也可能是 position:fixed，所以再看一眼有没有布局盒。
       两种判据合起来：面板不可见 ⇒ 不做任何工作。 */
    if (S.el.offsetParent === null && (!S.el.getClientRects || S.el.getClientRects().length === 0)) {
      return false;
    }
    return true;
  }

  /* 宿主提供的危险确认框。**没有它也能跑**（退回面板内抽屉），
     有它就必须用 —— 它带 requireWord，用户得手打一个词才能解锁确定按钮。 */
  function hostConfirm(opt) {
    if (!S.ctx || typeof S.ctx.confirmDanger !== "function") return null;
    try {
      var pr = S.ctx.confirmDanger(opt);
      return (pr && typeof pr.then === "function") ? pr : null;
    } catch (e) {
      return null;
    }
  }

  function esc(v) { return S.esc(v); }

  /* 后端文案里用 markdown 记号强调（`**丢弃**这个提交…`）。原文照给用户会看到
     两个星号，比不打更糟。这里只补这一种记号，而且**先转义再替换** ——
     顺序反过来就是一个现成的注入点。 */
  function escMd(v) {
    return esc(v).replace(/\*\*([^*\n]{1,400})\*\*/g, "<b>$1</b>");
  }

  /* 同一个记号，但**不产生 HTML** —— 给 ctx.confirmDanger 用。
     宿主的 confirmDanger 会自己对 opt.body 做 esc()，所以这里给出去的必须是
     纯文本：再塞 <b> 进去用户就会看见字面的尖括号。 */
  function plainMd(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/\*\*/g, "").trim();
  }

  function toast(kind, html) {
    try { if (S.ctx && typeof S.ctx.toast === "function") S.ctx.toast(kind, html); }
    catch (e) { /* 提示条挂了不该拖垮操作 */ }
  }

  function note(kind, html, closable) {
    /* 复用宿主注入的 noteHtml —— 不自己重写一套 note 样式。 */
    if (S.ctx && typeof S.ctx.noteHtml === "function") {
      return S.ctx.noteHtml(kind, html, closable !== false);
    }
    return '<div class="note ' + (kind || "") + '">' +
      (closable !== false ? '<span class="x" title="关闭">✕</span>' : "") + html + "</div>";
  }

  function refreshHost() {
    try {
      if (S.ctx && typeof S.ctx.refresh === "function") S.ctx.refresh();
    } catch (e) { /* 宿主刷新失败不影响本面板自己的重绘 */ }
  }

  /* ── 请求 ─────────────────────────────────────────────────────────── */
  /*
   * 过期判定用的是**代**（S.gen），不是「单调递增的请求序号」。
   * 原因：一页要并行发好几个 get（分支 + 标签），用序号的话后一个请求会把前一个
   * 的结果判成过期，两个列表就永远有一个是空的。代只在三处递增 ——
   * unmount / 切 tab / 开新抽屉 —— 正好是「旧结果不该再写 DOM」的三种情况。
   */
  function get(path, apply, label) {
    if (!isLive()) return Promise.resolve(null);
    var g = S.gen;
    S.inflight++;
    setBusy(true);
    return S.ctx.api(path).then(function (d) {
      S.inflight--;
      setBusy(S.inflight > 0);
      /* 代变了（卸载了 / 切走了 / 用户已经点了别的预演）：结果直接丢，不碰 DOM。
         面板不可见也一样丢 —— 「不可见时不做任何工作」。 */
      if (g !== S.gen || !isLive()) return null;
      apply(d);
      return d;
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g === S.gen && isLive()) showError(label || "读取", e);
      return null;
    });
  }

  function post(path, body, okMsg, after) {
    if (!isLive()) return Promise.resolve(null);
    var g = S.gen;
    S.inflight++;
    setBusy(true);
    return S.ctx.api(path, { method: "POST", body: body }).then(function (d) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive()) return null;
      /* okMsg **必须先判类型**再调。`/api/branch` 等走 _ok_with_status 的 POST
       返回的 `branch` 是字符串，而 okMsg 恰好也可能就是字符串 ——
       `okMsg ? okMsg(d) : …` 会在「okMsg 是字符串」时直接 TypeError，
       把一次成功的建分支报成失败。 */
      toast("ok", typeof okMsg === "function" ? okMsg(d)
        : (typeof okMsg === "string" && okMsg ? okMsg : "<b>完成</b>"));
      S.pending = null;          // 执行完就撤掉确认抽屉：同一个抽屉不能按第二次
      invalidate();
      if (after) after(d);
      render();
      refreshHost();
      /* 用户正在看的这一页立刻重拉一次：操作完还让他看着旧列表是骗人。 */
      loadTab(S.tab, true);
      return d;
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g === S.gen && isLive()) showError("操作", e);
      return null;
    });
  }

  function invalidate() {
    S.loaded = {};
    S.data = {};
    S.stashView = null;
  }

  /* 宿主每次 refresh() 都会喂一次 status。它只在面板**可见**时被调用
     （宿主自己判断），所以这里回调 = 「用户正看着这一页」。
     但只有**分支**变了才值得作废缓存：
       · 提交变了 —— 只有本面板自己执行过操作才知道，那条路径自己 invalidate；
         外部终端的 commit 没有信号，而为了抓它去轮询是明令禁止的。
       · 工作区脏了脏了几个文件 —— 面板缓存的是 log/refs/stash/reflog，
         跟暂存区无关；危险区的脏信息每次 ① 现拉，永远是最新的。
     所以只比 branch，别把 staged/unstaged 也塞进来：那样用户每存一次盘
     面板就整页作废一次。 */
  function onHostStatus(st) {
    if (!isLive()) return;
    var b = st && typeof st.branch === "string" ? st.branch : "";
    if (b === S.branchSeen) return;
    var wasLoaded = !!S.loaded[S.tab];
    S.branchSeen = b;
    invalidate();
    render();
    if (wasLoaded) loadTab(S.tab, true);   /* 已经看过的这一页立刻跟着切过去 */
    else toast("info", "已切换到分支 <b>" + esc(b) + "</b>，" +
      "本面板缓存的列表已作废 —— 点「刷新」重新拉。");
  }

  function showError(what, e) {
    var msg = (e && e.message) ? e.message : String(e);
    /* 后端**不返回** HistoryError 的 code 字段（见汇报），只能把原文照给用户。 */
    toast("err", "<b>" + esc(what) + "失败</b>\n" + esc(msg));
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 加载器：一个 tab 一份，点「刷新」或切到该 tab 且没加载过时才发请求。
   * ═══════════════════════════════════════════════════════════════════ */

  var LOADERS = {
    log: function () {
      return get("/api/log?limit=80", function (d) {
        S.data.log = asArray(d && d.log);
      }, "读取提交历史");
    },
    refs: function () {
      return Promise.all([
        get("/api/branches", function (d) {
          S.data.branches = asArray(d && d.branches);
        }, "读取分支列表"),
        get("/api/history/tags?limit=200", function (d) {
          S.data.tags = asArray(d);
        }, "读取标签列表")
      ]);
    },
    stash: function () {
      return get("/api/history/stashes?limit=100", function (d) {
        S.data.stashes = asArray(d);
      }, "读取 stash 列表");
    },
    reflog: function () {
      return get("/api/history/reflog?limit=60", function (d) {
        S.data.reflog = asArray(d);
      }, "读取 reflog");
    },
    danger: function () {
      /* 危险区只有表单,没有列表要拉。填 amend 的默认提示需要当前 HEAD,
         这里取一次 /api/log?limit=1 —— 由用户点进来/点刷新触发,不是预取。 */
      return get("/api/log?limit=1", function (d) {
        S.data.head = (asArray(d && d.log)[0]) || null;
      }, "读取 HEAD");
    }
  };

  function loadTab(tab, force) {
    if (!LOADERS[tab]) return Promise.resolve(null);
    if (S.loaded[tab] && !force) return Promise.resolve(null);
    if (!isLive()) return Promise.resolve(null);
    S.loaded[tab] = true;
    /* 数据到位后必须重画一次 —— 否则表格永远停在「尚未加载」那一屏。 */
    return LOADERS[tab]().then(function () {
      if (isLive()) render();
      return null;
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 渲染
   * ═══════════════════════════════════════════════════════════════════ */

  function render() {
    if (!S.el) return;
    renderBar();
    if (!S.el.isConnected) return;
    var html = "";
    switch (S.tab) {
      case "log": html = viewLog(); break;
      case "refs": html = viewRefs(); break;
      case "stash": html = viewStash(); break;
      case "reflog": html = viewReflog(); break;
      case "danger": html = viewDanger(); break;
      default: html = "";
    }
    html += viewPending();
    S.bodyEl.innerHTML = html;
  }

  /* tab 名单只写这一份：renderBar 画它，mount 校验 restore.tab 是不是它。 */
  var TABS = [
    ["log", "提交历史"],
    ["refs", "分支 / 标签"],
    ["stash", "暂存 stash"],
    ["reflog", "reflog 恢复"],
    ["danger", "⚠ 危险操作"]
  ];

  function renderBar() {
    var tabs = TABS;
    var h = '<div class="hp-tabs">';
    for (var i = 0; i < tabs.length; i++) {
      h += '<button type="button" class="hp-tab' +
        (S.tab === tabs[i][0] ? " active" : "") +
        (tabs[i][0] === "danger" ? " danger" : "") +
        '" data-act="tab" data-tab="' + esc(tabs[i][0]) + '">' +
        esc(tabs[i][1]) + "</button>";
    }
    h += '</div><div class="hp-tools">' +
      '<span class="hp-hint">' + (S.loaded[S.tab] ? "「刷新」重新拉取" : "尚未加载，点「刷新」") + "</span>" +
      '<button type="button" class="btn ghost tiny" data-act="reload">刷新</button>' +
      "</div>";
    S.barEl.innerHTML = h;
  }

  function setBusy(on) {
    S.busy = !!on;
    if (!S.barEl) return;
    var b = S.barEl.querySelector('[data-act="reload"]');
    if (b) { b.disabled = S.busy; b.textContent = S.busy ? "…" : "刷新"; }
  }

  function empty(msg) {
    return '<div class="hp-empty">' + esc(msg) + "</div>";
  }

  function needLoadHint() {
    if (S.loaded[S.tab]) return "";
    return note("warn",
      "这一页还没有数据。<b>点右上角「刷新」</b>去拉 —— " +
      "面板不会在你没要求的时候自己发请求（不预取、不轮询）。");
  }

  function copyBtn(value, label) {
    return '<button type="button" class="btn ghost tiny" data-act="copy" ' +
      'data-copy="' + esc(value) + '" title="复制到剪贴板">' +
      esc(label || "复制") + "</button>";
  }

  /* ── tab: 提交历史 ──────────────────────────────────────────────── */

  function viewLog() {
    var rows = asArray(S.data.log);
    var h = needLoadHint();
    if (S.loaded.log && !rows.length) {
      h += empty("这个仓库还没有提交（git log 是空的）。");
    }
    if (rows.length) {
      h += '<table class="hp-tbl"><thead><tr>' +
        "<th>提交</th><th>作者</th><th>日期</th><th>摘要</th><th></th>" +
        "</tr></thead><tbody>";
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i] || {};
        var sha = String(r.sha || "");
        var sel = S.selected && S.selected.sha === sha;
        h += '<tr class="hp-trow' + (sel ? " active" : "") +
          '" data-pick-log="' + esc(sha) + '" title="点这一行看详情">' +
          '<td class="hp-mono">' + esc(sha) + "</td>" +
          "<td>" + esc(r.author || "") + "</td>" +
          '<td class="hp-dim">' + esc(r.date || "") + "</td>" +
          '<td class="hp-subj">' + esc(r.subject || "") + "</td>" +
          '<td class="hp-nowrap">' + copyBtn(sha, "复制 hash") + "</td>" +
          "</tr>";
      }
      h += "</tbody></table>";
    }
    if (S.selected) h += viewCommitDetail(S.selected);
    return h;
  }

  function viewCommitDetail(c) {
    var sha = String(c.sha || "");
    var refs = [];
    var bs = asArray(S.data.branches);
    var ts = asArray(S.data.tags);
    /* 后端只有 8 位短 hash（/api/log 就是 8 位），没有「查 ref 指向哪个提交」的
       接口，所以这里只能靠短 hash 前缀去比 tags 里**完整**的 sha。分支接口干脆不
       给 sha —— 那就只显示名字，不谎称它指向这个提交。 */
    for (var i = 0; i < ts.length; i++) {
      if (sha && String(ts[i].sha || "").indexOf(sha) === 0) {
        refs.push("标签 " + ts[i].name);
      }
    }
    var h = '<div class="hp-detail"><h4>提交详情</h4><div class="hp-kv">' +
      row2("hash（8 位短 hash，可直接复制）",
        '<span class="hp-mono">' + esc(sha) + "</span> " + copyBtn(sha, "复制")) +
      row2("作者", esc(c.author || "")) +
      row2("日期", esc(c.date || "")) +
      row2("摘要", esc(c.subject || "")) +
      row2("指向它的标签", refs.length ? esc(uniq(refs).join("、"))
        : '<span class="hp-dim">（后端没给「ref → 提交」的接口，这里只按短 hash 前缀比对已知标签）</span>') +
      "</div>" +
      '<div class="hp-detail-acts">' +
      '<button type="button" class="btn ghost tiny" data-act="pick-revert" data-sha="' +
      esc(sha) + '">用这个提交做 revert…</button>' +
      '<span class="hp-dim">（跳到「危险操作」页，仍要走两步确认）</span>' +
      "</div></div>";
    return h;
  }

  function row2(k, vHtml) {
    return '<div class="hp-kvrow"><span class="hp-k">' + esc(k) +
      '</span><span class="hp-v">' + vHtml + "</span></div>";
  }

  /* ── tab: 分支 / 标签 ───────────────────────────────────────────── */

  function viewRefs() {
    var h = needLoadHint();
    var bs = asArray(S.data.branches), ts = asArray(S.data.tags);

    h += '<div class="hp-card"><h4>分支</h4>';
    if (S.loaded.refs && !bs.length) h += empty("没有分支。");
    h += '<table class="hp-tbl"><tbody>';
    for (var i = 0; i < bs.length; i++) {
      var b = bs[i] || {};
      h += '<tr class="hp-trow"><td class="hp-subj">' + esc(b.name || "") +
        (b.current ? ' <span class="badge hot">当前</span>' : "") + "</td>" +
        '<td class="hp-nowrap">' +
        '<button type="button" class="btn ghost tiny" data-act="plan-rename" data-name="' +
        esc(b.name) + '">改名</button> ' +
        '<button type="button" class="btn danger tiny" data-act="plan-del-branch" data-name="' +
        esc(b.name) + '">删除</button></td></tr>';
    }
    h += "</tbody></table>";
    /* 新建分支走 /api/branch(建完自动切过去) —— 这是宿主已有的接口，不是历史模块的。 */
    h += '<div class="hp-form"><label>新分支名</label>' +
      '<input type="text" id="hp-new-branch" placeholder="feature/…（只能字母数字 _ . / -）">' +
      '<button type="button" class="btn tiny" data-act="new-branch">新建并切换</button>' +
      '<span class="hp-dim">对应 <code>POST /api/branch</code>，命令行等价 <code>git checkout -b</code></span>' +
      "</div></div>";

    h += '<div class="hp-card"><h4>标签</h4>';
    if (S.loaded.refs && !ts.length) h += empty("没有标签。");
    h += '<table class="hp-tbl"><thead><tr><th>名称</th><th>类型</th>' +
      "<th>指向</th><th>日期</th><th></th></tr></thead><tbody>";
    for (var j = 0; j < ts.length; j++) {
      var t = ts[j] || {};
      h += '<tr class="hp-trow"><td class="hp-subj">' + esc(t.name || "") + "</td>" +
        "<td>" + esc(t.type || "") + (t.annotated ? "（附注）" : "") + "</td>" +
        '<td class="hp-mono">' + esc(shortSha(t.sha)) + "</td>" +
        '<td class="hp-dim">' + esc(t.date || "") + "</td>" +
        '<td class="hp-nowrap">' + copyBtn(String(t.sha || ""), "复制 hash") +
        ' <button type="button" class="btn danger tiny" data-act="plan-del-tag" data-name="' +
        esc(t.name) + '">删除</button></td></tr>';
    }
    h += "</tbody></table>";
    h += '<div class="hp-form"><label>新标签名</label>' +
      '<input type="text" id="hp-new-tag" placeholder="v1.0.0">' +
      '<input type="text" id="hp-new-tag-msg" placeholder="附注（留空＝轻量标签）">' +
      '<label class="hp-chk"><input type="checkbox" id="hp-new-tag-force"> 覆盖同名标签 (force)</label>' +
      '<button type="button" class="btn tiny" data-act="new-tag">创建标签</button>' +
      '<span class="hp-dim">对应 <code>POST /api/history/tag/create</code>，命令行等价 <code>git tag</code></span>' +
      "</div></div>";
    return h;
  }

  /* ── tab: stash ─────────────────────────────────────────────────── */

  function viewStash() {
    var rows = asArray(S.data.stashes);
    var h = needLoadHint();
    h += '<div class="hp-card"><h4>存起当前改动（git stash push）</h4>' +
      '<div class="hp-form"><label>说明</label>' +
      '<input type="text" id="hp-stash-msg" placeholder="留空＝不带说明">' +
      '<label class="hp-chk"><input type="checkbox" id="hp-stash-untracked"> 含未跟踪文件 (-u)</label>' +
      '<label class="hp-chk"><input type="checkbox" id="hp-stash-keepidx"> 保留暂存区 (--keep-index)</label>' +
      '<button type="button" class="btn tiny" data-act="plan-stash">① 查看影响（不执行）</button>' +
      '<span class="hp-dim">stash 可恢复，后端因此<b>不设</b>确认闸；这里仍然先给预演。</span>' +
      "</div></div>";

    h += '<div class="hp-card"><h4>stash 列表</h4>';
    if (S.loaded.stash && !rows.length) h += empty("没有 stash。");
    h += '<table class="hp-tbl"><thead><tr><th>引用</th><th>说明</th><th>原始行</th>' +
      "<th></th></tr></thead><tbody>";
    for (var i = 0; i < rows.length; i++) {
      var s = rows[i] || {};
      var idx = String(s.index);
      h += '<tr class="hp-trow"><td class="hp-mono">' + esc(s.ref || ("stash@{" + idx + "}")) + "</td>" +
        '<td class="hp-subj">' + esc(s.subject || "") + "</td>" +
        '<td class="hp-dim">' + esc(s.raw || "") + "</td>" +
        '<td class="hp-nowrap">' +
        '<button type="button" class="btn ghost tiny" data-act="stash-view" data-idx="' + esc(idx) + '">看补丁</button> ' +
        '<button type="button" class="btn ghost tiny" data-act="stash-apply" data-idx="' + esc(idx) + '">贴回</button> ' +
        '<button type="button" class="btn ghost tiny" data-act="stash-pop" data-idx="' + esc(idx) + '">弹出</button> ' +
        '<button type="button" class="btn danger tiny" data-act="plan-stash-drop" data-idx="' + esc(idx) + '">丢弃</button>' +
        "</td></tr>";
      if (S.stashView && String(S.stashView.index) === idx) {
        h += '<tr><td colspan="4"><pre class="hp-pre">' +
          esc(S.stashView.text || "(空)") + "</pre></td></tr>";
      }
    }
    h += "</tbody></table></div>";
    return h;
  }

  /* ── tab: reflog ────────────────────────────────────────────────── */

  function viewReflog() {
    var rows = asArray(S.data.reflog);
    var h = needLoadHint();
    h += '<div class="hp-card"><h4>从 reflog 恢复到某次状态</h4>' +
      '<div class="hp-form"><span class="hp-dim">在下面某一行的「恢复」上选模式。</span></div>' +
      '<div class="hp-note-warn">恢复 = <code>git reset --&lt;模式&gt; &lt;提交&gt;</code>。' +
      "<b>mode 不会预选</b>，必须你自己点。</div>";
    if (S.loaded.reflog && !rows.length) {
      h += empty("reflog 是空的（仓库还没有提交，或 reflog 被 gc 清掉了）。");
    }
    h += '<table class="hp-tbl"><thead><tr><th>选择器</th><th>hash</th>' +
      "<th>动作</th><th>作者</th><th>时间</th><th></th></tr></thead><tbody>";
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i] || {};
      h += '<tr class="hp-trow"><td class="hp-mono">' + esc(r.selector || "") + "</td>" +
        '<td class="hp-mono">' + esc(shortSha(r.sha)) + " " + copyBtn(String(r.sha || ""), "复制") + "</td>" +
        '<td class="hp-subj">' + esc(r.action || "") + "</td>" +
        "<td>" + esc(r.author || "") + "</td>" +
        '<td class="hp-dim">' + esc(r.date || "") + "</td>" +
        '<td class="hp-nowrap">' + modeButtons(r.index) + "</td></tr>";
    }
    h += "</tbody></table></div>";
    return h;
  }

  /* reset 模式：**一个都不预选**。破坏性操作不得有任何形式的默认值。 */
  function modeButtons(idx) {
    var modes = [["soft", "soft"], ["mixed", "mixed"], ["hard", "hard"]];
    var h = "";
    for (var i = 0; i < modes.length; i++) {
      h += '<button type="button" class="btn ' +
        (modes[i][0] === "hard" ? "danger" : "ghost") +
        ' tiny" data-act="plan-reflog-restore" data-idx="' + esc(String(idx)) +
        '" data-mode="' + modes[i][0] + '">恢复 ' + modes[i][1] + "</button> ";
    }
    return h;
  }

  /* ── tab: 危险操作 ──────────────────────────────────────────────── */

  function viewDanger() {
    var head = S.data.head;
    var h = needLoadHint();
    h += '<div class="hp-danger">' +
      '<div class="hp-danger-head">⚠ 危险操作区 —— 下面每一步都会改写或丢弃仓库内容。' +
      "本面板对它们一律走<b>两步</b>：① 先 GET <code>/api/history/plan/*</code> 把影响显示给你，" +
      "② 你看完点「确认执行」，才会发 POST。<b>没有一步到位的按钮。</b></div>";

    /* -- amend -- */
    h += '<div class="hp-op"><h4>1 · amend —— 改写上一次提交</h4>' +
      '<p class="hp-desc">改写历史。已推送过的提交被改写后，协作者必须重新拉取或 reset。</p>' +
      '<div class="hp-form"><label>新的提交信息</label>' +
      '<input type="text" id="hp-amend-msg" placeholder="' +
      esc(head ? head.subject || "" : "留空＝沿用原提交信息") + '">' +
      '<span class="hp-dim">留空＝沿用原提交信息</span>' +
      '<button type="button" class="btn tiny" data-act="plan-amend">① 查看影响（不执行）</button>' +
      '<span class="hp-dim">当前 HEAD：<span class="hp-mono">' +
      esc(head ? head.sha : "?") + "</span> " + esc(head ? head.subject : "") + "</span>" +
      "</div></div>";

    /* -- revert -- */
    h += '<div class="hp-op"><h4>2 · revert —— 新建一个反向提交</h4>' +
      '<p class="hp-desc">不改写历史，撤销错了可以再 revert 一次。后端把 revert 归为"安全操作"，' +
      "<b>不设</b> confirmed 闸；本面板仍然强制你走两步并先看预览。</p>" +
      '<div class="hp-form"><label>提交 hash / 引用</label>' +
      '<input type="text" id="hp-revert-sha" placeholder="HEAD 或完整 hash">' +
      '<label>mainline（仅 merge 提交需要）</label>' +
      '<input type="text" id="hp-revert-mainline" placeholder="留空＝非 merge 才可用">' +
      '<button type="button" class="btn tiny" data-act="plan-revert">① 查看影响（不执行）</button>' +
      '<span class="hp-dim">对应 <code>POST /api/history/revert</code>，命令行等价 <code>git revert --no-edit</code></span>' +
      "</div></div>";

    /* -- reset -- */
    h += '<div class="hp-op"><h4>3 · reset —— 撤销上一次提交（HEAD~1）</h4>' +
      '<div class="hp-note-warn"><b>三种模式都没有默认选中。</b>必须你自己点一个。' +
      "<b style=\"color:#ff8080\">选 hard 会丢弃工作区里所有未提交的改动，且不可恢复。</b></div>" +
      '<div class="hp-form hp-reset">' +
      resetRadio("soft", "soft", "reset --soft：提交撤销，改动全部回到暂存区") +
      resetRadio("mixed", "mixed", "reset --mixed：提交撤销，改动回到工作区且取消暂存") +
      resetRadio("hard", "hard", "reset --hard：提交撤销，未提交的改动永久丢失") +
      '<button type="button" class="btn danger tiny" data-act="plan-reset">① 查看影响（不执行）</button>' +
      "</div>" +
      '<div class="hp-dim" id="hp-reset-hint">' + resetHintHtml() + "</div></div>";

    /* revert 中途停下来时的收场按钮。没有它,一旦 revert 卡在冲突里,
       面板就成了「唯一入口」的反面 —— 用户只能去敲命令行。 */
    h += '<div class="hp-op"><h4>4 · revert --abort —— 收场</h4>' +
      '<p class="hp-desc">revert 撞上冲突时仓库会停在 revert 中途（REVERT_HEAD 还在）。' +
      "这个按钮把它退回去，命令等价 <code>git revert --abort</code>。" +
      "不在 revert 中途时后端会回 400（那是好事，说明确实没有要中止的东西）。</p>" +
      '<div class="hp-form"><button type="button" class="btn danger tiny" ' +
      'data-act="revert-abort">中止进行中的 revert</button></div></div>';

    h += "</div>";
    return h;
  }

  function resetRadio(name, label, desc) {
    /* checked 由 S.resetMode 回填，而不是写死在 HTML 里：
       预演回来会整块重绘，硬编码的 checked 每次都会被重绘抹掉。 */
    return '<label class="hp-radio hp-radio-' + esc(name) + '">' +
      '<input type="radio" name="hp-reset-mode" value="' + esc(name) + '"' +
      (S.resetMode === name ? " checked" : "") + "> " +
      "<b>" + esc(label) + "</b> <span class=\"hp-dim\">" + esc(desc) + "</span></label>";
  }

  /* 模式**默认一个都不选**（S.resetMode 初值 ""）。选 hard 时红字常驻。 */
  function resetHintHtml() {
    if (!S.resetMode) {
      return "还没选模式。<b>三种都没有默认值</b> —— 必须你自己点一个，" +
        "而且点 hard 之后它会一直显示下面这行红字。";
    }
    if (S.resetMode === "hard") {
      return '<b class="hp-red">已选 hard —— 未提交的改动将永久丢失。</b>';
    }
    return "已选 <b>" + esc(S.resetMode) + "</b>。点「① 查看影响」看这一步会做什么。";
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 确认抽屉 —— 两步协议的第二步在这里
   * ═══════════════════════════════════════════════════════════════════ */

  function viewPending() {
    var p = S.pending;
    if (!p) return "";
    var h = '<div class="hp-pending' + (p.danger ? " danger" : "") + '">';
    h += "<h4>② 确认执行：" + esc(p.title) + "</h4>";
    if (p.planError) {
      /* 预演失败与执行失败**不是一回事**，文案必须分开：
         说错会让用户以为操作已经做过了（或者反过来，重试一次）。 */
      h += note("err", p.execError
        ? "<b>执行失败。</b>后端原文：\n" + esc(p.planError)
        : "预演失败，<b>没有执行任何东西</b>：\n" + esc(p.planError));
    } else if (p.plan) {
      h += p.previewHtml;
      h += '<details class="hp-raw"><summary>后端返回的完整预演 JSON</summary>' +
        '<pre class="hp-pre">' + esc(prettyJson(p.plan)) + "</pre></details>";
    } else {
      h += note("warn", "预演请求还在路上…");
    }
    if (p.danger) {
      h += '<div class="hp-note-warn">' +
        (p.irreversible
          ? "<b>这一步不可撤销。</b>"
          : "<b>这一步会改仓库，但可以退回来。</b>") +
        "确认之后后端才会真正执行；这里也没有任何别的入口会替你执行。</div>";
    }
    if (p.blocked) {
      h += note("err", "这一步<b>现在不能做</b>：" + esc(p.blocked));
    }
    var disabled = (p.blocked || p.planError || !p.plan || p.running) ? " disabled" : "";
    h += '<div class="hp-pending-acts">' +
      '<button type="button" class="btn danger"' + disabled +
      ' data-act="exec-pending">' +
      (hostHasConfirm() ? "确认执行…" : "确认执行") + "</button> " +
      '<button type="button" class="btn ghost"' + disabled +
      ' data-act="cancel-pending">取消</button>' +
      '<span class="hp-dim">← 「取消」不会发任何 POST。' +
      (hostHasConfirm()
        ? "有宿主确认框时，还要在框里按键解锁才会真的执行。"
        : "（没有宿主 confirmDanger，用的是面板内抽屉；正式宿主上会走宿主确认框。）") +
      "</span></div></div>";
    return h;
  }

  function hostHasConfirm() {
    return !!(S.ctx && typeof S.ctx.confirmDanger === "function");
  }

  /* 预演 → 抽屉。执行体是闭包，不是拼进 HTML 的字符串。 */
  function openPending(cfg) {
    S.pending = {
      title: cfg.title,
      danger: cfg.danger !== false,
      /* irreversible 决定确认框里那句话。默认 true（破坏性操作），
         只有确实退得回来的操作才显式写 reversible: true —— 别把
         「可撤销」写成默认值，那正是把两次 amend 说成一次的那种话。 */
      irreversible: cfg.reversible !== true,
      /* 传给宿主 confirmDanger 的 requireWord。**空串 = 不要求键入**。
         只给真正退不回来的操作上锁；soft / mixed / revert 这些能靠
         reflog 或再 revert 一次退回来的，不该给用户加没必要的摩擦。 */
      requireWord: cfg.requireWord || "",
      /* 宿主确认框正文里的第一句话。**每个破坏性操作都必须自己写一句**，
         不能靠 requireWord 隐含 —— 确认框是最后一道闸，它上面不能再有别的门。 */
      dangerNote: cfg.dangerNote || "",
      bullets: cfg.bullets || [],
      plan: null,
      planError: null,
      execError: false,
      blocked: null,
      previewHtml: "",
      running: false,
      exec: cfg.exec
    };
    render();
    var g = ++S.gen;          /* 之前那个抽屉的预演结果立刻作废 */
    S.inflight++;
    setBusy(true);
    S.ctx.api(cfg.planPath).then(function (plan) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive() || !S.pending) return;
      S.pending.plan = plan || {};
      S.pending.blocked = plan && plan.blocked ? plan.blocked : null;
      /* 宿主确认框里的要点**从预演结果现算**，不靠每个调用点手写 ——
         少一处手写就少一处「忘了填」；planBullets 是唯一一个列点的地方。 */
      S.pending.bullets = (cfg.bullets || []).concat(planBullets(plan));
      S.pending.previewHtml = cfg.preview
        ? cfg.preview(plan || {})
        : note("warn", escMd(plan && plan.warning ? plan.warning : "(预演没有给出说明)"));
      render();
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive() || !S.pending) return;
      S.pending.planError = (e && e.message) ? e.message : String(e);
      render();
    });
  }

  function execPending() {
    var p = S.pending;
    if (!p || !p.exec || p.blocked || p.planError || !p.plan) return;
    if (!isLive()) return;
    /* 有宿主确认框就走它 —— 它带 requireWord（手打一个词才解锁确定），
       是这条链路上最后一道防手滑的闸。面板内抽屉只是「① 的结果」，
       本身不是「②」。没有 confirmDanger 的宿主才退回直接执行。
       opt.body 必须是**纯文本**（宿主会自己 esc），所以这里用 plainMd 而不是 escMd。 */
    var body = [];
    if (p.dangerNote) body.push(p.dangerNote);
    body.push(p.irreversible
      ? "确认之后后端才会真正执行这一步，后端会带 confirmed=true。"
      : "确认之后后端才会真正执行这一步。这条可以退回来。");
    var pw = plainMd(p.plan && p.plan.warning);
    if (pw) body.push("后端预演原文：" + pw);
    var hp = hostConfirm({
      title: p.title,
      body: body.join("\n\n"),
      bullets: p.bullets,
      okLabel: "确认执行",
      requireWord: p.requireWord
    });
    if (hp) {
      hp.then(function (ok) {
        if (ok === true && isLive()) doPending();
      });
      return;
    }
    doPending();
  }

  function doPending() {
    var p = S.pending;
    if (!p || !p.exec) return;
    p.running = true;
    render();
    S.inflight++;
    setBusy(true);
    var g = S.gen;
    p.exec().then(function (d) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive()) return;
      toast("ok", "<b>" + esc(p.title) + " 已执行</b>\n" +
        esc(prettyJson(d)).slice(0, 900));
      S.pending = null;
      invalidate();
      render();
      refreshHost();
      loadTab(S.tab, true);       /* 用户刚看见的那个 tab 立即重新拉一次 */
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive()) return;
      /* 抽屉留着：预演已经看过了，错误信息得贴在它旁边。
         而且这是**执行**失败 —— 用户需要知道下一步是「改参数重试」还是
         「仓库已经半途改了」，所以标记出来，措辞和预演失败不一样。 */
      p.running = false;
      p.execError = true;
      p.planError = (e && e.message) ? e.message : String(e);
      render();
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 各操作的「预演 → 执行」定义
   * ═══════════════════════════════════════════════════════════════════ */

  function valOf(id) {
    var n = S.el ? S.el.querySelector("#" + id) : null;
    return n ? String(n.value == null ? "" : n.value) : "";
  }
  function checkedOf(id) {
    var n = S.el ? S.el.querySelector("#" + id) : null;
    return !!(n && n.checked);
  }
  function resetMode() {
    var n = S.el ? S.el.querySelector('input[name="hp-reset-mode"]:checked') : null;
    /* DOM 是唯一真相：用户点了 radio，S.resetMode 一定跟着更新了，
       但读 DOM 也不依赖那个前提。 */
    return n ? String(n.value) : "";
  }

  function dirtyPreview(d) {
    if (!d) return '<span class="hp-dim">(预演没给工作区信息)</span>';
    return "已暂存 " + asArray(d.staged).length + " · 未暂存 " +
      asArray(d.unstaged).length + " · 未跟踪 " + asArray(d.untracked).length +
      " · 冲突 " + asArray(d.conflicted).length;
  }

  /* ── amend ── */
  function planAmend() {
    var msg = valOf("hp-amend-msg");
    var qs = "/api/history/plan/amend" +
      (msg === "" ? "" : "?message=" + encodeURIComponent(msg));
    openPending({
      title: "amend —— 改写上一次提交",
      danger: true,
      /* 改写历史退不回来（只能靠 reflog 碰运气），上锁。 */
      requireWord: "amend",
      dangerNote: "改写历史退不回来（只能靠 reflog 碰运气）。" +
        "已推送过的提交被改写后，协作者必须重新拉取或 reset。",
      planPath: qs,
      preview: function (p) {
        var h = "";
        h += '<div class="hp-kv">' +
          row2("将要被改写的提交",
            '<span class="hp-mono">' + esc(p.old_sha || "?") + "</span> " +
            copyBtn(p.old_short || shortSha(p.old_sha), "复制")) +
          row2("原摘要", esc(p.old_subject || "")) +
          row2("新提交信息", p.message_changes
            ? esc(p.new_message == null ? "" : p.new_message)
            : '<span class="hp-dim">未提供 → 沿用原提交信息</span>') +
          row2("工作区", dirtyPreview(p.dirty)) +
          row2("是否已推送", p.published && p.published.published
            ? '<span class="hp-red">已推送：' + esc(asArray(p.published.remotes).join("、")) + "</span>"
            : '<span class="hp-dim">' + esc(p.published && p.published.note || "未推送") + "</span>") +
          "</div>";
        h += note("warn", escMd(p.warning || ""));
        h += note("warn", "改完之后<b>新的 hash 由 git 现场生成</b>，" +
          "amend 之前算不出来 —— 预演只能告诉你「旧的是哪一条」。执行完这里会给出新 hash。");
        return h;
      },
      exec: function () {
        var body = { confirmed: true };
        if (msg !== "") body.message = msg;      /* 留空＝不带 message 字段，沿用原信息 */
        return S.ctx.api("/api/history/amend", { method: "POST", body: body });
      }
    });
  }

  /* ── revert ── */
  function planRevert() {
    var sha = valOf("hp-revert-sha").trim();
    var ml = valOf("hp-revert-mainline").trim();
    if (!sha) { toast("warn", "先填一个提交 hash 或引用（例如 HEAD）。"); return; }
    var qs = "/api/history/plan/revert?sha=" + encodeURIComponent(sha);
    if (ml !== "") qs += "&mainline=" + encodeURIComponent(ml);
    openPending({
      title: "revert —— 新建反向提交 " + sha,
      /* 后端把 revert 列为安全操作（SAFE_STEPS，无 confirmed 闸）。
         它确实退得回来（再 revert 一次），所以确认框写「可退回来」而不是
         「不可撤销」—— 把可撤销的操作说成不可撤销，和反过来一样是骗人。 */
      danger: true,
      reversible: true,
      dangerNote: "revert 不改写历史：它新建一个反向提交，" +
        "撤销错了可以再 revert 一次。后端把它算安全操作，不设 confirmed 闸。",
      planPath: qs,
      preview: function (p) {
        var subject = p.subject || "";
        var full = p.sha || "";
        var h = '<div class="hp-kv">' +
          row2("被撤销的提交",
            '<span class="hp-mono">' + esc(full) + "</span> " +
            copyBtn(full, "复制")) +
          row2("摘要", esc(subject)) +
          row2("父提交", asArray(p.parents).length
            ? '<span class="hp-mono">' +
            esc(asArray(p.parents).map(shortSha).join("  ")) + "</span>"
            : '<span class="hp-dim">（根提交）</span>') +
          row2("是 merge 提交", p.is_merge
            ? '<span class="hp-red">是</span>，mainline=' + esc(p.mainline)
            : "否") +
          row2("将生成的新提交信息",
            '<pre class="hp-pre">' +
            esc('Revert "' + subject + '"\n\nThis reverts commit ' + full + '.') +
            "</pre>") +
          "</div>";
        h += note("warn", escMd(p.warning || ""));
        h += note("warn", "<b>内容级 diff 看不到：</b>后端<b>没有任何</b>返回任意提交补丁的接口" +
          "（<code>/api/diff</code> 只给工作区差异，<code>/api/file</code> 给的是文件在某 ref 的全文）。" +
          "所以这里能给的预览是「提交信息 + 目标 hash」，不是逐行补丁。");
        if (p.is_merge && p.mainline != null) {
          h += note("err", "merge 提交的 mainline 选错会得到<b>完全相反</b>的逆向改动，" +
            "确认前请再看一眼上面那行 <code>mainline</code>。");
        }
        return h;
      },
      exec: function () {
        var body = { sha: sha };
        if (ml !== "") {
          var n = parseInt(ml, 10);
          body.mainline = isNaN(n) ? ml : n;   /* 非整数原样送回，让后端 400 说清楚 */
        }
        /* no_edit 故意不传：服务端默认 true（_bb(b,"no_edit",True)），
           对应 git revert --no-edit，不会卡在编辑器上。 */
        return S.ctx.api("/api/history/revert", { method: "POST", body: body });
      }
    });
  }

  /* ── reset（soft / mixed / hard，三个都不预选） ── */
  function planReset() {
    var mode = resetMode();
    if (!mode) {
      toast("warn", "reset 的三种模式<b>都没有默认值</b>，请先自己选一个。" +
        "（hard 会永久丢弃未提交的改动。）");
      return;
    }
    if (mode === "mixed") return planResetMixed();
    var drop = (mode === "hard");
    var qs = "/api/history/plan/undo-last-commit" + (drop ? "?drop_changes=1" : "");
    openPending({
      title: "reset --" + mode + " HEAD~1",
      danger: true,
      /* 只有 hard 会丢工作区内容。soft 能靠 reflog 退回来，不给它加锁。 */
      requireWord: drop ? "reset-hard" : "",
      /* 红线：reset --hard 的确认框**必须**写着「未提交的改动将永久丢失」。
         后端 warning 说的是「连同工作区里尚未提交的改动一起抹掉」—— 同一个意思，
         但确认框不能靠转述后端文案来满足这条，所以这里自己写死这一句。 */
      dangerNote: drop
        ? "未提交的改动将永久丢失。这等于 git reset --hard HEAD~1，" +
          "工作区里尚未提交的改动会被一起抹掉，没有任何后端接口能把它找回来。"
        : "提交撤销、改动回到暂存区。这条能靠 git reflog 退回来。",
      planPath: qs,
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("将被撤销的提交",
            '<span class="hp-mono">' + esc(p.target_sha || "?") + "</span> " +
            copyBtn(p.target_short || shortSha(p.target_sha), "复制")) +
          row2("摘要", esc(p.target_subject || "")) +
          row2("实际执行的 git", '<code>git reset --' + esc(p.mode || mode) + " HEAD~1</code>") +
          row2("父提交个数", p.parents != null ? esc(p.parents) +
            (p.parents > 1 ? "（merge 提交，reset 只会退到第一父）" : "") : "—") +
          row2("工作区", dirtyPreview(p.dirty)) +
          row2("出问题怎么退回来", '<code>' + esc(p.recoverable_via || "git reflog") + "</code>") +
          "</div>";
        h += note(drop ? "err" : "warn", escMd(p.warning || ""));
        if (drop) {
          h += note("err", "<b>未提交的改动将永久丢失。</b>" +
            "这个操作在 git 层面等于 <code>git reset --hard HEAD~1</code>：" +
            "工作区里尚未提交的改动会被一起抹掉，没有任何后端接口能把它找回来。");
        }
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/undo-last-commit", {
          method: "POST", body: { drop_changes: drop, confirmed: true }
        });
      }
    });
  }

  /* mixed：后端没有 HEAD~1 的 mixed 重置，只能经 reflog 落到同一条提交上。
     先用 /api/log?limit=2 拿到 HEAD~1 的 8 位短 hash，再去 reflog 里定位它的下标。 */
  function planResetMixed() {
    if (!isLive()) return;
    var g = S.gen;
    S.inflight++;
    setBusy(true);
    S.ctx.api("/api/log?limit=2").then(function (d) {
      var rows = asArray(d && d.log);
      if (rows.length < 2) {
        S.inflight--;
        setBusy(S.inflight > 0);
        toast("warn", "仓库只有一个提交，没有 HEAD~1 可重置。");
        return null;
      }
      var parent = String(rows[1].sha || "");
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive()) return null;
      return S.ctx.api("/api/history/reflog?limit=500").then(function (r) {
        if (g !== S.gen || !isLive()) return null;
        var list = asArray(r), hit = null;
        for (var i = 0; i < list.length; i++) {
          if (String(list[i].sha || "").indexOf(parent) === 0) { hit = list[i]; break; }
        }
        if (!hit) {
          toast("err", "reflog 里找不到 HEAD~1（" + parent + "），" +
            "reset --mixed 暂不可用。改用 soft / hard，或到 reflog 页对具体条目做 mixed 恢复。");
          return null;
        }
        openPending({
          title: "reset --mixed HEAD~1（经 reflog 落到 " + parent + "）",
          danger: true,
          /* mixed 不丢工作区内容，不上锁。 */
          requireWord: "",
          dangerNote: "mixed 保留工作区改动、取消暂存，不会丢内容 —— " +
            "但它同样改写当前分支的指向，已推送过的历史要另外处理。",
          planPath: "/api/history/plan/reflog-restore?index=" +
            encodeURIComponent(hit.index) + "&mode=mixed",
          preview: function (p) {
            var h = '<div class="hp-kv">' +
              row2("目标提交",
                '<span class="hp-mono">' + esc(p.target && p.target.sha) + "</span> " +
                copyBtn(p.target && p.target.short, "复制")) +
              row2("选择器", '<code>' + esc(p.target && p.target.selector) + "</code>") +
              row2("reflog 动作", esc(p.target && p.target.action || "")) +
              row2("当前 HEAD", '<span class="hp-mono">' +
                esc(shortSha(p.head_now)) + "</span>") +
              row2("实际执行的 git", '<code>git reset --mixed ' +
                esc(p.target && p.target.sha) + "</code>") +
              "</div>";
            h += note("warn", escMd(p.warning || ""));
            h += note("warn", "mixed 保留工作区改动、取消暂存，<b>不会</b>丢内容 —— " +
              "但它和 reset --soft 一样改写当前分支的指向，" +
              "已推送过的历史需要另外处理。");
            h += note("warn", "这个目标是通过 <code>/api/log</code> 的短 hash 到 reflog 里" +
              "反查出来的（后端没有 <code>HEAD~1</code> 的 mixed 重置接口）。");
            return h;
          },
          exec: function () {
            return S.ctx.api("/api/history/reflog/restore", {
              method: "POST",
              body: { index: hit.index, mode: "mixed", confirmed: true }
            });
          }
        });
        return null;
      });
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g === S.gen && isLive()) showError("定位 HEAD~1", e);
    });
  }

  function planReflogRestore(idx, mode) {
    var i = String(idx), m = String(mode);
    openPending({
      title: "从 reflog 恢复（reset --" + m + "）",
      danger: true,
      requireWord: m === "hard" ? "reset-hard" : "",
      dangerNote: m === "hard"
        ? "未提交的改动将永久丢失。这等于 git reset --hard HEAD@{" + i + "}，" +
          "工作区里尚未提交的改动会被一起抹掉。"
        : "恢复到选中的那次状态（reset --" + m + " HEAD@{" + i + "}）。" +
          "工作区改动不会被抹掉，但当前分支的指向会被改写。",
      planPath: "/api/history/plan/reflog-restore?index=" +
        encodeURIComponent(i) + "&mode=" + encodeURIComponent(m),
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("目标提交",
            '<span class="hp-mono">' + esc(p.target && p.target.sha) + "</span> " +
            copyBtn(p.target && p.target.short, "复制")) +
          row2("选择器", '<code>' + esc(p.target && p.target.selector) + "</code>") +
          row2("reflog 动作", esc(p.target && p.target.action || "")) +
          row2("当前 HEAD", '<span class="hp-mono">' + esc(shortSha(p.head_now)) + "</span>") +
          row2("模式", "<code>--" + esc(p.mode || m) + "</code>") +
          "</div>";
        h += note(m === "hard" ? "err" : "warn", escMd(p.warning || ""));
        if (m === "hard") {
          h += note("err", "<b>未提交的改动将永久丢失。</b>mode=hard 会把工作区" +
            "和暂存区一起对齐到目标提交。");
        }
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/reflog/restore", {
          method: "POST", body: { index: parseInt(i, 10), mode: m, confirmed: true }
        });
      }
    });
  }

  /* ── stash ── */
  function planStash() {
    var msg = valOf("hp-stash-msg");
    var u = checkedOf("hp-stash-untracked"), k = checkedOf("hp-stash-keepidx");
    var qs = "/api/history/plan/stash";
    if (msg !== "") qs += "?message=" + encodeURIComponent(msg);
    if (u) qs += (qs.indexOf("?") < 0 ? "?" : "&") + "include_untracked=1";
    openPending({
      title: "stash —— 把当前改动收起来",
      danger: false,
      reversible: true,
      dangerNote: "stash 是可恢复操作：贴回来用 apply / pop。" +
        "后端把它算安全操作，不设 confirmed 闸。",
      planPath: qs,
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("将要被收起的条目数", esc(p.will_stash)) +
          row2("收完之后仍在工作区的未跟踪文件", esc(p.untracked_kept)) +
          row2("现有 stash 条数", esc(p.current_stash_count) + " → " +
            esc((Number(p.current_stash_count) || 0) + 1)) +
          row2("工作区", dirtyPreview(p.dirty)) +
          "</div>";
        h += note("ok", "stash 是<b>可恢复</b>操作：贴回来用 apply / pop，" +
          "所以后端不设确认闸（SAFE_STEPS），这里仍然先把预演给你看。");
        if (p.warning) h += note("warn", escMd(p.warning));
        return h;
      },
      exec: function () {
        var body = {};
        if (msg !== "") body.message = msg;
        body.include_untracked = !!u;
        body.keep_index = !!k;
        return S.ctx.api("/api/history/stash", { method: "POST", body: body });
      }
    });
  }

  function stashByIndex(i) {
    var rows = asArray(S.data.stashes), want = String(i);
    for (var k = 0; k < rows.length; k++) {
      if (String(rows[k].index) === want) return rows[k];
    }
    return null;
  }

  function planStashDrop(idx) {
    var i = String(idx);
    openPending({
      title: "丢弃 stash " + i,
      danger: true,
      requireWord: "drop-stash",
      dangerNote: "这个 stash 丢掉就找不回来了（git 会连带把它的 reflog 一起清掉）。" +
        "确认框里给不出它的 commit hash —— 后端没有任何接口返回 stash 的 sha。",
      planPath: "/api/history/plan/stash-drop?index=" + encodeURIComponent(i),
      preview: function (p) {
        var s = p.stash || {};
        /* 预演本身**不返回** raw 行，raw 只在 /api/history/stashes 列表里有。
           不去列表里捞这一条，"原始行"就永远是空的 —— 而它是这里唯一
           还能看到 sha 形状的东西。 */
        var cached = stashByIndex(i);
        var raw = s.raw || (cached && cached.raw) || "";
        var h = '<div class="hp-kv">' +
          row2("引用", '<code>' + esc(s.ref || "") + "</code>") +
          row2("说明", esc(s.subject || "")) +
          row2("原始行", raw
            ? '<code>' + esc(raw) + "</code>"
            : '<span class="hp-dim">(列表里也没有这一条的 raw)</span>') +
          row2("剩余 stash 条数", esc(p.stash_count)) +
          "</div>";
        h += note("err", escMd(p.warning || ""));
        h += note("warn", "<b>后端缺陷（已实测）：</b>" +
          "<code>GET /api/history/plan/stash-drop</code> 的 <code>stash</code> 字段里" +
          "<b>没有这个 stash 的提交 hash</b>。它给的 <code>name</code> 会被 git stash 的" +
          "reflog 文案污染（<code>-m</code> 存的变成 <code>On &lt;分支&gt;</code>，" +
          "不带的变成 <code>WIP on &lt;分支&gt;</code>），" +
          "而全仓<b>没有任何接口</b>能取到 stash 的 sha。" +
          "所以这一条<b>只能显示 git 自己的 reflog 行原文</b>，无法显示被删对象的 hash。");
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/stash/drop", {
          method: "POST", body: { index: parseInt(i, 10), confirmed: true }
        });
      }
    });
  }

  /* ── 分支 / 标签 ── */
  function planBranchDelete(name) {
    var n = String(name);
    openPending({
      title: "删除分支 " + n,
      danger: true,
      requireWord: "delete-branch",
      dangerNote: "删掉这个分支之后，它的指向只能靠 git reflog 找回来，" +
        "而 reflog 会过期。没并进任何分支的提交会彻底消失。",
      planPath: "/api/history/plan/branch-delete?name=" + encodeURIComponent(n),
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("分支", esc(p.name || n)) +
          row2("hash", '<span class="hp-mono">' + esc(p.sha || "?") + "</span> " +
            copyBtn(p.sha || "", "复制")) +
          row2("是否当前分支", p.current ? "<b>是</b>" : "否") +
          row2("是否默认分支", p.is_default ? "<b>是</b>" : "否") +
          row2("未合并提交数", esc(p.unmerged_count == null ? "?" : p.unmerged_count) +
            (p.unmerged_count ? ' <span class="hp-red">未合并，删除会丢提交</span>' : "")) +
          row2("仓内分支总数", esc(p.branch_count)) +
          "</div>";
        var cs = asArray(p.commits);
        if (cs.length) {
          h += '<div class="hp-mini"><div class="hp-k">未合并提交（最多列 20 条）</div><ul>';
          for (var i = 0; i < cs.length; i++) {
            var c = cs[i] || {};
            h += "<li><span class=\"hp-mono\">" + esc(shortSha(c.sha || c.short)) +
              "</span> " + esc(c.subject || "") + "</li>";
          }
          h += "</ul></div>";
        }
        h += note(p.safe ? "warn" : "err", escMd(p.warning || ""));
        if (p.unmerged_count) {
          h += note("err", "这个分支有 <b>" + esc(p.unmerged_count) +
            "</b> 个未合并提交。<b>本面板不提供 force 强删按钮</b> —— " +
            "后端 <code>force=True</code> + <code>confirmed=True</code> 能做到，" +
            "但那需要你直接调 API 并明确写 force（命令行等价 <code>git branch -D</code>）。");
        }
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/branch/delete", {
          method: "POST", body: { name: n, force: false, confirmed: true }
        });
      }
    });
  }

  function planTagDelete(name) {
    var n = String(name);
    openPending({
      title: "删除标签 " + n,
      danger: true,
      requireWord: "delete-tag",
      dangerNote: "删掉这个标签之后，本地就没有它了（远端不受影响）。" +
        "轻量标签只是给某个提交起了个名，那个提交本身不受影响。",
      planPath: "/api/history/plan/tag-delete?name=" + encodeURIComponent(n),
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("标签", esc(p.name || n)) +
          row2("hash（被删对象）", '<span class="hp-mono">' + esc(p.sha || "?") + "</span> " +
            copyBtn(p.sha || "", "复制")) +
          "</div>";
        h += note("err", escMd(p.warning || ""));
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/tag/delete", {
          method: "POST", body: { name: n, confirmed: true }
        });
      }
    });
  }

  /* 改名需要一个新名字。先问一次，问到值再开抽屉 ——
     promptNewName 只能调一次：调两次就是弹两次框。 */
  function planBranchRename(oldName) {
    var o = String(oldName);
    var n = promptNewName(o);
    if (!n || n === o) {
      toast("warn", "没有填新名字，或新旧相同 —— 后端会把这种情况判成" +
        "「什么都不用做」并拒绝执行。");
      return;
    }
    openPending({
      title: "给分支 " + o + " 改名",
      danger: true,
      /* 改名能改回去，但后端把它登记成破坏性操作（DESTRUCTIVE_OPS），锁保持一致。 */
      requireWord: "rename-branch",
      dangerNote: "改名不改历史、不删提交，做完随时能改回去。",
      planPath: "/api/history/plan/branch-rename?old=" + encodeURIComponent(o) +
        "&new=" + encodeURIComponent(n),
      preview: function (p) {
        var h = '<div class="hp-kv">' +
          row2("旧分支", esc(p.old || o)) +
          row2("新分支", esc(p.new || n)) +
          row2("hash（改名不改变它）",
            '<span class="hp-mono">' + esc(p.old_sha || "?") + "</span> " +
            copyBtn(p.old_sha || "", "复制")) +
          "</div>";
        h += note(p.safe ? "warn" : "err", escMd(p.warning || ""));
        h += note("warn", "改名不改历史也不删提交，做完随时能改回去。" +
          "命令等价 <code>git branch -m " + esc(o) + " " + esc(n) + "</code>。");
        return h;
      },
      exec: function () {
        return S.ctx.api("/api/history/branch/rename", {
          method: "POST", body: { old: o, new: n, confirmed: true }
        });
      }
    });
  }

  function promptNewName(oldName) {
    var s = "";
    try { s = window.prompt("把分支 " + oldName + " 改叫什么？", oldName + "-new") || ""; }
    catch (e) { s = ""; }
    return s.trim();
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 事件：一律委托，零内联 onclick
   * ═══════════════════════════════════════════════════════════════════ */

  function onClick(ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var node = t.closest("[data-act]");
    if (!node || !S.el.contains(node)) return;
    var act = node.getAttribute("data-act");

    switch (act) {
      case "tab":
        S.tab = node.getAttribute("data-tab") || "log";
        S.gen++;               /* 上一页还在飞的请求：回来时作废 */
        render();
        loadTab(S.tab);
        return;
      case "reload":
        render();
        loadTab(S.tab, true);
        return;
      case "copy":
        copyToClipboard(node.getAttribute("data-copy") || "");
        return;
      case "pick-revert":
        S.tab = "danger";
        render();
        var n = S.el.querySelector("#hp-revert-sha");
        if (n) n.value = node.getAttribute("data-sha") || "";
        loadTab("danger");
        return;

      /* 分支 / 标签 */
      case "new-branch": return doNewBranch();
      case "new-tag": return doNewTag();
      case "plan-del-branch": return planBranchDelete(node.getAttribute("data-name"));
      case "plan-del-tag": return planTagDelete(node.getAttribute("data-name"));
      case "plan-rename": return planBranchRename(node.getAttribute("data-name"));

      /* stash */
      case "plan-stash": return planStash();
      case "plan-stash-drop": return planStashDrop(node.getAttribute("data-idx"));
      case "stash-view": return stashView(node.getAttribute("data-idx"));
      case "stash-apply":
        return post("/api/history/stash/apply", { index: num(node, "data-idx") },
          "已贴回 stash（stash 条目仍在，可再 apply 或 drop）。");
      case "stash-pop":
        return post("/api/history/stash/pop", { index: num(node, "data-idx") },
          "已贴回并删除该 stash。");

      /* reflog */
      case "plan-reflog-restore":
        return planReflogRestore(node.getAttribute("data-idx"),
          node.getAttribute("data-mode"));

      /* 危险区 */
      case "plan-amend": return planAmend();
      case "plan-revert": return planRevert();
      case "plan-reset": return planReset();
      case "revert-abort":
        /* 没有预演接口（后端只给了 do_revert_abort），它本身也不是破坏性操作
           —— 退回去比卡在中间好。跑挂了后端会说「没有进行中的 revert」。 */
        return post("/api/history/revert/abort", {},
          "<b>已中止进行中的 revert</b>（git revert --abort）。");
      case "exec-pending": return execPending();
      case "cancel-pending":
        S.pending = null; render(); return;
      default: return;
    }
  }

  function num(node, attr) {
    var v = parseInt(node.getAttribute(attr), 10);
    return isNaN(v) ? 0 : v;
  }

  function onChange(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    /* reset 模式：状态存进 S，红字提示只改这一小块（不整块重绘 ——
       重绘会把用户正在打的字、刚选的 radio 全冲掉）。 */
    if (t.name === "hp-reset-mode") {
      S.resetMode = String(t.value || "");
      var hint = S.el.querySelector("#hp-reset-hint");
      if (hint) hint.innerHTML = resetHintHtml();
    }
  }

  function copyToClipboard(text) {
    if (!text) return;
    var done = function () { toast("ok", "已复制 <b>" + esc(text) + "</b>"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "readonly");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      done();
    } catch (e) {
      toast("warn", "复制失败，请手动选中 <b>" + esc(text) + "</b>。");
    }
  }

  function stashView(idx) {
    var i = String(idx);
    if (S.stashView && String(S.stashView.index) === i) { S.stashView = null; render(); return; }
    if (!isLive()) return;
    var g = S.gen;
    S.inflight++;
    setBusy(true);
    /* 注意：GET /api/history/stash 在 git 失败时会返回 HTTP 200 + ok:false，
       所以必须 tolerateOkFalse 自己判 ok，否则错误信息会被吞成 "HTTP 200"。 */
    S.ctx.api("/api/history/stash?index=" + encodeURIComponent(i) + "&stat=1",
      { tolerateOkFalse: true }).then(function (d) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g !== S.gen || !isLive()) return;
      if (!d || d.ok !== true) {
        showError("读取 stash 补丁", new Error((d && d.error) || "后端没有返回补丁文本"));
        return;
      }
      S.stashView = { index: i, text: d.text };
      render();
    }).catch(function (e) {
      S.inflight--;
      setBusy(S.inflight > 0);
      if (g === S.gen && isLive()) showError("读取 stash 补丁", e);
    });
  }

  function doNewBranch() {
    var name = valOf("hp-new-branch").trim();
    if (!name) { toast("warn", "分支名不能为空。"); return; }
    post("/api/branch", { name: name },
      "已新建并切换到分支 <b>" + name + "</b>。").then(function () {
        var n = S.el && S.el.querySelector("#hp-new-branch");
        if (n) n.value = "";
      });
  }

  function doNewTag() {
    var name = valOf("hp-new-tag").trim();
    if (!name) { toast("warn", "标签名不能为空。"); return; }
    var msg = valOf("hp-new-tag-msg");
    var force = checkedOf("hp-new-tag-force");
    var body = { name: name, force: force };
    if (msg !== "") body.message = msg;
    post("/api/history/tag/create", body,
      "已创建标签 <b>" + esc(name) + "</b>").then(function () {
        var a = S.el && S.el.querySelector("#hp-new-tag");
        var b = S.el && S.el.querySelector("#hp-new-tag-msg");
        if (a) a.value = "";
        if (b) b.value = "";
      });
  }

  /* 把预演里最要紧的几行摘成 bullets，喂给宿主的 confirmDanger ——
     宿主模态只吃纯文本，这里必须**先转义再拼**，因为它内部还会再 esc 一次，
     两层 esc 会把中文标点之外的东西搞花（所以只放纯文本，不放标签）。 */
  function planBullets(p) {
    if (!p) return [];
    var out = [];
    var push = function (k, v) {
      if (v !== undefined && v !== null && v !== "") out.push(k + "：" + v);
    };
    push("操作", p.op);
    push("将执行", p.mode ? "--" + p.mode : "");
    if (p.old_short || p.old_sha) {
      push("旧提交", (p.old_short || shortSha(p.old_sha)) + " " + (p.old_subject || ""));
    }
    if (p.target_short || p.target_sha) {
      push("将撤销", (p.target_short || shortSha(p.target_sha)) + " " +
        (p.target_subject || ""));
    }
    if (p.target && p.target.sha) {
      push("目标", (p.target.short || shortSha(p.target.sha)) + " " +
        (p.target.action || ""));
    }
    if (p.stash && p.stash.ref) push("stash", p.stash.ref);
    if (p.name) push("对象", p.name);
    if (p.short) push("提交", p.short + " " + (p.subject || ""));
    if (p.sha) push("sha", p.sha);
    if (p.old_sha && !p.old_short) push("sha", p.old_sha);
    if (p.unmerged_count) push("未合并提交", p.unmerged_count);
    if (p.blocked) out.push("⚠ 现在不能做：" + p.blocked);
    return out;
  }

  /* ═══════════════════════════════════════════════════════════════════
   * mount / unmount
   * ═══════════════════════════════════════════════════════════════════ */

  var STYLE_ID = "hp-style";

  function injectStyle(doc) {
    if (!doc || doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement("style");
    s.id = STYLE_ID;
    s.textContent = [
      ".hp{display:flex;flex-direction:column;min-height:0;height:100%;font-size:13px}",
      ".hp-bar{flex:0 0 auto;display:flex;align-items:center;gap:8px;",
      "  padding:6px 8px;border-bottom:1px solid var(--border,#3c3c3c);",
      "  background:var(--bg-side,#252526);flex-wrap:wrap}",
      ".hp-tabs{display:flex;gap:4px;flex-wrap:wrap}",
      ".hp-tab{padding:4px 11px;border-radius:3px;border:1px solid transparent;",
      "  background:transparent;color:var(--fg-dim,#ccc);cursor:pointer;font-size:12px}",
      ".hp-tab:hover:not(.active){background:var(--bg-hover,#333);color:var(--fg,#eee)}",
      ".hp-tab.active{background:var(--bg-active,#04395e);color:#fff;border-color:#0e639c}",
      ".hp-tab.danger{color:#f0a0a0}",
      ".hp-tab.danger.active{background:#5a1f1f;border-color:#a1260d;color:#ffd0d0}",
      ".hp-tools{margin-left:auto;display:flex;align-items:center;gap:8px}",
      ".hp-hint{font-size:11px;color:var(--fg-dim,#999)}",
      ".hp-body{flex:1 1 auto;min-height:0;overflow:auto;padding:10px 12px}",
      ".hp-card{border:1px solid var(--border,#3c3c3c);border-radius:4px;",
      "  padding:9px 11px;margin:0 0 12px}",
      ".hp-card h4,.hp-op h4,.hp-detail h4,.hp-pending h4{margin:0 0 8px;font-size:12px;",
      "  letter-spacing:.4px;color:var(--fg,#eee)}",
      ".hp-tbl{width:100%;border-collapse:collapse;font-size:12px}",
      ".hp-tbl th{text-align:left;font-weight:600;color:var(--fg-dim,#999);",
      "  font-size:11px;padding:3px 6px;border-bottom:1px solid var(--border,#3c3c3c)}",
      ".hp-tbl td{padding:3px 6px;border-bottom:1px solid rgba(255,255,255,.05);",
      "  vertical-align:top}",
      ".hp-trow.active{background:var(--bg-active,#04395e)}",
      ".hp-mono{font-family:Consolas,Monaco,monospace;font-size:11px}",
      ".hp-dim{color:var(--fg-dim,#999);font-size:11px}",
      ".hp-red{color:#f14c4c}",
      ".hp-subj{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hp-nowrap{white-space:nowrap;text-align:right}",
      ".hp-empty{padding:14px;text-align:center;color:var(--fg-dim,#999);font-size:12px}",
      ".hp-form{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin:8px 0 0}",
      ".hp-form label{font-size:11px;color:var(--fg-dim,#999)}",
      ".hp-form input[type=text]{background:var(--bg-input,#3c3c3c);color:var(--fg,#eee);",
      "  border:1px solid var(--border,#555);border-radius:2px;padding:4px 7px;",
      "  font-size:12px;min-width:170px;flex:1 1 170px;max-width:340px}",
      ".hp-chk{display:inline-flex;align-items:center;gap:4px;cursor:pointer}",
      ".hp-radio{display:flex;align-items:center;gap:5px;cursor:pointer;font-size:12px;",
      "  padding:4px 6px;border:1px solid var(--border,#3c3c3c);border-radius:3px;",
      "  flex:1 1 320px;margin-bottom:5px}",
      ".hp-radio-hard{border-color:#7a2b2b}",
      ".hp-radio-hard input{accent-color:#a1260d}",
      ".hp-detail{border:1px solid var(--border,#3c3c3c);border-radius:4px;",
      "  padding:9px 11px;margin-top:10px}",
      ".hp-detail-acts{display:flex;align-items:center;gap:8px;margin-top:9px;flex-wrap:wrap}",
      ".hp-kv{margin:0}",
      ".hp-kvrow{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.05)}",
      ".hp-k{flex:0 0 190px;color:var(--fg-dim,#999);font-size:11px}",
      ".hp-v{flex:1 1 auto;min-width:0;word-break:break-word}",
      ".hp-pre{background:#1e1e1e;border:1px solid var(--border,#3c3c3c);border-radius:3px;",
      "  padding:7px 9px;font-family:Consolas,Monaco,monospace;font-size:11px;",
      "  white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto;margin:5px 0}",
      ".hp-mini{margin:8px 0}",
      ".hp-mini ul{margin:4px 0 0;padding-left:20px;font-size:11px}",
      ".hp-danger{border:2px solid #7a2b2b;border-radius:5px;padding:10px 12px;",
      "  background:rgba(120,25,10,.07)}",
      ".hp-danger-head{background:#3a1a1a;color:#ffb3b3;padding:8px 10px;",
      "  border-radius:3px;font-size:12px;line-height:1.75;margin-bottom:10px}",
      ".hp-op{border-top:1px solid rgba(255,255,255,.08);padding-top:9px;margin-top:9px}",
      ".hp-desc{margin:0 0 4px;font-size:11px;color:var(--fg-dim,#999)}",
      ".hp-note-warn{margin:8px 0;padding:7px 10px;border-left:3px solid #a1260d;",
      "  background:rgba(120,25,10,.16);font-size:12px;line-height:1.7;border-radius:0 3px 3px 0}",
      ".hp-pending{margin-top:12px;border:2px solid #0e639c;border-radius:5px;",
      "  padding:10px 12px;background:rgba(14,99,156,.08)}",
      ".hp-pending.danger{border-color:#a1260d;background:rgba(120,25,10,.09)}",
      ".hp-pending-acts{display:flex;align-items:center;gap:9px;margin-top:10px;flex-wrap:wrap}",
      ".hp-raw{margin-top:8px}",
      ".hp-raw summary{cursor:pointer;font-size:11px;color:var(--fg-dim,#999)}"
    ].join("");
    (doc.head || doc.documentElement).appendChild(s);
  }

  function mount(el, ctx, restore) {
    if (!el || !ctx) return;
    if (typeof ctx.api !== "function" || typeof ctx.esc !== "function") {
      /* esc 缺了绝不能自己写一个顶上 —— 那个「不转义引号 + 内联 onclick」
         的 XSS 就是这么来的。宁可面板不工作，也不用一个来历不明的转义函数。 */
      el.innerHTML = '<div class="hp-body"><div class="note err">' +
        "历史面板启动失败：宿主没有注入 ctx.api / ctx.esc。" +
        "本面板<b>不会</b>自己实现一个 esc 顶替（那正是刚修掉的 XSS 的成因）。</div></div>";
      return;
    }
    S.el = el;
    S.ctx = ctx;
    S.esc = ctx.esc;
    S.loaded = {};
    S.data = {};
    S.selected = null;
    S.stashView = null;
    S.pending = null;
    S.resetMode = "";      /* 重新挂载时一个模式都不选 —— 见下面 restore 那段 */
    S.gen++;
    S.inflight = 0;
    S.busy = false;

    /* restore 是宿主存下来的上一次 unmount 的返回值。逐项白名单取用：
       照单全收等于把一个外部对象直接变成渲染输入。 */
    var r = (restore && typeof restore === "object") ? restore : {};
    var tid = -1;
    for (var i = 0; i < TABS.length; i++) {
      if (TABS[i][0] === r.tab) { tid = i; break; }
    }
    S.tab = tid >= 0 ? TABS[tid][0] : "log";
    if (r.selected && typeof r.selected.sha === "string" && r.selected.sha) {
      S.selected = {
        sha: r.selected.sha, author: r.selected.author || "",
        date: r.selected.date || "", subject: r.selected.subject || ""
      };
    }
    if (r.stashView && r.stashView.index != null) {
      S.stashView = { index: r.stashView.index, text: r.stashView.text || "" };
    }
    /* ⚠ resetMode **故意不恢复**（就算宿主把它存了回来也不读）。
       restore 是「保视图状态」：在哪个 tab、选中哪条、展开哪个 stash。
       reset 的模式选择不是视图状态 —— 它是下一步就可能执行破坏性操作的输入。
       一次 amend 和一次 reset --hard 的差别，不该由「面板重新打开了一下」决定。 */
    S.branchSeen = (typeof ctx.status === "function")
      ? String((ctx.status() || {}).branch || "") : "";

    injectStyle(el.ownerDocument || document);

    el.innerHTML =
      '<div class="hp">' +
      '<div class="hp-bar" data-role="bar"></div>' +
      '<div class="hp-body" data-role="body"></div>' +
      "</div>";
    S.barEl = el.querySelector('[data-role="bar"]');
    S.bodyEl = el.querySelector('[data-role="body"]');

    el.addEventListener("click", onClick);
    el.addEventListener("change", onChange);
    el.addEventListener("click", onPick);
    /* 宿主只在「本面板可见」时喂 status，unmount 之后自动丢弃这个回调 ——
       「不可见时零开销」由宿主兜底，我只管回调里别乱发请求。 */
    if (typeof ctx.onStatus === "function") {
      try { ctx.onStatus(onHostStatus); } catch (e) { /* 注册不上就算了 */ }
    }
    /* setPill 故意不调：宿主每次 refresh() 都把 history 的角标清成 null
       （index.html 的 updatePills）。面板自己点亮它，用户看到的只会是
       角标闪一下又被抹掉。要角标就得改宿主，这里不跟它抢。 */

    /* mount 里**不预取**：只有 render + 挂监听。用户点「刷新」或切 tab 才发请求。 */
    render();
  }

  /* 返回值会被宿主记住，下次 mount 的第三个参数原样送回来。 */
  function unmount() {
    var keep = {
      tab: S.tab,
      selected: S.selected ? {
        sha: S.selected.sha, author: S.selected.author || "",
        date: S.selected.date || "", subject: S.selected.subject || ""
      } : null,
      stashView: S.stashView ? {
        index: S.stashView.index, text: S.stashView.text || ""
      } : null
    };
    if (S.el) {
      S.el.removeEventListener("click", onClick);
      S.el.removeEventListener("change", onChange);
      S.el.removeEventListener("click", onPick);
    }
    /* 把 in-flight 的结果作废掉：它们回来时 gen 对不上，也不再写任何 DOM。 */
    S.gen++;
    S.inflight = 0;
    S.el = null;
    S.bodyEl = null;
    S.barEl = null;
    S.ctx = null;
    S.data = {};
    S.loaded = {};
    S.selected = null;
    S.stashView = null;
    /* 确认抽屉**不留到下一次挂载** —— 它背后的宿主模态可能已经没了，
       留着等于让用户对着一个「上次的」确认框点确定。 */
    S.pending = null;
    S.resetMode = "";
    return keep;
  }

  /* 时间线行选中：委托到行上（行本身带 data-act）。这里补一个独立监听，
     避免把「选中」和「按钮」混在同一套 data-act 里。 */
  function onPick(ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var row = t.closest('[data-pick-log]');
    if (!row || !S.el || !S.el.contains(row)) return;
    var sha = row.getAttribute("data-pick-log");
    var rows = asArray(S.data.log), hit = null;
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].sha) === sha) { hit = rows[i]; break; }
    }
    if (!hit) return;
    S.selected = { sha: hit.sha, author: hit.author, date: hit.date, subject: hit.subject };
    render();
  }

  /* ═══════════════════════════════════════════════════════════════════
   * 注册
   * ═══════════════════════════════════════════════════════════════════ */

  var panel = {
    id: "history",
    title: "历史",
    mount: function (el, ctx, restore) { mount(el, ctx, restore); },
    unmount: unmount
  };

  var host = window.AigitPanels = window.AigitPanels || {};
  if (typeof host.register === "function") {
    host.register(panel);
  } else {
    /* 懒加载竞态：宿主还没定义 register 就先把自己排上，而不是静默丢掉。 */
    (host._pending = host._pending || []).push(panel);
  }
})();