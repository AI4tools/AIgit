# -*- coding: utf-8 -*-
"""基准（不是门禁）：fork 一个节点、回退到某个节点要多久。

用法：python -X utf8 c\tests\rollback_speed.py
      AIGIT_BENCH_FILES=5000 可以换仓库规模（默认 300）。

量「fork 一个节点」和「回退到某个节点」要多快。

测的是产品真身（c/build/aigit.exe），不是估算：
  * fork  = POST /api/branch（在当前节点开一条新分支）+ POST /api/checkout
  * 回退  = POST /api/history/reflog/restore（--hard，回到某个 reflog 节点）
  * 回退  = POST /api/history/revert（反向提交，历史安全）
  * 回退  = POST /api/discard（丢未提交改动）
  * 往返  = POST /api/history/stash + pop

每个数字都给 p50/p95/max，并带上服务器自报的 git spawn 次数
（X-aigit-git-calls），因为这里的时间几乎全是 spawn 成本。
"""
import http.client
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import socket

EXE = r"D:\project\aigit\c\build\aigit.exe"
N_FILES = int(os.environ.get("AIGIT_BENCH_FILES", "300"))
N_COMMITS = 20
ROUNDS = 10


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def api(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    data = json.dumps(body).encode() if body is not None else None
    t0 = time.perf_counter()
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read()
    dt = (time.perf_counter() - t0) * 1000
    calls = r.getheader("X-aigit-git-calls")
    c.close()
    try:
        js = json.loads(raw)
    except Exception:
        js = raw.decode("utf-8", "replace")
    return dt, r.status, js, calls


def build(root):
    sh("git", "init", "-q", "-b", "main", cwd=root)
    for k, v in (("user.name", "bench"), ("user.email", "b@b")):
        sh("git", "config", k, v, cwd=root)
    for i in range(N_FILES):
        with open(os.path.join(root, "f%04d.txt" % i), "w", encoding="utf-8") as f:
            f.write("".join("line %d of %d\n" % (j, i) for j in range(20)))
    sh("git", "add", "-A", cwd=root)
    sh("git", "commit", "-q", "-m", "c0", cwd=root)
    for k in range(1, N_COMMITS + 1):
        p = os.path.join(root, "f%04d.txt" % (k * 7 % N_FILES))
        with open(p, "a", encoding="utf-8") as f:
            f.write("commit %d\n" % k)
        sh("git", "add", "-A", cwd=root)
        sh("git", "commit", "-q", "-m", "c%d" % k, cwd=root)


def stats(name, xs, calls):
    xs = sorted(xs)
    print("  %-34s p50=%7.1fms  p95=%7.1fms  max=%7.1fms   spawn=%s"
          % (name, statistics.median(xs), xs[int(len(xs) * 0.95)], xs[-1], calls))


def main():
    root = tempfile.mkdtemp(prefix="aigit_speed_")
    build(root)
    port = free_port()
    env = dict(os.environ, AIGIT_GIT_COUNT="1")
    p = subprocess.Popen([EXE, "--root", root, "--port", str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health"); break
        except Exception:
            time.sleep(0.1)

    try:
        for _ in range(5):
            api(port, "GET", "/api/status")

        print("\n仓库：%d 个文件 × 20 行，%d 次提交（HEAD 在第 %d 个节点）"
              % (N_FILES, N_COMMITS, N_COMMITS))

        # ---- fork：在当前节点开新分支并切过去（= 复制一条会话线）----
        xs, calls = [], set()
        for i in range(ROUNDS):
            dt, st, js, c = api(port, "POST", "/api/branch", {"name": "fork%d" % i})
            xs.append(dt); calls.add(c)
            assert st == 200, js
        stats("fork: branch(创建并切过去)", xs, "/".join(sorted(calls)))

        xs, calls = [], set()
        for i in range(ROUNDS):
            dt, st, js, c = api(port, "POST", "/api/checkout",
                                {"name": "fork%d" % i})
            xs.append(dt); calls.add(c)
            dt, st, js, c = api(port, "POST", "/api/checkout", {"name": "main"})
            xs.append(dt); calls.add(c)
        stats("fork: checkout 来回（单次）", xs, "/".join(sorted(calls)))

        # ---- 回退：reflog restore --hard 到 N 个节点之前，再回来 ----
        dt, st, js, _ = api(port, "GET", "/api/history/reflog?limit=200")
        rows = js["reflog"]
        older = [r for r in rows if r["action"].startswith("commit")]
        idx_old = older[5]["index"] if len(older) > 5 else older[-1]["index"]
        xs, calls = [], set()
        for i in range(ROUNDS):
            dt, st, js, c = api(port, "POST", "/api/history/reflog/restore",
                                {"index": idx_old, "mode": "hard", "confirmed": True})
            xs.append(dt); calls.add(c)
            assert st == 200, js
            dt, st, js, c = api(port, "POST", "/api/history/reflog/restore",
                                {"index": 0, "mode": "hard", "confirmed": True})
            xs.append(dt); calls.add(c)
            assert st == 200, js
        stats("回退: reflog restore --hard（单次）", xs, "/".join(sorted(calls)))

        # ---- 回退：revert 一个提交（历史安全）----
        f = "f0007.txt"
        xs, calls = [], set()
        for i in range(5):
            # 每轮先造一个**新的**提交，再 revert 它（同一个提交 revert 两次会
            # 报「nothing to commit」，那不是被测对象）
            with open(os.path.join(root, f), "a", encoding="utf-8") as fh:
                fh.write("victim %d\n" % i)
            api(port, "POST", "/api/stage", {"files": [f]})
            api(port, "POST", "/api/commit", {"message": "victim %d" % i})
            dt2, st2, js2, _ = api(port, "GET", "/api/log?limit=1")
            victim = js2["log"][0]["sha"]
            dt, st, js, c = api(port, "POST", "/api/history/revert",
                                {"sha": victim, "no_edit": True})
            xs.append(dt); calls.add(c)
            assert st == 200, js
        stats("回退: revert（一次性反向提交）", xs, "/".join(sorted(calls)))

        # ---- 回退：discard 未提交改动 ----
        xs, calls = [], set()
        for i in range(ROUNDS):
            with open(os.path.join(root, f), "a", encoding="utf-8") as fh:
                fh.write("dirty %d\n" % i)
            dt, st, js, c = api(port, "POST", "/api/discard",
                                {"files": [f], "confirmed": True})
            xs.append(dt); calls.add(c)
            assert st == 200, js
        stats("回退: discard（丢未提交改动）", xs, "/".join(sorted(calls)))

        # ---- 往返：stash + pop ----
        xs, calls = [], set()
        for i in range(5):
            with open(os.path.join(root, f), "a", encoding="utf-8") as fh:
                fh.write("wip %d\n" % i)
            dt, st, js, c = api(port, "POST", "/api/history/stash",
                                {"message": "wip%d" % i})
            xs.append(dt); calls.add(c)
            dt, st, js, c = api(port, "POST", "/api/history/stash/pop",
                                {"index": 0, "confirmed": True})
            xs.append(dt); calls.add(c)
            assert st == 200, js
        stats("往返: stash + pop（合计）", xs, "/".join(sorted(calls)))

        print("\n  说明：这些耗时几乎全是 git 子进程的固定成本（本机 ~40ms/次）；")
        print("        fork 不复制文件，成本与仓库大小基本无关；")
        print("        restore --hard 只写「目标节点与当前不同」的文件。")
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
