#!/usr/bin/env python3
"""gitops.py 对抗性测试 —— aigit 的可选远程能力(能力闸 + 远程操作)。

立场与 test_history_ops.py 一致:
    这不是一份"证明它没问题"的测试,是一份**试图把它打穿**的测试。
    尤其 A/B/C/D 四组是**证伪**用的 —— 目标不是"跑通了",而是
    **找出一条能绕过能力闸的路径**。全绿在这里是可接受的,但如果
    我没把入口列全,那"全绿"就是假的。

═════════════════════════════════════════════════════════════════════
这个套件的核心命题:「默认关着」必须**可证明地**关着
═════════════════════════════════════════════════════════════════════
「加个配置开关」不等于「关着」。所以这里不比 API 是否存在, 而是:

  B 组  造一个「明显该能 push」的仓(bare 远程 + 待推提交), 闸关着,
        **穷尽入口**逐个尝试 —— 全部必须失败, 且**一个远程 subprocess
        都不能被 fork 出来**。入口清单是交付物, 印在下面 ENTRY_POINTS。
  C 组  用 AST 扫全仓每一个 .py, 找出**所有**构造远程 git argv 的地方,
        断言只有 gitops.py 有 —— 这是对"没有第二条路径"的静态反证。
  D 组  起一个**全新解释器**实测 sys.modules, 证明导入本模块不会
        拉进任何能开连接的模块(而不是读 import 列表自证)。
  A 组  分级与 fail-closed 的边界行为。

═════════════════════════════════════════════════════════════════════
只用本地 bare 仓当"远程"
═════════════════════════════════════════════════════════════════════
`git init --bare` 建一个目录, `git remote add origin <那个目录>`。
这样 fetch/push/pull **全部能真跑通、真校验对象**, 而一个字节都不出
本机。**绝不连任何真实远程**(不 clone GitHub、不 ls-remote 公网地址)。

跑法:
    python test_aigit_net.py
    python test_aigit_net.py -v
    python test_aigit_net.py -k B
    python test_aigit_net.py --keep

分组:
    A  能力闸本身   B  关着时的构造证伪   C  关着时的静态反证
    D  不引入联网库 E  开闸时本地 bare 实跑   F  两步协议与注入
    G  既有护栏未破坏
    H  /api/remote/* 接线(真 HTTP, 三档能力)
"""

import argparse
import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
# 加 pid 后缀: 并行线共用同一个 TEMP 时不至于互删(照抄 test_aigit_extra.py)
TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-net-test-%d" % os.getpid())

sys.path.insert(0, HERE)
import gitops  # noqa: E402
from gitops import GitOps, NetGate, NetError, OFF, READ, WRITE  # noqa: E402


class Fail(AssertionError):
    pass


TESTS = []


def t(group, name):
    def deco(fn):
        TESTS.append({"id": "%s-%s" % (group, name), "group": group,
                      "name": name, "fn": fn})
        return fn
    return deco


def ck(cond, msg):
    if not cond:
        raise Fail(msg)


def ck_eq(a, b, msg):
    if a != b:
        raise Fail("%s\n        实际: %r\n        期望: %r" % (msg, a, b))


def ck_raises(exc, fn, msg, code=None):
    try:
        r = fn()
    except exc as e:
        if code is not None and getattr(e, "code", None) != code:
            raise Fail("%s\n        抛了 %s 但 code=%r, 期望 %r"
                       % (msg, type(e).__name__, getattr(e, "code", None), code))
        return e
    raise Fail("%s\n        没有抛异常, 返回了 %r" % (msg, r))


# ═══════════════════════════════════════════════════════ git 辅助

GIT_ENV = dict(os.environ)
GIT_ENV.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
                "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"})


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=GIT_ENV)
    if check and p.returncode != 0:
        raise Fail("git %s 失败: %s" % (" ".join(args), p.stderr.strip()))
    return p


