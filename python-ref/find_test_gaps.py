#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""find_test_gaps.py —— 找「零提及」的响应面：某个真实返回的键，
在所有测试套件的 AST 里**一次都没出现过**。

============================================================================
为什么要有这个工具（2026-10-06 立）
============================================================================
在这之前，我「找测试缺口」用的是 grep 关键词。那套做法**三次被推翻**：

  1. 说「9 个端点没测」——实际 8 个被测过（字符串在注释里也命中）。
  2. 说 `stash/pop` 没测——底层被测过。
  3. 拿 `_run_full` 去 doc 里找，可那段在 README 里（尺子量错了对象）。

根因是三条，本工具逐条堵：
  * **只认 AST，不碰原文** —— 注释里的同名字不算。
  * **面必须来自行为**（真跑 server 看它返回什么），
    不从 `server.py` 的源码/文档推导 —— 那是「拿被检查者的说明书
    去检查被检查者」。
  * **尺子先证明自己会响**（`--selftest`）：拿已知会命中的哨兵喂它，
    证明它数得出来；再拿一个确定不存在的哨兵，证明它不会乱认。

============================================================================
这把尺子**只测一个方向**（刻意，不许反推）
============================================================================
    键在任何套件里都没出现   =>  一定没被测      （下界，可信）
    键出现了                 =/=>  被测了        （出现 != 断言）

所以输出的标题是「零提及」，**不是**「未覆盖」。本工具绝不说
「出现了就等于覆盖」。

它**不负责**发现「只被一个套件覆盖」这类脆弱面 —— 那类正是
2026-10-06 `/api/commit` 的 `output` 回归（当时 `smoke.py:513` 确实
断言了它，所以它不属于「零提及」）。那种面要靠**行为化的变异测试**，
见 `mutation_runner.py`（把键删掉看有没有套件变红）。

2026-10-06 补：面从「只有 GET」扩到「GET + 安全的 POST」。原先不探 POST
的理由是「契约测试覆盖了」，但契约测试只钉**必需键**，不保证返回里**每个**
键都有人管 —— 实测 `staged` / `unstaged` / `conflicted` 三个键原先零提及。
探 POST 的纪律：**先探 GET，再跑前置步骤，最后探 POST**（POST 会改仓，
放后面才不会污染 GET 面）。

============================================================================
用法
============================================================================
    python find_test_gaps.py                # 自证 + 真实扫描
    python find_test_gaps.py --selftest     # 只跑自证（不碰网络，快）
