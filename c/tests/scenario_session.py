# -*- coding: utf-8 -*-
"""端到端演练：把 aigit 当「带上下文的编程工具」真用一遍（不是 oracle 差分）。

它验证三件产品能力：
  1. 上下文管理：status / branches / log / history/file / blame / file?ref=；
  2. 每次工具改文件后能拿到 diff 块（含 @@ hunk），暂存前后一致；
  3. 会话节点出问题能回退现场：revert（历史安全）、discard（丢弃未提交）、
     reflog restore（回到某个节点，hard），以及 stash 往返。

判据是**真的跑项目自己的 unittest**：N3 引入回归后必须变红，回退后必须复绿。

项目：calc（stdlib Python，真 unittest）。
会话节点：
  N0  chore: 初始骨架
  N1  feat: 加 div（含 0 除保护）+ 测试
  N2  feat(cli): div 子命令 + --json + README
  N3  refactor: 「简化」div  -> 引入回归（// 而不是 /），真测试变红
  N4  revert N3 -> 测试恢复绿

三种回退：revert（历史安全）、discard（丢未提交改动）、
         reflog restore（回到某个节点现场，hard）+ 再退回原节点。
每个「工具改动」都从 /api/diff 取 diff 块（含 @@ hunk）并断言。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(
    os.path.dirname(HERE), "build", "aigit.exe")
BASE = tempfile.mkdtemp(prefix="aigit_scenario_")
FAILS = []
OKS = []

CORE_V1 = '''"""计算核心。"""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b
'''

CORE_V2 = CORE_V1 + '''

def div(a, b):
    if b == 0:
        raise ZeroDivisionError("除数不能为 0")
    return a / b
'''

CLI_V1 = '''import argparse

from . import core


def main(argv=None):
    p = argparse.ArgumentParser(prog="calc")
    p.add_argument("op", choices=["add", "sub", "mul"])
    p.add_argument("a", type=float)
    p.add_argument("b", type=float)
    args = p.parse_args(argv)
    print(getattr(core, args.op)(args.a, args.b))


if __name__ == "__main__":
    main()
'''

CLI_V2 = '''import argparse
import json

from . import core


def main(argv=None):
    p = argparse.ArgumentParser(prog="calc")
    p.add_argument("op", choices=["add", "sub", "mul", "div"])
    p.add_argument("a", type=float)
    p.add_argument("b", type=float)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    value = getattr(core, args.op)(args.a, args.b)
    if args.json:
        print(json.dumps({"op": args.op, "value": value}))
    else:
        print(value)


if __name__ == "__main__":
    main()
'''

TEST_V1 = '''import unittest

from calc import core


class TestCore(unittest.TestCase):
    def test_add(self):
        self.assertEqual(core.add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(core.sub(2, 3), -1)

    def test_mul(self):
        self.assertEqual(core.mul(-2, 3), -6)


if __name__ == "__main__":
    unittest.main()
'''

TEST_V2 = '''import unittest

from calc import core


class TestCore(unittest.TestCase):
    def test_add(self):
        self.assertEqual(core.add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(core.sub(2, 3), -1)

    def test_mul(self):
        self.assertEqual(core.mul(-2, 3), -6)

    def test_div(self):
        self.assertEqual(core.div(7, 2), 3.5)

    def test_div_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            core.div(1, 0)


if __name__ == "__main__":
    unittest.main()
'''


# ---------------------------------------------------------------- 基础工具
def log(msg):
    print(msg, flush=True)


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
        log("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILS.append(name)
        log("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def api(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read().decode("utf-8", "replace")
    c.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def w(repo, rel, text):
    p = os.path.join(repo, rel)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def rd(repo, rel):
    with open(os.path.join(repo, rel), encoding="utf-8") as f:
        return f.read()


def show_diff(title, js):
    d = js.get("diff", "") if isinstance(js, dict) else str(js)
    lines = d.splitlines()
    log("    ── diff 块：%s  (%d 行)" % (title, len(lines)))
    for ln in lines[:16]:
        log("    │ " + ln)
    if len(lines) > 16:
        log("    │ ... 省略 %d 行" % (len(lines) - 16))
    return d


def run_tests(repo):
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest",
                        "discover", "-s", "tests", "-t", ".", "-v"],
                       cwd=repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-3:]
    return r.returncode == 0, tail


def latest_short(port):
    st, js = api(port, "GET", "/api/log?limit=1")
    return js["log"][0]["sha"]


def find_reflog_index(rows, sha, prefer="commit"):
    cands = [r for r in rows if r["sha"] == sha]
    for r in cands:
        if prefer and r["action"].startswith(prefer):
            return r["index"]
    return cands[0]["index"] if cands else None


def head_full(repo):
    """真 sha。`/api/log` 给的是缩写的 8 位（与 Python 一致，展示用）；
    reflog / blame / file?ref 用的都是 40 位，比较时别混。"""
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                       capture_output=True, text=True)
    return r.stdout.strip()


def commit(port, repo, msg):
    st, js = api(port, "POST", "/api/commit", {"message": msg})
    if st != 200:
        log("    commit 失败: %s %s" % (st, js))
        return None
    return head_full(repo)


# ---------------------------------------------------------------- 阶段
def main():
    repo = os.path.join(BASE, "calc-demo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "agent", cwd=repo)
    sh("git", "config", "user.email", "agent@example.com", cwd=repo)

    log("\n=== 阶段 0：造项目骨架（N0）")
    w(repo, "calc/__init__.py", '"""最小计算器。"""\n')
    w(repo, "calc/core.py", CORE_V1)
    w(repo, "calc/cli.py", CLI_V1)
    w(repo, "tests/__init__.py", "")
    w(repo, "tests/test_core.py", TEST_V1)
    w(repo, ".gitignore", "__pycache__/\n*.pyc\n")
    w(repo, "README.md", "# calc\n\n最小计算器。\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "chore: 初始骨架", cwd=repo)
    pre_ok, tail = run_tests(repo)
    log("    骨架测试：" + " / ".join(tail[-2:]))
    ok("N0 骨架测试 3 项全绿", pre_ok)

    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health")
            break
        except Exception:
            time.sleep(0.1)

    try:
        log("\n=== 阶段 1：先把上下文拉出来（health / status / branches / log）")
        st, js = api(port, "GET", "/api/health")
        ok("health 200 且 isRepo", st == 200 and js["isRepo"] is True)
        st, js = api(port, "GET", "/api/status")
        ok("干净工作区", st == 200 and js["staged"] == [] and js["unstaged"] == [],
           "branch=%s" % js.get("branch"))
        st, js = api(port, "GET", "/api/branches")
        ok("branches 有 main",
           st == 200 and any(b["name"] == "main" for b in js["branches"]))
        st, js = api(port, "GET", "/api/log?limit=5")
        log("    log: " + " | ".join("%s %s" % (c["sha"][:8], c["subject"])
                                    for c in js["log"]))
        ok("log 有 N0", len(js["log"]) == 1 and
           js["log"][0]["subject"] == "chore: 初始骨架")

        # ---------------- N1 ----------------
        log("\n=== 节点 N1：加 div + 0 除保护（diff 块 -> stage -> staged diff）")
        w(repo, "calc/core.py", CORE_V2)
        st, js = api(port, "GET", "/api/diff?file=calc/core.py&staged=0")
        d1 = show_diff("calc/core.py 未暂存", js)
        ok("diff 有 hunk 头 @@", "@@" in d1)
        ok("diff 标出新函数 div", "+def div(a, b):" in d1)

        api(port, "POST", "/api/stage", {"files": ["calc/core.py"]})
        st, js = api(port, "GET", "/api/diff?file=calc/core.py&staged=1")
        d1s = show_diff("calc/core.py 已暂存", js)
        ok("暂存后 diff 块内容不变", d1s.strip() == d1.strip())

        w(repo, "tests/test_core.py", TEST_V2)
        st, js = api(port, "GET", "/api/diff?file=tests/test_core.py&staged=0")
        dt = show_diff("tests/test_core.py 未暂存", js)
        ok("测试文件 diff 标出新增用例", "+    def test_div(self):" in dt)
        api(port, "POST", "/api/stage", {"files": ["tests/test_core.py"]})
        st, js = api(port, "GET", "/api/status")
        ok("两个文件都在 staged", len(js["staged"]) == 2,
           str([x["path"] for x in js["staged"]]))
        sha_n1 = commit(port, repo, "feat: 加 div 与 0 除保护")
        ok("N1 提交成功", bool(sha_n1), (sha_n1 or "")[:8])
        t_ok, tail = run_tests(repo)
        log("    真测试：" + " / ".join(tail[-2:]))
        ok("N1 后测试全绿（5 项）", t_ok)

        # ---------------- N2 ----------------
        log("\n=== 节点 N2：CLI 加 div 与 --json，README 同步（多文件 diff）")
        w(repo, "calc/cli.py", CLI_V2)
        w(repo, "README.md",
          "# calc\n\n最小计算器，支持 add / sub / mul / div。\n\n"
          "```\npython -m calc.cli div 7 2 --json\n```\n")
        st, js = api(port, "GET", "/api/status")
        ok("status 报出 2 个改动", len(js["unstaged"]) == 2,
           str([x["path"] for x in js["unstaged"]]))
        for f in ("calc/cli.py", "README.md"):
            st, js = api(port, "GET", "/api/diff?file=" + urllib.parse.quote(f) + "&staged=0")
            dd = show_diff(f, js)
            ok("%s 的 diff 有 hunk" % f, "@@" in dd)
        api(port, "POST", "/api/stage", {"files": ["calc/cli.py", "README.md"]})
        sha_n2 = commit(port, repo, "feat(cli): div 子命令 + --json")
        ok("N2 提交成功", bool(sha_n2), (sha_n2 or "")[:8])
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "calc.cli",
                            "div", "7", "2", "--json"],
                           cwd=repo, capture_output=True, text=True)
        ok("CLI 真跑通", r.returncode == 0 and '"value": 3.5' in r.stdout,
           (r.stdout or r.stderr).strip())
        t_ok, tail = run_tests(repo)
        log("    真测试：" + " / ".join(tail[-2:]))
        ok("N2 后测试全绿（5 项）", t_ok)

        # ---------------- N3：回归 ----------------
        log("\n=== 节点 N3：一次「简化」引入回归（div 改成整除）")
        w(repo, "calc/core.py", rd(repo, "calc/core.py").replace(
            "    return a / b", "    return a // b"))
        st, js = api(port, "GET", "/api/diff?file=calc/core.py&staged=0")
        d3 = show_diff("calc/core.py（回归）", js)
        ok("回归 diff 是单行改动",
           [l for l in d3.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
           == ["-    return a / b", "+    return a // b"])
        api(port, "POST", "/api/stage", {"files": ["calc/core.py"]})
        sha_n3 = commit(port, repo, "refactor: 简化 div 的实现")
        ok("N3 提交成功（坏节点）", bool(sha_n3), (sha_n3 or "")[:8])
        t_ok, tail = run_tests(repo)
        log("    真测试：" + " / ".join(tail[-2:]))
        ok("N3 后测试变红（回归被真测试抓到）", not t_ok)

        # ---------------- 定位 ----------------
        log("\n=== 阶段 4：定位「是哪个节点弄坏的」")
        st, js = api(port, "GET", "/api/history/file?file=calc/core.py")
        log("    calc/core.py 的提交历史: " +
            " | ".join("%s %s" % (c["sha"][:8], c["subject"]) for c in js["commits"]))
        ok("历史里 3 次改动 core.py", len(js["commits"]) == 3)
        st, js = api(port, "GET", "/api/history/blame?file=calc/core.py")
        for l in js["lines"]:
            if "//" in l["content"]:
                log("    blame line %s: %r <- %s" % (l["line"], l["content"], l["short"]))
        bad = [l for l in js["lines"] if l["content"].strip() == "return a // b"]
        ok("blame 指向坏节点 N3",
           len(bad) == 1 and bad[0]["sha"] == sha_n3,
           "line %s <- %s" % (bad[0]["line"] if bad else "?",
                              bad[0]["sha"][:8] if bad else "?"))
        st, js = api(port, "GET", "/api/file?file=calc/core.py&ref=" + sha_n1)
        # 语义：old = ref 处的内容，new = 当前工作区
        ok("能取回 N1 的旧版本（上下文复原）",
           "    return a / b" in (js.get("old") or "")
           and "// b" not in (js.get("old") or ""))
        ok("同一响应里给出当前工作区版本做对照",
           "// b" in (js.get("new") or ""))

        # ---------------- 回退 A：revert ----------------
        log("\n=== 回退 A：revert 坏节点（不动历史）")
        st, plan = api(port, "GET", "/api/history/plan/revert?sha=" + sha_n3)
        ok("revert 预演 200", st == 200, "op=%s" % plan.get("op"))
        st, js = api(port, "POST", "/api/history/revert",
                     {"sha": sha_n3, "no_edit": True})
        ok("revert 执行 200", st == 200, str(js)[:70])
        sha_n4 = head_full(repo)
        st, js = api(port, "GET", "/api/log?limit=3")
        log("    log 顶部: " + " | ".join("%s %s" % (c["sha"][:8], c["subject"])
                                          for c in js["log"]))
        ok("顶层是新节点 N4（历史只追加）",
           js["log"][0]["sha"] == sha_n4[:8] and js["log"][1]["sha"] == sha_n3[:8])
        t_ok, tail = run_tests(repo)
        log("    真测试：" + " / ".join(tail[-2:]))
        ok("revert 后测试恢复绿", t_ok)
        ok("core.py 恢复成真除法", "    return a / b" in rd(repo, "calc/core.py"))

        # ---------------- 回退 B：discard ----------------
        log("\n=== 回退 B：未提交的坏改动 -> discard/plan -> discard")
        good = rd(repo, "calc/core.py")
        w(repo, "calc/core.py", good.replace("    return a / b", "    return 0  # 手滑"))
        st, js = api(port, "GET", "/api/status")
        ok("工作区有 1 个未提交改动", len(js["unstaged"]) == 1)
        st, plan = api(port, "GET", "/api/discard/plan?files=calc/core.py")
        files = [e.get("path") for e in (plan.get("entries") or [])]
        ok("discard 预演列出要丢的文件",
           st == 200 and any(str(f).endswith("core.py") for f in files), str(files)[:90])
        st, js = api(port, "POST", "/api/discard",
                     {"files": ["calc/core.py"], "confirmed": True})
        ok("discard 200", st == 200, str(js)[:60])
        ok("discard 后文件回到 N4 现场", rd(repo, "calc/core.py") == good)
        st, js = api(port, "GET", "/api/status")
        ok("discard 后工作区干净", js["unstaged"] == [] and js["staged"] == [])
        t_ok, _ = run_tests(repo)
        ok("discard 后测试仍绿", t_ok)

        # ---------------- stash ----------------
        log("\n=== 阶段 7：stash 往返（临时离开现场再回来）")
        wip = good.replace("def div(a, b):",
                           "def div(a, b):\n    # WIP: 想改成 Decimal")
        w(repo, "calc/core.py", wip)
        st, js = api(port, "POST", "/api/history/stash",
                     {"message": "wip: div 用 Decimal"})
        ok("stash 200", st == 200, str(js)[:60])
        st, js = api(port, "GET", "/api/status")
        ok("stash 后工作区干净", js["staged"] == [] and js["unstaged"] == [])
        st, js = api(port, "GET", "/api/history/stashes")
        ok("stash 列表里有 1 条", st == 200 and len(js.get("stashes") or []) == 1)
        st, js = api(port, "POST", "/api/history/stash/pop",
                     {"index": 0, "confirmed": True})
        ok("stash pop 200", st == 200, str(js)[:70])
        ok("pop 后 WIP 内容回来了", rd(repo, "calc/core.py") == wip)
        api(port, "POST", "/api/discard", {"files": ["calc/core.py"], "confirmed": True})

        # ---------------- 回退 C：reflog restore ----------------
        log("\n=== 回退 C：回到 N2 的现场（reflog restore hard），再退回 N4")
        st, js = api(port, "GET", "/api/file?file=calc/core.py&ref=" + sha_n2)
        snap_n2 = js["new"]
        st, js = api(port, "GET", "/api/history/reflog?limit=60")
        rows = js["reflog"]
        target = find_reflog_index(rows, sha_n2, prefer="commit")
        if target is None:
            log("    reflog 全部行: " + " | ".join(
                "%d %s %s" % (r["index"], r["short"], r["action"]) for r in rows[:12]))
        ok("reflog 里能找到 N2 那一条", target is not None, "index=%s" % target)
        n_before = len(rows)
        st, plan = api(port, "GET",
                       "/api/history/plan/reflog-restore?index=%s&mode=hard"
                       % (target if target is not None else 0))
        ok("restore 预演 200", st == 200,
           "target=%s mode=%s" % ((plan.get("target") or {}).get("short"), plan.get("mode")))
        st, js = api(port, "POST", "/api/history/reflog/restore",
                     {"index": target, "mode": "hard", "confirmed": True})
        ok("restore 执行 200", st == 200, "head_now=%s" % (js.get("head_now") or "")[:8])
        ok("HEAD 确实落在 N2", head_full(repo) == sha_n2)
        ok("工作区文件就是 N2 的版本（现场复原）", rd(repo, "calc/core.py") == snap_n2)
        st, js = api(port, "GET", "/api/status")
        ok("restore 后工作区干净", js["staged"] == [] and js["unstaged"] == [])
        st, js = api(port, "GET", "/api/history/reflog?limit=60")
        rows2 = js["reflog"]
        ok("restore 是追加而不是覆盖（reflog +1）", len(rows2) >= n_before + 1,
           "%d -> %d" % (n_before, len(rows2)))
        reach = subprocess.run(["git", "cat-file", "-e", sha_n4], cwd=repo,
                               capture_output=True)
        ok("被「抛下」的 N4 仍可达（不是丢历史）", reach.returncode == 0, sha_n4[:8])
        idx_n4 = find_reflog_index(rows2, sha_n4, prefer=None)
        if idx_n4 is None:
            log("    reflog 全部行: " + " | ".join(
                "%d %s %s" % (r["index"], r["short"], r["action"]) for r in rows2[:12]))
        ok("reflog 里能找到 N4", idx_n4 is not None, "index=%s" % idx_n4)
        st, js = api(port, "POST", "/api/history/reflog/restore",
                     {"index": idx_n4, "mode": "hard", "confirmed": True})
        ok("退回 N4 成功", st == 200 and head_full(repo) == sha_n4)
        t_ok, tail = run_tests(repo)
        log("    真测试：" + " / ".join(tail[-2:]))
        ok("退回后测试绿", t_ok)

        # ---------------- 收尾 ----------------
        log("\n=== 收尾：最终现场")
        st, js = api(port, "GET", "/api/status")
        log("    branch=%s  staged=%d  unstaged=%d" %
            (js.get("branch"), len(js["staged"]), len(js["unstaged"])))
        st, js = api(port, "GET", "/api/log?limit=10")
        for c in reversed(js["log"]):
            log("    %s  %s" % (c["sha"][:8], c["subject"]))
        st, js = api(port, "GET", "/api/history/reflog?limit=6")
        log("    reflog 顶部: " + " | ".join("%s %s" % (r["short"], r["action"])
                                            for r in js["reflog"][:6]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    log("\n" + "=" * 62)
    log("  通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    for f in FAILS:
        log("   FAIL: " + f)
    if os.environ.get("AIGIT_KEEP_SCENARIO") == "1":
        log("  临时仓（按要求保留）: " + repo)
    else:
        shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
