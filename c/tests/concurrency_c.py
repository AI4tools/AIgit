#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C 版并发冒烟：多线程同时打同一批读端点，结果必须与单线程基线逐项相等。

目的不是压吞吐，而是抓**线程/句柄共享**类缺陷（服务是每连接一线程）：
常驻 cat-file 的临界区、全局计数、buf 复用、文件句柄泄漏。
判据：所有响应 HTTP 码与 JSON 深比较都等于单线程基线；服务进程不能退出。
"""
import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(os.path.dirname(HERE), "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_conc_%d" % (os.getpid() % 100000))
GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost"]

CASES = [
    "/api/health",
    "/api/status",
    "/api/branches",
    "/api/log?limit=20",
    "/api/file?file=f1.txt",
    "/api/file?file=sub/a.txt",
    "/api/diff",
    "/api/history/file?file=f1.txt&limit=10",
    "/api/history/blame?file=f1.txt&start=1&end=10",
    "/api/history/tags?limit=20",
    "/api/history/reflog?limit=10",
    "/api/history/stashes?limit=10",
    "/api/conflict/state",
    "/api/conflict/list",
    "/api/remote/list",
]


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


def build():
    if os.path.isdir(BASE):
        shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    sh("git", "init", "-q", BASE)
    os.makedirs(os.path.join(BASE, "sub"))
    for i in range(1, 6):
        with open(os.path.join(BASE, "f%d.txt" % i), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("".join("line %d of f%d\n" % (j, i) for j in range(1, 41)))
    with open(os.path.join(BASE, "sub", "a.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("a1\na2\n")
    with open(os.path.join(BASE, "中文 空格.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("中\n")
    sh(*GIT, "add", "-A", cwd=BASE)
    sh(*GIT, "commit", "-q", "-m", "c1", cwd=BASE)
    with open(os.path.join(BASE, "f1.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("dirty\n")
    with open(os.path.join(BASE, "sub", "a.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("dirty2\n")
    sh(*GIT, "add", "sub/a.txt", cwd=BASE)
    sh("git", "tag", "-a", "v1", "-m", "tag v1", cwd=BASE)
    with open(os.path.join(BASE, "untracked.txt"), "w", encoding="utf-8") as f:
        f.write("u\n")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start():
    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               AIGIT_GIT_COUNT="1")
    p = subprocess.Popen([EXE, "--root", BASE, "--port", str(port)], env=env,
                         cwd=os.path.dirname(EXE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return p, base
        except Exception:
            if p.poll() is not None:
                err = p.stderr.read().decode("utf-8", "replace")
                raise RuntimeError("server died: " + err)
            time.sleep(0.1)
    raise RuntimeError("server slow")


def get(base, path):
    try:
        r = urllib.request.urlopen(base + path, timeout=120)
        return r.getcode(), json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, {"__unparsable__": raw[:200].decode("utf-8", "replace")}


def norm(x):
    """去掉计数头之类不参与比较的字段？这里比的是 body，不涉及头。"""
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    build()
    proc, base = start()
    try:
        baseline = {}
        for path in CASES:
            code, js = get(base, path)
            baseline[path] = (code, norm(js))

        def one(i):
            path = CASES[i % len(CASES)]
            code, js = get(base, path)
            return path, code, norm(js)

        t0 = time.time()
        bad = []
        total = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, i) for i in range(workers * rounds)]
            for f in concurrent.futures.as_completed(futs):
                total += 1
                path, code, body = f.result()
                want_code, want_body = baseline[path]
                if code != want_code or body != want_body:
                    bad.append((path, code, body[:200]))

        dt = time.time() - t0
        alive = proc.poll() is None
        print("  并发 %d 线程 × %d 轮 = %d 请求，%.1fs（%.0f req/s）"
              % (workers, rounds, total, dt, total / dt if dt else 0))
        print("  不一致：%d" % len(bad))
        for b in bad[:5]:
            print("    %s -> code=%s %s" % (b[0], b[1], b[2][:120]))
        print("  服务存活：%s" % ("是" if alive else "否"))
        fails = len(bad) + (0 if alive else 1)
        print("\n  失败 %d" % fails)
        return 0 if fails == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
