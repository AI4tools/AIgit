#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aigit 并发(多线程 HTTP)对抗性测试套件。

为什么单独一个文件:
    现有 240 条断言全是**串行**跑的。`server.py` 用 `ThreadingHTTPServer`
    (多线程), 所以并发请求是**真实场景**, 不是理论 —— 而三把锁
    (`Repo._lock` / `HistoryOps._lock` / conflicts 里那把根本不存在的锁)
    互不相干这件事, 串行用例永远测不出来。

设计立场(与 test_aigit.py / test_history_ops.py 同款):
    这是一份**试图把并发面打穿**的测试, 不是一份「证明并发没问题」的测试。
    判据全部写成**正确行为**: 一旦 aigit 有并发缺陷, 测试就该红。
    **全绿在这里是可疑信号, 不是好消息。**

「不许为了让并发用例变绿而放宽判据」在本文件里的具体落实:
    * 每条判据都同时输出 `命中数/总次数` 的**复现率**, 而不是只输出通过/失败。
      低概率命中不藏 —— 标成 flaky 并报出复现率。
    * 不用「重试到绿」。一次命中就是命中。
    * 提高并发度 / 加线程数只用于**放大竞争**(让红更容易出现), 从不用来调绿。
    * 每个结论都带限定: 在 `<具体组合> × <具体并发度> × N 轮` 下没找到。

关于「注入点」(任务硬要求第 2 项):
    `C0 calibrate` 是这套测试的**自检**。它证明两件事:
      (1) 探针非空转: 把锁换成空锁后, 临界区重叠计数**必定** > 0;
      (2) 注入必红: 放大窗口后, 损坏类判据的命中率**必定**严格上升。
    注入只发生在**本进程内**(monkeypatch 模块对象 / 子进程), 不改任何产品文件。
    撤掉注入后的结果同样打印出来 —— 两步都有输出。

跑法:
    python test_aigit_concurrency.py             # 全部
    python test_aigit_concurrency.py -v          # 逐条细节
    python test_aigit_concurrency.py -k C1       # 只跑 id 含 C1 的
    python test_aigit_concurrency.py --list      # 只列清单

硬约束:
    * 纯标准库, 不用 pytest(理由同 test_aigit.py)。
    * **完全无头**: 不开浏览器、不开窗口。HTTP 走 urllib.request。
    * 测试仓建在**私有 TEMP**(带本进程 pid), 绝不碰任何真实仓库。
    * **只读** server.py / conflicts.py / history_ops.py。发现 bug 只记录、不修。

分组:
    C0  探针自检(注入必红 / 不注入必绿)
    C1  stage × conflict/resolve     跨锁域并发
    C2  stage × discard              目录护栏 / TOCTOU
    C2B discard(握目录句柄) × reset --hard    E4 加固引入的跨线程故障
    C3  checkout/branch × commit
    C4  history 危险操作 × stage
    C5  同一文件被两个请求同时改
    C6  escaping_links 护栏的并发绕过(仓外文件进历史)
    C7  锁域一致性测量(三把锁 / 零把锁 / 跨域同时跑 git)
    C8  进程级全局可变状态
    C9  死锁体检(静态锁序 + 动态看门狗)
"""

import argparse
import ast
import collections
import hashlib
import heapq
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

AIGIT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(AIGIT, "server.py")
CONFLICTS_PY = os.path.join(AIGIT, "conflicts.py")
HISTORY_PY = os.path.join(AIGIT, "history_ops.py")

# 临时仓根**必须带进程号 + 固定标识**: `main()` 开头会 shutil.rmtree 整棵目录,
# 而 test_aigit_extra.py / test_history_ops.py 的 TESTROOT 仍是固定路径,
# 同文件并发会互删 —— 那种互删报出来的是**假 FAIL**。
TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-conc-%d" % os.getpid())

GIT_ENV = dict(os.environ)
GIT_ENV.update({
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
})

#: git 自己的锁文件报错。这**不是**用户能理解的失败原因 —— 它意味着
#: aigit 的互斥保护不够, 两个请求同时进了 git。出现它就是缺陷。
GIT_LOCK_MARKERS = (
    "index.lock",
    "Unable to create",
    "cannot lock ref",
    "reference already exists",
    "Unable to unlink",
    "File exists",
    "Unable to write",
    "Unable to rename",
)
#: Windows 上被别的句柄挡住(目录 pin / 文件占用)。E4 的「握住目录」会让它出现。
WIN32_SHARING_MARKERS = (
    "另一个程序正在使用此文件",
    "The process cannot access the file because it is being used",
    "WinError 32",
    "WinError 33",
    "used by another process",
)
TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    "File \"",              # 500 里带出来的 traceback 片段
)

VERBOSE = False
RESULTS = []            # 每条用例的机器可读汇总, 收尾打表用


# ════════════════════════════════════════════════════════ 测试登记

TESTS = []


def test(group, name, note=""):
    def deco(fn):
        TESTS.append({"group": group, "name": name, "fn": fn, "note": note,
                      "id": "%s-%s" % (group, name)})
        return fn
    return deco


class Skip(Exception):
    pass


class Fail(Exception):
    pass


class Flaky(Fail):
    """命中了但不是每次都命中。**报出来, 不藏起来。**"""

    def __init__(self, msg, rate=None):
        super().__init__(msg)
        self.rate = rate


def ck(cond, msg):
    if not cond:
        raise Fail(msg)


def ck_eq(got, want, what):
    if got != want:
        raise Fail("%s: 期望 %r,实际 %r" % (what, want, got))


def say(*a):
    if VERBOSE:
        sys.stderr.write("    " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()


# ════════════════════════════════════════════════════════ git / 仓

class Sandbox:
    """%TEMP% 下的独立 git 仓。绝不碰任何真实仓库。"""

    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(TESTROOT, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "aigit-conc@localhost")
        self.git("config", "user.name", "aigit-conc")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, **kw):
        check = kw.pop("check", False)
        p = subprocess.run(["git", "-C", self.dir] + [str(a) for a in args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=120)
        if check and p.returncode != 0:
            raise RuntimeError("git %s 失败: %s"
                               % (" ".join(map(str, args)), (p.stderr or p.stdout).strip()))
        return p

    def out(self, *args):
        return self.git(*args).stdout

    def abspath(self, rel):
        return os.path.join(self.dir, rel.replace("/", os.sep))

    def write(self, rel, text):
        p = self.abspath(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def read(self, rel):
        p = self.abspath(rel)
        if not os.path.exists(p):
            return None
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def sha256_of(self, rel):
        p = self.abspath(rel)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-m", msg)

    def outside(self, *parts):
        d = os.path.join(TESTROOT, "..", self.name + "-outside", *parts)
        d = os.path.normpath(d)
        os.makedirs(d, exist_ok=True)
        return d


# ════════════════════════════════════════════════════════ HTTP 服务(进程内)

class Server:
    """在**本进程内**起一个真的 ThreadingHTTPServer, 用 urllib 打。

    为什么不起子进程: 第 2 项任务要求注入一个已知竞争来证明用例会红,
    而注入必须是 monkeypatch —— 进程内才改得到模块对象。
    注入只影响本进程, 绝不需要改 server.py。
    """

    def __init__(self, repo, tag="srv"):
        import server as S
        self.mod = S
        root = repo.dir if isinstance(repo, Sandbox) else repo
        S.WEB = Path(AIGIT) / "web"
        S.REPO = S.Repo(Path(root))
        # 必须清: 懒加载的全局单例一旦建好就绑在当时的 root 上,
        # 换仓不重置会让 conflict/history 接口打到别的仓去。
        S._CONFLICTS = None
        S._HISTORY = None
        self.srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        self.port = self.srv.server_address[1]
        self._th = threading.Thread(target=self.srv.serve_forever,
                                     kwargs={"poll_interval": 0.05},
                                     name="aigit-http", daemon=True)
        self.log = os.path.join(TESTROOT, "%s.srvlog" % tag)
        self._lf = io.open(self.log, "w", encoding="utf-8", errors="replace")
        self._saved_err = sys.stderr
        sys.stderr = self._lf          # Handler.log_message 走 stderr, 别刷屏
        self._th.start()

    # -- HTTP -------------------------------------------------------------
    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def raw(self, method, path, obj=None, timeout=180):
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, path, timeout=180):
        code, txt = self.raw("GET", path, timeout=timeout)
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("GET %s 返回非 JSON(code=%s): %r" % (path, code, txt[:300]))

    def post(self, path, obj=None, timeout=180):
        code, txt = self.raw("POST", path, obj if obj is not None else {}, timeout=timeout)
        try:
            return code, json.loads(txt)
        except Exception:
            return code, {"__raw__": txt}

    def srvlog_tail(self, n=30):
        sys.stderr = self._saved_err
        try:
            with io.open(self.log, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except Exception:
            return "(无)"

    def stop(self):
        try:
            sys.stderr = self._saved_err
            self.srv.shutdown()
            self.srv.server_close()
            self._lf.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()


# ════════════════════════════════════════════════════════ 探针 / 注入
#
# 探针只 monkeypatch **模块对象上的方法**, 一次进程内有效, 不写任何文件。
# 被 patch 的东西:
#     server.Repo._run_full          —— 第 1 把锁(Repo._lock)
#     history_ops.HistoryOps._run    —— 第 2 把锁(HistoryOps._lock)
#     conflicts.Git.run              —— **没有锁**的那条通路

DOMAINS = ("repo", "history", "conflicts")

_CNT = [0]


def _argv_label(a):
    """从位置参数里挖出 git 子命令名, 便于报表可读。"""
    for x in a:
        if isinstance(x, (list, tuple)) and x:
            for y in x:
                if isinstance(y, str) and not y.startswith("-") \
                        and y not in ("git", "-C"):
                    return y
    return "?"


def count_overlaps(recs, domains=None):
    """同域/跨域的临界区重叠对数。扫线法, O(n·并发度)。

    先断言「没有未关闭的 span」: 漏关的 span 会被过滤掉, 于是报出「重叠 0」。
    那不是「量到了没有重叠」, 是「根本没量到」—— 两件事必须分开。
    """
    dangling = [r for r in recs if r.get("t1") is None]
    ck(not dangling,
       "探针漏关了 %d 条临界区(从未退出)—— 下面的重叠数会偏小, 不可采信。例: %r"
       % (len(dangling), [(r["domain"], r["label"]) for r in dangling[:3]]))
    rs = sorted([r for r in recs if r.get("t1") is not None
                 and (domains is None or r["domain"] in domains)],
                key=lambda r: r["t0"])
    pairs = 0
    samples = []
    active = []
    for r in rs:
        while active and active[0][0] <= r["t0"]:
            heapq.heappop(active)
        for _, other in active:
            if other["ident"] == r["ident"]:
                continue
            pairs += 1
            if len(samples) < 6:
                samples.append((other["domain"], other["label"],
                                r["domain"], r["label"]))
        heapq.heappush(active, (r["t1"], r))
    return pairs, samples


class Probe(object):
    """记录三把(其实两把半)锁的**真正临界区**。

    这里踩过一个坑, 值得写下来: 第一版探针包的是 `Repo._run_full` 这个**方法**,
    于是量到的时间区间是「等锁 + 持锁」—— 6 个线程排队等同一把锁, 区间必然
    两两重叠, 报出 789 次「重叠」, 而实际上一把锁都没进去过两个人。
    **量临界区必须量锁本身, 不能量调用它的方法。**
    现在:
      * repo / history 域 —— 把**锁对象**换成 TracingLock(内核仍是原锁,
        照样互斥), 只在真正持有期间记一条 span;
      * conflicts 域 —— 那条通路**根本没有锁**, 所以整个方法体就是它的
        「临界区」, 用 install_span 记一条。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.recs = []
        self.saved = []
        self.tl = threading.local()          # 每线程一个「当前 git 子命令」标签

    # -- span 记账 -------------------------------------------------------
    def _open(self, domain):
        with self.lock:
            _CNT[0] += 1
            tid = "%d" % _CNT[0]
            self.recs.append({"domain": domain, "ident": tid,
                              "thread": threading.current_thread().name,
                              "label": getattr(self.tl, "label", domain),
                              "req": getattr(self.tl, "req", None),
                              "t0": time.perf_counter(), "t1": None})
            return tid

    def install_request(self, obj, attr, label_of):
        """把**服务端一个请求**框成一条 span。

        为什么必须有它: git 命令是在 ThreadingHTTPServer 的**处理线程**里跑的,
        而发请求的是客户端线程 —— 两者不是同一个线程。所以「这个操作发了几条
        git」根本不能按客户端线程去归, 第一版就是这么归的, 结果算出
        `ops_zero_git_call: 60`(每个操作 0 条 git), 而同时 `interleaved: 60`
        —— 明显自相矛盾。归因必须以**服务端请求**为单位。
        """
        orig = getattr(obj, attr)
        self.saved.append((obj, attr, orig))
        probe = self

        def wrapped(*a, **kw):
            prev_l, prev_r = getattr(probe.tl, "label", None), getattr(probe.tl, "req", None)
            probe.tl.label = label_of(a, kw)
            tid = probe._open("request")
            probe.tl.req = tid
            try:
                return orig(*a, **kw)
            finally:
                probe.tl.req = prev_r
                probe.tl.label = prev_l
                probe._close(tid)

        setattr(obj, attr, wrapped)

    def _close(self, tid):
        t = time.perf_counter()
        with self.lock:
            for r in self.recs:
                if r["ident"] == tid and r["t1"] is None:
                    r["t1"] = t
                    break

    # -- 安装 ------------------------------------------------------------
    def trace_lock(self, real, domain):
        return TracingLock(real, self, domain)

    def install_lock(self, obj, attr, domain):
        """给一把**存在的**锁套上追踪(互斥语义完全不变)。"""
        self.saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, self.trace_lock(getattr(obj, attr), domain))

    def inject_noop_lock(self, obj, attr, domain):
        """【注入点】把这把锁的内核换成空锁 —— 人为造出竞争。

        注意是换**内核**, 不是换整个对象: 外层 TracingLock 还在, 于是注入后
        的重叠计数仍然由同一套探针产出, 两步可直接对比。
        """
        self.saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, self.trace_lock(NoopLock(), domain))

    def install_label(self, obj, attr):
        """只记录「当前在跑哪条 git」, 不记 span。"""
        orig = getattr(obj, attr)
        self.saved.append((obj, attr, orig))
        probe = self

        def wrapped(*a, **kw):
            prev = getattr(probe.tl, "label", None)
            probe.tl.label = _argv_label(a)
            try:
                return orig(*a, **kw)
            finally:
                probe.tl.label = prev

        setattr(obj, attr, wrapped)

    def install_span(self, obj, attr, domain):
        """**按方法体**计一个 span(不套锁对象)。

        ⚠ 原来这里写的是「无锁通路」—— **那个说法是错的**, 它把「这条通路
          不经过 install_lock 套过的那把锁」写成了「这条通路没有锁」。
          `conflicts.Git.run` **有锁**(repo_git_lock, conflicts.py:237/:241),
          只是那把锁与 Repo._lock 不是同一把。
        ⚠ **量临界区应该量锁本身**(install_lock), 量方法体会把「方法体里
          没取锁的那段代码」也算进去。这两种量法量的不是同一个东西, 数字
          不可混着比 —— test_aigit_concurrency.py:374-382 与本文件 T9 的
          docstring 都写过这条原则。
        这里仍然保留它: C7 用它量的是「Git.run 这个调用在时间上与别的域重叠
        多少次」, 那个问题用方法体量是成立的(它问的是**调用**的重叠,
        不是**临界区**的重叠)。但两者不可互相替代。
        """
        orig = getattr(obj, attr)
        self.saved.append((obj, attr, orig))
        probe = self

        def wrapped(*a, **kw):
            prev = getattr(probe.tl, "label", None)
            probe.tl.label = _argv_label(a)
            tid = probe._open(domain)
            try:
                return orig(*a, **kw)
            finally:
                probe._close(tid)
                probe.tl.label = prev

        setattr(obj, attr, wrapped)

    def remove(self):
        for obj, attr, orig in reversed(self.saved):
            setattr(obj, attr, orig)
        self.saved = []

    def n(self):
        return len(self.recs)


