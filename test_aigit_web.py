#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aigit 前端无头审计 —— 补 doc/aigit-review.md §4.5「前端一行都没测」那个洞。

工具约束:纯 Python 标准库,**不开浏览器、不开窗口**。

四层:
  L1 静态      扫 web/*.html + virtuallist.js 的 sink 与转义,不需要任何外部进程
  L2 判定      一个够用的 HTML 分词器,回答「payload 落在文本节点 / 属性值 / 标签里」
               —— 这一层是为了不靠 grep 下结论
  L3 活体      真起 server.py 子进程 + 真 git 仓库,打真 HTTP
  L4 执行      用 node 把 index.html / conflicts-demo.html 里**原封不动的那段
               <script>** 放进一个 DOM 假身里跑,捕获它写进 innerHTML 的真实字符串
               (node 缺失则跳过并标注 SKIP —— 不影响其余三层)

退出码:0 = 没有可注入的 sink;1 = 至少一个**实测确认**的注入。
        （仓库当前状态预期是 1,因为 conflicts-demo.html 有真洞。
          想只验脚本自身健康度、加 --selfcheck。）
"""

import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
TARGETS = ["index.html", "conflicts-demo.html", "virtuallist.js"]

# NTFS 禁止 < > : " / \ | ? * ,所以这些 payload 只能走 query string,
# 不可能做成真文件名。文件名那一路只用 NTFS 合法的字符。
# 注意:文件名里不能带 `/` —— Windows 上 `//` 会被当成路径分隔符,
# Python 的 open() 会真的建出一个子目录,git 报出来的路径就变了。
# 「单引号」这一条要留,它是 conflicts-demo.html 那个洞唯一能落地的文件名向量
# (index.html 的 esc 不转义单引号,但它只用双引号属性,所以那边不成立)。
FS_PAYLOADS = ["';alert(1);--.txt", "{{7+7}}.txt", "${7+7}.txt", "`x`.txt"]
Q_PAYLOADS = [
    "<img src=x onerror=alert(1)>.txt",
    '"><script>alert(1)</script>.txt',
    "';alert(1);//",
    "javascript:alert(1)",
    "{{7*7}}",
    "${7*7}",
    "</script><img src=x onerror=alert(1)>",
    '"onmouseover="alert(1)',
    "`x`",
]

RESULTS = []      # (status, layer, name, detail)
_pending = []


def rec(status, layer, name, detail=""):
    """status: PASS / FAIL / WARN / SKIP / INFO"""
    RESULTS.append((status, layer, name, detail))
    print(f"[{status:4}] {layer} {name}" + (f"\n         {detail}" if detail else ""))
    return status


def sha_of(p: Path):
    h = hashlib.sha256()
    h.update(p.read_bytes())
    st = p.stat()
    return h.hexdigest(), st.st_mtime, st.st_size


# ══════════════════════════════════════════════════════ L1 静态分析

SINKS = [
    ("innerHTML", re.compile(r"\.innerHTML\s*=")),
    ("outerHTML", re.compile(r"\.outerHTML\s*=")),
    ("insertAdjacentHTML", re.compile(r"\.insertAdjacentHTML\s*\(")),
    ("document.write", re.compile(r"document\.write(?:ln)?\s*\(")),
    ("eval", re.compile(r"(?<![.\w])eval\s*\(")),
    ("new Function", re.compile(r"new\s+Function\s*\(")),
    (".href=", re.compile(r"\.href\s*=")),
    (".src=", re.compile(r"\.src\s*=")),
    ("url(", re.compile(r"url\(\s*['\"]?javascript:", re.I)),
    ("setAttribute", re.compile(r"\.setAttribute\s*\(")),
    ("location.assign/replace", re.compile(r"location\.(?:assign|replace)\s*\(")),
    ("window.open", re.compile(r"window\.open\s*\(")),
    ("inline on* handler", re.compile(r"""on(?:click|error|load|mouseover|focus|input)\s*=\s*["']""", re.I)),
]

# 会「执行代码」的属性名 —— payload 落在这些属性里才是真洞
DANGEROUS_ATTRS = {
    "onclick", "onerror", "onload", "onmouseover", "onfocus", "oninput",
    "onsubmit", "onchange", "onmouseenter", "onanimationstart",
    "href", "src", "action", "formaction", "xlink:href", "srcdoc", "style",
}


def strip_strings_comments(js: str) -> str:
    """把 JS 里的字符串 / 行注释 / 块注释 / **正则字面量** 换成等长空白。

    为什么必须连正则字面量一起处理:index.html 的 esc() 里就有一句
        .replace(/"/g, "&quot;")
    这个 `/"/g` 里含一个双引号。不认识正则字面量的话,词法器会把它当成
    「字符串开始」,一路扫到下一个 `"` 才收工 —— 从此往后全部错位,
    后面每一行真正的 sink 都会被当成「字符串内容」抹掉。
    这个坑我踩过一次:扫出来只剩 9/22 个 innerHTML,少的那 13 个全是这个原因。

    已知取舍:模板字符串 `` `a${b}c` `` 整体当字符串抹掉,所以写在 `${...}` 插值
    里面的 sink 会被漏掉。index.html 里一个反引号都没有(已核对),所以本文件
    不受影响 —— 但这是这个词法器的真实边界,不是没有。
    """
    out = []
    i, n = 0, len(js)
    prev = "\n"          # 上一个有意义的字符,用来判断 `/` 是正则还是除号

    def emit(ch):
        out.append(ch)

    while i < n:
        c = js[i]
        # ---- 字符串
        if c in "\"'`":
            q = c
            emit(" "); i += 1
            while i < n and js[i] != q:
                if js[i] == "\\":
                    emit(" "); i += 1
                    if i < n:
                        emit(" "); i += 1
                    continue
                emit("\n" if js[i] == "\n" else " ")
                i += 1
            emit(" "); i += 1
            prev = "x"          # 字符串整体算一个「值」
            continue
        # ---- 注释
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                emit(" "); i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            emit("  "); i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                emit("\n" if js[i] == "\n" else " ")
                i += 1
            emit("  "); i += 2
            prev = "x"
            continue
        # ---- 正则字面量:前一个有意义的字符决定这是正则还是除号
        if c == "/" and (prev == "\n" or prev in "(,=:[!&|?{};+-*%~^<>"):
            j = i + 1
            in_class = False
            ok = False
            while j < n:
                ch = js[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break                      # 正则不能跨行 → 当成除号
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    ok = True
                    break
                j += 1
            if ok:
                j += 1
                while j < n and js[j].isalpha():   # g / i / m / s / u / y 标志
                    j += 1
                for k in range(i, j):
                    emit("\n" if js[k] == "\n" else " ")
                i = j
                prev = "x"
                continue
        emit(c)
        if not c.isspace():
            prev = c
        elif c == "\n":
            prev = "\n"
        i += 1
    return "".join(out)


def lexer_selftest():
    """给上面那个词法器自测。静态层的所有结论都建立在它身上,坏了就得知道。"""
    cases = [
        # (源码, 抹掉后必须仍然保留的代码片段, 必须被抹掉的字符串/正则/注释片段)
        # 正则字面量整体抹掉 —— 里面没有 sink,而且不抹就会让后面全部错位
        ('s.replace(/"/g, "&quot;");', ["s.replace(", ");"], ['/"/g', "&quot;"]),
        ("var a = 'x' + b;", ["var a = ", " + b;"], ["'x'"]),
        ("var a = 'he said \"hi\"';", ["var a = "], ["he said", "hi"]),
        # 模板字符串整体抹掉(含 ${}),这是本词法器的已知边界,见 docstring
        ("t = `a${b}c`;", ["t = ", ";"], ["a", "b", "c"]),
        ("var q = a / b / c;", ["a / b / c"], []),          # 除号不能当正则
        ("var r = /[/]/g;", ["var r = "], ["/[/]/g"]),       # 字符类里的 / 不结束正则
        ("// fake .innerHTML = 1\nvar x = 1;", ["var x = 1"], [".innerHTML"]),
        ("/* blk .innerHTML */ var y = 2;", ["var y = 2"], [".innerHTML"]),
        ("if (L.indexOf('@@') === 0) { var z = 3; }",
         ["if (L.indexOf(", ") === 0) { var z = 3; }"], ["@@"]),
        ("var w = /^(index |new file mode)$/.test(L);",
         ["var w = ", ".test(L);"], ["index |new file mode"]),
    ]
    bad = []
    for src, keep, kill in cases:
        got = strip_strings_comments(src)
        if len(got) != len(src):
            bad.append(f"长度被破坏 {len(src)}->{len(got)}: {src!r}")
            continue
        if got.count("\n") != src.count("\n"):
            bad.append(f"行数被破坏: {src!r}")
        for k in keep:
            if k not in got:
                bad.append(f"代码片段被误抹 {k!r}  <-  {src!r}  => {got!r}")
        for k in kill:
            if k in got:
                bad.append(f"字符串/注释没抹掉 {k!r}  <-  {src!r}  => {got!r}")
    rec("PASS" if not bad else "FAIL", "L1", "JS 词法器自测(静态层的地基)",
        f"{len(cases)} 个用例全过" if not bad else "\n".join(bad))
    return not bad


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def static_scan():
    print("\n" + "=" * 72)
    print("L1 静态分析")
    print("=" * 72)
    lexer_selftest()
    files = {}
    for name in TARGETS:
        p = WEB / name
        if not p.exists():
            rec("WARN", "L1", f"{name} 不存在")
            continue
        files[name] = p.read_text(encoding="utf-8", errors="replace")

    def js_bodies(text, name):
        """返回 [(脚本文本, 该脚本首行在整份文件里的行号)]。
        没有 <script> 的文件(.js)就整份当脚本,偏移 0。"""
        if not name.endswith(".html"):
            return [(text, 1)]
        out = []
        for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", text, re.I):
            # 脚本正文的第一个字符就在 <script...> 那一行,所以偏移 = <script 所在行
            out.append((m.group(1), line_of(text, m.start())))
        return out or [(text, 1)]

    # --- 1a sink 清单(行号换算回整份文件)
    for name, text in files.items():
        for body, off in js_bodies(text, name):
            stripped = strip_strings_comments(body)
            hits = []
            for label, rx in SINKS:
                for m in rx.finditer(stripped):
                    hits.append((line_of(stripped, m.start()) + off - 1, label,
                                 m.group(0).strip()))
            hits.sort()
            for ln, label, frag in hits:
                rec("INFO", "L1", f"sink  {name}:{ln}", f"{label}   ←  {frag}")

    # --- 1b 静态 HTML 里的内联事件处理器
    for name, text in files.items():
        if not name.endswith(".html"):
            continue
        body = strip_strings_comments(text)
        for m in SINKS[11][1].finditer(body):
            rec("INFO", "L1", f"sink  {name}:{line_of(body, m.start())}",
                f"内联 {m.group(0).strip()}")

    # --- 1c esc() 的定义,以及它到底转义了哪几个字符
    for name, text in files.items():
        for body, off in js_bodies(text, name):
            for m in re.finditer(r"(?:function\s+esc\b|(?:const|let|var)\s+esc\s*=)", body):
                seg = body[m.start():m.start() + 500]
                quoted = re.findall(r"replace\(\s*/(.?)/g", seg[:400])
                fl = line_of(body, m.start()) + off - 1
                rec("INFO", "L1", f"esc() 定义  {name}:{fl}",
                    f"转义字符 = {' '.join(repr(c) for c in quoted) or '(没解析出)'}  "
                    f"含单引号 {'是' if chr(39) in quoted else '否!! ← 危险'}  "
                    f"含双引号 {'是' if chr(34) in quoted else '否!! ← 危险'}")

    # --- 1d URL sink 总数(报告点名的第 4 项)
    url_sinks = []
    for name, text in files.items():
        for body, off in js_bodies(text, name):
            for label, rx in SINKS[7:]:
                for m in rx.finditer(strip_strings_comments(body)):
                    url_sinks.append((name, line_of(body, m.start()) + off - 1, label))
        for m in re.finditer(r"""(?:href|src)\s*=\s*["']""", text):
            url_sinks.append((name, line_of(text, m.start()), "静态 href/src 属性"))
    rec("PASS" if not url_sinks else "FAIL", "L1",
        "URL sink(href / src / url( / javascript:)总数",
        "0 个 —— 前端根本没有把任何数据拼进 URL 的地方,"
        "所以报告点名的「javascript: URL 注入」这一类在本项目不成立(实测,非推断)"
        if not url_sinks else f"{len(url_sinks)} 个: {url_sinks}")
    return files


