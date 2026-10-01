/*!
 * virtuallist.js —— 固定行高列表的虚拟滚动。
 *
 * 只渲染可视区 + 上下各 OVERSCAN 行,其余用一根 spacer 撑出滚动高度。
 * 1 万行的列表里实际存在的 DOM 节点始终是 O(视口行数),与总行数无关。
 *
 * 设计约束:
 *   * 零依赖,零构建。原生 <script> 引入即可,不用打包器。
 *   * 固定行高。这是虚拟滚动能省掉「测量」的前提;行高可变就得两趟渲染,
 *     复杂度上一个台阶,当前的文件列表用不上。
 *   * 不碰 DOM 之外的全局。不劫持 scroll 事件之外的任何东西。
 *
 * 与 web/index.html 里那段内联 paintV() 是同一套算法(ROW_H / OVERSCAN /
 * translateY 定位),抽出来是为了能独立测试、也能让别的面板复用。
 * 两边并存不冲突:index.html 没用本文件,要用的话由集成时二选一。
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    rowHeight: 22,      // 必须与 CSS 里 .row 的实际高度一致
    overscan: 8,        // 上下各多渲染几行,滚动时不会看见空白
    scroller: null,     // 滚动容器(必须有固定高度 + overflow:auto)
    spacer: null,       // 撑高度的占位元素
    viewport: null,     // 行容器,会被 translateY 定位
    renderRow: null,    // function(item, index, isActive) -> htmlString
    onRowClick: null,   // function(item, index, event)
    getKey: null        // function(item, index) -> string,用于跨渲染保持选中态
  };

  /**
   * @param {Object} opts 见 DEFAULTS。
   * @returns {Object} 控制器: setItems / scrollToIndex / refresh / destroy / stats
   */
  function createVirtualList(opts) {
    var cfg = {};
    for (var k in DEFAULTS) cfg[k] = DEFAULTS[k];
    if (opts) for (var k2 in opts) if (opts[k2] !== undefined) cfg[k2] = opts[k2];

    if (!cfg.scroller || !cfg.spacer || !cfg.viewport) {
      throw new Error("virtuallist: scroller/spacer/viewport 必填");
    }
    if (typeof cfg.renderRow !== "function") {
      throw new Error("virtuallist: renderRow 必填");
    }

    var items = [];
    var keys = {};          // key -> item,用于「只加载了部分页」时的高亮
    var activeKey = null;
    var frame = 0;          // 待执行的 rAF 句柄
    var lastRange = { first: -1, last: -1 };
    var paints = 0;         // 累计渲染次数,给基准测试用
    var rowsBuilt = 0;      // 累计构建过的 DOM 行数(真实工作量,非节点数)

    function height() { return cfg.scroller.clientHeight || 400; }

    /** 当前需要渲染的行区间 [first, last)。 */
    function range() {
      var top = cfg.scroller.scrollTop;
      var h = height();
      var first = Math.max(0, Math.floor(top / cfg.rowHeight) - cfg.overscan);
      var last = Math.min(items.length,
        Math.ceil((top + h) / cfg.rowHeight) + cfg.overscan);
      return { first: first, last: last };
    }

    function paint(force) {
      var r = range();
      if (!force && r.first === lastRange.first && r.last === lastRange.last) {
        return;                       // 区间没变,一个字符都不重写
      }
      lastRange = r;
      var html = "";
      for (var i = r.first; i < r.last; i++) {
        var it = items[i];
        var key = cfg.getKey ? cfg.getKey(it, i) : String(i);
        html += cfg.renderRow(it, i, key === activeKey);
        rowsBuilt++;
      }
      paints++;
      cfg.spacer.style.height = (items.length * cfg.rowHeight) + "px";
      cfg.viewport.style.transform = "translateY(" + (r.first * cfg.rowHeight) + "px)";
      cfg.viewport.innerHTML = html;
      if (cfg.onRowClick) bindClicks();
    }

    function bindClicks() {
      // 事件委托:只挂在 viewport 上,重绘 innerHTML 不会丢监听。
      cfg.viewport.onclick = function (ev) {
        var el = ev.target;
        while (el && el !== cfg.viewport && !el.getAttribute("data-vkey")) {
          el = el.parentNode;
        }
        if (!el || el === cfg.viewport) return;
        var key = el.getAttribute("data-vkey");
        var item = keys[key];
        if (item && cfg.onRowClick) cfg.onRowClick(item, ev);
      };
    }

    function onScroll() {
      // 一帧最多重绘一次。连续滚动事件比帧还密时,这是不掉帧的关键。
      if (frame) return;
      frame = (global.requestAnimationFrame || function (f) { return setTimeout(f, 16); })(function () {
        frame = 0;
        paint(false);
      });
    }

    cfg.scroller.addEventListener("scroll", onScroll, { passive: true });

    return {
      setItems: function (list) {
        items = list || [];
        keys = {};
        for (var i = 0; i < items.length; i++) {
          keys[cfg.getKey ? cfg.getKey(items[i], i) : String(i)] = items[i];
        }
        cfg.scroller.scrollTop = 0;
        lastRange = { first: -1, last: -1 };
        paint(true);
      },
      /** 只加不删,且调用方已保证末尾 —— 无限滚动追加用这个,比重设全量便宜。 */
      append: function (more) {
        if (!more || !more.length) return;
        for (var i = 0; i < more.length; i++) {
          keys[cfg.getKey ? cfg.getKey(more[i], items.length + i)
                           : String(items.length + i)] = more[i];
        }
        items = items.concat(more);
        cfg.spacer.style.height = (items.length * cfg.rowHeight) + "px";
      },
      setActiveKey: function (key) {
        activeKey = key;
        paint(true);
      },
      getActiveKey: function () { return activeKey; },
      scrollToIndex: function (idx) {
        var top = idx * cfg.rowHeight;
        var h = height();
        var cur = cfg.scroller.scrollTop;
        // 已经露出来就别动,否则会把用户正在看的内容顶走。
        if (top < cur || top + cfg.rowHeight > cur + h) {
          cfg.scroller.scrollTop = top;
        }
        paint(true);
      },
      refresh: function () { paint(true); },
      destroy: function () {
        cfg.scroller.removeEventListener("scroll", onScroll);
        if (frame) (global.cancelAnimationFrame || clearTimeout)(frame);
      },
      /** 基准测试读这个:paint 次数 / 实际构建过的行数 / 当前 DOM 行数。 */
      stats: function () {
        return {
          items: items.length,
          paints: paints,
          rowsBuilt: rowsBuilt,
          domRows: cfg.viewport.childNodes.length,
          windowRows: lastRange.last - lastRange.first,
          rowHeight: cfg.rowHeight
        };
      }
    };
  }

  var api = { create: createVirtualList, DEFAULTS: DEFAULTS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.VirtualList = api;
})(typeof window !== "undefined" ? window : this);
