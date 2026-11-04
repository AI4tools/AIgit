# -*- coding: utf-8 -*-
"""M1 一致性判据：/api/history/{file,blame}（C 版 vs Python 版）。

判据不是「C 版能跑」，而是**同一个仓、同一时刻、同一份请求**下：
  * HTTP 状态码相等
  * JSON 深比较相等（键名 / 类型 / null / 截断字段 / 顺序无关但值必须逐项相同）
  * 计数头 X-aigit-git-calls / X-aigit-git-cmds 相等

fixture：
  main   多提交 + 改名历史（follow=1/0）+ 中文+空格路径 + 二进制 + 大文件分页 + 目录 + 未跟踪
  clean  干净单文件仓
  empty  空仓（git init 后没有任何提交）

用法：
    python -X utf8 c/tests/conformance_histfile.py
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
C_ROOT = os.path.dirname(HERE)                 # ...\c
# Python 参考版：本任务的 C 副本在别处，参考版固定在 D:\project\aigit\python-ref（只读）。
PY_ROOT = os.environ.get("AIGIT_PY_ROOT") or r"D:\project\aigit\python-ref"
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_hist_%d" % (os.getpid() % 100000))


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a, env=None):
    p = sh(repo, *a, env=env)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:3]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def free_port():
    """一次只取一个（旧版）。保留给单端口场景，成对取用 free_ports()。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def free_ports(n):
    """同时占住 n 个端口再一起放掉 —— 逐个取会拿到同一个端口（实测踩过：
    两个服务绑同一个端口，于是「C vs Python」变成了「C vs C」，全绿是假的）。"""
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    assert len(set(ports)) == n, ports
    return ports


# ---------------------------------------------------------------- fixtures

def make_main():
    d = os.path.join(BASE, "main")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    # 本机全局配置里有 core.autocrlf=true（实测）。C 版目前拿不到用户视角的
    # core.*（git.c 的 load_semantic 把 UTF-8 当 UTF-16 转，实测 keys=0），
    # 所以这里把 autocrlf 钉成 false，让「CRLF 内容」这条用例只测 \r 转义。
    # autocrlf=true 的那一半单独放 crlfauto fixture，标 KNOWN-DIFF。
    sh(d, "config", "core.autocrlf", "false")

    # c1：建文件
    write_text(os.path.join(d, "a.txt"), "l1\nl2\nl3\n")
    write_text(os.path.join(d, "中文 空格.txt"), "中1\n中2\n")
    os.makedirs(os.path.join(d, "sub"))
    write_text(os.path.join(d, "sub", "b.txt"), "b\n")
    write_text(os.path.join(d, "big.txt"),
               "".join("line %d\n" % i for i in range(1, 301)))
    write_bytes(os.path.join(d, "bin.dat"), b"\x00\x01\x02binary\x00tail\n")
    # NUL 在 8000 字节之后：Python 的二进制检测只看前 8000 字节，所以它**不是**
    # binary_file，content 里要带 \x00（C 版曾在这里把内容截断）。
    write_bytes(os.path.join(d, "late.bin"), b"x" * 9000 + b"\x00" + b"tail\n")
    write_text(os.path.join(d, "empty.txt"), "")
    write_text(os.path.join(d, "gone.txt"), "g\n")
    write_bytes(os.path.join(d, "crlf.txt"), b"a\r\nb\r\nc\r\n")
    # 非法 UTF-8：Python 侧是 errors="replace" 解码的，C 版必须吐同样的 U+FFFD
    write_bytes(os.path.join(d, "latin.txt"), b"caf\xe9\nab\xe4\xb8\n")
    write_text(os.path.join(d, "a+b 100%.txt"), "p\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c1: base")

    # c2：改两个文件
    write_text(os.path.join(d, "a.txt"), "l1\nl2\nl3\nl4\n")
    write_text(os.path.join(d, "中文 空格.txt"), "中1\n中2改\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c2: modify")

    # c3：纯改名（R100，好让 --follow 跟得上）
    must(d, "mv", "a.txt", "renamed.txt")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c3: rename a->renamed")

    # c4：再改改名后的文件
    write_text(os.path.join(d, "renamed.txt"), "l1\nl2\nl3\nl4\nl5\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c4: modify renamed")

    # c5：中文作者 + 中文提交信息 + 只改 big.txt 的两行（让 blame 的
    # sha/author/summary/time 出现两种取值，而不是整份文件同一条提交）
    lines = ["line %d\n" % i for i in range(1, 301)]
    lines[4] = "line 5 changed\n"
    lines[9] = "line 10 changed\n"
    write_text(os.path.join(d, "big.txt"), "".join(lines))
    must(d, "add", "-A")
    must(d, "-c", "user.name=吴戈", "-c", "user.email=wu@localhost",
         "commit", "-q", "-m", "c5: 中文提交信息 with space")

    write_text(os.path.join(d, "untracked.txt"), "u\n")
    # 已跟踪但工作区删掉：file_history 报 tracked=true/exists_in_worktree=false，
    # blame 按 Python 的逻辑（`not ap.exists()`）报 no_such_file。
    os.remove(os.path.join(d, "gone.txt"))
    # 已跟踪且工作区改过（未提交）：blame 要出 "Not Committed Yet" 那条路
    write_text(os.path.join(d, "renamed.txt"), "l1\nl2 changed\nl3\nl4\nl5\nl6\n")
    return d


