# -*- coding: utf-8 -*-
"""M1 一致性判据：冲突 / 远程**只读**端点（C 版 vs Python 版）。

判据：同一个仓、同一时刻、同一份请求，两边的 **HTTP 码 + JSON 深比较**相等。
计数头（X-aigit-git-calls / cmds）**不比** —— C 版为了简单把跨文件查询拆成
逐文件 spawn，spawn 数本来就会不同（结果必须相同，次数不必）。

覆盖的端点（6 个）：
    /api/conflict/state
    /api/conflict/list
    /api/conflict/inspect?file=
    /api/remote/capability
    /api/remote/list
    /api/remote/ahead-behind?remote=&branch=

fixture：
    clean         非合并态 / 无 remote
    conflict      真冲突（含「中文 空格」路径）+ 有 remote 但无跟踪 ref
    resolved      冲突已解决（git add 过，仍在合并态）
    tracking      有 remote 且有跟踪 ref（ahead/behind 走数字分支）
    empty         未出生 HEAD（ahead-behind 走 400 git_failed）
    detached      游离 HEAD
    crlf          CRLF 工作区里的冲突（验 \\r 留在行内容里的口径）

用法：
    python -X utf8 c/tests/conformance_conflict.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)                       # ...\c
PY_ROOT = r"D:\project\aigit"                        # Python 参考版（只读）
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m1_%d" % (os.getpid() % 100000))

ENDPOINTS = [
    "/api/conflict/state",
    "/api/conflict/list",
    "/api/remote/capability",
    "/api/remote/list",
    "/api/remote/ahead-behind",
]


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def w(repo, name, text):
    path = os.path.join(repo, name)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def init_repo(d, autocrlf="false"):
    os.makedirs(d, exist_ok=True)
    must(d, "init", "-q", "-b", "main")
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    must(d, "config", "core.autocrlf", autocrlf)
    return d


def start_py(repo, port, env_extra=None, log=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(env_extra or {})
    return subprocess.Popen([sys.executable, "-X", "utf8",
                             os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT,
                            stdout=log or subprocess.DEVNULL,
                            stderr=log or subprocess.DEVNULL)


def start_c(repo, port, env_extra=None, log=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(env_extra or {})
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
                            stdout=log or subprocess.DEVNULL,
                            stderr=log or subprocess.DEVNULL)


def wait_health(port, proc, timeout=25.0):
    url = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
    return False


def req(port, path):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    try:
        r = urllib.request.urlopen(rq, timeout=90)
        code, payload = r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = {"__raw__": payload.decode("utf-8", "replace")}
    return code, js


# ---------------------------------------------------------------- fixtures

def fx_clean():
    d = init_repo(os.path.join(BASE, "clean"))
    w(d, "a.txt", "l1\nl2\nl3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "base")
    w(d, "a.txt", "l1\nl2 changed\nl3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c2")
    return d


def make_conflict(d, autocrlf="false", resolve=False):
    init_repo(d, autocrlf)
    w(d, "a.txt", "l1\nl2\nl3\nl4\nl5\n")
    w(d, "中文 空格.txt", "x1\nx2\nx3\n")
    w(d, "keep.txt", "k\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "a.txt", "l1\nl2 feature\nl3\nl4\nl5\n")
    w(d, "中文 空格.txt", "x1\nx2 feature\nx3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "feature")
    must(d, "checkout", "-q", "main")
    w(d, "a.txt", "l1\nl2 main\nl3\nl4\nl5\n")
    w(d, "中文 空格.txt", "x1\nx2 main\nx3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "main2")
    sh(d, "merge", "feature")                 # 期望冲突（rc=1）
    if resolve:
        w(d, "a.txt", "l1\nl2 merged\nl3\nl4\nl5\n")
        w(d, "中文 空格.txt", "x1\nx2 merged\nx3\n")
        must(d, "add", "--", "a.txt", "中文 空格.txt")
    return d


def fx_conflict():
    d = make_conflict(os.path.join(BASE, "conflict"))
    bare = os.path.join(BASE, "bare.git")
    must(BASE, "init", "-q", "--bare", "-b", "main", bare)
    must(d, "remote", "add", "origin", bare)
    return d


def fx_resolved():
    return make_conflict(os.path.join(BASE, "resolved"), resolve=True)


def fx_tracking():
    d = init_repo(os.path.join(BASE, "tracking"))
    w(d, "a.txt", "1\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    w(d, "a.txt", "2\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c2")
    w(d, "a.txt", "3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c3")
    bare = os.path.join(BASE, "bare2.git")
    must(BASE, "init", "-q", "--bare", "-b", "main", bare)
    must(d, "remote", "add", "origin", bare)
    # 让跟踪 ref 与 main 分叉：ahead 与 behind 同时非零
    must(d, "checkout", "-q", "-b", "side", "HEAD~2")
    w(d, "a.txt", "side\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "side")
    must(d, "update-ref", "refs/remotes/origin/main", "side")
    must(d, "checkout", "-q", "main")
    return d


def wb(repo, name, data):
    path = os.path.join(repo, name)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def fx_rich():
    """一个仓里凑齐：多冲突块 / 删除-修改 / 二进制 / add-add / 手工删标记。"""
    d = init_repo(os.path.join(BASE, "rich"))
    lines = "".join("L%d\n" % i for i in range(1, 21))
    w(d, "multi.txt", lines)
    w(d, "del.txt", "d1\nd2\nd3\n")
    wb(d, "bin.dat", b"\x00\x01base\n")
    w(d, "manual.txt", "m1\nm2\nm3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "multi.txt", lines.replace("L3\n", "L3 feature\n").replace("L15\n", "L15 feature\n"))
    os.remove(os.path.join(d, "del.txt"))
    wb(d, "bin.dat", b"\x00\x01feature\n")
    w(d, "addnew.txt", "new-feature\n")
    w(d, "manual.txt", "m1\nm2 feature\nm3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "feature")
    must(d, "checkout", "-q", "main")
    w(d, "multi.txt", lines.replace("L3\n", "L3 main\n").replace("L15\n", "L15 main\n"))
    w(d, "del.txt", "d1\nd2 main\nd3\n")
    wb(d, "bin.dat", b"\x00\x01main\n")
    w(d, "addnew.txt", "new-main\n")
    w(d, "manual.txt", "m1\nm2 main\nm3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "main2")
    sh(d, "merge", "feature")
    # 手工把标记删掉（索引仍是 U）→ manuallyEdited=true / regions 空
    w(d, "manual.txt", "m1\nm2 resolved by hand\nm3\n")
    return d


def fx_diff3():
    d = init_repo(os.path.join(BASE, "diff3"))
    must(d, "config", "merge.conflictStyle", "diff3")
    w(d, "d.txt", "a\nb\nc\nd\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "d.txt", "a\nb feature\nc\nd\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "feature")
    must(d, "checkout", "-q", "main")
    w(d, "d.txt", "a\nb main\nc\nd\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "main2")
    sh(d, "merge", "feature")
    return d


def fx_worktree():
    """链接工作区：`.git` 是**文件**，gitdir 在别处（走 rev-parse 回退）。"""
    d = init_repo(os.path.join(BASE, "wtmain"))
    w(d, "w.txt", "w1\nw2\nw3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "w.txt", "w1 feature\nw2\nw3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "feature")
    must(d, "checkout", "-q", "main")
    w(d, "w.txt", "w1 main\nw2\nw3\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "main2")
    wt = os.path.join(BASE, "wt")
    must(d, "worktree", "add", "-q", wt, "feature")
    sh(wt, "merge", "main")            # 期望冲突
    return wt


def fx_empty():
    return init_repo(os.path.join(BASE, "empty"))


def fx_detached():
    d = init_repo(os.path.join(BASE, "detached"))
    w(d, "a.txt", "1\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    must(d, "checkout", "-q", "--detach")
    return d


def fx_crlf():
    return make_conflict(os.path.join(BASE, "crlf"), autocrlf="true")


def q(s):
    return urllib.parse.quote(s, safe="")


def cases_for(tag):
    """每个 fixture 的请求清单。"""
    base = [(p, p) for p in ENDPOINTS]
    base += [
        ("/api/remote/ahead-behind?remote=origin&branch=main",
         "/api/remote/ahead-behind?remote=origin&branch=main"),
    ]
    if tag == "conflict":
        base += [
            ("/api/conflict/inspect?file=" + q("a.txt"),
             "/api/conflict/inspect?file=" + q("a.txt")),
            ("/api/conflict/inspect?file=" + q("中文 空格.txt"),
             "/api/conflict/inspect?file=" + q("中文 空格.txt")),
            # 参数错误：文案必须逐字一致
            ("/api/conflict/inspect", "/api/conflict/inspect"),
            ("/api/conflict/inspect?file=", "/api/conflict/inspect?file="),
            ("/api/conflict/inspect?file=" + q("keep.txt"),
             "/api/conflict/inspect?file=" + q("keep.txt")),
            ("/api/conflict/inspect?file=" + q("sub"),
             "/api/conflict/inspect?file=" + q("sub")),
            ("/api/conflict/inspect?file=" + q("../out.txt"),
             "/api/conflict/inspect?file=" + q("../out.txt")),
            ("/api/conflict/inspect?file=" + q(".git/config"),
             "/api/conflict/inspect?file=" + q(".git/config")),
            ("/api/remote/ahead-behind?remote=-x",
             "/api/remote/ahead-behind?remote=-x"),
            ("/api/remote/ahead-behind?remote=origin&branch=a:b",
             "/api/remote/ahead-behind?remote=origin&branch=a:b"),
            ("/api/remote/ahead-behind?remote=or%20igin",
             "/api/remote/ahead-behind?remote=or%20igin"),
            ("/api/remote/ahead-behind?branch=nope",
             "/api/remote/ahead-behind?branch=nope"),
            # 内嵌 NUL：Python 明确拒绝（路径走私防线），C 也必须同文案
            ("/api/conflict/inspect?file=a.txt%00",
             "/api/conflict/inspect?file=a.txt%00"),
            ("/api/conflict/inspect?file=a.txt%00x",
             "/api/conflict/inspect?file=a.txt%00x"),
            ("/api/conflict/inspect?file=%FF%FE",
             "/api/conflict/inspect?file=%FF%FE"),
            ("/api/remote/ahead-behind?remote=origin%00x",
             "/api/remote/ahead-behind?remote=origin%00x"),
            ("/api/remote/ahead-behind?branch=main%00x",
             "/api/remote/ahead-behind?branch=main%00x"),
        ]
    if tag == "crlf":
        base += [
            ("/api/conflict/inspect?file=" + q("a.txt"),
             "/api/conflict/inspect?file=" + q("a.txt")),
        ]
    if tag == "tracking":
        base += [
            ("/api/remote/ahead-behind?remote=origin&branch=side",
             "/api/remote/ahead-behind?remote=origin&branch=side"),
        ]
    if tag == "rich":
        for f in ("multi.txt", "del.txt", "bin.dat", "addnew.txt", "manual.txt"):
            base.append(("/api/conflict/inspect?file=" + q(f),
                         "/api/conflict/inspect?file=" + q(f)))
    if tag == "diff3":
        base.append(("/api/conflict/inspect?file=" + q("d.txt"),
                     "/api/conflict/inspect?file=" + q("d.txt")))
    if tag == "worktree":
        base.append(("/api/conflict/inspect?file=" + q("w.txt"),
                     "/api/conflict/inspect?file=" + q("w.txt")))
    return base


FIXTURES = [
    ("clean", fx_clean),
    ("conflict", fx_conflict),
    ("resolved", fx_resolved),
    ("tracking", fx_tracking),
    ("rich", fx_rich),
    ("diff3", fx_diff3),
    ("worktree", fx_worktree),
    ("empty", fx_empty),
    ("detached", fx_detached),
    ("crlf", fx_crlf),
]


def run_fixture(tag, repo, results, env_extra=None):
    pa, pb = free_port(), free_port()
    logdir = os.path.join(BASE, "logs")
    os.makedirs(logdir, exist_ok=True)
    la = open(os.path.join(logdir, "%s_py.log" % tag.replace(":", "_")), "wb")
    lb = open(os.path.join(logdir, "%s_c.log" % tag.replace(":", "_")), "wb")
    pp = start_py(repo, pa, env_extra, la)
    pc = start_c(repo, pb, env_extra, lb)
    try:
        ok_py = wait_health(pa, pp)
        ok_c = wait_health(pb, pc)
        if not (ok_py and ok_c):
            print("  FAIL %-9s 服务没起来 (py=%s c=%s)" % (tag, ok_py, ok_c))
            for path, _ in cases_for(tag):
                results[(path, tag)] = "SERVFAIL"
            return 1
        fails = 0
        for _, path in cases_for(tag):
            try:
                ca, ja = req(pa, path)
            except Exception as e:
                ca, ja = "EXC", {"__err__": str(e)}
            try:
                cb, jb = req(pb, path)
            except Exception as e:
                cb, jb = "EXC", {"__err__": str(e)}
            if ca == cb and ja == jb:
                results[(path, tag)] = "PASS"
                print("  PASS %-9s %-52s code=%s" % (tag, path, ca))
            else:
                fails += 1
                results[(path, tag)] = "FAIL"
                print("  FAIL %-9s %-52s" % (tag, path))
                print("       python: code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:400]))
                print("       c     : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:400]))
                if pc.poll() is not None:
                    print("       C 进程已退出 rc=%s（stderr 见下）" % pc.returncode)
                    try:
                        lb.flush()
                        with open(lb.name, "rb") as f:
                            print("       " + f.read().decode("utf-8", "replace")[-800:])
                    except Exception:
                        pass
        return fails
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
        la.close()
        lb.close()


def run_merge_msg_variants(results):
    """MERGE_MSG / MERGE_HEAD 的各种形态：branches / defaultMessage / mergeHead。

    两边读的是同一个文件、同一时刻，所以这是对 `_branches_from_merge_msg`
    与「只取第一个 token」这两条规则最直接的对照。
    """
    repo = make_conflict(os.path.join(BASE, "msg"))
    gd = os.path.join(repo, ".git")
    variants = [
        ("branches2", "Merge branches 'alpha' and 'beta'\n\n# Conflicts:\n"),
        ("remote", "Merge remote-tracking branch 'origin/main'\n"),
        ("commit", "Merge commit '0123456789abcdef0123456789abcdef01234567'\n"),
        ("tag", "Merge tag 'v1.0'\n"),
        ("other", "Something else entirely\n"),
        ("leadblank", "\n\n  Merge branch 'x'  \n"),
        ("crlf", "Merge branch 'y'\r\n"),
        ("quotes", "Merge branch 'it''s'\n"),
    ]
    pa, pb = free_port(), free_port()
    logdir = os.path.join(BASE, "logs")
    os.makedirs(logdir, exist_ok=True)
    la = open(os.path.join(logdir, "msg_py.log"), "wb")
    lb = open(os.path.join(logdir, "msg_c.log"), "wb")
    pp = start_py(repo, pa, None, la)
    pc = start_c(repo, pb, None, lb)
    fails = 0
    try:
        if not (wait_health(pa, pp) and wait_health(pb, pc)):
            print("  FAIL msg 服务没起来")
            results[("/api/conflict/state", "msg")] = "SERVFAIL"
            return 1
        for name, text in variants:
            with open(os.path.join(gd, "MERGE_MSG"), "w", encoding="utf-8", newline="") as f:
                f.write(text)
            ca, ja = req(pa, "/api/conflict/state")
            cb, jb = req(pb, "/api/conflict/state")
            key = "/api/conflict/state [MERGE_MSG=%s]" % name
            if ca == cb and ja == jb:
                results[(key, "msg")] = "PASS"
                print("  PASS msg       %-52s code=%s" % (key, ca))
            else:
                fails += 1
                results[(key, "msg")] = "FAIL"
                print("  FAIL msg       %-52s" % key)
                print("       python: code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:300]))
                print("       c     : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:300]))
        # MERGE_HEAD 两个 sha：mergeHead 只取第一个 token
        shas = must(repo, "rev-parse", "HEAD", "MERGE_HEAD").split()
        with open(os.path.join(gd, "MERGE_HEAD"), "w", encoding="utf-8", newline="\n") as f:
            f.write(shas[0] + "\n" + shas[1] + "\n")
        ca, ja = req(pa, "/api/conflict/state")
        cb, jb = req(pb, "/api/conflict/state")
        key = "/api/conflict/state [MERGE_HEAD=2 sha]"
        if ca == cb and ja == jb:
            results[(key, "msg")] = "PASS"
            print("  PASS msg       %-52s code=%s" % (key, ca))
        else:
            fails += 1
            results[(key, "msg")] = "FAIL"
            print("  FAIL msg       %-52s" % key)
            print("       python: code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:300]))
            print("       c     : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:300]))
        return fails
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
        la.close()
        lb.close()


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    if not os.path.isfile(os.path.join(PY_ROOT, "server.py")):
        print("  缺 Python 参考版 %s" % PY_ROOT)
        return 1
    results = {}
    fails = 0
    try:
        for tag, fn in FIXTURES:
            repo = fn()
            print("[%s] %s" % (tag, repo))
            fails += run_fixture(tag, repo, results)
        print("[msg] MERGE_MSG / MERGE_HEAD 形态")
        fails += run_merge_msg_variants(results)
        # AIGIT_NET 解析（capability 的 level/source/note）
        print("[env] AIGIT_NET=ReAd")
        fails += run_fixture("env:read", os.path.join(BASE, "clean"), results,
                             {"AIGIT_NET": "ReAd"})
        print("[env] AIGIT_NET=bogus")
        fails += run_fixture("env:bogus", os.path.join(BASE, "clean"), results,
                             {"AIGIT_NET": "bogus"})
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    # 汇总表：行 = 端点，列 = fixture
    print()
    print("汇总（行=端点, 列=fixture）")
    cols = []
    for (_, c) in results:
        if c not in cols:
            cols.append(c)
    header = "%-46s" % "endpoint"
    for c in cols:
        header += " %-9s" % c
    print(header)
    paths = []
    for (p, _) in results:
        if p not in paths:
            paths.append(p)
    for p in paths:
        row = "%-46s" % (p[:46])
        for c in cols:
            row += " %-9s" % results.get((p, c), "-")
        print(row)
    print("失败 %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