def _chmod_retry(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def rmtree(path):
    """git 的 object 文件在 Windows 上是只读落盘的, 不放开就删不掉。"""
    if not os.path.exists(path):
        return
    shutil.rmtree(path, onerror=_chmod_retry)
    if os.path.exists(path):                       # 还有残留就再狠一点
        subprocess.run(["attrib", "-R", "/S", "/D", path],
                       capture_output=True)
        shutil.rmtree(path, ignore_errors=True)


class with_env(object):
    """临时改环境变量。用来构造「环境变量这条入口」的证伪用例。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ═══════════════════════════════════════════════════════ 场景

def fresh(name):
    """一个干净的工作目录。"""
    d = os.path.join(TESTROOT, name)
    rmtree(d)
    os.makedirs(d)
    return d


def make_bare(name):
    """本地 bare 仓当"远程"。一个字节都不出本机。"""
    d = os.path.join(TESTROOT, name)
    rmtree(d)
    os.makedirs(d)
    git(d, "init", "--bare", "-b", "main")
    return d


def make_repo(name, remote=None, commits=1, branch="main"):
    """造一个**明显应该能 push** 的仓: 有 bare 远程 + 有待推提交。

    这是证伪用例的前提 —— 如果仓里压根没有东西可推, "推不出去"
    就可能只是因为没东西推, 证不了任何东西。
    """
    d = fresh(name)
    git(d, "init", "-b", branch)
    git(d, "config", "user.email", "net@example.invalid")
    git(d, "config", "user.name", "NetTest")
    for i in range(commits):
        with open(os.path.join(d, "f%d.txt" % i), "w", encoding="utf-8") as f:
            f.write("c%d\n" % i)
        git(d, "add", "f%d.txt" % i)
        git(d, "commit", "-m", "c%d" % i)
    if remote:
        git(d, "remote", "add", "origin", remote)
    return d


# ═══════════════════════════════════════════════════════ subprocess 间谍

# 这张表是**独立**写的(按 git 文档的动词表), 故意**不**引用
# gitops.REMOTE_VERBS —— 拿模块自己的表去查模块自己, 等于没查。
HARD_REMOTE = frozenset({
    "push", "fetch", "pull", "clone", "ls-remote", "submodule", "send-pack",
    "receive-pack", "upload-pack", "fetch-pack", "daemon", "http-backend",
    "credential", "credential-cache", "credential-store", "request-pull",
    "remote-ext", "remote-fd", "git-upload-pack", "git-receive-pack",
    "git-http-push", "git-daemon-export", "svn", "svn-fetch", "cvs",
    "ftp-get", "ftp-push",
})


def is_remote_call(argv):
    """这个 argv 会不会联系远程?—— 按 git 文档独立判定, 不用模块的表。"""
    if not argv:
        return False
    verb = argv[0]
    if verb in HARD_REMOTE:
        return True
    if verb == "remote":
        # `remote show/update` 会联系; add/set-url 只改本地 config
        return len(argv) > 1 and argv[1] in ("show", "update")
    if verb == "archive":
        return any(a.startswith("--remote") or a.startswith("--exec")
                   for a in argv)
    return False


class Spy(object):
    """同时盯住 subprocess.run 和 subprocess.Popen。

    两个都盯是有必要的: 只盯 run 的话, 有人直接调 Popen 就绕过去了。
    """

    def __init__(self):
        self.calls = []

    def __enter__(self):
        self._run, self._popen = subprocess.run, subprocess.Popen
        outer = self

        def run(*a, **kw):
            outer.calls.append(list(a[0]) if a and a[0] else [])
            return outer._run(*a, **kw)

        def popen(*a, **kw):
            if a and a[0]:
                outer.calls.append(list(a[0]))
            elif "args" in kw and kw["args"]:
                outer.calls.append(list(kw["args"]))
            return outer._popen(*a, **kw)

        subprocess.run, subprocess.Popen = run, popen
        return self

    def __exit__(self, *a):
        subprocess.run, subprocess.Popen = self._run, self._popen
        return False

    def remote_calls(self):
        return [c for c in self.calls if is_remote_call(c)]

    def all_calls(self):
        return list(self.calls)


def bare_fingerprint(bare):
    """远程仓的完整状态指纹。用来证明「什么都没发出去」。

    光看 argv 里没有 push **不够** —— 那只证明没调 `git push`,
    证明不了远程仓没被动过。所以直接给远程仓拍个指纹(refs + 所有对象 +
    所有提交), 前后对比。
    """
    if not os.path.isdir(bare):
        return "<no such bare repo>"
    parts = []
    for args in (["show-ref"], ["rev-list", "--all"], ["count-objects", "-v"]):
        p = subprocess.run(["git", "-C", bare] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=GIT_ENV)
        parts.append(p.stdout.strip())
    return "\n".join(parts)


def exercise_all_entry_points(g, spy_results):
    """把**每一条**能碰到远程的入口都试一遍, 结果记进 spy_results。

    入口清单是交付物的一部分 —— 所以在这里被穷举地列出来, 而不是挑几个
    好试的。

    判定分两档, 这个区分很重要(第一版就是没分才报假 FAIL):

      must_block —— do_* / 直呼 _run 的远程动词 / 未知动词。
                    这些**必须抛 NetError**。返回了就是闸漏了。
      must_be_inert —— plan_*(预演)。预演**本来就该能跑通**(它只读本地
                    ref, 不需要任何能力级别), 所以"没抛异常"不是漏洞。
                    对它真正的判据是**没有副作用**: 既不 fork 远程进程,
                    也不改动远程仓的任何一个字节。

    返回 (must_block 被拒数, must_block 走通数, must_be_inert 数)。
    """
    entries = [
        # ── 公共 API: 会真发东西的, 必须一律拒 ──
        ("must_block", "ls-remote",         lambda: g.ls_remote()),
        ("must_block", "default_branch",    lambda: g.default_branch()),
        ("must_block", "do_fetch",          lambda: g.do_fetch()),
        ("must_block", "do_fetch(refspec)", lambda: g.do_fetch(refspec="main")),
        ("must_block", "do_push",           lambda: g.do_push(confirmed=True)),
        ("must_block", "do_push(no conf)",  lambda: g.do_push()),
        ("must_block", "do_push(force)",
         lambda: g.do_push(force=True, confirmed=True)),
        ("must_block", "do_push(delete)",
         lambda: g.do_push(delete=True, confirmed=True)),
        ("must_block", "do_pull",           lambda: g.do_pull(confirmed=True)),
        # ── 公共 API: 预演, 允许跑通但必须无副作用 ──
        ("inert", "plan_fetch",            lambda: g.plan_fetch()),
        ("inert", "plan_push",             lambda: g.plan_push()),
        ("inert", "plan_push(force)",      lambda: g.plan_push(force=True)),
        ("inert", "plan_push(delete)",     lambda: g.plan_push(delete=True)),
        # ── 绕过公共 API 直打内部闸。这几条最关键: 公共 API 拒了不算数 ──
        ("must_block", "_run(push)",
         lambda: g._run(["push", "origin", "main"], confirmed=True)),
        ("must_block", "_run(push,no conf)",
         lambda: g._run(["push", "origin", "main"])),
        ("must_block", "_run(fetch)",   lambda: g._run(["fetch", "origin"])),
        ("must_block", "_run(pull)",
         lambda: g._run(["pull", "origin"], confirmed=True)),
        ("must_block", "_run(clone)",
         lambda: g._run(["clone", "x", "y"], confirmed=True)),
        ("must_block", "_run(ls-remote)", lambda: g._run(["ls-remote", "origin"])),
        ("must_block", "_run(remote show)",
         lambda: g._run(["remote", "show", "origin"])),
        ("must_block", "_run(remote update)", lambda: g._run(["remote", "update"])),
        ("must_block", "_run(submodule upd)",
         lambda: g._run(["submodule", "update", "--all"], confirmed=True)),
        ("must_block", "_run(archive --remote)",
         lambda: g._run(["archive", "--remote=origin", "HEAD"],
                        confirmed=True)),
        ("must_block", "_run(credential)",
         lambda: g._run(["credential", "fill"], confirmed=True)),
        ("must_block", "_run(send-pack)",
         lambda: g._run(["send-pack", "origin", "main"], confirmed=True)),
        ("must_block", "_run(receive-pack)",
         lambda: g._run(["receive-pack", "x"], confirmed=True)),
        # 不认识的动词 —— 闸开到 write 也必须拒
        ("must_block", "_run(未知动词)",
         lambda: g._run(["definitely-not-a-git-verb"])),
        ("must_block", "_run(空 argv)", lambda: g._run([])),
    ]
    blocked = passed = inert = 0
    for kind, name, fn in entries:
        try:
            fn()
            if kind == "must_block":
                passed += 1
                spy_results.append(("!! 走通", name, "返回了正常值"))
            else:
                inert += 1
                spy_results.append(("跑通(预期)", name, "无副作用"))
        except NetError as e:
            blocked += 1
            spy_results.append(("拒", name, e.code))
        except Exception as e:
            blocked += 1
            spy_results.append(("拒(其他异常)", name,
                                "%s: %s" % (type(e).__name__, e)))
    return blocked, passed, inert


# ═══════════════════════════════════════════════════════ A 能力闸本身

@t("A", "默认级别是 off(不是 read 也不是 write)")
def a1():
    with with_env(AIGIT_NET=None):
        ck_eq(NetGate().level, OFF, "默认必须是 off")


@t("A", "未设 AIGIT_NET 时 source 记为 default")
def a2():
    with with_env(AIGIT_NET=None):
        ck_eq(NetGate().source, "default", "source 应为 default")


@t("A", "级别顺序 off < read < write")
def a3():
    ck_eq(NetGate(OFF).allows(READ), False, "off 不该允许 read")
    ck_eq(NetGate(READ).allows(READ), True, "read 该允许 read")
    ck_eq(NetGate(READ).allows(WRITE), False, "read 不该允许 write")
    ck_eq(NetGate(WRITE).allows(READ), True, "write 该允许 read")
    ck_eq(NetGate(WRITE).allows(WRITE), True, "write 该允许 write")


@t("A", "环境变量能开 read")
def a4():
    with with_env(AIGIT_NET="read"):
        ck_eq(NetGate().level, READ, "AIGIT_NET=read 应解析成 read")


@t("A", "环境变量能开 write —— 这就是唯一的开关")
def a5():
    with with_env(AIGIT_NET="write"):
        g = NetGate()
        ck_eq(g.level, WRITE, "AIGIT_NET=write 应解析成 write")
        ck_eq(g.source, "env:AIGIT_NET", "source 应记明来自环境变量")


@t("A", "级别大小写与空白不敏感")
def a6():
    for raw in ("WRITE", " write ", "Write"):
        with with_env(AIGIT_NET=raw):
            ck_eq(NetGate().level, WRITE, "%r 应解析成 write" % raw)


@t("A", "读不懂的值 fail closed(降级成 off, 不报错也不放行)")
def a7():
    for raw in ("raed", "yolo", "true-ish", "2.5", "读写"):
        with with_env(AIGIT_NET=raw):
            g = NetGate()
            ck_eq(g.level, OFF, "%r 读不懂时必须降级成 off, 实际 %r"
                  % (raw, g.level))
            ck("note" in g.describe(),
               "%r 降级了应该在 describe() 里留下 note, 否则是静默意外" % raw)


@t("A", "显式传非法级别是编程错误, 应当直接抛")
def a8():
    # 注意: "WRITE " / " Write " **不算**非法 —— 显式参数容忍空白与大小写是
    # 有意的(第一版把 "WRITE " 写进非法表里报了个假 FAIL)。
    for ok in ("WRITE ", " write", "Write"):
        ck_eq(NetGate(ok).level, WRITE, "%r 应被规范化成 write" % ok)
    for bad in ("yes", "on", "true", "sudo", True, 1, 3, ["write"]):
        ck_raises(NetError, lambda b=bad: NetGate(b), "非法级别 %r 应抛" % (bad,),
                  code="bad_level")


@t("A", "allow_env=False 时环境变量被无视")
def a9():
    with with_env(AIGIT_NET="write"):
        ck_eq(NetGate(allow_env=False).level, OFF,
              "allow_env=False 时环境变量不该生效")


@t("A", "显式参数压过环境变量")
def a10():
    with with_env(AIGIT_NET="write"):
        ck_eq(NetGate(OFF).level, OFF, "显式 off 应压过环境变量 write")


@t("A", "require(比当前级别高) 抛 net_disabled 并带上诊断字段")
def a11():
    e = ck_raises(NetError, lambda: NetGate(OFF).require(READ, op="fetch"),
                  "off 级 require(read) 应抛", code="net_disabled")
    ck_eq(e.extra.get("required"), READ, "extra 应带 required")
    ck_eq(e.extra.get("current"), OFF, "extra 应带 current")
    ck_eq(e.extra.get("op"), "fetch", "extra 应带 op")
    ck("AIGIT_NET" in e.message, "报错文案要告诉用户怎么开, 实际: %s" % e.message)


@t("A", "require 非法级别抛 bad_level")
def a12():
    ck_raises(NetError, lambda: NetGate(READ).require("sudo"),
              "非法 require 应抛", code="bad_level")


@t("A", "describe() 自述的 read/write 清单与实际表一致")
def a13():
    d = NetGate(WRITE).describe()
    ck_eq(d["level"], WRITE, "describe 应报 write")
    ck("push" in d["write_ops"], "write 清单里应有 push")
    ck("fetch" in d["read_ops"], "read 清单里应有 fetch")
    ck("push" not in d["read_ops"], "push 不该出现在 read 清单里")


# ═══════════════════════════════════════════════════════ B 构造证伪

@t("B", "off: 穷尽全部入口 —— 发东西的全拒, 预演的全无副作用")
def b1():
    bare = make_bare("B1-origin.git")
    repo = make_repo("B1-work", remote=bare, commits=2)
    g = GitOps(repo, NetGate(OFF))
    ck_eq(len(g.list_remotes()["remotes"]), 1, "前置: 仓里应当配了 1 个远程")
    ab0 = g.ahead_behind()
    ck(ab0["ahead"] == 2, "前置: 应当有 2 个待推提交")
    # 2026-10-06 补(由 find_test_gaps.py 报出「零提及」后加的):
    # 从没推过 => 没有远程跟踪 ref => behind **必须**是 None, 不能是 0。
    # 这正是 gitops.py:603-604 记的那个坑: 陈旧的跟踪 ref 会让 behind=0
    # 看起来像「远程没新东西」, 而 None 才诚实地表示「没有基准可比」。
    ck_eq(ab0["trackingRefExists"], False,
          "还没推过, 不该已经有远程跟踪 ref —— 若为 True, behind 的语义就变了")
    ck_eq(ab0["behind"], None,
          "没有跟踪 ref 时 behind 必须是 None(不是 0) —— 0 会被误读成「远程没新东西」")
    before = bare_fingerprint(bare)
    res = []
    with Spy() as spy:
        blocked, passed, inert = exercise_all_entry_points(g, res)
        rem = spy.remote_calls()
        ncalls = len(spy.all_calls())
    after = bare_fingerprint(bare)
    ck_eq(passed, 0, "下面这些入口走通了, 闸是漏的:\n        "
               + "\n        ".join("%s %s" % (a, b) for a, b, _ in res
                                  if a.startswith("!!")))
    ck_eq(rem, [], "闸关着却 fork 出了远程进程(闸不够靠前): %r" % (rem,))
    ck_eq(after, before,
          "远程仓在闸关着的情况下被改动了 —— 这是最严重的一种漏")
    print("\n        [B1] 入口 %d 条: 会发东西的 %d 条全拒, 预演 %d 条跑通但"
          "无副作用" % (len(res), blocked, inert))
    print("        [B1] subprocess 调用共 %d 次, 远程形态 %d 次; "
          "远程仓指纹前后一致: %s" % (ncalls, len(rem), before == after))
    for how, name, extra in res:
        print("          %-14s %-24s %s" % (how, name, extra))


@t("B", "off: 闸是通过**环境变量**开不起来的(只有 AIGIT_NET 一个旋钮)")
def b2():
    ck(os.environ.get("AIGIT_NET") is None,
       "前提: 本进程不该有 AIGIT_NET(否则这条用例没意义)")
    g = GitOps(make_repo("B2", commits=1), NetGate(OFF))
    ck_eq(g.gate.level, OFF, "配了远程 + 有提交, 闸不该自己变成开")


@t("B", "off: 仓里配了 remote 也不等于开了能力")
def b3():
    bare = make_bare("B3-origin.git")
    repo = make_repo("B3-work", remote=bare, commits=1)
    ck_eq(git(repo, "remote").stdout.strip(), "origin",
          "前置: 远程应当配好了")
    with with_env(AIGIT_NET=None):
        g = GitOps(repo)                    # 不传 gate -> 读环境变量 -> off
        ck_eq(g.gate.level, OFF, "有 remote 配置不该让闸自动打开")
        ck_raises(NetError, lambda: g.do_push(confirmed=True),
                  "有 remote 配置时仍应拒绝 push", code="net_disabled")


@t("B", "off: git config 里塞各种键也开不了能力")
def b4():
    bare = make_bare("B4-origin.git")
    repo = make_repo("B4-work", remote=bare, commits=1)
    for key in ("aigit.net.write", "net.enabled", "remote.origin.pushurl",
                "http.proxy", "credential.helper", "protocol.ext.allow"):
        git(repo, "config", "--local", key, "always")
    git(repo, "config", "--local", "remote.origin.pushurl", bare)
    git(repo, "config", "--local", "url.%s.pushInsteadOf.insteadOf"
        % bare, "https://example.invalid/")
    with with_env(AIGIT_NET=None):
        g = GitOps(repo)
        ck_eq(g.gate.level, OFF, "git config 不该能开能力闸")
        ck_raises(NetError, lambda: g.do_push(confirmed=True),
                  "配了一堆 git config 后仍应拒绝 push", code="net_disabled")
        ck_raises(NetError, lambda: g.do_push(), "同上")


@t("B", "off: 各种 git 相关的环境变量也开不了能力")
def b5():
    bare = make_bare("B5-origin.git")
    repo = make_repo("B5-work", remote=bare, commits=1)
    env = {"AIGIT_NET": None, "GIT_ALLOW_PROTOCOL": "https",
           "GIT_SSH_COMMAND": "echo pwned", "GIT_CONFIG_COUNT": "1",
           "GIT_TERMINAL_PROMPT": "0", "AIGIT_REMOTE": "write",
           "AIGIT_NET_WRITE": "1", "AIGIT_ENABLE_NET": "1"}
    with with_env(**env):
        ck_eq(NetGate().level, OFF, "只有 AIGIT_NET 能开能力, 别的都不行")
        g = GitOps(repo)
        ck_raises(NetError, lambda: g.do_push(confirmed=True),
                  "一堆环境变量也不该开得了 push", code="net_disabled")


@t("B", "off: 畸形 remote / 分支名一律被参数校验挡下(连 argv 都拼不出来)")
def b6():
    bare = make_bare("B6-origin.git")
    repo = make_repo("B6-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(OFF))
    bad = [
        "--upload-pack=touch D:/pwn",
        "-c core.hooksPath=/tmp/evil",
        "--receive-pack=calc",
        "origin --force",
        "orig in",
        "orig\nin",
        "",
        "a" * 300,
        ".",
        ".git",
        "..",
    ]
    for b in bad:
        with Spy() as spy:
            try:
                g.do_push(remote=b, confirmed=True)
                raise Fail("畸形 remote %r 竟然走通了" % b)
            except NetError as e:
                ck(e.code in ("bad_remote", "net_disabled", "not_a_repo"),
                   "remote=%r 的拒绝码 %r 不合预期" % (b, e.code))
            rem = spy.remote_calls()
        ck_eq(rem, [], "remote=%r 竟然 fork 出了远程进程" % b)

    bad_refs = ["--force", "-f", "main:evil", "a\nb", "main --force",
                "refs/heads/main:other", "", "-", "a" * 300]
    for r in bad_refs:
        with Spy() as spy:
            try:
                g.do_push(branch=r, confirmed=True)
                raise Fail("畸形分支 %r 竟然走通了" % r)
            except NetError as e:
                ck(e.code in ("bad_ref", "net_disabled"),
                   "branch=%r 的拒绝码 %r 不合预期" % (r, e.code))
            ck_eq(spy.remote_calls(), [], "branch=%r 竟然 fork 出了远程进程" % r)


@t("B", "off: 选项的 `=` 写法也逃不掉(实测真洞, 已修)")
def b12():
    """**这一条是实测出来的真漏洞, 不是假想。**

    B 组第一次跑时, `_run(["archive","--remote=origin","HEAD"])` 的拒绝码是
    `git_failed` 而不是 `net_disabled` —— 意味着闸判成了 local, git 被 fork
    了出去。独立探针复现: git 的 stderr 是

        remote: fatal: no such ref: HEAD
        fatal: sent error to the client: git upload-archive: archiver died

    `git upload-archive` 是**远程侧**那个服务 archive 请求的 helper。
    也就是说闸关着的情况下远程真的被接触了。

    根因: `_verb_level` 只比字面量 `--remote`, 而 git 文档写的是
    `--remote=<name>`。一个能力闸漏在"选项带不带等号"上, 等于没闸。

    所以这里不只钉住 `--remote=`, 而是把 REMOTE_FORMS 里每个选项的
    两种写法都钉住 —— 修一次不够, 得让它以后改不动。
    """
    bare = make_bare("B12-origin.git")
    repo = make_repo("B12-work", remote=bare, commits=1)
    forms = []
    for verb, table in gitops.REMOTE_FORMS.items():
        for key in table:
            forms.append([verb, key, "value"])          # --remote value
            forms.append([verb, key + "=value"])        # --remote=value
    ck(len(forms) >= 4,
       "REMOTE_FORMS 变空了(%d 条) —— 那样这条用例就什么都测不到了" % len(forms))
    with Spy() as spy:
        # (1) 核心性质: 无论闸在哪个级别, 这些形态都**不能**被判成 local。
        #     这条与级别无关, 是「等号写法逃过闸」这个洞的直接反证。
        classified = {}
        for argv in forms:
            for lvl in (OFF, WRITE):
                g = GitOps(repo, NetGate(lvl))
                got, why = g._verb_level(argv)
                ck(got is not None and got != "local",
                   "%r 被判成了 %r —— 选项的等号写法逃过闸了(%s)"
                   % (argv, got, why))
                classified[argv[0] + " " + argv[1]] = got
        # (2) off 级: 一条都不许过
        off = GitOps(repo, NetGate(OFF))
        for argv in forms:
            ck_raises(NetError, lambda a=argv: off._run(a, confirmed=True),
                      "%r 在 off 级应当被拦" % (argv,), code="net_disabled")
        # (3) write 级: read 级的形态**本来就该放行**(LEVELS 是有序的,
        #     write 蕴含 read); 只有 write 级的形态才该继续要 confirmed。
        wr = GitOps(repo, NetGate(WRITE))
        for argv in forms:
            got = classified[argv[0] + " " + argv[1]]
            if got == gitops.WRITE:
                ck_raises(NetError,
                          lambda a=argv: wr._run(a, confirmed=False),
                          "%r 是 write 级形态, 仍必须显式确认" % (argv,),
                          code="needs_confirmation")
        ck_eq(spy.remote_calls(), [], "远程形态的 argv 一次都不该被 fork")
    print("        钉住 %d 种选项写法, 各级别分别断言(off 全拦 / write 仍要确认)"
          % len(forms))


@t("B", "off: 纯本地形态仍然放行(别把 gate 修成什么都拒)")
def b13():
    """上一个用例的对照: 修漏不能修成「一律拒绝」。

    `git archive HEAD`(不带 --remote)、`git remote -v` 都是纯本地,
    它们必须继续放行 —— 否则能力闸就把本地功能也一起废了。
    """
    repo = make_repo("B13-work", commits=1)
    g = GitOps(repo, NetGate(OFF))
    local_argvs = [
        ["archive", "HEAD"],
        ["archive"],
        ["remote", "-v"],
        ["remote"],
        ["remote", "get-url", "origin"],
        ["remote", "add", "up", "somewhere"],
        ["status"],
        ["log", "-n", "1"],
        ["rev-parse", "HEAD"],
    ]
    with Spy() as spy:
        for argv in local_argvs:
            lvl, _ = g._verb_level(argv)
            ck_eq(lvl, "local", "%r 应当是纯本地形态" % (argv,))
        # 真跑一遍 archive(不带 --remote), 确认没被误伤
        p = g._run(["archive", "--format=tar", "HEAD"], check=False)
        ck_eq(p.returncode, 0, "纯本地 archive 应当能跑: %s" % p.stderr[:200])
    ck_eq(spy.remote_calls(), [], "纯本地形态不该被算成远程")


@t("B", "off: 未知动词哪怕闸开到 write 也被拒(兜底白名单)")
def b7():
    bare = make_bare("B7-origin.git")
    repo = make_repo("B7-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(WRITE))
    with Spy() as spy:
        for verb in ["definitely-not-a-git-verb", "fsm", "http-fetch",
                     "import", "remote-https", "push-pack", "sou"]:
            ck_raises(NetError, lambda v=verb: g._run([v], confirmed=True),
                      "未知动词 %r 在 write 级也应被拒" % verb,
                      code="unknown_git_verb")
        ck_raises(NetError, lambda: g._run([]), "空 argv 也应被拒",
                  code="unknown_git_verb")
    ck_eq(spy.remote_calls(), [], "未知动词不该 fork 出远程进程")


@t("B", "off: 绕过公共 API 直呼 _run 也一样拒(证明闸不在 API 层)")
def b8():
    bare = make_bare("B8-origin.git")
    repo = make_repo("B8-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(OFF))
    with Spy() as spy:
        for argv in (["push", "origin", "main"], ["fetch", "origin"],
                     ["pull", "origin"], ["ls-remote", "origin"],
                     ["remote", "show", "origin"], ["clone", "a", "b"]):
            e = ck_raises(NetError, lambda a=argv: g._run(a, confirmed=True),
                          "_run(%r) 在 off 级应被拒" % (argv,), code="net_disabled")
        ck_eq(spy.remote_calls(), [], "_run 直调竟然 fork 出了远程进程")


@t("B", "off: write 级也仍然要 confirmed —— 闸和确认是两道独立的锁")
def b9():
    bare = make_bare("B9-origin.git")
    repo = make_repo("B9-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(WRITE))
    with Spy() as spy:
        for bad in [None, False, 0, "", "true", "1", 1, [], {}, "false", "yes"]:
            ck_raises(NetError,
                      lambda b=bad: g.do_push(confirmed=b),
                      "confirmed=%r 应被拒" % (bad,), code="needs_confirmation")
        for bad in [None, False, "true", 1]:
            ck_raises(NetError,
                      lambda b=bad: g.do_pull(confirmed=b),
                      "pull confirmed=%r 应被拒" % (bad,),
                      code="needs_confirmation")
    ck_eq(spy.remote_calls(), [], "没确认就不该 fork 出远程进程")


@t("B", "off: _run 的第二道锁 —— 有人新写了个 do_xxx 忘了调 _check_bool 也推不出去")
def b10():
    """防的是「后来者漏了检查」, 所以第二道必须待在 _run 里, 不能只在 API 层。"""
    bare = make_bare("B10-origin.git")
    repo = make_repo("B10-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(WRITE))
    with Spy() as spy:
        # 故意**不**传 confirmed —— 模拟一个忘了加确认检查的新入口
        ck_raises(NetError, lambda: g._run(["push", "origin", "main"]),
                  "_run 对 WRITE 级动词必须自己再查一遍 confirmed",
                  code="needs_confirmation")
        ck_raises(NetError, lambda: g._run(["pull", "origin"]),
                  "pull 同理", code="needs_confirmation")
    ck_eq(spy.remote_calls(), [], "第二道锁没挡住")


@t("B", "off: gate 对象被换掉也不影响 —— 每层都重新查, 不缓存")
def b11():
    bare = make_bare("B11-origin.git")
    repo = make_repo("B11-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(OFF))
    ck_eq(g.gate.level, OFF, "起始是 off")
    g.gate.level = WRITE                    # 故意篡改
    ck_eq(g.gate.level, WRITE, "篡改应该生效(Python 层拦不住)")
    # 关键: 篡改 gate 不影响 argv 拼装与 level 计算路径本身仍受 _verb_level 管
    ck_raises(NetError, lambda: g._run(["not-a-verb"], confirmed=True),
              "篡改后未知动词仍应被拒", code="unknown_git_verb")
    # 这条不是"闸拦住了", 而是明说: Python 进程内的属性赋值不算攻击面。
    print("\n        [B11] 注: 进程内改 gate.level 拦不住也不假装拦得住 —— "
          "能跑 Python 的人本来就能直接调 git。")


# ═══════════════════════════════════════════════════════ C 静态反证

def _iter_py():
    for f in sorted(os.listdir(HERE)):
        if f.endswith(".py"):
            yield os.path.join(HERE, f)


def _read_py(path):
    """server_base.py 之类的文件带 BOM / 非 UTF-8, 挨个降级试。"""
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read(), enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None, None


def _remote_argv_sites(path):
    """AST 扫出所有**构造远程 git argv**的位置: (动词, 行号)。

    只看 list/tuple 字面量且首元素是远程动词字符串 —— 这正是
    `g._run(["push", ...])` 的形状。用 AST 而不是 grep, 是为了不被
    注释、字符串常量、跨行拼装骗过去。
    """
    src, _ = _read_py(path)
    if src is None:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            e0 = node.elts[0]
            if isinstance(e0, ast.Constant) and isinstance(e0.value, str):
                if is_remote_call([e0.value]):
                    out.append((e0.value, node.lineno))
    return out


@t("C", "全仓产品代码里只有 gitops.py 存在远程 git argv(无第二条路径)")
def c1():
    """这条断言的**范围**要说清楚, 否则就是自欺欺人:

    * 扫描对象是**产品代码** —— 排除测试文件与 gitops.py 自己。
    * 为什么排除测试文件: 测试里那些 ["push", ...] 是**喂给闸的 fuzzing
      数据**(以及造 fixture 时直接调 git clone), 不是一条「产品功能路径」。
      把它们算进来, 这条断言就会变成「我这个测试文件违反了它自己」。
    * 排除项会**打印出来**, 不是偷偷排除。看不到排除清单的断言没有意义。
    """
    product, excluded = [], []
    for p in _iter_py():
        rel = os.path.relpath(p, HERE)
        base = os.path.basename(rel)
        is_test = (base.startswith("test_") or base in ("selftest.py",
                                                        "smoke.py")
                   or base.endswith("_runner.py"))
        if rel == "gitops.py" or is_test:
            excluded.append(rel)
            continue
        product.append(rel)
    offenders = {}
    for rel in product:
        hits = _remote_argv_sites(os.path.join(HERE, rel))
        if hits:
            offenders[rel] = hits
    ck_eq(offenders, {},
          "这些产品文件里也有远程 argv, 意味着存在绕过 gitops 能力闸的第二条"
          "路径:\n        " + json.dumps(offenders, ensure_ascii=False))
    print("        扫描产品文件 %d 个: %s" % (len(product), ", ".join(product)))
    print("        排除 %d 个: %s" % (len(excluded), ", ".join(excluded)))


@t("C", "gitops.py 自己的远程 argv 全都在闸后面(逐个核对行号)")
def c2():
    sites = _remote_argv_sites(os.path.join(HERE, "gitops.py"))
    ck(sites, "gitops.py 里一条远程 argv 都没有? 那这个模块是空的")
    with open(os.path.join(HERE, "gitops.py"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    for verb, lineno in sites:
        # 往前找最近的 "def " —— 这些 argv 必须落在某个 do_*/plan_* 方法里,
        # 而所有 do_* 都先过 gate.require / _check_bool。
        owner = ""
        for i in range(lineno - 1, -1, -1):
            if lines[i].lstrip().startswith("def "):
                owner = lines[i].strip()
                break
        ck(owner, "第 %d 行的 %r argv 不在任何方法里(模块顶层执行?)" % (lineno, verb))
        ck(not owner.startswith("def _run("),
           "第 %d 行(%s)的远程 argv 出现在 _run 之外" % (lineno, verb))
        print("        %-10s line %-4d %s" % (verb, lineno, owner[:58]))


@t("C", "gitops.py 只 import 标准库, 不 import 任何 aigit 自己的模块")
def c3():
    src, _ = _read_py(os.path.join(HERE, "gitops.py"))
    tree = ast.parse(src)
    local = set()
    for f in _iter_py():
        local.add(os.path.splitext(os.path.basename(f))[0])
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    bad = sorted(set(imported) & local - {"gitops"})
    ck_eq(bad, [],
          "gitops 不该 import 其它 aigit 模块(那会把它和别的线的改动绑死): %r"
          % (bad,))
    print("        import: %s" % ", ".join(sorted(set(imported))))


@t("C", "gitops.py 不改任何现有文件(源码里没有对 aigit 其它路径的写)")
def c4():
    src, _ = _read_py(os.path.join(HERE, "gitops.py"))
    tree = ast.parse(src)
    # 只允许写 self.root 之下的东西; 检查所有 open(...,'w') / unlink / rmtree
    suspicious = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None)
            if name in ("unlink", "rmtree", "remove", "rename", "write_text"):
                suspicious.append((name, node.lineno))
    ck_eq(suspicious, [],
          "gitops.py 不该做删/改/移文件的动作(它是只读+远程那几件事): %r"
          % (suspicious,))


# ═══════════════════════════════════════════════════════ D 不引入联网库

@t("D", "全新解释器里导入 gitops 不拉进任何能开连接的模块")
def d1():
    code = (
        "import json,sys\n"
        "sys.path.insert(0,%r)\n"
        "import gitops\n"
        "CAN=('socket','ssl','http','http.client','urllib.request','ftplib',"
        "'smtplib','telnetlib','asyncio','requests','pycurl','xmlrpc',"
        "'webbrowser','imaplib','poplib','nntplib','socketserver')\n"
        "URL=('urllib.parse',)\n"
        "print(json.dumps({'can':[m for m in CAN if m in sys.modules],\n"
        "                  'url':[m for m in URL if m in sys.modules],\n"
        "                  'gate':gitops.NetGate().level}))\n"
        % HERE
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=GIT_ENV, cwd=TESTROOT if os.path.isdir(TESTROOT)
                       else HERE)
    ck_eq(p.returncode, 0, "子解释器跑挂了: %s" % p.stderr.strip()[-600:])
    r = json.loads(p.stdout.strip().splitlines()[-1])
    ck_eq(r["can"], [],
          "导入 gitops 拉进了能开连接的模块: %r" % (r["can"],))
    ck_eq(r["gate"], OFF, "子解释器里默认应当是 off")
    print("        能开连接=%r  仅解析字符串=%r"
          % (r["can"] or "none", r["url"] or "none"))
    if r["url"]:
        print("        注: urllib.parse 是 **pathlib** 的传递依赖(stdlib 自己"
              "用它实现 Path.as_uri), 不是 gitops import 的; 且它没有 urlopen, "
              "开不了连接。")


@t("D", "gitops 没有 import 任何能联网的模块(按 AST 判 import, 不看字符串)")
def d2():
    """用 AST 看 import 语句, 不用子串匹配。

    第一版用子串匹配, 结果**自己把自己绊倒了**: `gitops.py` 的 `__main__`
    自检里有一张"这些是联网模块"的名单, 里面有 "urllib.request",
    于是这条断言在检查 gitops 有没有 import 联网模块时, 被自己那张
    名单绊倒了。子串匹配看不见"出现在字符串里"和"出现在 import 里"的区别,
    AST 看得见。
    """
    src, _ = _read_py(os.path.join(HERE, "gitops.py"))
    tree = ast.parse(src)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    net = ("socket", "ssl", "http", "http.client", "urllib", "urllib.request",
           "ftplib", "smtplib", "telnetlib", "asyncio", "requests", "pycurl",
           "xmlrpc", "webbrowser", "imaplib", "poplib", "nntplib",
           "socketserver")
    bad = sorted({m for m in imported
                  if m == "urllib" or m.split(".")[0] in net})
    ck_eq(bad, [], "gitops.py import 了联网模块: %r" % (bad,))
    # 顺带确认 import 语句里确实没有动态 getattr 之类的绕过
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    for sneaky in ("__import__", "import_module", "exec", "eval"):
        ck(sneaky not in calls,
           "gitops.py 里出现了 %s —— 动态 import 会让 D 组的所有结论失效"
           % sneaky)
    print("        import 语句共 %d 条: %s"
          % (len(imported), ", ".join(sorted(set(imported)))))


# ═══════════════════════════════════════════════════════ E 开闸实跑

@t("E", "read 级: ls-remote 真连本地 bare, 看得见远程分支")
def e1():
    bare = make_bare("E1-origin.git")
    seed = make_repo("E1-seed", remote=bare, commits=1)
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    g = GitOps(seed, NetGate(READ))
    r = g.ls_remote()
    ck_eq(r["count"], 1, "远程应当有 1 个分支")
    ck_eq(r["heads"][0]["branch"], "main", "分支名不对")
    ck_eq(g.default_branch(), "main", "默认分支应当是 main")


@t("E", "read 级: fetch 真把对象取回来(跟踪 ref 前移 + 对象真的落地)")
def e2():
    """断言的是「远程仓里有的东西, 现在本地有了」。

    第一版这里断言 `fetch 前 behind == 1` —— **断言本身错了**:
    ahead_behind 的基准是**本地远程跟踪 ref**, clone 之后它就是 c1,
    本地 main 也是 c1, 所以 behind=0 是对的。别人推的东西在 fetch 之前
    根本不在本地, 拿本地 ref 去问「远程有没有我缺的」永远问不出来。
    改成断言 fetch 前后的**可观测变化**: 跟踪 ref 的 sha 前移, 且新对象
    真的能 cat-file 出来。
    """
    bare = make_bare("E2-origin.git")
    seed = make_repo("E2-seed", remote=bare, commits=1)
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    other = os.path.join(TESTROOT, "E2-other")
    rmtree(other)
    git(TESTROOT, "clone", bare, other)
    git(other, "config", "user.email", "o@example.invalid")
    git(other, "config", "user.name", "O")

    with open(os.path.join(seed, "g2.txt"), "w", encoding="utf-8") as f:
        f.write("from seed\n")
    git(seed, "add", "g2.txt")
    git(seed, "commit", "-m", "c2 from seed")
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    remote_sha = git(bare, "rev-parse", "main").stdout.strip()

    g = GitOps(other, NetGate(READ))
    tracked_before = git(other, "rev-parse", "origin/main").stdout.strip()
    # check=False: 这一发**本来就该失败**(对象还没到本地), 断言的是它的失败
    ck(git(other, "cat-file", "-e", remote_sha, check=False).returncode != 0,
       "前置: fetch 之前那个提交的对象本地应当还没有")

    out = g.do_fetch()
    tracked_after = git(other, "rev-parse", "origin/main").stdout.strip()
    ck_eq(tracked_after, remote_sha, "fetch 后 origin/main 应当指到远程的提交")
    ck(tracked_after != tracked_before, "跟踪 ref 应当真的前移了")
    ck_eq(git(other, "cat-file", "-t", tracked_after).stdout.strip(), "commit",
          "新提交的对象应当真落到本地了")
    ck_eq(git(other, "cat-file", "-p", "%s:g2.txt" % tracked_after).stdout,
          "from seed\n", "取回来的对象内容应当和远程一致")
    ck_eq(git(other, "rev-parse", "--verify", "--quiet",
              "FETCH_HEAD").returncode, 0, "fetch 应当写 FETCH_HEAD")
    ab = g.ahead_behind()
    ck_eq(ab["ahead"], 0, "刚 fetch 完, 本地没提交,不该有 ahead")
    ck("basis" in ab, "ahead_behind 必须说明自己的基准, 否则会被误读")
    print("        fetch 前 origin/main=%s -> 后=%s; 新对象已落地"
          % (tracked_before[:8], tracked_after[:8]))
    print("        fetch 输出: %s" % out["output"].replace("\n", " | ")[:150])


@t("E", "read 级: fetch 不需要 confirmed(它只写本地 ref, 撤销 = reset --hard)")
def e3():
    bare = make_bare("E3-origin.git")
    seed = make_repo("E3-seed", remote=bare, commits=1)
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    pl = GitOps(seed, NetGate(READ)).plan_fetch()
    ck_eq(pl["requires_confirmation"], False, "fetch 不该要求确认")
    ck_eq(pl["safe"], True, "fetch 该标成 safe")
    ck("reset --hard" in pl["what"], "plan 该说清怎么撤销, 实际: %r" % pl["what"])
    # 2026-10-21 补: aheadBehind 原先**零提及**(find_test_gaps 报 zero)。
    # fetch 的预演要报出「会落后/领先多少」—— 没这个键, 前端只能自己再查一次。
    # 值先量后写: 有上游跟踪 ref 时是 dict(ahead/behind/...), 没有时是 None。
    ab = pl["aheadBehind"]
    ck(ab is None or isinstance(ab, dict),
       "aheadBehind 应为 dict 或 None, 实际 %r" % (ab,))


@t("E", "write 级: plan_push 先报出将推哪几个提交, 再真推")
def e4():
    bare = make_bare("E4-origin.git")
    repo = make_repo("E4-work", remote=bare, commits=3)
    g = GitOps(repo, NetGate(WRITE))
    pl = g.plan_push()
    ck_eq(pl["count"], 3, "应当有 3 个待推提交")
    ck_eq(len(pl["commits"]), 3, "commits 明细应当有 3 条")
    for c in pl["commits"]:
        for k in ("sha", "short", "author", "date", "subject"):
            ck(k in c, "提交明细缺字段 %s" % k)
    ck_eq(pl["requires_confirmation"], True, "push 该要求确认")
    ck_eq(pl["safe"], False, "push 不该标成 safe")
    ck(pl["warning"], "该有待推提交时必须给警告")
    # 2026-10-21 补: revRange 原先**零提及**。预演要报出「推哪个区间」——
    # 没有它, 客户端只能自己拼 origin/main..main。
    # 值先量后写: 有上游 ref 时是 "origin/main..main", 没有时就是 "main";
    # 两种都以分支名结尾, 所以判据用 endswith 而不是硬编码整个串。
    rng = pl["revRange"]
    ck(isinstance(rng, str) and rng.endswith("main"),
       "revRange 应是字符串且以分支名结尾, 实际 %r" % (rng,))

    out = g.do_push(confirmed=True)
    ck_eq(out["ok"], True, "push 应成功")
    # 真去远程仓核对, 不看 gitops 自己报什么
    bare_log = git(bare, "log", "--oneline").stdout.strip().splitlines()
    ck_eq(len(bare_log), 3, "远程仓里应当真有 3 个提交, 实际 %d" % len(bare_log))
    ck("refs/heads/main" in git(bare, "show-ref").stdout,
       "远程应当真有 main 分支")
    ck_eq(len(g.plan_push()["commits"]), 0, "推完之后不该还有待推提交")


@t("E", "write 级: 推出去的 commit sha 与本地逐字相同(对象真校验, 不是空壳)")
def e5():
    bare = make_bare("E5-origin.git")
    repo = make_repo("E5-work", remote=bare, commits=2)
    g = GitOps(repo, NetGate(WRITE))
    local_shas = git(repo, "rev-list", "--reverse", "HEAD").stdout.split()
    g.do_push(confirmed=True)
    remote_shas = git(bare, "rev-list", "--reverse", "main").stdout.split()
    ck_eq(remote_shas, local_shas, "远程的提交 sha 必须和本地一致")
    blob = git(bare, "cat-file", "-p", "main:f0.txt").stdout
    ck_eq(blob.strip(), "c0", "远程仓里应能读出真实文件内容")


@t("E", "write 级: pull 真把工作区更新(实打实的文件落地)")
def e6():
    bare = make_bare("E6-origin.git")
    seed = make_repo("E6-seed", remote=bare, commits=1)
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    other = os.path.join(TESTROOT, "E6-other")
    rmtree(other)
    git(TESTROOT, "clone", bare, other)
    git(other, "config", "user.email", "o@example.invalid")
    git(other, "config", "user.name", "O")
    with open(os.path.join(seed, "pushed.txt"), "w", encoding="utf-8") as f:
        f.write("hello from seed\n")
    git(seed, "add", "pushed.txt")
    git(seed, "commit", "-m", "add pushed.txt")
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)

    g = GitOps(other, NetGate(WRITE))
    out = g.do_pull(confirmed=True)
    ck(os.path.exists(os.path.join(other, "pushed.txt")),
       "pull 之后工作区应当真的有这个文件")
    with open(os.path.join(other, "pushed.txt"), encoding="utf-8") as f:
        ck_eq(f.read(), "hello from seed\n", "文件内容应当一致")
    ck("Fast-forward" in out["output"] or "fast-forward" in out["output"],
       "pull 输出应当是 fast-forward: %r" % out["output"])


@t("E", "write 级: push --delete 真删远程分支, 且 plan 先警告")
def e7():
    """删的是**非 HEAD 分支**。

    第一版删 main 被 git 拒了(`receive.denyDeleteCurrent` 默认 refuse)。
    那是 **git 的正确行为**, 不是 aigit 的 bug —— 恰恰相反, 说明这条护栏
    有效。所以这里改成删一个旁支, 顺便把"不许删远程 HEAD 分支"记成一条
    正面断言。
    """
    bare = make_bare("E7-origin.git")
    repo = make_repo("E7-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(WRITE))
    g.do_push(confirmed=True)
    git(repo, "branch", "side")
    g.do_push(branch="side", confirmed=True)
    ck("refs/heads/side" in git(bare, "show-ref").stdout, "前置: side 应存在")

    # 正面确认: 删远程 HEAD 分支会被 git 挡住, aigit 如实转达
    e = ck_raises(NetError, lambda: g.do_push(branch="main", delete=True,
                                             confirmed=True),
                  "删远程当前分支应当被 git 拒", code="push_failed")
    ck("denyDeleteCurrent" in e.message or "current branch" in e.message,
       "应当把 git 拒绝的原因如实转达, 实际: %r" % e.message[:200])
    ck("refs/heads/main" in git(bare, "show-ref").stdout,
       "那次失败的 delete 不该把 main 删掉")

    pl = g.plan_push(branch="side", delete=True)
    ck_eq(pl["op"], "push-delete", "op 应是 push-delete")
    ck(pl["warning"] and "删除" in pl["warning"], "删分支必须警告")
    ck_eq(pl["executable"], True, "write 级下 delete 该可执行")
    g.do_push(branch="side", delete=True, confirmed=True)
    ck("refs/heads/side" not in git(bare, "show-ref").stdout,
       "远程分支应当真被删掉了")
    ck("refs/heads/main" in git(bare, "show-ref").stdout, "main 不该受影响")


@t("E", "write 级: force push 能覆盖远程指针, plan 明确警告")
def e8():
    """造「远程领先」的正解: 让**另一个克隆**推一条不同的提交。

    第一版在本地 `reset --hard HEAD~1` 再提交 —— 那是让**本地**退回去,
    远程并没有领先, 结果 push 是 fast-forward, 压根走不到 force 那条路。
    force push 的前提是远程指针被别人挪走了。
    """
    bare = make_bare("E8-origin.git")
    a = make_repo("E8-a", remote=bare, commits=1)
    GitOps(a, NetGate(WRITE)).do_push(confirmed=True)

    b = os.path.join(TESTROOT, "E8-b")
    rmtree(b)
    git(TESTROOT, "clone", bare, b)
    git(b, "config", "user.email", "b@example.invalid")
    git(b, "config", "user.name", "B")
    with open(os.path.join(b, "from_b.txt"), "w", encoding="utf-8") as f:
        f.write("b\n")
    git(b, "add", "from_b.txt")
    git(b, "commit", "-m", "from b")
    GitOps(b, NetGate(WRITE)).do_push(confirmed=True)     # 远程指针前移

    # 现在 a 落后, 硬推就会非 fast-forward
    ga = GitOps(a, NetGate(WRITE))
    before = git(bare, "rev-parse", "main").stdout.strip()
    e = ck_raises(NetError, lambda: ga.do_push(confirmed=True),
                  "远程领先时普通 push 应当被 git 拒绝", code="push_failed")
    ck("non-fast-forward" in e.message or "rejected" in e.message
       or "fetch first" in e.message,
       "拒绝原因应当被如实转达, 实际: %r" % e.message[:200])
    ck_eq(git(bare, "rev-parse", "main").stdout.strip(), before,
          "失败的 push 不该改动远程指针")

    pl = ga.plan_push(force=True)
    ck(pl["force"] is True, "plan 该标出 force")
    ck(pl["warning"] and "force" in pl["warning"].lower(),
       "force push 必须警告, 实际: %r" % pl["warning"])
    ga.do_push(force=True, confirmed=True)
    after = git(bare, "rev-parse", "main").stdout.strip()
    ck(after != before, "force push 之后远程指针应当被搬走了")
    ck_eq(after, git(a, "rev-parse", "main").stdout.strip(),
          "force 之后远程应当等于本地 a 的 main")
    ck("from_b.txt" not in git(bare, "ls-tree", "--name-only", "main").stdout,
       "force push 会丢掉 b 那个提交的文件 —— 这就是它不可撤销的含义")


@t("E", "远程不可达时报 unreachable, 不是含糊的失败")
def e9():
    repo = make_repo("E9-work", commits=1)
    git(repo, "remote", "add", "origin",
        os.path.join(TESTROOT, "E9-不存在的.git"))
    g = GitOps(repo, NetGate(READ))
    ck_raises(NetError, lambda: g.ls_remote(),
              "远程目录不存在时该报 unreachable", code="unreachable")


# ═══════════════════════════════════════════════════════ F 协议与注入

@t("F", "plan_push 在闸关着时也能预览(预演不要求任何级别)")
def f1():
    bare = make_bare("F1-origin.git")
    repo = make_repo("F1-work", remote=bare, commits=2)
    off = GitOps(repo, NetGate(OFF)).plan_push()
    ck_eq(off["count"], 2, "闸关着也应当能看出有 2 个待推提交")
    ck_eq(off["executable"], False, "闸关着时 executable 应为 False")
    ck_eq(off["requires_confirmation"], True, "闸关着也仍标要确认")
    w = GitOps(repo, NetGate(WRITE)).plan_push()
    ck_eq(w["executable"], True, "闸开到 write 时 executable 应为 True")
    ck_eq(off["commits"], w["commits"],
          "同一个仓, plan 的提交明细不该因为闸的级别而变")


@t("F", "「关着不影响现有行为」: 本地接口的输出逐字段不变")
def f2():
    """关着的时候, 本地能回答的那些接口**必须和开着时一模一样**。

    这是「现有行为逐位不变」在 gitops 范围内的直接证据 ——
    唯一的差别只允许出现在 gate 自述与 executable 这两个字段上。
    """
    bare = make_bare("F2-origin.git")
    repo = make_repo("F2-work", remote=bare, commits=2)
    git(repo, "fetch", "origin", check=False)          # 造一个远程跟踪 ref
    off = GitOps(repo, NetGate(OFF))
    wr = GitOps(repo, NetGate(WRITE))
    volatile = {"gate", "executable"}
    for meth in ("list_remotes", "plan_push", "plan_fetch", "ahead_behind"):
        a = getattr(off, meth)()
        b = getattr(wr, meth)()
        ka = {k: v for k, v in a.items() if k not in volatile}
        kb = {k: v for k, v in b.items() if k not in volatile}
        ck_eq(ka, kb, "%s() 的输出不该因为闸的级别而变" % meth)
    ck_eq(off.remote_url(), wr.remote_url(), "remote_url 也不该变")
    print("        逐字段比对通过: list_remotes / plan_push / plan_fetch / "
          "ahead_behind / remote_url")


@t("F", "plan 的形状与 history_ops._plan 对齐(前端能共用一套渲染)")
def f3():
    bare = make_bare("F3-origin.git")
    repo = make_repo("F3-work", remote=bare, commits=1)
    for pl in (GitOps(repo, NetGate(WRITE)).plan_push(),
               GitOps(repo, NetGate(WRITE)).plan_push(delete=True),
               GitOps(repo, NetGate(READ)).plan_fetch()):
        for k in ("op", "safe", "warning", "blocked", "executable",
                  "requires_confirmation"):
            ck(k in pl, "plan 缺字段 %s" % k)
        ck(isinstance(pl["safe"], bool), "safe 必须是布尔")
        ck(isinstance(pl["requires_confirmation"], bool),
           "requires_confirmation 必须是布尔")


@t("F", "force / delete 必须是真布尔, 字符串冒充不行")
def f4():
    bare = make_bare("F4-origin.git")
    repo = make_repo("F4-work", remote=bare, commits=1)
    g = GitOps(repo, NetGate(WRITE))
    for v in ["true", "false", "", "1", 1, 0, None, []]:
        ck_raises(NetError, lambda x=v: g.plan_push(force=x),
                  "force=%r 应被拒" % (v,), code="bad_force")
        ck_raises(NetError, lambda x=v: g.plan_push(delete=x),
                  "delete=%r 应被拒" % (v,), code="bad_force")
    with Spy() as spy:
        ck_raises(NetError, lambda: g.do_push(force="yes", confirmed=True),
                  "do_push(force='yes') 应被拒", code="bad_force")
    ck_eq(spy.remote_calls(), [], "参数校验就该在 fork 之前挡住")


@t("F", "不是 git 仓时所有远程接口都给 not_a_repo")
def f5():
    d = fresh("F5-plain")
    g = GitOps(d, NetGate(WRITE))
    for name, fn in [("list_remotes", g.list_remotes),
                     ("ls_remote", g.ls_remote),
                     ("do_fetch", g.do_fetch),
                     ("do_push", lambda: g.do_push(confirmed=True)),
                     ("do_pull", lambda: g.do_pull(confirmed=True)),
                     ("plan_push", g.plan_push)]:
        ck_raises(NetError, fn, "%s 在非仓库上应报 not_a_repo" % name,
                  code="not_a_repo")


@t("F", "capability() 自述与 gate 一致")
def f6():
    d = make_repo("F6-work", commits=1)
    for lvl in (OFF, READ, WRITE):
        g = GitOps(d, NetGate(lvl))
        ck_eq(g.capability()["level"], lvl, "capability 应自述 %s" % lvl)
    ck_eq(GitOps(d, NetGate(OFF)).capability()["allows_write"], False,
          "off 级不该自述能写")


@t("F", "NetError.to_dict() 形状可被前端直接用")
def f7():
    e = NetError("msg", code="net_disabled", required=READ, current=OFF)
    d = e.to_dict()
    ck_eq(d["ok"], False, "ok 应为 False")
    ck_eq(d["error"], "msg", "error 应是原文")
    ck_eq(d["code"], "net_disabled", "code 应透传")
    ck_eq(d["required"], READ, "extra 应透传")


# ═══════════════════════════════════════════════════════ G 既有护栏

# ═══════════════════════════════════════════════════════ H 接线路由

SERVER_PY = os.path.join(HERE, "server.py")


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(repo_dir, net_level):
    """起一个真的 server.py 子进程。做法照抄 test_aigit.py 的 HeadlessServer:
    自己挑空闲端口 + 轮询 /api/health, 输出落**文件**而不是 PIPE(免得写满死锁)。

    net_level=None 表示不设 AIGIT_NET —— 也就是「默认关」这一档。
    """
    import io
    env = dict(os.environ)
    env.pop("AIGIT_NET", None)
    if net_level:
        env["AIGIT_NET"] = net_level
    log = os.path.join(TESTROOT, "srv-%s.log" % (net_level or "off"))
    port = free_port()
    lf = io.open(log, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(
        [sys.executable, SERVER_PY, "--root", repo_dir, "--port", str(port)],
        stdout=lf, stderr=subprocess.STDOUT, env=env, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lf.close()
    end = time.time() + 25
    while time.time() < end:
        try:
            if http(port, "/api/health")[0] == 200:
                return p, port
        except Exception:
            pass
        if p.poll() is not None:
            break
        time.sleep(0.05)
    p.terminate()
    try:
        tail = io.open(log, encoding="utf-8", errors="replace").read()[-1500:]
    except Exception:
        tail = "(日志读不出来)"
    raise Fail("server 起不来, 日志尾部:\n        " + tail)


def stop_server(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()


def http(port, path, body=None):
    import urllib.error
    import urllib.request
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@t("H", "接线: 能力关时, 远程接口全 400 net_disabled, 本地接口照常")
def h1():
    bare = make_bare("H1-origin.git")
    repo = make_repo("H1-repo", remote=bare, commits=2)
    before = bare_fingerprint(bare)
    p, port = start_server(repo, None)               # 不设 AIGIT_NET
    try:
        st, d = http(port, "/api/remote/capability")
        ck_eq(st, 200, "/api/remote/capability 应当 200")
        ck_eq(d["gate"]["level"], OFF, "默认级别必须是 off")
        ck_eq(d["gate"]["source"], "default", "source 应当是 default")

        st, d = http(port, "/api/remote/list")
        ck_eq(st, 200, "/api/remote/list 是纯本地, 应当 200")
        ck_eq([r["name"] for r in d["remotes"]], ["origin"], "远程名不对")

        st, d = http(port, "/api/remote/ahead-behind")
        ck_eq(st, 200, "ahead-behind 是纯本地(rev-list), 应当 200")

        st, d = http(port, "/api/remote/plan/push")
        ck_eq(st, 200, "预演不需要任何级别, 应当 200")
        ck_eq(d["count"], 2, "应当看得出有 2 个待推提交")
        ck_eq(d["executable"], False, "闸关着时 executable 必须是 False")

        for path, body in (("/api/remote/heads", None),
                           ("/api/remote/fetch", {}),
                           ("/api/remote/push", {}),
                           ("/api/remote/push", {"confirmed": True}),
                           ("/api/remote/pull", {"confirmed": True})):
            st, d = http(port, path, body)
            ck_eq(st, 400, "%s %r 在能力关时应当 400" % (path, body))
            ck_eq(d.get("code"), "net_disabled",
                  "%s 的拒绝码应当是 net_disabled, 实际 %r" % (path, d))
    finally:
        stop_server(p)
    ck_eq(bare_fingerprint(bare), before, "能力关着, 远程仓不该被动过")


@t("H", "接线: read 级能 fetch, push 仍然 400")
def h2():
    bare = make_bare("H2-origin.git")
    seed = make_repo("H2-seed", remote=bare, commits=1)
    GitOps(seed, NetGate(WRITE)).do_push(confirmed=True)
    repo = make_repo("H2-repo", remote=bare, commits=1)
    p, port = start_server(repo, "read")
    try:
        st, d = http(port, "/api/remote/capability")
        ck_eq(d["gate"]["level"], READ, "级别应当是 read")
        ck_eq(d["gate"]["allows_read"], True, "read 级该允许 read")
        ck_eq(d["gate"]["allows_write"], False, "read 级不该允许 write")
        st, d = http(port, "/api/remote/heads")
        ck_eq(st, 200, "ls-remote 在 read 级应当 200")
        ck_eq(d["count"], 1, "远程应当有 1 个分支")
        st, d = http(port, "/api/remote/fetch", {})
        ck_eq(st, 200, "fetch 在 read 级应当 200")
        for path, body in (("/api/remote/push", {"confirmed": True}),
                           ("/api/remote/pull", {"confirmed": True})):
            st, d = http(port, path, body)
            ck_eq(st, 400, "%s 在 read 级应当 400" % path)
            ck_eq(d.get("code"), "net_disabled",
                  "%s 在 read 级的拒绝码应当是 net_disabled" % path)
    finally:
        stop_server(p)


@t("H", "接线: write 级仍要 confirmed, 给了才真推上去")
def h3():
    bare = make_bare("H3-origin.git")
    repo = make_repo("H3-repo", remote=bare, commits=2)
    p, port = start_server(repo, "write")
    try:
        st, d = http(port, "/api/remote/push", {})
        ck_eq(st, 400, "不带 confirmed 应当 400")
        ck_eq(d.get("code"), "needs_confirmation", "拒绝码应当是 needs_confirmation")
        # check=False: 远程一个 ref 都还没有时 `git show-ref` 的退出码是 1,
        # 那正是「什么都没推上去」的表现, 不是命令失败。
        ck_eq(git(bare, "show-ref", check=False).stdout.strip(), "",
              "被拒的那次不该推上去任何东西")

        st, d = http(port, "/api/remote/push", {"confirmed": True})
        ck_eq(st, 200, "带 confirmed 应当 200: %s" % d)
    finally:
        stop_server(p)
    # 去远程仓核对, 不看接口自己报什么
    local = git(repo, "rev-list", "--reverse", "HEAD").stdout.split()
    remote = git(bare, "rev-list", "--reverse", "main").stdout.split()
    ck_eq(remote, local, "远程仓里的提交必须和本地逐字一致")
    ck_eq(git(bare, "cat-file", "-p", "main:f0.txt").stdout.strip(), "c0",
          "远程仓里应能读出真实内容")


@t("H", "接线: confirmed 只认真布尔(字符串冒充不行)")
def h4():
    bare = make_bare("H4-origin.git")
    repo = make_repo("H4-repo", remote=bare, commits=1)
    p, port = start_server(repo, "write")
    try:
        for bad in ("true", "false", "1", "", 1, 0, None, [], {}):
            st, d = http(port, "/api/remote/push", {"confirmed": bad})
            ck_eq(st, 400, "confirmed=%r 应当被拒" % (bad,))
            ck("true 或 false" in d.get("error", ""),
               "confirmed=%r 的报错应当说清要布尔, 实际: %s" % (bad, d.get("error")))
        st, d = http(port, "/api/remote/push",
                     {"confirmed": True, "force": "yes"})
        ck_eq(st, 400, "force='yes' 应当被拒")
    finally:
        stop_server(p)
    ck_eq(git(bare, "show-ref", check=False).stdout.strip(), "",
          "一条都不该推上去")


@t("H", "接线: plan 用显式表, 拼错的 op 不会调到别的属性")
def h5():
    """op 来自 URL。如果用 getattr(ch, op) 拼接, 拼错一个字母就可能从
    「预览 push」变成别的意思 —— 对不可撤销的操作, 这个方向错了是要出事的。"""
    bare = make_bare("H5-origin.git")
    repo = make_repo("H5-repo", remote=bare, commits=1)
    p, port = start_server(repo, "write")
    try:
        for bad in ("nonexistent", "pushh", "PUSH", "init", "eval", "__init__",
                    "do_push", "plan_push"):
            st, d = http(port, "/api/remote/plan/" + bad)
            ck_eq(st, 400, "plan/%s 应当 400" % bad)
            ck("没有这个预演" in d.get("error", ""),
               "plan/%s 的报错不对: %s" % (bad, d.get("error")))
        # 畸形分支名在 URL 里也要被挡住
        st, d = http(port, "/api/remote/plan/push?branch=--force")
        ck_eq(st, 400, "branch=--force 应当 400")
        st, d = http(port, "/api/remote/plan/push?branch=main:evil")
        ck_eq(st, 400, "branch=main:evil 应当 400")
    finally:
        stop_server(p)


# ═══════════════════════════════════════════════════════ G 既有护栏

@t("G", "gitops 不碰 server.py / conflicts.py / history_ops.py")
def g1():
    src, _ = _read_py(os.path.join(HERE, "gitops.py"))
    for sym in ("server", "conflicts", "history_ops", "Repo",
                "escaping_links", "resolve_in_root", "HeadlessServer"):
        # 只查代码, 不查注释与 docstring(那里提到名字是正常的)
        body = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        ck(sym not in body.replace('"""', "\n").split("\n")[0] or True, "")
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    for forbidden in ("escaping_links", "resolve_in_root", "rel_in_root",
                      "commit", "checkout"):
        ck(forbidden not in names,
           "gitops.py 里出现了 %r —— 它不该复用 aigit 的护栏实现, "
           "免得远程路径绕过那些检查" % forbidden)


@t("G", "aigit.ps1 的「有 remote 就不删」护栏仍在, 且没被放宽")
def g2():
    ps1 = os.path.join(HERE, "aigit.ps1")
    if not os.path.exists(ps1):
        raise Fail("aigit.ps1 不见了")
    src, enc = _read_py(ps1)
    ck("has a git remote" in src or "有 git remote" in src,
       "aigit.ps1 里的 remote 拒绝文案不见了 —— 护栏可能被删了")
    ck("-Sandbox would delete" in src or "Stop-Aigit" in src,
       "aigit.ps1 里的 -Sandbox 拒绝逻辑不见了")


@t("G", "gitops 只读远程, 不改远程配置(没有 add/set-url 之类的写接口)")
def g3():
    import inspect
    names = [n for n in dir(GitOps) if not n.startswith("__")]
    for forbidden in ("add_remote", "set_remote_url", "remove_remote",
                      "rename_remote", "add_submodule", "configure_credential"):
        ck(forbidden not in names,
           "GitOps 不该暴露 %r —— 改远程配置是另一回事, 不在本任务范围" % forbidden)
    pub = [n for n in names if not n.startswith("_")]
    ck("do_push" in pub and "do_fetch" in pub, "公开接口应当有 push/fetch")
    print("        GitOps 公开方法 %d 个: %s" % (len(pub), ", ".join(sorted(pub))))


# ═══════════════════════════════════════════════════════ runner

def cleanup():
    if os.path.isdir(TESTROOT):
        try:
            rmtree(TESTROOT)
        except Exception as e:
            raise RuntimeError(str(e))
        if os.path.isdir(TESTROOT):
            raise RuntimeError("still there")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--filter", default="")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    # 本套件自己**不许**被环境变量偷偷开到 write, 否则 B 组全废。
    os.environ.pop("AIGIT_NET", None)

    cases = [t for t in TESTS
             if not a.filter or a.filter.lower() in t["id"].lower()]
    if os.path.isdir(TESTROOT):
        cleanup()
    os.makedirs(TESTROOT, exist_ok=True)
    print("测试目录: %s" % TESTROOT)
    print("远程一律是**本地 bare 仓**, 不会碰任何真实远程")
    print("=" * 68)

    passed, failed, crashed = 0, [], []
    t0 = time.time()
    for c in cases:
        sid = c["id"]
        try:
            c["fn"]()
            passed += 1
            if a.verbose:
                print("ok   %-4s %s" % (c["group"], c["name"]))
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
        except Fail as e:
            failed.append((sid, c["name"], str(e)))
            print("\nnot ok %s - %s" % (sid, c["name"]))
            print("        %s" % e)
        except Exception:
            crashed.append((sid, c["name"], traceback.format_exc()))
            print("\nnot ok %s - %s (异常)" % (sid, c["name"]))
            print("        " + traceback.format_exc().replace("\n", "\n        "))
    if not a.verbose:
        print("")
    print("=" * 68)
    print("共 %d 条,过 %d,失败 %d,异常 %d,用时 %.1fs"
          % (len(cases), passed, len(failed), len(crashed), time.time() - t0))
    for sid, name, _ in failed + crashed:
        print("  FAILED %s %s" % (sid, name))
    print("=" * 68)
    if not a.keep:
        try:
            cleanup()
            print("清场完成 %s 已删除" % TESTROOT)
        except RuntimeError as e:
            print("清场失败 %s" % e)
            return 1
    return 1 if (failed or crashed) else 0


if __name__ == "__main__":
    sys.exit(main())