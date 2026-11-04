# -*- coding: utf-8 -*-
"""M0 一致性判据：C 版 vs Python 版（同一个仓、同一时刻、同一份请求）。

判据不是「C 版能跑」，而是**两边响应逐项相等**：
  * /api/health  —— JSON 相等（含 root / isRepo）
  * /api/status  —— JSON 深比较相等（staged/unstaged/conflicted 的顺序也要一致，
                    因为它们都来自同一条 git 输出）
  * 计数头        —— X-aigit-git-calls / X-aigit-git-cmds 相等
  * 404 / 405    —— 状态码、JSON、Allow 头相等

用法：
    python -X utf8 c/tests/conformance_m0.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
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
C_ROOT = os.path.dirname(HERE)                 # ...\aigit\c
PY_ROOT = os.path.join(os.path.dirname(C_ROOT), "python-ref")              # ...\aigit
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m0_%d" % (os.getpid() % 100000))


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:200]))
    return p.stdout


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def make_repo():
    d = os.path.join(BASE, "repo")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("a\n")
    os.makedirs(os.path.join(d, "sub"))
    with open(os.path.join(d, "sub", "b.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("b\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-q", "-m", "c1")
    # 各种脏状态：改、暂存、未跟踪、删除、改名、中文+空格
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("a2\n")
    with open(os.path.join(d, "sub", "b.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("b2\n")
    must(d, "add", "--", "sub/b.txt")
    with open(os.path.join(d, "u 中文.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("u\n")
    os.remove(os.path.join(d, "sub", "b.txt"))
    must(d, "mv", "a.txt", "a3.txt")
    must(d, "branch", "feature")
    return d


def start_py(repo, port, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", AIGIT_GIT_COUNT="1")
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py"),
                          "--root", repo, "--port", str(port)],
                         env=env, cwd=PY_ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def start_c(repo, port, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", AIGIT_GIT_COUNT="1")
    env.update(env_extra or {})
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                         env=env, cwd=C_ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def wait_health(port, proc, timeout=20.0):
    base = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(base, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
    return False


def req(port, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(rq, timeout=60)
        code, payload, hdr = r.getcode(), r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, payload, hdr = e.code, e.read(), e.headers
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = None
    return {
        "code": code,
        "json": js,
        "calls": hdr.get("X-aigit-git-calls"),
        "cmds": hdr.get("X-aigit-git-cmds"),
        "allow": hdr.get("Allow"),
    }


CASES = [
    ("GET", "/api/health", True),
    ("GET", "/api/status", True),
    ("GET", "/api/status", True),          # 第二次：确认无状态泄漏
    ("GET", "/api/nope", True),
    ("POST", "/api/nope", True),
    ("PUT", "/api/status", False),
    ("DELETE", "/api/status", False),
    ("PATCH", "/api/status", False),
]


def compare_crlf():
    """CRLF 工作区 + core.autocrlf=true：两边都必须报「干净」。

    这条专门验 C 版有没有把「用户视角的 core.*」透传给 git —— 不透传就会
    把 CRLF 工作区报成已修改（Python 版曾经踩过，见 doc/aigit-perf.md §12.16）。
    """
    os.makedirs(BASE, exist_ok=True)
    cfg = os.path.join(BASE, "cfg_autocrlf")
    with open(cfg, "w", encoding="utf-8", newline="\n") as f:
        f.write("[core]\n\tautocrlf = true\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=cfg)
    d = os.path.join(BASE, "crlf")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main", env=env)
    sh(d, "config", "user.name", "bench", env=env)
    sh(d, "config", "user.email", "bench@localhost", env=env)
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("line1\nline2\n")
    sh(d, "add", "-A", env=env)
    sh(d, "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-q", "-m", "c1", env=env)
    os.remove(os.path.join(d, "a.txt"))
    sh(d, "checkout", "-f", "--", "a.txt", env=env)
    with open(os.path.join(d, "a.txt"), "rb") as f:
        raw = f.read()
    if b"\r\n" not in raw:
        print("  SKIP crlf 前置不成立（工作区没有 CRLF）")
        return 0
    pa, pb = free_port(), free_port()
    extra = {"GIT_CONFIG_GLOBAL": cfg}
    pp = start_py(d, pa, extra)
    pc = start_c(d, pb, extra)
    fails = 0
    try:
        if not (wait_health(pa, pp) and wait_health(pb, pc)):
            print("  FAIL crlf: 服务没起来")
            return 1
        a = req(pa, "GET", "/api/status")
        b = req(pb, "GET", "/api/status")
        if a["json"] == b["json"] and a["calls"] == b["calls"]:
            print("  PASS crlf(C)  status 与 Python 一致（unstaged=%d）"
                  % len(b["json"].get("unstaged") or []))
        else:
            fails += 1
            print("  FAIL crlf(C): python=%s c=%s"
                  % (json.dumps(a["json"], ensure_ascii=False)[:200],
                     json.dumps(b["json"], ensure_ascii=False)[:200]))
    finally:
        for p in (pp, pc):
            try:
                p.terminate(); p.wait(10)
            except Exception:
                try: p.kill()
                except Exception: pass
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    return fails

def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    repo = make_repo()
    pa, pb = free_port(), free_port()
    pp = start_py(repo, pa)
    pc = start_c(repo, pb)
    fails = 0
    try:
        ok_py = wait_health(pa, pp)
        ok_c = wait_health(pb, pc)
        print("  python 健康: %s   C 健康: %s" % (ok_py, ok_c))
        if not (ok_py and ok_c):
            return 1
        for method, path, compare_calls in CASES:
            a = req(pa, method, path)
            b = req(pb, method, path)
            same = (a["code"] == b["code"] and a["json"] == b["json"])
            if compare_calls:
                same = same and a["calls"] == b["calls"] and a["cmds"] == b["cmds"]
            if method in ("PUT", "DELETE", "PATCH"):
                same = same and (a["allow"] or "") == (b["allow"] or "")
            if same:
                print("  PASS %-6s %-18s code=%s calls=%s cmds=%s"
                      % (method, path, a["code"], a["calls"], a["cmds"]))
            else:
                fails += 1
                print("  FAIL %-6s %-18s" % (method, path))
                print("       python: code=%s json=%s calls=%s cmds=%s allow=%s"
                      % (a["code"], json.dumps(a["json"], ensure_ascii=False)[:220],
                         a["calls"], a["cmds"], a["allow"]))
                print("       c     : code=%s json=%s calls=%s cmds=%s allow=%s"
                      % (b["code"], json.dumps(b["json"], ensure_ascii=False)[:220],
                         b["calls"], b["cmds"], b["allow"]))
    finally:
        for p in (pp, pc):
            try:
                p.terminate()
                p.wait(10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    fails += compare_crlf()
    print("  失败 %d / %d" % (fails, len(CASES) + 1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