"""

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# --------------------------------------------------------------------------
# 面：**在沙盒里安全可探的端点**（写死的字面量，运行时不读 server.py）
# --------------------------------------------------------------------------
# GET 全是无副作用的读。POST 只挑**在沙盒里跑不坏、且能造出真响应**的：
#   stage / commit / branch / merge(无冲突 -> 400, 拿 error 键)。
#
# ⚠ 顺序有意义：**先探 GET，再跑 PRE_STEPS，最后探 POST** ——
#   POST 会改仓（commit 产生提交、branch 产生分支），放在后面才不会污染
#   GET 面。merge 放最后，它是唯一会返回 400 的。
#
# 2026-10-06 才加 POST：原先只探 GET，理由是「POST 由契约测试覆盖」。
# 但契约测试只钉**必需键**，不保证「返回里每个键都有人管」—— 实测
# `staged` / `unstaged` / `conflicted` 这三个键原先**零提及**。
PROBE_PATHS = (
    "/api/health",
    "/api/status",
    "/api/branches",
    "/api/log?limit=3",
    "/api/diff?file=probe.txt",
    "/api/remote/list",
    "/api/remote/capability",
    "/api/remote/heads",
    "/api/remote/ahead-behind",
    "/api/history/stashes",
    "/api/history/tags",
    "/api/history/reflog",
    "/api/conflict/list",
    "/api/conflict/state",
    # -- 带参 GET（2026-10-20 补）--------------------------------------
    # `--sandbox` 只建 `README.md`（实测 server.py:2184），所以上面那条
    # `/api/diff?file=probe.txt` 探的其实是**不存在**的文件 —— 吃到的是 400
    # 错误体。下面这些一律指向**真实存在**的 README.md，才测得到正常形状。
    "/api/file?file=README.md",
    "/api/diff?file=README.md",
    "/api/history/blame?file=README.md",
    "/api/history/file?file=README.md",
    "/api/discard/plan?files=README.md",
    "/api/reset/plan?mode=hard",
    "/api/history/stash?index=0",
    # -- 只读 plan 预演（2026-10-21 补）--------------------------------
    # 这些是两步协议里「这一步会做什么」的**只读**预演, 不改任何东西。
    # 先量后设计: probe_plans.py 实测这 5 条都返回 200 且键很丰富
    # (amend 13 键 / push 13 / revert 12 / branch-rename 10 / fetch 10)。
    "/api/history/plan/amend",
    "/api/history/plan/revert?sha=HEAD",
    "/api/history/plan/branch-rename?old=main&new=x",
    "/api/remote/plan/fetch?remote=origin",
    "/api/remote/plan/push?remote=origin",
)

# POST 面：(路径, 请求体)。请求体是写死的字面量，不从 server.py 推导。
PROBE_POSTS = (
    ("/api/stage", {"files": ["probe.txt"]}),
    ("/api/commit", {"message": "gap probe"}),
    ("/api/branch", {"name": "gap-probe"}),
    ("/api/merge", {"branch": "gap-probe"}),
)

# 前置步骤：POST 之前先跑（**不记录**），造出后续端点需要的前置状态。
# commit 需要「有东西可提」，所以先 stage 一次 probe.txt。
PRE_STEPS = (
    ("/api/stage", {"files": ["probe.txt"]}),
)

# 非空守卫：面太薄就是静默跳过，比判错更险
MIN_PROBED_ENDPOINTS = 6
MIN_PROBED_KEYS = 12

# 判据里不许出现会漂移的绝对值 —— 所以这里不写死「应有 N 个键」，
# 只写「至少这么多」。真实数字当场打印。

SUITE_GLOBS = ("test_", "smoke.py", "selftest.py")


# --------------------------------------------------------------------------
# 1) 扫测试套件的 AST，收集「所有字符串常量」
# --------------------------------------------------------------------------


def suite_files(root=HERE):
    """要纳入的套件文件。**不含** mutation_runner.py 的探针 ——
    探针不在常规套件里跑，算了会高估覆盖。"""
    out = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        if name.startswith("test_") or name in ("smoke.py", "selftest.py"):
            out.append(os.path.join(root, name))
    return out


def mentions_in_source(src):
    """返回源码里出现的所有字符串常量集合。

    **AST-only**：注释、docstring 之外的原文一概不看。
    docstring 是 `Expr(Constant(str))`，也算字符串常量 —— 刻意保留，
    因为 docstring 里写「键 output」也可能误导；但它出现在**哪个文件**
    会被如实列出来，不隐藏。
    """
    tree = ast.parse(src)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def scan_suites(files=None):
    """返回 {文件: 该文件出现的字符串常量集合}。解析失败直接抛 ——
    静默跳过一条比判错更险。"""
    files = suite_files() if files is None else files
    per_file = {}
    for path in files:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        per_file[os.path.basename(path)] = mentions_in_source(src)
    return per_file


# --------------------------------------------------------------------------
# 2) 真跑一遍 server，记录每个端点**实际**返回的键
# --------------------------------------------------------------------------


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def call(port, path, body=None, timeout=30):
    """打一个端点。body 为 None 就是 GET，否则 POST 一个 JSON 请求体。

    返回 (状态码, 解析后的对象或 None)。

    **HTTPError 的 body 也要读** —— 4xx 的响应体同样是响应面：
    `/api/merge` 无冲突时返回 400 + `{"ok": false, "error": ...}`，
    那个 `error` 键正是要探的东西。不读 body 就会把它当成「无键」。
    """
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, None
    except Exception as exc:  # noqa: BLE001 - 探针不该因为一个端点崩掉整轮
        return -1, {"__error__": repr(exc)}


def surface_entry(method, path, st, obj):
    """把一次探测结果记成 (面键, 键列表或 None)。**纯函数**，可单测。

    面键带方法前缀（`GET /api/health` / `POST /api/stage`），
    否则 GET 与 POST 撞同名路径时会互相覆盖。
    """
    keys = sorted(obj.keys()) if isinstance(obj, dict) else None
    return "%s %s" % (method, path), keys


def wait_health(port, proc, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, "server 进程已退出 rc=%s" % proc.returncode
        st, obj = call(port, "/api/health", timeout=3)
        if st == 200 and isinstance(obj, dict) and obj.get("ok"):
            return True, "ok"
        time.sleep(0.3)
    return False, "等待 health 超时 %ds" % timeout


def probe_surface():
    """返回 (面, 说明)。面 = {端点: (状态码, 键列表或 None)}。

    `--sandbox` 让 server 自建一个演练仓，**不碰任何真实仓库**。
    探针的根目录/日志都落在系统临时目录，**不在仓库里留任何文件**。
    """
    tmp = tempfile.mkdtemp(prefix="aigit_gap_probe_")
    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    errpath = os.path.join(tmp, "server.err")
    errlog = open(errpath, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", SERVER, "--root",
         os.path.join(tmp, "root"), "--port", str(port), "--sandbox"],
        stdout=subprocess.DEVNULL, stderr=errlog, env=env,
    )
    surface = {}
    try:
        ok, info = wait_health(port, proc)
        if not ok:
            errlog.flush()
            tail = ""
            try:
                with open(errpath, encoding="utf-8", errors="replace") as fh:
                    tail = fh.read()[-500:]
            except OSError:
                pass
            raise RuntimeError("起服务失败: %s\n%s" % (info, tail))
        # 1) GET 面：无副作用的读，**先探**，免得被 POST 改过的仓污染
        for path in PROBE_PATHS:
            st, obj = call(port, path)
            k, keys = surface_entry("GET", path, st, obj)
            surface[k] = (st, keys)
        # 2) 前置步骤：**不记录**，只造后续端点需要的前置状态
        for path, body in PRE_STEPS:
            call(port, path, body)
        # 3) POST 面：会改仓，放最后
        for path, body in PROBE_POSTS:
            st, obj = call(port, path, body)
            k, keys = surface_entry("POST", path, st, obj)
            surface[k] = (st, keys)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        errlog.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return surface


# --------------------------------------------------------------------------
# 3) 分类（纯函数 —— 阳性对照才能单独跑）
# --------------------------------------------------------------------------


def classify(key, per_file):
    """返回 (判定, 命中文件列表)。

    判定只有两档，刻意不做「已覆盖」这种越界结论：
      "zero"      —— 零提及（任何套件 AST 里都没有这个字符串常量）
      "mentioned" —— 出现了（**不等于**被测）
    """
    hits = sorted(f for f, keys in per_file.items() if key in keys)
    return ("mentioned" if hits else "zero"), hits


def crosscheck(key, per_file):
    """对单个键做**两把不同来源的尺子**，不一致就抛。

    尺子 A: AST 扫（collect_mentions 那条路）
    尺子 B: 逐文件 `key in mentions` 的集合判定（本函数里独立重算一遍）
    两者同源就没意义 —— 所以 B 走的是**文件内容里有没有该子串**的
    粗判，只用来抓「AST 扫漏了」。A 说没提及而 B 说内容里有 =>
    说明这个键在原文里出现但不在字符串常量里（例如拼在 f-string 里），
    属于需要人看的灰区，**不当成 zero 报出去**。
    """
    hits = sorted(f for f, keys in per_file.items() if key in keys)
    if hits:
        return "mentioned", hits
    # A 说没有：用原文粗判兜一层
    loose = []
    for path in suite_files():
        with open(path, encoding="utf-8") as fh:
            if key in fh.read():
                loose.append(os.path.basename(path))
    if loose:
        return "gray", sorted(loose)
    return "zero", []


# --------------------------------------------------------------------------
# 4) 自证：尺子先证明自己会响
# --------------------------------------------------------------------------

SENTINEL_YES = "ZZZ_gap_sentinel_present_ZZZ"
SENTINEL_NO = "ZZZ_gap_sentinel_absent_ZZZ"


def selftest(report):
    """每条都要有**阳性**与**阴性**对照。0 项检查恒真，必须防。"""
    # --- 4a. 扫描器：合成本源码，证明它能认出字符串常量 ---
    fake_src = (
        "def t():\n"
        "    d = {}\n"
        "    assert %r in d\n"
        "    # 注释里的 %s 不该算（注释不是 AST）\n"
        "    s = 'x'  # %s\n"
        % (SENTINEL_YES, SENTINEL_NO, SENTINEL_NO)
    )
    got = mentions_in_source(fake_src)
    report("扫描器: 认出合成源码里的哨兵(阳性)", SENTINEL_YES in got,
           "got=%s" % sorted(got))
    report("扫描器: 注释里的哨兵**不**算(阴性)", SENTINEL_NO not in got,
           "注释/尾注释里的串若被算进来, 就退回 grep 了")

    # --- 4b. 分类器：拿一份**已知**的 per_file 喂它 ---
    fake = {"A.py": {"alpha", "beta"}, "B.py": {"beta", "gamma"}}
    v, hits = classify("beta", fake)
    report("分类器: 出现过的键判 mentioned", v == "mentioned" and hits == ["A.py", "B.py"],
           "%s %s" % (v, hits))
    v, hits = classify("delta", fake)
    report("分类器: 没出现的键判 zero", v == "zero" and hits == [],
           "%s %s" % (v, hits))

    # --- 4c. 非空守卫：per_file 为空时必须能被识别（不许恒真）---
    report("分类器: 空 per_file 下一切都 zero(说明必须防 0 项恒真)",
           classify("alpha", {})[0] == "zero",
           "空集合里当然什么都找不到 —— 所以真实扫描前要断言面非空")

    # --- 4d. 真实仓库层面的对照：两个已知事实 ---
    per_file = scan_suites()
    total = sum(len(v) for v in per_file.values())
    report("真实套件: 扫到的字符串常量总数 > 0(防 0 项恒真)", total > 0,
           "%d 个常量, 来自 %d 个文件" % (total, len(per_file)))
    # hasStaged 是 server status 响应的真实键, smoke.py 断言过它
    v, hits = classify("hasStaged", per_file)
    report("真实套件: 已知出现过的键 'hasStaged' 判 mentioned(阳性)",
           v == "mentioned" and bool(hits), "%s %s" % (v, hits))
    # 一个几乎不可能存在的串
    v, hits = classify(SENTINEL_NO, per_file)
    report("真实套件: 确定不存在的哨兵判 zero(阴性)", v == "zero", "%s %s" % (v, hits))

    # --- 4e. 记录器：纯函数，阳性/阴性各一条 ---
    k, keys = surface_entry("POST", "/api/stage", 200, {"ok": True, "branch": "main"})
    report("记录器: dict 记成键列表且带方法前缀(阳性)",
           k == "POST /api/stage" and keys == ["branch", "ok"],
           "k=%r keys=%r" % (k, keys))
    k2, keys2 = surface_entry("GET", "/api/nope", 404, None)
    report("记录器: 非 dict 记成 None(阴性)", k2 == "GET /api/nope" and keys2 is None,
           "k=%r keys=%r" % (k2, keys2))

    # --- 4f. call 的 HTTPError body 也要读（真 HTTP 服务做阳性/阴性对照）---
    # 阳性：一个真的 400 + JSON，call 必须把 body 读出来（否则 error 键丢了）。
    # 阴性：死端口必须返回 -1，**不许静默成功**。
    import http.server
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, obj):
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._send(200, {"ok": True})

        def do_POST(self):
            self._send(400, {"ok": False, "error": "boom"})

        def log_message(self, *a):  # 静音
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = srv.server_address[1]
        st, obj = call(p, "/api/merge", {"branch": "x"})
        report("call: 400 的 body 也要读出来(阳性)",
               st == 400 and isinstance(obj, dict) and obj.get("error") == "boom",
               "st=%s obj=%r" % (st, obj))
        st2, obj2 = call(p, "/api/health")
        report("call: GET 200 正常读(阳性)",
               st2 == 200 and isinstance(obj2, dict) and obj2.get("ok") is True,
               "st=%s obj=%r" % (st2, obj2))
        dead = free_port()
        st3, obj3 = call(dead, "/api/health", timeout=2)
        report("call: 死端口必须返回 -1(阴性, 防静默成功)",
               st3 == -1 and isinstance(obj3, dict),
               "st=%s obj=%r" % (st3, obj3))
    finally:
        srv.shutdown()
        srv.server_close()
    return per_file


# --------------------------------------------------------------------------
# 5) 主流程
# --------------------------------------------------------------------------


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only_selftest = "--selftest" in argv

    print("=" * 74)
    print("  测试缺口查找器 —— 零提及响应面（下界，不反推）")
    print("=" * 74)

    checks = []

    def report(name, ok, detail=""):
        checks.append(bool(ok))
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        if detail:
            print("        %s" % detail)

    print("\n  ---- 自证组（跑真实面之前，先证明尺子会响）----")
    per_file = selftest(report)
    p, f = sum(checks), len(checks) - sum(checks)
    print("\n  自证: %d 条  PASS %d  FAIL %d" % (len(checks), p, f))
    if only_selftest:
        return 1 if f else 0

    print("\n  ---- 真实面组（--sandbox 自建演练仓，不碰任何真实仓库）----")
    surface = probe_surface()
    live = [(ep, st, keys) for ep, (st, keys) in sorted(surface.items())]
    with_keys = [(ep, st, keys) for (ep, st, keys) in live if keys]
    print("  探了 %d 个端点, 其中 %d 个返回 JSON 对象并带键"
          % (len(live), len(with_keys)))

    # 非空守卫
    if len(with_keys) < MIN_PROBED_ENDPOINTS:
        print("  !! 面太薄: 只有 %d 个端点返回带键对象, 少于守卫值 %d —— 拒出结论"
              % (len(with_keys), MIN_PROBED_ENDPOINTS))
        return 4
    all_keys = set()
    for _ep, _st, keys in with_keys:
        all_keys.update(keys)
    if len(all_keys) < MIN_PROBED_KEYS:
        print("  !! 面太薄: 只有 %d 个不同的键, 少于守卫值 %d —— 拒出结论"
              % (len(all_keys), MIN_PROBED_KEYS))
        return 4

    print("\n  端点 -> 实际返回的键（行为事实, 非源码推导）:")
    for ep, st, keys in live:
        mark = "  " if keys else "!!"
        print("    %s %-40s HTTP %-4s %s"
              % (mark, ep, st, keys if keys else "(无 JSON 对象)"))

    print("\n  每个键在套件 AST 里的出现情况:")
    zero, mentioned, gray = [], [], []
    for key in sorted(all_keys):
        verdict, hits = crosscheck(key, per_file)
        if verdict == "zero":
            zero.append(key)
        elif verdict == "gray":
            gray.append((key, hits))
        else:
            mentioned.append((key, hits))
        tag = {"zero": "零提及", "gray": "灰区  ", "mentioned": "有提及"}[verdict]
        print("    %s %-22s %s" % (tag, key, (hits if hits else "")))

    print("\n  ---- 结论（只说这一个方向）----")
    print("  参与核对的键: %d 个（面守卫 >= %d）" % (len(all_keys), MIN_PROBED_KEYS))
    print("  零提及 : %d 个 —— **一定没被测**（下界，可信）" % len(zero))
    if zero:
        for key in zero:
            print("      - %s" % key)
    print("  灰区   : %d 个 —— 原文里有、AST 字符串常量里没有，需人看，**不算零提及**"
          % len(gray))
    for key, hits in gray:
        print("      - %s  (%s)" % (key, ", ".join(hits)))
    print("  有提及 : %d 个 —— **不等于被测**，本工具不据此下任何结论" % len(mentioned))
    print("\n  注: 「只被一个套件覆盖」这类脆弱面不在这里查 —— 那是行为化变异测试的活。")

    print("\n  共 %d 条自证  PASS %d  FAIL %d" % (len(checks), p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
