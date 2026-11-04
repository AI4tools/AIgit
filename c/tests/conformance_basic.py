# -*- coding: utf-8 -*-
"""M1 基础读端点一致性判据：C 版 vs Python 版（同一仓、同一时刻、同一份请求）。

覆盖 4 个端点 × 6 个 fixture：
    /api/branches   /api/log   /api/file   /api/diff
    dirty（暂存+未暂存+未跟踪+删除+改名+中文空格+大文件+二进制 NUL+packed refs）
    unborn（未出生 HEAD）      detached（游离 HEAD）
    crlf（core.autocrlf=false，CRLF blob）    worktree（.git 是文件）

判据（逐条全等，不看「像不像」）：
    * HTTP 状态码
    * JSON 深比较（键名/类型/null/数组顺序）
    * X-aigit-git-calls / X-aigit-git-cmds（spawn 次数与子命令序列）

用法：
    python -X utf8 c/tests/conformance_basic.py
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
C_ROOT = os.path.dirname(HERE)                 # ...\c_agent_basic\c
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
# Python 参考版：优先找同级仓（...\aigit），否则用绝对路径
_PY_SIBLING = os.path.join(os.path.dirname(C_ROOT), "python-ref", "server.py")
PY_ROOT = os.path.dirname(_PY_SIBLING) if os.path.isfile(_PY_SIBLING) else r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_basic_%d" % (os.getpid() % 100000))


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def new_repo(name):
    d = os.path.join(BASE, name)
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    return d


def write(path, data, mode="wb"):
    with open(path, mode) as f:
        f.write(data)


# ---------------------------------------------------------------- fixtures

def fx_clean():
    d = new_repo("clean")
    write(os.path.join(d, "a.txt"), b"a\n")
    os.makedirs(os.path.join(d, "sub"))
    write(os.path.join(d, "sub", "b.txt"), b"b\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 clean")
    must(d, "branch", "dev")
    return d


def fx_dirty():
    d = new_repo("dirty")
    write(os.path.join(d, "a.txt"), b"a\nb\n")
    os.makedirs(os.path.join(d, "sub"))
    write(os.path.join(d, "sub", "b.txt"), b"b\n")
    write(os.path.join(d, "空 格.txt"), "中文\n".encode("utf-8"))
    # 二进制：NUL + 非法 UTF-8 + CRLF
    write(os.path.join(d, "bin.dat"), b"a\x00b\xff\xe0\x80\x80\r\nc")
    write(os.path.join(d, "big.bin"), b"x" * (1100 * 1024))
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=b@b", "commit", "-q",
         "-m", "c1 中文主题")
    # 暂存后又改：a.txt 同时有 staged 与 unstaged
    write(os.path.join(d, "a.txt"), b"a2\nb\n")
    must(d, "add", "--", "a.txt")
    write(os.path.join(d, "a.txt"), b"a3\nb\n")
    # 未跟踪
    write(os.path.join(d, "u.txt"), b"u\n")
    # 未跟踪 + 名字带空格：porcelain v2 的 `?` 记录路径必须取到行尾
    write(os.path.join(d, "untracked space.txt"), b"s\n")
    # 已暂存改名
    must(d, "mv", "sub/b.txt", "sub/c.txt")
    # 工作区删除（索引/HEAD 里还在）
    os.remove(os.path.join(d, "空 格.txt"))
    # 分支：普通 / 带斜杠 / 中文；先打包一部分 refs，再建一条松散的
    must(d, "branch", "feature")
    must(d, "branch", "feat/x")
    must(d, "branch", "中文分支")
    must(d, "pack-refs", "--all")
    must(d, "branch", "later")
    return d


def fx_unborn():
    d = new_repo("unborn")
    write(os.path.join(d, "x.txt"), b"x\n")
    return d


def fx_detached():
    d = new_repo("detached")
    write(os.path.join(d, "f.txt"), b"one\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    write(os.path.join(d, "f.txt"), b"two\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c2")
    must(d, "checkout", "-q", "--detach", "HEAD~1")
    write(os.path.join(d, "f.txt"), b"three\n")
    return d


def fx_crlf():
    d = new_repo("crlf")
    sh(d, "config", "core.autocrlf", "false")
    write(os.path.join(d, "crlf.txt"), b"l1\r\nl2\r\n")
    write(os.path.join(d, "plain.txt"), b"p\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    write(os.path.join(d, "crlf.txt"), b"l1\r\nl2x\r\n")
    return d


def fx_paths():
    d = new_repo("paths")
    os.makedirs(os.path.join(d, "Sub", "Dir"))
    write(os.path.join(d, "Sub", "Dir", "File.TXT"), b"x\n")
    write(os.path.join(d, "a.txt"), b"a\n")
    write(os.path.join(d, "empty.txt"), b"")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    write(os.path.join(d, "staged_new.txt"), b"s\n")
    must(d, "add", "staged_new.txt")
    must(d, "branch", "中文")
    return d


def fx_worktree():
    main = new_repo("wt_main")
    write(os.path.join(main, "f.txt"), b"one\n")
    must(main, "add", "-A")
    must(main, "commit", "-q", "-m", "c1")
    must(main, "branch", "wt")
    wt = os.path.join(BASE, "wt_linked")
    must(main, "worktree", "add", "-q", wt, "wt")
    write(os.path.join(wt, "f.txt"), b"two\n")
    write(os.path.join(wt, "new.txt"), b"n\n")
    return wt


FIXTURES = [
    ("clean", fx_clean, [
        "/api/branches",
        "/api/log",
        "/api/file?file=a.txt",
        "/api/file?file=sub/b.txt",
        "/api/diff?file=a.txt",
        "/api/diff?file=sub/b.txt",
    ]),
    ("dirty", fx_dirty, [
        "/api/branches",
        "/api/log",
        "/api/log?limit=1",
        "/api/log?limit=0",
        "/api/log?limit=99999",
        "/api/log?limit=-3",
        "/api/log?limit=abc",
        "/api/log?limit=",
        "/api/file?file=a.txt",
        "/api/file?file=u.txt",
        "/api/file?file=sub/c.txt",
        "/api/file?file=sub/b.txt",
        "/api/file?file=" + urllib.parse.quote("空 格.txt"),
        "/api/file?file=nope.txt",
        "/api/file?file=sub",
        "/api/file?file=",
        "/api/file",
        "/api/file?file=a.txt&ref=HEAD~1",
        "/api/file?file=a.txt&ref=nope",
        "/api/file?file=a.txt&ref=bad..ref",
        "/api/file?file=bin.dat",
        "/api/file?file=big.bin",
        "/api/file?file=" + urllib.parse.quote("../../etc"),
        "/api/diff?file=a.txt",
        "/api/diff?file=a.txt&staged=1",
        "/api/diff?file=a.txt&staged=yes",
        "/api/diff?file=a.txt&staged=TRUE",
        "/api/diff?file=a.txt&staged=",
        "/api/diff?file=u.txt",
        "/api/diff?file=sub",
        "/api/diff?file=nope.txt",
        "/api/diff?file=sub/b.txt",
        "/api/diff?file=" + urllib.parse.quote("空 格.txt"),
        "/api/diff?file=bin.dat",
        "/api/diff?file=big.bin",
        "/api/diff",
    ]),
    ("unborn", fx_unborn, [
        "/api/branches",
        "/api/log",
        "/api/log?limit=5",
        "/api/file?file=x.txt",
        "/api/file?file=nope.txt",
        "/api/diff?file=x.txt",
        "/api/diff?file=nope.txt",
    ]),
    ("detached", fx_detached, [
        "/api/branches",
        "/api/log",
        "/api/log?limit=1",
        "/api/file?file=f.txt",
        "/api/file?file=f.txt&ref=HEAD",
        "/api/diff?file=f.txt",
        "/api/diff?file=nope.txt",
    ]),
    ("crlf", fx_crlf, [
        "/api/branches",
        "/api/log?limit=2",
        "/api/file?file=crlf.txt",
        "/api/file?file=plain.txt",
        "/api/diff?file=crlf.txt",
        "/api/diff?file=plain.txt",
    ]),
    ("paths", fx_paths, [
        "/api/file?file=a.txt",
        "/api/file?file=A.TXT",                                   # 大小写归一
        "/api/file?file=SUB/DIR/FILE.TXT",
        "/api/file?file=sub%5Cdir%5Cfile.txt",                    # 反斜杠分隔
        "/api/file?file=empty.txt",                               # 空文件：old=null, new=""
        "/api/file?file=staged_new.txt",                          # 只在暂存区
        "/api/file?file=.git%2Fconfig",                           # .git 拒绝
        "/api/file?file=%FF",                                     # 非法 UTF-8 路径
        "/api/file?file=a.txt&ref=",                              # 空 ref -> HEAD
        "/api/file?file=a.txt&ref=%40%7B-1%7D",                   # @{-1}
        "/api/file?file=a.txt&ref=" + urllib.parse.quote("中文"),
        "/api/file?file=a.txt&ref=-x",
        "/api/file?file=a.txt&ref=a%3Ab",
        "/api/file?file=a.txt&file=b.txt",                        # 重复参数取第一个
        "/api/file?file=a.txt%00x",                               # NUL 路径拒绝
        "/api/file?file=%00",
        "/api/file?file=.",                                       # 根目录自己
        "/api/file?file=a.txt&ref=HEAD%00x",
        "/api/file?file=a.txt&ref=" + urllib.parse.quote("。"),   # 非 ASCII 标点不是 \w
        "/api/file?file=a.txt&ref=a%20b",
        "/api/diff?file=a.txt%00x",
        "/api/diff?file=A.TXT",
        "/api/diff?file=empty.txt",
        "/api/diff?file=staged_new.txt",
        "/api/diff?file=.git%2Fconfig",
        "/api/diff?file=a.txt&staged=on",                         # 只认 1/true/yes
        "/api/log?limit=1_0",                                     # Python int() 认下划线
        "/api/log?limit=%2B5",
        "/api/log?limit=%205%20",
        "/api/log?limit=99999999999999999999999999",
    ]),
    ("worktree", fx_worktree, [
        "/api/branches",
        "/api/log",
        "/api/file?file=f.txt",
        "/api/file?file=new.txt",
        "/api/diff?file=f.txt",
        "/api/diff?file=nope.txt",
    ]),
]


# ---------------------------------------------------------------- 服务

def start_py(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", AIGIT_GIT_COUNT="1")
    return subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", AIGIT_GIT_COUNT="1")
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def req(port, path, tries=3):
    last = None
    for attempt in range(tries):
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="GET")
            try:
                r = urllib.request.urlopen(rq, timeout=120)
                code, payload, hdr = r.getcode(), r.read(), r.headers
            except urllib.error.HTTPError as e:
                code, payload, hdr = e.code, e.read(), e.headers
            try:
                js = json.loads(payload.decode("utf-8"))
            except Exception as e:
                js = ("<<not json>>", str(e), payload[:120].decode("utf-8", "replace"))
            return {
                "code": code,
                "json": js,
                "calls": hdr.get("X-aigit-git-calls"),
                "cmds": hdr.get("X-aigit-git-cmds"),
            }
        except Exception as e:            # 连接被重置 / 超时：重试一次再判
            last = e
            time.sleep(0.2)
    return {"code": "EXC", "json": repr(last), "calls": None, "cmds": None}


def stop(p):
    try:
        p.terminate()
        p.wait(15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 比较

def short(js, n=300):
    s = json.dumps(js, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "...(len=%d)" % len(s)


def endpoint_of(path):
    return path.split("?")[0].rsplit("/", 1)[-1]


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    print("  Python 参考版: %s" % PY_ROOT)
    print("  C 版 exe     : %s" % EXE)

    # endpoint -> fixture -> [pass, fail]
    matrix = {}
    fails_total = 0
    cases_total = 0
    try:
        for fname, builder, cases in FIXTURES:
            repo = builder()
            pa, pb = free_port(), free_port()
            while pb == pa:
                pb = free_port()
            pp = start_py(repo, pa)
            pc = start_c(repo, pb)
            try:
                ok_py = wait_health(pa, pp)
                ok_c = wait_health(pb, pc)
                if not (ok_py and ok_c):
                    print("  FAIL [%s] 服务没起来 (python=%s c=%s)" % (fname, ok_py, ok_c))
                    fails_total += len(cases)
                    cases_total += len(cases)
                    continue
                print("\n=== fixture: %s (%s)" % (fname, repo))
                for path in cases:
                    ep = endpoint_of(path)
                    cell = matrix.setdefault(ep, {}).setdefault(fname, [0, 0])
                    a = req(pa, path)
                    b = req(pb, path)
                    same = (a["code"] == b["code"] and a["json"] == b["json"] and
                            a["calls"] == b["calls"] and a["cmds"] == b["cmds"])
                    cases_total += 1
                    if same:
                        cell[0] += 1
                        print("  PASS %-52s code=%s calls=%s cmds=%s"
                              % (path, a["code"], a["calls"], a["cmds"]))
                    else:
                        cell[1] += 1
                        fails_total += 1
                        print("  FAIL %-52s" % path)
                        print("       python: code=%s calls=%s cmds=%s json=%s"
                              % (a["code"], a["calls"], a["cmds"], short(a["json"])))
                        print("       c     : code=%s calls=%s cmds=%s json=%s"
                              % (b["code"], b["calls"], b["cmds"], short(b["json"])))
            finally:
                stop(pp)
                stop(pc)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    # 矩阵
    eps = ["branches", "log", "file", "diff"]
    print("\n=== PASS/FAIL 矩阵（端点 × fixture）")
    header = "  %-10s" % "endpoint"
    for fname, _, _ in FIXTURES:
        header += " %-12s" % fname
    header += " %s" % "合计"
    print(header)
    for ep in eps:
        row = "  %-10s" % ("/" + ep)
        tp = tf = 0
        for fname, _, _ in FIXTURES:
            p_, f_ = matrix.get(ep, {}).get(fname, [0, 0])
            tp += p_
            tf += f_
            row += " %-12s" % ("%dP/%dF" % (p_, f_) if (p_ + f_) else "-")
        row += " %d/%d" % (tp, tp + tf)
        print(row)
    print("\n  失败 %d / %d" % (fails_total, cases_total))
    return 1 if fails_total else 0


if __name__ == "__main__":
    sys.exit(main())
