#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C 版长跑/泄漏判据：多线程持续打混合请求，盯句柄数与工作集。

判据不是「跑完没崩」，而是：
  * 0 个 5xx / 0 个连接异常 / 0 个 JSON 解析失败；
  * 句柄数不随请求数单调增长（末值 <= 首值 + 16）；
  * 工作集不持续增长（末值 <= 首值 + 64MB）。

只对临时仓操作；写请求只在 stage/unstage 之间来回，不改 HEAD。
"""
import concurrent.futures
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(os.path.dirname(HERE), "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_soak_%d" % (os.getpid() % 100000))
GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost"]

READS = [
    "/api/health", "/api/status", "/api/branches", "/api/log?limit=20",
    "/api/file?file=f1.txt", "/api/diff", "/api/history/file?file=f1.txt&limit=10",
    "/api/history/blame?file=f1.txt&start=1&end=10", "/api/history/tags?limit=20",
    "/api/history/reflog?limit=10", "/api/history/stashes?limit=10",
    "/api/conflict/state", "/api/conflict/list", "/api/remote/list",
    "/api/history/plan/stash",
]
WRITES = [("/api/stage", {"files": ["f2.txt"]}),
          ("/api/unstage", {"files": ["f2.txt"]})]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def sample(pid):
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, None
    try:
        hc = wt.DWORD(0)
        k.GetProcessHandleCount(h, ctypes.byref(hc))
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        return hc.value, pmc.WorkingSetSize
    finally:
        k.CloseHandle(h)


def sh(*a):
    return subprocess.run(list(a), cwd=BASE, capture_output=True)


def build():
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    sh("git", "init", "-q")
    sh("git", "config", "user.name", "bench")
    sh("git", "config", "user.email", "bench@localhost")
    for i in range(1, 4):
        with open(os.path.join(BASE, "f%d.txt" % i), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("".join("line %d\n" % j for j in range(1, 21)))
    sh("git", "add", "-A")
    sh("git", "commit", "-q", "-m", "c1")
    with open(os.path.join(BASE, "f2.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("dirty\n")
    sh("git", "add", "f2.txt")
    sh("git", "tag", "-a", "v1", "-m", "tag v1")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start():
    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.Popen([EXE, "--root", BASE, "--port", str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return p, base
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("server died: " +
                                   p.stderr.read().decode("utf-8", "replace"))
            time.sleep(0.1)
    raise RuntimeError("server slow")


def get(base, path):
    try:
        r = urllib.request.urlopen(base + path, timeout=120)
        return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(base, path, body):
    data = json.dumps(body).encode("utf-8")
    rq = urllib.request.Request(base + path, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        r = urllib.request.urlopen(rq, timeout=120)
        return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    build()
    proc, base = start()
    try:
        first_h, first_ws = sample(proc.pid)
        bad = []
        samples = []
        total = 0

        def one(i):
            n = i % (len(READS) + len(WRITES))
            if n < len(READS):
                code, body = get(base, READS[n])
            else:
                path, payload = WRITES[n - len(READS)]
                code, body = post(base, path, payload)
            if code >= 500:
                return ("5xx", code, body[:120])
            try:
                json.loads(body.decode("utf-8"))
            except Exception:
                return ("badjson", code, body[:120])
            return None

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, i) for i in range(workers * rounds)]
            for idx, f in enumerate(concurrent.futures.as_completed(futs), 1):
                total += 1
                r = f.result()
                if r:
                    bad.append(r)
                if idx % max(1, workers * rounds // 6) == 0:
                    samples.append(sample(proc.pid))
        last_h, last_ws = sample(proc.pid)
        dt = time.time() - t0
        alive = proc.poll() is None

        print("  %d 请求 / %.1fs（%.0f req/s）  异常 %d"
              % (total, dt, total / dt if dt else 0, len(bad)))
        for b in bad[:5]:
            print("    ", b)
        hs = [h for h, _ in samples if h is not None] + [h for h in (first_h, last_h) if h]
        ws = [w for _, w in samples if w]
        print("  句柄: 首=%s 末=%s 采样=%s" % (first_h, last_h, hs))
        if ws:
            print("  工作集: 首=%.1fMB 末=%.1fMB 峰值=%.1fMB"
                  % (first_ws / 1e6, last_ws / 1e6, max(ws) / 1e6))
        print("  服务存活:", "是" if alive else "否")

        fails = len(bad) + (0 if alive else 1)
        if first_h is not None and last_h is not None and last_h > first_h + 16:
            fails += 1
            print("  FAIL 句柄增长过多: %d -> %d" % (first_h, last_h))
        if first_ws and last_ws and last_ws > first_ws + 64 * 1024 * 1024:
            fails += 1
            print("  FAIL 工作集增长过多: %.1fMB" % ((last_ws - first_ws) / 1e6))
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
