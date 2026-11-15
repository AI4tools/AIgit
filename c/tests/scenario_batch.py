# -*- coding: utf-8 -*-
"""批量审核 / 批量重置的实测（300 个改动文件 + 100 个未跟踪）。

覆盖：
  1. 一次 stage 300 个文件（审核队列）      —— 时间 + 计数
  2. 逐个取 diff（前端审核的实际做法）      —— 单文件均价 × 300 的推算
  3. 一次 unstage 300 个                    —— 时间 + 计数
  4. 一次 discard 300 个                    —— 逐字节回滚校验
  5. reset --hard（整树 300 个脏文件）      —— 时间
  6. 边界：重复路径 / 不存在路径 / .git 路径 / .. 越界 / 1000 个文件
"""
import hashlib
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
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(
    os.path.dirname(HERE), "build", "aigit.exe")
BASE = tempfile.mkdtemp(prefix="aigit_batch_")
N = 300
N_UNTRACKED = 100
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
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
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


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    repo = os.path.join(BASE, "repo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        sh("git", "config", k, v, cwd=repo)
    names = ["f%03d.txt" % i for i in range(N)]
    for n in names:
        open(os.path.join(repo, n), "w", encoding="utf-8").write("base %s\n" % n)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    base_sha = {n: sha(os.path.join(repo, n)) for n in names}

    port = free()
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health"); break
        except Exception:
            time.sleep(0.1)

    try:
        log("\n=== 造 %d 个改动 + %d 个未跟踪" % (N, N_UNTRACKED))
        for n in names:
            open(os.path.join(repo, n), "w", encoding="utf-8").write("edited %s\n" % n)
        untracked = ["u%03d.txt" % i for i in range(N_UNTRACKED)]
        for n in untracked:
            open(os.path.join(repo, n), "w", encoding="utf-8").write("new %s\n" % n)
        # status 把未跟踪文件也放在 unstaged 里（status == "?"），没有单独字段
        _, st, js = api(port, "GET", "/api/status")
        n_q = sum(1 for x in js["unstaged"] if x.get("status") == "?")
        ok("status 报出 300 个改动 + 100 个未跟踪（合并在 unstaged）",
           len(js["unstaged"]) == N + N_UNTRACKED and n_q == N_UNTRACKED,
           "unstaged=%d 其中未跟踪=%d" % (len(js["unstaged"]), n_q))

        log("\n=== 1. 批量审核：一次 stage 300 个")
        dt, st, js = api(port, "POST", "/api/stage", {"files": names})
        _, st2, s2 = api(port, "GET", "/api/status")
        ok("一次 stage 300 个成功", st == 200 and len(s2["staged"]) == N,
           "%.1fms" % dt)

        log("\n=== 2. 逐文件取 diff（前端审核的实际做法）")
        sample = names[:50]
        xs = []
        for n in sample:
            d, s_, j = api(port, "GET", "/api/diff?file=%s&staged=1" % urllib.parse.quote(n))
            xs.append(d)
            assert s_ == 200 and "+edited" in (j.get("diff") or ""), (s_, j)
        med = statistics.median(xs)
        log("    50 个文件逐个 diff：p50=%.1fms/个，300 个约 %.1fs" % (med, med * 300 / 1000))
        ok("每个文件的 staged diff 都含本次改动", True, "p50=%.1fms" % med)

        log("\n=== 3. 批量重置之一：一次 unstage 300 个")
        dt, st, js = api(port, "POST", "/api/unstage", {"files": names})
        _, _, s3 = api(port, "GET", "/api/status")
        ok("一次 unstage 300 个成功", st == 200 and s3["staged"] == []
           and len(s3["unstaged"]) == N + N_UNTRACKED, "%.1fms" % dt)

        log("\n=== 4. 批量重置之二：一次 discard 300 个（逐字节校验）")
        q = "&".join("files=" + urllib.parse.quote(n) for n in names)
        dq, stq, plan = api(port, "GET", "/api/discard/plan?" + q)
        ok("discard/plan 一次列出 300 个（query 长 %.1fKB）",
           stq == 200 and len(plan.get("entries") or []) == N,
           "%.1fms, %.1fKB" % (dq, len(q) / 1024))
        dt, st, js = api(port, "POST", "/api/discard",
                         {"files": names, "confirmed": True})
        same = sum(1 for n in names if sha(os.path.join(repo, n)) == base_sha[n])
        _, _, s4 = api(port, "GET", "/api/status")
        left_q = sum(1 for x in s4["unstaged"] if x.get("status") == "?")
        ok("一次 discard 300 个成功且逐字节回滚（未跟踪的不动）",
           st == 200 and same == N and len(s4["unstaged"]) == N_UNTRACKED
           and left_q == N_UNTRACKED,
           "%.1fms，%d/%d 字节一致，剩 %d 个未跟踪" % (dt, same, N, left_q))

        log("\n=== 5. 批量重置之三：reset --hard（整树 300 个脏文件）")
        for n in names:
            open(os.path.join(repo, n), "a", encoding="utf-8").write("dirty\n")
        _, _, s5 = api(port, "GET", "/api/status")
        ok("reset 前有 300 个脏文件", sum(1 for x in s5["unstaged"] if x.get("status") != "?") == N)
        dt, st, js = api(port, "POST", "/api/reset",
                         {"mode": "hard", "ref": "HEAD", "confirmed": True})
        same = sum(1 for n in names if sha(os.path.join(repo, n)) == base_sha[n])
        _, _, s5b = api(port, "GET", "/api/status")
        n_tracked_left = sum(1 for x in s5b["unstaged"] if x.get("status") != "?")
        ok("reset --hard 一次干净且逐字节回滚（未跟踪的不动）",
           st == 200 and same == N and n_tracked_left == 0,
           "%.1fms，%d/%d 字节一致" % (dt, same, N))

        log("\n=== 6. 边界")
        w = lambda n, t: open(os.path.join(repo, n), "w", encoding="utf-8").write(t)
        w("dup.txt", "dup\n")
        dt, st, js = api(port, "POST", "/api/stage", {"files": ["dup.txt", "dup.txt"]})
        ok("批量里出现重复路径不会失败", st == 200, "%.1fms" % dt)
        api(port, "POST", "/api/discard", {"files": ["dup.txt"], "confirmed": True})

        dt, st, js = api(port, "POST", "/api/stage", {"files": ["nope.txt"]})
        # 与 Python 一致：未知路径被 git add 忽略，整体仍 200（已对拍）
        ok("批量里有不存在的文件 -> 200 且整批被忽略", st == 200,
           "%.1fms" % dt)

        dt, st, js = api(port, "POST", "/api/stage", {"files": [".git/config"]})
        ok("批里夹带 .git/config 被拒", st == 400, str(js)[:80])

        dt, st, js = api(port, "POST", "/api/stage", {"files": ["../outside.txt"]})
        ok("批里夹带越界路径被拒", st == 400, str(js)[:80])

        log("\n=== 7. 规模：一次 1000 个文件（测命令行上限）")
        big = ["big%04d.txt" % i for i in range(1000)]
        for n in big:
            open(os.path.join(repo, n), "w", encoding="utf-8").write("x\n")
        sh("git", "add", "-A", cwd=repo)
        sh("git", "commit", "-q", "-m", "big", cwd=repo)
        big_sha = {n: sha(os.path.join(repo, n)) for n in big}
        for n in big:
            open(os.path.join(repo, n), "a", encoding="utf-8").write("dirty\n")
        dt, st, js = api(port, "POST", "/api/stage", {"files": big})
        _, _, s7 = api(port, "GET", "/api/status")
        ok("一次 stage 1000 个（无命令行长度问题）",
           st == 200 and len(s7["staged"]) == 1000, "%.1fms" % dt)
        # 已暂存 -> discard 是 no-op（Python 也是 `git checkout -- path`，从索引恢复）。
        # 这里把这条语义**显式钉住**，免得以后有人以为它该回滚。
        dt, st, js = api(port, "POST", "/api/discard",
                         {"files": big, "confirmed": True})
        same = sum(1 for n in big if sha(os.path.join(repo, n)) == big_sha[n])
        ok("已暂存时 discard 是 no-op（与 Python 同义，不会误删）",
           st == 200 and same == 0 and len(
               [x for x in (api(port, "GET", "/api/status")[2].get("staged") or [])]) == 1000,
           "%.1fms" % dt)

        # 正确的批量重置：先 unstage 再 discard
        api(port, "POST", "/api/unstage", {"files": big})
        dt, st, js = api(port, "POST", "/api/discard",
                         {"files": big, "confirmed": True})
        same = sum(1 for n in big if sha(os.path.join(repo, n)) == big_sha[n])
        ok("先 unstage 再 discard 1000 个，逐字节回滚",
           st == 200 and same == 1000, "%.1fms，%d/1000" % (dt, same))
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