# ══════════════════════════════════════════════════════ L2 HTML 判定

def parse_regions(html: str):
    """把 HTML 切成区域,给每个字符标一个 mode。

    mode ∈ {TEXT, TAG_NAME, ATTR_NAME, ATTR_VALUE(属性名), COMMENT}
    返回 [(start, end, mode, attr_name)]
    """
    regions = []
    i, n = 0, len(html)
    buf = []

    def flush(m, extra=""):
        if buf:
            regions.append((i - len(buf), i, m, extra))
            buf.clear()

    while i < n:
        c = html[i]
        if c != "<":
            buf.append(c)
            i += 1
            continue
        # 注释
        if html.startswith("<!--", i):
            flush("TEXT")
            j = html.find("-->", i + 4)
            j = n if j < 0 else j + 3
            regions.append((i, j, "COMMENT", ""))
            i = j
            continue
        # 结束标签
        if html.startswith("</", i):
            flush("TEXT")
            j = html.find(">", i)
            j = n - 1 if j < 0 else j
            regions.append((i, j + 1, "TAG_NAME", "/"))
            i = j + 1
            continue
        # 开始标签
        flush("TEXT")
        start = i
        i += 1
        tagname_start = i
        while i < n and html[i] not in " \t\r\n/>":
            i += 1
        tag = html[tagname_start:i]
        regions.append((start, i, "TAG_NAME", tag))
        # 属性
        while i < n:
            while i < n and html[i] in " \t\r\n":
                buf_s = html[i]
                if buf:
                    regions.append((i, i + 1, "TEXT", ""))
                i += 1
            if i >= n:
                break
            if html[i] == ">":
                regions.append((i, i + 1, "TAG_NAME", ">"))
                i += 1
                break
            if html[i] == "/" and i + 1 < n and html[i + 1] == ">":
                regions.append((i, i + 2, "TAG_NAME", "/>"))
                i += 2
                break
            astart = i
            while i < n and html[i] not in "= \t\r\n/>":
                i += 1
            aname = html[astart:i]
            regions.append((astart, i, "ATTR_NAME", aname))
            while i < n and html[i] in " \t\r\n":
                i += 1
            if i < n and html[i] == "=":
                regions.append((i, i + 1, "ATTR_NAME", "="))
                i += 1
                while i < n and html[i] in " \t\r\n":
                    i += 1
                if i < n and html[i] in "\"'":
                    q = html[i]
                    # 两个引号本身都各占一个区域。落在 ATTR_QUOTE 上意味着
                    # payload 的裸引号提前关掉了/重新打开了某个属性。
                    regions.append((i, i + 1, "ATTR_QUOTE", aname))
                    vstart = i + 1
                    j = html.find(q, vstart)
                    j = n if j < 0 else j
                    regions.append((vstart, j, "ATTR_VALUE", aname))
                    if j < n:
                        regions.append((j, j + 1, "ATTR_QUOTE", aname))
                    i = j + 1
                else:
                    vstart = i
                    while i < n and html[i] not in " \t\r\n>":
                        i += 1
                    regions.append((vstart, i, "ATTR_VALUE", aname))
            if not aname:
                i += 1
    flush("TEXT")
    return regions


