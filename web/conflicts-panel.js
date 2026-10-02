/*!
 * conflicts-panel.js —— VSCode「源代码管理」风格的冲突解决面板。
 *
 * 只做渲染和编排。所有数据与写操作都走 HTTP(后端 server.py + conflicts.py),
 * 面板本身不持有任何 git 概念的实现 —— 不跑 git、不自己判断三方内容对不对。
 * 工作区内容、三方全文、块边界、sha256 全部由后端给。
 *
 * ── 与宿主(index.html)的契约 ───────────────────────────────────────────
 *   window.AigitPanels.register({
 *     id: 'conflicts', title: '冲突',
 *     mount(el, ctx), unmount()
 *   })
 *   ctx = { api(path, opts), esc(s), toast(kind, html), noteHtml(kind, html, closable),
 *           refresh(alsoBranches) }
 *
 *   opts = { method: "GET"|"POST", body: <对象> }。ctx.api 在 ok:false 时**抛**,
 *   错误对象是 Error,e.message 就是后端原文。ctx.noteHtml 返回一整条提示的 HTML。
 *
 *   没有 ctx.esc / ctx.noteHtml 时退回到本文件内的等价实现,好让这个模块能被
 *   单独打开一个测试页自测(测试页按验收要求已删除,不在 web/ 里)。
 *   ctx.toast 没有被用到:提示走面板自己的提示条(宿主 toast 落在 diff 面板上,
 *   冲突面板的消息不该跑到那儿去)。
 *
 * ── 三条硬需求怎么落的 ─────────────────────────────────────────────────
 *   1) 面板不是任何功能的唯一入口:六个冲突接口全是普通 HTTP,不经过本文件。
 *      空状态直接写明「用 合并 按钮 或 POST /api/merge 制造冲突」。
 *   2) 不耗 UI 性能:mount() **不取任何数据**,只画骨架 + 挂一个
 *      IntersectionObserver。第一次真正可见才拉数据,之后只有用户点
 *      「刷新」才再拉 —— 全文件没有 setInterval / setTimeout / rAF 轮询,
 *      不可见时观察器只回调「不做事」。unmount() 断观察器 + 作废在途请求
 *      (用 generation 号,不是 AbortController:ctx.api 是宿主给的,不一定
 *       支持 signal)。
 *   3) 复用而不复制:esc() 一律用 ctx.esc()。**没有任何内联 onclick 拼字符串**;
 *      列表行和块按钮在 HTML 里只放**下标**(纯数字),路径和块数据走闭包,
 *      全部靠事件委托 + dataset 取。等于是说用户数据一次都不进 HTML 属性。
 *
 * ── 后端契约(2026-10 实测,不是照抄文档)────────────────────────────────
 *   GET  /api/conflict/state              -> {ok,inMerge,mergeHead,head,currentBranch,
 *                                             branches[],defaultMessage,remaining,files[]}
 *   GET  /api/conflict/list               -> {ok,inMerge,count,files[{path,name,columns,
 *                                             regionCount,noBase,binary,manuallyEdited,
 *                                             verified,oursLabel,theirsLabel,...}]}
 *   GET  /api/conflict/inspect?file=<rel>  -> {ok,path,noBase,columns,mergeBase,
 *                                             sides{ours,theirs,base}{text,exists,deleted,label},
 *                                             worktree{text,sha256,hasMarkers,manuallyEdited},
 *                                             regions[{oursMarkerLine,sepLine,theirsMarkerLine,
 *                                             oursText,theirsText,baseText,baseLocatedText,
 *                                             oursLineStart,oursLineEnd,verified}]}
 *   POST /api/conflict/resolve  {file, content, expect_sha256?, encoding?}
 *   POST /api/conflict/choose   {file, side:"ours"|"theirs"|"base"}
 *   POST /api/conflict/delete   {file, expect_sha256?}
 *   POST /api/conflict/commit   {message?}
 *   POST /api/conflict/abort    {}
 *
 *   ⚠ 字段名是 **file / expect_sha256**(下划线)。仓里那个
 *   web/conflicts-demo.html 发的是 {path, expectSha256},后端读不到 file,
 *   整条写路径实测全被拒。本文件严格用下划线名。
 *   ⚠ resolve 的 content **必须显式给**;缺键 400。空串是合法的「清空文件」。
 *   ⚠ 这几个 POST **不返回宿主 status**(不像别的 POST 走 _ok_with_status),
 *     所以写完必须自己 ctx.refresh()。
 *   ⚠ 没有任何「取消解决 / 退回冲突态」的接口。resolve 之后 git index 里
 *     的 U 条目就没了,POST /api/unstage 也退不回来(实测 ls-files -u 仍为空)。
 *   ⚠ state 内部会调 list_conflicts,两者一样贵。面板只调 list —— state 里
 *     独有的 branches/defaultMessage 只有「完成合并」时用得上,而那一步用
 *     .git/MERGE_MSG 的默认信息就够,不必为了显示一行横幅多花一次全量遍历。
 */