def make_clean():
    d = os.path.join(BASE, "clean")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    write_text(os.path.join(d, "a.txt"), "a\nb\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c1")
    return d


def make_crlfauto():
    """core.autocrlf 只在**用户级**配置里的仓：钉住「C 版拿不到用户视角 core.*」。

    Python 的 gitguard.apply_semantic 会把用户级 core.autocrlf 注入 git；C 版的
    git.c::load_semantic 实测 keys=0（把 UTF-8 字节当 UTF-16 转），于是同一份
    工作区，Python 说「干净」、C 说「Not Committed Yet」。根因在 src/git.c，
    不在 read_history.c —— 这里显式记成 KNOWN-DIFF，修好后会打 KNOWN-FIXED。

    前置：本机**用户级** git 配置里有 core.autocrlf=true，否则这条不成立（SKIP）。
    """
    g = subprocess.run(["git", "config", "--get", "core.autocrlf"], cwd=BASE,
                       capture_output=True, encoding="utf-8", errors="replace")
    if (g.stdout or "").strip().lower() != "true":
        return None
    d = os.path.join(BASE, "crlfauto")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    write_bytes(os.path.join(d, "crlf.txt"), b"a\r\nb\r\nc\r\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=bench", "-c", "user.email=bench@localhost",
         "commit", "-q", "-m", "c1")
    return d


def make_empty():
    d = os.path.join(BASE, "empty")
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    write_text(os.path.join(d, "x.txt"), "x\n")
    write_bytes(os.path.join(d, "bin.dat"), b"\x00bin\n")
    return d


# ---------------------------------------------------------------- 服务

def start_py(repo, port, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               AIGIT_GIT_COUNT="1")
    env.update(env_extra or {})
    log = open(os.path.join(BASE, "py_%d.log" % port), "wb")
    p = subprocess.Popen([sys.executable, "-X", "utf8",
                          os.path.join(PY_ROOT, "server.py"),
                          "--root", repo, "--port", str(port)],
                         env=env, cwd=PY_ROOT,
                         stdout=log, stderr=subprocess.STDOUT)
    p._log = log
    return p


def start_c(repo, port, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               AIGIT_GIT_COUNT="1")
    env.update(env_extra or {})
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_health(port, proc, timeout=40.0):
    url = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            if proc.poll() is not None:
                print("       health 失败（进程已退出 rc=%s）: %s"
                      % (proc.poll(), last))
                logp = getattr(proc, "_log", None)
                if logp:
                    try:
                        logp.flush()
                        with open(logp.name, "rb") as f:
                            print("       py log: %s"
                                  % f.read()[-800:].decode("utf-8", "replace"))
                    except Exception:
                        pass
                return False
            time.sleep(0.1)
    print("       health 超时 %ss: %s" % (timeout, last))
    return False


def req(port, path):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                method="GET")
    try:
        r = urllib.request.urlopen(rq, timeout=120)
        code, payload, hdr = r.getcode(), r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, payload, hdr = e.code, e.read(), e.headers
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = {"__unparsable__": payload[:400].decode("utf-8", "replace")}
    return {"code": code, "json": js,
            "server": hdr.get("Server"),
            "calls": hdr.get("X-aigit-git-calls"),
            "cmds": hdr.get("X-aigit-git-cmds")}


# ---------------------------------------------------------------- 用例

def q(path, **kw):
    parts = []
    for k, v in kw.items():
        if v is None:
            continue
        parts.append("%s=%s" % (k, urllib.parse.quote(str(v), safe="")))
    return path + ("?" + "&".join(parts) if parts else "")


CN = "中文 空格.txt"