def classify(html: str, payload: str):
    """payload 在这段 HTML 里会被浏览器当成什么。返回 (verdict, reason)。"""
    regions = parse_regions(html)
    idx = html.find(payload)
    if idx < 0:
        return _classify_absent(html, payload, regions)
    return _classify_at(html, payload, idx, regions)


def _inert_after_breakout(tail: str):
    """逃逸出来的引号后面那段文本,到底会不会执行?

    只看捕获到的 HTML 字符串本身,不猜浏览器:
      * 后面还有 `<字母` 或 `</` → 会造出一个新标签
      * 后面还有 ` on<事件名>=` → 会造出一个新的事件处理器属性
    两者都没有 → 逃逸了但这次没造成执行(仍要报出来,不能当安全)"""
    opens_tag = re.search(r"</?[A-Za-z]", tail) is not None
    opens_handler = re.search(r"\son[a-z]+\s*=", tail, re.I) is not None
    return opens_tag, opens_handler


def _classify_at(html, payload, idx, regions):
    for (s, e, mode, attr) in regions:
        if s <= idx < e:
            if mode == "TEXT":
                return "SAFE_TEXT", f"落在文本节点内(偏移 {s}-{e}),浏览器只当字面量显示"
            if mode == "COMMENT":
                return "SAFE_COMMENT", "落在注释里,不执行"
            if mode == "ATTR_QUOTE":
                tag, handler = _inert_after_breakout(html[idx + 1:])
                if handler:
                    return ("DANGER_ATTR_BREAKOUT",
                            f"payload 的裸引号**提前关掉了 {attr!r} 属性**,"
                            f"后面紧跟着新的事件处理器属性 → 会执行")
                if tag:
                    return ("DANGER_ATTR_BREAKOUT",
                            f"payload 的裸引号提前关掉了 {attr!r} 属性,"
                            f"后面紧跟着 `<` 开头的标签 → 会造出真元素")
                return ("WARN_ATTR_BREAKOUT",
                        f"payload 的裸引号提前关掉了 {attr!r} 属性,"
                        f"但后面没有可执行内容 → 本次不执行,逃逸本身仍然存在")
            if mode == "ATTR_VALUE":
                if attr.lower().startswith("on"):
                    # 事件处理器属性里,HTML 转义规则不保护 JS 字面量
                    return ("DANGER_EVENT_ATTR",
                            f"落在属性 {attr!r} 的值里 —— 这是事件处理器属性,"
                            f"里面的内容是 JS 源码,HTML 实体不提供任何保护")
                if attr.lower() in ("href", "src", "action", "formaction", "srcdoc"):
                    return ("WARN_URL_ATTR", f"落在 URL 属性 {attr!r} 的值里")
                return "SAFE_ATTR", f"落在普通属性 {attr!r} 的值里"
            if mode == "ATTR_NAME":
                return "DANGER_ATTR_NAME", f"落在属性名位置(上一标签的 {attr!r} 属性)"
            if mode == "TAG_NAME":
                return "DANGER_TAG", f"落在标签名位置(标签 {attr!r})"
    return "SAFE_OTHER", "落在已解析区域的边界上"


def _classify_absent(html, payload, regions):
    """payload 原样字面量不在 HTML 里。

    两种子情形必须分开,否则会误判:
      (a) 它的 `<` `>` 之类被转义了 → 纯文本,安全;
      (b) 它的**引号没被转义**,但因为同串里 `<` 被转义了,导致整个 payload
          找不到原样字面量 —— 这时引号逃逸照样成立,只是这次没造成执行。
    """
    probe = (payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))
    if probe in html:
        return ("SAFE_ESCAPED",
                f"payload 的元字符被转义后出现在页面里({probe!r})→ 浏览器解析成纯文本")

    for q in ("'", '"'):
        if q not in payload:
            continue
        qi = html.find(q)
        if qi < 0:
            continue
        for (s, e, mode, attr) in regions:
            if s <= qi < e and mode == "ATTR_QUOTE":
                tag, handler = _inert_after_breakout(html[qi + 1:])
                if handler or tag:
                    return ("DANGER_ATTR_BREAKOUT",
                            f"payload 含未转义的 {q!r},它落在 {attr!r} 属性的引号位置上 → "
                            f"属性逃逸,且后面有可执行内容")
                return ("WARN_ATTR_BREAKOUT",
                        f"payload 含未转义的 {q!r},落在 {attr!r} 属性的引号位置上 → "
                        f"属性逃逸成立,但本 payload 后续没有可执行内容")
    return ("ABSENT",
            "HTML 里既没有原样字面量,也没有转义形式 —— "
            "这个值可能根本没进 DOM,**需要人工确认**")


def unescape_attr(v: str) -> str:
    v = re.sub(r"&(quot|#34|#x22);", '"', v, flags=re.I)
    v = re.sub(r"&(apos|#39|#x27);", "'", v, flags=re.I)
    v = re.sub(r"&(lt|#60);", "<", v, flags=re.I)
    v = re.sub(r"&(gt|#62);", ">", v, flags=re.I)
    v = re.sub(r"&nbsp;", "\u00a0", v, flags=re.I)
    v = re.sub(r"&amp;", "&", v, flags=re.I)
    return v


# ══════════════════════════════════════════════════════ L3 活体服务

