#!/usr/bin/env python3
"""aigit 无头对抗性测试套件 —— 后端(server.py) + 生成器(gen.py)。

设计立场:
    这不是一份「证明 aigit 没问题」的测试。这是一份**试图把它打穿**的测试。
    所以本文件里的断言全部写的是**正确行为**;一旦 aigit 有缺陷,测试就该红。
    「全绿」在这里是可疑信号,不是好消息。

跑法:
    python test_aigit.py            # 全部
    python test_aigit.py -v         # 逐条显示细节
    python test_aigit.py -k merge   # 只跑名字含 merge 的
    python test_aigit.py -k B -v     # 只跑 B 组(路径越界/安全)

硬约束:
    * 纯标准库,不用 pytest。原因:这工具要在别人机器上直接跑,`pip install` 本身就是失败原因。
    * **完全无头**:不开浏览器、不开任何窗口、不截图。HTTP 走 urllib.request,子进程输出重定向到文件。
    * 所有测试仓建在 %TEMP%\\aigit-test\\ 下,绝不碰 C:\\poject\\ 下任何真实仓库。
    * 只读 server.py / gen.py。发现 bug 只记录、不修。

分组:
    A  HTTP 接口契约      B  路径越界(安全)   C  破坏性操作护栏
    D  生成器             E  与 git 的一致性   X  额外对抗测试
"""

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

AIGIT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(AIGIT, "server.py")
GEN_PY = os.path.join(AIGIT, "gen.py")
# 临时仓根**必须带进程号**。这不是洁癖: `main()` 开头会 `shutil.rmtree(TESTROOT)`
# 把整棵目录删掉, 而这个路径原来是全局固定的 `%TEMP%\aigit-test` ——
# 于是两个 `test_aigit.py` 同时跑, 后启动的那个会把前一个**正在用**的仓删光。
# 症状不是「测试失败」, 是**看起来像产品缺陷的假 FAIL**:
#   * `/api/gen` 返 400, error='仓里还没有任何提交(HEAD 不存在)...'
#   * 直接打 gen.py 的用例报 `fatal: not a git repository`(它自己的仓被删了)
#   * 上一遍还绿的用例这一遍变红(被误伤, 不是回归)
# 也就是说修好之前, 任何「N/55」都只在别的线静止时才是真的。
# `test_aigit_extra.py:79` 早就为此改用了 `aigit-test-extra`; 这里补上 pid。
TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-test-%d" % os.getpid())

GIT_ENV = dict(os.environ)
GIT_ENV.update({
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": os.environ.get("USERPROFILE", os.environ.get("HOME", "")),
})


# ════════════════════════════════════════════════════════ 测试登记

TESTS = []


def test(group, name, bug="", note=""):
    """登记一条测试。bug 填发现编号,便于报告 1:1 对应。"""
    def deco(fn):
        TESTS.append({"group": group, "name": name, "fn": fn,
                      "bug": bug, "note": note, "id": "%s-%s" % (group, name)})
        return fn
    return deco


class Skip(Exception):
    pass


class Fail(Exception):
    pass


def ck(cond, msg):
    if not cond:
        raise Fail(msg)


def ck_eq(got, want, what):
    if got != want:
        raise Fail("%s: 期望 %r,实际 %r" % (what, want, got))


# ════════════════════════════════════════════════════════ 测试仓 + 无头服务

