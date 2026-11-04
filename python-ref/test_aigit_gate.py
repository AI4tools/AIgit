#!/usr/bin/env python3
"""aigit —— **破坏性操作的确认闸门**的拒绝路径测试(本文件的全部意义)。

为什么单独一个文件:
    `server.py` 给 `/api/reset` 和 `/api/discard` 加了确认闸门之后, 其它测试
    套件里那几十处调用方都被改成了「带 confirmed」—— 也就是**全部走的是放行
    路径**。于是「闸门还在不在」这件事在**任何**一个既有套件里都没有被测到。

    这正是闸门最容易消失的方式: 某次重构把 `if _bb(b, "confirmed") is not True`
    删掉, 整套测试**依然全绿** —— 因为没有一条测试会**不带** confirmed 去打这两个
    路由。加闸门容易; 证明「不带会被拒」才是闸门活过下一次重构的唯一凭据。

本文件只做两件事:
    1. 证明**每一个**拒绝分支都会拒(不只「没带 confirmed」这一种)。
    2. 证明放行路径**真的执行了**, 不是「一律拒绝」那种假修法
       —— 只测拒绝的话, 把 `REPO.discard()` / `REPO.reset()` 两行删掉也能全绿。

跑法:
    python test_aigit_gate.py              # 全部(约 1 分钟)
    python test_aigit_gate.py -v           # 逐条显示细节
    python test_aigit_gate.py -k string    # 只跑名字含 string 的
    python test_aigit_gate.py --red-proof  # **变异证明**: 自动把闸门拆掉重跑,
                                            # 证明本文件真的会红(见下)

`--red-proof` 是这份测试的自我审计, 不是装饰:
    它把 `server.py` 复制到临时目录, 在**副本**上做两处定点变异, 用副本起服务,
    然后重跑同一批用例。副本用完即删, **product server.py 一个字节都不动**。
      变异 A: 拆掉 reset/discard 的 confirmed 闸门与 mode 三态检查
      变异 B: 把 `_bb` 退化成 `bool()`(接受字符串 "false" 这种真值)
    预期: 变异 A 至少打红「不带 confirmed」那几条; 变异 B 必须打红字符串那几条。
    如果某次重构把闸门拆了而本文件还全绿, `--red-proof` 会立刻发现。

硬约束(与 test_aigit.py 同款):
    * 纯标准库, 不用 pytest。
    * **完全无头**。HTTP 走 urllib.request, 子进程输出重定向到文件。
    * 临时仓建在 %TEMP%\\aigit-gate-<pid>\\ 下, 绝不碰任何真实仓库。
      **必须带进程号**: `main()` 开头会 rmtree 整个 TESTROOT, 固定路径会让并发的
      两个本文件互相删仓, 症状是「上一遍还绿的这一遍变红」这种假 FAIL。
    * **只读** aigit 源。`--red-proof` 只改临时目录里的副本。
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
#: 默认打真的 server.py; `--red-proof` / 环境变量可以把这里换成**副本**,
#: 这样「把闸门拆掉会怎样」就能在不动产品代码的前提下实测。
PRODUCT_SERVER_PY = (os.environ.get("AIGIT_GATE_SERVER_PY")
                     or os.path.join(AIGIT, "server.py"))
#: **当前**要跑的那个 server.py。red_proof 循环里会临时把它指到变异体,
#: 所以必须和「产品文件」分开存 —— 否则第 2 个变异体会从第 1 个变异体改出来,
#: 两个对照长得一模一样(实测踩过: 两个都报「5/10 红」, 因为第二个根本没生效)。
SERVER_PY = PRODUCT_SERVER_PY
#: 副本落在别处时它 import 不到 conflicts / history_ops / gitops / gen(那些是
#: 按**脚本所在目录**进 sys.path 的)。所以给子进程补一条 PYTHONPATH 指向真正的
#: aigit 目录 —— 只对副本有意义, 对真 server.py 是无害的冗余。
SERVER_DIR = os.path.dirname(os.path.abspath(SERVER_PY))
SERVER_EXTRA_PATH = os.pathsep.join(
    [AIGIT] + ([SERVER_DIR] if os.path.normcase(SERVER_DIR) != os.path.normcase(AIGIT) else []))

TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-gate-%d" % os.getpid())

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
    """登记一条测试。"""
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
        raise Fail("%s: 期望 %r, 实际 %r" % (what, want, got))


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
        p = subprocess.run(["git", "-C", self.dir] + list(args),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=120)
        if kw.get("check", False) and p.returncode != 0:
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
    """起一个真的 server.py 子进程, 用 urllib 打。输出落文件, 不用 PIPE(免得写满死锁)。"""

    def __init__(self, repo):
        self.repo = repo
        self.log = os.path.join(repo.dir + ".srvlog")
        self.port = None
        self.proc = None
        env = dict(os.environ)
        if SERVER_EXTRA_PATH:
            env["PYTHONPATH"] = SERVER_EXTRA_PATH + os.pathsep + env.get("PYTHONPATH", "")
        for _ in range(6):
            port = free_port()
            lf = io.open(self.log, "w", encoding="utf-8", errors="replace")
            p = subprocess.Popen(
                [sys.executable, SERVER_PY, "--root", repo.dir, "--port", str(port)],
                stdout=lf, stderr=subprocess.STDOUT, cwd=AIGIT, env=env,
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
        raise RuntimeError("server 起不来, 日志: " + self.tail())

    def _wait(self, port, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                code, _body = self._raw(port, "GET", "/api/health")
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
    def _raw(self, port, method, path, body=None, ctype="application/json",
             headers=None, timeout=120, with_headers=False):
        data = body
        if isinstance(data, str):
            data = data.encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=data, method=method)
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
        """走 json.dumps —— 所以 Python 的 True 会变成 **JSON 的 true**。

        这正是本文件要测的那一位: `_bb` 只认真布尔, 字符串 "true"/"false"
        一律 400。所以客户端**必须**能发真 JSON 布尔; 如果哪天这里的 obj
        被拼成字符串拼 JSON, 整个拒绝路径测试就变成自欺欺人。
        """
        assert isinstance(obj, dict), obj
        return self.jpost_raw(path, json.dumps(obj))

    def jpost_raw(self, path, raw_body):
        code, txt = self.raw("POST", path, raw_body)
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("POST %s 返回非 JSON (code=%s): %r" % (path, code, txt[:200]))

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


# ════════════════════════════════════════════════════════ 共用夹具

class Fixture:
    """一条用例的仓。

    摆的是一个**三态都有的**工作区, 这样「拒了」和「执行了」都能从磁盘上看出来:
      * ``keep.txt``    已跟踪、被改过    -> hard reset 后必须回到 ``HEAD``
      * ``junk.txt``    未跟踪的文件      -> hard reset 的 ``clean -fd`` 后必须消失
      * ``junkdir/``    未跟踪的目录      -> 同上
      * ``.gitignore``  忽略 ``ignored.txt`` -> **不该**出现在预演里(clean -fd 不删它)
    """

    def __init__(self, repo, srv):
        self.repo = repo
        self.srv = srv

    @classmethod
    def build(cls, repo, srv):
        r = repo
        r.write("keep.txt", "HEAD-VERSION\n")
        r.write(".gitignore", "ignored.txt\n")
        r.commit("c1")
        r.write("keep.txt", "WORKING-MODIFIED\n")
        r.write("junk.txt", "junk\n")
        r.write("junkdir/inner.txt", "junk-inner\n")
        r.write("ignored.txt", "ignored\n")
        return cls(r, srv)

    # -- 磁盘上的「后果」断言 -------------------------------------------
    def worktree_intact(self):
        """破坏性动作**没有**发生: 未跟踪的还在, 已跟踪的改动还在。"""
        r = self.repo
        ck(r.exists("junk.txt"), "未跟踪文件 junk.txt 没了 —— 闸门没挡住, 操作被执行了")
        ck(r.exists("junkdir/inner.txt"), "未跟踪目录 junkdir 没了 —— 闸门没挡住")
        ck_eq(r.read("keep.txt"), "WORKING-MODIFIED\n",
              "已跟踪文件的改动被吞了 —— 闸门没挡住")

    def reset_ran_hard(self):
        """`reset --hard` **真的执行了**: 改动被还原 + clean -fd 清掉未跟踪。"""
        r = self.repo
        ck_eq(r.read("keep.txt"), "HEAD-VERSION\n", "reset 后 keep.txt 没回到 HEAD")
        ck(not r.exists("junk.txt"), "reset 后未跟踪文件 junk.txt 还在 —— clean -fd 没跑")
        ck(not r.exists("junkdir"), "reset 后未跟踪目录 junkdir 还在 —— clean -fd 没跑")
        # 被 .gitignore 忽略的文件 clean -fd 不删(要 -x 才是)。顺带钉一下:
        # 真删了的话说明这条用例量的不是 hard reset 的语义。
        ck(r.exists("ignored.txt"), "reset 把 .gitignore 忽略的文件也删了(那是 clean -fx 的语义)")

    def discard_ran_on(self, rel):
        """discard 指定的路径**真的**被处理了。"""
        ck(not self.repo.exists(rel), "discard 没有删掉 %s —— 放行路径没真执行" % rel)


def _unchanged_assertion(fx, ctx, what):
    """拒绝路径的统一后果断言: 什么都没动 + 服务还活着。"""
    fx.worktree_intact()
    ck(fx.srv.alive(), "%s 之后服务不健康" % what)
    ck(os.path.exists(os.path.join(fx.repo.dir, ".git")), "%s 之后 .git 没了" % what)


def plan_q(targets):
    """/api/discard/plan 的 files 参数拼法。

    **必须是重复的 query 参数**(`?files=a&files=b`), 不是 JSON 数组 ——
    服务端走的是 `as_file_list(q.get("files"))`, 而 `parse_qs` 的值天然是
    列表, 于是 `?files=["a","b"]` 会被当成**一个**名叫 `["a","b"]` 的路径,
    预演照常返回 200 + `action: "missing"`, 不报任何错。

    这个坑值得记一笔: 一个记错拼法的调用方会拿到「这个路径不存在」
    而不是「你参数给错了」, 于是它以为「没什么会被删」就直接让用户点了确认。
    所以下面每条用到 files 的用例都走这个函数, 不手写。
    """
    return "&".join("files=" + urllib.parse.quote(t) for t in targets)


# ════════════════════════════════════════════════════════ R 组:reset 的三态

@test("R", "reset-no-confirmed-400", bug="P0-闸门")
def t_r1(ctx):
    """R1 reset 不带 confirmed -> 400 + code=needs_confirmation, 且什么都没发生。

    这是闸门的主拒绝分支。同时断言**磁盘后果**: 只看状态码的话, 一条
    「返回 200 但什么也没做」的假实现也能过。
    """
    fx = Fixture.build(ctx.repo, ctx.srv)
    code, j = ctx.srv.jpost("/api/reset", {"mode": "hard"})
    ctx.note("POST /api/reset {\"mode\":\"hard\"} -> %s %s" % (code, j.get("code")))
    ck_eq(code, 400, "reset 不带 confirmed 应 400")
    ck_eq(j.get("ok"), False, "拒绝时 ok 应为 false")
    ck_eq(j.get("code"), "needs_confirmation",
          "前端要靠 code 分支('先看预演' vs '操作失败'), 不能靠 match 中文文案")
    ck_eq(j.get("op"), "reset", "响应里应带 op=reset 标明是哪个操作要确认")
    ck(bool(j.get("error")), "拒绝时必须给一句能显示给人看的话")
    # 指路的那句话必须真的指得到路: 闸门文案让用户「先 GET /api/reset/plan」,
    # 那个路由必须存在, 否则这条指路把人引向 404。
    code2, _j2 = ctx.srv.jget("/api/reset/plan?mode=hard")
    ck_eq(code2, 200, "闸门文案指路的 GET /api/reset/plan 居然打不通")
    _unchanged_assertion(fx, ctx, "reset 不带 confirmed")


@test("R", "reset-confirmed-no-mode-400", bug="P0-三态无默认")
def t_r2(ctx):
    """R2 reset 带了 confirmed 但**没给 mode** -> 400。

    刻意点名: `soft` / `mixed` / `hard` 决定「工作区的改动活不活」。给默认值
    就是**替用户做这个决定**, 所以缺省一律 400, 不给也不猜。

    这条同时钉住「不能靠加默认值让调用方过」—— 有的人会想让缺省等于 hard,
    那条 route 的 `mode not in (...)` 一旦放宽, 这里立刻红。
    """
    fx = Fixture.build(ctx.repo, ctx.srv)
    for body in ({"confirmed": True},
                 {"confirmed": True, "mode": None},
                 {"confirmed": True, "mode": ""},
                 {"confirmed": True, "mode": "HARD"},      # 大小写也不认
                 {"confirmed": True, "mode": "--hard"}):
        code, j = ctx.srv.jpost("/api/reset", body)
        ctx.note("POST /api/reset %r -> %s" % (body, code))
        ck_eq(code, 400, "reset 缺 mode(%r)应 400 —— 三态不许有默认值" % (body.get("mode"),))
        ck_eq(j.get("ok"), False, "拒绝时 ok 应为 false")
        # 缺 mode 是**用法**错误(走 GitError), 不是「等你确认」——
        # 所以这里不该回 needs_confirmation, 否则前端会一直弹确认框。
        ck(j.get("code") != "needs_confirmation",
           "缺 mode 应报「用法错误」而不是 needs_confirmation, 否则前端会反复弹确认框")
        ck(bool(j.get("error")), "拒绝时必须给一句能显示给人看的话")
    # 三态真的被接受(soft / mixed / hard 都是合法值)
    for mode in ("soft", "mixed", "hard"):
        code, _j = ctx.srv.jpost("/api/reset",
                                 {"confirmed": True, "mode": mode, "ref": "HEAD"})
        ck_eq(code, 200, "reset mode=%r 是合法三态之一, 应 200" % mode)
    fx.reset_ran_hard()


@test("R", "reset-confirmed-string-400", bug="P0-_bb-只认真布尔")
def t_r3(ctx):
    """R3 `confirmed` 给**字符串**(哪怕是 "true")-> 仍然 400。

    为什么这条比「不带 confirmed」更该测: `"confirmed": "false"` 在 Python 里
    是**真的**, 而它恰好是「确认执行一个不可撤销操作」的那一位。用 `bool(x)`
    收这个字段等于把「前端把 false 序列化成字符串」这个极常见的 bug 变成一次
    误删。`_bb` 就是为这件事存在的, 所以它必须有测试 —— 否则它随时可以
    被「顺手简化」回 `bool(b.get(...))`。

    顺带钉住 mode 与 confirmed 的**检查次序**: 两种畸形各自报自己的错,
    不该含糊成一句。
    """
    fx = Fixture.build(ctx.repo, ctx.srv)
    for bad in ("false", "true", "False", "0", "1", "", "yes", "null"):
        code, j = ctx.srv.jpost("/api/reset",
                                {"confirmed": bad, "mode": "hard"})
        ctx.note("confirmed=%r(字符串) -> %s %s" % (bad, code, str(j.get("error"))[:70]))
        ck_eq(code, 400, 'confirmed=%r 是字符串, 必须 400 —— _bb 只认真布尔' % bad)
        ck_eq(j.get("ok"), False, "拒绝时 ok 应为 false")
        ck(bool(j.get("error")), "拒绝时必须给一句能显示给人看的话")
    # 真 JSON 布尔 true 同一个接口就该放行 —— 上面那批不是「reset 一律拒绝」
    code, _j = ctx.srv.jpost("/api/reset", {"confirmed": True, "mode": "hard"})
    ck_eq(code, 200, "confirmed=true(JSON 布尔)应放行")
    fx.reset_ran_hard()


@test("R", "reset-confirmed-hard-runs", bug="P0-放行路径")
def t_r4(ctx):
    """R4 confirmed=true + mode=hard -> 200, 且**真的执行了**。

    这条是拒绝路径那几条的**对照组**, 少一条整套测试就有个致命洞:
    「把 `REPO.reset(...)` 那一行删掉」能让 R1/R2/R3 全绿, 而产品已经废了。
    所以「拒」与「做」必须成对出现。
    """
    fx = Fixture.build(ctx.repo, ctx.srv)
    code, j = ctx.srv.jpost("/api/reset", {"confirmed": True, "mode": "hard"})
    ctx.note("POST /api/reset {confirmed:true,mode:hard} -> %s ok=%s"
             % (code, j.get("ok")))
    ck_eq(code, 200, "reset 给了 confirmed+mode 应 200")
    ck_eq(j.get("ok"), True, "reset 放行时 ok 应为 true")
    fx.reset_ran_hard()          # 真的执行了(内容还原 + clean -fd 清掉未跟踪)
    ck(ctx.srv.alive(), "reset 之后服务不健康")
    ck(os.path.exists(os.path.join(ctx.repo.dir, ".git")), "reset 之后 .git 没了")


@test("R", "reset-soft-mixed-keeps-worktree", bug="P0-三态语义")
def t_r5(ctx):
    """R5 soft / mixed 走的是**它们自己的**语义, 不是 hard 的。

    这条是「mode 要按真实意图填」的判据依据: 三态确有区别。
      * soft  : HEAD 移到 ref, 改动进暂存区, 工作区文件不动
      * mixed : HEAD 移到 ref, 改动留在工作区, 暂存区清空
      * hard  : 连工作区一起还原, 再 clean -fd
    """
    # -- soft -------------------------------------------------------------
    # soft 只「移动 HEAD」, 所以 ref 必须真的比 HEAD 老, 否则它整个是 no-op,
    # 断言「改动进了暂存区」会永远失败 —— 而且是**假的**失败(语义没坏, 是没观察到)。
    # 这也是这条用例自己的一个教训: 测 reset 三态必须先让 HEAD 有得可退。
    r = ctx.repo
    r.write("keep.txt", "V1\n")
    r.commit("c1")
    r.write("keep.txt", "V2\n")
    r.commit("c2")                 # HEAD=c2(V2), 工作区干净

    code, _j = ctx.srv.jpost("/api/reset",
                             {"confirmed": True, "mode": "soft", "ref": "HEAD~1"})
    ck_eq(code, 200, "reset soft 应 200")
    ck_eq(r.read("keep.txt"), "V2\n", "soft 不该动工作区文件")
    ck_eq(r.out("diff", "--cached", "--name-only").split(), ["keep.txt"],
          "soft 应把 HEAD 退掉的那次提交留在暂存区")

    # -- mixed -----------------------------------------------------------
    # mixed 一定把索引重置到 ref —— 哪怕 ref 就是 HEAD(HEAD 不动, 索引照清)。
    # 所以这里 ref=HEAD 就是一次「只清索引、不动工作区」的纯操作, 语义清楚。
    r.write("keep.txt", "V3\n")     # 工作区 V3, 索引仍是 V2, HEAD 是 c1(V1)
    code, _j = ctx.srv.jpost("/api/reset",
                             {"confirmed": True, "mode": "mixed", "ref": "HEAD"})
    ck_eq(code, 200, "reset mixed 应 200")
    ck_eq(r.read("keep.txt"), "V3\n", "mixed 也不该动工作区文件")
    ck_eq(r.out("diff", "--cached", "--name-only").split(), [],
          "mixed 应把索引重置到 HEAD")

    # -- 三档合起来必须彼此不同 -----------------------------------------
    # 已经分别量过 soft / hard 的后果; 这里再钉一条最容易出的错:
    # 三档不许**悄悄**变成同一档(比如有人图省事让 mode 只影响错误文案)。
    r.write("keep.txt", "V4\n")
    r.write("junk.txt", "junk\n")
    code, _j = ctx.srv.jpost("/api/reset",
                             {"confirmed": True, "mode": "hard", "ref": "HEAD"})
    ck_eq(code, 200, "reset hard 应 200")
    ck(not r.exists("junk.txt"), "hard 应带 clean -fd 清掉未跟踪文件")
    ck(ctx.srv.alive(), "三态之后服务不健康")


# ════════════════════════════════════════════════════════ D 组:discard 的闸门

@test("D", "discard-no-confirmed-400", bug="P0-闸门")
def t_d1(ctx):
    """D1 discard 不带 confirmed -> 400 + code=needs_confirmation, **文件还在**。

    这是 README 里写着「会连带删掉所有未跟踪文件和目录」的那道门。
    后果断言是这条的全部价值: 只查状态码的话, 一条「返回 400 但先把文件删了」
    的实现照样绿。
    """
    r = ctx.repo
    r.write("keep.txt", "HEAD\n")
    r.commit("c1")
    r.write("junk.txt", "junk\n")
    r.write("junkdir/inner.txt", "junk-inner\n")

    for body in ({"files": ["junk.txt"]},              # 指名道姓
                 {"files": ["junkdir"]},
                 {"files": ["keep.txt"]},
                 {}):                                    # 连 files 都不给
        code, j = ctx.srv.jpost("/api/discard", body)
        ctx.note("POST /api/discard %r -> %s %s" % (body, code, j.get("code")))
        ck_eq(code, 400, "discard 不带 confirmed 应 400")
        ck_eq(j.get("ok"), False, "拒绝时 ok 应为 false")
        ck_eq(j.get("code"), "needs_confirmation", "拒绝必须带 code=needs_confirmation")
        ck_eq(j.get("op"), "discard", "响应里应带 op=discard")
        ck(bool(j.get("error")), "拒绝时必须给一句能显示给人看的话")
        ck(r.exists("junk.txt"), "junk.txt 没了 —— 闸门没挡住")
        ck(r.exists("junkdir/inner.txt"), "junkdir 没了 —— 闸门没挡住")
        ck_eq(r.read("keep.txt"), "HEAD\n", "已跟踪文件的改动被吞了 —— 闸门没挡住")
        ck(ctx.srv.alive(), "discard 不带 confirmed 之后服务不健康")
    # 闸门文案指的那条路必须打得通
    code2, _j2 = ctx.srv.jget("/api/discard/plan")
    ck_eq(code2, 200, "闸门文案指路的 GET /api/discard/plan 居然打不通")


@test("D", "discard-confirmed-string-400", bug="P0-_bb-只认真布尔")
def t_d2(ctx):
    """D2 discard 的 confirmed 给字符串 -> 400(理由同 R3)。

    「前端把布尔序列化成字符串」在这里的后果比 reset 更重: discard 会**真的删
    文件**, 而且删掉的正是用户还没提交的东西。
    """
    r = ctx.repo
    r.write("keep.txt", "HEAD\n")
    r.commit("c1")
    r.write("junk.txt", "junk\n")
    for bad in ("false", "true", "False", "1", "", "yes"):
        code, j = ctx.srv.jpost("/api/discard", {"files": ["junk.txt"],
                                                  "confirmed": bad})
        ctx.note("discard confirmed=%r -> %s" % (bad, code))
        ck_eq(code, 400, 'discard confirmed=%r 是字符串, 必须 400' % bad)
        ck(r.exists("junk.txt"), "confirmed=%r 却把 junk.txt 删了" % bad)
    # 对照组: 真 JSON 布尔就放行
    code, _j = ctx.srv.jpost("/api/discard", {"files": ["junk.txt"],
                                              "confirmed": True})
    ck_eq(code, 200, "discard confirmed=true(JSON 布尔)应放行")
    ck(not r.exists("junk.txt"), "放行之后 junk.txt 应当真被删掉")
    ck(ctx.srv.alive(), "服务不健康")


@test("D", "discard-confirmed-runs", bug="P0-放行路径")
def t_d3(ctx):
    """D3 confirmed=true -> 200, 且预演说的**确实发生了**。

    「预演说 A、实删 B」比没有预演更危险 —— 用户照着预演点了确认。所以这条
    把 plan 与实删钉在一起: plan 怎么分类, 执行就必须是那个结果。
      * 未跟踪文件  -> delete-file  -> 真没了
      * 未跟踪目录  -> delete-tree  -> 真没了(整棵树)
      * 已跟踪文件  -> restore      -> 真回到 HEAD
    """
    r = ctx.repo
    r.write("keep.txt", "HEAD\n")
    r.write("tracked_dir/in.txt", "in-head\n")
    r.commit("c1")
    r.write("keep.txt", "MODIFIED\n")
    r.write("junk.txt", "junk\n")
    r.write("junkdir/a.txt", "a\n")
    r.write("junkdir/nested/b.txt", "b\n")

    targets = ["junk.txt", "junkdir", "keep.txt"]
    code, plan = ctx.srv.jget("/api/discard/plan?" + plan_q(targets))
    ck_eq(code, 200, "discard/plan 应 200")
    actions = {e["path"]: e["action"] for e in plan.get("entries", [])}
    ctx.note("plan 分类: %r" % (actions,))
    ck_eq(actions.get("junk.txt"), "delete-file", "plan 对未跟踪文件的分类不对")
    ck_eq(actions.get("junkdir"), "delete-tree", "plan 对未跟踪目录的分类不对")
    ck_eq(actions.get("keep.txt"), "restore", "plan 对已跟踪文件的分类不对")

    code, _j = ctx.srv.jpost("/api/discard",
                             {"files": targets, "confirmed": True})
    ck_eq(code, 200, "discard 给了 confirmed 应 200")
    # 预演说的三件事, 一件一件核对
    ck(not r.exists("junk.txt"), "plan 说 delete-file, 实际 junk.txt 还在")
    ck(not r.exists("junkdir"), "plan 说 delete-tree, 实际 junkdir 还在(含 nested/)")
    ck_eq(r.read("keep.txt"), "HEAD\n", "plan 说 restore, 实际 keep.txt 没回到 HEAD")
    ck(r.exists("tracked_dir/in.txt"), "没点名的已跟踪文件不该被动")
    ck(ctx.srv.alive(), "服务不健康")


# ════════════════════════════════════════════════════════ P 组:预演

@test("P", "discard-plan-enumerates-untracked", bug="P0-预演")
def t_p1(ctx):
    """P1 GET /api/discard/plan 逐个列出**每一个**会被删的未跟踪文件/目录。

    为什么必须逐个: 前端那个「丢弃全部改动」按钮**根本不传 files**
    (语义等价于 `git clean -fd`), 所以要用的不是 `entries` 而是
    `untracked_total` / `untracked_count`。而 `entries` 也不能是空的 ——
    预演存在的意义就是「真删了哪些」能一条条摆出来, 让用户看清再点。

    判据是**和磁盘/git 逐条对账**, 不是「字段存在」: 一个只回 untracked_count
    而不列内容的实现也能骗过前一种写法。
    """
    r = ctx.repo
    r.write("keep.txt", "HEAD\n")
    r.write("tracked_dir/in.txt", "in-head\n")
    r.write(".gitignore", "ignored.txt\nignored_dir/\n")
    r.commit("c1")
    # 未跟踪: 两个文件 + 一个平铺目录 + 一个嵌套目录
    r.write("junk.txt", "junk\n")
    r.write("loose2.txt", "junk2\n")
    r.write("junkdir/a.txt", "a\n")
    r.write("junkdir/nested/b.txt", "b\n")
    # 被 ignore 的: `clean -fd` 不删它们, 预演**不该**虚报损失
    r.write("ignored.txt", "ignored\n")
    r.write("ignored_dir/x.txt", "ignored\n")

    code, j = ctx.srv.jget("/api/discard/plan")
    ck_eq(code, 200, "GET /api/discard/plan 应 200")
    ck_eq(j.get("ok"), True, "discard/plan 的 ok 应为 true")

    total = j.get("untracked_total")
    ck(isinstance(total, list), "discard/plan 必须逐个列出未跟踪项, 实际 %r"
       % (type(total).__name__,))
    ck_eq(j.get("untracked_count"), len(total),
          "untracked_count 与 untracked_total 对不上")

    # 和 git 自己算的集合逐条对账(服务端走的是
    # `git ls-files --others --exclude-standard --directory --no-empty-directory`)
    want = sorted(p for p in r.out("ls-files", "--others", "--exclude-standard",
                                   "--directory", "--no-empty-directory").split()
                  if p.strip())
    ck_eq(total, want, "预演列出的未跟踪项与 git 算的不一致")
    ctx.note("未跟踪项(%d): %r" % (len(total), total))

    # 注意形状: `git ls-files --others --directory` 给的目录是**带尾斜杠**的
    # ("junkdir/"), 而同一个响应的 `entries` 里同一个目录是不带斜杠的
    # ("junkdir", 因为那边走的是 Path.relative_to().as_posix())。
    # 两个字段形状不一致本身值得记一笔(见报告), 这里如实按实测形状断言。
    total_norm = sorted(x.rstrip("/") for x in total)
    for expect in ("junk.txt", "loose2.txt", "junkdir"):
        ck(expect in total_norm,
           "预演漏列了 %r —— 用户点确认之前看不到它要被删, 实际列的是 %r"
           % (expect, total))
    for absent in ("keep.txt", "tracked_dir/in.txt"):
        ck(absent not in total_norm, "预演把已跟踪文件 %r 算成会删 —— 虚报损失" % absent)
    for ig in ("ignored.txt", "ignored_dir"):
        ck(ig not in total_norm,
           "预演把 .gitignore 忽略的 %r 算成会删 —— `clean -fd` 不删它, 这是虚报" % ig)

    # 目录按**一棵树**列, 不是把里面每个文件摊平
    ck("junkdir/nested/b.txt" not in total_norm,
       "预演把未跟踪目录里的文件摊平列了 —— 用户会以为要删 %d 个文件, 实际是删一整个目录"
       % len([t for t in total_norm if t.startswith("junkdir/")]))

    # 显式点名 files 时, 逐个说明「会怎么死」(已跟踪/未跟踪文件/未跟踪目录三分)
    targets = ["junk.txt", "junkdir", "keep.txt", "tracked_dir", "no-such.txt"]
    code, j2 = ctx.srv.jget("/api/discard/plan?" + plan_q(targets))
    ck_eq(code, 200, "discard/plan?files= 应 200")
    got = {e["path"]: e["action"] for e in j2.get("entries", [])}
    ctx.note("entries: %r  blocked: %r" % (got, j2.get("blocked")))
    ck_eq(got.get("junk.txt"), "delete-file", "未跟踪文件应分类为 delete-file")
    ck_eq(got.get("junkdir"), "delete-tree", "未跟踪目录应分类为 delete-tree")
    ck_eq(got.get("keep.txt"), "restore", "已跟踪文件应分类为 restore")
    ck_eq(got.get("no-such.txt"), "missing", "不存在的路径应分类为 missing")
    # 含已跟踪文件的目录走的是**另一个列表**(blocked), 不在 entries 里 ——
    # 这一点值得钉死: 它的语义是「整条拒绝」(不删目录里的已跟踪文件),
    # 和 delete-tree 完全不是一回事, 前端必须分开显示。
    blocked = {b["path"] for b in j2.get("blocked", [])}
    ck("tracked_dir" not in got,
       "含已跟踪文件的目录被写进了 entries(它不会被 delete-tree), 实际 %r" % got)
    ck("tracked_dir" in blocked, "blocked 列表里应有 tracked_dir, 实际 %r" % (blocked,))
    ck_eq(len(blocked), 1, "blocked 里不该有多余项: %r" % (blocked,))
    ck_eq(j2.get("untracked_count"), j.get("untracked_count"),
          "点名 files 不该改变仓里未跟踪项的总数")

    ck(ctx.srv.alive(), "预演之后服务不健康")
    ck(r.exists("junk.txt"), "预演是只读的 —— junk.txt 不该被动")
    ck(r.exists("junkdir/nested/b.txt"), "预演是只读的 —— junkdir 不该被动")
    ck_eq(r.read("keep.txt"), "HEAD\n", "预演是只读的 —— 已跟踪文件不该被动")
    ck(r.exists("ignored.txt"), "预演是只读的 —— 忽略文件不该被动")


@test("P", "reset-plan-requires-mode", bug="P0-预演")
def t_p2(ctx):
    """P2 reset/plan 是只读的, 且 mode 必填(与 POST 侧同一个三态规矩)。

    顺带量出 soft/mixed/hard 预演的差别, 让「为什么要显式给 mode」有依据。
    """
    r = ctx.repo
    r.write("keep.txt", "HEAD\n")
    r.commit("c1")
    r.write("keep.txt", "MODIFIED\n")

    code, j = ctx.srv.jget("/api/reset/plan")
    ck_eq(code, 400, "reset/plan 缺 mode 应 400 —— 和 POST 侧同一个三态规矩")
    notes = {}
    for mode in ("soft", "mixed", "hard"):
        code, j = ctx.srv.jget("/api/reset/plan?mode=" + mode)
        ck_eq(code, 200, "reset/plan?mode=%s 应 200" % mode)
        ck_eq(j.get("ok"), True, "reset/plan 的 ok 应为 true")
        ck_eq(j.get("mode"), mode, "预演回显的 mode 不对")
        ck("note" in j, "预演必须用一句话说清这一档会怎么样, 实际 %r" % (sorted(j),))
        notes[mode] = j["note"]
    ctx.note("三档说明: %r" % (notes,))
    ck_eq(len(set(notes.values())), 3, "soft/mixed/hard 三档的说明完全一样 —— 分不清")
    ck_eq(r.read("keep.txt"), "MODIFIED\n", "预演是只读的 —— 工作区不该被动")
    ck(os.path.exists(os.path.join(r.dir, ".git")), "预演是只读的 —— .git 不该被动")


# ════════════════════════════════════════════════════════ 运行器

class Ctx:
    def __init__(self, repo=None, srv=None):
        self.repo = repo
        self.srv = srv
        self.notes = []
        self.detail = "(测试没走到断言就结束了)"

    def note(self, msg):
        self.notes.append(str(msg))


def _slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def run_one(t, verbose):
    repo = srv = None
    ctx = Ctx()
    status = "?"
    try:
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
    tb = "" if status in ("PASS", "FAIL", "SKIP") else traceback.format_exc(limit=10)
    if verbose:
        for n in ctx.notes:
            print("      . %s" % n)
    if status == "PASS":
        out = ("PASS", ctx, "")
    elif status == "SKIP":
        out = ("SKIP", ctx, "[跳过] " + ctx.detail)
    else:
        out = (status, ctx, (ctx.detail + ("\n" + tb if tb else "")))
    # 无论如何都要收摊: 否则起着的子进程会一直占端口
    if srv is not None:
        srv.stop()
    if repo is not None:
        shutil.rmtree(repo.dir, ignore_errors=True)
    return out + (list(ctx.notes),)


# ──────────────────────────────────────────────────── 变异体(只改副本)

#: 变异 A: 把 reset / discard 的闸门整段拆掉。
#: 这是「下一次重构把闸门删了」的最忠实模拟 —— 也是最危险的那种: 产品功能
#: 全都还在, 一切照常工作, 只是不再需要确认。
_MUT_NO_GATE = [
    ('''                if _bb(b, "confirmed") is not True:
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "discard",''',
     '''                if False:      # MUTANT: gate removed
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "discard",'''),
    ('''                mode = b.get("mode")
                if mode not in ("soft", "mixed", "hard"):
                    raise GitError(''',
     '''                mode = b.get("mode") or "hard"    # MUTANT: a default was added
                if mode not in ("soft", "mixed", "hard"):
                    raise GitError('''),
    ('''                if _bb(b, "confirmed") is not True:
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "reset",''',
     '''                if False:      # MUTANT: gate removed
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "reset",'''),
]

#: 变异 B: `_bb` 退化成 `bool()` —— 也就是大多数人会写的那个版本。
#: 只测「不带 confirmed」的话, 这个变异**永远测不出来**: 不带的时候
#: `bool(None)` 和 `bool(False)` 都是 False, 一样被拒。区别只在字符串。
_MUT_LAX_BOOL = [
    ('''    v = b.get(key, default)
    if isinstance(v, bool):
        return v
    raise GitError("%s 必须是 true 或 false,收到 %r" % (key, v))''',
     '''    return bool(b.get(key, default))'''),
]

#: 变异对照表: (tag, patches, 说明, 必须变红的用例, 必须保持绿的用例)
#:
#: `must_stay_green` 不是摆设 —— 变异 B 那一列才是本文件存在理由的证明:
#: 「不带 confirmed」那几条在 `_bb` 退化成 `bool()` 之后**照样绿**
#: (因为 `bool(None)` 和 `bool(False)` 都是 False, 一样被拒)。也就是说
#: 如果本文件只有「不带 confirmed」的用例, `_bb` 随时可以被人「顺手简化」
#: 成 `bool(b.get("confirmed"))` 而**没有任何测试变红** —— 那道防误删的闸门
#: 就只剩下一层薄壳。能抓住它的只有字符串那几条。
MUTANTS = [
    ("nogate", _MUT_NO_GATE,
     "把 confirmed 闸门 + mode 三态检查整段拆掉(最危险的静默回归)",
     ["R-reset-no-confirmed-400", "R-reset-confirmed-no-mode-400",
      "R-reset-confirmed-string-400", "D-discard-no-confirmed-400",
      "D-discard-confirmed-string-400"],
     # 闸门拆了, 放行路径与只读预演不该受影响 —— 它们红了才说明用例本身脆
     ["R-reset-confirmed-hard-runs", "R-reset-soft-mixed-keeps-worktree",
      "D-discard-confirmed-runs", "P-discard-plan-enumerates-untracked",
      "P-reset-plan-requires-mode"]),
    ("laxbool", _MUT_LAX_BOOL,
     '_bb 退化成 bool() —— 字符串 "confirmed":"false" 会被当成真值放行',
     ["R-reset-confirmed-string-400", "D-discard-confirmed-string-400"],
     # 关键约束: 这两条**必须还是绿的**。它们绿, 才说明变异 B 是靠字符串用例
     # 抓到的; 它们也红, 就说明这份对照分不出「_bb 被弱化」和「闸门被拆掉」。
     ["R-reset-no-confirmed-400", "D-discard-no-confirmed-400",
      "R-reset-confirmed-no-mode-400", "R-reset-confirmed-hard-runs",
      "D-discard-confirmed-runs", "P-discard-plan-enumerates-untracked",
      "P-reset-plan-requires-mode"]),
]


def make_mutant(tag, patches):
    """把**产品** server.py 复制到临时目录并做定点变异, 返回副本路径。

    源文件固定是 ``PRODUCT_SERVER_PY``, **不是**当前的 ``SERVER_PY`` ——
    踩过的坑: 原来这里读的是全局 ``SERVER_PY``, 而 red_proof 循环里上一轮已经
    把它指到「上一轮的变异体」上了。于是第 2 个变异体是**从第 1 个变异体**改出来的,
    两个变异体的红绿完全一样 —— 看起来「都红 5 条」很有说服力, 实际上第 2 个
    根本没验证自己的补丁。两个一模一样的对照等于没有对照。

    **只读产品文件, 一个字节都不改。** 找不到 patch 锚点就直接报错 ——
    宁可让 --red-proof 失败, 也不要「没变异成功却报红证明通过」。
    """
    with io.open(PRODUCT_SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    for old, new in patches:
        if src.count(old) != 1:
            raise Fail("变异 %s: 锚点在 server.py 里出现 %d 次(要求恰好 1 次), "
                       "server.py 变了形状, 锚点要跟着更新" % (tag, src.count(old)))
        src = src.replace(old, new, 1)
    out = os.path.join(TESTROOT, "mutants", "server_%s.py" % tag)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return out


def red_proof(sel, verbose):
    """对每个变异体重跑同一批用例, 要求它**恰好**在声明的用例上变红。

    为什么不是「至少红一条」就够了: 那只证明「本文件能变红」, 不证明
    「**它测的是这一件事**」。每个变异体都带一份 `must_break`(必须红)与
    `must_stay_green`(必须绿)清单, 两条都核对:

      * 变异 A 拆闸门  -> 「不带 confirmed」「缺 mode」那几条必须红。
      * 变异 B 把 _bb 退化成 bool() -> **只有**字符串那几条必须红, 而
        「不带 confirmed」那几条**必须保持绿**。这条约束是这份对照的关键:
        它证明字符串用例测的确实是 `_bb` 的语义(而不是被闸门的 400 顺带
        挡住的), 同时也证明「不带 confirmed」那几条**单独存在是不够的** ——
        没有字符串用例, 变异 B 会全程绿灯。

    任何一个 must_break 没红 / must_stay_green 变红, 都算对照失败(rc=1)。
    """
    global SERVER_PY, SERVER_EXTRA_PATH
    orig_py, orig_extra = SERVER_PY, SERVER_EXTRA_PATH
    verdicts = []
    try:
        for tag, patches, why, must_break, must_green in MUTANTS:
            try:
                SERVER_PY = make_mutant(tag, patches)
            except Fail as e:
                print("  变异 %-8s 构造失败: %s" % (tag, e))
                verdicts.append((tag, why, "CONSTRUCTION-FAILED", [], [], sel))
                continue
            SERVER_EXTRA_PATH = os.pathsep.join(
                [AIGIT, os.path.dirname(SERVER_PY)])
            print("\n" + "=" * 78)
            print("  变异体 %s —— %s" % (tag, why))
            print("  源(产品 server.py): %s" % PRODUCT_SERVER_PY)
            print("  副本: %s" % SERVER_PY)
            print("=" * 78)
            results = {}
            for t in sel:
                st, _ctx, detail, _notes = run_one(t, False)
                results[t["id"]] = (st, detail)
                print("  %-5s %-40s %s"
                      % (st, t["id"], (detail or "").strip().split("\n")[0][:88]))
            red = sorted(i for i, (st, _d) in results.items() if st != "PASS")
            broke = sorted(set(red) & set(must_break))
            missed = sorted(set(must_break) - set(red))
            collateral = sorted(set(red) - set(must_break))
            stayed = sorted(i for i in must_green if results.get(i, ("PASS",))[0] == "PASS")
            lost = sorted(set(must_green) - set(stayed))
            print("  -> 变异 %s: 红 %d / %d 条  %s"
                  % (tag, len(red), len(sel), ("(" + ", ".join(red) + ")") if red else ""))
            print("     声明必须红: %s" % (("全红(%d)" % len(broke)) if not missed
                                         else "!! 漏红 %s" % missed))
            print("     声明必须绿: %s" % (("全绿(%d)" % len(stayed)) if not lost
                                         else "!! 误红 %s" % lost))
            if collateral:
                print("     (附带变红, 未声明: %s —— 属预期外, 值得看一眼)" % collateral)
            ok = (not missed) and (not lost)
            verdicts.append((tag, why, ("RED" if ok else "PROOF-FAILED"),
                             red, missed + lost, sel))
    finally:
        SERVER_PY, SERVER_EXTRA_PATH = orig_py, orig_extra
    return verdicts


def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 破坏性操作确认闸门的拒绝路径测试")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条显示细节")
    ap.add_argument("-k", "--filter", default="", help="只跑名字/编号含该串的")
    ap.add_argument("--list", action="store_true", help="只列出测试名")
    ap.add_argument("--red-proof", action="store_true",
                    help="额外做变异对照: 拆掉闸门重跑, 证明本文件真的会红"
                         "(只改临时目录里的 server.py 副本)")
    a = ap.parse_args(argv)

    sel = [t for t in TESTS if a.filter.lower() in (t["id"] + " " + t["name"]).lower()]
    if a.list:
        for t in sel:
            print("  %-40s %s" % (t["id"], t["name"]))
        return 0
    if not sel:
        print("没有测试匹配 %r" % a.filter)
        return 2

    shutil.rmtree(TESTROOT, ignore_errors=True)
    os.makedirs(TESTROOT, exist_ok=True)
    print("=" * 78)
    print("  aigit 破坏性操作确认闸门 —— 拒绝路径测试")
    print("  临时仓根: %s" % TESTROOT)
    print("  服务源码: %s" % SERVER_PY)
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
        st, ctx, detail, notes = run_one(t, a.verbose)
        results.append((t, st, detail, notes))
        mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip ",
                "ERROR": "ERROR "}[st]
        line = "  %s %-40s %s" % (mark, t["id"], t["name"])
        if st != "PASS" and detail:
            line += "\n           -> %s" % detail.replace("\n", "\n           ")
        print(line)

    print()
    print("=" * 78)
    cnt = {}
    for _t, st, _d, _n in results:
        cnt[st] = cnt.get(st, 0) + 1
    print("  汇总: 共 %d 条   PASS %d   FAIL %d   SKIP %d   ERROR %d   用时 %.1fs"
          % (len(results), cnt.get("PASS", 0), cnt.get("FAIL", 0),
             cnt.get("SKIP", 0), cnt.get("ERROR", 0), time.time() - t0))
    bad = [(t, st, d) for t, st, d, _ in results if st in ("FAIL", "ERROR")]
    for t, st, d in bad:
        print("    [%s] %-40s %s" % (st, t["id"], d.split("\n")[0][:150]))
    print("=" * 78)

    rc = 0 if not bad else 1
    if a.red_proof:
        import hashlib
        before = hashlib.sha256(
            io.open(PRODUCT_SERVER_PY, "rb").read()).hexdigest()
        verdicts = red_proof(sel, a.verbose)
        after = hashlib.sha256(
            io.open(PRODUCT_SERVER_PY, "rb").read()).hexdigest()
        print()
        print("=" * 78)
        print("  变异对照结论")
        for tag, why, verdict, red, miss, _tot in verdicts:
            print("    %-9s %-14s 红 %d 条" % (tag, verdict, len(red)))
            print("               %s" % why)
            if miss:
                print("               !! 未按声明变红/变绿: %s" % miss)
        print("    产品 server.py sha256 收尾复核: %s" % after[:32])
        print("                            变异前: %s" % before[:32])
        ck_ok = before == after
        print("                            %s" % ("一致 —— 产品文件未被改动" if ck_ok
                                          else "!! 不一致 —— 变异体泄漏到产品文件了"))
        silent = [v for v in verdicts if v[2] != "RED"]
        if silent:
            print("    !! %d 个变异对照没有按声明生效 —— 本文件测不出它, "
                  "或变异构造失败" % len(silent))
            rc = 1
        if not ck_ok:
            rc = 1
        print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