(function (global) {
  "use strict";

  var PANEL_ID = "conflicts";
  var PANEL_TITLE = "冲突";
  var STYLE_ID = "aigit-conflicts-panel-style";
  var STORE_KEY = "aigit.conflicts.session.v1";

  /* 单侧全文最多渲染多少字符。超了截断并写明截了多少 —— 一个几千行的
     文件不该在点开时建出几十万个 DOM 节点。结果栏(textarea)不截:
     它是写回的源,截了就是数据损坏。 */
  var MAX_SIDE_CHARS = 20000;

  /* ═══════════════════════════════════════════════════════════════════
     小工具
     ═══════════════════════════════════════════════════════════════════ */

  /* 只在 ctx.esc 缺席时用。引号也要转:不转的话任何「拼进属性」的写法
     就是注入点(和 conflicts-demo.html 里那段注释同一个理由)。 */
  function fallbackEsc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* 拆行规则必须和 conflicts.py 的 _split_lines 一致:只按 \n 拆,
     末尾换行不算一行。**不用 splitlines()** —— 它会在 lone \r、\x0b、
     \u2028 处拆行,文件内容是用户的数据,拆错位置等于替他改坏文件。 */
  function splitLines(text) {
    if (text === "") return { lines: [], trailing: false };
    var trailing = text.charAt(text.length - 1) === "\n";
    var lines = text.split("\n");
    if (trailing) lines.pop();
    return { lines: lines, trailing: trailing };
  }
  function joinLines(lines, trailing) {
    return lines.join("\n") + (trailing ? "\n" : "");
  }
  function clampText(text) {
    if (text === null || text === undefined) return { text: "", cut: 0 };
    var s = String(text);
    return s.length > MAX_SIDE_CHARS
      ? { text: s.slice(0, MAX_SIDE_CHARS), cut: s.length - MAX_SIDE_CHARS }
      : { text: s, cut: 0 };
  }
  function hhmm(ts) {
    try {
      var d = new Date(ts);
      return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    } catch (e) { return ""; }
  }

  /* ═══════════════════════════════════════════════════════════════════
     样式
     ═══════════════════════════════════════════════════════════════════ */

  var CSS = [
    /* .cp 只做命名空间与最小修饰 —— **不接管宿主的布局**。
       真正的 flex 列布局挂在内层 .cp-in 上:宿主给的那个 el 属于宿主的
       标签页骨架,在这里强设 display:flex / height:100% 会把别人的布局顶掉。 */
    ".cp{--cp-ours:#0b3d2e;--cp-theirs:#3d2a0b;--cp-base:#252526;",
    "     --cp-line:#3c3c3c;--cp-dim:#858585;--cp-bad:#ff7979}",
    ".cp-in{display:flex;flex-direction:column;height:100%;min-height:0;",
    "        color:var(--fg,#d4d4d4);",
    "        font:13px/1.5 \"Cascadia Code\",Consolas,monospace}",
    ".cp *{box-sizing:border-box}",
    ".cp-hd{display:flex;gap:8px;align-items:center;flex-wrap:wrap;",
    "       padding:6px 10px;border-bottom:1px solid var(--cp-line);flex:0 0 auto}",
    ".cp-hd .cp-t{font-weight:600}",
    ".cp-hd .cp-sp{flex:1}",
    ".cp-btn{background:#2d2d2d;color:inherit;border:1px solid #555;border-radius:3px;",
    "         padding:3px 9px;cursor:pointer;font:inherit;white-space:nowrap}",
    ".cp-btn:hover:not(:disabled){background:#3d3d3d}",
    ".cp-btn:disabled{opacity:.4;cursor:default}",
    ".cp-btn.pri{background:#1d5c33;border-color:#2d8a4a}",
    ".cp-btn.pri:hover:not(:disabled){background:#26743f}",
    ".cp-btn.danger{border-color:#8a3b3b}",
    ".cp-btn.danger:hover:not(:disabled){background:#4a2020}",
    ".cp-btn.armed{background:#7a2020;border-color:#c04040;font-weight:600}",
    ".cp-btn.sm{padding:1px 6px;font-size:11px}",
    ".cp-pill{padding:1px 8px;border-radius:10px;font-size:11px;",
    "           border:1px solid var(--cp-line);color:var(--cp-dim)}",
    ".cp-pill.on{background:var(--cp-ours);border-color:#1d6b4f;color:#b0ffd8}",
    ".cp-tag{font-size:10px;padding:0 5px;border-radius:8px;background:#3c3c3c}",
    ".cp-tag.nb{background:#5c3d0b;color:#ffd479}",
    ".cp-tag.uf{background:#0b5c3d;color:#79ffb0}",
    ".cp-tag.bad{background:#5c0b0b;color:#ff7979}",
    ".cp-tag.man{background:#3d3d0b;color:#d4d479}",
    ".cp-note{margin:6px 10px 0;flex:0 0 auto}",
    ".cp-note .note{margin:0}",
    ".cp-body{display:flex;flex:1 1 auto;min-height:0}",
    ".cp-list{width:250px;flex:0 0 250px;overflow:auto;",
    "          border-right:1px solid var(--cp-line)}",
    ".cp-grp{padding:3px 10px;background:#252525;color:var(--cp-dim);font-size:11px;",
    "         position:sticky;top:0;border-bottom:1px solid #2f2f2f;z-index:1}",
    ".cp-row{padding:5px 10px;border-bottom:1px solid #2a2a2a;cursor:pointer}",
    ".cp-row:hover{background:#2a2a2a}",
    ".cp-row.sel{background:#0d3b5c}",
    ".cp-row .n{display:flex;gap:5px;align-items:center;flex-wrap:nowrap;",
    "             overflow:hidden}",
    ".cp-row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".cp-row .m{color:var(--cp-dim);font-size:11px;margin-top:1px;",
    "             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".cp-detail{flex:1 1 auto;min-width:0;overflow:auto;padding-bottom:12px}",
    ".cp-empty{padding:20px;color:var(--cp-dim);max-width:78ch;white-space:pre-wrap}",
    ".cp-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;",
    "        padding:6px 10px;border-bottom:1px solid var(--cp-line)}",
    ".cp-bar .cp-t{font-weight:600;overflow:hidden;text-overflow:ellipsis}",
    ".cp-msg{padding:4px 10px;color:var(--cp-dim);font-size:11px;",
    "        border-bottom:1px solid #2a2a2a;white-space:pre-wrap}",
    /* 三栏:ours | result(可编辑) | theirs —— 顺序与 VSCode 合并编辑器一致,
       可编辑的结果栏在中间。base 是第四栏,默认折叠。 */
    ".cp-cols{display:flex;align-items:stretch;border-bottom:1px solid var(--cp-line)}",
    ".cp-col{flex:1 1 0;min-width:0;border-right:1px solid var(--cp-line);",
    "        display:flex;flex-direction:column}",
    ".cp-col:last-child{border-right:none}",
    ".cp-col>h4{margin:0;padding:3px 8px;font-size:11px;font-weight:600;",
    "           background:#252525;border-bottom:1px solid var(--cp-line);",
    "           display:flex;gap:6px;align-items:center}",
    ".cp-col>pre{margin:0;padding:6px 8px;overflow:auto;flex:1 1 auto;",
    "             min-height:150px;max-height:46vh;font:inherit;white-space:pre;",
    "             tab-size:4}",
    ".cp-col.ours>pre{background:var(--cp-ours)}",
    ".cp-col.theirs>pre{background:var(--cp-theirs)}",
    ".cp-col.base>pre{background:var(--cp-base)}",
    ".cp-col.ta>textarea{flex:1 1 auto;min-height:150px;max-height:46vh;",
    "       width:100%;margin:0;padding:6px 8px;background:#1a1a1a;color:inherit;",
    "       border:none;resize:vertical;font:inherit;white-space:pre;",
    "       overflow:auto;tab-size:4}",
    ".cp-col.ta.edited>textarea{background:#241f14}",
    ".cp-gone{padding:14px 10px;color:var(--cp-bad);font-style:italic}",
    ".cp-cut{padding:2px 8px;font-size:11px;color:var(--cp-dim)}",
    /* 逐块卡片:O(块数) 个节点,不随文件行数增长 */
    ".cp-rg{border-bottom:1px solid #2a2a2a}",
    ".cp-rg>summary{padding:5px 10px;cursor:pointer;display:flex;gap:7px;",
    "                 align-items:center;flex-wrap:wrap;background:#202020}",
    ".cp-rg[open]>summary{border-bottom:1px solid #2a2a2a}",
    ".cp-rg .cp-acts{margin-left:auto;display:flex;gap:5px}",
    ".cp-rg pre{margin:0;padding:5px 10px;font:inherit;white-space:pre-wrap;",
    "              word-break:break-word;max-height:220px;overflow:auto}",
    ".cp-rg pre.ours{background:var(--cp-ours)}",
    ".cp-rg pre.theirs{background:var(--cp-theirs)}",
    ".cp-rg pre.pick{background:#0d2a3b}",
    ".cp-rg .cp-lab{font-size:10px;color:var(--cp-dim);padding:1px 10px 0}",
    ".cp-ft{flex:0 0 auto;padding:5px 10px;border-top:1px solid var(--cp-line);",
    "        display:flex;gap:8px;align-items:center;flex-wrap:wrap;",
    "        color:var(--cp-dim);font-size:11px}"
  ].join("");

  function ensureStyle(doc) {
    if (!doc || doc.getElementById(STYLE_ID)) return;
    var st = doc.createElement("style");
    st.id = STYLE_ID;
    st.textContent = CSS;
    (doc.head || doc.documentElement).appendChild(st);
  }

  /* ═══════════════════════════════════════════════════════════════════
     面板
     ═══════════════════════════════════════════════════════════════════ */

  function create() {
    var I = {
      root: null, ctx: null, doc: null,
      esc: fallbackEsc,
      noteHtml: null,                    // 宿主给了就用宿主的
      mounted: false, gen: 0,
      io: null,                          // IntersectionObserver
      everVisible: false,
      loading: false,
      list: null,                        // /api/conflict/list 的返回
      rows: [],                          // 列表行 -> 数据(下标映射)
      sel: null,                         // 当前打开的冲突文件路径
      detail: null,                      // inspect 的返回
      picks: null,                       // picks[i] = ours|theirs|both|base|none
      result: "",                        // 结果栏当前文本
      edited: false,                     // 用户手工改过结果栏
      store: null                        // 已解决清单(见 loadStore)
    };

    /* -- 存取「已解决」清单 ----------------------------------------------
     * 后端**没有**「这次合并里我解决了哪些」的接口:resolve 之后该路径在
     * `git diff --diff-filter=U` 里就消失了,/api/conflict/list 立刻不再
     * 报它,索引里也只留下一个普通 M(和任何别的暂存改动长得一样)。
     * 所以这份清单只能由前端记。
     * 记在 sessionStorage 里(刷新页面不丢,换标签页不共享 —— 这是有意的:
     * 它只是「本次合并里点过解决」的 UI 状态,不是仓库状态)。
     * 什么时候清:
     *   - /api/conflict/list 报 inMerge=false(不在合并态)→ 清;
     *   - commit / abort 成功 → 清;
     *   - 写操作返回的 mergeHead 和记着的不一样(换了次合并)→ 清。
     * 残留边角:两次合并之间用户一次都没刷新过面板,旧清单会带过来;
     * 那种情况下第一次写操作返回的 mergeHead 就会把它清掉。 */
    function loadStore() {
      I.store = { mergeHead: null, resolved: {} };
      try {
        var raw = global.sessionStorage && global.sessionStorage.getItem(STORE_KEY);
        if (raw) {
          var j = JSON.parse(raw);
          if (j && typeof j === "object" && j.resolved &&
              typeof j.resolved === "object") {
            I.store = j;
          }
        }
      } catch (e) { /* file:// 或隐私模式:退化成「不跨刷新记忆」 */ }
    }
    function saveStore() {
      try {
        if (global.sessionStorage) {
          global.sessionStorage.setItem(STORE_KEY, JSON.stringify(I.store));
        }
      } catch (e) { /* 同上,忽略 */ }
    }
    function markResolved(path) {
      I.store.resolved[path] = Date.now();
      saveStore();
    }
    /* 写操作返回里带 mergeHead —— 用它把「换了次合并」这件事认出来。 */
    function syncMergeHead(res) {
      if (!res || typeof res.mergeHead !== "string") return;
      if (I.store.mergeHead && I.store.mergeHead !== res.mergeHead) {
        I.store.resolved = {};
      }
      I.store.mergeHead = res.mergeHead;
      saveStore();
    }

    /* -- 基础渲染 -------------------------------------------------------- */
    function h(tag, cls, text) {
      var e = I.doc.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined && text !== null) e.textContent = String(text);
      return e;
    }
    function clearNote() {
      var box = I.root.querySelector(".cp-note");
      if (box) box.innerHTML = "";
    }
    /* 提示条:noteHtml 是宿主注入的,复用它而不是自己写一份样式。
       独立自测页没给 noteHtml 时用本文件的兜底实现。 */
    function note(kind, html) {
      clearNote();
      if (!html) return;
      var box = I.root.querySelector(".cp-note");
      if (!box) return;
      if (I.noteHtml) {
        box.innerHTML = I.noteHtml(kind, html, true);
      } else {
        /* kind 只可能是 ok/err/warn 三个常量,不是用户数据。 */
        box.innerHTML = '<div class="note ' + (kind || "") + '">' +
          '<span class="x" title="关闭">✕</span>' + html + "</div>";
      }
    }

    function buildHead() {
      var hd = h("div", "cp-hd");
      hd.appendChild(h("span", "cp-t", PANEL_TITLE));
      var pillMerge = h("span", "cp-pill", "未加载");
      pillMerge.id = "cp-merge-pill";
      hd.appendChild(pillMerge);
      var left = h("span", "cp-pill", "未解决 —");
      left.id = "cp-left-pill";
      hd.appendChild(left);
      hd.appendChild(h("span", "cp-sp"));
      [
        ["刷新", "", function () { load(true); }],
        ["全部接受当前", "", function () { massPick("ours"); }],
        ["全部接受传入", "", function () { massPick("theirs"); }],
        ["放弃合并", "danger", function () { doAbort(); }],
        ["完成合并", "pri", function () { doCommit(); }]
      ].forEach(function (spec) {
        var b = h("button", "cp-btn" + (spec[1] ? " " + spec[1] : ""), spec[0]);
        b.type = "button";
        b.addEventListener("click", spec[2]);
        hd.appendChild(b);
      });
      return hd;
    }

    function buildFoot() {
      var f = h("div", "cp-ft");
      f.id = "cp-ft";
      f.appendChild(h("span", null,
        "「接受某侧」只改结果栏,不碰仓库;「写回并 add」才落盘并 git add。"));
      return f;
    }

    /* -- 列表:未解决 / 已解决 两组 ---------------------------------------- */
    function paintList() {
      var box = I.root.querySelector(".cp-list");
      I.rows = [];
      box.innerHTML = "";

      if (!I.list) {
        box.appendChild(h("div", "cp-empty",
          I.loading ? "正在读冲突清单…" : "尚未加载。"));
        return;
      }

      var unresolved = I.list.files || [];
      box.appendChild(h("div", "cp-grp", "未解决 (" + unresolved.length + ")"));
      if (!unresolved.length) {
        box.appendChild(h("div", "cp-row", "没有未解决的冲突文件。"));
      }
      unresolved.forEach(function (f) {
        var i = I.rows.length;
        I.rows.push({ kind: "un", path: f.path, data: f });
        var row = h("div", "cp-row" + (I.sel === f.path ? " sel" : ""));
        row.setAttribute("data-cp-i", String(i));
        var n = h("div", "n");
        if (f.error) {
          /* inspect 在这个文件上抛了(后端把 error 挂在这一项里)。
             照实显示,别渲染成「undefined 栏」。 */
          n.appendChild(h("span", "cp-tag bad", "读不了"));
          n.appendChild(h("span", "nm", f.name));
        } else {
          n.appendChild(h("span", "cp-tag", f.columns + " 栏"));
          n.appendChild(h("span", "nm", f.name));
          if (f.noBase) n.appendChild(h("span", "cp-tag nb", "无基线"));
          if (f.binary) n.appendChild(h("span", "cp-tag bad", "二进制"));
          if (f.manuallyEdited) n.appendChild(h("span", "cp-tag man", "已手改"));
          if (f.crossCheckMismatch && f.crossCheckMismatch.length) {
            n.appendChild(h("span", "cp-tag bad", "三方不一致"));
          }
          if (f.verified === true) n.appendChild(h("span", "cp-tag uf", "已核对"));
        }
        row.appendChild(n);
        row.appendChild(h("div", "m", f.error
          ? String(f.error)
          : f.regionCount + " 块 · " + (f.oursLabel || "HEAD") +
            " vs " + (f.theirsLabel || "?")));
        box.appendChild(row);
      });

      var resolvedPaths = Object.keys(I.store.resolved);
      box.appendChild(h("div", "cp-grp", "已解决 (" + resolvedPaths.length + ")"));
      if (!resolvedPaths.length) {
        box.appendChild(h("div", "cp-row", "本次合并里还没有解决过任何文件。"));
      }
      resolvedPaths.forEach(function (p) {
        var i = I.rows.length;
        I.rows.push({ kind: "res", path: p, at: I.store.resolved[p] });
        var row = h("div", "cp-row" + (I.sel === p ? " sel" : ""));
        row.setAttribute("data-cp-i", String(i));
        var n = h("div", "n");
        n.appendChild(h("span", "cp-tag uf", "已 add"));
        n.appendChild(h("span", "nm", p.split("/").pop()));
        row.appendChild(n);
        row.appendChild(h("div", "m", hhmm(I.store.resolved[p]) + " 解决 · " + p));
        box.appendChild(row);
      });
    }

    function paintHeader() {
      var pillMerge = I.root.querySelector("#cp-merge-pill");
      var left = I.root.querySelector("#cp-left-pill");
      if (!pillMerge || !left) return;
      if (!I.list) {
        pillMerge.textContent = "未加载";
        pillMerge.className = "cp-pill";
        left.textContent = "未解决 —";
        return;
      }
      if (I.list.inMerge) {
        pillMerge.textContent = "合并进行中";
        pillMerge.className = "cp-pill on";
      } else {
        pillMerge.textContent = "不在合并态";
        pillMerge.className = "cp-pill";
      }
      left.textContent = "未解决 " + I.list.count +
        " · 已解决 " + Object.keys(I.store.resolved).length;
    }

    /* -- 三栏 ------------------------------------------------------------ */
    function sideCol(key, d) {
      var s = d.sides[key];
      var col = h("div", "cp-col " + key);
      var hd4 = h("h4");
      hd4.appendChild(h("span", null,
        key === "ours" ? "当前(ours)" :
        key === "theirs" ? "传入(theirs)" : "基线(base)"));
      if (s) {
        hd4.appendChild(h("span", "cp-tag", s.label || s.ref || ""));
        if (s.binary) hd4.appendChild(h("span", "cp-tag bad", "二进制"));
        if (s.lineCount !== null && s.lineCount !== undefined) {
          hd4.appendChild(h("span", "cp-tag", s.lineCount + " 行"));
        }
      }
      col.appendChild(hd4);
      if (!s || s.deleted || s.text === null) {
        col.appendChild(h("div", "cp-gone", !s ? "这一侧不存在"
          : (s.deleted ? "这一侧在基线之上被删除" : "这一侧是二进制,读不出文本")));
        return col;
      }
      var c = clampText(s.text);
      col.appendChild(h("pre", null, c.text));
      if (c.cut) {
        col.appendChild(h("div", "cp-cut", "… 已截断显示,完整内容共 " +
          s.text.length + " 字符(还差 " + c.cut + ")"));
      }
      return col;
    }

    /* 把 picks 渲染成一份**没有冲突标记**的结果文本。
     *
     * 从后端给的 regions 里取每个块在**工作区文件**里的行号
     * (oursMarkerLine / theirsMarkerLine,1-based 闭区间),把整块换成选中的
     * 那一侧。做法是「按块顺序切片拼接」,不是逐块 splice —— 一次线性扫描,
     * 大文件也不会退化成 O(n^2)。
     *
     * 守卫:块必须单调不重叠。不满足就**放弃重建**,直接给后端那份原始工作区
     * 文本 —— 那份至少是仓库里的真内容,总比重建错行号强。 */
    function deriveResult(d, picks) {
      var wt = d.worktree && d.worktree.text;
      if (wt === null || wt === undefined) return "";
      var regions = d.regions || [];
      if (!regions.length) return wt;
      var sp = splitLines(wt);
      var lines = sp.lines;
      var out = [];
      var cursor = 1;                        // 1-based
      for (var i = 0; i < regions.length; i++) {
        var r = regions[i];
        var a = r.oursMarkerLine, b = r.theirsMarkerLine;
        if (!(a >= cursor && b >= a)) return wt;      // 块不单调 -> 放弃重建
        var seg = lines.slice(cursor - 1, a - 1);
        for (var k = 0; k < seg.length; k++) out.push(seg[k]);
        var pp = splitLines(regionText(d, r, picks[i] || "ours")).lines;
        for (var k2 = 0; k2 < pp.length; k2++) out.push(pp[k2]);
        cursor = b + 1;
      }
      var tail = lines.slice(cursor - 1);
      for (var k3 = 0; k3 < tail.length; k3++) out.push(tail[k3]);
      return joinLines(out, sp.trailing);
    }

    /* 一个块选中某一侧时的文本。
     * both = ours 接 theirs(VSCode「Accept Combination」的语义:两段都留,
     * 当前在前)。base 优先用 diff3 风格里切出来的 baseText;普通 merge
     * 风格没有 baseText,用锚点定位出来的 baseLocatedText。 */
    function regionText(d, r, pick) {
      if (pick === "none") return "";
      if (pick === "theirs") return r.theirsText === null ? "" : r.theirsText;
      if (pick === "base") {
        var bt = (r.baseText !== null && r.baseText !== undefined)
          ? r.baseText : r.baseLocatedText;
        return bt === null || bt === undefined ? (r.oursText || "") : bt;
      }
      if (pick === "both") return concatTexts(r.oursText, r.theirsText);
      return r.oursText === null ? "" : r.oursText;
    }
    function concatTexts(a, b) {
      if (!a) return b || "";
      if (!b) return a;
      return /\n$/.test(a) ? a + b : a + "\n" + b;
    }

    function paintDetail() {
      var box = I.root.querySelector(".cp-detail");
      box.innerHTML = "";

      if (!I.list) {
        box.appendChild(h("div", "cp-empty",
          I.loading ? "正在读冲突清单…" : "点「刷新」读一次冲突清单。"));
        return;
      }
      if (!I.list.inMerge) {
        box.appendChild(h("div", "cp-empty",
          "当前不在合并态,没有要解决的冲突。\n\n" +
          "造一个冲突:用顶栏的「合并」按钮选一个分支,或者不开界面直接调\n" +
          "    POST /api/merge   {\"name\": \"<分支名>\"}\n" +
          "合并出冲突后回到这个面板。\n\n" +
          "放弃一次进行中的合并(等价 git merge --abort):\n" +
          "    POST /api/merge-abort   {}"));
        return;
      }
      if (!I.sel) {
        var unresolved = I.list.files || [];
        box.appendChild(h("div", "cp-empty", unresolved.length
          ? "左边选一个冲突文件。\n共 " + unresolved.length + " 个待解决。"
          : "合并进行中,没有未解决的冲突文件。\n" +
            "可以点右上角「完成合并」收尾。\n" +
            "(已经解决的文件都在左边「已解决」分组里。)"));
        return;
      }

      var row = null;
      for (var i = 0; i < I.rows.length; i++) {
        if (I.rows[i].path === I.sel) { row = I.rows[i]; break; }
      }
      if (!row) { I.sel = null; return paintDetail(); }
      if (row.kind === "res") { box.appendChild(resolvedPane(row)); return; }
      if (I.loading && !I.detail) {
        box.appendChild(h("div", "cp-empty", "正在读 " + row.path + " …"));
        return;
      }
      if (!I.detail || I.detail.path !== row.path) {
        box.appendChild(h("div", "cp-empty", "选一个文件。"));
        return;
      }
      if (I.detail.error) {
        box.appendChild(h("div", "cp-empty", String(I.detail.error)));
        return;
      }
      box.appendChild(conflictPane(I.detail));
    }

    function resolvedPane(row) {
      var wrap = h("div", "cp-empty");
      wrap.appendChild(h("div", null, "已解决:" + row.path));
      wrap.appendChild(h("div", null,
        "内容已写回工作区并 git add 落入索引(索引里不再是 U,是一条普通暂存改动)。"));
      /* 回退按钮:故意置灰并写明为什么 —— 诚实比有个点不动的按钮强。 */
      var b = h("button", "cp-btn", "回退这个文件的解决");
      b.type = "button";
      b.disabled = true;
      b.title = "后端没有「取消解决 / 退回冲突态」的接口";
      wrap.appendChild(b);
      wrap.appendChild(h("div", null,
        "\n后端目前没有 unresolve 接口:resolve 之后索引里的 U 条目就没了," +
        "\nPOST /api/unstage 也退不回来(实测 ls-files -u 仍为空)。" +
        "\n能一次性回退整次合并的只有「放弃合并」:" +
        "\n    POST /api/conflict/abort   {}\n" +
        "\n但它会把已经解决的全部一起丢掉。"));
      wrap.appendChild(h("div", null, "\n解决于 " + hhmm(row.at)));
      return wrap;
    }

    function conflictPane(d) {
      var frag = I.doc.createDocumentFragment();

      var bar = h("div", "cp-bar");
      bar.appendChild(h("span", "cp-t", d.path));
      bar.appendChild(h("span", "cp-pill", d.columns + " 栏"));
      bar.appendChild(h("span", "cp-pill", d.regionCount + " 块"));
      if (d.noBase) bar.appendChild(h("span", "cp-tag nb", "无共同祖先(2 栏)"));
      if (d.binary) bar.appendChild(h("span", "cp-tag bad", "二进制"));
      if (d.worktree.manuallyEdited) {
        bar.appendChild(h("span", "cp-tag man", "冲突标记已被手工删掉"));
      }
      if (d.verified === false) bar.appendChild(h("span", "cp-tag bad", "三方定位未核对"));
      if (d.verified === true) bar.appendChild(h("span", "cp-tag uf", "三方已核对"));
      if (d.crossCheckMismatch && d.crossCheckMismatch.length) {
        bar.appendChild(h("span", "cp-tag bad",
          "三方与索引不一致:" + d.crossCheckMismatch.join(",")));
      }
      frag.appendChild(bar);

      var msgBox = h("div", "cp-msg");
      msgBox.id = "cp-msg";
      msgBox.textContent = statusLine(d);
      frag.appendChild(msgBox);

      var acts = h("div", "cp-bar");
      acts.appendChild(mkBtn("写回并 add", "pri", doWrite));
      acts.appendChild(mkBtn("整份取当前(丢弃传入)", "", function () { doChoose("ours"); }));
      acts.appendChild(mkBtn("整份取传入(丢弃当前)", "", function () { doChoose("theirs"); }));
      if (d.sides.base && !d.sides.base.deleted && d.sides.base.text !== null) {
        acts.appendChild(mkBtn("整份取基线", "", function () { doChoose("base"); }));
      }
      acts.appendChild(mkBtn("删除文件", "danger", doDelete));
      acts.appendChild(mkBtn("重新读取(丢弃未写回的编辑)", "",
                             function () { openFile(I.sel); }));
      acts.appendChild(mkBtn(d.showBase ? "隐藏基线栏" : "显示基线栏", "", function () {
        d.showBase = !d.showBase;
        paintDetail();
      }));
      frag.appendChild(acts);

      /* 三栏:ours | result(可编辑) | theirs。base 是第四栏,默认折叠。 */
      var cols = h("div", "cp-cols");
      cols.appendChild(sideCol("ours", d));
      var taHd = h("h4");
      taHd.appendChild(h("span", null, "结果(可编辑)"));
      taHd.appendChild(h("span", "cp-tag", I.edited ? "已手工编辑" : "按块重建"));
      var taLab = h("span", "cp-tag", I.result.length + " 字符");
      taHd.appendChild(taLab);
      var ta = h("textarea");
      ta.spellcheck = false;
      ta.value = I.result;
      ta.setAttribute("aria-label", "合并结果(可编辑)");
      var taCol = h("div", "cp-col ta" + (I.edited ? " edited" : ""));
      taCol.appendChild(taHd);
      taCol.appendChild(ta);
      cols.appendChild(taCol);
      cols.appendChild(sideCol("theirs", d));
      if (d.showBase && d.sides.base) cols.appendChild(sideCol("base", d));
      frag.appendChild(cols);

      /* 输入时**不重画整块面板**:重画 innerHTML 会把焦点和光标吃掉。
         只改三个小标签,以及各块卡片里那句「结果栏里这一块现在是」——
         手工编辑之后按块号已经反查不出来了,那句话必须同步改成
         「无法按块反查」,否则它会停在上一次按块重建时的旧值上。 */
      ta.addEventListener("input", function () {
        I.result = ta.value;
        I.edited = true;
        taCol.className = "cp-col ta edited";
        taHd.children[1].textContent = "已手工编辑";
        taLab.textContent = I.result.length + " 字符";
        var previews = I.root.querySelectorAll(".cp-rg pre.pick");
        for (var pi = 0; pi < previews.length; pi++) {
          previews[pi].textContent = "已手工编辑,无法按块反查";
        }
        var mb = I.root.querySelector("#cp-msg");
        if (mb) mb.textContent = statusLine(d);
      });
      frag.appendChild(regionsBlock(d));
      frag.appendChild(h("div", "cp-ft",
        "每块默认取当前侧 —— 想留两边就点「接受双方」,想手动改就直接改中间那栏。"));
      return frag;
    }

    function mkBtn(label, cls, fn) {
      var b = h("button", "cp-btn" + (cls ? " " + cls : ""), label);
      b.type = "button";
      b.addEventListener("click", fn);
      return b;
    }

    /* 状态行:说清楚「现在写回会丢掉什么」,以及手工编辑与按块按钮的关系。 */
    function statusLine(d) {
      var n = (d.regions || []).length;
      var oneSide = 0;
      for (var i = 0; i < n; i++) {
        var p = I.picks && I.picks[i];
        if (p === "ours" || p === "theirs" || p === "base") oneSide++;
      }
      var s = n + " 个冲突块:当前 " + (n - oneSide) + " 块保留了双方," +
              oneSide + " 块只取了一侧";
      if (oneSide > 0) {
        s += " —— 写回后另一侧的改动就没了,而且后端没有「取消解决」接口,退不回来";
      }
      if (I.edited) {
        s += "。你已手工改过结果栏:再点「接受某侧」会先问一次,确认后按块重建," +
             "手工编辑会被覆盖(想保留就自己改完后直接写回)";
      }
      return s;
    }

    /* 逐块卡片:每个块一个 <details>,里面只放**这一块**的三方文本。
       DOM 规模 O(块数),不随文件行数增长。 */
    function regionsBlock(d) {
      var box = h("div");
      var regions = d.regions || [];
      if (!regions.length) {
        box.appendChild(h("div", "cp-empty", d.worktree.manuallyEdited
          ? "工作区里已经没有冲突标记了(索引还是 U)。直接编辑中间那栏改成" +
            "想要的样子,然后「写回并 add」。"
          : "后端没有在这个文件里解析出冲突块。仍然可以编辑中间那栏后" +
            "「写回并 add」,或者用上面三个「整份取某侧」。"));
        return box;
      }
      regions.forEach(function (r, idx) {
        var det = h("details", "cp-rg");
        if (idx === 0) det.open = true;
        var sm = h("summary");
        sm.appendChild(h("span", "cp-tag", "块 " + idx));
        sm.appendChild(h("span", null,
          "标记行 " + r.oursMarkerLine + "–" + r.theirsMarkerLine +
          " · ours " + r.oursLines + " 行 / theirs " + r.theirsLines + " 行"));
        if (r.verified === true) sm.appendChild(h("span", "cp-tag uf", "已核对"));
        if (r.verified === false) sm.appendChild(h("span", "cp-tag bad", "定位未核对"));
        var acts2 = h("span", "cp-acts");
        acts2.appendChild(pickBtn("接受当前", idx, "ours"));
        acts2.appendChild(pickBtn("接受传入", idx, "theirs"));
        acts2.appendChild(pickBtn("接受双方", idx, "both"));
        if (d.sides.base && !d.sides.base.deleted) {
          acts2.appendChild(pickBtn("接受基线", idx, "base"));
        }
        acts2.appendChild(pickBtn("清空此块", idx, "none"));
        sm.appendChild(acts2);
        det.appendChild(sm);

        det.appendChild(h("div", "cp-lab", "当前(" + (d.sides.ours.label || "HEAD") + ")"));
        det.appendChild(h("pre", "ours", r.oursText === null ? "(空)" : r.oursText));
        det.appendChild(h("div", "cp-lab", "传入(" + (d.sides.theirs.label || "?") + ")"));
        det.appendChild(h("pre", "theirs", r.theirsText === null ? "(空)" : r.theirsText));
        var bl = (r.baseText !== null && r.baseText !== undefined)
          ? r.baseText : r.baseLocatedText;
        if (bl) {
          det.appendChild(h("div", "cp-lab", "基线(" + (d.sides.base.label || "") +
            (r.baseLineStart ? ",第 " + r.baseLineStart + "–" + r.baseLineEnd + " 行" : "") + ")"));
          det.appendChild(h("pre", null, bl));
        }
        det.appendChild(h("div", "cp-lab", "结果栏里这一块现在是:"));
        det.appendChild(h("pre", "pick", resultForRegion(d, idx)));
        box.appendChild(det);
      });
      return box;
    }

    /* 结果栏文本里,块 idx 那段现在是什么。
     * 手工编辑过就没法按块号反查(用户可能把整段改了),这时如实说
     * 「已手工编辑,无法按块反查」,不假装知道。 */
    function resultForRegion(d, idx) {
      if (I.edited) return "已手工编辑,无法按块反查";
      return regionText(d, d.regions[idx], I.picks[idx] || "ours") || "(空)";
    }

    function pickBtn(label, idx, pick) {
      var b = h("button", "cp-btn sm", label);
      b.type = "button";
      /* 行内只放下标与一个固定词,没有路径、没有用户文本。 */
      b.setAttribute("data-cp-r", String(idx));
      b.setAttribute("data-cp-pick", pick);
      b.title = "只改结果栏,不写盘";
      return b;
    }

    /* -- 事件委托:一次挂在根上,行内只放下标 -------------------------- */
    function onClick(ev) {
      var t = ev.target;
      while (t && t !== I.root) {
        if (t.getAttribute) {
          var rIdx = t.getAttribute("data-cp-r");
          if (rIdx !== null && rIdx !== undefined) {
            ev.preventDefault();
            setPick(Number(rIdx), t.getAttribute("data-cp-pick"));
            return;
          }
          var fIdx = t.getAttribute("data-cp-i");
          if (fIdx !== null && fIdx !== undefined) {
            ev.preventDefault();
            var row = I.rows[Number(fIdx)];
            if (row) selectRow(row);
            return;
          }
          if (t.className === "x") {         // 提示条上的 ✕
            clearNote();
            return;
          }
        }
        t = t.parentNode;
      }
    }

    /* -- 动作 ----------------------------------------------------------- */

    /* 覆盖手工编辑是不可逆的,所以先问一句:第一次点把按钮变成 armed,
       第二次点才真改。 */
    function setPick(idx, pick) {
      if (!I.detail || !I.detail.regions || !I.detail.regions[idx]) return;
      if (I.edited) {
        var b = I.root.querySelector('[data-cp-r="' + idx + '"][data-cp-pick="' + pick + '"]');
        if (b && b.className.indexOf("armed") < 0) {
          b.className = "cp-btn sm armed";
          b.textContent = "会覆盖手工编辑,点此确认";
          b.addEventListener("click", function once(ev2) {
            ev2.stopPropagation();
            ev2.preventDefault();
            b.removeEventListener("click", once);
            applyPick(idx, pick);
          });
          return;
        }
      }
      applyPick(idx, pick);
    }
    function applyPick(idx, pick) {
      I.picks[idx] = pick;
      I.edited = false;
      I.result = deriveResult(I.detail, I.picks);
      paintDetail();
    }

    function setMsg(text) {
      var m = I.root && I.root.querySelector("#cp-msg");
      if (m) m.textContent = text;
    }
    function fail(what, e) {
      setMsg("");
      note("err", "<b>" + I.esc(what) + "失败</b>\n" +
                   I.esc((e && e.message) || String(e)));
    }

    function doWrite() {
      var d = I.detail;
      if (!d) return;
      /* 二进制 / 工作区读不出文本时**不能走 resolve**:content 只能是文本,
         送过去等于把二进制文件截成一个空文件。这种文件只有「整份取某侧」
         和「删除文件」两条路(后端 choose 会自己拒二进制)。 */
      if (d.binary || (d.worktree && d.worktree.text === null)) {
        note("warn", "这是二进制文件(或工作区读不出文本),结果栏写不回去。\n" +
          "只能用「整份取当前/传入」或「删除文件」。");
        return;
      }
      var oneSide = 0;
      (d.regions || []).forEach(function (r, i) {
        var p = I.picks && I.picks[i];
        if (p === "ours" || p === "theirs" || p === "base") oneSide++;
      });
      if (oneSide > 0) {
        /* 动作给的是 writeNow 而不是 doWrite 自己:把 doWrite 当回调传进去
           会在确认之后**再进一次同一个判断**,于是点「确认」又弹一次
           「确认」,永远写不出去。确认条要的是「已经答过 yes 的那一步」。 */
        armConfirm("确认写回并 add",
          "还有 " + oneSide + " 个块只取了一侧。写回后另一侧的改动就没了," +
          "而且后端没有「取消解决」接口,退不回来。", writeNow);
        return;
      }
      writeNow();
    }
    function writeNow() {
      var d = I.detail;
      var path = d.path;
      var body = { file: path, content: I.result };
      /* 乐观并发:带上后端读文件时给的 sha256。期间谁在别处(编辑器 /
         命令行)改过这个文件,后端会拒掉,不会静默覆盖。 */
      if (d.worktree && d.worktree.sha256) {
        body.expect_sha256 = d.worktree.sha256;
      }
      I.loading = true;
      setMsg("正在写回 " + path + " …");
      I.ctx.api("/api/conflict/resolve", { method: "POST", body: body })
        .then(function (res) {
          syncMergeHead(res);
          markResolved(path);
          clearOpen();
          note("ok", "<b>已写回并 add</b>\n" + I.esc(path) +
            "\n未解决 " + res.remainingCount + " 个");
          /* 用响应里的 remaining 直接改本地清单,不再拉一次全量
             (40 个冲突时一次 list 实测 16 秒,而响应里已经给了后端算的真值)。 */
          applyRemaining(res.remaining);
          I.loading = false;
          paintAll();                     // 不重画的话分组/计数/详情都停在旧状态
          return hostRefresh();
        })
        .catch(function (e) {
          I.loading = false;
          fail("写回", e);
          paintAll();
          /* 可能是 sha256 对不上(文件被别人改过)。重新读一次,让用户看到
             现在的真内容,而不是留着一个过期的编辑框。 */
          openFile(path);
        });
    }
    function clearOpen() {
      I.sel = null; I.detail = null; I.picks = null; I.result = "";
      I.edited = false;
    }

    /* 响应里的 remaining 是后端算的真值(来自 git diff --diff-filter=U)。 */
    function applyRemaining(remaining) {
      if (!I.list || !Array.isArray(remaining)) return;
      var left = {};
      remaining.forEach(function (p) { left[p] = true; });
      I.list.files = I.list.files.filter(function (f) { return left[f.path]; });
      I.list.count = I.list.files.length;
    }

    function doChoose(side) {
      var d = I.detail;
      if (!d) return;
      var mine = side === "ours" ? "当前" : "传入";
      var other = side === "ours" ? "传入" : "当前";
      var s = d.sides[side] || {};
      armConfirm("确认整份取" + mine,
        "这会用" + mine + "侧(" + (s.label || s.ref || side) + ")的全文覆盖文件," +
        other + "侧对这个文件的改动全部丢弃,且退不回来。",
        function () {
          runWrite(d.path, "/api/conflict/choose", { file: d.path, side: side },
                   "已整份取" + mine);
        });
    }

    function doDelete() {
      var d = I.detail;
      if (!d) return;
      armConfirm("确认删除文件",
        "会把 " + d.path + " 从工作区和索引里删掉。" +
        "这一步没有后端接口能撤销(文件已被删,解决态也退不回冲突态)。",
        function () {
          runWrite(d.path, "/api/conflict/delete", { file: d.path }, "已删除文件");
        });
    }

    /* 两段式确认:第一次点弹出说明条,条上再点一次才真做。
       不用 window.confirm —— 它是模态弹窗,自测拦不住,也会打断批量操作。 */
    function armConfirm(label, why, action) {
      clearNote();
      var box = I.root.querySelector(".cp-note");
      var wrap = h("div", "note err");
      wrap.appendChild(h("span", null, why));
      var line = h("div");
      line.style.marginTop = "5px";
      var ok = h("button", "cp-btn armed", label);
      ok.type = "button";
      var no = h("button", "cp-btn", "取消");
      no.type = "button";
      no.style.marginLeft = "6px";
      ok.addEventListener("click", function () { clearNote(); action(); });
      no.addEventListener("click", clearNote);
      line.appendChild(ok);
      line.appendChild(no);
      wrap.appendChild(line);
      box.innerHTML = "";
      box.appendChild(wrap);
    }

    function runWrite(path, endpoint, body, okText) {
      I.loading = true;
      setMsg("正在提交 " + path + " …");
      I.ctx.api(endpoint, { method: "POST", body: body })
        .then(function (res) {
          syncMergeHead(res);
          markResolved(path);
          clearOpen();
          note("ok", "<b>" + I.esc(okText) + "</b>\n" + I.esc(path) +
            "\n未解决 " + res.remainingCount + " 个");
          applyRemaining(res.remaining);
          return hostRefresh();
        })
        .catch(function (e) { fail(okText, e); })
        .then(function () { I.loading = false; paintAll(); });
    }

    /* 「全部接受当前 / 全部接受传入」:串行,逐个文件报进度,可中止。
     * 串行是刻意的:后端 ConflictResolver 没有锁,并发写同一个仓库的索引
     * 会互相踩。而且每个文件一次调用在 40 冲突的仓里就要十几秒 ——
     * 谎称它很快不如老实报进度。 */
    var massStop = false;
    function massPick(side) {
      if (!I.list || !I.list.inMerge) {
        note("warn", "当前不在合并态,没有可批量处理的冲突。");
        return;
      }
      var files = I.list.files || [];
      if (!files.length) { note("warn", "没有未解决的冲突文件。"); return; }
      var label = side === "ours" ? "当前" : "传入";
      armConfirm("确认全部接受" + label,
        "会把 " + files.length + " 个文件的全文都换成" + label + "侧," +
        "另一侧在这些文件上的改动全部丢弃。逐个执行、无法撤销。",
        function () { runMass(files, side); });
    }
    function runMass(files, side) {
      massStop = false;
      var i = 0;
      var ft = I.root.querySelector("#cp-ft");
      /* 上一轮批量可能留着一个「停止」按钮,先摘掉再挂新的,免得叠多个。 */
      var olds = ft.querySelectorAll("button");
      for (var oi = 0; oi < olds.length; oi++) olds[oi].remove();
      var bStop = h("button", "cp-btn sm danger", "停止");
      bStop.type = "button";
      bStop.addEventListener("click", function () { massStop = true; });
      ft.appendChild(bStop);

      function done(text) {
        I.loading = false;
        ft.textContent = text;
        paintHeader();
        paintList();
        load(true);
        hostRefresh();      // 批量改完索引也变了,宿主那边一样要刷
      }
      function step() {
        if (massStop) { return done("已中止。还剩 " + (files.length - i) + " 个没处理。"); }
        if (i >= files.length) {
          note("ok", "<b>批量处理完成</b>\n共 " + files.length + " 个文件");
          return done("批量处理完成:" + files.length + " 个文件。");
        }
        var f = files[i];
        ft.textContent = "正在处理 " + (i + 1) + "/" + files.length + ":" + f.path;
        I.ctx.api("/api/conflict/choose",
                   { method: "POST", body: { file: f.path, side: side } })
          .then(function (res) {
            syncMergeHead(res);
            markResolved(f.path);
            i++;
            step();
          })
          .catch(function (e) {
            fail("批量接受" + (side === "ours" ? "当前" : "传入"), e);
            done("在 " + f.path + " 上停住了(" + (i + 1) + "/" + files.length +
                 "),后面的没动。");
          });
      }
      I.loading = true;
      step();
    }

    function doCommit() {
      if (!I.list || !I.list.inMerge) {
        note("warn", "当前不在合并态,没有可完成的合并。");
        return;
      }
      if (I.list.count > 0) {
        note("warn", "还有 " + I.list.count + " 个冲突没解决,后端会拒绝提交。" +
          "先逐个解决,或用「全部接受当前/传入」。");
        return;
      }
      armConfirm("确认完成合并",
        "会用 .git/MERGE_MSG 里的默认信息提交这次合并" +
        "(不开界面的话等价于 git commit,不带 -m)。提交后合并态结束," +
        "这个面板回到空状态。",
        function () {
          I.ctx.api("/api/conflict/commit", { method: "POST", body: {} })
            .then(function (res) {
              I.store.resolved = {};
              saveStore();
              I.sel = null; I.detail = null;
              note("ok", "<b>已提交 " + I.esc(res.shortCommit || "") + "</b>\n" +
                I.esc((res.message || "").split("\n")[0]));
              /* 合并态结束了,重新读一次拿真状态(这时 list 很快:
                 不在合并态 / 没有冲突时后端不逐文件 inspect)。 */ 
              load(true);
              return hostRefresh();
            })
            .catch(function (e) { fail("完成合并", e); });
        });
    }

    function doAbort() {
      if (!I.list || !I.list.inMerge) {
        note("warn", "当前不在合并态。");
        return;
      }
      var n = Object.keys(I.store.resolved).length;
      armConfirm("确认放弃合并",
        "等价于 git merge --abort:已经解决的 " + n +
        " 个文件的解决结果会一起丢掉,回到合并前的样子。",
        function () {
          I.ctx.api("/api/conflict/abort", { method: "POST", body: {} })
            .then(function (res) {
              I.store.resolved = {};
              saveStore();
              clearOpen();
              note("ok", "<b>已放弃合并</b>\ninMerge=" + (res.inMerge ? "true" : "false"));
              load(true);          // 同上:重新读一次拿真状态
              return hostRefresh();
            })
            .catch(function (e) { fail("放弃合并", e); });
        });
    }

    /* 写操作之后让宿主也刷一遍:这几个冲突 POST **不返回宿主 status**
       (不像别的 POST 走 _ok_with_status),所以文件树 / 日志 / 提交框要靠
       refresh() 才知道变了。宿主没给 refresh 时安静跳过。 */
    function hostRefresh() {
      if (!I.ctx || typeof I.ctx.refresh !== "function") return null;
      try {
        var r = I.ctx.refresh();
        return (r && typeof r.then === "function") ? r.then(noop, noop) : null;
      } catch (e) { return null; }
    }
    function noop() {}

    /* -- 加载 ------------------------------------------------------------ */
    function load(force) {
      if (I.loading && !force) return;
      var my = ++I.gen;
      I.loading = true;
      paintHeader();
      paintDetail();
      I.ctx.api("/api/conflict/list")
        .then(function (d) {
          if (!I.mounted || my !== I.gen) return;   // 已被 unmount / 新一轮顶掉
          if (!d.inMerge) {
            /* 不在合并态 -> 上一次的已解决清单作废(否则换个合并还会显示)。 */
            I.store.resolved = {};
            saveStore();
          }
          I.list = d;
          if (I.sel && !(d.files || []).some(function (f) { return f.path === I.sel; }) &&
              !I.store.resolved[I.sel]) {
            I.sel = null;
            I.detail = null;
          }
          I.loading = false;
          paintAll();
        })
        .catch(function (e) {
          if (!I.mounted || my !== I.gen) return;
          I.loading = false;
          note("err", "<b>读冲突清单失败</b>\n" + I.esc((e && e.message) || String(e)));
          paintAll();
        });
    }

    function openFile(path) {
      var my = ++I.gen;
      I.sel = path;
      I.detail = null; I.picks = null; I.result = ""; I.edited = false;
      clearNote();
      paintList();
      paintDetail();
      I.ctx.api("/api/conflict/inspect?file=" + encodeURIComponent(path))
        .then(function (d) {
          if (!I.mounted || my !== I.gen) return;
          I.detail = d;
          d.showBase = false;
          I.picks = [];
          (d.regions || []).forEach(function (r, i) {
            /* 默认每块取当前侧 —— 与 VSCode 合并编辑器的初始态一致。
               当前侧被删掉(一边删一边改)时默认取传入侧。 */
            I.picks[i] = (d.sides.ours && d.sides.ours.deleted &&
                          !d.sides.theirs.deleted) ? "theirs" : "ours";
          });
          I.result = deriveResult(d, I.picks);
          I.edited = false;
          paintAll();
        })
        .catch(function (e) {
          if (!I.mounted || my !== I.gen) return;
          I.detail = null;
          fail("读 " + path, e);
          paintAll();
        });
    }

    /* 点列表行。已解决的那一组**不去 inspect**:那个路径的 U 条目已经
       没了,inspect 必然 400(「该路径没有未合并的索引条目」)—— 白白发一个
       必然失败的请求,还要靠 catch 吞掉。这里只切选中态。 */
    function selectRow(row) {
      if (row.kind === "res") {
        I.sel = row.path;
        I.detail = null; I.picks = null; I.result = ""; I.edited = false;
        clearNote();
        paintList();
        paintDetail();
        return;
      }
      openFile(row.path);
    }

    function paintAll() {
      if (!I.mounted) return;
      paintHeader();
      paintList();
      paintDetail();
    }

    /* -- mount / unmount -------------------------------------------------- */
    return {
      id: PANEL_ID,
      title: PANEL_TITLE,

      mount: function (el, ctx) {
        ctx = ctx || {};
        if (typeof ctx.api !== "function") {
          throw new Error("conflicts-panel: ctx.api 必填");
        }
        I.root = el;
        I.ctx = ctx;
        I.doc = el.ownerDocument || global.document;
        I.esc = typeof ctx.esc === "function" ? ctx.esc : fallbackEsc;
        I.noteHtml = typeof ctx.noteHtml === "function" ? ctx.noteHtml : null;
        loadStore();
        ensureStyle(I.doc);

        el.classList.add("cp");
        el.innerHTML = "";
        var inner = h("div", "cp-in");
        inner.appendChild(buildHead());
        inner.appendChild(h("div", "cp-note"));
        var body = h("div", "cp-body");
        body.appendChild(h("div", "cp-list"));
        body.appendChild(h("div", "cp-detail"));
        inner.appendChild(body);
        inner.appendChild(buildFoot());
        el.appendChild(inner);

        /* 只挂一次委托,之后所有重绘都走它(重绘 innerHTML 不会丢监听)。 */
        el.addEventListener("click", onClick);

        I.mounted = true;
        paintHeader();
        paintDetail();

        /* mount() **不预取数据**。用 IntersectionObserver 等真正可见了再拉
           第一次 —— 面板在后台标签里一个请求都不发。观察器回调里除了
           「第一次可见」这一下不做任何事,而且立刻 disconnect。
           之后只有用户点「刷新」才再拉,全文件没有轮询。 */
        var IO = global.IntersectionObserver;
        if (IO) {
          I.io = new IO(function (entries) {
            for (var i = 0; i < entries.length; i++) {
              if (entries[i].isIntersecting && !I.everVisible) {
                I.everVisible = true;
                if (I.io) { I.io.disconnect(); I.io = null; }
                load(true);
                return;
              }
            }
          }, { threshold: 0.01 });
          I.io.observe(el);
        } else {
          /* 没有 IO 的环境(很老的浏览器)才走的退化路径:挂上就拉一次。 */
          I.everVisible = true;
          load(true);
        }
      },

      unmount: function () {
        I.mounted = false;
        I.gen++;                       // 在途响应回来时全部作废
        if (I.io) { I.io.disconnect(); I.io = null; }
        if (I.root) {
          I.root.removeEventListener("click", onClick);
          I.root.innerHTML = "";
          I.root.classList.remove("cp");
        }
        I.root = null; I.ctx = null; I.list = null; I.detail = null;
        I.rows = []; I.picks = null; I.sel = null; I.result = "";
        I.edited = false; I.loading = false; I.everVisible = false;
        I.store = null;
        massStop = true;
      }
    };
  }

  /* 单例:同一时刻只有一个冲突面板实例。 */
  var singleton = null;
  var def = {
    id: PANEL_ID,
    title: PANEL_TITLE,
    mount: function (el, ctx) {
      if (singleton) { try { singleton.unmount(); } catch (e) { /* 忽略 */ } }
      var inst = create();
      inst.mount(el, ctx);          // mount 抛错时不要留下半个实例
      singleton = inst;
    },
    unmount: function () {
      if (singleton) { singleton.unmount(); singleton = null; }
    }
  };

  /* 两条注册路径:
     1) 宿主(index.html)先加载本文件 -> 走 AigitPanels.register;
     2) 独立自测页直接用 window.AigitConflictsPanel.mount(el, ctx)。
     两条都可用,不需要宿主先在。 */
  var api = {
    id: PANEL_ID,
    title: PANEL_TITLE,
    register: function (host) {
      host = host || global;
      if (!host.AigitPanels) {
        host.AigitPanels = {
          _panels: {},
          register: function (p) { this._panels[p.id] = p; }
        };
      }
      host.AigitPanels.register(def);
      return api;
    },
    create: create,
    mount: def.mount,
    unmount: def.unmount
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.AigitConflictsPanel = api;

  /* 三条注册路径,按可靠性排:
     1) 加载本文件时 AigitPanels 已经在 -> 直接注册(宿主 index.html 的正常情形);
     2) 加载时还没有 -> 在 DOMContentLoaded 上**试一次**(一次性事件监听,
        不是轮询)。覆盖「宿主先 <script> 引本文件、后定义注册表」这种写法;
     3) 一直都没有 -> 用 window.AigitConflictsPanel.register() 或 .mount()
        手动接。独立自测页走这条。 */
  function tryRegister() {
    if (global.AigitPanels && typeof global.AigitPanels.register === "function") {
      global.AigitPanels.register(def);
      return true;
    }
    return false;
  }
  if (!tryRegister()) {
    if (global.document && global.document.readyState === "loading") {
      global.document.addEventListener("DOMContentLoaded", tryRegister, { once: true });
    }
  }
})(typeof window !== "undefined" ? window : this);