MAIN_CASES = [
    # 文件历史：改名历史 + follow
    ("file renamed follow=1", q("/api/history/file", file="renamed.txt", follow=1)),
    ("file renamed follow=0", q("/api/history/file", file="renamed.txt", follow=0)),
    ("file old-name follow=1", q("/api/history/file", file="a.txt", follow=1)),
    ("file old-name follow=0", q("/api/history/file", file="a.txt", follow=0)),
    ("file follow default", q("/api/history/file", file="renamed.txt")),
    ("file follow=on", q("/api/history/file", file="renamed.txt", follow="on")),
    ("file follow=empty", q("/api/history/file", file="renamed.txt", follow="")),
    # 截断
    ("file limit=2", q("/api/history/file", file="renamed.txt", limit=2)),
    ("file limit=1", q("/api/history/file", file="renamed.txt", limit=1)),
    ("file limit=3", q("/api/history/file", file="renamed.txt", limit=3)),
    ("file limit=4", q("/api/history/file", file="renamed.txt", limit=4)),
    ("file limit=5", q("/api/history/file", file="renamed.txt", limit=5)),
    ("file limit=0 -> 1", q("/api/history/file", file="renamed.txt", limit=0)),
    ("file limit=99999", q("/api/history/file", file="renamed.txt", limit=99999)),
    ("file limit=-5", q("/api/history/file", file="renamed.txt", limit=-5)),
    # 中文 + 空格
    ("file 中文+空格", q("/api/history/file", file=CN)),
    # 其它形态
    ("file binary", q("/api/history/file", file="bin.dat")),
    ("file big", q("/api/history/file", file="big.txt")),
    ("file 不存在", q("/api/history/file", file="nope.txt")),
    ("file 空文件", q("/api/history/file", file="empty.txt")),
    ("file 工作区已删", q("/api/history/file", file="gone.txt")),
    ("file crlf", q("/api/history/file", file="crlf.txt")),
    ("file latin1", q("/api/history/file", file="latin.txt")),
    ("file +%名", q("/api/history/file", file="a+b 100%.txt")),
    ("file 重复参数", q("/api/history/file", file="renamed.txt") + "&file=nope.txt"),
    ("file 目录", q("/api/history/file", file="sub")),
    ("file 目录带斜杠", q("/api/history/file", file="sub/")),
    ("file 缺参数", "/api/history/file"),
    ("file 空串", q("/api/history/file", file="")),
    ("file limit=abc", q("/api/history/file", file="renamed.txt", limit="abc")),
    ("file limit=+3", q("/api/history/file", file="renamed.txt", limit="+3")),
    ("file limit=' 5'", q("/api/history/file", file="renamed.txt", limit=" 5")),
    ("file limit=1_0", q("/api/history/file", file="renamed.txt", limit="1_0")),
    ("file ./a.txt", q("/api/history/file", file="./renamed.txt")),
    ("file sub\\b.txt", q("/api/history/file", file="sub\\b.txt")),
    ("file renamed.txt/", q("/api/history/file", file="renamed.txt/")),
    ("file RENAMED.TXT", q("/api/history/file", file="RENAMED.TXT")),
    ("file 越界", q("/api/history/file", file="../x")),
    ("file .git", q("/api/history/file", file=".git/config")),
    # %00 路径走私：Python 的 resolve_in_root 专门挡这个（q_get 会静默截断）
    ("file NUL", "/api/history/file?file=renamed.txt%00.png"),
    ("file limit NUL", "/api/history/file?file=renamed.txt&limit=1%00"),
    ("file follow NUL", "/api/history/file?file=renamed.txt&follow=1%00"),

    # blame：分页
    ("blame big default", q("/api/history/blame", file="big.txt")),
    ("blame big limit=10", q("/api/history/blame", file="big.txt", limit=10)),
    ("blame big start=50", q("/api/history/blame", file="big.txt", start=50, limit=10)),
    ("blame big start=50 end=60", q("/api/history/blame", file="big.txt", start=50, end=60)),
    ("blame big end=5", q("/api/history/blame", file="big.txt", end=5)),
    ("blame big start=295", q("/api/history/blame", file="big.txt", start=295, limit=10)),
    ("blame big start=300", q("/api/history/blame", file="big.txt", start=300, limit=10)),
    ("blame big start=301", q("/api/history/blame", file="big.txt", start=301)),
    ("blame big start=0", q("/api/history/blame", file="big.txt", start=0, limit=3)),
    ("blame big limit=1", q("/api/history/blame", file="big.txt", limit=1)),
    ("blame big end=1000000", q("/api/history/blame", file="big.txt", end=1000000)),
    ("blame big end=0", q("/api/history/blame", file="big.txt", end=0)),
    ("blame big start=1000", q("/api/history/blame", file="big.txt", start=1000)),
    ("blame big limit=99999", q("/api/history/blame", file="big.txt", limit=99999)),
    ("blame big limit=0", q("/api/history/blame", file="big.txt", limit=0)),
    ("blame 中文+空格", q("/api/history/blame", file=CN)),
    ("blame renamed", q("/api/history/blame", file="renamed.txt")),
    ("blame 脏工作区", q("/api/history/blame", file="renamed.txt", limit=2)),
    ("blame old-name", q("/api/history/blame", file="a.txt")),
    # blame：错误
    ("blame binary", q("/api/history/blame", file="bin.dat")),
    ("blame NUL>8000", q("/api/history/blame", file="late.bin")),
    ("blame 空文件", q("/api/history/blame", file="empty.txt")),
    ("blame 工作区已删", q("/api/history/blame", file="gone.txt")),
    ("blame crlf", q("/api/history/blame", file="crlf.txt")),
    ("blame latin1", q("/api/history/blame", file="latin.txt")),
    ("blame +%名", q("/api/history/blame", file="a+b 100%.txt")),
    ("blame start=10 end=", q("/api/history/blame", file="big.txt", start=10, end="")),
    ("blame start=0 end=0", q("/api/history/blame", file="big.txt", start=0, end=0)),
    ("blame 不存在", q("/api/history/blame", file="nope.txt")),
    ("blame 目录", q("/api/history/blame", file="sub")),
    ("blame 缺参数", "/api/history/blame"),
    ("blame 空串", q("/api/history/blame", file="")),
    ("blame start=abc", q("/api/history/blame", file="big.txt", start="abc")),
    ("blame end=abc", q("/api/history/blame", file="big.txt", end="abc")),
    ("blame limit=abc", q("/api/history/blame", file="big.txt", limit="abc")),
    ("blame start=10 end=5", q("/api/history/blame", file="big.txt", start=10, end=5)),
    ("blame start=-3", q("/api/history/blame", file="big.txt", start=-3, limit=5)),
    ("blame start=10^30", q("/api/history/blame", file="big.txt", start="1" + "0" * 30)),
    ("blame limit=' 2 '", q("/api/history/blame", file="big.txt", limit=" 2 ")),
    ("blame 越界", q("/api/history/blame", file="../x")),
    ("blame NUL", "/api/history/blame?file=renamed.txt%00.png"),
    ("blame start NUL", "/api/history/blame?file=big.txt&start=1%00"),
]