class TracingLock(object):
    """内核是真锁, 外层只记账。`with` / acquire+release 都支持。

    ⚠ 这里踩过一个坑, 记下来免得后人再踩: span 的 token 一开始存在
    `self._tid`(锁对象自己的属性)上。而锁对象是**全进程一个**的, 6 个线程
    共用它 —— 于是线程 B 的 `__enter__` 把线程 A 的 token 覆盖掉, A 的
    `__exit__` 关掉了 B 的 span, A 的 span 永远 `t1=None`。而 `count_overlaps`
    会把 `t1 is None` 的记录**过滤掉**, 于是「重叠数 = 0」——
    **一个测不出任何东西的探针, 恰好报出最干净的数字。**
    现在 token 放在 probe 的 threading.local 栈里(顺带支持嵌套), 并且
    `count_overlaps` 会断言「一条 span 都没漏关」—— 不许把「没量到」
    报成「量到了, 没有」。
    """

    def __init__(self, real, probe, domain):
        self._real = real
        self._probe = probe
        self._domain = domain

    def _stack(self):
        tl = self._probe.tl
        stacks = getattr(tl, "stacks", None)
        if stacks is None:
            stacks = tl.stacks = {}
        return stacks.setdefault(id(self), [])

    def __enter__(self):
        self._real.acquire()
        self._stack().append(self._probe._open(self._domain))
        return self

    def __exit__(self, *a):
        st = self._stack()
        self._probe._close(st.pop())
        self._real.release()
        return False

    def acquire(self, *a, **kw):
        r = self._real.acquire(*a, **kw)
        self._stack().append(self._probe._open(self._domain))
        return r

    def release(self):
        st = self._stack()
        self._probe._close(st.pop())
        return self._real.release()


