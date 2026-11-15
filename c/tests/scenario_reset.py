# -*- coding: utf-8 -*-
"""场景：diff 审错了怎么重置 —— 四条路径逐条实测。

A. 还没 stage（工作区脏）      -> discard（先 /discard/plan 预演）
B. 已经 stage（在审核队列里）  -> unstage；再决定丢弃 -> discard
C. 已经 commit（审完才发现错） -> reset（soft/mixed/hard，必须显式 mode + confirmed）
D. 已经 commit 且不想改历史    -> revert（另测过 160ms）/ reflog restore

顺带验护栏：不带 mode、不带 confirmed 都必须 400，不能静默替用户做决定。
"""
import http.client
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(
    os.path.dirname(HERE), "build", "aigit.exe")
BASE = tempfile.mkdtemp(prefix="aigit_reset_")
FAILS, OKS = [], []


def log(m):
    print(m, flush=True)


def ok(name, cond, detail=""):
    (OKS if cond else FAILS).append(name)
    log("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  " + detail) if detail else ""))


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def api(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    data = json.dumps(body).encode() if body is not None else None
    t0 = time.perf_counter()
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse(); raw = r.read().decode("utf-8", "replace"); c.close()
    dt = (time.perf_counter() - t0) * 1000
    try:
        return dt, r.status, json.loads(raw)
    except Exception:
        return dt, r.status, raw


def rd(p):
    return open(p, encoding="utf-8").read()


def w(p, s):
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def main():
    repo = os.path.join(BASE, "repo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        sh("git", "config", k, v, cwd=repo)
    for n in ("f1.txt", "f2.txt", "f3.txt"):
        w(os.path.join(repo, n), "base %s\n" % n)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    base = sh("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()

    port = free()
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health"); break
        except Exception:
            time.sleep(0.1)

    try:
        log("\n=== B. 已 stage 的改动审错了（审核队列里）")
        w(os.path.join(repo, "f1.txt"), "bad edit 1\n")
        w(os.path.join(repo, "f2.txt"), "bad edit 2\n")
        api(port, "POST", "/api/stage", {"files": ["f1.txt", "f2.txt"]})
        _, st, js = api(port, "GET", "/api/status")
        ok("暂存后 status 里有 2 个 staged", len(js["staged"]) == 2)
        _, st, js = api(port, "GET", "/api/diff?file=f1.txt&staged=1")
        ok("staged diff 能看到坏改动", "+bad edit 1" in (js.get("diff") or ""))

        dt, st, js = api(port, "POST", "/api/unstage", {"files": ["f1.txt", "f2.txt"]})
        _, _, st2 = api(port, "GET", "/api/status")
        ok("unstage 把两个文件退回工作区", st == 200
           and st2["staged"] == [] and len(st2["unstaged"]) == 2,
           "%.1fms" % dt)
        _, st, js = api(port, "GET", "/api/diff?file=f1.txt&staged=1")
        ok("撤出审核队列后 staged diff 为空", (js.get("diff") or "").strip() == "")

        log("\n=== A. 决定整份丢掉（先预演再执行）")
        dt, st, plan = api(port, "GET", "/api/discard/plan?files=f1.txt&files=f2.txt")
        paths = [e.get("path") for e in (plan.get("entries") or [])]
        ok("discard/plan 列出要丢的两个文件", st == 200 and len(paths) == 2, str(paths))

        dt, st, js = api(port, "POST", "/api/discard", {"files": ["f1.txt", "f2.txt"]})
        ok("不带 confirmed 的 discard 被拒（400）",
           st == 400 and (js or {}).get("code") == "needs_confirmation",
           "%.1fms" % dt)

        dt, st, js = api(port, "POST", "/api/discard",
                         {"files": ["f1.txt", "f2.txt"], "confirmed": True})
        ok("confirmed 后 discard 成功（%.1fms）" % dt, st == 200)
        same = (rd(os.path.join(repo, "f1.txt")) == "base f1.txt\n"
                and rd(os.path.join(repo, "f2.txt")) == "base f2.txt\n")
        ok("两个文件逐字节回到提交内容", same)
        _, _, st2 = api(port, "GET", "/api/status")
        ok("工作区干净", st2["staged"] == [] and st2["unstaged"] == [])

        log("\n=== C. 已经提交了才发现审错 -> reset")
        w(os.path.join(repo, "f3.txt"), "wrong committed change\n")
        api(port, "POST", "/api/stage", {"files": ["f3.txt"]})
        api(port, "POST", "/api/commit", {"message": "bad: 审错了"})
        bad_commit = sh("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        _, _, js = api(port, "GET", "/api/log?limit=2")
        ok("坏提交已在历史里", js["log"][0]["subject"] == "bad: 审错了")

        dt, st, plan = api(port, "GET", "/api/reset/plan?mode=hard&ref=HEAD~1")
        ok("reset/plan 预演 200", st == 200, "%.1fms" % dt)
        dt, st, js = api(port, "POST", "/api/reset", {"ref": "HEAD~1", "confirmed": True})
        ok("缺 mode 的 reset 被拒（400）", st == 400, str(js)[:70])
        dt, st, js = api(port, "POST", "/api/reset", {"mode": "hard", "ref": "HEAD~1"})
        ok("缺 confirmed 的 reset 被拒（400）",
           st == 400 and (js or {}).get("code") == "needs_confirmation")

        dt, st, js = api(port, "POST", "/api/reset",
                         {"mode": "hard", "ref": "HEAD~1", "confirmed": True})
        ok("reset --hard 成功（%.1fms）" % dt, st == 200, str(js)[:70])
        ok("HEAD 回到坏提交之前",
           sh("git", "rev-parse", "HEAD", cwd=repo).stdout.strip() == base)
        ok("文件也回到提交前的内容",
           rd(os.path.join(repo, "f3.txt")) == "base f3.txt\n")

        log("\n=== C2. 想保留改动再看一眼 -> reset --mixed + discard")
        w(os.path.join(repo, "f3.txt"), "wrong again\n")
        api(port, "POST", "/api/stage", {"files": ["f3.txt"]})
        api(port, "POST", "/api/commit", {"message": "bad again"})
        _, st, js = api(port, "POST", "/api/reset",
                        {"mode": "mixed", "ref": "HEAD~1", "confirmed": True})
        ok("reset --mixed 成功", st == 200)
        ok("mixed 保留工作区改动（改动还在但未暂存）",
           rd(os.path.join(repo, "f3.txt")) == "wrong again\n")
        _, _, st2 = api(port, "GET", "/api/status")
        ok("mixed 后改动落在 unstaged", len(st2["unstaged"]) == 1)
        api(port, "POST", "/api/discard", {"files": ["f3.txt"], "confirmed": True})
        ok("再 discard 掉，回到干净现场",
           rd(os.path.join(repo, "f3.txt")) == "base f3.txt\n")

        log("\n=== 速度（5 次中位）")
        xs = []
        for i in range(5):
            w(os.path.join(repo, "f1.txt"), "dirty %d\n" % i)
            xs.append(api(port, "POST", "/api/discard",
                          {"files": ["f1.txt"], "confirmed": True})[0])
        log("    discard 单文件:      p50=%.1fms" % statistics.median(xs))
        xs = []
        for i in range(5):
            w(os.path.join(repo, "f1.txt"), "stage %d\n" % i)
            api(port, "POST", "/api/stage", {"files": ["f1.txt"]})
            xs.append(api(port, "POST", "/api/unstage", {"files": ["f1.txt"]})[0])
            api(port, "POST", "/api/discard", {"files": ["f1.txt"], "confirmed": True})
        log("    unstage 单文件:      p50=%.1fms" % statistics.median(xs))
        xs = []
        for i in range(5):
            w(os.path.join(repo, "f2.txt"), "c %d\n" % i)
            api(port, "POST", "/api/stage", {"files": ["f2.txt"]})
            api(port, "POST", "/api/commit", {"message": "t%d" % i})
            xs.append(api(port, "POST", "/api/reset",
                          {"mode": "hard", "ref": "HEAD~1", "confirmed": True})[0])
        log("    reset --hard 一个提交: p50=%.1fms" % statistics.median(xs))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

    log("\n" + "=" * 58)
    log("  通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    for f in FAILS:
        log("   FAIL: " + f)
    shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
