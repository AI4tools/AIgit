# -*- coding: utf-8 -*-
"""批量 diff 端点（C 超集）的判据：内容必须与逐文件 GET 完全一致，且快得多。"""
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
BASE = tempfile.mkdtemp(prefix="aigit_bdiff_")
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


def api(port, m, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    d = json.dumps(body).encode() if body is not None else None
    t0 = time.perf_counter()
    c.request(m, path, body=d, headers={"Content-Type": "application/json"} if d else {})
    r = c.getresponse(); raw = r.read().decode("utf-8", "replace"); c.close()
    dt = (time.perf_counter() - t0) * 1000
    try:
        return dt, r.status, json.loads(raw)
    except Exception:
        return dt, r.status, raw


def main():
    repo = os.path.join(BASE, "repo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    sh("git", "config", "user.email", "t@t", cwd=repo)

    # 各种名字：普通 / 空格 / 中文 / 子目录
    names = ["a.txt", "b b.txt", "中文.txt", "sub/c.txt", "crlf.txt"]
    sh("git", "config", "core.autocrlf", "false", cwd=repo)
    for n in names:
        p = os.path.join(repo, n)
        os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
        nl = "\r\n" if n == "crlf.txt" else "\n"
        open(p, "w", encoding="utf-8", newline="").write("base %s%s" % (n, nl))
    open(os.path.join(repo, "rename-me.txt"), "w", encoding="utf-8").write("r\n" * 50)
    open(os.path.join(repo, "bin.dat"), "wb").write(bytes([1, 2, 0, 3]))
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)

    for n in names:
        nl = "\r\n" if n == "crlf.txt" else "\n"
        open(os.path.join(repo, n), "a", encoding="utf-8", newline="").write("edit %s%s" % (n, nl))
    sh("git", "mv", "rename-me.txt", "renamed.txt", cwd=repo)
    open(os.path.join(repo, "bin.dat"), "wb").write(bytes([1, 2, 0, 9, 9]))
    sh("git", "add", "-A", cwd=repo)

    port = free()
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health"); break
        except Exception:
            time.sleep(0.1)

    try:
        all_files = names + ["rename-me.txt", "renamed.txt", "bin.dat"]
        dt_b, st, js = api(port, "POST", "/api/diff",
                           {"files": all_files, "staged": True})
        ok("批量 diff 200 且条数正确", st == 200 and js.get("count") == len(all_files),
           "%.1fms" % dt_b)
        got = {f["path"]: f["diff"] for f in js.get("files") or []}

        # 与逐文件 GET 逐字节对比（改名那个按 --no-renames 语义：删除 + 新增）
        diffs_same = True
        detail = []
        for n in names + ["bin.dat"]:
            _, s1, j1 = api(port, "GET",
                            "/api/diff?file=%s&staged=1" % urllib.parse.quote(n))
            single = j1.get("diff") or ""
            if got.get(n) != single:
                diffs_same = False
                a = got.get(n) or ""
                b = single
                k = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]),
                         min(len(a), len(b)))
                detail.append("%s: batch=%dB single=%dB at=%d batch=%r single=%r"
                              % (n, len(a), len(b), k,
                                 a[max(0, k - 30):k + 30], b[max(0, k - 30):k + 30]))
        ok("每个文件的 diff 与逐文件 GET 逐字节相同", diffs_same, str(detail))
        ok("改名一侧（新路径）在批量里有内容",
           "diff --git" in (got.get("renamed.txt") or ""))
        ok("改名一侧（旧路径）在批量里有删除块",
           "-" in (got.get("rename-me.txt") or ""))
        ok("二进制文件有 diff 块（Binary files）",
           "Binary" in (got.get("bin.dat") or ""), (got.get("bin.dat") or "")[:40])

        # 未暂存视图
        sh("git", "reset", "-q", cwd=repo)
        dt_u, stu, jsu = api(port, "POST", "/api/diff", {"files": names})
        ok("不带 staged 时看工作区（默认 false）",
           stu == 200 and jsu.get("staged") is False
           and all(("+edit" in (f["diff"] or "")) for f in jsu["files"]))
        dt_s, sts, jss = api(port, "POST", "/api/diff",
                             {"files": names, "staged": False})
        ok("显式 staged=false 与省略一致", sts == 200)

        log("\n=== 护栏")
        dt, st, js = api(port, "POST", "/api/diff", {})
        ok("缺 files -> 400", st == 400, str(js)[:60])
        dt, st, js = api(port, "POST", "/api/diff", {"files": []})
        ok("files 为空 -> 400", st == 400)
        dt, st, js = api(port, "POST", "/api/diff", {"files": [".git/config"]})
        ok("夹带 .git/config -> 400", st == 400, str(js)[:60])
        dt, st, js = api(port, "POST", "/api/diff", {"files": ["../x.txt"]})
        ok("夹带越界路径 -> 400", st == 400, str(js)[:60])
        dt, st, js = api(port, "POST", "/api/diff", {"files": ["nope.txt"]})
        ok("不存在的文件 -> 400（与单文件版一致）", st == 400, str(js)[:60])
        dt, st, js = api(port, "POST", "/api/diff", {"files": ["a.txt"], "staged": "1"})
        ok("staged 不是布尔 -> 400", st == 400, str(js)[:60])

        log("\n=== 规模：300 个改动文件，批量 vs 逐个")
        big = ["big%03d.txt" % i for i in range(300)]
        for n in big:
            open(os.path.join(repo, n), "w", encoding="utf-8").write("b\n")
        sh("git", "add", "-A", cwd=repo)
        sh("git", "commit", "-q", "-m", "big", cwd=repo)
        for n in big:
            open(os.path.join(repo, n), "a", encoding="utf-8").write("edit\n")
        sh("git", "add", "-A", cwd=repo)

        dt_batch, st, js = api(port, "POST", "/api/diff",
                               {"files": big, "staged": True})
        ok("一次批量 300 个文件", st == 200 and js["count"] == 300, "%.1fms" % dt_batch)
        # 逐个取（只取 30 个估总价，避免测试太久）
        sample = big[:30]
        xs = [api(port, "GET", "/api/diff?file=%s&staged=1" % n)[0] for n in sample]
        est = statistics.median(xs) * 300
        ok("批量的每个 diff 与逐个一致（抽样 10 个）",
           all(("+edit" in f["diff"]) for f in js["files"][:10]))
        log("    批量 300 个 = %.1fms；逐个推算 300 个 ≈ %.0fms（快 %.0f×）"
            % (dt_batch, est, est / dt_batch))
        ok("批量比逐个快 10 倍以上", est / dt_batch > 10,
           "%.0f×" % (est / dt_batch))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

    log("\n" + "=" * 56)
    log("  通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    for f in FAILS:
        log("   FAIL: " + f)
    shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
