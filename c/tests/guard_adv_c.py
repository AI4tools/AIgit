#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Guard 对抗：仓库配置里的 filter / diff driver 不能被执行。

覆盖：直接配置、include 引入、includeIf（条件不匹配也按 Python 口径包含）、
带转义字符的子段名。判据是**行为**：这些命令若被执行会写标记文件。

不比较 Python（C 是产品实现）；只要求 C 一个标记都不产生。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(os.path.dirname(HERE), "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_guard_adv_%d" % (os.getpid() % 100000))


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
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        open(os.path.join(d, name), "w").write("base\n")
    sh("git", "add", "-A", cwd=d)
    sh("git", "commit", "-q", "-m", "base", cwd=d)

    open(os.path.join(d, ".git", "inc.cfg"), "w").write(
        '[filter "inc"]\n\tclean = cmd /c echo X>hacked_inc.txt\n')
    open(os.path.join(d, ".git", "inc2.cfg"), "w").write(
        '[diff "inc2"]\n\ttextconv = cmd /c echo X>hacked_inc2.txt\n')
    cfg = os.path.join(d, ".git", "config")
    t = open(cfg, encoding="utf-8").read()
    t += '''
[filter "evil"]
\tclean = cmd /c echo X>hacked_filter.txt
[diff "evil2"]
\ttextconv = cmd /c echo X>hacked_diff.txt
[merge "evil3"]
\tdriver = cmd /c echo X>hacked_merge.txt
[filter "a\\\\b"]
\tclean = cmd /c echo X>hacked_esc.txt
[include]
\tpath = inc.cfg
[includeIf "gitdir:C:/definitely/not/this/repo"]
\tpath = inc2.cfg
'''
    open(cfg, "w", encoding="utf-8").write(t)
    open(os.path.join(d, ".gitattributes"), "w").write(
        "a.txt filter=evil\nb.txt filter=inc\nc.txt diff=evil2\nd.txt diff=inc2\n")
    sh("git", "add", ".gitattributes", cwd=d)
    sh("git", "commit", "-q", "-m", "attrs", cwd=d)
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        open(os.path.join(d, name), "a").write("more\n")

    markers = [os.path.join(d, n) for n in
               ("hacked_filter.txt", "hacked_diff.txt", "hacked_inc.txt",
                "hacked_inc2.txt", "hacked_esc.txt", "hacked_merge.txt")]
    for m in markers:
        if os.path.exists(m):
            os.remove(m)
    return markers


def req(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse(); b = r.read(); c.close()
    return r.status, b


def main():
    d = os.path.join(BASE, "repo")
    markers = build(d)
    port = free()
    p = subprocess.Popen([EXE, "--root", d, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(80):
            try:
                req(port, "GET", "/api/health")
                break
            except Exception:
                time.sleep(0.1)
        st1, _ = req(port, "POST", "/api/stage", {"files": ["a.txt", "b.txt"]})
        st2, _ = req(port, "GET", "/api/diff?file=c.txt&staged=0")
        st3, _ = req(port, "GET", "/api/diff?file=d.txt&staged=0")
        hit = [os.path.basename(m) for m in markers if os.path.exists(m)]
        print("  stage=%s diff=%s,%s  被执行的配置: %s"
              % (st1, st2, st3, hit if hit else "无"))
        fails = 1 if hit else 0
        print("\n  失败 %d" % fails)
        return 0 if fails == 0 else 1
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