CLEAN_CASES = [
    ("file a.txt", q("/api/history/file", file="a.txt")),
    ("file a.txt limit=1", q("/api/history/file", file="a.txt", limit=1)),
    ("file 不存在", q("/api/history/file", file="nope.txt")),
    ("blame a.txt", q("/api/history/blame", file="a.txt")),
    ("blame 不存在", q("/api/history/blame", file="nope.txt")),
]

EMPTY_CASES = [
    ("file 空仓", q("/api/history/file", file="x.txt")),
    ("file 空仓 不存在", q("/api/history/file", file="nope.txt")),
    ("blame 空仓", q("/api/history/blame", file="x.txt")),
    ("blame 空仓 二进制", q("/api/history/blame", file="bin.dat")),
    ("blame 空仓 不存在", q("/api/history/blame", file="nope.txt")),
]

# autocrlf=true 仓：已知差异（根因在 src/git.c::load_semantic，不在 read_history.c）
CRLFAUTO_CASES = [
    ("file crlf autocrlf=1", q("/api/history/file", file="crlf.txt")),
    ("blame crlf autocrlf=1", q("/api/history/blame", file="crlf.txt")),
]
# 原本 blame 那条会分叉（C 的 git.c 把 semantic 配置的 UTF-8 当 UTF-16，global 里的
# core.autocrlf 丢失）。2026-11-03 已修 git.c::load_semantic + 环境块同名键去重，
# 这条现在逐项相等；保留空集合，若哪天再分叉会被计成 FAIL 而不是被 KNOWN 吞掉。
KNOWN_DIFF = set()


def norm(js):
    """抹掉「Not Committed Yet」记录里的 time。

    未提交行的 author-time 是 git 在**请求时刻**取的墙钟（不是文件 mtime），
    两个请求跨秒就会不同 —— 那是判据本身的噪声，不是实现差异。
    """
    if isinstance(js, dict):
        lines = js.get("lines")
        if isinstance(lines, list):
            for ln in lines:
                if isinstance(ln, dict) and ln.get("sha") == "0" * 40:
                    ln["time"] = "<now>"
    return js


