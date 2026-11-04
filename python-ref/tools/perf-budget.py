# -*- coding: utf-8 -*-
"""性能预算门禁：同一台机器上跑 22 个操作，超预算就红。

## 为什么是「spawn 硬指标 + 墙钟软带」

spawn 次数是**确定性**的：同一份代码、同一组操作，数出来永远是同一个数。
墙钟受机器负载影响（实测同一份代码两轮之间单项抖动 ±35ms），所以它只能当带宽，
不能当判据。`doc/aigit-perf.md` §8.1 定的就是这套分层判据：

    读          ≤1 spawn / 请求（ref/对象类 ≤0）    ≤100ms
    轻写        ≤2 spawn                            ≤170ms
    discard     ≤3 spawn（例外，见 §8.1）           ≤170ms
    commit      ≤8 spawn                            ≤650ms

用法：
    python -X utf8 tools/perf-budget.py              # 跑并判定
    python -X utf8 tools/perf-budget.py --json out.json
退出码：0 全部达标；1 有超预算项。
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(os.environ["TEMP"], "aigit_perfbudget_%d" % (os.getpid() % 100000))
REPO = os.path.join(BASE, "repo")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", AIGIT_GIT_COUNT="1")

# 名称 -> (spawn 预算, 墙钟上限 ms)
BUDGET = {
    "GET /api/status": (1, 100),
    "GET /api/log": (1, 100),
    "GET /api/branches": (0, 100),
    "GET /api/history/tags": (0, 100),
    "GET /api/history/stashes": (0, 100),
    "GET /api/history/reflog": (0, 100),
    "GET /api/diff": (1, 100),
    "GET /api/file": (0, 100),
    "GET /api/history/blame": (1, 100),
    "GET /api/conflict/state": (0, 100),
    "GET /api/conflict/list": (0, 100),
    "GET /api/remote/capability": (0, 100),
    "GET /api/health": (0, 100),
    "POST /api/stage": (2, 170),
    "POST /api/stage (dirty big)": (2, 170),
    "POST /api/stage (second round)": (2, 170),
    "POST /api/unstage": (2, 170),
    "POST /api/checkout": (2, 170),
    "POST /api/discard": (3, 170),
    "POST /api/commit": (8, 650),
    "POST /api/branch": (2, 170),
    "POST /api/merge": (2, 170),
    # --- opt-out status 变体（UI 走的就是这条；它后面紧跟 refresh()）---
    "POST /api/stage (no-status)": (1, 140),
    "POST /api/unstage (no-status)": (1, 140),
    "POST /api/checkout (no-status)": (2, 140),
    "POST /api/branch (no-status)": (1, 140),
    "POST /api/discard (no-status)": (2, 140),
    "POST /api/commit (no-status)": (7, 600),
}

READS = [
    ("GET /api/status", "/api/status"),
    ("GET /api/log", "/api/log?limit=30"),
    ("GET /api/branches", "/api/branches"),
    ("GET /api/history/tags", "/api/history/tags"),
    ("GET /api/history/stashes", "/api/history/stashes"),
    ("GET /api/history/reflog", "/api/history/reflog"),
    ("GET /api/diff", "/api/diff?file=big.txt"),
    ("GET /api/file", "/api/file?file=big.txt"),
    ("GET /api/history/blame", "/api/history/blame?file=big.txt"),
    ("GET /api/conflict/state", "/api/conflict/state"),
    ("GET /api/conflict/list", "/api/conflict/list"),
    ("GET /api/remote/capability", "/api/remote/capability"),
    ("GET /api/health", "/api/health"),
]


def sh(*a, cwd=None):
    p = subprocess.run(["git"] + list(a), cwd=cwd or REPO, capture_output=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:200]))
    return p.stdout


def build():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    os.makedirs(REPO)
    sh("init", "-q", "-b", "main")
    sh("config", "user.name", "bench")
    sh("config", "user.email", "bench@localhost")
    with open(os.path.join(REPO, "big.txt"), "w", encoding="utf-8", newline="\n") as f:
        for i in range(1, 1001):
            f.write("line %04d: quick brown fox jumps over lazy dog %d\n" % (i, i))
    os.makedirs(os.path.join(REPO, "src"))
    for i in range(200):
        with open(os.path.join(REPO, "src", "f%03d.py" % i), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("# m%d\nv_%d = %d\n" % (i, i, i))
    sh("add", "--", "big.txt", "src")
    sh("-c", "user.name=bench", "-c", "user.email=bench@localhost",
       "commit", "-q", "-m", "init")


def dirty():
    with open(os.path.join(REPO, "big.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("line 1001: dirty tail\n")
    for i in range(10):
        with open(os.path.join(REPO, "src", "f%03d.py" % i), "a",
                  encoding="utf-8", newline="\n") as f:
            f.write("# dirty\n")
    for i in range(20):
        with open(os.path.join(REPO, "u%02d.txt" % i), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("u%d\n" % i)


def start():
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    finally:
        s.close()
    p = subprocess.Popen([sys.executable, "-X", "utf8",
                          os.path.join(ROOT, "server.py"),
                          "--root", REPO, "--port", str(port)],
                         env=ENV, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return p, base, port
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("server died")
            time.sleep(0.25)
    raise RuntimeError("server slow")


def req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    rq = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        r = urllib.request.urlopen(rq, timeout=60)
        code, payload, hdr = r.getcode(), r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, payload, hdr = e.code, e.read(), e.headers
    ms = (time.perf_counter() - t0) * 1000.0
    try:
        j = json.loads(payload.decode("utf-8"))
    except Exception:
        j = {}
    return {"ms": ms, "http": code, "calls": hdr.get("X-aigit-git-calls"),
            "cmds": hdr.get("X-aigit-git-cmds"), "ok": j.get("ok"),
            "error": j.get("error")}


def med(base, method, path, body=None, runs=6):
    rows = [req(base, method, path, body) for _ in range(runs)]
    times = sorted(r["ms"] for r in rows)
    if runs >= 4:
        times = times[1:-1]
    out = dict(rows[-1])
    out["ms"] = statistics.median(times)
    out["n"] = runs
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    build()
    dirty()
    proc, base, port = start()
    res = {}
    try:
        for name, path in READS:
            res[name] = med(base, "GET", path)
        staged = ["src/f%03d.py" % i for i in range(10)]
        # ⚠ 写操作**只跑一次**、并且**断言 200**：重复发同一个写请求，第二次
        # 往往变成「分支已存在 / 没有可提交的改动」的失败路径 —— 那测到的 spawn
        # 数不是成功路径的（踩过：branch 1 spawn、commit 6 spawn）。
        def w(name, path, body):
            r = req(base, "POST", path, body)
            r["write_ok"] = (r["http"] == 200)
            res[name] = r

        w("POST /api/stage", "/api/stage", {"files": staged})
        w("POST /api/stage (dirty big)", "/api/stage", {"files": ["big.txt"]})
        w("POST /api/stage (second round)", "/api/stage", {"files": staged})
        w("POST /api/unstage", "/api/unstage", {"files": staged})
        sh("checkout", "-q", "-b", "perfbr")
        w("POST /api/checkout", "/api/checkout", {"name": "main"})
        w("POST /api/branch", "/api/branch", {"name": "perfbr2"})
        w("POST /api/merge", "/api/merge", {"name": "perfbr"})
        w("POST /api/discard", "/api/discard",
          {"files": ["u00.txt"], "confirmed": True})
        w("POST /api/commit", "/api/commit", {"message": "perf budget commit"})

        # --- 同一批写操作，但客户端声明「不要响应里的 status」 ---
        # UI 的每个写操作后面都紧跟 refresh()（它自己会取 /api/status），
        # 所以这一份 status 是重复的。这里量的是「省掉它」之后的样子。
        dirty()
        w("POST /api/stage (no-status)", "/api/stage",
          {"files": staged, "status": False})
        w("POST /api/unstage (no-status)", "/api/unstage",
          {"files": staged, "status": False})
        # 从当前 HEAD 建一个**同树**分支再切过去：脏文件在两边内容一样,
        # git 不会拒绝（切到 main 会 400 —— 那是正确行为, 不是缺陷）。
        sh("branch", "perfns")
        w("POST /api/checkout (no-status)", "/api/checkout",
          {"name": "perfns", "status": False})
        w("POST /api/branch (no-status)", "/api/branch",
          {"name": "perfbr3", "status": False})
        w("POST /api/discard (no-status)", "/api/discard",
          {"files": ["u01.txt"], "confirmed": True, "status": False})
        w("POST /api/commit (no-status)", "/api/commit",
          {"message": "no status commit", "status": False})
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()

    bad = []
    print("  %-34s %7s %6s %6s %6s  %s" % ("操作", "ms", "spawn", "sp预算", "ms预算", ""))
    for name, v in res.items():
        sp_budget, ms_budget = BUDGET.get(name, (99, 10 ** 9))
        spawn = int(v["calls"] or 0)
        ok_sp = spawn <= sp_budget
        ok_ms = v["ms"] <= ms_budget
        ok_path = v.get("write_ok", True)
        mark = "ok" if (ok_sp and ok_ms and ok_path) else "FAIL"
        if not (ok_sp and ok_ms and ok_path):
            bad.append((name, v["ms"], spawn, ms_budget, sp_budget,
                        "" if ok_path else "HTTP %s(非成功路径)" % v.get("http")))
        print("  %-34s %7.1f %6d %6d %6d  %s"
              % (name[:34], v["ms"], spawn, sp_budget, ms_budget, mark))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if bad:
        print()
        for name, ms, spawn, ms_budget, sp_budget, why in bad:
            print("  FAIL %-32s ms=%.1f(≤%d) spawn=%d(≤%d) %s"
                  % (name[:32], ms, ms_budget, spawn, sp_budget, why))
        print("  超预算 %d 项 / %d" % (len(bad), len(res)))
        return 1
    print("  全部在预算内（%d 项）" % len(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