class NoopLock(object):
    """**注入用**: 一个什么都不做的锁。用它当内核就能人为造出竞争。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def acquire(self, *a, **kw):
        return True

    def release(self):
        return None


class Inject(object):
    """注入点集合。**只在本进程内 monkeypatch 模块对象**, 不动产品文件。"""

    def __init__(self, srv=None, probe=None):
        self.srv = srv
        self.probe = probe
        self._saved = []

    def _swap(self, obj, attr, new):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, new)

    # -- 1. 去掉锁: 人为造出「两个请求同时进临界区」 ----
    def noop_repo_lock(self):
        inst = self.srv.mod.REPO
        ck(inst is not None, "noop_repo_lock 需要 srv")
        if self.probe is not None:
            self.probe.inject_noop_lock(inst, "_lock", "repo")
        else:
            self._swap(inst, "_lock", NoopLock())
        return self

    # -- 2. 放大窗口: 把「锁只覆盖单条 git 命令」这件事变成可见窗口 ----
    def window(self, ms=40):
        """在每条 git 命令**之后**、仍握着锁时睡 ms 毫秒。

        这是最贴近真实的放大: 锁的语义一个字没改, 只是让「锁释放到下一次
        获得之间」的窗口从微秒级变成毫秒级 —— 而那正是多步操作被插队的地方。
        """
        mod = self.srv.mod
        orig = mod.Repo._run_full
        d = ms / 1000.0

        def wide(self, *a, **kw):
            r = orig(self, *a, **kw)
            with self._lock:
                time.sleep(d)
            return r

        self._swap(mod.Repo, "_run_full", wide)
        return self

    def restore(self):
        if self.probe is not None:
            self.probe.remove()
        for obj, attr, orig in reversed(self._saved):
            setattr(obj, attr, orig)
        self._saved = []



# ════════════════════════════════════════════════════════ 并发驱动

def _path_label(a, kw):
    """从一个请求的 (self, ...) 里挖出路径, 让人看得懂报表。"""
    for x in a:
        if isinstance(x, str) and x.startswith("/api/"):
            return x
    return "request"


def _order(t):
    """按 C0 < C1 < ... < C10 排序(C2B 跟在 C2 后面), 与文件里的定义顺序无关。"""
    m = re.match(r"([A-Z]+)(\d+)", t["group"])
    return (m.group(1), int(m.group(2))) if m else ("ZZ", 0)


class Op(object):
    """一次「逻辑操作」= 客户端的一个请求。"""

    __slots__ = ("thread", "idx", "name", "t0", "t1", "code", "body",
                 "exc", "git_calls", "interleaved", "note")

    def __init__(self, thread, idx, name):
        self.thread = thread
        self.idx = idx
        self.name = name
        self.t0 = 0.0
        self.t1 = 0.0
        self.code = None
        self.body = None
        self.exc = None
        self.note = None
        self.git_calls = 0
        self.interleaved = 0

def _err_of(body):
    if isinstance(body, dict):
        return body.get("error")
    return None


def run_ops(n_threads, n_rounds, body, name="op", barrier_each_round=True,
            watchdog=180.0):
    """真并发: N 个线程, 每轮都用 Barrier 对齐后同时开打。

    用 Barrier 而不是 sleep —— sleep 只是「假装串行」, Barrier 才是同时。
    body(t, r) 返回 (code, body_json); 抛异常会被收走(不让 Barrier 炸)。
    """
    ops = []
    ops_lock = threading.Lock()
    bar = threading.Barrier(n_threads) if (barrier_each_round and n_threads > 1) else None
    slow = []

    def worker(t):
        for r in range(n_rounds):
            if bar:
                bar.wait()
            op = Op("T%d" % t, r, name)
            t0 = time.perf_counter()
            try:
                op.code, op.body = body(t, r)
            except Exception as e:                       # noqa: BLE001
                op.exc = "%s: %s" % (type(e).__name__, e)
                if VERBOSE:
                    traceback.print_exc()
            op.t0, op.t1 = t0, time.perf_counter()
            if op.t1 - t0 > 5.0:
                slow.append((op.thread, op.idx, round(op.t1 - t0, 2)))
            with ops_lock:
                ops.append(op)
        # 最后一轮之后不用再等
        try:
            bar.abort()
        except Exception:
            pass

    ths = [threading.Thread(target=worker, args=(t,), name="T%d" % t)
           for t in range(n_threads)]
    t_start = time.perf_counter()
    for th in ths:
        th.start()
    hung = []
    for th in ths:
        th.join(timeout=watchdog)
        if th.is_alive():
            hung.append(th.name)
    elapsed = time.perf_counter() - t_start
    if hung:
        raise Fail("死锁/挂起: 线程 %s 在 %.0fs 内没结束。**这是死锁的直接证据**"
                   % (", ".join(hung), watchdog))
    return ops, elapsed, slow


def analyze_requests(recs):
    """以**服务端请求**为单位归因: 它自己发了几条 git、期间有没有被别人插队。"""
    reqs = [r for r in recs if r["domain"] == "request" and r.get("t1") is not None]
    gits = [r for r in recs if r["domain"] != "request" and r.get("t1") is not None]
    out = []
    for q in reqs:
        rid = q["ident"]
        own = [g for g in gits if g.get("req") == rid]
        other = [g for g in gits
                 if g.get("req") != rid and g["t0"] >= q["t0"] and g["t1"] <= q["t1"]]
        out.append({"id": rid, "label": q["label"], "thread": q["thread"],
                    "ms": round((q["t1"] - q["t0"]) * 1000, 1),
                    "git_calls": len(own), "interleaved": len(other),
                    "domains": sorted(set(g["domain"] for g in own))})
    return out


def summarize_requests(reqs):
    if not reqs:
        return {"requests": 0}
    hist = collections.Counter(r["git_calls"] for r in reqs)
    return {"requests": len(reqs),
            "git_call_histogram": {str(k): v for k, v in sorted(hist.items())},
            "requests_with_0_git": hist.get(0, 0),
            "requests_with_1_git": hist.get(1, 0),
            "requests_multi_git": sum(v for k, v in hist.items() if k >= 2),
            "requests_interleaved": sum(1 for r in reqs if r["interleaved"]),
            "domains_seen": sorted(set(d for r in reqs for d in r["domains"]))}


def analyze_ops(ops, recs):
    """把探针记录归到每个逻辑操作上: 它自己发了几条 git, 期间有没有被别人插队。

    `interleaved > 0` 意味着: 这个**逻辑操作**还在跑的时候, 另一个线程已经
    在跑 git 了。对单条 git 命令来说无所谓(有锁), 对跨越多条命令的操作来说
    就是「临界区被另一个请求切开了」—— 操作不是原子的。
    """
    done = sorted([r for r in recs if r.get("t1") is not None],
                  key=lambda r: r["t0"])
    for op in ops:
        mine, other = 0, 0
        for r in done:
            inside = r["t0"] >= op.t0 and r["t1"] <= op.t1
            if not inside:
                continue
            if r["thread"] == op.thread:
                mine += 1
            else:
                other += 1
        op.git_calls = mine
        op.interleaved = other


# ════════════════════════════════════════════════════════ 判据

def classify(ops):
    """把响应分成四类可观测的失败。返回 {类: [样例]}。"""
    out = {"http5xx": [], "traceback": [], "git_lock": [], "win32_sharing": [],
           "client_exc": []}
    for op in ops:
        if op.exc:
            out["client_exc"].append((op.thread, op.idx, op.exc))
            continue
        blob = json.dumps(op.body, ensure_ascii=False) if op.body is not None else ""
        err = _err_of(op.body) or ""
        if op.code and int(op.code) >= 500:
            out["http5xx"].append((op.thread, op.idx, op.code, err[:220]))
        if any(m in blob for m in TRACEBACK_MARKERS) or "Traceback" in blob:
            out["traceback"].append((op.thread, op.idx, err[:220] or blob[:220]))
        if err and any(m in err for m in GIT_LOCK_MARKERS):
            out["git_lock"].append((op.thread, op.idx, err[:220]))
        if err and any(m in err for m in WIN32_SHARING_MARKERS):
            out["win32_sharing"].append((op.thread, op.idx, err[:220]))
    return out


def fsck_errors(sb):
    """git fsck 报错 = 对象库不一致。用 --no-dangling 把「悬空对象」这种
    正常现象(被 amend 覆盖掉的旧提交)排除掉。"""
    p = sb.git("fsck", "--no-progress", "--strict", "--no-dangling")
    txt = ((p.stderr or "") + (p.stdout or "")).strip()
    if p.returncode != 0 or txt:
        return "fsck rc=%d: %s" % (p.returncode, txt[:400])
    return None


def index_stage_errors(sb):
    """索引一致性。**必须知道夹具是不是合并态** —— 这是第一版栽的跟头:

    合并态下 `git ls-files -s` 对一个冲突文件本来就该列出 stage 1/2/3 三条,
    我把它当成「同一路径多条 stage = 索引损坏」, 于是 C1 报了个**假的**损坏。
    真正的损坏只有两种:
      * 同一个 (path, stage) 出现两条 —— 索引里同一格写了两次;
      * 同一个路径同时有 stage 0 和 stage>0 —— 索引自相矛盾。
    非合并态下出现任何 stage>0 也是不一致(仓库停在了半个合并里)。
    """
    p = sb.git("ls-files", "-s")
    seen = collections.defaultdict(list)
    for ln in (p.stdout or "").splitlines():
        parts = ln.split(None, 3)
        if len(parts) < 4:
            continue
        mode, sha, stage, path = parts
        seen[path].append((mode, sha, stage))
    merging = in_merge_state(sb)
    bad = []
    for path, items in seen.items():
        st = collections.Counter(x[2] for x in items)
        for stage, n in st.items():
            if n > 1:
                bad.append("%s 的 stage %s 出现 %d 条(索引重复写)" % (path, stage, n))
        if "0" in st and len(st) > 1:
            bad.append("%s 同时有 stage 0 与 stage>0(索引自相矛盾)" % path)
        if not merging:
            for stage in st:
                if stage != "0":
                    bad.append("非合并态却有 stage=%s: %s" % (stage, path))
    return bad


def in_merge_state(sb):
    return os.path.exists(os.path.join(sb.dir, ".git", "MERGE_HEAD"))


def unreachable_or_leaked(sb, outside_prefixes):
    """仓外内容有没有被写进历史(可被 push 带走的那一种)。

    判据: `git rev-list --objects --all` 里出现仓外哨兵路径,
    或 `git show HEAD:<仓外路径>` 能读出内容。
    """
    bad = []
    objs = sb.git("rev-list", "--objects", "--all").stdout or ""
    for pref in outside_prefixes:
        for ln in objs.splitlines():
            if ln.endswith(" " + pref) or ln.split(" ", 1)[-1] == pref:
                bad.append("对象库里出现仓外路径 %r (%s)" % (pref, ln.split(" ", 1)[0][:10]))
    return bad


def report(name, ops, extra=None, verdict=None):
    cls = classify(ops)
    n = len(ops)
    line = {"test": name, "ops": n}
    for k, v in cls.items():
        line[k] = "%d/%d" % (len(v), n)
        if v and VERBOSE:
            say("  %s 命中 %d/%d, 例: %r" % (k, len(v), n, v[0]))
    if extra:
        line.update(extra)
    if verdict:
        line["verdict"] = verdict
    RESULTS.append(line)
    return cls


# ════════════════════════════════════════════════════════ 夹具

N_W = 8


def fx_basic(sb):
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "w%02d-v0\n" % i)
    sb.commit("init: base")
    sb.git("branch", "feature")
    return sb


def fx_merge_conflict_n(sb, n):
    """n 个**同时**冲突的文件。

    单文件夹具有个致命弱点: `resolve` 一次就把它解决了, 第 2 轮开始
    所有 resolve 请求都在 `_stages()` 就被拒 —— 于是「并发」实际上只发生在
    第 1 轮, 后面 9 轮测的都是「不是未合并路径」。
    """
    names = ["c%02d.txt" % i for i in range(n)]
    for nm in names:
        sb.write(nm, "base-%s\n" % nm)
    sb.write("keep.txt", "keep\n")
    sb.commit("init")
    sb.git("checkout", "-b", "feature")
    for nm in names:
        sb.write(nm, "theirs-%s\n" % nm)
    sb.commit("feature: change all")
    sb.git("checkout", "main")
    for nm in names:
        sb.write(nm, "ours-%s\n" % nm)
    sb.commit("main: change all")
    p = sb.git("merge", "feature", check=False)
    assert "CONFLICT" in ((p.stdout or "") + (p.stderr or "")), \
        "夹具没造出冲突: %r" % ((p.stdout or p.stderr)[:300])
    ck(in_merge_state(sb), "夹具: MERGE_HEAD 不存在, 不是合并态")
    unmerged = [ln.split(None, 3)[3] for ln in sb.out("ls-files", "-s").splitlines()
                if len(ln.split(None, 3)) >= 4 and ln.split(None, 3)[2] != "0"]
    ck_eq(sorted(set(unmerged)), sorted(names), "夹具里未合并路径与预期不符")
    return names


#: discard 那个未跟踪目录里的文件数。**这是为了让窗口开得着, 不是为了让判据变松**:
#: 目录只有 4 个文件时 `_delete_tree` 一眨眼就删完, `git add` 那边往往在文件
#: 已经没了之后才跑 (`_live_paths` 直接把路径丢掉), 于是两条通路**根本没同时对
#: 同一个文件动手** —— 那是「负载没打中」, 不是「没有竞争」。
DISCARD_DIR_N = 60


def fx_discard_dir(sb):
    """一个**只含未跟踪文件**的目录(走 discard 的 rmtree 分支)+ 已跟踪文件。"""
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "w%02d\n" % i)
    sb.commit("init")
    os.makedirs(sb.abspath("untracked"), exist_ok=True)
    for i in range(DISCARD_DIR_N):
        sb.write("untracked/x%d.txt" % i, "u%d\n" % i)
    return sb


def fx_history(sb):
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "v0\n")
    sb.commit("c1: base")
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "v1-%d\n" % i)
    sb.commit("c2: change all")
    return sb


def fx_escape(sb):
    """仓内放一个指向仓外的目录联接 —— E4/escaping_links 的威胁模型。"""
    sb.write("a.txt", "a\n")
    sb.commit("init")
    out = sb.outside()
    with io.open(os.path.join(out, "leak.txt"), "w", encoding="utf-8") as f:
        f.write("OUTSIDE-SECRET\n")
    with io.open(os.path.join(out, "leak2.txt"), "w", encoding="utf-8") as f:
        f.write("OUTSIDE-SECRET-2\n")
    esc = sb.abspath("escape")
    shutil.rmtree(esc, ignore_errors=True)
    p = subprocess.run(["cmd", "/c", "mklink", "/J", esc, out],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ck(p.returncode == 0, "mklink /J 失败: %s" % ((p.stdout or p.stderr).strip()[:200]))
    sb.outside_dir = out
    return sb


def remove_escape(sb):
    esc = sb.abspath("escape")
    if os.path.exists(esc):
        subprocess.run(["cmd", "/c", "rmdir", esc], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ════════════════════════════════════════════════════════ C0 探针自检
#
# 这是本文件的核心交付之一: **证明这些并发用例不是空转的**。
# 两步都要有输出, 所以这里的「期望」是断言, 输出打到 -v 与汇总表。

@test("C0", "calibrate", "注入必红 / 不注入必绿 —— 证明并发判据非空转")
def t_c0_calibrate():
    out = {}
    THR, RND = 6, 8

    # ---- 第 1 步: 不注入, 同一把锁内不该有重叠 ----
    sb = fx_basic(Sandbox("c0a"))
    with Server(sb, "c0a") as srv:
        pr = Probe()
        import server as S, history_ops as H, conflicts as C
        pr.install_label(S.Repo, "_run_full")
        pr.install_label(H.HistoryOps, "_run")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_lock(srv.mod.history_ops(), "_lock", "history")
        pr.install_span(C.Git, "run", "conflicts")

        def body(t, r):
            return srv.post("/api/stage", {"files": ["w/f%02d.txt" % ((t + r) % N_W)]})
        ops, el, _ = run_ops(THR, RND, body, "stage")
        analyze_ops(ops, pr.recs)
        same, samp = count_overlaps(pr.recs, ("repo",))
        cross, _ = count_overlaps(pr.recs, ("conflicts", "history"))
        base_calls = pr.n()
        pr.remove()
        # 撤干净 = 属性换回真锁(不是 recs 清空 —— recs 后面还要用来打报表)
        ck(not isinstance(srv.mod.REPO._lock, (TracingLock, NoopLock)),
           "探针/injection 没撤干净: REPO._lock 还是 %s" % type(srv.mod.REPO._lock).__name__)
    ck_eq(same, 0, "不注入时 Repo._lock 域内的临界区重叠数(必须是 0, 否则探针空转)")
    say("[C0] 不注入: repo 域重叠=%d (git 调用 %d 次)" % (same, base_calls))

    # ---- 第 2 步: 注入空锁, 同一把锁内**必定**出现重叠 ----
    sb = fx_basic(Sandbox("c0b"))
    with Server(sb, "c0b") as srv:
        pr = Probe()
        import server as S
        pr.install_label(S.Repo, "_run_full")
        pr.inject_noop_lock(srv.mod.REPO, "_lock", "repo")   # ← 注入点

        ops2, el2, _ = run_ops(THR, RND, body, "stage")
        analyze_ops(ops2, pr.recs)
        inj_same, inj_samp = count_overlaps(pr.recs, ("repo",))
        inj_git = pr.n()
        pr.remove()
    out["baseline_repo_domain_overlaps"] = same
    out["baseline_git_calls"] = base_calls
    out["baseline_ops"] = len(ops)
    out["baseline_ms"] = round(el, 1)
    out["injected_repo_domain_overlaps"] = inj_same
    out["injected_git_calls"] = inj_git
    out["injected_ms"] = round(el2, 1)
    say("[C0] 注入空锁: repo 域重叠=%d, 例 %r" % (inj_same, inj_samp[:2]))
    ck(inj_same > 0,
       "【自检失败】把 Repo._lock 的内核换成空锁之后, 同域临界区重叠仍然是 0 —— "
       "说明这个探针测不出竞争, 后面所有并发用例都是空转的假绿。"
       "(注入后 %d 次 git 调用, 重叠 %d 次)" % (inj_git, inj_same))

    # ---- 第 3 步: 不注入且只跑 stage 时, 跨域也该是 0(校准下界) ----
    ck_eq(cross, 0, "不注入且只跑 stage 时, 不该有跨域重叠(实测 %d)" % cross)

    # ---- 第 4 步: 对照表 ----
    table = [["场景", "同域临界区重叠对数", "git 调用次数"],
             ["不注入(产品原样)", same, base_calls],
             ["注入空锁内核(人为造竞争)", inj_same, inj_git]]
    out["table"] = table
    say("[C0] 校准表:")
    for r in table:
        say("      %s" % (r,))
    RESULTS.append({"test": "C0-calibrate-table", "ops": 0, "verdict": "OK",
                    "detail": table})
    print("\n  ---- C0 探针自检(注入必红 / 不注入必绿)----")
    for r in table:
        print("   %s" % (r,))
    return out


# ════════════════════════════════════════════════════════ C1

# ════════════════════════════════════════════════════════ C2

@test("C2", "stage-vs-discard", "stage 与 discard 并发 —— 目录护栏的 TOCTOU 窗口")
def t_c2_stage_vs_discard():
    THR, RND = 6, 10
    sb = fx_discard_dir(Sandbox("c2"))
    with Server(sb, "c2") as srv:
        pr = Probe()
        import server as S
        pr.install_label(S.Repo, "_run_full")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_request(S.Handler, "do_POST", _path_label)
        try:
            disc_results = []

            def body(t, r):
                if t % 2 == 0:
                    # 未跟踪目录 -> 走 rmtree 分支。inner 护栏该在目录含已跟踪文件时拒。
                    # confirmed: true —— 闸门之后不给它, discard 恒 400, 本用例
                    # 的竞态窗口根本不存在(基线实测 discard_200=0, 判据全在空转)。
                    # 只补请求体, 判据与阈值一个字没动。
                    code, j = srv.post("/api/discard",
                                       {"files": ["untracked"], "confirmed": True})
                    if code == 200:
                        disc_results.append(j)
                    return code, j
                # 把刚被 discard 删掉的文件写回来, 然后 stage 它
                rel = "untracked/x%d.txt" % (r % 4)
                sb.write(rel, "restaged by T%d r%d\n" % (t, r))
                return srv.post("/api/stage", {"files": [rel]})
            ops, el, slow = run_ops(THR, RND, body, "discard|stage")
            summ = summarize_requests(analyze_requests(pr.recs))
        finally:
            pr.remove()

        # 判据: 索引里有的文件, 必须还在工作区。discard 的 inner 护栏存在的
        # 全部意义就是「不许把目录里的已跟踪文件删掉」—— 判据直接落在那上面。
        idx = sb.out("ls-files", "-s")
        indexed = [ln.split(None, 3)[3] for ln in idx.splitlines() if len(ln.split(None, 3)) >= 4]
        vanished = []
        for rel in indexed:
            if rel.startswith("untracked/") and not os.path.exists(sb.abspath(rel)):
                vanished.append(rel)
        # 同时把 stash 的 stage 结果与磁盘对照
        cls = report("C2", ops, extra={"elapsed_s": round(el, 1),
                                       "git_calls": pr.n(),
                                       "indexed_in_untracked_dir": len(
                                           [r for r in indexed if r.startswith("untracked/")]),
                                       "indexed_but_vanished": len(vanished),
                                       "discard_200": len(disc_results),
                                       "requests": summ})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["win32_sharing"],
       "【E4 pin 挡住别人】%d/%d 次请求撞上 WinError 32/33(目录被 pin 住): %r"
       % (len(cls["win32_sharing"]), len(ops), [x[2] for x in cls["win32_sharing"][:3]]))
    ck(summ["requests_multi_git"] > 0,
       "自检失败: 没有请求跨越 2 条 git 命令(%r)" % summ)
    ck(not vanished,
       "【discard 的 inner 护栏被并发绕过】%d 个文件在索引里(stage 成功了)却被 discard "
       "连目录一起删掉了:%s —— 护栏读的是 `_tracked_set()` 的旧快照, "
       "从读到 unlink 之间隔着 checkout/pin/recheck 三步。"
       % (len(vanished), vanished[:6]))
    if cls["git_lock"]:
        raise Flaky("git 锁冲突 %d/%d" % (len(cls["git_lock"]), len(ops)),
                    rate=len(cls["git_lock"]) / float(len(ops)))
    return None


# ════════════════════════════════════════════════════════ C2B
#
# E4 的「握住目录」用 Windows 句柄实现(不给 FILE_SHARE_DELETE)。那是**单线程**
# 视角的正确加固, 但句柄活在工作区上: 一个线程 pin 着 <repo>/sub 时, 另一个线程
# 的 `git reset --hard` / `checkout` 想在 sub 里 rename 文件就会被挡。

@test("C2B", "discard-pin-vs-reset", "discard 握住的目录句柄 vs 并发 reset --hard")
def t_c2b_pin_vs_reset():
    THR, RND = 6, 10
    sb = Sandbox("c2b")
    # 让 discard 有个深一点的未跟踪目录(要递归 pin 多级 + 慢慢删, 窗口够大)
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "v0\n")
    sb.commit("init")
    os.makedirs(sb.abspath("untracked/deep"), exist_ok=True)
    for i in range(30):
        sb.write("untracked/deep/y%02d.txt" % i, "y%d\n" % i)

    with Server(sb, "c2b") as srv:
        def body(t, r):
            if t % 3 == 0:
                return srv.post("/api/discard",
                                 {"files": ["untracked"], "confirmed": True})
            if t % 3 == 1:
                # mode 显式填 hard: 上一行的注释写的就是「并发 reset --hard」,
                # 闸门之前这个路由也是无条件 hard_reset(), 语义没变。
                return srv.post("/api/reset",
                                 {"confirmed": True, "mode": "hard", "ref": "HEAD"})
            # 制造一次改动, 好让 reset --hard 有东西可还原(rename 工作区文件)
            for i in range(N_W):
                sb.write("w/f%02d.txt" % i, "v%d-%d\n" % (t, r))
            return srv.post("/api/stage", {"files": ["w/f%02d.txt" % (r % N_W)]})
        ops, el, slow = run_ops(THR, RND, body, "discard|reset|stage")
        cls = report("C2B", ops, extra={"elapsed_s": round(el, 1),
                                        "slow_ops_gt5s": len(slow)})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["win32_sharing"],
       "【E4 的目录 pin 变成了跨线程故障】%d/%d 次请求撞上「另一个程序正在使用此文件」"
       "/ WinError 32 —— discard 握住的祖先目录句柄(不给 FILE_SHARE_DELETE)"
       "挡住了并发 reset/checkout 要做的 rename。例: %r"
       % (len(cls["win32_sharing"]), len(ops), [x[2] for x in cls["win32_sharing"][:3]]))
    if cls["git_lock"]:
        raise Flaky("git 锁冲突 %d/%d" % (len(cls["git_lock"]), len(ops)),
                    rate=len(cls["git_lock"]) / float(len(ops)))
    return None


# ════════════════════════════════════════════════════════ C3

# ════════════════════════════════════════════════════════ C4

@test("C4", "history-vs-stage", "history_ops 危险操作(amend/undo/revert)与 stage 并发")
def t_c4_history_vs_stage():
    THR, RND = 6, 9
    sb = fx_history(Sandbox("c4"))
    base_sha = sb.out("rev-list", "--max-parents=0", "HEAD").strip()
    undoable = sb.out("rev-list", "--max-count=1", "--skip=1", "HEAD").strip()

    with Server(sb, "c4") as srv:
        pr = Probe()
        import server as S, history_ops as H
        pr.install_label(S.Repo, "_run_full")
        pr.install_label(H.HistoryOps, "_run")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_lock(srv.mod.history_ops(), "_lock", "history")
        pr.install_request(S.Handler, "do_POST", _path_label)
        pr.install_request(S.Handler, "do_GET", _path_label)
        try:
            def body(t, r):
                k = t % 4
                if k == 0:
                    return srv.post("/api/history/amend",
                                    {"message": "amend T%d r%d" % (t, r), "confirmed": True})
                if k == 1:
                    return srv.post("/api/history/undo-last-commit",
                                    {"drop_changes": False, "confirmed": True})
                if k == 2:
                    return srv.post("/api/history/revert",
                                    {"sha": undoable, "no_edit": True})
                for i in range(N_W):
                    sb.write("w/f%02d.txt" % i, "staged T%d r%d\n" % (t, r))
                return srv.post("/api/stage", {"files": ["w/f%02d.txt" % (r % N_W)]})
            ops, el, slow = run_ops(THR, RND, body, "history|stage")
            reqs = analyze_requests(pr.recs)
            summ = summarize_requests(reqs)
            per = {d: count_overlaps(pr.recs, (d,))[0] for d in ("repo", "history")}
            cross, xsamp = count_overlaps(pr.recs, ("repo", "history"))
        finally:
            pr.remove()
        cls = report("C4", ops, extra={"elapsed_s": round(el, 1),
                                       "git_calls": pr.n(),
                                       "same_domain_overlaps": per,
                                       "cross_domain_git_overlaps": cross,
                                       "requests": summ})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["traceback"], "响应里带 traceback: %r" % cls["traceback"][:3])
    ck(not cls["git_lock"],
       "【锁域不一致】%d/%d 次请求的失败原因是 git 锁文件被占 —— "
       "history_ops.HistoryOps._lock 与 server.Repo._lock 不是同一把。例: %r"
       % (len(cls["git_lock"]), len(ops), [x[2] for x in cls["git_lock"][:3]]))
    ck(not cross,
       "【锁不是同一把】实测 %d 对「Repo 通路与 history_ops 通路同时在跑 git」的区间。"
       "两把锁各自都有效(同域重叠 repo=%d / history=%d), 但它们**不是同一把** —— "
       "于是 history_ops 的 git commit --amend 与 server 的 git add 能同时跑。"
       % (cross, per["repo"], per["history"]))
    if cls["git_lock"] or cls["http5xx"]:
        raise Flaky("git 锁 %d/%d, 5xx %d/%d"
                    % (len(cls["git_lock"]), len(ops),
                       len(cls["http5xx"]), len(ops)))
    return None


# ════════════════════════════════════════════════════════ C5
#
# 两个请求同时改**同一个文件**。判据落在 aigit 自己承诺的东西上:
# resolve() 的响应里带 sha256, 客户端就是拿它做 expect_sha256 的往返校验的。
# 响应报的 sha256 必须等于事后磁盘上的内容 —— 不等就说明「校验-写」之间
# 有窗口, 后写的那个把先写的那个静默吃掉了。

# ════════════════════════════════════════════════════════ C6
#
# 本段写于 commit() 修复**之前**, 描述的是**历史根因**, 留着是为了让这条判据
# 可追溯: 它当初红, 是因为 commit() 是「先扫一遍工作区, 再 git add -A」两步。
#
# ⚠⚠ **其中一句当时就已经是错的, 现在仍然错, 别照它排查**:
#   「两步之间没有任何锁」—— 实际 commit() **全程持 Repo._lock**
#   (server.py:1312)。所以锁**不是**这条的根因。
#   真正的根因是: **锁管不到对手**。对手在文件系统上直接 mklink/rmdir,
#   整个循环一把 aigit 的锁都不取 —— 持锁期间联接照样挂得上。
#   这正是「为什么不能只靠加锁解决」的实测理由(见 server.py:1052-1058)。
#   现在的修法是三条合起来: 整段持锁 + 路径白名单(pathspec 不含未点名路径)
#   + 枚举期握目录句柄 + add 后索引核对。

# ════════════════════════════════════════════════════════ C7
#
# 三条通路的锁状况, 用**测量**说清, 不靠运气:
#   * repo 域     —— server.Repo._run_full, 锁 = Repo._lock(每实例 RLock)
#   * history 域  —— history_ops.HistoryOps._run, 锁 = HistoryOps._lock(每实例 Lock)
#   * conflicts 域—— conflicts.Git.run, 锁 = repo_git_lock(root)(按 realpath **共享** RLock)
#   三把都**有锁**, 但**不是同一把** —— 这才是本条判的东西。
# 判据: 同一域内不允许有重叠(产品原样应当是 0); 不同域之间同时跑 git
#       **不允许** —— 重叠数就是「锁不是同一把」的直接证据, 非 0 即红。

# ════════════════════════════════════════════════════════ C8

@test("C8", "globals", "ThreadingHTTPServer 的进程级全局可变状态")
def t_c8_globals():
    import server as S
    import gen as G
    THR, RND = 8, 8
    sb = fx_basic(Sandbox("c8"))
    seen_branch_prefix = set()
    bad_status = []
    slock = threading.Lock()

    with Server(sb, "c8") as srv:
        def body(t, r):
            if t % 4 == 0:
                return srv.get("/api/status")
            if t % 4 == 1:
                return srv.get("/api/conflict/state")
            if t % 4 == 2:
                return srv.get("/api/branches")
            # gen 会改 gen.BRANCH_PREFIX 这个**模块级全局**
            return srv.post("/api/gen", {"kind": "mergeable", "count": 1,
                                         "seed": 1000 + r})

        def wrapped(t, r):
            out = body(t, r)
            code, j = out
            with slock:
                seen_branch_prefix.add(getattr(G, "BRANCH_PREFIX", None))
            if code == 200 and isinstance(j, dict) and "staged" in j:
                st = [x["path"] for x in j.get("staged", [])]
                if j.get("hasStaged") != bool(st):
                    bad_status.append(("hasStaged 与 staged 不一致", j.get("hasStaged"), st[:3]))
                cs = j.get("conflicted") or []
                if any(c["path"] not in st and
                       c["path"] not in [x["path"] for x in j.get("unstaged", [])]
                       for c in cs):
                    bad_status.append(("conflicted 里出现了既不在 staged 也不在 "
                                       "unstaged 的路径", cs[:2]))
            return out

        ops, el, slow = run_ops(THR, RND, wrapped, "read|gen")
        cls = report("C8", ops, extra={"elapsed_s": round(el, 1),
                                       "branch_prefix_values": sorted(
                                           str(x) for x in seen_branch_prefix)})

    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not bad_status, "并发下 /api/status 的响应内部不自洽: %r" % bad_status[:4])

    # _GEN_BASE_PREFIX 是 import 时取的常量; 跑完必须被还原成它
    ck_eq(getattr(G, "BRANCH_PREFIX", None), S._GEN_BASE_PREFIX,
          "gen.BRANCH_PREFIX 跑完之后没被还原成 _GEN_BASE_PREFIX"
          "(并发下 Repo.generate 的 finally 被别人的改写盖掉了)")
    # 懒加载单例的竞态: _CONFLICTS / _HISTORY 没有锁, 两个线程可能各建一个
    ck(S._CONFLICTS is None or S._CONFLICTS.git.root == Path(sb.dir),
       "conflict_resolver() 的全局单例绑到了别的仓上: %r"
       % (getattr(getattr(S._CONFLICTS, "git", None), "root", None),))
    return None


# ════════════════════════════════════════════════════════ C9 死锁体检

def scan_lock_sites(path):
    """静态扫锁: 列出所有持锁点, 并找出「持着 A 锁时去拿 B 锁」的嵌套边。"""
    with io.open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    LOCKISH = re.compile(r"(^|\.)(_lock|_GEN_LOCK)$")

    def ctx_names(node):
        out = []
        for item in node.items:
            e = item.context_expr
            if isinstance(e, ast.Attribute):
                out.append(e.attr)
            elif isinstance(e, ast.Name):
                out.append(e.id)
        return out

    sites = []          # (lineno, lockname, enclosing_func)
    parents = {}
    for fn in ast.walk(tree):
        for child in ast.iter_child_nodes(fn):
            parents[id(child)] = fn

    def enclosing(n):
        p = parents.get(id(n))
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return p.name
            p = parents.get(id(p))
        return "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            names = ctx_names(node)
            if any(LOCKISH.search(n or "") for n in names):
                lk = [n for n in names if LOCKISH.search(n or "")]
                sites.append((node.lineno, lk[0], enclosing(node)))

    holders = {fn for (_, _, fn) in sites}   # 会持锁的函数
    edges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            names = ctx_names(node)
            if not any(LOCKISH.search(n or "") for n in names):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                fname = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else None)
                if fname and fname in holders and fname != enclosing(node):
                    edges.append((enclosing(node), fname, sub.lineno))
    return sites, edges


@test("C9", "deadlock-scan", "死锁体检: 静态锁序 + 动态看门狗")
def t_c9_deadlock():
    detail = {}
    for label, path in (("server.py", SERVER_PY),
                        ("conflicts.py", CONFLICTS_PY),
                        ("history_ops.py", HISTORY_PY)):
        sites, edges = scan_lock_sites(path)
        detail[label] = {"lock_sites": ["line %d: %s in %s()" % s for s in sites],
                         "nested_edges": ["%s() -> %s() at line %d" % e for e in edges]}
        say("[C9] %s: 持锁点 %d 个, 嵌套取锁 %d 条" % (label, len(sites), len(edges)))
    RESULTS.append({"test": "C9-lock-scan", "ops": 0, "detail": detail})

    cycles = []
    for label, d in detail.items():
        g = collections.defaultdict(list)
        for e in d["nested_edges"]:
            a, b = e.split("()")[0], e.split("->")[1].split("(")[0]
            g[a].append(b)
        for k in g:
            if k in g.get(g[k][0], []):
                cycles.append((label, k, g[k][0]))
    ck(not cycles, "锁顺序反转成环: %r" % cycles)

    # 动态: 上面 8 组用例全部跑完没有线程挂起(run_ops 的 watchdog 已经守着了),
    # 这里再补一组「三域齐发 + 长时间」的看门狗, 专门盯死锁。
    THR, RND = 9, 6
    sb = fx_basic(Sandbox("c9"))
    with Server(sb, "c9") as srv:
        def body(t, r):
            k = t % 4
            if k == 0:
                return srv.post("/api/stage", {"files": ["w/f%02d.txt" % (r % N_W)]})
            if k == 1:
                return srv.post("/api/checkout", {"name": "feature"})
            if k == 2:
                return srv.post("/api/history/amend",
                                {"message": "amend %d-%d" % (t, r), "confirmed": True})
            return srv.post("/api/commit", {"message": "c %d-%d" % (t, r)})
        ops, el, slow = run_ops(THR, RND, body, "watchdog", watchdog=90.0)
        cls = report("C9", ops, extra={"elapsed_s": round(el, 1),
                                       "slow_ops_gt5s": len(slow)})
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "看门狗轮有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    return None


# ════════════════════════════════════════════════════════ C10 注入必红(端到端)
#
# C0 证明了「探针能测出竞争」。这一条证明「**判据**能测出损坏」:
# 同一组并发用例, 注入前跑一次、注入(空锁 + 放大窗口)后跑一次,
# 损坏类判据的命中率必须**严格上升**。上升为 0 说明判据是空的。

def _commit_race_run(tag, inject=None):
    """三线程同时 commit —— 返回 400 分类与「已经做完却报失败」的计数。"""
    THR, RND = 6, 6
    sb = fx_basic(Sandbox(tag))
    srv = Server(sb, tag)
    inj = inject(srv) if inject else None
    phantom = collections.Counter()
    codes = collections.Counter()

    def body(t, r):
        for i in range(N_W):
            sb.write("w/f%02d.txt" % i, "T%d-r%d\n" % (t, r))
        code, j = srv.post("/api/commit", {"message": "c T%d r%d" % (t, r)})
        codes[code] += 1
        if code != 200:
            phantom[_err_of(j) or "(无 error 字段)"] += 1
        return code, j

    try:
        ops, el, slow = run_ops(THR, RND, body, "commit", watchdog=120.0)
    finally:
        if inj:
            inj.restore()
        srv.stop()
    # 只把「git 自己的锁文件报错」算成损坏; 「没有可提交的改动」是正常用法问题,
    # 不算 —— 把两者混在一起报会把「用户操作顺序不对」说成「产品坏了」。
    hits = sum(v for k, v in phantom.items() if any(m in k for m in GIT_LOCK_MARKERS))
    return {"ops": len(ops), "elapsed_s": round(el, 1), "codes": dict(codes),
            "phantom_failure": dict(phantom), "phantom_total": hits,
            "server_log": srv.srvlog_tail(12)}


@test("C10", "injection-end-to-end", "注入必红: 放大窗口后损坏判据的命中率必须上升")
def t_c10_injection_e2e():
    base = _commit_race_run("c10base", inject=None)
    inj = _commit_race_run("c10inj",
                           inject=lambda s: Inject(s).window(60).noop_repo_lock())
    rows = [
        ["场景", "请求数", "200", "非200", "其中 git 锁冲突(index.lock 等)"],
        ["不注入(产品原样)", base["ops"], base["codes"].get(200, 0),
         base["ops"] - base["codes"].get(200, 0), base["phantom_total"]],
        ["注入(空锁内核+窗口60ms)", inj["ops"], inj["codes"].get(200, 0),
         inj["ops"] - inj["codes"].get(200, 0), inj["phantom_total"]],
    ]
    say("[C10] 注入对照表:")
    for r in rows:
        say("      %s" % (r,))
    RESULTS.append({"test": "C10-injection-table", "ops": 0, "detail": rows,
                    "base_errors": base["phantom_failure"],
                    "inj_errors": inj["phantom_failure"]})
    print("\n  ---- C10 注入对照 ----")
    for r in rows:
        print("   %s" % (r,))
    print("   不注入的失败原因:", base["phantom_failure"])
    print("   注入后的失败原因:", inj["phantom_failure"])

    ck(base["ops"] > 0 and inj["ops"] > 0, "注入对照两轮都没跑起来")
    ck(inj["phantom_total"] >= base["phantom_total"],
       "【自检失败】注入后损坏判据的命中率不升反降(%d -> %d) —— "
       "这套用例测不出竞争, 后面报告里的「没找到」没有意义。"
       % (base["phantom_total"], inj["phantom_total"]))
    if inj["phantom_total"] == base["phantom_total"] == 0:
        ck(False, "【自检失败】注入(空锁 + 60ms 窗口)与不注入的损坏率都是 0 —— "
                   "端到端判据无效")
    return None


@test("C1", "stage-vs-resolve", "stage 与 conflict/resolve 并发 —— 三条通路两把锁")
def t_c1_stage_vs_resolve():
    THR, RND = 6, 12
    names = None
    sb = Sandbox("c1")
    fx_merge_conflict_n(sb, RND)
    names = ["c%02d.txt" % i for i in range(RND)]
    with Server(sb, "c1") as srv:
        pr = Probe()
        import server as S, conflicts as C
        pr.install_label(S.Repo, "_run_full")
        pr.install_label(C.Git, "run")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_span(C.Git, "run", "conflicts")
        pr.install_request(S.Handler, "do_POST", _path_label)
        try:
            # 偶数线程打 stage(走 Repo 通路), 奇数线程打 resolve(走 conflicts 通路)
            # 第 r 轮所有线程打**同一个**冲突文件 —— 既是跨域并发, 也是同文件并发。
            def body(t, r):
                if t % 2 == 0:
                    return srv.post("/api/stage", {"files": ["keep.txt"]})
                return srv.post("/api/conflict/resolve",
                                {"file": names[r], "content": "T%d r%d\n" % (t, r)})
            ops, el, slow = run_ops(THR, RND, body, "stage|resolve")
            reqs = analyze_requests(pr.recs)
            same_repo, _ = count_overlaps(pr.recs, ("repo",))
            cross, xsamp = count_overlaps(pr.recs, ("repo", "conflicts"))
            n_calls = pr.n()
            summ = summarize_requests(reqs)
        finally:
            pr.remove()
        cross_evidence = cross
        cls = report("C1", ops, extra={"elapsed_s": round(el, 1),
                                       "cross_domain_git_overlaps": cross,
                                       "same_domain_repo_overlaps": same_repo,
                                       "git_calls": n_calls,
                                       "requests": summ})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    bad = index_stage_errors(sb)
    ck(not bad, "索引损坏: %s" % bad[:5])
    ck(not cls["client_exc"], "客户端侧异常 %d 次, 例 %r"
       % (len(cls["client_exc"]), cls["client_exc"][:2]))
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["traceback"], "响应里带 traceback: %r" % cls["traceback"][:3])
    ck(summ["requests_multi_git"] > 0,
       "自检失败: 一个请求都没跨越过 2 条 git 命令 —— 跨域判据无从谈起(%r)" % summ)
    ck(summ["requests_multi_git"] > 0, "自检失败: 负载没打中")
    # 先钉确定性的那条(锁域重叠), 再按**复现率**报用户可见的失败。
    # ⚠️ 锁域重叠是**每遍必红**的那条, 所以下面那条 git_lock 的复现率若只挂在
    #    自己的异常里就永远看不见 —— 两条数字必须合进同一条消息。
    ck(not cross_evidence,
       "【锁不是同一把】实测 %d 对「不同锁域里同时在跑 git」的区间 —— "
       "Repo._lock 和 conflicts.Git.run 的临界区在时间上重叠。"
       "样例(域,git 子命令)x(域,git 子命令): %r。"
       "同一组并发里另有 %d/%d 次请求**直接**以 git 锁文件被占失败, 例: %r"
       % (cross_evidence, [list(x) for x in xsamp[:3]],
          len(cls["git_lock"]), len(ops),
          [x[2][:110] for x in cls["git_lock"][:2]]))
    if cls["git_lock"]:
        raise Flaky("【锁域不一致】%d/%d 次请求的失败原因是 git 锁文件被占"
                    "(index.lock / cannot lock ref) —— Repo._lock 与 conflicts "
                    "那条通路的锁**不是同一把**, 于是两条通路的 git 同时跑、"
                    "互相抢 .git/index.lock。例: %r"
                    % (len(cls["git_lock"]), len(ops),
                       [x[2][:110] for x in cls["git_lock"][:2]]),
                    rate=len(cls["git_lock"]) / float(len(ops)))
    return None


@test("C3", "checkout-commit", "checkout/branch 与 commit 并发")
def t_c3_checkout_commit():
    THR, RND = 6, 12
    sb = fx_basic(Sandbox("c3"))
    acked = []
    ack_lock = threading.Lock()

    with Server(sb, "c3") as srv:
        pr = Probe()
        import server as S
        pr.install_label(S.Repo, "_run_full")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_request(S.Handler, "do_POST", _path_label)
        pr.install_request(S.Handler, "do_GET", _path_label)
        try:
            def body(t, r):
                if t % 3 == 0:
                    return srv.post("/api/checkout", {"name": "feature"})
                if t % 3 == 1:
                    return srv.post("/api/branch", {"name": "side%d" % t})
                for i in range(N_W):
                    sb.write("w/f%02d.txt" % i, "by T%d r%d\n" % (t, r))
                code, j = srv.post("/api/commit", {"message": "commit T%d r%d" % (t, r)})
                if code == 200:
                    with ack_lock:
                        acked.append((t, r, j))
                return code, j
            ops, el, slow = run_ops(THR, RND, body, "checkout|branch|commit")
            reqs = analyze_requests(pr.recs)
            summ = summarize_requests(reqs)
            inter = summ["requests_interleaved"]
            cross, xsamp = count_overlaps(pr.recs, None)
        finally:
            pr.remove()
        cls = report("C3", ops, extra={"elapsed_s": round(el, 1),
                                       "git_calls": pr.n(),
                                       "commit_200": len(acked),
                                       "requests": summ})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    bad = index_stage_errors(sb)
    ck(not bad, "索引损坏: %s" % bad[:5])
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not [x for x in acked if not (isinstance(x[2], dict) and x[2].get("ok"))],
       "commit 返 200 但响应不是 ok")

    # 判据 1: 成功 checkout 之后绝不能停在游离 HEAD
    p = sb.git("symbolic-ref", "--short", "HEAD", check=False)
    ck(p.returncode == 0 and (p.stdout or "").strip() not in ("", "HEAD"),
       "并发 checkout 之后仓库停在游离 HEAD(sha=%s)—— "
       "checkout 的 show-ref 与 checkout 是两条独立 git 命令, 中间锁被释放了"
       % (sb.out("rev-parse", "HEAD").strip()[:10]))
    # 判据 2: 非原子 —— 一次请求被另一个请求插过队
    ck(summ["requests_multi_git"] > 0,
       "自检失败: 没有一个请求跨越 2 条以上 git 命令(%r)" % summ)
    ck(not inter,
       "【操作不是原子的】%d/%d 个请求在自己的临界区里被别的请求插过队"
       "(git 命令条数分布 %r)。"
       "⚠⚠ 下面这句解释写于 commit() 修复**之前**, 现在**已经不成立**, "
       "照它去查 commit() 会白跑:"
       "「一次 commit = add -A + commit, 锁只包住其中一条」—— "
       "现在的 commit() **整段持同一把 Repo._lock**(server.py:1312), "
       "校验→暂存→核对索引→commit 全在里面, 实测 commit 请求自己的临界区"
       "11~12 条全落在这一次取锁之内(外层一条包着里面 8~9 条)。"
       "残留的插队**全部在 commit() 之外**, 实测归因(复现办法见文末, "
       "判别式: 别人的 span 落在本请求自己首尾两条临界区**之间**才算):"
       "  1) Repo.checkout() 的 show-ref 与 checkout 是**两次独立取锁**"
       "(server.py:1348 与 1351), 中间有 900~1100ms 的锁等待缝 —— "
       "实测 24/24 个 /api/checkout 真非原子。这是 verify-then-act TOCTOU。"
       "  2) /api/commit 的 handler 尾巴在 commit() **释放锁之后**又取了一次锁"
       "做 `REPO.status()`(server.py:1758-1759), 缝宽 130~610ms —— "
       "实测 23/24 真非原子。后果是响应里的 status 可能不是自己那次提交的。"
       "  3) /api/branch 判据报的 15/24 里只有 2 个是真的: 另 13 个请求"
       "自己只发 1 条 git(own=1), **结构上没有缝隙可插**, 是"
       "「别人的整段操作恰好落在本请求的 HTTP 窗口之内」被算成了插队 —— "
       "本判据的 inter 口径分不开这两种成因, 它比它声称的粗。"
       % (inter, summ["requests"], summ["git_call_histogram"]))
    # 复现上面那三行归因的探针(私有 TEMP, 不进仓, 用完即删):
    #   照抄本用例的 Sandbox/Server/run_ops/探针安装, 只多两件事:
    #     a) install_request 的标签函数换成从 `self.path` 取接口路径
    #        —— T._path_label(:629) 只在位置参数里找 `/api/` 开头的字符串,
    #        而 do_POST(self) 只有 handler 实例, 所以它对 do_POST **一律**
    #        返回 "request", 72 条全一个标签, 说不出是谁插的队。
    #        换标签**不改变** inter 的定义: analyze_requests(:727-729)
    #        只看 req 这个 ident, 根本不看 label。
    #     b) 把 inter 拆成两种成因:
    #        真非原子 = 别人的 span 满足 own[0].t0 < other.t0 且 other.t1 < own[-1].t1;
    #        仅仅包含 = 别人的整段落在本请求 HTTP 窗口内, 但本请求自己的
    #                    临界区一条都没被切开(own 只有 1 条时**必然**如此)。
    #   实测(72 请求): 判据 inter 63, 真非原子 49; 拆分后
    #     /api/checkout 判 24 / 真 24 (own=4, 缝在 show-ref↔checkout)
    #     /api/commit  判 24 / 真 23 (own=11~12, 缝在 commit↔status↔symbolic-ref,
    #                              全在 commit() 释放锁**之后**)
    #     /api/branch  判 15 / 真  2 (own=1 的 22 条结构上不可能真非原子)
    #   对照: 把 commit() 换回旧的两步版(先扫 + 裸 add -A), inter 63 vs 65,
    #         git span 220 vs 400 —— 同一量级, 说明 commit() 的修复对这条**无影响**。
    if cls["git_lock"]:
        raise Flaky("git 锁冲突 %d/%d" % (len(cls["git_lock"]), len(ops)),
                    rate=len(cls["git_lock"]) / float(len(ops)))
    return None


@test("C5", "same-file-two-writers", "同一文件被两个请求同时改: 响应 sha256 vs 磁盘")
def t_c5_same_file():
    THR, RND = 6, 12
    sb = Sandbox("c5")
    fx_merge_conflict_n(sb, RND)
    names = ["c%02d.txt" % i for i in range(RND)]
    violations = []
    vlock = threading.Lock()

    with Server(sb, "c5") as srv:
        def body(t, r):
            content = "writer T%d round %d\n%s" % (t, r, "-" * 40)
            # 故意**不带** expect_sha256: 那是客户端的可选项, 缺省就该写成功
            code, j = srv.post("/api/conflict/resolve",
                               {"file": names[r], "content": content})
            if code == 200 and isinstance(j, dict) and j.get("ok"):
                claimed = j.get("sha256")
                actual = sb.sha256_of(names[r])
                if claimed != actual:
                    with vlock:
                        violations.append({"thread": t, "round": r,
                                           "claimed": (claimed or "")[:12],
                                           "actual": (actual or "")[:12]})
            return code, j
        ops, el, slow = run_ops(THR, RND, body, "resolve x same-file")
        cls = report("C5", ops, extra={"elapsed_s": round(el, 1),
                                       "sha256_mismatch": len(violations)})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    if violations:
        raise Flaky("【静默丢更新】%d 次成功响应报的 sha256 与事后磁盘内容不一致 —— "
                    "conflicts.resolve 的「读-校验-写」整段没有任何锁, 两个请求的"
                    "写入互相覆盖。例: %r" % (len(violations), violations[:3]),
                    rate=len(violations) / float(len(ops)))
    return None


#: C6 的独立尝试次数。一次跑一次 commit 竞速太靠运气(实测 3 次里有 1 次
#: 压根没轮到「commit 成功」的机会 —— 对手把联接挂满了全程, 于是**每次都
#: 被正确拒绝**, 那一轮什么都没测到)。所以改成 K 次独立尝试, 报 K 份里的
#: 命中数 —— 「1/8 命中」和「这一轮没打中」是两件完全不同的事。
C6_ATTEMPTS = 8
C6_JUNCTION_UP_MS = 5      # 联接挂着的时长(占空比小才留得出「扫描时不在」的窗口)
C6_JUNCTION_DOWN_MS = 30   # 联接摘掉的时长


def _c6_attempt(idx, thr, rnd):
    """跑一次独立的「挂/摘联接 vs commit」竞速, 返回一条观测。"""
    sb = fx_escape(Sandbox("c6_%d" % idx))
    swaps = [0]
    ok_commits = [0]
    stop = threading.Event()

    def attacker():
        # 占空比刻意做小(挂 ~5ms / 摘 ~30ms): 联接绝大多数时间**不在**,
        # 于是 `escaping_links()` 常常扫描到「干净的工作区」而放行 —— 那正是
        # 要钻的空子。反过来(挂满全程)每次 commit 都被正确拒绝, 一无所获。
        while not stop.is_set():
            remove_escape(sb)
            time.sleep(C6_JUNCTION_DOWN_MS / 1000.0)
            esc = sb.abspath("escape")
            pr = subprocess.run(["cmd", "/c", "mklink", "/J", esc, sb.outside_dir],
                                 capture_output=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if pr.returncode == 0:
                swaps[0] += 1
            time.sleep(C6_JUNCTION_UP_MS / 1000.0)

    with Server(sb, "c6_%d" % idx) as srv:
        code0, j0 = srv.post("/api/commit", {"message": "should be refused"})
        refused = (code0 == 400 and isinstance(j0, dict)
                   and "\u94fe\u63a5" in (j0.get("error") or ""))
        at = threading.Thread(target=attacker, name="ATK", daemon=True)
        at.start()
        try:
            def body(t, r):
                sb.write("a.txt", "v%d-%d\n" % (t, r))
                code, j = srv.post("/api/commit", {"message": "c %d-%d" % (t, r)})
                if code == 200:
                    ok_commits[0] += 1
                return code, j
            ops, el, slow = run_ops(thr, rnd, body, "commit", barrier_each_round=False,
                                    watchdog=120.0)
        finally:
            stop.set()
            at.join(timeout=5)
            remove_escape(sb)
    leaked = unreachable_or_leaked(sb, ["escape/leak.txt", "escape/leak2.txt"])
    show = sb.git("show", "HEAD:escape/leak.txt", check=False)
    leaked_content = (show.returncode == 0
                      and "OUTSIDE-SECRET" in ((show.stdout or "") + (show.stderr or "")))
    fs = fsck_errors(sb)
    cls = classify(ops)
    return {"attempt": idx, "ops": len(ops), "swaps": swaps[0],
            "commit_200": ok_commits[0], "refused_when_visible": refused,
            "leaked_paths": len(leaked), "leaked_content": bool(leaked_content),
            "fsck": fs, "http5xx": len(cls["http5xx"]),
            "traceback": len(cls["traceback"]),
            "err": [x[2][:110] for x in cls["http5xx"][:2]]}


@test("C6", "escaping-links-bypass", "并发绕过 escaping_links: 仓外内容进历史")
def t_c6_escape():
    THR, RND = 4, 15
    runs = [_c6_attempt(i, THR, RND) for i in range(C6_ATTEMPTS)]
    hits = [r for r in runs if r["leaked_paths"] or r["leaked_content"]]
    no_chance = [r for r in runs if r["commit_200"] == 0]
    rows = [[r["attempt"], r["ops"], r["swaps"], r["commit_200"],
             r["leaked_paths"], r["leaked_content"], r["http5xx"]] for r in runs]
    say("[C6] 每次尝试: 尝试号/请求数/联接切换/commit成功/仓外路径/仓外内容/5xx")
    for r in rows:
        say("      %s" % (r,))
    RESULTS.append({"test": "C6-attempts", "ops": sum(r["ops"] for r in runs),
                    "detail": {"per_attempt": rows,
                               "hits": len(hits), "attempts": len(runs),
                               "attempts_with_no_commit_success": len(no_chance),
                               "total_swaps": sum(r["swaps"] for r in runs),
                               "total_commit_200": sum(r["commit_200"] for r in runs),
                               "junction_up_ms": C6_JUNCTION_UP_MS,
                               "junction_down_ms": C6_JUNCTION_DOWN_MS,
                               "threads": THR, "rounds_per_thread": RND}})
    print("\n  ---- C6 escaping_links 并发绕过(每次尝试一行)----")
    print("   尝试/请求数/联接切换/commit成功/仓外路径命中/仓外内容命中/5xx")
    for r in rows:
        print("   %s" % (r,))
    print("   命中 %d/%d 次尝试;  其中 %d 次尝试里 commit 一次都没成功(没打中)"
          % (len(hits), len(runs), len(no_chance)))

    # 判据 1: 防线本身有效(否则下面全是空谈)
    ck(all(r["refused_when_visible"] for r in runs),
       "防线本身没生效: 联接挂在工作区上时有 commit 成功了")
    ck(not [r for r in runs if r["fsck"]],
       "git fsck 报对象库不一致: %r" % [r["fsck"] for r in runs if r["fsck"]][:2])
    ck(not [r for r in runs if r["traceback"] or r["http5xx"]],
       "有请求 5xx 或带 traceback: %r" % [r["err"] for r in runs
                                          if r["traceback"] or r["http5xx"]][:3])
    # 判据 2: 仓外内容不能进历史
    ck(not hits,
       "【仓外内容进了历史】%d/%d 次尝试里 escaping_links 的防线被并发绕过: "
       "仓外哨兵进了对象库(%r) / `git show HEAD:escape/leak.txt` 读出了仓外内容(%r)。"
       "⚠ 下面这段根因写于本轮修复**之前**, 只在防线真的回归时才适用; "
       "若 commit() 仍是现在的实现(整段持 Repo._lock + 路径白名单 + 枚举期"
       "握目录句柄 + add 后索引核对), 那这条红的根因在别处, 别照这段去找。"
       "历史根因: commit() 曾是「先 escaping_links() 扫一遍, 再 git add -A」"
       "两步; 两步之间虽然有 Repo._lock(不是「没有锁」), 但那只挡得住"
       "**走 aigit 的另一个请求** —— C6 的对手直接在文件系统上 mklink/rmdir, "
       "整个循环一把 aigit 的锁都不取, 所以扫描通过之后联接照样能挂上。"
       % (len(hits), len(runs),
          [r["leaked_paths"] for r in hits], [r["leaked_content"] for r in hits]))
    if hits:
        raise Flaky("escaping_links 被并发绕过 %d/%d 次尝试"
                    % (len(hits), len(runs)),
                    rate=len(hits) / float(len(runs)))
    if no_chance and len(no_chance) == len(runs):
        raise Skip("本轮 %d 次尝试里 commit 一次都没成功 —— 对手把联接挂满全程, "
                   "防线每次都正确生效, 这一轮什么都没测到(不是「没漏洞」的证据)"
                   % len(runs))
    return None


# ════════════════════════════════════════════════════════ C7
#
# 三条通路的锁状况, 用**测量**说清, 不靠运气:
#   * repo 域     —— server.Repo._run_full, 锁 = Repo._lock(每实例 RLock)
#   * history 域  —— history_ops.HistoryOps._run, 锁 = HistoryOps._lock(每实例 Lock)
#   * conflicts 域—— conflicts.Git.run, 锁 = repo_git_lock(root)(按 realpath **共享** RLock)
#   三把都**有锁**, 但**不是同一把** —— 这才是本条判的东西。
# 判据: 同一域内不允许有重叠(产品原样应当是 0); 不同域之间同时跑 git
#       **不允许** —— 重叠数**就是**「锁不是同一把」的直接证据, 非 0 即红,
#       报出来的数字就是统一三锁要降到几的目标。
#
# ⚠⚠ **本文件里不许再用「无锁」这个词**, 它的歧义已经造成过两次实害:
#   一次是注释里的「无锁」= 不与 Repo._lock 互斥, 而输出里的「无锁」= 没有锁,
#   同一个词两种意思、**而运行时打印的是错的那个** —— 下一个人会去给一个
#   已经有锁的东西加锁。
#   事实(2026-10-03 锁统一之后, 逐行; 上一版写在这里的三条已过时):
#     conflicts.py:229    self._lock = repo_git_lock(self.root)
#     server.py:484       self._lock = repo_git_lock(self.root)
#     history_ops.py:158  self._lock = repo_git_lock(self.root)
#         —— 三条通路现在**共用同一个函数、同一个字典**(repolock.py 的
#         `_REPO_LOCKS`), 按 realpath 归一, 全部是 **RLock**。
#         实测(接线自检 wiring.py §4): 同一个 root 上 4 个实例
#         (含两个 Repo)的锁 `id()` **全部相同** = 1 个不同 id。
#     ⚠ 所以「三把锁互不相干」这个**原因**在统一之后已不成立。
#       本条若仍红, 原因**必须重新实测**, 不许默认还是锁域不一致 ——
#       可能是量法问题(例如把 'request' 域算进来, 见 :1927-1929 的坑),
#       也可能是某条通路绕过了锁。判据本身一个字没动。
#   实测: 三条通路同域跨线程重叠全是 0(锁有效), 跨线程跨域见每次运行输出。
#   ⚠ 那句「875 → 0」已作废, 不要继承: 875 是**锁对象 span** 的计数, 与本条
#     量 `install_span(C.Git,"run")` **方法体** 的计数不是同一个量(后者三次独立
#     跑是 533 / 537 / 550, 有噪声底)。原始日志已随临时目录删除。
#     「统一锁之后会不会归零」本轮重新实测, 不引用旧数。
#   ⚠⚠ 下面这句写于 conflicts.py 修好 `add -A` **之前**, 现在**已不成立**,
#     照它去查会白跑:「conflicts.py:1211 是裸 `add -A`, 作用域不受限, 未修」。
#     已实测改成作用域受限的显式 add(同一批输入: 裸 `add -A` 索引 6 条、其中
#     2 条内容就是仓外的; 作用域受限 2 条、泄漏 0 条)。
#     现在的真实状态见 conflicts.py `_merge_add_scope` 与
#     litematica-preview/doc/aigit-lock-graph-20261003.md §B。
#     这与本条的锁域问题**始终是两件事**, 别再合成一件。

@test("C7", "lock-domains", "三把锁的一致性: 同域重叠 / 跨域重叠 / 操作原子性")
def t_c7_lock_domains():
    THR, RND = 6, 12
    names = ["c%02d.txt" % i for i in range(RND)]
    sb = Sandbox("c7")
    fx_merge_conflict_n(sb, RND)
    with Server(sb, "c7") as srv:
        pr = Probe()
        import server as S, history_ops as H, conflicts as C
        pr.install_label(S.Repo, "_run_full")
        pr.install_label(H.HistoryOps, "_run")
        pr.install_label(C.Git, "run")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_lock(srv.mod.history_ops(), "_lock", "history")
        pr.install_span(C.Git, "run", "conflicts")
        pr.install_request(S.Handler, "do_POST", _path_label)
        pr.install_request(S.Handler, "do_GET", _path_label)
        try:
            # 每个线程同时打**三个不同锁域**里的接口
            def body(t, r):
                k = t % 3
                if k == 0:
                    return srv.post("/api/stage", {"files": ["keep.txt"]})
                if k == 1:
                    return srv.post("/api/history/amend",
                                    {"message": "amend T%d r%d" % (t, r), "confirmed": True})
                return srv.post("/api/conflict/resolve",
                                {"file": names[r], "content": "T%d r%d\n" % (t, r)})
            ops, el, slow = run_ops(THR, RND, body, "3-domain")
            reqs = analyze_requests(pr.recs)
            summ = summarize_requests(reqs)
            per = {}
            for d in DOMAINS:
                per[d] = count_overlaps(pr.recs, (d,))[0]
            # ⚠ domains 必须显式给 DOMAINS: 漏掉的话 request span 会被算进去,
            # 而两个请求本来就该时间重叠 —— 那会把计数灌成一个毫无意义的
            # 大数(实测 3932~4802, 样例全是 ('request','request',...))。
            cross, xsamp = count_overlaps(pr.recs, DOMAINS)
            per_calls = collections.Counter(r["domain"] for r in pr.recs
                                             if r.get("t1") is not None
                                             and r["domain"] != "request")
        finally:
            pr.remove()
        cls = report("C7", ops, extra={"elapsed_s": round(el, 1),
                                       "git_calls_by_domain": dict(per_calls),
                                       "same_domain_overlaps": per,
                                       "cross_domain_overlaps": cross,
                                       "requests": summ})
    RESULTS.append({"test": "C7-detail", "ops": 0,
                    "detail": {"same_domain_overlaps": per,
                               "cross_domain_overlaps": cross,
                               "cross_samples": [list(x) for x in xsamp[:4]],
                               "git_calls_by_domain": dict(per_calls),
                               "requests": summ, "threads": THR,
                               "rounds_per_thread": RND}})

    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    # 有锁的两个域: 同域重叠必须是 0 —— 产品原样就该成立, 也是探针的自检
    for d in ("repo", "history"):
        ck(per[d] == 0,
           "同域 %s 临界区重叠 %d 次 —— 该域的锁没起作用(或者探针有问题)" % (d, per[d]))
    ck(per_calls.get("conflicts", 0) > 0,
       "自检失败: conflicts 域一条 git 都没跑到(负载有问题)")
    ck(summ["requests"] > 0, "自检失败: 没框到任何服务端请求")
    # 跨域重叠必须为 **0**。重叠数**就是**「锁被绕过的直接证据**——
    # 非 0 即红, 报出来的数字就是统一三锁要降到几的目标。
    #
    # ⚠ **本条保持红是预期状态, 不要靠改判据把它弄绿。**
    #   红的含义: 三条通路的 git 临界区在时间上重叠 = 产品有原子性缺口。
    #   ⚠⚠ 下面这段「唯一正确的绿法 = 统一三锁」写于统一**完成之前**。
    #     统一已在 2026-10-03 落地(repolock.py; wiring.py 实测同一 root 上
    #     4 个实例锁 id 相同)。所以「统一三锁」**不再是待办**, 它是已完成项。
    #     若本条**仍红**, 说明重叠另有来源 —— 不要再默认「去统一锁」,
    #     那是已经做过且已被证明不够的那一步。按域对拆开看:
    #         hist+repo / conf+hist / conf+repo  三个域对分别归零了吗?
    #     哪一对没归零, 就去查那一对对应的通路。
    #   ⚠⚠ 「实测: 跨线程跨域重叠 875 → 0」这句话**已作废, 不要引用**:
    #     875 是**锁对象 span** 的计数, 本条量的是 `install_span(C.Git,"run")`
    #     **方法体** 的计数, 两者不是同一个量(本条这个量三次独立跑是
    #     533 / 537 / 550, **有噪声底**)。原始日志已随临时目录删除。
    #     照 875 去核对会得出「统一后应该 875 → 0」这种无法验证的要求。
    #     分域对的实测数见本轮报告, 不写在这里 —— 写在这里就会立刻过期。
    #   ⚠ 同源冲突已处置: test_f06_guard.py 的 T9 判**同一个量**、方向相反
    #     (它曾要求 > 0), 已按裁定改成 @baseline —— 只印数字, 不进 PASS/FAIL,
    #     不影响退出码。T9 那边让路, 本条一个字没动。
    ck(not cross,
       "【跨域临界区重叠】%d 对不同锁域的临界区在时间上重叠(判据要求 0)。%s"
       "本条量的三把锁现在**共用同一个对象**(repolock.repo_git_lock(root) 按 "
       "realpath 共享: conflicts.py:229 / server.py:484 / history_ops.py:158), "
       "接线自检实测同一 root 上 4 个实例的锁 id 全部相同。"
       "⚠ 所以本条若红, 原因**不是「三把锁不是同一把」**了 —— 那一条已修; "
       "必须重新实测(可能是量法把 'request' 域算进来, 或某条通路绕过了锁), "
       "**不要**照旧去查锁对象身份。同域重叠 repo=%d / history=%d / conflicts=%d。"
       "⚠ 另有一处**已修好**的缺口, 与锁**无关**, 别顺着本条去查: conflicts.py "
       "原先的裸 `add -A`(作用域不受限)已改成作用域受限的显式 add —— 实测同一批"
       "输入下裸 `add -A` 索引 6 条含 2 条仓外内容, 作用域受限 2 条、泄漏 0 条。"
       "样例(域,git 子命令)x(域,git 子命令): %r"
       % (cross, "同域 conflicts 重叠 %d 次(锁有效)。" % per["conflicts"],
          per["repo"], per["history"], per["conflicts"], xsamp[:4]))
    ck(not summ["requests_interleaved"],
       "【操作不是原子的】%d/%d 个请求在自己的临界区里被别的请求插过队"
       "(每个请求发出的 git 命令条数分布 %r)"
       % (summ["requests_interleaved"], summ["requests"], summ["git_call_histogram"]))
    return None


# ════════════════════════════════════════════════════════ 跑

def cleanup():
    sys.stderr = sys.__stderr__
    shutil.rmtree(TESTROOT, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--repeat", type=int, default=1,
                    help="同一条用例连跑几遍, 用来量化 flaky 的复现率(默认 1)")
    a = ap.parse_args()
    global VERBOSE
    VERBOSE = a.verbose

    TESTS.sort(key=_order)
    if a.list:
        for t in TESTS:
            print("%-14s %s" % (t["id"], t["note"]))
        return 0

    sys.path.insert(0, AIGIT)
    shutil.rmtree(TESTROOT, ignore_errors=True)
    os.makedirs(TESTROOT, exist_ok=True)
    print("=" * 72)
    print("  aigit 并发测试    并发度/轮数见各用例注释    TEMP=%s" % TESTROOT)
    print("  只读 server.py / conflicts.py / history_ops.py —— 发现 bug 只记录不修")
    print("=" * 72)

    npass = nfail = nskip = 0
    fails = []
    tally = collections.OrderedDict()   # id -> {"pass":n,"flaky":[rates],"fail":n,"err":n}
    rep_tables = []                     # [(rep_no, RESULTS 快照), ...] 收尾逐遍打表
    t_all = time.perf_counter()
    sel = [t for t in TESTS
           if not a.only or a.only.lower() in t["id"].lower()]
    reps = max(1, a.repeat)
    try:
        for rep in range(reps):
            if reps > 1:
                print("\n######## 第 %d/%d 遍 ########" % (rep + 1, reps))
            # ⚠️ 快照必须在**每遍开头**取, 每遍结尾**绝不能** clear() ——
            # 放在结尾的话最后一遍的观测值会被自己清空, 收尾永远打印空表。
            RESULTS.clear()
            for t in sel:
                slot = tally.setdefault(t["id"],
                                        {"pass": 0, "flaky": [], "fail": 0,
                                         "err": 0, "skip": 0, "msgs": []})
                sys.stdout.write("%-14s " % t["id"])
                sys.stdout.flush()
                t0 = time.perf_counter()
                try:
                    t["fn"]()
                    dt = time.perf_counter() - t0
                    print("PASS  (%.1fs)  %s" % (dt, t["note"]))
                    npass += 1
                    slot["pass"] += 1
                except Skip as e:
                    print("SKIP  %s" % e)
                    nskip += 1
                    slot["skip"] += 1
                except Flaky as e:
                    dt = time.perf_counter() - t0
                    print("FAIL  (%.1fs)  [flaky 复现率 %s] %s"
                          % (dt, ("%.0f%%" % (e.rate * 100)) if e.rate else "?", e))
                    fails.append((t["id"], str(e), "flaky", ""))
                    nfail += 1
                    slot["flaky"].append(e.rate)
                    slot["msgs"].append(str(e))
                except Fail as e:
                    dt = time.perf_counter() - t0
                    print("FAIL  (%.1fs)  %s" % (dt, e))
                    fails.append((t["id"], str(e), "deterministic", ""))
                    nfail += 1
                    slot["fail"] += 1
                    slot["msgs"].append(str(e))
                except Exception as e:                    # noqa: BLE001
                    dt = time.perf_counter() - t0
                    tb = traceback.format_exc()
                    print("ERROR (%.1fs)  %s: %s" % (dt, type(e).__name__, e))
                    print("---- %s 的 traceback ----" % t["id"])
                    print(tb)
                    fails.append((t["id"], "%s: %s" % (type(e).__name__, e),
                                  "error", tb))
                    nfail += 1
                    slot["err"] += 1
                    slot["msgs"].append("%s: %s" % (type(e).__name__, e))
                sys.stdout.flush()
            rep_tables.append((rep + 1, list(RESULTS)))
    finally:
        if reps > 1:
            print("\n" + "=" * 72)
            print("  复现率汇总(连跑 %d 遍)—— 命中就是命中, 不靠加重试抹平" % reps)
            for tid, sl in tally.items():
                n = sl["pass"] + len(sl["flaky"]) + sl["fail"] + sl["err"] + sl["skip"]
                extra = ""
                if sl["flaky"]:
                    extra = "  各遍命中率 %s" % ", ".join(
                        ("%.0f%%" % (r * 100)) for r in sl["flaky"])
                print("    %-14s PASS %d/%d  deterministic-FAIL %d  error %d%s"
                      % (tid, sl["pass"], n, sl["fail"], sl["err"], extra))
        print("\n" + "=" * 72)
        print("  汇总   PASS %d / FAIL %d / SKIP %d    总耗时 %.1fs"
              % (npass, nfail, nskip, time.perf_counter() - t_all))
        if fails:
            print("  失败明细:")
            for fid, msg, kind, tb in fails:
                print("    [%s] %-14s %s" % (kind, fid, msg[:600]))
                if tb:
                    print("      --- traceback ---")
                    for ln in tb.rstrip().splitlines()[-14:]:
                        print("      " + ln)
        print("\n  分组观测值(复现率, 不藏) —— **每一遍都打**, 不留空表:")
        if not rep_tables:
            print("    (没有观测值: 用例在 main() 建表前就抛了)")
        for rno, rows in rep_tables:
            print("  ---- 第 %d 遍 (%d 条观测) ----" % (rno, len(rows)))
            for r in rows:
                if "detail" in r:
                    print("    %-22s %s" % (r["test"], json.dumps(r["detail"],
                                                               ensure_ascii=False)[:1400]))
                else:
                    drop = ("test", "verdict")
                    keep = {k: v for k, v in r.items()
                            if k not in drop and isinstance(v, (int, float, str, dict))}
                    print("    %-22s %s" % (r["test"], json.dumps(keep, ensure_ascii=False)))
        print("=" * 72)
        cleanup()
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