class TestRepo:
    """%TEMP% 下的独立 git 仓。"""

    def __init__(self, name):
        self.dir = os.path.join(TESTROOT, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "aigit-test@localhost")
        self.git("config", "user.name", "aigit-test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, **kw):
        check = kw.get("check", False)
        p = subprocess.run(["git", "-C", self.dir] + list(args),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=120)
        if check and p.returncode != 0:
            raise RuntimeError("git %s 失败: %s" % (" ".join(args),
                                                  (p.stderr or p.stdout).strip()))
        return p

    def out(self, *args):
        return self.git(*args).stdout

    def write(self, rel, text):
        p = os.path.join(self.dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def read(self, rel):
        p = os.path.join(self.dir, rel.replace("/", os.sep))
        if not os.path.exists(p):
            return None
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.dir, rel.replace("/", os.sep)))

    def commit(self, msg, *extra):
        self.git("add", "-A")
        self.git("commit", "-m", msg, *extra)

    def server(self):
        return HeadlessServer(self)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HeadlessServer:
    """起一个真的 server.py 子进程,用 urllib 打。输出落文件,不用 PIPE(免得写满死锁)。"""

    def __init__(self, repo):
        self.repo = repo
        self.log = os.path.join(repo.dir + ".srvlog")
        self.port = None
        self.proc = None
        for _ in range(6):
            port = free_port()
            lf = io.open(self.log, "w", encoding="utf-8", errors="replace")
            p = subprocess.Popen(
                [sys.executable, SERVER_PY, "--root", repo.dir, "--port", str(port)],
                stdout=lf, stderr=subprocess.STDOUT, cwd=AIGIT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lf.close()
            if self._wait(port, 8.0):
                self.port = port
                self.proc = p
                return
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        raise RuntimeError("server 起不来,日志: " + self.tail())

    def _wait(self, port, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                code, body = self._raw(port, "GET", "/api/health")
                if code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def tail(self, n=25):
        try:
            with io.open(self.log, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except Exception:
            return "(无日志)"

    # -- HTTP ------------------------------------------------------------
    @staticmethod
    def _url(port, path):
        return "http://127.0.0.1:%d%s" % (port, path)

    def _raw(self, port, method, path, body=None, ctype="application/json",
             headers=None, timeout=180, with_headers=False):
        """默认回 (code, body_str); `with_headers=True` 时回 (code, body_str, headers)。

        加这个开关而不是新开一个方法: 50+ 处调用点都按 (code, body) 解包,
        改返回值形状就得全改一遍, 而多带一个头只在查静态文件时才有意义。
        """
        data = body
        if isinstance(data, str):
            data = data.encode("utf-8")
        req = urllib.request.Request(self._url(port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
                return (r.status, txt, dict(r.headers)) if with_headers \
                    else (r.status, txt)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            return (e.code, txt, dict(e.headers)) if with_headers else (e.code, txt)

    def raw(self, method, path, body=None, **kw):
        return self._raw(self.port, method, path, body, **kw)

    def jget(self, path):
        code, txt = self.raw("GET", path)
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("GET %s 返回非 JSON (code=%s): %r" % (path, code, txt[:200]))

    def jpost(self, path, obj):
        code, txt = self.raw("POST", path, json.dumps(obj))
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("POST %s 返回非 JSON (code=%s): %r" % (path, code, txt[:200]))

    def post_raw(self, path, raw_body, **kw):
        return self.raw("POST", path, raw_body, **kw)

    def alive(self):
        code, j = self.jget("/api/health")
        return code == 200 and j.get("ok") is True

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            os.remove(self.log)
        except Exception:
            pass


def outside_marker(tag):
    """在仓库外放一个哨兵文件。越界成功 = 哨兵被删/被改。"""
    base = os.path.join(TESTROOT, "_outside")
    os.makedirs(base, exist_ok=True)
    p = os.path.join(base, tag + ".canary")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write("CANARY:" + tag)
    return p


def outside_intact(p):
    """哨兵是否还在且内容没变。

    注意别用 basename[:-6] 之类的定长截断: ".canary" 是 7 个字符, 会算错。
    """
    if not os.path.exists(p):
        return False
    tag = os.path.basename(p).rsplit(".", 1)[0]
    with io.open(p, "r", encoding="utf-8") as f:
        return f.read() == "CANARY:" + tag


# ════════════════════════════════════════════════════════ A 组:HTTP 契约

@test("A", "get-endpoints-ok", bug="A1")
def t_a1(ctx):
    """A1 每个 GET 接口返回 JSON 且 ok=true。"""
    r, s = ctx.repo, ctx.srv
    r.write("a.txt", "hello\n")
    r.commit("c1")
    with io.open(os.path.join(r.dir, "a.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("dirty\n")
    checks = [
        ("/api/health", None),
        ("/api/status", None),
        ("/api/log?limit=5", None),
        ("/api/branches", None),
        ("/api/diff?file=a.txt", None),
        ("/api/file?file=a.txt", None),
    ]
    for path, _ in checks:
        code, j = ctx.srv.jget(path)
        ck_eq(code, 200, "GET %s 状态码" % path)
        ck(j.get("ok") is True, "GET %s ok!=true, 返回=%r" % (path, j))
        ctx.note("GET %-24s ok=true" % path)


@test("A", "index-html", bug="A2-web")
def t_a1b(ctx):
    """A1 GET / 应返回前端页面。web/ 目录在仓库里存在吗?"""
    code, txt = ctx.srv.raw("GET", "/")
    ctx.note("GET / -> code=%s, body 前 120 字=%r" % (code, txt[:120]))
    ck_eq(code, 200, "GET / 状态码")
    ck("<html" in txt.lower() or "<!doctype" in txt.lower(),
       "GET / 返回的不是 HTML, 而是 %r" % txt[:120])


@test("A", "post-endpoints-ok", bug="A3")
def t_a2(ctx):
    """A2 每个 POST 接口返回 JSON 且 ok=true。"""
    r, s = ctx.repo, ctx.srv
    r.write("a.txt", "x\n")
    r.commit("c1")
    r.write("b.txt", "y\n")
    for path, payload in [
        ("/api/stage", {"files": ["b.txt"]}),
        ("/api/unstage", {"files": ["b.txt"]}),
        ("/api/branch", {"name": "feat/probe"}),
        ("/api/checkout", {"name": "main"}),
        ("/api/merge-abort", {}),
    ]:
        code, j = ctx.srv.jpost(path, payload)
        ck_eq(code, 200, "POST %s 状态码" % path)
        ck(j.get("ok") is True, "POST %s ok!=true, 返回=%r" % (path, j))
        ctx.note("POST %-18s ok=true" % path)
    code, j = ctx.srv.jpost("/api/reset", {})
    ck_eq(code, 200, "POST /api/reset 状态码")
    ck(j.get("ok") is True, "POST /api/reset ok!=true")


@test("A", "get-404-not-500", bug="A4")
def t_a3(ctx):
    """A3 不存在的 GET 接口返回 404, 不是 500 堆栈。"""
    for path in ["/api/nope", "/api/status/extra", "/api/", "/nope.txt"]:
        code, txt = ctx.srv.raw("GET", path)
        ck_eq(code, 404, "GET %s 状态码" % path)
        ck("Traceback" not in txt, "GET %s 泄露了堆栈" % path)
        try:
            j = json.loads(txt)
            ck(j.get("ok") is False, "GET %s 的 404 响应 ok 字段=%r" % (path, j.get("ok")))
        except Exception:
            raise Fail("GET %s 的 404 响应不是 JSON: %r" % (path, txt[:120]))
        ctx.note("GET %-20s -> 404 JSON" % path)
    ck(ctx.srv.alive(), "404 之后服务不健康")


@test("A", "post-404-not-500", bug="A4")
def t_a3b(ctx):
    """A3 不存在的 POST 接口返回 404。"""
    for path in ["/api/nope", "/nope"]:
        code, txt = ctx.srv.raw("POST", path, "{}")
        ck_eq(code, 404, "POST %s 状态码" % path)
        ck("Traceback" not in txt, "POST %s 泄露了堆栈" % path)
    ck(ctx.srv.alive(), "404 之后服务不健康")


@test("A", "bad-json-body", bug="A5")
def t_a4(ctx):
    """A4 非法 JSON 请求体不把服务搞崩。"""
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    for body in [b"{not json", b"[1,2,3", b"", b"\xff\xfe\x00binary",
                 b'"just a string"', b"123", b"null"]:
        code, txt = ctx.srv.raw("POST", "/api/stage", body)
        ctx.note("body=%-22r -> code=%s" % (body[:18], code))
        ck(code in (200, 400), "非法 JSON 得到状态码 %s, 期望 400(它是坏请求)" % code)
        ck(ctx.srv.alive(), "发完 %r 之后服务不健康" % body[:18])
    ck(ctx.srv.alive(), "最终服务仍健康")


@test("A", "huge-body", bug="A6")
def t_a5(ctx):
    """A5 超长请求体不把服务搞崩。"""
    for n in (200 * 1024, 4 * 1024 * 1024):
        payload = json.dumps({"files": ["a.txt"], "pad": "x" * n})
        code, txt = ctx.srv.raw("POST", "/api/stage", payload, timeout=60)
        ctx.note("请求体 %8d 字节 -> code=%s" % (len(payload), code))
        ck(code in (200, 400), "超长请求体得到 %s" % code)
        ck(ctx.srv.alive(), "发完 %d 字节后服务不健康" % len(payload))


@test("A", "lying-content-length", bug="A7")
def t_a5b(ctx):
    """A5b Content-Length 撒谎(声明 5MB 但只发 10 字节)会怎样。"""
    import socket as _s
    s = _s.create_connection(("127.0.0.1", ctx.srv.port), timeout=10)
    s.sendall(b"POST /api/stage HTTP/1.1\r\nHost: x\r\n"
              b"Content-Type: application/json\r\n"
              b"Content-Length: 5000000\r\n\r\n{\"a\":1")
    s.settimeout(6)
    try:
        data = s.recv(4096)
        got = data[:60]
    except Exception as e:
        got = ("recv 超时/异常: %s" % type(e).__name__).encode()
    finally:
        s.close()
    ctx.note("撒谎的 Content-Length -> 服务端首个响应: %r" % got)
    # 服务本身(别的连接)必须还能用
    ck(ctx.srv.alive(), "撒谎的 Content-Length 把服务卡死了")


@test("A", "wrong-types-in-body", bug="A8")
def t_a6(ctx):
    """A6 body 里字段类型不对(数字当路径 / null 当 message)应给 400 而不是 500。"""
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    cases = [
        ("/api/stage", {"files": [123]}),
        ("/api/stage", {"files": "notalist"}),
        ("/api/stage", {"files": None}),
        ("/api/discard", {"files": [None]}),
        ("/api/commit", {"message": None}),
        ("/api/branch", {"name": 12345}),
        ("/api/checkout", {"name": ["a", "b"]}),
        ("/api/gen", {"count": "notanint"}),
        ("/api/gen", {"kind": 123}),
    ]
    for path, payload in cases:
        code, txt = ctx.srv.raw("POST", path, json.dumps(payload))
        ctx.note("POST %-14s %-30s -> %s" % (path, json.dumps(payload)[:30], code))
        ck(code != 500, "POST %s %r 返回 500: %s" % (path, payload, txt[:160]))
        ck(ctx.srv.alive(), "之后服务不健康")


@test("A", "query-param-garbage", bug="A9")
def t_a7(ctx):
    """A7 查询参数是垃圾时返回 400 而不是 500。"""
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    for path in ["/api/log?limit=abc", "/api/log?limit=-5", "/api/log?limit=1e99",
                 "/api/log?limit=", "/api/diff?staged=maybe&file=a.txt",
                 "/api/file?file=a.txt&ref=@@@badref@@@"]:
        code, txt = ctx.srv.raw("GET", path)
        ctx.note("GET %-42s -> %s" % (path, code))
        ck(code != 500, "GET %s 返回 500: %s" % (path, txt[:160]))


# ════════════════════════════════════════════════════════ B 组:路径越界

def _reject(ctx, path, files, tag, what):
    """POST 一个含 files 的破坏性接口, 要求被拒且外部哨兵不动。"""
    canary = outside_marker(tag)
    code, txt = ctx.srv.raw("POST", path, json.dumps({"files": files}))
    ctx.note("%s %s -> code=%s  %s" % (what, json.dumps(files, ensure_ascii=False)[:44],
                                       code, txt[:90].replace("\n", " ")))
    ck_eq(code, 400, "%s %r 期望 400(拒绝)" % (what, files))
    ck(outside_intact(canary), "哨兵被动了 —— 越界成功")
    ck(ctx.srv.alive(), "之后服务不健康")
    try:
        j = json.loads(txt)
        ck(j.get("ok") is False, "拒绝时 ok 应为 false,实际 %r" % j.get("ok"))
    except Exception:
        raise Fail("拒绝响应不是 JSON: %r" % txt[:120])


@test("B", "dotdot-slash", bug="B1")
def t_b1(ctx):
    """B1 file='../../../Windows/System32/whatever' 必须拒绝。"""
    _reject(ctx, "/api/discard", ["../../../Windows/System32/whatever"], "b1", "discard")
    _reject(ctx, "/api/stage", ["../../../Windows/System32/whatever"], "b1s", "stage")
    code, txt = ctx.srv.raw("GET", "/api/diff?file=../../../Windows/System32/whatever")
    ck_eq(code, 400, "GET /api/diff 越界应 400")


@test("B", "backslash-traversal", bug="B2")
def t_b2(ctx):
    """B2 file='..\\..\\secrets.txt' 必须拒绝(Windows 上 \\ 也是分隔符)。"""
    _reject(ctx, "/api/discard", ["..\\..\\secrets.txt"], "b2", "discard")
    _reject(ctx, "/api/stage", ["..\\..\\secrets.txt"], "b2s", "stage")
    code, _ = ctx.srv.jget("/api/file?file=" + urllib.parse.quote("..\\..\\secrets.txt"))
    ck_eq(code, 400, "GET /api/file 反斜杠越界应 400")


@test("B", "empty-path", bug="B3-crit")
def t_b3(ctx):
    """B3 file='' 必须拒绝。

    这一条最危险: resolve_in_root('') 会解析成 repo root 本身,
    而 discard 对目录会 shutil.rmtree —— 也就是「丢弃一个空文件名」= 整个仓没了。
    """
    repo_gone = not os.path.exists(ctx.repo.dir)
    ctx.note("发请求前 repo 存在? %s" % (not repo_gone))
    canary = outside_marker("b3")
    code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": [""]}))
    ctx.note("POST /api/discard {\"files\": [\"\"]} -> code=%s  %s" % (code, txt[:120]))
    still = os.path.exists(ctx.repo.dir)
    ctx.note("请求后 repo 目录还在? %s" % still)
    ck_eq(code, 400, "discard 空文件名期望 400(拒绝)")
    ck(still, "repo 目录消失了 —— discard('') 整个 rmtree 掉了仓库!灾难级")
    ck(outside_intact(canary), "哨兵被动了")
    ck(os.path.exists(os.path.join(ctx.repo.dir, ".git")),
       ".git 没了 —— 仓库被摧毁")


@test("B", "dot-path", bug="B3b-crit")
def t_b3b(ctx):
    """B3b file='.' 同样解析成 repo root, discard 会 rmtree 整个仓。"""
    canary = outside_marker("b3b")
    code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": ["."]}))
    ctx.note("POST /api/discard {\"files\": [\".\"]} -> code=%s  %s" % (code, txt[:120]))
    ck_eq(code, 400, "discard '.' 期望 400")
    ck(os.path.exists(os.path.join(ctx.repo.dir, ".git")), ".git 没了 —— 仓库被摧毁")
    ck(outside_intact(canary), "哨兵被动了")


@test("B", "absolute-posix", bug="B4")
def t_b4(ctx):
    """B4 file='/etc/passwd' 必须拒绝。"""
    _reject(ctx, "/api/discard", ["/etc/passwd"], "b4", "discard")
    _reject(ctx, "/api/stage", ["/etc/passwd"], "b4s", "stage")


@test("B", "absolute-windows", bug="B5")
def t_b5(ctx):
    """B5 绝对路径塞进去必须拒绝(两种写法)。"""
    for p in ["C:\\Windows\\System32\\drivers\\etc\\hosts",
              "C:/Windows/System32/drivers/etc/hosts",
              "\\\\?\\C:\\Windows\\System32",
              "\\\\localhost\\c$\\Windows"]:
        _reject(ctx, "/api/discard", [p], "b5", "discard")


@test("B", "symlink-escape", bug="B6-crit")
def t_b6(ctx):
    """B6 仓库内一个指向仓库外的符号链接/目录联接, 必须被拒绝。

    Windows 上 os.symlink 需要特权, 退化用 mklink /J 造目录联接。
    两条都造不出来时 SKIP, 并明确写清原因 —— 不能假装测过。
    """
    outside = os.path.join(TESTROOT, "_outside_target")
    shutil.rmtree(outside, ignore_errors=True)
    os.makedirs(outside, exist_ok=True)
    secret = os.path.join(outside, "secret.txt")
    with io.open(secret, "w", encoding="utf-8") as f:
        f.write("TOP SECRET\n")
    link = os.path.join(ctx.repo.dir, "escape")
    made = None
    try:
        os.symlink(outside, link)
        made = "symlink"
    except OSError:
        p = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                           capture_output=True, timeout=30)
        if os.path.exists(link):
            made = "junction"
        else:
            ctx.note("symlink 造不出来: %s" % p.stderr.decode("utf-8", "replace")[:120])
    if not made:
        raise Skip("本机无法创建符号链接/目录联接(os.symlink 需要 SeCreateSymbolicLinkPrivilege), "
                   "B6 未能实测")

    # 确认 pathlib 确实会跟着它解析出去 —— 否则这条测试本身没意义
    import pathlib
    resolved = str(pathlib.Path(link).resolve())
    follows = os.path.normcase(os.path.realpath(outside)) in os.path.normcase(resolved).replace("/", "\\")
    ctx.note("链接类型=%s  pathlib.resolve -> %s  会跟随=%s"
             % (made, resolved.replace(ctx.repo.dir, "<ROOT>"), follows))
    ck(follows, "造出来的链接 pathlib 不跟随, 这条测不出东西(请在报告里注明)")

    canary = outside_marker("b6")
    for files in (["escape/secret.txt"], ["escape"], ["escape/"], ["./escape/secret.txt"]):
        code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": files}))
        ctx.note("discard %-24s -> %-4s %s"
                 % (files, code, txt[:80].replace("\n", " ")))
        ck_eq(code, 400, "链接越界 %r 期望 400" % files)
        ck(os.path.exists(secret), "仓库外的 secret.txt 被删了 —— 链接越界成功")
    # stage 同样不能跟出去
    for files in (["escape/secret.txt"], ["escape"]):
        code, txt = ctx.srv.raw("POST", "/api/stage", json.dumps({"files": files}))
        ctx.note("stage   %-24s -> %-4s %s"
                 % (files, code, txt[:80].replace("\n", " ")))
        ck_eq(code, 400, "stage 链接越界 %r 期望 400" % files)
    # /api/file 不能把仓库外的内容读出来
    code, txt = ctx.srv.raw("GET", "/api/file?file=" +
                            urllib.parse.quote("escape/secret.txt"))
    ctx.note("GET /api/file escape/secret.txt -> %s %r" % (code, txt[:120]))
    ck("TOP SECRET" not in txt, "/api/file 读出了仓库外的内容 —— 链接越界成功")
    ck_eq(code, 400, "/api/file 链接越界期望 400")
    ck(outside_intact(canary), "哨兵被动了")


@test("B", "unicode-normalization-bypass", bug="B7-crit")
def t_b7(ctx):
    """B7 大小写/Unicode 归一化绕过(如全角 ..／..)必须拒绝。

    注意: 全角字符对操作系统不是分隔符, 所以「接受」本身不等于越界。
    真正的判据是: 接受之后有没有可能读写到 root 之外的东西。
    """
    import pathlib
    root = pathlib.Path(ctx.repo.dir).resolve()
    vectors = [
        "..／..",          # 全角斜杠
        "．．",             # 全角句点
        "．．／．．／etc",
        "‥/‥",             # TWO DOT LEADER
        "．．/secrets.txt",
        "..∕..",           # DIVISION SLASH
        "..%2f..",         # URL 编码(未经解码, 当普通名)
        "..／．．",
    ]
    escaped = []
    for v in vectors:
        try:
            p = (root / v).resolve()
            inside = True
            try:
                p.relative_to(root)
            except ValueError:
                inside = False
            resolved = str(p)
            if not inside:
                escaped.append((v, resolved))
            ctx.note("%-16r -> %s" % (v, "接受(%s)" % resolved.replace(str(root), "<ROOT>")
                                      if inside else "**解析到 root 之外** %s" % resolved))
        except Exception as e:
            ctx.note("%-16r -> 异常 %s: %s" % (v, type(e).__name__, e))
    ck(not escaped, "这些输入解析到了 root 之外: %r" % escaped)

    # 「被接受」不等于「越界」。这里要钉死的是: 接受之后有没有真的碰到 root 之外的东西。
    outside = os.path.join(TESTROOT, "_b7_outside")
    shutil.rmtree(outside, ignore_errors=True)
    os.makedirs(outside, exist_ok=True)
    secret = os.path.join(outside, "secret.txt")
    with io.open(secret, "w", encoding="utf-8") as f:
        f.write("TOPSECRET-B7\n")
    # 把仓库根也复制一份到 outside 之外, 用来确认路径没被解析到奇怪的地方
    canary = outside_marker("b7")
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")

    for v in vectors:
        for method, path, body in (
                ("GET", "/api/diff?file=", None),
                ("GET", "/api/file?file=", None),
                ("POST", "/api/discard", json.dumps({"files": [v]})),
                ("POST", "/api/stage", json.dumps({"files": [v]}))):
            if method == "GET":
                code, txt = ctx.srv.raw(method, path + urllib.parse.quote(v))
            else:
                code, txt = ctx.srv.raw(method, path, body)
            ck(code != 500, "%s %r -> 500: %s" % (method, v, txt[:140]))
            ck("TOPSECRET-B7" not in txt, "%s %r 把仓库外的内容读出来了" % (method, v))
        ck(os.path.exists(secret), "仓库外 secret.txt 没了 (%r)" % v)
        ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了 (%r)" % v)
    ck(outside_intact(canary), "哨兵被动了")
    ck(ctx.srv.alive(), "服务不健康")


@test("B", "normal-path-still-works", bug="B8")
def t_b8(ctx):
    """B8 正常路径仍然工作(别为了堵洞把正常功能也堵了)。"""
    r = ctx.repo
    r.write("sub/dir/file.txt", "v1\n")
    r.commit("c1")
    code, j = ctx.srv.jpost("/api/stage", {"files": ["sub/dir/file.txt"]})
    ck_eq(code, 200, "stage 正常路径")
    ck(j.get("ok") is True, "stage 正常路径 ok!=true")
    code, j = ctx.srv.jpost("/api/unstage", {"files": ["sub/dir/file.txt"]})
    ck_eq(code, 200, "unstage 正常路径")
    code, j = ctx.srv.jget("/api/diff?file=" + urllib.parse.quote("sub/dir/file.txt"))
    ck_eq(code, 200, "diff 正常路径")
    # 仓库自身也必须活着
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")


@test("B", "nul-byte-and-type-confusion", bug="B9")
def t_b9(ctx):
    """B9 NUL 字节 / 非字符串类型不能变成 500, 更不能变成越界。"""
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    for files in [["a\x00.txt"], [123], [{"k": 1}], [True], [[]]]:
        code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": files}))
        ctx.note("discard files=%-16r -> %s" % (files, code))
        ck(code != 500, "discard files=%r 返回 500: %s" % (files, txt[:140]))
        ck(code != 400 or os.path.exists(os.path.join(r.dir, ".git")),
           "出错了但仓库被破坏")
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")
    ck(ctx.srv.alive(), "服务不健康")


@test("B", "resolve-unit-level", bug="B10-crit")
def t_b10(ctx):
    """B10 直接打 resolve_in_root 这个唯一的防线, 穷举输入。

    判据分两档, 别混:
      * 解析到 root **之外**  = 真越界, 必须拒绝;
      * 解析到 root **本身**  = 看着「在里面」, 但 discard 对目录会 rmtree,
        所以一样必须拒绝。这是最容易漏的一档。
    """
    sys.path.insert(0, AIGIT)
    sys.modules.pop("server", None)
    import server as S
    import pathlib
    repo = S.Repo(pathlib.Path(ctx.repo.dir))
    root = repo.root

    must_reject = ["", ".", "./", "..", "../", "a/../..", "sub/../../..",
                   "..\\..", "\\", "//", "C:\\", "C:/", "/", "\\\\",
                   "../..", "a/b/../../..", ".git", ".git/.."]
    info_only = ["~", "a.txt", "./a.txt", "sub/../a.txt", "中文.txt", "C:foo"]

    escaped, became_root = [], []
    for v in must_reject:
        try:
            p = repo.resolve_in_root(v)
            try:
                p.relative_to(root)
                inside = True
            except ValueError:
                inside = False
            is_root = os.path.normcase(str(p)) == os.path.normcase(str(root))
            ctx.note("resolve_in_root(%-16r) -> %-28s %s"
                     % (v, str(p).replace(str(root), "<ROOT>"),
                        "**解析到 root 之外**" if not inside else
                        ("**解析到 root 自己**" if is_root else "(在 root 内)")))
            if not inside:
                escaped.append(v)
            elif is_root:
                became_root.append(v)
        except S.GitError as e:
            ctx.note("resolve_in_root(%-16r) -> 拒绝 (%s)" % (v, str(e)[:40]))
        except Exception as e:
            ctx.note("resolve_in_root(%-16r) -> 未捕获异常 %s: %s" % (v, type(e).__name__, e))
            escaped.append("%s(%s)" % (v, type(e).__name__))

    ctx.note("--- 以下只记录, 不判 fail ---")
    for v in info_only:
        try:
            p = repo.resolve_in_root(v)
            ctx.note("resolve_in_root(%-16r) -> %s" % (v, str(p).replace(str(root), "<ROOT>")))
        except Exception as e:
            ctx.note("resolve_in_root(%-16r) -> %s: %s" % (v, type(e).__name__, e))

    ck(not escaped, "resolve_in_root 放行到 root 之外的输入: %r" % escaped)
    ck(not became_root,
       "resolve_in_root 把这些输入解析成了 repo root 本身: %r\n"
       "        (discard/stage/... 拿到的是目录, discard 会 rmtree 整个仓)" % became_root)


@test("B", "missing-file-param-defaults-empty", bug="B11")
def t_b11(ctx):
    """B11 /api/diff 与 /api/file 的 file 参数默认值就是空串。

    F-01 的根因(空串解析成 repo root)在只读路径上是什么表现, 这里量一下。
    """
    r = ctx.repo
    r.write("a.txt", "v1\n")
    r.commit("c1")
    r.write("a.txt", "v2\n")
    r.write("secret_untracked.txt", "TOPSECRET-READ\n")
    for path in ["/api/diff", "/api/diff?file=", "/api/file", "/api/file?file="]:
        code, txt = ctx.srv.raw("GET", path)
        ctx.note("GET %-20s -> %-4s %r" % (path, code, txt[:130]))
        ck(code != 500, "GET %s 返回 500: %s" % (path, txt[:140]))
        ck("TOPSECRET-READ" not in txt, "GET %s 把仓外/不该读的内容吐出来了" % path)
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")
    ck(ctx.srv.alive(), "服务不健康")


# ════════════════════════════════════════════════════════ C 组:破坏性操作

@test("C", "discard-untracked-deletes", bug="C1")
def t_c1(ctx):
    """C1 discard 一个未跟踪文件, 真的删掉。"""
    r = ctx.repo
    r.write("tracked.txt", "a\n")
    r.commit("c1")
    r.write("junk.txt", "junk\n")
    ck(r.exists("junk.txt"), "前置: junk.txt 应存在")
    code, j = ctx.srv.jpost("/api/discard", {"files": ["junk.txt"]})
    ck_eq(code, 200, "discard 未跟踪文件")
    ck(j.get("ok") is True, "ok!=true")
    ck(not r.exists("junk.txt"), "junk.txt 没被删掉")
    ctx.note("discard 未跟踪文件 -> 已删除")


@test("C", "discard-modified-tracked-restores", bug="C2")
def t_c2(ctx):
    """C2 discard 一个已修改的已跟踪文件, 真的还原。"""
    r = ctx.repo
    r.write("t.txt", "original\n")
    r.commit("c1")
    r.write("t.txt", "MODIFIED\n")
    code, j = ctx.srv.jpost("/api/discard", {"files": ["t.txt"]})
    ck_eq(code, 200, "discard 已跟踪文件")
    ck_eq(r.read("t.txt"), "original\n", "还原后的内容")


@test("C", "discard-missing-file-no-crash", bug="C3")
def t_c3(ctx):
    """C3 discard 不存在的文件, 不崩。"""
    r = ctx.repo
    r.write("t.txt", "a\n")
    r.commit("c1")
    for files in [["nope.txt"], ["a/b/c/deep.txt"], []]:
        code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": files}))
        ctx.note("discard %-22r -> %s %s" % (files, code, txt[:70].replace("\n", " ")))
        ck(code in (200, 400), "discard %r 得到 %s" % (files, code))
        ck("Traceback" not in txt, "泄露堆栈")
    ck(ctx.srv.alive(), "服务不健康")
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")


@test("C", "discard-directory-no-escape", bug="C11-crit")
def t_c4(ctx):
    """C4 discard 一个目录, 不能越界删到目录外, 也不能把目录里的已跟踪文件删掉。

    「丢弃一个目录」在 VSCode 里不是标准操作; aigit 接受它, 就必须保证
    不会顺手删掉一堆已跟踪文件(git 视角里它们会变成 deleted)。
    """
    r = ctx.repo
    r.write("keep/tracked.txt", "important\n")
    r.commit("c1")
    r.write("tracked_dir/a.txt", "tracked-in-dir\n")
    r.commit("c2")
    r.write("untracked_dir/b.txt", "junk\n")
    os.makedirs(os.path.join(r.dir, "untracked_dir", "nested"), exist_ok=True)
    r.write("untracked_dir/nested/c.txt", "deep\n")

    before_status = r.out("status", "--porcelain")
    ctx.note("discard 前 git status: %r" % before_status[:160])

    code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": ["tracked_dir"]}))
    ctx.note("discard tracked_dir(里面有**已跟踪**文件) -> %s %s"
             % (code, txt[:80].replace("\n", " ")))
    after = r.out("status", "--porcelain")
    ctx.note("discard 后 git status: %r" % after[:160])
    deleted_tracked = [l for l in after.split("\n") if l.startswith(" D")]
    ck(not deleted_tracked,
       "discard 一个含已跟踪文件的目录, 把已跟踪文件删掉了: %r" % deleted_tracked)
    ck_eq(code, 400, "discard 含已跟踪文件的目录期望 400(或至少不删已跟踪文件)")


@test("C", "discard-dir-outside-untouched", bug="C12")
def t_c4b(ctx):
    """C4b discard 目录时, 仓库外的东西一个都不能少。"""
    outside = os.path.join(TESTROOT, "_outside_dir")
    shutil.rmtree(outside, ignore_errors=True)
    os.makedirs(os.path.join(outside, "inner"), exist_ok=True)
    canary = os.path.join(outside, "inner", "canary.txt")
    with io.open(canary, "w", encoding="utf-8") as f:
        f.write("KEEP")
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    # 造一个链接指向仓库外, 再尝试 discard
    link = os.path.join(r.dir, "outlink")
    if not os.path.exists(link):
        subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                       capture_output=True, timeout=30)
    if not os.path.exists(link):
        raise Skip("本机造不出目录联接, 无法测这条")
    code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": ["outlink"]}))
    ctx.note("discard outlink(指向仓库外) -> %s" % code)
    ck_eq(code, 400, "链接指向仓库外, 期望 400")
    ck(os.path.exists(canary), "仓库外的 canary 被删了 —— 越界成功")


@test("C", "commit-empty-message", bug="C5")
def t_c5(ctx):
    """C5 commit 空信息, 拒绝并给清晰错误。"""
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    r.write("a.txt", "changed\n")
    for msg in ["", "   ", "\n", "\t\n "]:
        code, j = ctx.srv.jpost("/api/commit", {"message": msg})
        ctx.note("commit message=%-8r -> %s %r" % (msg, code, str(j.get("error"))[:60]))
        ck_eq(code, 400, "空提交信息应 400")
        ck(j.get("ok") is False, "ok 应为 false")
        ck(bool(j.get("error")), "没有给出错误信息")
    ck(r.read("a.txt") == "changed\n", "改动被吞了 —— 不该提交")


@test("C", "checkout-nonexistent-branch", bug="C6")
def t_c6(ctx):
    """C6 checkout 不存在的分支, 报错不崩。"""
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    for name in ["no-such-branch", "", "refs/heads/nope", "main..main"]:
        code, txt = ctx.srv.raw("POST", "/api/checkout", json.dumps({"name": name}))
        ctx.note("checkout %-20r -> %s" % (name, code))
        ck(code in (400, 200), "checkout %r 得到 %s" % (name, code))
        ck("Traceback" not in txt, "泄露堆栈")
    ck(ctx.srv.alive(), "服务不健康")
    ck(r.out("rev-parse", "--abbrev-ref", "HEAD").strip() == "main", "分支状态被搞乱了")


@test("C", "branch-name-injection", bug="C7-crit")
def t_c7(ctx):
    """C7 分支名含 ; && $() 等必须拒绝。

    new_branch 的白名单是 re.fullmatch(r"[\\w./-]{1,100}")。
    这里穷举畸形输入, 并且额外验一件更要紧的事: 白名单没覆盖到的形态,
    git 自己会不会当成选项。
    """
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    hostile = [
        "x;rm -rf /", "x&&whoami", "x|nc", "x$(id)", "x`id`",
        "x\nrm -rf /", "x\ty", "../../evil", "-b", "--orphan", "-f",
        "--", "x --force", "a/b;c", "'x'", '"x"', "x\\y", "x\x00y",
        "*", "?", "[a-z]", "..", ".", "a..b", "a//b", "a/../b",
        "~", "^", "HEAD", "@", "x~1", "x^", "x:", "x@{0}",
        "a" * 101, "x y", "x=y", "x%20y", "x;echo", "a\nb",
        "--quiet", "-q", "x\u0000", "a\u202eb", "\u202eabc",
    ]
    accepted = []
    for name in hostile:
        code, txt = ctx.srv.raw("POST", "/api/branch", json.dumps({"name": name}))
        ctx.note("branch %-16r -> %-4s %s" % (name, code, txt[:60].replace("\n", " ")))
        ck(code != 500, "branch %r 返回 500: %s" % (name, txt[:140]))
        if code == 200:
            accepted.append(name)
    ck(not accepted, "这些分支名被接受了(要求全部拒绝): %r" % accepted)
    # 关键: 拒绝之后仓库还正常吗
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")
    branches = r.out("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    ctx.note("事后分支列表: %r" % branches)
    ck(branches == ["main"], "事后分支应只有 main, 实际 %r" % branches)


@test("C", "branch-name-unicode-allowed", bug="C15-info")
def t_c7c(ctx):
    """C7b 白名单 `\\w` 在 Python3 里是 Unicode 感知的, 会放行 CJK / emoji 分支名。

    这**不是**注入(git 本身允许 UTF-8 ref 名), 但与「只允许字母数字下划线」
    的直觉不符, 且控制字符 U+202E(RTL override)能过白名单就有换行/伪造显示名
    的余地。这条只做记录, 不判 fail。
    """
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    results = {}
    for name in ["中文名", "branchname", "a b", "a\u202eb", "\u202egnp.py", "café"]:
        code, txt = ctx.srv.raw("POST", "/api/branch", json.dumps({"name": name}))
        results[name] = code
        ctx.note("branch %-12r -> %-4s %s" % (name, code, txt[:60].replace("\n", " ")))
    ck(all(v in (200, 400) for v in results.values()), "有请求既不是 200 也不是 400")
    got = r.out("for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n")
    ctx.note("最终分支: %r" % [g for g in got if g])
    ck(os.path.exists(os.path.join(r.dir, ".git")), ".git 没了")
    ck(ctx.srv.alive(), "服务不健康")


@test("C", "checkout-has-no-whitelist", bug="C13-crit")
def t_c7b(ctx):
    """C7b /api/checkout 必须有白名单, 与 /api/branch 同一套。

    **这条断言的方向在 2026-10-01 修正过一次, 理由必须留着**:
    原断言是 `ck(not accepted, "接口无任何校验")` + 「`a.txt` 必须变成 `'a\\n'`」
    —— 后者要求 `-f` **被接受并丢弃用户的未提交改动**。那是在钉 bug。

    回到规格原文(`doc/aigit-review.md:201`, F-04):
        「**期望行为**: 这类值应被拒绝。」
    以及修法表 `doc/aigit-review.md:691`:
        「给 `checkout` 加和 `new_branch` 同一套白名单」

    「应被拒绝」的推论是: 一次 `git checkout` 都没跑过 ⇒ 未提交改动**必须还在**。
    原断言的期望值与规格直接矛盾, 不是实现偏离规格 —— 详见
    `doc/aigit-close-3-failures.md` §3。
    """
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.commit("c1")
    r.write("a.txt", "modified\n")          # 脏工作区
    hostile = ["--orphan", "--detach", "-f", "--", ".", ":/", "-B", "HEAD@{1}"]
    accepted = []
    for name in hostile:
        code, txt = ctx.srv.raw("POST", "/api/checkout", json.dumps({"name": name}))
        branch = ""
        try:
            branch = json.loads(txt).get("branch", "")
        except Exception:
            pass
        ctx.note("checkout %-12r -> %-4s branch=%r %s"
                 % (name, code, branch, txt[:50].replace("\n", " ")))
        ck(code != 500, "checkout %r 返回 500: %s" % (name, txt[:140]))
        if code == 200:
            accepted.append(name)
    # 先量后果, 最后才断言 —— ck() 是立刻抛的, 顺序反了就永远量不到证据。
    _checkout_impact(ctx, r)
    # 「这类值应被拒绝」= 白名单在生效。原值是 `not accepted`, 那在
    # 「全被拒」与「全被接受」两种世界里都成立, 挡不住「只放行一个」。
    ck_eq(accepted, [], "checkout 接受了这些值(白名单没拦住): %r" % accepted)


def _checkout_impact(ctx, r):
    """白名单拒绝之后, 断言工作区/分支/status **一个都没被动过**。

    原版这里是反的: 它断言 `a.txt == "a\\n"`(改动被 force 丢掉了)与
    「分支 != HEAD」(被 `--detach` 推走了)—— 两条都在要求危害发生。
    现在这三条是钉住「拒绝即无副作用」, 比原版**更严**: 原版只查两个方向,
    这里把 status 也钉住, 并且值从「危害已发生」翻成「危害未发生」。
    """
    ctx.note("事后 git 认定的分支: %r"
             % r.out("rev-parse", "--abbrev-ref", "HEAD").strip())
    ctx.note("事后 a.txt 内容: %r  (HEAD 里是 'a\\n', 动手前工作区是 'modified\\n')"
             % r.read("a.txt"))
    ctx.note("事后 git status: %r  (空 = 未提交改动被静默丢弃)" % r.out("status", "--porcelain")[:160])
    ck_eq(r.read("a.txt"), "modified\n",
          "未提交改动消失了 —— 而所有 hostile 值本该被 400 拒绝, 不该有任何副作用")
    ck_eq(r.out("rev-parse", "--abbrev-ref", "HEAD").strip(), "main",
           "被拒的 checkout 竟然改了当前分支(游离 HEAD 是 aigit 自己不认的状态)")
    # 不 strip: git status --porcelain 的首列(X 索引位)是空格, 有意义
    ck_eq(r.out("status", "--porcelain"), " M a.txt\n",
          "被拒的 checkout 竟然改了 git status")


@test("C", "reset-is-unconditional", bug="C14")
def t_c8(ctx):
    """C8 /api/reset 没有任何护栏, 直接 hard reset + clean -fd。"""
    r = ctx.repo
    r.write("keep.txt", "keep\n")
    r.commit("c1")
    r.write("untracked.txt", "u\n")
    r.write("keep.txt", "modified\n")
    code, j = ctx.srv.jpost("/api/reset", {})
    ctx.note("POST /api/reset -> %s ok=%s" % (code, j.get("ok")))
    ck_eq(code, 200, "reset 状态码")
    ck_eq(r.read("keep.txt"), "keep\n", "reset 后内容")
    ck(not r.exists("untracked.txt"), "reset 后未跟踪文件应被 clean 掉")


# ════════════════════════════════════════════════════════ D 组:生成器

def gen_available():
    return os.path.exists(GEN_PY)


def gen_module():
    if not gen_available():
        raise Skip("gen.py 不存在(aigit2 那条线还没交付)")
    sys.path.insert(0, AIGIT)
    sys.modules.pop("gen", None)
    import gen
    return gen


def independent_merge_check(root, branches, expect_clean, label, ctx):
    """不信生成器自报的字段, 自己再跑一遍 git merge。"""
    res = []
    work = root
    for br in branches:
        # 上一轮留下的冲突状态会让后续 merge 直接 rc=128, 那样测的不是分支本身。
        # 每个分支动手前先把工作区收拾干净。
        subprocess.run(["git", "-C", work, "merge", "--abort"],
                       capture_output=True, env=GIT_ENV, timeout=60)
        p = subprocess.run(["git", "-C", work, "merge", "--no-edit", br],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=120)
        u = subprocess.run(["git", "-C", work, "diff", "--name-only", "--diff-filter=U"],
                           capture_output=True, text=True, encoding="utf-8", env=GIT_ENV)
        unmerged = [x for x in u.stdout.split("\n") if x.strip()]
        st = subprocess.run(["git", "-C", work, "status", "--porcelain"],
                            capture_output=True, text=True, encoding="utf-8", env=GIT_ENV)
        res.append({"branch": br, "rc": p.returncode, "unmerged": unmerged,
                    "status": [x for x in st.stdout.split("\n") if x.strip()],
                    "out": (p.stdout + p.stderr).strip()[:200]})
        ctx.note("  merge %-28s rc=%s unmerged=%d status=%d"
                 % (br, p.returncode, len(unmerged), len(res[-1]["status"])))
    if expect_clean:
        for r in res:
            ck_eq(r["unmerged"], [], "%s: %s 合并后仍有未合并路径" % (label, r["branch"]))
            ck_eq(r["status"], [], "%s: %s 合并后工作区不干净: %r" % (label, r["branch"], r["status"]))
            ck_eq(r["rc"], 0, "%s: %s 合并返回非 0: %s" % (label, r["branch"], r["out"]))
    else:
        conflicted = [r for r in res if r["unmerged"] or r["rc"] != 0]
        ck(conflicted, "%s: 期望产生冲突, 实际 %d/%d 次合并都干净了"
           % (label, len(conflicted), len(res)))
    return res


def api_gen(ctx, kind, count=None, seed=None):
    payload = {"kind": kind}
    if count is not None:
        payload["count"] = count
    if seed is not None:
        payload["seed"] = seed
    code, j = ctx.srv.jpost("/api/gen", payload)
    return code, j


def _collect_branches(j):
    out = []
    for b in (j.get("branches") or []):
        if isinstance(b, dict):
            for n in (b.get("branches") or []):
                out.append(n)
    return out


@test("D", "mergeable-via-api", bug="D1")
def t_d1(ctx):
    """D1 /api/gen kind=mergeable 造出的分支, 真的 merge 成功(独立复核)。"""
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    code, j = api_gen(ctx, "mergeable")
    ck_eq(code, 200, "/api/gen 状态码")
    ck(j.get("ok") is True, "ok!=true: %r" % j)
    brs = _collect_branches(j)
    ctx.note("产出分支: %r" % brs)
    ck(len(brs) >= 2, "至少要 2 个分支才能测合并, 实际 %r" % brs)
    independent_merge_check(r.dir, brs, True, "api/mergeable", ctx)


@test("D", "unrelated-via-api", bug="D2")
def t_d2(ctx):
    """D2 /api/gen kind=unrelated 同上。"""
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    code, j = api_gen(ctx, "unrelated")
    ck_eq(code, 200, "/api/gen 状态码")
    brs = _collect_branches(j)
    ctx.note("产出分支: %r" % brs)
    ck(len(brs) >= 2, "实际 %r" % brs)
    independent_merge_check(r.dir, brs, True, "api/unrelated", ctx)


@test("D", "adddel-via-api", bug="D3")
def t_d3(ctx):
    """D3 /api/gen kind=adddel 同上。"""
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    code, j = api_gen(ctx, "adddel")
    ck_eq(code, 200, "/api/gen 状态码")
    brs = _collect_branches(j)
    ctx.note("产出分支: %r" % brs)
    ck(len(brs) >= 2, "实际 %r" % brs)
    independent_merge_check(r.dir, brs, True, "api/adddel", ctx)


@test("D", "conflict-via-api", bug="D4")
def t_d4(ctx):
    """D4 /api/gen kind=conflict 真的产生冲突(独立复核)。"""
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    code, j = api_gen(ctx, "conflict")
    ck_eq(code, 200, "/api/gen 状态码")
    brs = _collect_branches(j)
    ctx.note("产出分支: %r" % brs)
    ck(len(brs) >= 2, "实际 %r" % brs)
    res = independent_merge_check(r.dir, brs, False, "api/conflict", ctx)
    for rr in res:
        if rr["unmerged"]:
            for p in rr["unmerged"]:
                full = os.path.join(r.dir, p.replace("/", os.sep))
                if os.path.exists(full):
                    sz = os.path.getsize(full)
                    ctx.note("  冲突文件 %s 大小=%d" % (p, sz))
                    ck(sz > 0, "冲突文件 %s 是空的" % p)


@test("D", "determinism-via-api", bug="D5")
def t_d5(ctx):
    """D5 同 seed 两次产出完全相同。

    注意: /api/gen 是有状态的(它往仓库里写东西), 所以两次必须用**两个独立的新仓**,
    否则第二次是在第一次的残留上生成, 测的就不是确定性了。
    """
    r1 = TestRepo("d5-a")
    r1.write("seed.txt", "seed\n")
    r1.commit("init")
    s1 = r1.server()
    c1 = Ctx(r1, s1)
    c1.notes = ctx.notes
    try:
        code1, j1 = api_gen(c1, "mergeable", seed=12345)
        f1 = sorted(_walk_rel(r1.dir))
        txt1 = {rel: _read(os.path.join(r1.dir, rel)) for rel in f1}
    finally:
        s1.stop()
    r2 = TestRepo("d5-b")
    r2.write("seed.txt", "seed\n")
    r2.commit("init")
    s2 = r2.server()
    c2 = Ctx(r2, s2)
    c2.notes = ctx.notes
    try:
        code2, j2 = api_gen(c2, "mergeable", seed=12345)
        f2 = sorted(_walk_rel(r2.dir))
        txt2 = {rel: _read(os.path.join(r2.dir, rel)) for rel in f2}
    finally:
        s2.stop()
    shutil.rmtree(r1.dir, ignore_errors=True)
    shutil.rmtree(r2.dir, ignore_errors=True)
    ctx.note("第一次 code=%s 分支=%r" % (code1, _collect_branches(j1)))
    ctx.note("第二次 code=%s 分支=%r" % (code2, _collect_branches(j2)))
    ck_eq(code1, 200, "第一次 /api/gen")
    ck_eq(code2, 200, "第二次 /api/gen")
    b1, b2 = _collect_branches(j1), _collect_branches(j2)
    ctx.note("seed=12345 第一次分支: %r" % b1)
    ctx.note("seed=12345 第二次分支: %r" % b2)
    ck_eq(b1, b2, "同 seed 两次产出的分支不同 —— 不确定")
    ctx.note("仓内文件集合相等? %s (%d 个)" % (f1 == f2, len(f1)))
    if f1 != f2:
        ctx.note("  仅第一仓: %r" % sorted(set(f1) - set(f2))[:8])
        ctx.note("  仅第二仓: %r" % sorted(set(f2) - set(f1))[:8])
    ck_eq(f1, f2, "同 seed 两次产出文件集合不同")
    # 内容也要一样
    diffs = [rel for rel in f1 if rel in f2 and txt1[rel] != txt2[rel]]
    ck(not diffs, "同 seed 下这些文件内容不同: %r" % diffs[:8])


@test("D", "count-3-different", bug="D6")
def t_d6(ctx):
    """D6 count=3 真的造出 3 套不同场景(不是复制粘贴)。

    这是打 count 参数的死代码: server.py 的 generate() 收了 count 但从没用过。
    """
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    code, j = api_gen(ctx, "all", count=3, seed=777)
    ck_eq(code, 200, "/api/gen 状态码")
    b = j.get("branches") or []
    ctx.note("count=3 kind=all 产出 %d 个场景条目: %r" % (len(b), [x.get("file") for x in b]))
    names = []
    for x in b:
        names.extend(x.get("branches") or [])
    ctx.note("总分支数 %d: %r" % (len(names), names))
    ck(len(names) >= 6, "count=3 要求至少 6 个分支(3 套 × 2), 实际 %d: %r" % (len(names), names))
    ck_eq(len(set(names)), len(names), "分支名有重复: %r" % names)


@test("D", "dirty-repo-rejected-via-api", bug="D7-crit")
def t_d7(ctx):
    """D7 在有未提交改动的仓里生成, 必须拒绝。

    这条是数据安全: server.py 的 generate() 没有任何脏检查, 而且内部会
    `git add -A` + `git commit` —— 等于把用户没提交的工作**悄悄提交掉**。
    """
    r = ctx.repo
    r.write("mine.txt", "MY UNCOMMITTED WORK\n")
    r.write("other.txt", "x\n")
    r.commit("init")
    r.write("mine.txt", "MY UNCOMMITTED WORK v2\n")
    r.write("brand_new.txt", "brand new, never committed\n")
    dirty_before = r.out("status", "--porcelain")
    ctx.note("生成前 status: %r" % dirty_before[:200])
    log_before = r.out("log", "--oneline")

    code, j = api_gen(ctx, "mergeable")
    ctx.note("/api/gen 在脏仓里 -> code=%s ok=%s err=%s"
             % (code, j.get("ok"), str(j.get("error"))[:70]))

    ck_eq(code, 400, "脏仓里生成必须被拒绝(400)")
    ck_eq(j.get("ok"), False, "脏仓里生成 ok 应为 false")

    # 用户的改动必须一字不动
    ck_eq(r.read("mine.txt"), "MY UNCOMMITTED WORK v2\n", "用户改动被改了")
    ck(r.exists("brand_new.txt"), "用户未跟踪的新文件消失了")
    ctx.note("生成后 status: %r" % r.out("status", "--porcelain")[:200])
    ctx.note("生成后 log:   %r" % r.out("log", "--oneline")[:200])
    ck_eq(r.out("log", "--oneline"), log_before, "历史被写入了 —— 用户的工作被偷偷提交了")


@test("D", "generate-twice-branch-collision", bug="D8")
def t_d8(ctx):
    """D8 连着生成两次, 第二次会不会因为分支重名而炸。

    server.py 的 _gen_disjoint 用的是**固定**分支名 feat/disjoint-0 / -1,
    第二次必然撞名。
    """
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("init")
    c1, j1 = api_gen(ctx, "unrelated", seed=1)
    ctx.note("第一次 unrelated seed=1 -> %s 分支=%r" % (c1, _collect_branches(j1)))
    c2, j2 = api_gen(ctx, "unrelated", seed=2)
    ctx.note("第二次 unrelated seed=2 -> %s 分支=%r" % (c2, _collect_branches(j2)))
    ctx.note("错误文案: %r" % str(j2.get("error"))[:220])
    ck_eq(c2, 200, "第二次生成应当成功(不同 seed 应产出不同分支名)")
    ck(j2.get("ok") is True, "第二次生成 ok!=true: %r" % j2)


@test("D", "gen-module-direct", bug="D9")
def t_d9(ctx):
    """D9 直接打 gen.py 的 ScenarioGen(D 组不能只测 server 那套旧的)。

    这条只测两件 selftest.py 明显没覆盖的事:
      a) count=3 是否真出 3 套;
      b) determinism 是否跨「不同初始仓」成立。
    """
    gen = gen_module()
    snaps = []
    for idx in range(2):
        r = TestRepo("d9-%d" % idx)
        r.write("seed.txt", "seed\n")
        r.commit("init")
        g = gen.ScenarioGen(r.dir)
        res = g.generate("mergeable", count=3, seed=4242)
        # gen.py 的返回: branches 是扁平的分支名列表(不是 server.py 那种嵌套 dict)
        snaps.append({
            "branches": list(res.get("branches") or []),
            "files": sorted(res.get("files") or []),
            "scenarios": len(res.get("scenarios") or []),
            "allVerified": res.get("allVerified"),
            "tree": sorted(_walk_rel(r.dir)),
        })
        ctx.note("ScenarioGen 第 %d 次 count=3 -> 场景数=%d 分支数=%d allVerified=%s"
                 % (idx, snaps[-1]["scenarios"], len(snaps[-1]["branches"]),
                    snaps[-1]["allVerified"]))
        ctx.note("   分支: %r" % snaps[-1]["branches"])
        shutil.rmtree(r.dir, ignore_errors=True)
    n = len(snaps[0]["branches"])
    ck_eq(snaps[0]["scenarios"], 3, "count=3 应当造出 3 个场景")
    ck(n >= 6, "count=3 应至少产出 6 个分支(3 套 × 2), 实际 %d" % n)
    ck(snaps[0]["allVerified"] is True,
       "gen.py 自报的 allVerified=%r —— 它自己说验证没过" % snaps[0]["allVerified"])
    ck_eq(snaps[0]["branches"], snaps[1]["branches"], "同 seed 两次分支名不同 —— 不确定")
    ck_eq(snaps[0]["tree"], snaps[1]["tree"], "同 seed 两次文件树不同 —— 不确定")


def _walk_rel(root):
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            yield os.path.relpath(full, root).replace("\\", "/")


def _read(p):
    try:
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


# ════════════════════════════════════════════════════════ E 组:与 git 一致

def _correct_porcelain(out):
    """按 git 文档正确解析 --porcelain=v1 -z。

    关键: -z 模式下 rename/copy 的原路径是**独立的 NUL 字段**,
    不是 "old -> new"。这一点 server.py 用错了(见 E2)。
    """
    fields = [f for f in out.split("\0") if f]
    rows = []
    i = 0
    while i < len(fields):
        e = fields[i]
        if len(e) < 3:
            i += 1
            continue
        x, y, path = e[0], e[1], e[3:]
        if x in ("R", "C"):
            i += 2      # 吃掉原路径字段
        else:
            i += 1
        rows.append((x, y, path))
    return rows


@test("E", "status-letters-match-git", bug="E1")
def t_e1(ctx):
    """E1 status() 里 staged/unstaged 的字母必须与 git 原始输出一致。"""
    r = ctx.repo
    r.write("mod.txt", "v1\n")
    r.write("del.txt", "d\n")
    r.write("staged.txt", "s\n")
    r.write("staged_mod.txt", "s\n")
    r.commit("c1")
    r.write("mod.txt", "v2\n")                 # M  unstaged
    os.remove(os.path.join(r.dir, "del.txt"))   # D  unstaged
    r.git("rm", "--cached", "del.txt")
    r.write("staged_mod.txt", "s2\n")           # MM staged+unstaged
    r.git("add", "staged_mod.txt")
    r.write("staged.txt", "s2\n")
    r.git("add", "staged.txt")                  # M  staged only
    r.write("untracked.txt", "u\n")             # ??
    raw = r.out("status", "--porcelain=v1", "-z", "--untracked-files=all")
    want = _correct_porcelain(raw)
    ctx.note("git 原始解析: %r" % want)
    code, j = ctx.srv.jget("/api/status")
    ck_eq(code, 200, "/api/status")
    got = sorted([(i["status"], i["path"], i["staged"])
                  for i in j.get("staged", [])]
                 + [(i["status"], i["path"], i["staged"])
                    for i in j.get("unstaged", [])])
    # 正确期望: x 决定 staged 一条, y 决定 unstaged 一条。MM 会出现两条。
    wantset = []
    for x, y, p in want:
        if x == "?" and y == "?":
            wantset.append(("?", p, False))
            continue
        if x not in (" ", "?"):
            wantset.append((x, p, True))
        if y not in (" ", "?"):
            wantset.append((y, p, False))
    wantset = sorted(wantset)
    ctx.note("server staged+unstaged: %r" % got)
    ctx.note("git 期望            : %r" % wantset)
    ck_eq(got, wantset, "status 与 git 原始输出不一致")


@test("E", "rename-parsing", bug="E5-crit")
def t_e2(ctx):
    """E2 rename 的解析要正确。

    git 的 --porcelain -z 对 rename 输出 "XY new\\0old\\0" —— 原路径是**独立字段**。
    server.py 按 "\\0" 切分后把原路径当成一条新 entry, 于是凭空多出一条垃圾记录。
    """
    r = ctx.repo
    r.write("old_name.txt", "content\n")
    r.commit("c1")
    r.git("mv", "old_name.txt", "new_name.txt")
    raw = r.out("status", "--porcelain=v1", "-z")
    ctx.note("git -z 原始输出: %r" % raw)
    ctx.note("含 ' -> '? %s" % (" -> " in raw))
    correct = _correct_porcelain(raw)
    ctx.note("正确解析应得: %r" % correct)
    ck_eq(len(correct), 1, "前置: 应当只有 1 条记录")

    code, j = ctx.srv.jget("/api/status")
    staged = [(i["status"], i["path"]) for i in j.get("staged", [])]
    ctx.note("server 报的 staged: %r" % staged)
    ck_eq(len(staged), 1, "server 报出了 %d 条 staged(rename 应只有 1 条)" % len(staged))
    ck_eq(staged[0][1], "new_name.txt", "rename 后的路径不对")


@test("E", "conflict-statuses", bug="E6")
def t_e3(ctx):
    """E3 UU / AA / DD / AU / UD / UA / DU 七种冲突状态都要能被标出来。"""
    r = ctx.repo
    r.write("f.txt", "base\n")
    r.commit("c1")
    r.git("checkout", "-b", "side")
    r.write("f.txt", "side\n")
    r.commit("side edit")
    r.git("checkout", "main")
    r.write("f.txt", "main\n")
    r.commit("main edit")
    p = r.git("merge", "side")
    ctx.note("merge 返回码=%s(非 0 才该冲突)" % p.returncode)
    raw = r.out("status", "--porcelain=v1", "-z")
    ctx.note("git 原始: %r" % raw)
    ck(p.returncode != 0, "前置: 这两个分支应当冲突")

    code, j = ctx.srv.jget("/api/status")
    conf = j.get("conflicted") or []
    ctx.note("server conflicted: %r" % [(i["status"], i["path"]) for i in conf])
    ck(len(conf) >= 1, "冲突文件没有被标出来")
    ck(any(i["path"] == "f.txt" for i in conf), "冲突文件 f.txt 不在 conflicted 列表里")
    ck(all(i["status"] == "U" for i in conf),
       "conflicted 的 status 字段应统一为 U, 实际 %r" % [i["status"] for i in conf])


@test("E", "diff-is-unified", bug="E7")
def t_e4(ctx):
    """E4 /api/diff 输出必须是合法 unified diff。"""
    r = ctx.repo
    body = "".join("line %d\n" % i for i in range(1, 21))
    r.write("d.txt", body)
    r.commit("c1")
    new = body.replace("line 10\n", "line 10 CHANGED\n")
    r.write("d.txt", new)
    r.git("add", "d.txt")

    code, j = ctx.srv.jget("/api/diff?file=d.txt&staged=1")
    ck_eq(code, 200, "/api/diff staged")
    d = j.get("diff") or ""
    ctx.note("diff 长度=%d 前 200 字=%r" % (len(d), d[:200]))
    ck(d.startswith("diff --git") or d.startswith("---"), "diff 没有头")
    ck("---" in d, "缺 --- 行")
    ck("+++" in d, "缺 +++ 行")
    ck("@@" in d, "缺 @@ hunk 头")
    ck("-line 10" in d, "缺删除行")
    ck("+line 10 CHANGED" in d, "缺新增行")

    # 工作区版本也要能出 diff
    r.write("d.txt", new + "extra\n")
    code, j = ctx.srv.jget("/api/diff?file=d.txt")
    ck_eq(code, 200, "/api/diff unstaged")
    ck("@@" in (j.get("diff") or ""), "工作区 diff 缺 @@")


# ════════════════════════════════════════════════════════ X 组:额外对抗

@test("X", "merge-endpoint-works", bug="X1-crit")
def t_x1(ctx):
    """X1 /api/merge 到底能不能用。

    Repo._run() 返回的是 p.stdout(字符串), 但 merge() 写的是 p.returncode —— 属性错误。
    """
    r = ctx.repo
    r.write("m.txt", "base\n")
    r.commit("c1")
    r.git("checkout", "-b", "side")
    r.write("m.txt", "side\n")
    r.commit("side")
    r.git("checkout", "main")
    code, txt = ctx.srv.raw("POST", "/api/merge", json.dumps({"name": "side"}))
    ctx.note("POST /api/merge -> code=%s" % code)
    ctx.note("响应: %r" % txt[:200])
    ck_eq(code, 200, "/api/merge 应当成功合并却返回了 %s: %s" % (code, txt[:200]))
    j = json.loads(txt)
    ck(j.get("ok") is True, "ok!=true")
    # 顺带: /api/gen 返回体里也带了 ok, 不该和 merge 的 ok 混
    code, txt = ctx.srv.raw("POST", "/api/merge", json.dumps({"name": "no-such"}))
    ctx.note("POST /api/merge 不存在的分支 -> %s %r" % (code, txt[:120]))
    ck(ctx.srv.alive(), "服务不健康")


@test("X", "count-param-actually-used", bug="X2")
def t_x2(ctx):
    """X2 count 参数到底有没有被用。

    直接读源码 + 端到端两头对照, 避免只靠代码阅读下结论。
    """
    import inspect
    sys.path.insert(0, AIGIT)
    sys.modules.pop("server", None)
    import server as S
    src = inspect.getsource(S.Repo.generate)
    ctx.note("generate 源码 %d 行, 提到 count %d 次"
             % (src.count(chr(10)), src.count("count")))
    ctx.note("generate 是否转调 gen.py: %s"
             % ("ScenarioGen" in src or "import gen" in src))

    # 必须在**两个各自全新的仓**里跑: 同一个仓里第二次生成会因为分支重名被拒,
    # 那种 400 是「撞名保护」, 不是「count 被忽略」, 两码事。
    def run_count(n):
        r = TestRepo("x2-%d" % n)
        r.write("seed.txt", "s\n")
        r.commit("init")
        s = r.server()
        c = Ctx(r, s)
        c.notes = ctx.notes
        try:
            code, j = api_gen(c, "mergeable", count=n, seed=5)
        finally:
            s.stop()
        brs = _collect_branches(j)
        ctx.note("count=%d -> code=%s 分支数=%d  %r" % (n, code, len(brs), brs[:3]))
        shutil.rmtree(r.dir, ignore_errors=True)
        return code, brs

    code1, b1 = run_count(1)
    code7, b7 = run_count(7)
    ck_eq(code1, 200, "count=1 的 /api/gen")
    ck_eq(code7, 200, "count=7 的 /api/gen")
    ck(len(b7) > len(b1),
       "count 参数被忽略: count=1 得 %d 个分支, count=7 得 %d 个"
       % (len(b1), len(b7)))


@test("X", "no-ui-web-dir", bug="X3")
def t_x3(ctx):
    """X3 前端资源到底在不在。

    server.py 的 GET / 读 WEB/index.html, WEB = <server.py 所在目录>/web。
    """
    web = os.path.join(AIGIT, "web")
    ctx.note("web 目录存在? %s" % os.path.isdir(web))
    if os.path.isdir(web):
        ctx.note("web 内容: %r" % sorted(os.listdir(web)))
    code, txt = ctx.srv.raw("GET", "/")
    ctx.note("GET / -> %s  %r" % (code, txt[:160]))
    ck_eq(code, 200, "GET / 返回 %s(前端缺失?)" % code)


@test("X", "sandbox-rmtree-guard", bug="X4")
def t_x4(ctx):
    """X4 --sandbox 会 shutil.rmtree(root), 且不加任何确认。

    只验证「它确实会删」这个事实(在临时目录里), 以及是否有护栏。
    """
    r = ctx.repo
    r.write("precious.txt", "precious\n")
    r.commit("c1")
    victim = r.dir + "-sandbox"
    shutil.rmtree(victim, ignore_errors=True)
    os.makedirs(victim, exist_ok=True)
    with io.open(os.path.join(victim, "old.txt"), "w", encoding="utf-8") as f:
        f.write("old\n")
    sub = os.path.join(victim, "keep", "deep.txt")
    os.makedirs(os.path.dirname(sub), exist_ok=True)
    with io.open(sub, "w", encoding="utf-8") as f:
        f.write("deep\n")
    port = free_port()
    p = subprocess.Popen([sys.executable, SERVER_PY, "--root", victim,
                          "--port", str(port), "--sandbox"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=AIGIT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        end = time.time() + 20
        wiped, reinit = False, False
        while time.time() < end and not (wiped and reinit):
            # --sandbox 是「rmtree 之后重建」, 目录会整个消失再回来。
            # .git 是 rmtree 之后才 init 出来的, 必须等它出现, 否则撞上中间那一下。
            if not os.path.exists(os.path.join(victim, "old.txt")):
                wiped = True
            if wiped and os.path.exists(os.path.join(victim, ".git")):
                reinit = True
            time.sleep(0.05)
        ctx.note("--sandbox 之后 old.txt 还在? %s ; .git 重建? %s"
                 % (os.path.exists(os.path.join(victim, "old.txt")), reinit))
        ctx.note("keep/deep.txt 还在? %s" % os.path.exists(sub))
        ck(wiped, "--sandbox 没有清空原目录(行为与文档不符)")
        ck(not os.path.exists(sub), "--sandbox 留下了 keep/deep.txt, rmtree 不彻底")
        ck(reinit, "--sandbox 清空之后没有重建 git 仓")
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        shutil.rmtree(victim, ignore_errors=True)


def _can_make_name(n):
    """Windows 文件名里有一批非法字符, 造不出来就老实跳过, 别假装测过。"""
    d = os.path.join(TESTROOT, "_nameprobe")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, n)
    try:
        with io.open(p, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(p)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("X", "unicode-filename-roundtrip", bug="X5")
def t_x5(ctx):
    """X5 带中文/空格/引号的文件名能不能正确往返。"""
    r = ctx.repo
    # 注意: Windows 文件名里不允许 `"`, 所以引号那条只能靠 git 的 core.quotePath
    # 在**非 ASCII** 名字上间接验, 不能直接造。这里只放能造出来的。
    names = ["中文文件.txt", "with space.txt", "dash-and_underscore.txt",
             "emoji-\U0001F600.txt", "tab\tname.txt", "semi;colon.txt",
             "amp&and.txt", "star*.txt", "brack[et].txt"]
    names = [n for n in names if _can_make_name(n)]
    ctx.note("本机可造的文件名: %r" % names)
    for n in names:
        r.write(n, "content of %s\n" % n)
    r.commit("c1")
    for n in names:
        r.write(n, "modified %s\n" % n)
    code, j = ctx.srv.jget("/api/status")
    ck_eq(code, 200, "/api/status")
    reported = sorted(i["path"] for i in j.get("unstaged", []))
    ctx.note("git 实际未跟踪/修改: %r" % sorted(
        [x[3:] for x in _correct_porcelain(
            r.out("status", "--porcelain=v1", "-z", "--untracked-files=all"))]))
    ctx.note("server 报未暂存  : %r" % reported)
    ck_eq(reported, sorted(names), "非 ASCII / 带空格引号的文件名没被正确解析")

    for n in names:
        code, j = ctx.srv.jget("/api/diff?file=" + urllib.parse.quote(n))
        ck_eq(code, 200, "/api/diff %r" % n)
        code, j = ctx.srv.jpost("/api/stage", {"files": [n]})
        ck_eq(code, 200, "/api/stage %r" % n)
    code, j = ctx.srv.jget("/api/status")
    ck_eq(sorted(i["path"] for i in j.get("staged", [])), sorted(names),
         "stage 之后 staged 列表不对")


@test("X", "concurrency", bug="X6")
def t_x6(ctx):
    """X6 并发请求会不会把状态搞坏或把服务搞死。"""
    import threading as th
    r = ctx.repo
    for i in range(12):
        r.write("f%02d.txt" % i, "v1\n")
    r.commit("c1")
    for i in range(12):
        r.write("f%02d.txt" % i, "v2\n")
    errs = []

    def worker(i):
        try:
            if i % 3 == 0:
                code, j = ctx.srv.jpost("/api/stage", {"files": ["f%02d.txt" % i]})
            elif i % 3 == 1:
                code, j = ctx.srv.jget("/api/status")
            else:
                code, j = ctx.srv.jget("/api/log?limit=3")
            if code != 200 or j.get("ok") is not True:
                errs.append((i, code, str(j)[:80]))
        except Exception as e:
            errs.append((i, "EXC", "%s: %s" % (type(e).__name__, e)))

    ts = [th.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    ctx.note("并发 12 请求, 异常 %d 个" % len(errs))
    for e in errs[:6]:
        ctx.note("  %r" % (e,))
    ck(not errs, "并发下有 %d 个请求异常: %r" % (len(errs), errs[:4]))
    ck(ctx.srv.alive(), "并发后服务不健康")


@test("X", "lock-serializes-commands", bug="X7")
def t_x7(ctx):
    """X7 写操作的锁是否真的覆盖了「读-改-写」(discard 的 ls-files 后再删)。

    这是竞态而不是功能缺陷, 很难稳定复现; 这里至少确认 discard 一次多个文件
    时不会因为分多次 git 调用而把仓库搞成半截状态。
    """
    r = ctx.repo
    r.write("a.txt", "a\n")
    r.write("b.txt", "b\n")
    r.commit("c1")
    r.write("a.txt", "A\n")
    r.write("b.txt", "B\n")
    r.write("untracked.txt", "u\n")
    code, j = ctx.srv.jpost("/api/discard", {"files": ["a.txt", "b.txt", "untracked.txt"]})
    ck_eq(code, 200, "批量 discard")
    ck_eq(r.read("a.txt"), "a\n", "a.txt 还原")
    ck_eq(r.read("b.txt"), "b\n", "b.txt 还原")
    ck(not r.exists("untracked.txt"), "untracked.txt 未删")
    st = r.out("status", "--porcelain")
    ck_eq(st.strip(), "", "批量 discard 后工作区应干净, 实际 %r" % st)


@test("X", "discard-raw-vs-resolved-path", bug="X8-crit")
def t_x8(ctx):
    """X8 discard 用**原始字符串**去比对 ls-files, 而不是用 resolve 之后的路径。

    Repo.discard 里:
        for f, ap in zip(files, paths):
            (tracked if f in tracked_set else untracked).append((f, ap))
    f 是请求里的原始串, tracked_set 来自 `git ls-files`(git 规范化后的路径)。
    两者写法不同 -> 同一个已跟踪文件被判成「未跟踪」-> 走 unlink 分支,
    于是工作区里那个已跟踪文件被**删掉**而不是被 checkout 还原。
    """
    r = ctx.repo
    r.write("a.txt", "original\n")
    r.commit("c1")
    r.write("a.txt", "MODIFIED\n")
    ck_eq(r.read("a.txt"), "MODIFIED\n", "前置: a.txt 应是修改过的")

    for spelling in ["./a.txt", "sub/../a.txt", "a.txt"]:
        r.write("a.txt", "MODIFIED-%s\n" % spelling)
        before = r.out("status", "--porcelain")
        code, txt = ctx.srv.raw("POST", "/api/discard", json.dumps({"files": [spelling]}))
        after = r.out("status", "--porcelain")
        ctx.note("discard %-16r -> %-4s  status 前=%r 后=%r"
                 % (spelling, code, before[:40], after[:40]))
        ck_eq(code, 200, "discard %r 状态码" % spelling)
        ck_eq(r.read("a.txt"), "original\n",
              "discard %r 之后 a.txt 应还原为 original, 实际 %r"
              % (spelling, r.read("a.txt")))
        # 还原后 git 必须认为它是干净的, 不能变成「已删除」
        st = r.out("status", "--porcelain").strip()
        ck("D " not in st and not st.startswith(" D"),
           "discard %r 之后 git 认为文件被删了: %r" % (spelling, st))
        if spelling == "a.txt":
            break       # 只在这个直白的写法上验一次就够


@test("X", "git-pathspec-magic", bug="X9")
def t_x9(ctx):
    """X9 校验的是 resolve 后的路径, 交给 git 的却是**原始字符串**。

    git 的 pathspec magic(`:(glob)` `:(exclude)` `:/` `:(top)`)不是文件系统路径,
    resolve_in_root 会把它当成一个普通文件名放行, 然后 git 按 magic 解释它。
    结果: 声称「stage 这一个文件」, 实际 stage 了一批。

    **期望值在 2026-10-01 修正过一次, 理由必须留着**:
    原断言是 `ck_eq(sorted(st), ["a.txt"], ...)`。规格原文
    (`doc/aigit-review.md:281`, F-06)写的是:

        「**期望行为**: 只 stage 那个字面名文件(不存在 → 无操作),
          **或** 400 拒绝。」

    两个合法结局是「无操作」(stage 不到任何东西)与「400 拒绝」,
    **两个都不产生 `['a.txt']`**。而且 `['a.txt']` 不可达: 仓里 `a.txt` 与
    `b.txt` 完全对称(同层、同后缀、同为已跟踪、同在 `c1`、同被改成
    `modified`、同未暂存), 请求串 `:(glob)**/*.txt` 里没有任何东西能提供
    区分依据 —— 按深度/后缀/状态/提交都区分不了。穷举与论证见
    `doc/aigit-close-3-failures.md` §4。
    """
    r = ctx.repo
    for n in ["a.txt", "b.txt", "deep/c.txt", "keep.md"]:
        r.write(n, "x\n")
    r.commit("c1")
    for n in ["a.txt", "b.txt", "deep/c.txt"]:
        r.write(n, "modified\n")

    # F-06 的核心: 声称 stage 一个 pathspec, 绝不能 stage 出一批。
    # 「或 400 拒绝」是规格给的第二个合法结局, 所以两个都放行 ——
    # 唯一不放行的是「stage 出多于请求范围的文件」。
    for magic in (":(glob)**/*.txt", ":(exclude)a.txt", ":(top)", ":/"):
        code, j = ctx.srv.jpost("/api/stage", {"files": [magic]})
        st = sorted(r.git("diff", "--cached", "--name-only").stdout.split())
        ctx.note("stage %-20r -> %-4s 实际被 stage 的: %r" % (magic, code, st))
        ck(code == 400 or st == [],
           "magic %r 必须被 400 拒绝或退化成无操作, 实际 code=%s staged=%r"
           % (magic, code, st))
        r.git("reset", "-q", "HEAD", "--")

    # 对照: 正常字面名必须还能 stage —— 挡住「一律不 stage 也能过上面那组」。
    code, j = ctx.srv.jpost("/api/stage", {"files": ["a.txt"]})
    ctx.note("对照: stage 'a.txt' -> %s" % code)
    ck_eq(code, 200, "正常字面名 stage 状态码")
    ck_eq(sorted(r.git("diff", "--cached", "--name-only").stdout.split()), ["a.txt"],
          "正常字面名文件没被 stage 成功 —— 修复不能是把 stage 整体弄坏")
    r.git("reset", "-q", "HEAD", "--")


# ════════════════════════════════ N 组: 接线可达性(conflicts / history_ops)
#
# 这一组的存在理由与其他 54 条不同: 别的组测「接口做了什么」, 这组测
# 「接口**存在**」。`conflicts.py`(40KB)与 `history_ops.py`(65KB)一直在仓里,
# 也各有自己的测试套件(test_conflicts.py 14 条 / test_history_ops.py 37 条),
# 但 server.py **从来没有 import 过它们** —— 于是 aigit 最有分量的这两块功能
# 在产品里 100% 不可达: 所有 `/api/conflict/*` 与 `/api/history/*` 全是 404。
# 「模块测得再好, 产品里够不着」不算数, 所以门禁必须落在 HTTP 这一层。
#
# ⚠ 方向:这组测的是**该通的通 + 该拒的拒**, 不是「某个洞存在」。
#   反过来说, 如果哪天有人把接线摘掉, 这组会红 —— 那是**对**的。

@test("N", "conflict-routes-wired", bug="N1-接线")
def t_n1(ctx):
    """N1 `/api/conflict/*`:真造一个冲突态, 把写路径端到端跑通。

    为什么非要**真冲突**:只打 `GET /api/conflict/state` 只能证明路由活着。
    `resolve` / `choose` / `commit` 三条路在「没有冲突」时会被模块自己的
    「不在合并态」前置检查挡掉, 返回的 400 与「路由坏了」的 404 长得一样 ——
    分不开。所以这里造两个冲突文件, 一个走 `choose`、一个走 `resolve`,
    再提交合并, 并核对**磁盘上的内容**。
    """
    r = ctx.repo
    for n in ("cf.txt", "cg.txt"):
        r.write(n, "base\n")
    r.commit("c1")
    main_branch = r.out("rev-parse", "--abbrev-ref", "HEAD").strip()
    r.git("checkout", "-b", "side")
    for n in ("cf.txt", "cg.txt"):
        r.write(n, "side\n")
    r.commit("side change")
    r.git("checkout", main_branch)          # 不写死 "main": git init 的默认名不是它
    for n in ("cf.txt", "cg.txt"):
        r.write(n, "main\n")
    r.commit("main change")
    mg = r.git("merge", "side", check=False)
    ck(mg.returncode != 0, "没造出冲突,这一条后面全是空跑(merge rc=0)")
    ck_eq(sorted(x for x in r.out("status", "--porcelain").split("\n") if x),
          ["UU cf.txt", "UU cg.txt"], "冲突态没造出来")

    code, j = ctx.srv.jget("/api/conflict/state")
    ck_eq(code, 200, "/api/conflict/state 应 200")
    ck_eq(j.get("inMerge"), True, "state 没认出合并态")
    ck_eq(j.get("remaining"), 2, "state 报的未解决数不对")
    ctx.note("参与分支 = %r" % (j.get("branches"),))

    code, j = ctx.srv.jget("/api/conflict/list")
    ck_eq(code, 200, "/api/conflict/list 应 200")
    ck_eq(sorted(x["path"] for x in (j.get("files") or [])),
          ["cf.txt", "cg.txt"], "冲突清单不对")

    code, j = ctx.srv.jget("/api/conflict/inspect?file=" + urllib.parse.quote("cf.txt"))
    ck_eq(code, 200, "/api/conflict/inspect 应 200")
    ck(j.get("regionCount", 0) >= 1, "inspect 没给出冲突块数")

    # 路径校验: 换了模块不等于少一道拦截
    for bad_f in ("../x", ".git/config", "cf.txt/../../x"):
        code, _ = ctx.srv.jget("/api/conflict/inspect?file="
                              + urllib.parse.quote(bad_f))
        ck_eq(code, 400, "inspect %r 期望 400" % bad_f)

    # 未解决时不许提交合并
    code, _ = ctx.srv.jpost("/api/conflict/commit", {"message": "m"})
    ck_eq(code, 400, "还有未合并路径时 /api/conflict/commit 期望 400")

    # 写路径 A: 整份取一侧
    code, j = ctx.srv.jpost("/api/conflict/choose",
                            {"file": "cf.txt", "side": "ours"})
    ck_eq(code, 200, "choose ours 应 200")
    ck_eq(j.get("stillUnmerged"), False, "choose 之后仍算未合并")
    ck_eq(r.read("cf.txt"), "main\n", "choose ours 落盘的内容不对")

    code, _ = ctx.srv.jpost("/api/conflict/choose",
                            {"file": "cf.txt", "side": "evil"})
    ck_eq(code, 400, "非法 side 期望 400")

    # 写路径 B: 用户编辑的内容真落盘。
    # `content` 的三种坏传法都必须 400 —— 模块里是 `(content or "").encode()`,
    # 漏传字段与「故意清空」在模块里同义, 是接线层必须分开的那一刀。
    marked = r.read("cg.txt")
    for body, why in (({"file": "cg.txt"}, "漏传 content"),
                      ({"file": "cg.txt", "content": None}, "content=null"),
                      ({"file": "cg.txt", "content": 42}, "content=数字")):
        code, _ = ctx.srv.jpost("/api/conflict/resolve", body)
        ck_eq(code, 400, "resolve %s 期望 400" % why)
        ck_eq(r.read("cg.txt"), marked, "resolve %s 却动了工作区" % why)

    # expect_sha256 必须真的透传。**必须在成功 resolve 之前打这一发** ——
    # resolve 之后该路径已不是未合并态, `resolve()` 里「有没有 U 条目」那道
    # 检查排在 sha256 检查**前面**, 报出来的是「不是未合并路径」, 根本走不到
    # sha256 那一步 —— 那样这条断言验证不了任何东西(这个坑我踩过两次)。
    code, txt = ctx.srv.raw("POST", "/api/conflict/resolve", json.dumps(
        {"file": "cg.txt", "content": "x\n", "expect_sha256": "0" * 64}))
    ck_eq(code, 400, "错的 expect_sha256 期望 400")
    ck("sha256" in txt, "报的错不是 sha256 不匹配,说明 expect_sha256 没接上: %r"
       % txt[:140])
    ck_eq(r.read("cg.txt"), marked, "expect_sha256 对不上却动了工作区")

    merged = "手工合并结果\n第二行\n"
    code, j = ctx.srv.jpost("/api/conflict/resolve",
                            {"file": "cg.txt", "content": merged})
    ck_eq(code, 200, "resolve 应 200")
    ck_eq(r.read("cg.txt"), merged, "resolve 落盘的内容不对")
    ck_eq(j.get("stillUnmerged"), False, "resolve 之后仍算未合并")

    code, j = ctx.srv.jget("/api/conflict/list")
    ck_eq(j.get("count"), 0, "解决完还有未合并文件")

    code, j = ctx.srv.jpost("/api/conflict/commit", {"message": "merge side"})
    ck_eq(code, 200, "提交合并应 200")
    ck_eq(j.get("committed"), True, "commit_merge 没说自己提交了")
    ck_eq(r.read("cg.txt"), merged, "提交后工作区内容被改了")
    ck_eq(ctx.srv.jget("/api/conflict/state")[1].get("inMerge"), False,
          "提交后还在合并态")

    # 合并已结束: 任何写入都必须被拒(模块的 require_merge)
    code, _ = ctx.srv.jpost("/api/conflict/resolve",
                            {"file": "cg.txt", "content": "y\n"})
    ck_eq(code, 400, "合并结束后 resolve 期望 400")
    ck_eq(r.read("cg.txt"), merged, "合并结束后 resolve 却动了工作区")
    ck(ctx.srv.alive(), "服务不健康")


@test("N", "history-routes-wired", bug="N2-接线")
def t_n2(ctx):
    """N2 `/api/history/*`:只读清单、预演、危险操作的两步护栏。

    危险操作一个都不真做(unconfirmed / wrong-sha / 非法名字 就该全被拒),
    因为这条测的是**接线与护栏**, 不是 git 的行为 —— `test_history_ops.py`
    37 条已经把模块本身测透了, 这里只保证产品够得着、且够着的方式是安全的。
    """
    r = ctx.repo
    r.write("h.txt", "one\n")
    r.commit("c1")
    r.git("branch", "spare")           # 有一条「已存在但不是当前分支」可预演删除
    r.write("h.txt", "two\n")
    r.write("untracked.txt", "u\n")

    # 预演分两类, 不能一律当 200:
    #   * 会给出影响面的 -> 200(哪怕 safe:false / blocked 非空, 那都是**信息**)
    #   * 一开始就没法做的 -> 400(分支不存在、工作区干净…) —— 那是模块的用法护栏,
    #     不是接线坏了。所以下面两条单独断言 400。
    for path, key in (("/api/history/reflog", None),
                      ("/api/history/tags", None),
                      ("/api/history/stashes", None),
                      ("/api/history/plan/amend", "op"),
                      ("/api/history/plan/undo-last-commit", "op"),
                      ("/api/history/plan/undo-last-commit?drop_changes=1", "op"),
                      ("/api/history/plan/revert?sha=HEAD", "op"),
                      ("/api/history/plan/branch-delete?name=spare", "op"),
                      ("/api/history/plan/stash", "op"),
                      ("/api/history/plan/stash?include_untracked=1", "op")):
        code, j = ctx.srv.jget(path)
        ck_eq(code, 200, "%s 应 200" % path)
        if key:
            ck(j.get(key), "%s 没返回 %r" % (path, key))
    # 分支不存在 -> 400: 预演也照样有用法护栏, 不是「一律返回 200 的空壳」
    code, _ = ctx.srv.jget("/api/history/plan/branch-delete?name=no-such-branch")
    ck_eq(code, 400, "预演删除一个不存在的分支期望 400")
    ck(isinstance(ctx.srv.jget("/api/history/reflog")[1], list),
       "reflog 应回一个数组")

    for path in ("/api/history/blame?file=h.txt",
                 "/api/history/file?file=h.txt"):
        code, j = ctx.srv.jget(path)
        ck_eq(code, 200, "%s 应 200" % path)
        ck(j.get("ok") is True, "%s 没回 ok" % path)
    code, _ = ctx.srv.jget("/api/history/blame?file=" + urllib.parse.quote("../x"))
    ck_eq(code, 400, "blame 越界期望 400")

    # op 来自 URL: 拼错一个字母必须 400, 而不是 getattr 到别的东西上
    code, _ = ctx.srv.jget("/api/history/plan/rm-rf")
    ck_eq(code, 400, "不存在的预演期望 400")
    code, _ = ctx.srv.jget("/api/history/plan/__init__")
    ck_eq(code, 400, "预演 op 不该能调到任意属性")

    # 两步护栏: 没 confirmed 就必须被拒
    for path, body in (("/api/history/amend", {"message": "x"}),
                       ("/api/history/stash/drop", {"index": 0}),
                       ("/api/history/branch/delete", {"name": "x"}),
                       ("/api/history/tag/delete", {"name": "x"}),
                       ("/api/history/undo-last-commit", {}),
                       ("/api/history/reflog/restore", {"index": 0})):
        code, txt = ctx.srv.raw("POST", path, json.dumps(body))
        ck_eq(code, 400, "%s 未确认期望 400" % path)
        ck("plan" in txt, "%s 的拒绝理由里没提到先看预演: %r" % (path, txt[:120]))

    # 布尔开关只认真 bool —— `"confirmed": "false"` 在 Python 里是真的,
    # 收得松一点就是把「前端把 false 序列化成字符串」变成一次误删
    for body in ({"index": 0, "confirmed": "true"},
                 {"index": 0, "confirmed": 1},
                 {"message": "w", "include_untracked": "true"}):
        path = ("/api/history/stash" if "include_untracked" in body
                else "/api/history/stash/drop")
        code, _ = ctx.srv.jpost(path, body)
        ck_eq(code, 400, "%s 收到非布尔 %r 期望 400" % (path, body))

    # 真做一步可恢复的(建 tag / stash / stash apply) —— 这几条不破坏历史
    code, j = ctx.srv.jpost("/api/history/tag/create",
                            {"name": "v1", "message": "m"})
    ck_eq(code, 200, "建 tag 应 200")
    ck(any(x.get("name") == "v1"
           for x in ctx.srv.jget("/api/history/tags")[1]), "tag 列表里没有 v1")
    code, _ = ctx.srv.jpost("/api/history/tag/create", {"name": "-x"})
    ck_eq(code, 400, "以 - 开头的 tag 名期望 400")

    code, j = ctx.srv.jpost("/api/history/stash",
                            {"message": "wip", "include_untracked": True})
    ck_eq(code, 200, "stash 应 200")
    ck_eq(r.out("status", "--porcelain").strip(), "", "stash 之后工作区还脏")
    code, j = ctx.srv.jpost("/api/history/stash/apply", {"index": 0})
    ck_eq(code, 200, "stash apply 应 200")
    ck(os.path.exists(os.path.join(r.dir, "untracked.txt")),
       "stash apply 没把文件贴回来")
    ck(ctx.srv.alive(), "服务不健康")


@test("N", "static-pages-served", bug="N3-接线")
def t_n3(ctx):
    """N3 冲突演示页在产品里必须打得到。

    以前 `web/conflicts-demo.html` 只存在于 test_aigit_web.py 自己起的那个
    静态服务器里, aigit 自己不发它 —— 于是「演示页能跑」这件事只在测试里成立。
    接口接上之后这页才有意义, 接上却不给人看等于没接。
    """
    for path, magic, ctype in (
            ("/conflicts-demo.html", "onclick", "text/html"),
            ("/virtuallist.js", "function", "application/javascript")):
        code, body, hdrs = ctx.srv.raw("GET", path, with_headers=True)
        ck_eq(code, 200, "GET %s 应 200" % path)
        ck(magic in body, "%s 内容不像 %r" % (path, magic))
        ck(ctype in (hdrs.get("Content-Type") or ""),
           "%s 的 Content-Type 应含 %r, 实际 %r"
           % (path, ctype, hdrs.get("Content-Type")))
    # 白名单之外一律 404: 静态文件**不按路径拼**, 所以这两个必须是「没有这个接口」
    for path in ("/conflicts.py", "/../server.py", "/web/../../server.py"):
        code, _ = ctx.srv.raw("GET", path)
        ck(code in (400, 404), "GET %s 返回 %s —— 不该发仓内文件" % (path, code))
    ck(ctx.srv.alive(), "服务不健康")


# ════════════════════════════════════════════════════════ 运行器

class Ctx:
    def __init__(self, repo=None, srv=None):
        self.repo = repo
        self.srv = srv
        self.notes = []
        self.detail = "(测试没走到断言就结束了)"

    def note(self, msg):
        self.notes.append(str(msg))

    def ctx_of(self, outer):
        """把当前 repo/srv 绑到一个外层 ctx 上(给 D5 那种要起第二个服务的测试)。"""
        c = Ctx(self.repo, self.srv)
        c.notes = outer.notes
        return c


def _slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def run_one(t, verbose, keep):
    repo = None
    srv = None
    ctx = Ctx()
    status = "?"
    try:
        # 目录名用 id, 不用 hash(): str 的 hash 每进程随机, 复现时对不上
        repo = TestRepo("t-%s" % _slug(t["id"]))
        srv = repo.server()
        ctx.repo, ctx.srv = repo, srv
        t["fn"](ctx)
        status = "PASS"
    except Skip as e:
        status, ctx.detail = "SKIP", str(e)
    except Fail as e:
        status, ctx.detail = "FAIL", str(e)
    except Exception as e:
        status, ctx.detail = "ERROR", "%s: %s" % (type(e).__name__, e)
    tb = "" if status in ("PASS", "FAIL", "SKIP") else traceback.format_exc(limit=8)
    if verbose:
        for n in ctx.notes:
            print("      . %s" % n)
    if status == "PASS":
        out = ("PASS", ctx, "")
    elif status == "SKIP":
        out = ("SKIP", ctx, "[跳过] " + ctx.detail)
    elif status == "FAIL":
        out = ("FAIL", ctx, ctx.detail)
    else:
        out = ("ERROR", ctx, ctx.detail + "\n" + tb)
    try:
        if srv:
            srv.stop()
        if repo and not keep:
            shutil.rmtree(repo.dir, ignore_errors=True)
            try:
                os.remove(repo.dir + ".srvlog")
            except Exception:
                pass
    except Exception:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 无头对抗性测试套件")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条显示细节")
    ap.add_argument("-k", "--filter", default="", help="只跑名字/编号含该串的")
    ap.add_argument("--keep", action="store_true", help="保留临时仓(调试用)")
    ap.add_argument("--list", action="store_true", help="只列出测试名")
    a = ap.parse_args(argv)

    sel = [t for t in TESTS if a.filter.lower() in (t["id"] + " " + t["name"]).lower()]
    if a.list:
        for t in sel:
            print("  %-14s %s%s" % (t["id"], t["name"],
                                    ("   [%s]" % t["bug"]) if t["bug"] else ""))
        return 0
    if not sel:
        print("没有测试匹配 %r" % a.filter)
        return 2

    shutil.rmtree(TESTROOT, ignore_errors=True)
    os.makedirs(TESTROOT, exist_ok=True)
    print("=" * 78)
    print("  aigit 无头对抗性测试套件")
    print("  临时仓根: %s" % TESTROOT)
    print("  aigit 源: %s" % AIGIT)
    print("  选中 %d / %d 条" % (len(sel), len(TESTS)))
    print("=" * 78)

    t0 = time.time()
    results = []
    cur_group = None
    for t in sel:
        g = t["id"].split("-")[0]
        if g != cur_group:
            cur_group = g
            print("\n[%s 组]" % g)
        st, ctx, detail = run_one(t, a.verbose, a.keep)
        results.append((t, st, detail, list(ctx.notes)))
        mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip ", "ERROR": "ERROR "}[st]
        line = "  %s %-14s %s" % (mark, t["id"], t["name"])
        if st != "PASS" and detail:
            line += "\n           -> %s" % detail.replace("\n", "\n           ")
        print(line)

    print()
    print("=" * 78)
    cnt = {}
    for _, st, _, _ in results:
        cnt[st] = cnt.get(st, 0) + 1
    print("  汇总: 共 %d 条   PASS %d   FAIL %d   SKIP %d   ERROR %d   用时 %.1fs"
          % (len(results), cnt.get("PASS", 0), cnt.get("FAIL", 0),
             cnt.get("SKIP", 0), cnt.get("ERROR", 0), time.time() - t0))
    bad = [(t, st, d) for t, st, d, _ in results if st in ("FAIL", "ERROR")]
    if bad:
        print()
        print("  失败清单(每一条都对应 server.py / gen.py 的一个缺陷):")
        for t, st, d in bad:
            print("    [%s] %-14s %-26s bug=%s" % (st, t["id"], t["name"], t["bug"] or "-"))
            print("           %s" % d.split("\n")[0][:150])
    print("=" * 78)
    # 根目录带 pid, 所以**只有自己**会 rmtree 它 —— 崩在半路的进程留下的目录
    # 也不会被别人顺手清掉, 得以留作现场。--keep 时故意留着。
    if not a.keep:
        shutil.rmtree(TESTROOT, ignore_errors=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())