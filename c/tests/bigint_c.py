#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""超出 2^126 / int64 的整数参数：C 必须与 Python 同码同文案。

C 内部只用 __int128 / int64，Python 的 int 是任意精度。差别**只在回显**：
`requested_first`、`没有第 N 个 stash`、`end_line(...)`、以及 stash 的
`needs_confirmation` 里拼的 `?index=`。这些地方必须按用户原文的规范十进制给
（去前导零、-0 -> 0），而不是饱和后的 170141183460469231731687303715884105728
截断值。

判据：状态码 + JSON 语义（忽略 root）。
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
BASE = os.path.join(os.environ["TEMP"], "aigit_bigint_%d" % (os.getpid() % 100000))

HUGE = ["1" + "0" * 40, "1" + "0" * 126, "1" + "0" * 200, "-" + "1" + "0" * 200,
        "9223372036854775807", "9223372036854775808",
        "170141183460469231731687303715884105727",
        "170141183460469231731687303715884105728",
        "007", "-0", "+12"]

QGET = []
for v in HUGE:
    q = urllib.parse.quote(v)
    QGET += [
        "/api/log?limit=" + q,
        "/api/history/tags?limit=" + q,
        "/api/history/blame?file=a.txt&start=" + q + "&end=" + q,
        "/api/history/blame?file=a.txt&end=" + q,
        "/api/history/stash?index=" + q,
    ]

QBODY = [{"index": int(v)} for v in HUGE if v not in ("007", "-0", "+12")]
QBODY += [{"index": 1.5}, {"index": "12"}, {"index": None}]
BPOST = ["/api/history/stash/apply", "/api/history/stash/pop",
         "/api/history/stash/drop"]


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
        open(os.path.join(d, "a.txt"), "a").write("line %d\n" % i)
        sh("git", "add", "-A", cwd=d)
        sh("git", "commit", "-q", "-m", "c%d" % i, cwd=d)
    sh("git", "tag", "v1", cwd=d)


def norm(status, raw):
    try:
        js = json.loads(raw)
    except Exception:
        return (status, raw)
    if isinstance(js, dict):
        js.pop("root", None)
    return (status, json.dumps(js, ensure_ascii=False, sort_keys=True))


def run(argv, repo):
    port = free()
    p = subprocess.Popen(argv + ["--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/health"); c.getresponse().read(); c.close()
            break
        except Exception:
            time.sleep(0.1)
    out = []
    try:
        for path in QGET:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request("GET", path)
            r = c.getresponse(); b = r.read().decode("utf-8", "replace"); c.close()
            out.append(("GET " + path[:46],) + norm(r.status, b))
        for path in BPOST:
            for body in QBODY:
                data = json.dumps(body).encode()
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                c.request("POST", path, body=data,
                          headers={"Content-Type": "application/json"})
                r = c.getresponse(); b = r.read().decode("utf-8", "replace"); c.close()
                out.append(("POST " + path[5:] + " " + json.dumps(body)[:30],)
                           + norm(r.status, b))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    return out


def main():
    # 同一个仓：两次启动之间只读，sha/日期必须一致
    shared = os.path.join(BASE, "repo")
    build(shared)
    a = run([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py")], shared)
    b = run([EXE], shared)
    fails = 0
    for (n, s1, r1), (_, s2, r2) in zip(a, b):
        if s1 != s2 or r1 != r2:
            fails += 1
            print("  FAIL %s" % n)
            print("     python=%s %s" % (s1, r1[:120]))
            print("     c     =%s %s" % (s2, r2[:120]))
    print("\n  %d 例，失败 %d" % (len(a), fails))
    shutil.rmtree(BASE, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
