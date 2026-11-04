#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""整数参数的 Unicode 数字：C 必须与 Python 同码同结果。

覆盖：阿拉伯-印度 ٥、١٢、全角 ５、扩展阿拉伯 ۱_۲（数字间下划线）。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(C_ROOT)
PY_ROOT = os.environ.get("AIGIT_PY_ROOT") or os.path.join(REPO, "python-ref")
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_uniint_%d" % (os.getpid() % 100000))

CASES = [
    "/api/history/tags?limit=" + urllib.parse.quote("\u0665"),
    "/api/history/tags?limit=" + urllib.parse.quote("\u0661\u0662"),
    "/api/log?limit=" + urllib.parse.quote("\uff15"),
    "/api/history/tags?limit=" + urllib.parse.quote("\u06f1_\u06f2"),
]


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True)


def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def build(d):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    sh("git", "init", "-q", cwd=d)
    sh("git", "config", "user.name", "t", cwd=d)
    sh("git", "config", "user.email", "t@t", cwd=d)
    for i in range(3):
        open(os.path.join(d, "f%d.txt" % i), "w").write("x\n")
        sh("git", "add", "-A", cwd=d)
        sh("git", "commit", "-q", "-m", "c%d" % i, cwd=d)
    for i in range(4):
        sh("git", "tag", "v%d" % i, cwd=d)


def start(argv, repo):
    port = free()
    p = subprocess.Popen(argv + ["--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/health"); c.getresponse().read(); c.close()
            return p, port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("slow")


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path)
    r = c.getresponse(); b = r.read(); c.close()
    try:
        return r.status, json.loads(b)
    except Exception:
        return r.status, None


def main():
    out = {}
    for name, argv in (("python", [sys.executable, "-X", "utf8",
                                   os.path.join(PY_ROOT, "server.py")]),
                       ("c", [EXE])):
        d = os.path.join(BASE, name)
        build(d)
        p, port = start(argv, d)
        try:
            rows = []
            for path in CASES:
                code, js = get(port, path)
                n = len((js or {}).get("tags") or (js or {}).get("log") or [])
                rows.append((code, n))
            out[name] = rows
            print("  %-6s %r" % (name, rows))
        finally:
            p.terminate()
            try: p.wait(timeout=5)
            except Exception: p.kill()

    fails = 0 if out.get("python") == out.get("c") else 1
    print("\n  失败 %d" % fails)
    shutil.rmtree(BASE, ignore_errors=True)
    return fails


if __name__ == "__main__":
    sys.exit(main())