def run_repo(name, repo, cases, rows, env_extra=None):
    pa, pb = free_ports(2)
    pp = start_py(repo, pa, env_extra)
    pc = start_c(repo, pb, env_extra)
    try:
        ok_py = wait_health(pa, pp)
        ok_c = wait_health(pb, pc)
        if not (ok_py and ok_c):
            print("  FAIL %s: 服务没起来 (py=%s c=%s)" % (name, ok_py, ok_c))
            rows.append((name, "<启动>", False, "py=%s c=%s" % (ok_py, ok_c)))
            return 1
        # 防「自己跟自己比」：用响应头 Server 区分（Python 的 http.server 一定发
        # Server: BaseHTTP/...，C 版不发）。不能再用某个端点还 404 来判别 ——
        # M1 读端点全接上后，两边对所有读路径都是 200。
        sa = req(pa, "/api/health")["server"]
        sb = req(pb, "/api/health")["server"]
        if not sa or sa == sb:
            print("  FAIL %s: 判别器失效（Server 头 py=%r c=%r），"
                  "很可能两个请求打到了同一个服务" % (name, sa, sb))
            rows.append((name, "<判别器>", False, "Server 头 %r/%r" % (sa, sb)))
            return 1
        print("  %-6s 判别器 OK（Server py=%r c=%r）" % (name, sa, sb))
        fails = 0
        for label, path in cases:
            a = req(pa, path)
            b = req(pb, path)
            why = []
            if a["code"] != b["code"]:
                why.append("code py=%s c=%s" % (a["code"], b["code"]))
            if norm(a["json"]) != norm(b["json"]):
                why.append("json 不同")
            if a["calls"] != b["calls"]:
                why.append("calls py=%s c=%s" % (a["calls"], b["calls"]))
            if a["cmds"] != b["cmds"]:
                why.append("cmds py=%s c=%s" % (a["cmds"], b["cmds"]))
            ok = not why
            known = (label, path) in KNOWN_DIFF
            if known:
                if ok:
                    print("  KNOWN-FIXED %-6s %-28s（已知差异已消失，请移出 KNOWN_DIFF）"
                          % (name, label))
                else:
                    print("  KNOWN   %-6s %-28s %s" % (name, label, "; ".join(why)))
                rows.append((name, label, ok, "KNOWN: " + "; ".join(why)))
                continue
            if not ok:
                fails += 1
            rows.append((name, label, ok, "; ".join(why)))
            if not ok:
                print("  FAIL %-6s %-28s %s" % (name, label, "; ".join(why)))
                print("       python: code=%s json=%s"
                      % (a["code"], json.dumps(a["json"], ensure_ascii=False)[:600]))
                print("       c     : code=%s json=%s"
                      % (b["code"], json.dumps(b["json"], ensure_ascii=False)[:600]))
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


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    os.makedirs(BASE)
    rows = []
    fails = 0
    total = 0
    try:
        main_repo = make_main()
        clean_repo = make_clean()
        empty_repo = make_empty()
        crlf_repo = make_crlfauto()
        plan = [("main", main_repo, MAIN_CASES, None),
                ("clean", clean_repo, CLEAN_CASES, None),
                ("empty", empty_repo, EMPTY_CASES, None)]
        if crlf_repo:
            plan.append(("crlfauto", crlf_repo, CRLFAUTO_CASES, None))
        else:
            print("  SKIP   crlfauto：本机用户级配置里没有 core.autocrlf=true")
        for name, repo, cases, extra in plan:
            f = run_repo(name, repo, cases, rows, extra)
            fails += f
            total += len(cases)
            known_n = sum(1 for label, p in cases if (label, p) in KNOWN_DIFF)
            print("  %-8s %d/%d PASS%s"
                  % (name, len(cases) - f - known_n, len(cases) - known_n,
                     ("（另 %d 个 KNOWN-DIFF）" % known_n) if known_n else ""))
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    print()
    print("  端点 × fixture 汇总（PASS/FAIL/KNOWN）：")
    by = {}
    for name, label, ok, why in rows:
        key = label.split()[0]
        d = by.setdefault((name, key), [0, 0, 0])
        if why.startswith("KNOWN: "):
            d[2] += 1
        elif ok:
            d[0] += 1
        else:
            d[1] += 1
    for (name, key), (p, f, k) in sorted(by.items()):
        print("    %-8s %-6s PASS=%d FAIL=%d KNOWN=%d" % (name, key, p, f, k))
    print()
    print("  失败 %d / %d" % (fails, total))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