class AigitServer:
    def __init__(self, repo: Path, port: int):
        self.repo, self.port, self.proc = repo, port, None
        self.base = f"http://127.0.0.1:{port}"

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "server.py"), "--root", str(self.repo),
             "--port", str(self.port)],
            cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(120):
            if self.proc.poll() is not None:
                raise RuntimeError("server.py 起不来:\n"
                                   + self.proc.stderr.read().decode("utf-8", "replace"))
            try:
                self.get("/api/health")
                return self
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("server.py 120 次轮询仍未就绪")

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def get(self, p, timeout=15):
        try:
            with urllib.request.urlopen(self.base + p, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(self, p, obj, timeout=60):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.base + p, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "aigit-a2@localhost")
    git(repo, "config", "user.name", "aigit-a2")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("# aigit-A2\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ══════════════════════════════════════════════════════ L4 真正执行 JS

JS_HARNESS = r"""
'use strict';
const fs = require('fs');
const OUT = { cases: [], errors: [], esc: {}, node: process.version };
function out(o){ OUT.cases.push(o); }

function mkEl(id){
  const el = {
    id: id || '',
    _html: '', _text: '', _attrs: {},
    style: {}, children: [], childNodes: [],
    className: '', title: '', value: '', disabled: false, type: '',
    clientHeight: 400, scrollTop: 0, scrollHeight: 0,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    appendChild(c){ this.children.push(c); this.childNodes.push(c); return c; },
    removeChild(){}, addEventListener(){}, removeEventListener(){},
    focus(){}, select(){}, closest(){ return null; }, contains(){ return false; },
    setAttribute(k,v){ this._attrs[k]=v; if(k==='innerHTML') this._html=v; },
    getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    get innerHTML(){ return this._html; },
    set innerHTML(v){ this._html = v; OUT.writes.push({id:this.id, prop:'innerHTML', value:v}); },
    get textContent(){ return this._text; },
    set textContent(v){ this._text = v; OUT.writes.push({id:this.id, prop:'textContent', value:String(v)}); },
    insertAdjacentHTML(pos, html){ OUT.writes.push({id:this.id, prop:'insertAdjacentHTML:'+pos, value:html}); },
  };
  return el;
}

function buildDom(html){
  OUT.writes = [];
  const els = {};
  for (const m of html.matchAll(/\bid="([^"]+)"/g)) els[m[1]] = els[m[1]] || mkEl(m[1]);
  const document = {
    body: mkEl('body'),
    getElementById(id){ return els[id] || (els[id] = mkEl(id)); },
    createElement(t){ return mkEl('#new-'+t); },
    querySelector(sel){ return els[sel.replace(/^#/,'') ] || (els[sel.replace(/^#/,'')]=mkEl(sel)); },
    querySelectorAll(){ return []; },
    addEventListener(){},
  };
  return {document, els};
}

// 把 index.html 里**原封不动**的那段 <script> 跑起来
function loadIndex(){
  const html = fs.readFileSync(WEBDIR + '/index.html', 'utf8');
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
  if (!blocks.length) throw new Error('index.html 里找不到内联 <script>');
  const src = blocks[blocks.length-1];
  const {document} = buildDom(html);
  /* 破坏性操作走两步之后，doDiscard/doReset 一进来就 GET 预演, 所以这个
     fetch 桩**第一次被真正用上了**(改之前只有 onOk 里那次 POST,而用例从不
     点确定)。FETCH_DATA 以前在这里被引用却**从没定义过** —— 那时是无害的,
     因为没有一条用例真的调 api(); 现在它是被依赖的, 缺了就是 ReferenceError。

     预演响应里**故意塞 payload**: 确认框现在渲染的是服务端回的 path/detail,
     所以真正要验的注入面从「点击时传的参数」搬到了「服务端响应」——
     桩不给 payload 的话这条用例会测一个已经没人走的 sink。 */
  const FETCH_DATA = {
    '/api/discard/plan': {
      ok: true, op: 'discard',
      entries: (process.env.A2_PAYLOADS ? JSON.parse(process.env.A2_PAYLOADS) : [])
        .map((p, i) => ({path: p, action: ['restore','delete-file','delete-tree'][i % 3],
                         detail: 'detail-' + p})),
      blocked: [],
      untracked_total: process.env.A2_PAYLOADS ? JSON.parse(process.env.A2_PAYLOADS) : [],
      untracked_count: 1,
      note: 'note **强调** <b>raw</b>'
    },
    '/api/reset/plan': {
      ok: true, op: 'reset', mode: 'hard', ref: 'HEAD',
      undone_commits: (process.env.A2_PAYLOADS ? JSON.parse(process.env.A2_PAYLOADS) : [])
        .map(p => ({sha: '0'.repeat(40), short: p.slice(0,8), summary: 'sum-' + p})),
      note: 'hard: note **强调** <b>raw</b>',
      warning: 'warning <b>raw</b>'
    }
  };
  const calls = [];
  const fetch = (url, init) => {
    calls.push({url: String(url), method: (init && init.method) || 'GET',
                body: init && init.body});
    return Promise.resolve({
      status: 200,
      text: () => Promise.resolve(JSON.stringify(
        FETCH_DATA[String(url).split('?')[0]] || {ok:true}))
    });
  };
  const fn = new Function('document','fetch','window','setTimeout','console', src + `
;return {S:S, esc:esc, paintV:paintV, renderLog:renderLog, renderMerge:renderMerge,
 renderScenarios:renderScenarios, renderDiffBody:renderDiffBody, parseDiff:parseDiff,
 renderSidebar:renderSidebar, renderTop:renderTop, toast:toast, fail:fail,
 openModal:openModal, doCheckout:doCheckout, doDiscard:doDiscard, doReset:doReset,
 resetModeChecked:resetModeChecked, loadResetPlan:loadResetPlan,
 summarize:summarize,
 noteHtml:noteHtml, stClass:stClass, rowKey:rowKey, trackedPaths:trackedPaths,
 buildRows:buildRows, findItem:findItem, loadDiff:loadDiff, switchTab:switchTab};`);
  return {api: fn(document, fetch, {}, setTimeout, console), document, calls};
}

function loadConflicts(){
  const html = fs.readFileSync(WEBDIR + '/conflicts-demo.html', 'utf8');
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
  const src = blocks[blocks.length-1];
  const {document} = buildDom(html);
  const fetch = () => Promise.resolve({status:200, json: () => Promise.resolve({ok:false, error:'stub'})});
  const fn = new Function('document','fetch','window','setTimeout','console', src +
    ';return {drawList:drawList, note:note, esc:esc, reload:reload};');
  return {api: fn(document, fetch, {}, setTimeout, console), document};
}

const PAYLOADS = JSON.parse(process.env.A2_PAYLOADS);
const WEBDIR = process.env.A2_WEBDIR;

/* 点模态框的「确定」。openModal 往 #modalFoot 里 appendChild 的顺序是
   [取消, 确定], 确定那个的 onclick 就是 modalOnOk。用例要走到 POST 就必须
   真点它 —— 否则序列里只有预演那一次 GET, 断言「POST 带 confirmed」就成了
   自欺欺人(实测踩过: 没点确定时「一次 POST 都没有」被当成产品缺陷报了 FAIL,
   其实只是用例没点到按钮)。

   document 必须由调用方传进来: 桩身是 loadIndex() 里的局部变量, 外面那个
   标识符根本不存在(第一版就这么写的, 直接 ReferenceError)。 */
function clickModalOk(doc){
  const foot = doc.getElementById('modalFoot');
  const btns = (foot && foot.children) || [];
  const ok = btns[btns.length - 1];
  if (ok && typeof ok.onclick === 'function') ok.onclick();
  else OUT.errors.push('clickModalOk: #modalFoot 里没找到带 onclick 的确定按钮');
}
/* 让 fetch 桩那些已经 resolve 的 promise 链跑完(微任务 + 一层 then)。 */
function tick(){ return new Promise(r => setTimeout(r, 0)); }

// ---------- index.html ----------
/* async: 破坏性操作改成两步之后, doDiscard / loadResetPlan 一进来就 GET 预演,
   确认框要等那个 promise 落地才写进 #modalBody。同步取的话读到的是**上一个**
   模态框(第 8 条 doCheckout 那个)的残留 —— 用例照样 PASS, 但它验的是错的
   sink。宁可 await, 也不要一个看起来绿、其实没测到东西的用例。 */
async function runIndex(){
try {
  const {api, calls, document} = loadIndex();
  OUT.esc.index = {
    lt: api.esc('<'), gt: api.esc('>'), amp: api.esc('&'),
    dq: api.esc('"'), sq: api.esc("'"),
  };
  const I = api;

  // 1 侧栏:文件路径(已跟踪 M / 未跟踪 ?)两种,两个 action 按钮都带 data-path
  for (const st of ['M','?']){
    I.S.rows = [];
    I.S.status = { branch:'main', root:'/tmp', conflicted:[],
      staged: [], unstaged:[{path: PAYLOADS[0], status: st, staged:false}] };
    I.renderSidebar();
    const w = OUT.writes.filter(x=>x.id==='vrows' && x.prop==='innerHTML').pop();
    out({ case:'sidebar-'+st, sink:'#vrows.innerHTML',
          html: w ? w.value : '(没写)', payload: PAYLOADS[0] });
  }

  // 2 侧栏:文件名里带单引号(专门打 esc() 不转义单引号这点)
  I.S.rows = [];
  I.S.status = { branch:'main', root:'/tmp', conflicted:[], staged:[],
    unstaged:[{path: PAYLOADS[1], status:'M', staged:false}] };
  I.renderSidebar();
  out({ case:'sidebar-squote', sink:'#vrows.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='vrows'&&x.prop==='innerHTML').pop()||{value:''}).value,
        payload: PAYLOADS[1] });

  // 3 提交历史
  I.S.log = [{sha:PAYLOADS[0], author:PAYLOADS[0], date:PAYLOADS[0], subject:PAYLOADS[0]}];
  I.renderLog();
  out({ case:'log', sink:'#logRows.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='logRows').pop()||{value:''}).value,
        payload: PAYLOADS[0] });

  // 4 分支表 / 顶栏下拉
  I.S.status = { branch: PAYLOADS[0], root:'/tmp', conflicted:[], staged:[], unstaged:[] };
  I.S.branches = [{name:PAYLOADS[0], current:false}, {name:"'", current:false}, {name:'main', current:true}];
  I.renderTop();
  I.S.scenarios = [];
  I.renderMerge();
  const mt = OUT.writes.filter(x=>x.prop==='innerHTML').map(x=>x.value).join('\n');
  out({ case:'branches', sink:'renderMerge tr.innerHTML + #branchSel option',
        html: mt, payload: PAYLOADS[0] });

  // 5 场景明细表
  I.S.scenarios = [{ id:1, kind: PAYLOADS[0],
    branches:[PAYLOADS[0], PAYLOADS[1]], files:[PAYLOADS[0]],
    expect: PAYLOADS[0], verified:false }];
  I.renderScenarios();
  out({ case:'scenarios', sink:'#scenWrap.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='scenWrap').pop()||{value:''}).value,
        payload: PAYLOADS[0] });

  // 6 diff 正文:把 payload 当成一行新增内容
  const diffText = ['diff --git a/'+PAYLOADS[0]+' b/'+PAYLOADS[0],
                    '--- a/'+PAYLOADS[0], '+++ b/'+PAYLOADS[0],
                    '@@ -0,0 +1,2 @@', '+'+PAYLOADS[0], '+'+PAYLOADS[1]].join('\n');
  I.renderDiffBody(I.parseDiff(diffText));
  out({ case:'diff-body', sink:'#diffBox.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='diffBox').pop()||{value:''}).value,
        payload: PAYLOADS[0] });

  // 7 服务端原样回显的 path 走进错误提示条(fail -> toast -> insertAdjacentHTML)
  I.fail('读取差异', new Error(PAYLOADS[0]));
  const note = OUT.writes.filter(x=>x.prop && x.prop.startsWith('insertAdjacentHTML')).pop();
  out({ case:'server-echo-error', sink:'#diffBox.insertAdjacentHTML',
        html: note ? note.value : '(没写)', payload: PAYLOADS[0] });

  // 8 模态框:分支名进 modalBody
  I.doCheckout(PAYLOADS[0]);
  out({ case:'modal', sink:'#modalBody.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='modalBody').pop()||{value:''}).value,
        payload: PAYLOADS[0] });

  // 9 discard 确认框。两步协议: 渲染的是**服务端预演回的** path/detail,
  //   所以注入面从「点击时传的参数」搬到了「服务端响应」。
  OUT.writes = [];
  await I.doDiscard([PAYLOADS[0], PAYLOADS[1]]);
  out({ case:'discard-modal', sink:'#modalBody.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='modalBody').pop()||{value:''}).value,
        payload: PAYLOADS[0] });

  // 点「确定」, 让那个 POST 真的发出去。不点的话 calls 里只有预演那一次 GET,
  //   而「POST 带没带 confirmed」正是这条用例的全部意义 —— 只 GET 的话
  //   接线断言会自己骗自己。
  OUT.writes = [];
  clickModalOk(document);
  await tick();
  OUT.discardCalls = calls.slice();

  // 9c reset 的模式选择框。**不走注入判定**: 这个框里没有一行服务端数据,
  //    全是我们自己写的静态文案 + 一个硬编码的 radio 列表, payload 是空串。
  //    喂给 classify 只会把正文里我们自己写的 <b> 报成 DANGER_TAG(实测踩过:
  //    「必须显式选」那几个 <b> 被判成危险标签)。它真正要证明的是
  //    「三个模式都在、且一个都没预选」—— 那是需求, 交给下面的结构断言。
  OUT.writes = [];
  I.doReset();
  OUT.resetModalHtml =
    (OUT.writes.filter(x=>x.id==='modalBody').pop()||{value:''}).value;
  OUT.resetModeDefault = I.resetModeChecked();

  // 9d reset 走完两步: 预演回来之后弹第二个框, 渲染 note / undone_commits /
  //    untracked_total —— 全是服务端给的字符串, 同样要过 esc。
  OUT.writes = [];
  await I.loadResetPlan('hard');
  out({ case:'reset-plan-modal', sink:'#modalBody.innerHTML',
        html:(OUT.writes.filter(x=>x.id==='modalBody').pop()||{value:''}).value,
        payload: PAYLOADS[0] });
  // 点「确定」—— 不点就永远看不到 POST, 而 POST 带没带 confirmed 正是这条
  //   用例的全部意义。modalFoot.children = [取消, 确定](openModal 里的顺序)。
  OUT.writes = [];
  clickModalOk(document);
  await tick();
  OUT.resetHardCalls = calls.slice();
  calls.length = 0;          // 两条流程的调用序列必须分开记, 否则混在一起看不出谁是谁

  // 9e **再走一遍 soft**。这一遍专门是为了钉住「mode 是用户选的那个」:
  //     只跑 hard 的话, 把 body 里的 mode 写死成 "hard" 是**测不出来的**
  //     —— "hard" 是合法三态之一, 断言 in (soft,mixed,hard) 照样过。
  //     实测踩过: 变异体把 mode 硬编码成 "hard", 第一版接线断言全绿。
  //     跑一遍非 hard 的档, 「写死」就立刻现形。
  OUT.writes = [];
  await I.loadResetPlan('soft');
  clickModalOk(document);
  await tick();
  OUT.resetSoftCalls = calls.slice();

  // 10 esc() 单元行为
  OUT.esc.samples = PAYLOADS.map(p => ({p:p, out:I.esc(p)}));

  // 11 前端到底会不会用服务端回显的 d.path?
  I.S.rows=[]; I.S.status={branch:'main',root:'/tmp',conflicted:[],staged:[],
      unstaged:[{path:'plain.txt', status:'M', staged:false}]};
  I.buildRows();
  OUT.rowKeyRoundTrip = I.S.rows.filter(r=>r.type==='file').map(r=>r.key);

  // 12 顶栏下拉里到底有没有 HEAD
  I.S.branches = JSON.parse(process.env.A2_BRANCHES || '[]');
  I.S.status = { branch:'main', root:'/tmp', conflicted:[], staged:[], unstaged:[] };
  OUT.writes = [];
  I.renderTop();
  OUT.branchOptions = I.S.branches.map(b=>b.name);

  // 13 F-05 rename 幽灵条目:用真 status 造出 ghost,看 buildRows 收不收
  I.S.status = JSON.parse(process.env.A2_STATUS || '{"staged":[],"unstaged":[],"conflicted":[]}');
  I.S.rows = [];
  I.buildRows();
  OUT.ghostRows = I.S.rows.filter(r=>r.type==='file')
      .map(r => ({path: r.item.path, status: r.item.status, key: r.key}));

} catch (e) {
  OUT.errors.push('index: ' + (e && e.stack || e));
}
}

// ---------- conflicts-demo.html ----------
try {
  const {api} = loadConflicts();
  const C = api;
  OUT.esc.conflicts = { lt:C.esc('<'), gt:C.esc('>'), dq:C.esc('"'), sq:C.esc("'") };
  for (const p of [PAYLOADS[0], PAYLOADS[1], PAYLOADS[2], PAYLOADS[7]]){
    if (p === undefined) continue;
    OUT.writes = [];
    C.drawList([{path:p, name:p, columns:'3', regionCount:1,
                 oursLabel:'ours', theirsLabel:'theirs', verified:true}]);
    out({ case:'conflicts-drawlist', sink:'#list.innerHTML (含内联 onclick)',
          html:(OUT.writes.filter(x=>x.id==='list').pop()||{value:''}).value, payload:p });
  }
} catch (e) {
  OUT.errors.push('conflicts: ' + (e && e.stack || e));
}

/* 收尾必须排在 runIndex() 之后 —— 里面有两个 await, 同步 write 的话
   9d 那条会在预演回来之前就把结果吐出去, OUT.resetCalls 整个是 undefined。 */
runIndex().then(function(){
  process.stdout.write('@@A2@@' + JSON.stringify(OUT));
}).catch(function (e) {
  process.stdout.write('@@A2@@' + JSON.stringify(
    Object.assign(OUT, {errors: OUT.errors.concat(['tail: ' + (e && e.stack || e)])})));
});
"""


def run_node(tmp: Path, payload_list, branches, status_json):
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        for cand in (r"D:\cangku\nodejs\node.exe",
                     r"C:\Program Files\nodejs\node.exe"):
            if Path(cand).exists():
                node = cand
                break
    if not node:
        return None, "PATH 里没有 node / nodejs,也没有 D:\\cangku\\nodejs\\node.exe"

    harness = tmp / "a2_harness.js"
    harness.write_text(JS_HARNESS, encoding="utf-8")
    env = dict(os.environ)
    env.update(A2_PAYLOADS=json.dumps(payload_list, ensure_ascii=False),
               A2_WEBDIR=str(WEB),
               A2_BRANCHES=json.dumps(branches, ensure_ascii=False),
               A2_STATUS=json.dumps(status_json, ensure_ascii=False),
               NODE_DISABLE_COLORS="1")
    p = subprocess.run([node, str(harness)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180,
                       cwd=str(tmp))
    m = re.search(r"@@A2@@(\{.*\})", p.stdout or "", re.S)
    if not m:
        return None, f"node 跑完但没吐出结果。rc={p.returncode}\n" \
                     f"stdout={p.stdout[-1500:]}\nstderr={p.stderr[-1500:]}"
    data = json.loads(m.group(1))
    data["_node_path"] = node
    return data, None


# ══════════════════════════════════════════════════════ 判定

BAD = {"DANGER_TAG", "DANGER_ATTR_NAME", "DANGER_EVENT_ATTR", "DANGER_ATTR_BREAKOUT"}


DUMP = []


def judge(html, payload, sink, case):
    verdict, reason = classify(html or "", payload)
    st = ("FAIL" if verdict in BAD
          else "WARN" if verdict.startswith(("WARN_URL", "WARN_ATTR"))
          else "PASS")
    rec(st, "L2/L4", f"{case}  →  {sink}", f"[{verdict}] {reason}")
    DUMP.append((case, sink, payload, html or "", verdict, reason))
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true",
                    help="只验脚本自身健康度(不把已知漏洞算成失败)")
    ap.add_argument("--dump", action="store_true",
                    help="把每个 sink 捕获到的原始 HTML 原样打出来")
    args = ap.parse_args()

    print("=" * 72)
    print("aigit 前端无头审计 (test_aigit_web.py)")
    print("=" * 72)
    print("web/ 目录:")
    for name in TARGETS:
        p = WEB / name
        if p.exists():
            print(f"  {name:24} {p.stat().st_size:>7} B  sha256={hashlib.sha256(p.read_bytes()).hexdigest()[:16]}")

    static_scan()
    tmp = Path(tempfile.mkdtemp(prefix="aigit-A2-"))
    live = {}
    try:
        _live_layers(tmp, live)
        _node_layer(tmp, live)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n[INFO ] 临时目录已清理 {tmp}")

    n_fail = sum(1 for s, *_ in RESULTS if s == "FAIL")
    n_skip = sum(1 for s, *_ in RESULTS if s == "SKIP")
    n_warn = sum(1 for s, *_ in RESULTS if s == "WARN")

    if args.dump and DUMP:
        print("\n" + "=" * 72)
        print("每个 sink 捕获到的原始 HTML(--dump)")
        print("=" * 72)
        for case, sink, payload, html, verdict, reason in DUMP:
            print(f"\n--- {case}  [{verdict}]")
            print(f"    sink    : {sink}")
            print(f"    payload : {payload!r}")
            print(f"    判定    : {reason}")
            print(f"    HTML    : {html.strip()[:700]}")

    print("\n" + "=" * 72)
    print(f"汇总:{sum(1 for s,*_ in RESULTS if s=='PASS')} PASS / {n_fail} FAIL / "
          f"{n_warn} WARN / {n_skip} SKIP   共 {len(RESULTS)} 条")
    if args.selfcheck:
        print("--selfcheck:忽略所有 FAIL")
        return 0
    if n_fail:
        print("→ 至少一个实测确认的注入点,退出码 1")
        return 1
    print("→ 没有可注入的 sink,退出码 0")
    return 0


def _live_layers(tmp: Path, live: dict):
    print("\n" + "=" * 72)
    print("L3 活体服务:真 server.py 子进程 + 真 git 仓库")
    print("=" * 72)
    repo = make_repo(tmp)

    # 提交历史里塞一条带 payload 的主题(必须先做,否则后面 --allow-empty 会把 rename 一起吃掉)
    git(repo, "commit", "-q", "--allow-empty", "-m", "<img src=x onerror=alert(1)>")
    # F-05:造**三个真的** rename,其中 o_name.txt 正是报告点名的那个形态
    for old, new in (("old.txt", "new.txt"),
                     ("o_name.txt", "n_name.txt"),
                     ("aa.txt", "bb.txt")):
        (repo / old).write_text("x\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add files to rename")
    for old, new in (("old.txt", "new.txt"),
                     ("o_name.txt", "n_name.txt"),
                     ("aa.txt", "bb.txt")):
        r = git(repo, "mv", old, new)
        rec("INFO", "L3", f"git mv {old} -> {new}",
            f"rc={r.returncode} {(r.stdout + r.stderr).strip()}")

    # F-05 的证据本体:git 真正吐给 server.py 的那串字节
    raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    chunks = raw.split("\0")
    rec("INFO", "L3", "git status --porcelain=v1 -z 的原始字节(按 \\0 切开)",
        " | ".join(repr(c) for c in chunks))
    rec("INFO", "L3", "同一份仓库的非 -z 视图(给人看)",
        git(repo, "status", "--porcelain=v1").stdout.rstrip().replace("\n", " ; "))

    # 未跟踪文件:只能用 NTFS 合法字符
    for p in FS_PAYLOADS:
        full = repo / p
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("hello\n", encoding="utf-8", newline="\n")

    port = free_port()
    with AigitServer(repo, port) as srv:
        live["base"] = srv.base
        shas = git(repo, "rev-list", "--all").stdout.split()
        # aigit 自己的静态路由覆盖范围
        for route in ("/", "/index.html", "/virtuallist.js", "/conflicts-demo.html"):
            code, b = srv.get(route)
            head = b[:60].lstrip().lower()
            kind = "HTML" if head.startswith(("<!doctype", "<html")) else repr(b[:24])
            rec("INFO", "L3", f"aigit 静态路由 GET {route}",
                f"HTTP {code}  {len(b)} B  {kind}")

        st, body = srv.get("/api/status")
        status = json.loads(body)
        live["status"] = status
        rec("INFO", "L3", "/api/status 原始条目(前端就是吃这个)")
        for grp in ("conflicted", "staged", "unstaged"):
            for it in status.get(grp) or []:
                rec("INFO", "L3", f"  {grp}", json.dumps(it, ensure_ascii=False))

        # 报告点名的「原样回显 path」
        echo_hits = []
        for p in Q_PAYLOADS:
            for ep in ("/api/diff?file={}&staged=0",
                       "/api/file?file={}&ref=HEAD"):
                url = ep.format(urllib.parse.quote(p))
                try:
                    code, b = srv.get(url)
                except urllib.error.HTTPError as e:
                    code, b = e.code, e.read().decode("utf-8", "replace")
                j = json.loads(b)
                same = j.get("path") == p
                if same and ("<" in p or ">" in p):
                    echo_hits.append((url.split("?")[0], p))
        rec("WARN" if echo_hits else "PASS", "L3",
            "服务端原样回显 path(server.py:318 / :330)",
            f"{len(echo_hits)} 个 payload 被**逐字节原样**放回 JSON 的 path 字段。"
            f"例:{echo_hits[0][0]} file={echo_hits[0][1]!r}" if echo_hits
            else "未复现原样回显")

        # 越界仍应被拒
        code, b = srv.get("/api/diff?file=" + urllib.parse.quote("../outside.txt"))
        rec("PASS" if code == 400 else "FAIL", "L3", "越界路径被 resolve_in_root 拒绝",
            f"HTTP {code} {b[:120]}")

        # F-04:checkout 的白名单(现在有 check_branch_name 了)。
        #   判据不是「返回码」,是**后果** —— 光看 400 只能证明有校验,证明不了有防护。
        MUST_REJECT = ["--detach", "-f", "--", ".", "-b", "-B", "@{-1}", "HEAD~1",
                       "HEAD^{tree}", "-", ":/", "main^", ""]
        for name in MUST_REJECT:
            code, b = srv.post("/api/checkout", {"name": name})
            rec("PASS" if code == 400 else "FAIL", "L3",
                f"F-04 POST /api/checkout name={name!r} 应被白名单拒绝",
                f"HTTP {code} {b[:110]}")
        # 合法分支名仍应放行
        code, b = srv.post("/api/checkout", {"name": "main"})
        rec("PASS" if code == 200 else "FAIL", "L3",
            "F-04 合法分支名 'main' 仍可 checkout", f"HTTP {code}")

        # 关键:'HEAD' 全是 \w,能过 [\w][\w./-]{0,99} 这条白名单。
        # 直接量后果 —— 发完之后 HEAD 到底还是不是附着的。
        for name in ("HEAD", shas[0], shas[0][:8], shas[0][:7]):
            before = json.loads(srv.get("/api/status")[1]).get("branch")
            code, b = srv.post("/api/checkout", {"name": name})
            after = json.loads(srv.get("/api/status")[1]).get("branch")
            detached = after == "HEAD" and before != "HEAD"
            rec("FAIL" if detached else "PASS", "L3",
                f"F-04 POST /api/checkout name={name!r} 的后果",
                f"HTTP {code};branch {before!r} → {after!r}"
                + ("  **仓库进入游离 HEAD,已脱离 main**" if detached else "  (未游离)"))
            srv.post("/api/checkout", {"name": "main"})

        # F-06:pathspec magic(现在 diff 用了 --literal-pathspecs)
        baseline = {"bb.txt", "n_name.txt", "new.txt"}
        code, b = srv.post("/api/stage", {"files": [":(glob)**/*.txt"]})
        rec("INFO", "L3", "F-06 POST /api/stage files=':(glob)**/*.txt'",
            f"HTTP {code} {b[:160]}")
        staged = [i["path"] for i in json.loads(srv.get("/api/status")[1]).get("staged") or []]
        rec("PASS" if set(staged) <= baseline else "FAIL", "L3",
            "F-06 pathspec magic 是否越权暂存了别的文件",
            f"staged = {staged}(基线 {sorted(baseline)})")
        # 反证:endpoint 本身是好的,上面那个 PASS 不是「因为啥也没发生」
        probe = FS_PAYLOADS[1]
        code, b = srv.post("/api/stage", {"files": [probe]})
        staged2 = [i["path"] for i in json.loads(srv.get("/api/status")[1]).get("staged") or []]
        rec("PASS" if probe in staged2 else "FAIL", "L3",
            "F-06 反证:合法路径仍能暂存(说明上一条不是空跑)",
            f"stage {probe!r} → HTTP {code};staged = {staged2}")
        srv.post("/api/unstage", {"files": [probe]})

        # F-04:分支表里有没有 HEAD
        code, b = srv.get("/api/branches")
        branches = json.loads(b).get("branches", [])
        live["branches"] = branches
        names = [x["name"] for x in branches]
        rec("INFO", "L3", "/api/branches 返回", json.dumps(names, ensure_ascii=False))
        rec("PASS" if "HEAD" not in names else "WARN", "L3",
            "for-each-ref refs/heads 是否吐出 HEAD",
            f"分支表 = {names} —— HEAD {'在里面' if 'HEAD' in names else '不在里面'}")

        st, body = srv.get("/api/log?limit=30")
        live["log"] = json.loads(body).get("log", [])
        hot = [r for r in live["log"] if "<img" in (r.get("subject") or "")]
        rec("INFO", "L3", "/api/log:带 payload 的提交主题被原样返回",
            json.dumps(hot[0] if hot else (live["log"][0] if live["log"] else {}),
                       ensure_ascii=False))

    # 独立静态文件服务(web/ 目录),用来证明这三个文件本身能被取到
    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    sport = free_port()
    sh = http.server.ThreadingHTTPServer(("127.0.0.1", sport), functools.partial(
        Q, directory=str(WEB)))
    th = threading.Thread(target=sh.serve_forever, daemon=True)
    th.start()
    try:
        for name in TARGETS:
            url = f"http://127.0.0.1:{sport}/{name}"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    rec("INFO", "L3", f"静态服务 GET /{name}",
                        f"HTTP {r.status}  {len(r.read())} B")
            except urllib.error.HTTPError as e:
                rec("WARN", "L3", f"静态服务 GET /{name}", f"HTTP {e.code}")
    finally:
        sh.shutdown()
        sh.server_close()


def _node_layer(tmp: Path, live: dict):
    print("\n" + "=" * 72)
    print("L4 真执行 JS:DOM 假身里跑 index.html / conflicts-demo.html 的原脚本")
    print("=" * 72)
    data, err = run_node(tmp, Q_PAYLOADS,
                         live.get("branches", []), live.get("status", {}))
    if err:
        rec("SKIP", "L4", "node 不可用或 harness 失败", err)
        return
    rec("INFO", "L4", "引擎", data.get("node", "?") + "  (" + data.get("_node_path", "") + ")")
    for e in data.get("errors", []):
        rec("FAIL", "L4", "harness 异常", e)

    ei = data.get("esc", {}).get("index", {})
    if ei:
        rec("INFO", "L4", "index.html esc() 实测行为",
            "  ".join(f"{k}={v!r}" for k, v in ei.items()))
        rec("INFO", "L4", "index.html esc() 是否转义单引号",
            f"esc(chr(39)) = {ei.get('sq')!r} —— 不转义。"
            "但 index.html 里 esc() 的产物**只落在双引号属性值和文本节点里**,"
            "双引号自己是转义了的,所以这条不构成漏洞(见下面逐 sink 的判定)")

    ec = data.get("esc", {}).get("conflicts", {})
    if ec:
        rec("INFO", "L4", "conflicts-demo.html esc() 实测行为",
            "  ".join(f"{k}={v!r}" for k, v in ec.items()))
        rec("FAIL" if (ec.get("dq") == '"' or ec.get("sq") == "'") else "PASS", "L4",
            "conflicts-demo.html esc() 是否转义引号",
            f"esc(chr(34))={ec.get('dq')!r}  esc(chr(39))={ec.get('sq')!r} —— "
            "**两种引号都原样穿过**。它和 index.html 的 esc 不是同一个实现,"
            "index.html 还多转义了一个双引号")

    for c in data.get("cases", []):
        if "esc" in c.get("case", ""):
            continue
        judge(c["html"], c["payload"], c["sink"], c["case"])

    # --- 前端是否会拿服务端回显的 d.path 当路径用
    rk = data.get("rowKeyRoundTrip", [])
    rec("INFO", "L4", "行键(路径原样穿过 esc→data-key→split 回来)",
        json.dumps(rk, ensure_ascii=False))

    # --- F-04:顶栏下拉实际生成了哪些 option
    opts = data.get("branchOptions", [])
    rec("PASS" if "HEAD" not in opts else "WARN", "L4",
        "顶栏 #branchSel 实际 option 列表",
        f"{opts} —— UI 上{'能' if 'HEAD' in opts else '不能'}点到 HEAD")

    # --- F-05:幽灵条目有没有进 buildRows
    ghost = data.get("ghostRows", [])
    rec("INFO", "L4", "buildRows 收到的全部文件行(前端一字不落全收)",
        json.dumps(ghost, ensure_ascii=False))
    # porcelain 的 XY 字母表:索引位大写 A M D R C U T,工作区位小写 m d r c u t 或 ?
    VALID = set("AMDRCUT") | set("amdrcut") | set("?")
    bad_status = [g for g in ghost if g["status"] not in VALID]
    real_renames = [g for g in ghost if g["status"] == "R"]
    rec("INFO", "L4", "F-05 拆分",
        f"真 rename(R) {len(real_renames)} 条:"
        f"{[g['path'] for g in real_renames]} / "
        f"状态字母不在 porcelain 表里的 {len(bad_status)} 条:"
        f"{[(g['path'], g['status']) for g in bad_status]}")
    rec("FAIL" if bad_status else "PASS", "L4",
        "F-05 rename 幽灵条目是否被前端渲染成可点行",
        "**会显示**。前端 buildRows() 对 /api/status 的每一条一字不落全收,"
        "不校验 status 字母、不校验路径形态。幽灵条目因此在「已暂存」和「未暂存」"
        "两段各出现一次,而且带可点的「取消暂存」「暂存」按钮 —— 点下去会把幽灵路径"
        "原样 POST 回 /api/unstage、/api/stage" if bad_status
        else "本次 /api/status 里没有非法 status 字母")

    # ══════════════════════════════════════════════════ 破坏性操作的接线
    #
    # 上面那些用例全在验「注入」, 这里验的是另一件事: **前端到底发不发得动
    # 那两个路由**。这是一件已经被实测确认过的产物缺陷 ——
    #   POST /api/discard {"files":[...]}  -> 400 needs_confirmation
    #   POST /api/reset {}                 -> 400(三态没给默认值)
    # 两个按钮一次都没调通过, 界面上表现为「点了没反应 / 一条红色报错」。
    #
    # 为什么不用「真打一次 HTTP」来测: 那是 test_aigit_gate.py 的活(它已经在
    # 测三条拒绝路径了)。这里要钉的是**另一头** —— 前端这个调用方有没有跟着
    # 接线走两步。闸门在服务端测过了, 但闸门挡不住「调用方没跟上」:
    # 一个从不发 confirmed 的前端, 在服务端那 10 条用例里全绿。
    _destructive_wiring(data)


def _destructive_wiring(data):
    """discard / reset 的两步协议接线断言。

    判据全部来自 harness 记下的**真实 fetch 调用序列**, 不是对源码做 grep ——
    grep 只能证明字符串在文件里, 证明不了它真的被发出去过。
    """
    bad = []

    # ---- discard: 先 GET /api/discard/plan, 再 POST /api/discard 带 confirmed
    dc = data.get("discardCalls") or []
    gets = [c for c in dc if c["method"] == "GET"]
    posts = [c for c in dc if c["method"] == "POST"]
    if not any(c["url"].startswith("/api/discard/plan") for c in gets):
        bad.append("doDiscard 没有先 GET /api/discard/plan —— 两步协议的第一步不见了。"
                   "分类就退回前端用 status==='?' 猜, 而服务端 discard() 判"
                   "「已跟踪/未跟踪」用的是 resolve 之后的规范化路径, "
                   "两边判据不同就可能「确认框说恢复、实际 unlink」。")
    if not posts:
        bad.append("doDiscard 整个序列里**一次 POST 都没有** —— 连确认后执行那步都没有。")
    for p in posts:
        if p["url"] != "/api/discard":
            continue
        body = json.loads(p["body"]) if p.get("body") else {}
        # `is True` 而不是「有就行」: _bb 只认真布尔, 字符串 "true" 会被 400。
        # 而把 false 序列化成字符串是极常见的 bug, 在这里等于「没确认」。
        if body.get("confirmed") is not True:
            bad.append(f"POST /api/discard 的 body 里 confirmed={body.get('confirmed')!r}, "
                       "不是 JSON 布尔 true —— 服务端 _bb 会拒成 400。"
                       "必须是字面量 true, 不能是 \"true\"。")
        if not isinstance(body.get("files"), list) or not body["files"]:
            bad.append(f"POST /api/discard 的 files 不是非空数组: {body.get('files')!r}")

    # ---- reset: 预演 GET, mode 显式给, confirmed:true
    #
    # 分 hard / soft 两条流程各记一次调用序列。**必须分**: 只跑 hard 的话
    # 把 body 的 mode 写死成 "hard" 测不出来 —— "hard" 是合法三态之一,
    # `mode in (soft,mixed,hard)` 照样过(实测踩过这一版)。
    # 跑一条非 hard 的, 「写死」立刻现形。
    for tag, want_mode in (("hard", "hard"), ("soft", "soft")):
        rc = data.get("reset%sCalls" % tag.capitalize()) or []
        if not rc:
            bad.append(f"reset {tag} 那条流程一个请求都没发 —— "
                       "harness 没跑到(看 L4 harness 异常那几行)。")
            continue
        if not any(c["url"].startswith("/api/reset/plan") for c in rc
                   if c["method"] == "GET"):
            bad.append(f"reset {tag}: 没有先 GET /api/reset/plan —— 预演那一步不见了。")
        rposts = [c for c in rc if c["method"] == "POST" and c["url"] == "/api/reset"]
        if not rposts:
            bad.append(f"reset {tag}: 预演之后**没有 POST /api/reset** —— "
                       "确认后执行那一步不见了。")
            continue
        for p in rposts:
            body = json.loads(p["body"]) if p.get("body") else {}
            if body.get("confirmed") is not True:
                bad.append(f"reset {tag}: POST /api/reset 的 "
                           f"confirmed={body.get('confirmed')!r}, 不是 JSON 布尔 true"
                           " —— 会被 _bb 拒成 400。")
            # 这一条是本组断言的核心: **必须是用户选的那一档**。
            # 只查「在三态里」不够, 写死成合法值测不出来(见上)。
            if body.get("mode") != want_mode:
                bad.append(f"reset {tag}: 界面这一档选的是 {want_mode}, "
                           f"POST 发的却是 mode={body.get('mode')!r} —— "
                           "前端替用户做了决定。默认 hard 就是 reset --hard "
                           "不经确认。")
        if tag == "hard":
            # hard 会连带 git clean -fd, 未跟踪清单要单独问 /api/discard/plan。
            if not any(c["url"].startswith("/api/discard/plan") for c in rc
                       if c["method"] == "GET"):
                bad.append("reset hard: 没有 GET /api/discard/plan —— "
                           "弹窗里那句「未跟踪文件和目录会被直接删除」"
                           "还是没有数量的空话(服务端 discard_plan 的 docstring "
                           "就是为这个存在的)。")

    # ---- mode 没有默认值: 这条比上面几条更靠前, 因为它是「不许替用户做决定」
    rm = data.get("resetModalHtml") or ""
    for mode in ("soft", "mixed", "hard"):
        if f'value="{mode}"' not in rm:
            bad.append(f"reset 的模式选择框里没有 {mode} 这个选项 —— "
                       "三态必须**全都**列出来让用户自己挑。")
    if "checked" in rm:
        # openModal 之后 checked 只能是某个 radio 上的 —— 也就是前端替用户选了。
        bad.append("reset 的模式选择框里有 checked —— 前端给 reset **预选**了模式。"
                   "默认 hard 就是 reset --hard 不经确认。")
    if data.get("resetModeDefault") != "":
        bad.append(f"resetModeChecked() 在没人点任何 radio 时读出 "
                   f"{data.get('resetModeDefault')!r}, 不是空串 —— "
                   "说明前端给 reset 预选了模式。默认 hard 就是 reset --hard "
                   "不经确认。")

    for m in bad:
        rec("FAIL", "L4", "破坏性操作两步协议接线", m)
    if not bad:
        rec("PASS", "L4", "破坏性操作两步协议接线",
            "discard: GET /api/discard/plan -> POST /api/discard{confirmed:true}; "
            "reset: GET /api/reset/plan?mode=X -> POST /api/reset{mode:X,confirmed:true}"
            "(hard 档额外 GET /api/discard/plan 取未跟踪清单); "
            "resetModeChecked() 初值空串 —— 三个 radio 一个都没 checked")


if __name__ == "__main__":
    sys.exit(main())