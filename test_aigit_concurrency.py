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


def same_actor(a, b):
    """这两条 span 是不是**同一个执行者**(同一个线程)?

    ## 2026-10-04: 这里原来写的是 `a["ident"] == b["ident"]`, 而那是死代码

    `Probe._open` 的 ident 是全局递增号(`_CNT[0] += 1`), 每条 span 独一份,
    所以两条不同 span 的 ident 永不相等 ⇒ 那条豁免**一次都没执行过**。
    实测证据(diag_c0_deadguard.py): /api/stage 负载 THR=6 RND=8 下
    repo 域时间相交 96 对, 其中「ident 相等」的 **0 对**、「thread 相等」96 对、
    **跨线程 0 对** —— 也就是那 96 对全是**同一个线程自己的嵌套取锁**,
    而互斥一点没坏。

    ## 为什么豁免本来就该有

    这条判据声称量的是「同一把锁内**并发**的重叠」。一个线程持可重入锁取两次
    (`status()` 里的 `with self._lock:` → `_run_full()` 里的 `with self._lock:`),
    两条 span 时间上互相包含, 但**同一个线程不可能与自己并发** —— 那是嵌套。
    把嵌套算成互斥违反, 判据量的东西就和它声称的不一样了。

    ## 身份为什么是 (tid, 名字) 而不是 req 或光名字

    * `req` 不能单独用: 探针也记不在任何请求里的 span(服务端启动等), 那时 req
      是 None, 两条都 None 会被错当同一实体;
    * 名字不够: `ThreadingHTTPServer` 的处理线程名会重复;
    * 判据的语义是「有没有两个**执行者**同时在临界区」, 执行者就是线程。

    判据是不是被放宽过头了, 由 C0 第 0 步的阳性对照回答(见 `_selfcheck_same_actor`):
    跨线程重叠必须**仍然**被算成重叠, 线程名相同但 tid 不同也算。
    """
    return (a.get("tid"), a["thread"]) == (b.get("tid"), b["thread"])


def _selfcheck_same_actor():
    """`count_overlaps` 的阳性对照/阴性对照, 纯内存, 每次 C0 必跑。

    没有它, 那条豁免可以宽到「一律不算」而 C0 第 1 步照样绿 —— 那就是空壳闸门。
    四个方向:
      (a) 同线程嵌套        -> 必须**不**算重叠
      (b) 跨线程重叠        -> 必须算重叠          <- 区分力
      (c) 名字相同 tid 不同 -> 必须算重叠          <- 证明没拿名字当身份
      (d) 漏关检查仍在      -> t1=None 必须仍然被拒
    """
    base = time.perf_counter()

    def span(tid, thread, t0, t1, ident, domain="repo"):
        return {"domain": domain, "ident": ident, "thread": thread, "tid": tid,
                "label": "git-" + ident, "req": None,
                "t0": base + t0, "t1": base + t1}

    p, _ = count_overlaps([span(1, "T0", 0.0, 1.0, "1"),
                           span(1, "T0", 0.2, 0.5, "2")], ("repo",))
    ck_eq(p, 0, "(a) 同一线程的嵌套必须不算重叠 —— 这正是本次修的那条豁免")

    p, samp = count_overlaps([span(1, "T0", 0.0, 1.0, "1"),
                              span(2, "T1", 0.2, 0.5, "2")], ("repo",))
    ck_eq(p, 1, "(b) 不同线程的临界区重叠必须仍被算成 1 对,实际 %d —— "
                "豁免放宽过头了, 这条判据已经量不出并发" % p)
    ck_eq(len(samp), 1, "(b) 应当给出 1 个样例")

    p, _ = count_overlaps([span(1, "T0", 0.0, 1.0, "1"),
                           span(2, "T0", 0.2, 0.5, "2")], ("repo",))
    ck_eq(p, 1, "(c) 线程名相同但 tid 不同必须仍算重叠 —— 不许拿名字当身份")

    dangling = [span(1, "T0", 0.0, 1.0, "1")]
    dangling[0]["t1"] = None
    raised = None
    try:
        count_overlaps([dangling[0], span(2, "T1", 0.2, 0.5, "2")], ("repo",))
    except Fail as e:
        raised = e
    # ⚠ 判据消息里那句原文是「探针漏关了 %d 条临界区(从未退出)」——
    #   **不含 "t1=None" 这个子串**。第一版我拿 "t1=None" 去认, 结果认不出,
    #   走进 `raise` 又抛出去, C0 于是 0.0s 就 FAIL(实测 t33)。
    #   教训: 认一条异常靠**它消息里真实存在的字样**, 不靠我记忆里的措辞。
    ck(raised is not None,
       "(d) 有 t1=None 的 span 时 count_overlaps 必须报错 —— "
       "漏关的 span 会被过滤掉, 于是报出「重叠 0」, "
       "那是「没量到」不是「量到了没有」")
    ck("漏关" in str(raised) and "从未退出" in str(raised),
       "(d) 报错消息要能让人看出是「漏关」, 实际拿到 %r" % str(raised))
    return {"nested": 0, "cross_thread": 1, "same_name_diff_tid": 1}


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
            if same_actor(other, r):
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
                              # ⚠ 2026-10-04 新增: **线程身份**。count_overlaps 的
                              # 同一性豁免要靠它区分「同一执行者的嵌套」与
                              # 「两个执行者真的同时在临界区」。原来只有 thread
                              # (名字), 而 ThreadingHTTPServer 的处理线程名会重复,
                              # 光靠名字不唯一。名字保留, 报告里还要给人看。
                              "tid": threading.get_ident(),
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
    """从一个请求的 (self, ...) 里挖出接口路径, 让人看得懂报表。

⚠⚠ 2026-10-05 **就地改正**。**原来这个函数在 `a` 里找 `/api/` 开头的字符串**
(`for x in a: if isinstance(x, str) and x.startswith("/api/")`), 而
`install_request(S.Handler, "do_POST", ...)` 包的是 `do_POST(self)` —— 位置
参数里**只有 handler 实例**, 所以它对 do_POST **一律**返回 `"request"`,
72 条请求全一个标签。后果实测过: C7 读到 48/72 时, 报表里**说不出是哪三个
接口**(C7 的负载是 /api/stage、/api/history/amend、/api/conflict/resolve),
因为它们的 label 一模一样。

正确的取法是 `self.path` —— `BaseHTTPRequestHandler` 在解析请求行时就填好了。

⚠ **这只改报表可读性, 不改任何判定**(下面两条是实测确认的, 不是推测):
  * `analyze_requests` 只按 `req` 这个 ident 归因, **根本不读 label**;
  * 全文**没有任何地方**拿 label 与 `"request"` 比较 —— 只有 `domain` 被断言。
所以 `count_overlaps` / `analyze_requests` / `summarize_requests` 的取值一个都没变,
变的是**报表和交叉样本里那几列字**。配套的阳性自检见 `analyze_requests` 末尾。
"""
    h = a[0] if a else None
    p = getattr(h, "path", None)
    if isinstance(p, str) and p.startswith("/api/"):
        return p
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
    """以**服务端请求**为单位归因: 它自己发了几条 git、期间有没有被别人插队。

    🔴 `interleaved` 的口径 **2026-10-04 就地改正**。旧口径测错了东西:

        other = 别人的 git span 整段落在我这个请求的 **HTTP 窗口** [q.t0, q.t1] 内

    它把「别人的整段操作恰好落在我等锁的这段时间里」也算成插队 —— 那是**锁竞争的
    正常结果, 不是原子性缺口**。实测后果(2026-10-04): 产品改到「每个请求真非原子 = 0」
    之后该量**仍 53~56/72**; 而把 HTTP 请求整段串行化能消掉它, 却会打红 C0
    (注入空锁后同域重叠必须 > 0)。**判据之间自相矛盾, 按旧口径在并发下不可能为真。**

    正确口径是**本文件 `:1661` 早就写明的判别式**:

        「别人的 span 落在本请求自己首尾两条临界区**之间**才算」

    落地: 别人的 git span 与窗口 [我第一条临界区起点, 我最后一条临界区终点] **相交**即算,
    且本请求必须**自己发过 >= 2 条 git** —— own==1 时窗口退化成零宽、结构上没有缝可插。
    (旧注释 `:1668-1670` 记的「own=1 的那 13/15 是假插队」就是这条。)

    ⚠ 口径变了, 分母也必须跟着变, 所以 `summarize_requests` 同时给出
    `requests_interleavable` = **有资格被插队的请求数(own>=2)**。
    **断言前必须先确认它非空**, 否则这个量恒 0 是「没测到」而不是「没插队」。
    """
    reqs = [r for r in recs if r["domain"] == "request" and r.get("t1") is not None]
    gits = [r for r in recs if r["domain"] != "request" and r.get("t1") is not None]
    out = []
    for q in reqs:
        rid = q["ident"]
        own = [g for g in gits if g.get("req") == rid]
        other = []
        if len(own) >= 2:
            lo = min(g["t0"] for g in own)
            hi = max(g["t1"] for g in own)
            other = [g for g in gits
                     if g.get("req") != rid and g["t0"] < hi and g["t1"] > lo]
        out.append({"id": rid, "label": q["label"], "thread": q["thread"],
                    "ms": round((q["t1"] - q["t0"]) * 1000, 1),
                    "git_calls": len(own), "interleaved": len(other),
                    "interleavable": 1 if len(own) >= 2 else 0,
                    # 带上 span 身份: 只记个数的话, 「反假阳性」那条断言无从下手
                    # —— 得能指着具体哪条 span 说它同时属于我和别人。
                    "own_ids": [g["ident"] for g in own],
                    "inter_ids": [g["ident"] for g in other],
                    "domains": sorted(set(g["domain"] for g in own))})
    # ⚠ **阳性自检**(2026-10-05 加): 量具自己要证明会响。
    #   少了这一条, `_path_label` 静默退化成全 "request" 时**没有任何判据会红** ——
    #   而那种情况下报表里的接口归属是**假的**(不是「没插队」, 是「说不出是谁」)。
    #   判据: **至少有一条**请求的 label 不是 "request"。
    #   先断言两侧非空再比: 断言 out 非空, 否则下面这个量恒 0 是「没测到」。
    assert out, "自检失败: analyze_requests 收到 0 条服务端请求 —— 探针没框到东西"
    labeled = [q for q in out if q["label"] != "request"]
    ck(labeled,
       "自检失败: %d/%d 条请求的 label 全是 'request' —— 探针取不到接口路径"
       "(见 :%d _path_label, 它只能从 handler 的 self.path 取)。"
       "⚠ 这**不是**「没插队」, 是「说不出是哪条请求插的队」: 报表里的接口"
       "归属是假的。别把这个量读成插队数。"
       % (len(out) - len(labeled), len(out), 718))
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
             # 口径改正配套: 有资格被插队的请求数(own>=2)。
             # **断言前必须先确认它非空**, 否则该量恒 0 是「没测到」不是「没插队」。
             "requests_interleavable": sum(1 for r in reqs if r.get("interleavable")),
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


FX_DEEP_BASE = 6
FX_DEEP_N = 24


def fx_history_deep(sb, n=None):
    """FX_DEEP_N 个提交, 分两段, 给三种操作各备一份互不干扰的 fodder。

    n=None 时用 FX_DEEP_N(C4B 用); C4C 传更大的数, 因为它的 undo 次数更多。

    第 1 段(`c1`..`cFX_DEEP_BASE`): 每个提交只动一个**专属**文件 `r/r0i.txt`。
        这几个是 revert 的 fodder。专属文件不会被后续提交再改, 所以无论 HEAD
        停在哪一格, revert 它的逆补丁都能干净贴上。
    第 2 段: 循环动 `w/f00..07`, 只为给 undo 提供**深度**。

    为什么不能用 `fx_history`(只有 2 个提交): undo 是 `reset --hard HEAD~1`,
        一步一步往回走; 2 个提交的仓里第 3 次就 `fatal: ambiguous argument
        'HEAD~1'`, 成功响应被压到 2 条 —— 分母 2 的判据什么都不能说明。

    第一版 C4B 踩过的坑(记下来, 别再犯): 让 revert 去 revert 中段某条、而那个
        文件**也被后续提交改过**。HEAD 被 undo 拉回那几格之后逆补丁贴不上 ->
        冲突 -> `git revert` 失败并留下 REVERT_HEAD; 而 `do_revert` 失败时
        **不清理**它, 于是 `plan_revert` 的 `dirty()["conflicted"]` 闸把**后面
        全部** revert 一起拒掉: 实测 revert_ok 塌到 **3/16**。
        那是夹具的毛病, 不是产品的事实 —— 要让判据的分母站得住, 先修夹具。
    """
    n = FX_DEEP_N if n is None else n
    for i in range(N_W):
        sb.write("w/f%02d.txt" % i, "v0\n")
    sb.commit("c1: base")
    for k in range(2, FX_DEEP_BASE + 1):
        sb.write("r/r%02d.txt" % k, "r%d\n" % k)
        sb.commit("c%d: revert fodder %d" % (k, k))
    for k in range(FX_DEEP_BASE + 1, n + 1):
        sb.write("w/f%02d.txt" % (k % N_W), "v%d\n" % k)
        sb.commit("c%d: depth %d" % (k, k))
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

    # ---- 第 0 步: 判据自检(纯内存, 不过服务器) ----
    # ⚠ 这一步在 2026-10-04 加的, 因为 count_overlaps 的同一性豁免刚被修过
    #   (见 same_actor 的注释: 原来那条 ident 判据是死代码)。修完必须先证明
    #   「豁免只豁免同一执行者」, 否则第 1 步那个 ==0 可以靠「一律不算」骗过去。
    sc = _selfcheck_same_actor()
    say("[C0] 判据自检: 同线程嵌套=%d / 跨线程=%d / 同名不同 tid=%d"
        % (sc["nested"], sc["cross_thread"], sc["same_name_diff_tid"]))

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
# ════════════════════════════════════════════════════════ C0B 插队判据的阳性对照
#
# 为什么必须单开一条: 2026-10-04 `analyze_requests` 的 `interleaved` **换了口径**
# —— 从「别人的整段落在我这个请求的 HTTP 窗口内」改成「别人的 span 与我自己的
# 首尾两条临界区窗口相交, 且我发过 >=2 条 git」。**换过口径的判据必须重新证明
# 它有牙**, 否则就是造了一个不会失败的闸门。
#
# ## 三行对照, 两行判定
#
#   行                     并发度  注入空锁   判什么
#   ------------------------------------------------------------------
#   必绿                    1       否        interleaved **必须** 0, 且 eligible > 0
#   必红                    6       是        interleaved **必须** > 0
#   (只报数, 不判定)         6       否        产品真实水平, 由 C3/C7 去判
#
# ⚠ 为什么不拿第三行跟第二行比大小: 2026-10-04 实测第三行已经是 49/50 ——
#   **基线本身顶在上限**, 「注入 > 不注入」只剩 50>49 的侥幸, 跑满 50 就必翻。
#   那是**天生 flaky 的闸门**, 比没有闸门更坏。所以判定只依赖前两行, 它们与
#   「产品有多坏」**无关**, 只依赖判据能不能区分「没有并发」和「人为并发」。
#
# ⚠ 三步都先验分母 requests_interleavable > 0: 分母为空时读数恒 0,
#   那是「没测到」不是「没插队」。两件事必须分开, 所以先验分母再验读数。
#
# ⚠ 另有一条**反假阳性**断言: 被算成插队的那些 span, 一条都不许与「我自己」的
#   任何 span 相交。同一把互斥锁下这在逻辑上不可能 —— 一旦发生, 要么判据在数
#   「等锁时间」(本文件 :374-377 记过那个坑: 量方法体会把等锁算成临界区),
#   要么锁根本不互斥。两种情况下 interleaved 都不可采信。
#
# ⚠ 负载是**照抄** C3 的 body, 不抽公共 helper —— 故意不共用:
#   校准要的是一份**独立测量**; 共用 helper 等于「用被判据自己的代码证明自己」。


@test("C0B", "interleaved-criterion",
      "插队判据换口径后的阳性对照: 并发度1 必为0 / 注入空锁 必非0")
def t_c0b_interleaved_criterion():
    RND = 12

    def one(tag, thr, inject):
        sb = fx_basic(Sandbox(tag))
        with Server(sb, tag) as srv:
            pr = Probe()
            import server as S
            pr.install_label(S.Repo, "_run_full")
            if inject:
                pr.inject_noop_lock(srv.mod.REPO, "_lock", "repo")   # ← 注入点
            else:
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
                    return srv.post("/api/commit",
                                    {"message": "commit T%d r%d" % (t, r)})
                ops, el, _ = run_ops(thr, RND, body, "checkout|branch|commit")
                reqs = analyze_requests(pr.recs)
                summ = summarize_requests(reqs)
            finally:
                pr.remove()
            # 撤干净 = 属性换回真锁(不是 recs 清空 —— recs 后面还要用来打报表)
            ck(not isinstance(srv.mod.REPO._lock, (TracingLock, NoopLock)),
               "探针/injection 没撤干净: REPO._lock 还是 %s"
               % type(srv.mod.REPO._lock).__name__)
        summ["_thr"] = thr
        summ["_inject"] = 1 if inject else 0
        summ["_ops"] = len(ops)
        summ["_elapsed_s"] = round(el, 1)
        return summ, reqs

    def row(name, s):
        return [name, s["_thr"], "是" if s["_inject"] else "否",
                s["requests_interleavable"], s["requests_interleaved"],
                s["requests"], s["git_call_histogram"], s["_elapsed_s"]]

    solo, solo_recs = one("c0b_solo", 1, False)     # 必绿那一步
    inj, inj_recs = one("c0b_inj", 6, True)        # 必红那一步
    real, real_recs = one("c0b_real", 6, False)     # 只报数

    table = [["场景", "并发度", "注入空锁", "有资格(own>=2)", "被插队",
              "全部请求", "git 条数分布", "耗时s"],
             row("必绿: 只有一个请求在跑", solo),
             row("必红: 注入空锁内核", inj),
             row("实测: 产品原样(只报数)", real)]
    out = {"table": table,
           "solo_eligible": solo["requests_interleavable"],
           "solo_interleaved": solo["requests_interleaved"],
           "injected_eligible": inj["requests_interleavable"],
           "injected_interleaved": inj["requests_interleaved"],
           "real_eligible": real["requests_interleavable"],
           "real_interleaved": real["requests_interleaved"]}
    say("[C0B] 校准表:")
    for r in table:
        say("      %s" % (r,))
    RESULTS.append({"test": "C0B-interleaved-criterion", "ops": 0,
                    "verdict": "OK", "detail": out})

    # ---- 三步都先验分母: 分母为空时读数恒 0, 是「没测到」而不是「没插队」 ----
    for tag, s in (("并发度1", solo), ("注入空锁", inj), ("产品原样", real)):
        ck(s["requests_interleavable"] > 0,
           "【自检失败】%s 那一行 requests_interleavable=0 —— 没有任何请求发过 "
           "2 条以上 git, 换过口径的插队判据在这次运行里**根本没测到东西**, "
           "对照无意义。git 条数分布 %r" % (tag, s["git_call_histogram"]))
    # ---- 反假阳性: 被算成插队的 span 一条都不许与「我自己」的 span 相交 ----
    # 同一把互斥锁下这在逻辑上不可能。真发生了只有两种可能, 两种都让
    # interleaved 不可采信: (a) 判据在数「等锁时间」而不是临界区
    #     (本文件 :374-377 记过那个坑: 量方法体会把等锁算成临界区);
    #     (b) 那把锁根本不互斥。
    # 注入空锁那一步**不在此列** —— 那把锁本来就被换成不互斥的了, 有交集是应当的。
    for tag, rs in (("并发度1", solo_recs), ("产品原样", real_recs)):
        bp = [(r["id"], i) for r in rs
              for i in r.get("inter_ids", []) if i in r.get("own_ids", [])]
        ck(not bp,
           "【自检失败】%s 那一行: 有 %d 条被算成插队的 span 同时也属于它自己那个"
           "请求, 例 %r —— 同一把 Repo._lock 下这在逻辑上不可能。说明判据把"
           "「等锁时间」当成了临界区, 或者那把锁根本不互斥; 两种情况下 "
           "requests_interleaved 都不可采信。" % (tag, len(bp), bp[:4]))

    # ---- 必绿: 并发度 1 时不可能有别的请求插队 ----
    ck_eq(solo["requests_interleaved"], 0,
          "并发度=1 时 requests_interleaved 必须是 0"
          "(只有一个请求在跑, 没有「别人」可插; 实测 %d/%d)"
          % (solo["requests_interleaved"], solo["requests_interleavable"]))

    # ---- 必红: 注入空锁内核之后必须非 0 ----
    ck(inj["requests_interleaved"] > 0,
       "【自检失败】把 Repo._lock 的内核换成空锁、并发度=6 之后, "
       "requests_interleaved 仍然是 0(分母 %d)—— 换过口径的插队判据"
       "**抓不住人为造出来的竞争**, 它是空转的, C3/C7 里那条判据也就跟着空转。"
       % inj["requests_interleavable"])

    print("\n  ---- C0B 插队判据阳性对照(并发度1 必为0 / 注入空锁 必非0)----")
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

# ════════════════════════════════════════════════════════ C4 的请求级原子性判据
#
# ⚠ 这一段是 2026-10-05 新加的, 它接的是 C7 原来那条判据(见 C7 里的移交说明)。
#
# ## 判据落在哪
# C7 原来用 `ck(not summ["requests_interleaved"])` 表达「操作不是原子的」, 实测
# 恒红(72 个有资格的请求里 48 个被插过队), 但**那个数不携带任何信息**:
# 锁是**每条 git 命令取一次**(server.py:497 的 _run_full), 一次请求的两次 git
# 调用之间必然有缝, 而缝的宽度与并发度成正比。实测 836/836 个外来 span **全部**
# 落在缝里, 真重叠 0。换成「真重叠 = 0」会把缺陷**洗成绿**, 因为本条真正
# 要抓的损害**不需要真重叠**也能发生。
#
# ## 判据是什么(以及为什么只有一条)
# `HistoryOps.do_amend`(history_ops.py:607-625)末尾是:
#       rc, out = self._ok(["commit", "--amend", ...])
#       new_sha = self.head_sha()                 # ← commit **之后**才读
#       return {... "subject": self.subject_of(new_sha) ...}
# 锁在 _run 里是**每条 git 命令取一次**, 所以在 `commit` 与 `head_sha()` 之间
# 别的 amend 可以插进来把 HEAD 再改一次。于是 **本请求的响应里报出来的
# new_sha / subject 是别人的提交**。2026-10-05 实测(6 线程 3 stage + 3 amend,
# x8 轮, 夹具 fx_history, 共 24 条 amend): **16/24 = 66.7% 的响应 subject 不是
# 自己提交的那条 message**(例: 线程 5 发 AM-t05-r00, 返回提交的 subject 是
# AM-t04-r00)。同一跑里 amend 返回码全 200、git fsck rc=0、提交信息没有被拼接
# —— 所以这是**归属/上报错**, 不是仓体损坏。
#
# ⚠⚠ **只断言这一条, 不断言「tree 守恒」** —— 这是刻意的, 不是漏写:
#   同一跑里量到 tree(new_sha) != tree(old_sha) 有 **9/24 = 37.5%**, 差异确实是
#   别的线程 stage 的文件。但 `do_amend` 的 docstring **明写**「刻意不传 -a:
#   amend 的语义是『用当前 index 重写上次提交』」—— 所以「提交了当前 index 里的
#   内容」是**文档写明的语义, 不是缺陷**。断言 tree 守恒 = 断言产品从未承诺过的
#   契约 = **假判据**, 那正是「改判据 = 掩盖」的另一种形态。
#   tree 那个数在 C4 的输出里以 `amend_tree_changed` **照记**, 但**不进判据**;
#   真要把它变成红, 得先改产品(让请求整段持锁), 不是改判据。

def amend_subject_violations(recs):
    """响应里的 subject 不是我发的那条 message -> 违例。

    返回 None 表示**测不到**(输入为空), 不表示「通过」。
    """
    if not recs:
        return None
    bad = []
    for e in recs:
        if e["code"] != 200 or not (e["body"] or {}).get("ok"):
            continue                      # 请求失败的算另一条判据的事
        got = (e["body"] or {}).get("subject")
        if got != e["msg"]:
            bad.append({"t": e["t"], "r": e["r"], "want": e["msg"], "got": got,
                        "new": (e["body"] or {}).get("new_sha", "")[:12]})
    return bad


def amend_tree_changed(sb, recs):
    """tree(new_sha) != tree(old_sha) 的条数 —— **只测量, 不判据**。

    见上面那段注释为什么不能拿它当判据。返回 (条数, 分母)。
    分母为 0 时返回 (0, 0) —— 调用方**必须**自己判分母, 别拿 0/0 当 0%。
    """
    n = den = 0
    for e in recs:
        if e["code"] != 200 or not (e["body"] or {}).get("ok"):
            continue
        b = e["body"]
        old, new = b.get("old_sha"), b.get("new_sha")
        if not old or not new:
            continue
        den += 1
        t1 = sb.git("rev-parse", old + "^{tree}").stdout.strip()
        t2 = sb.git("rev-parse", new + "^{tree}").stdout.strip()
        if t1 != t2:
            n += 1
    return n, den


# ═══════════════════════════════════════════ HEAD 归属: undo / revert / reflog
#
# 与 amend_subject_violations 同一类缺陷(响应报的 HEAD 是别人的)。判据按「能不能
# 被独立证伪」分档, **每一条都写了它成立的理由, 以及被实测推翻过的候选**。
#
# ✅ 站得住的判据(现在就在断言):
#   · reflog 恢复 —— **绝对**「head_now 必须 == restored.sha」。reset 的目标是从
#     reflog 某一行读出来的 sha, 那行写下之后不再变(每次 HEAD 移动只是**追加**
#     一条), 所以不管 plan 之后别人把 HEAD 挪到哪, 我的落点恒为它。
#   · revert     —— **绝对必要条件**「new_head 必须是一个 revert 提交」
#     (subject 以 `Revert "` 开头)。git 自己写的提交信息长这样; new_head 一旦被
#     别人的提交顶替, subject 就是 `amend T3 r5` 这类东西。不依赖任何时序假设。
#   · undo       —— **绝对**「head_now 必须是一个可解析的提交」(弱, 但成立);
#     「互不相同」那条挪到 C4C 的 undo-only 负载里去判(见 C4C 的注释)。
#
# ✗ 被自己的阴性对照**证伪**的两条关系判据 —— 记下来, 免得下一个人再写一遍:
#   第一版 C4B 对 undo 和 revert 都写了「两次成功响应不得报同一个 sha」。假的, 两条
#   独立实验把它推翻(台账 §22.11 记了命令与数字):
#   A. **单线程串行**(零并发, 任何时刻只有一个请求 -> 缝根本踩不到): 跑同一套
#      undo+revert+reflog, undo 的 head_now 在 12 次响应里只有 **1 个**不同值,
#      朴素撞车 **11 条**。原因: HEAD 在混编负载下**不是单调**的 ——
#      reflog 恢复到 index 0 把 HEAD 放回 undo 刚访问过的提交, 下一个 undo 再退
#      一格就合法地落回同一个提交。
#   B. **同一 HEAD、同一秒**内 revert 同一条提交两次 => 两个 new_head 是**同一个
#      sha**。git 的提交是「内容+元数据」寻址的: parent / tree / message /
      #      author / committer 秒级时间戳全同, 就是同一个对象。
#   ⇒ 关系判据只有在**该操作独占 HEAD 轨迹**的负载里才成立, 所以 undo 的那条搬去
#     C4C(undo-only, HEAD 严格单调), 混编负载里只报不判。
#
# ⚠ 另外两个试过、同样不成立的口径:
#   1. `head_now == parent(undone)` —— undone 是 `plan_undo_last_commit`
#      **在锁外**读的(plan 与 reset 之间隔着两次取锁), 那期间 HEAD 可能已被别人
#      改过, 于是**正确实现**下这个等式也会不成立。拿它当判据就是断言产品从没
#      承诺过的契约 —— 假判据。
#   2. `head_now` 必须是「请求前 HEAD」的祖先 —— 那个请求前 HEAD 是**测试侧**读的,
#      夹在它和我的请求之间别的线程连着动两次 HEAD, 我的 undo 就会合法地落到那个
#      HEAD 的**严格后代**上 -> 假阳性。
def _ok_recs(recs):
    return [e for e in recs if e["code"] == 200 and (e["body"] or {}).get("ok")]


def fail_reasons(recs, limit=6):
    """失败的响应按 (HTTP 码, 错误码) 归类 —— **分母为什么这么小**要能看见。

    没有这个, 判据分母一旦塌掉(比如 revert_ok 从 16 掉到 4)就只能猜: 是产品
    的问题还是夹具的问题。这两种必须分得开。
    """
    from collections import Counter
    c = Counter()
    for e in recs:
        b = e["body"] or {}
        if e["code"] == 200 and b.get("ok"):
            c["ok"] += 1
            continue
        code = b.get("code") or b.get("error") or "(无 code)"
        msg = str(b.get("message") or b.get("error") or "").splitlines()
        head = msg[0][:70] if msg else ""
        c["HTTP %s / %s / %s" % (e["code"], code, head)] += 1
    return c.most_common(limit)


def undo_mono_violations(recs):
    """undo-**only** 负载下: 两次成功响应不得报同一个 head_now —— **硬判据**。

    成立的前提由调用方保证、并在用例里断言: 本负载**只有 undo 会动 HEAD**,
    于是每次成功都把 HEAD 严格往父提交退一格, 所有 head_now 必然互不相同。
    混编负载里这条是**假判据**(已被单线程阴性对照证伪, 见 undo_head_report),
    所以它只在本用例这种「独占 HEAD 轨迹」的形状下才成立。

    返回 None 表示**测不到**(没有一条成功响应), 不表示「通过」。
    """
    ok = _ok_recs(recs)
    if not ok:
        return None
    seen, bad = {}, []
    for e in ok:
        h = (e["body"] or {}).get("head_now")
        if not h:
            bad.append({"t": e["t"], "r": e["r"], "why": "响应里没有 head_now",
                        "body_keys": sorted((e["body"] or {}).keys())})
            continue
        if h in seen:
            bad.append({"t": e["t"], "r": e["r"], "other": list(seen[h]),
                        "why": "与另一条成功响应报了同一个 head_now",
                        "head_now": h[:12]})
        else:
            seen[h] = (e["t"], e["r"])
    return bad


def undo_head_report(sb, recs):
    """undo 响应的 HEAD —— 混编负载里**只报不判**。返回 None 表示**测不到**。

    返回 {"bad": [...], "collisions": n, "distinct": k, "den": n}:
      · `bad`         —— 真违例, 只收「响应缺 head_now」与「head_now 解析不出提交」。
                        这两条是绝对的: do_undo_last_commit 报的是 `self.head_sha()`,
                        锁内取的, 必然是一个真提交。
      · `collisions`  —— 「两条成功响应报了同一个 head_now」的条数。**混编负载里
                        不断言**, 因为它已被实测证伪(见上面那段注释的实验 A/B),
                        理由是 HEAD 在混编下非单调。undo-only 负载里它才是硬判据,
                        由 C4C 去断言。

    ⚠ 「只报不判」不是放过 —— 判据被证伪这件事本身是结论, 数字照样进台账,
      而且 undo 的归属另有两道防线: C4C 的 undo-only 硬判据, 以及静态的
      t112_seam_survey.py AST 普查(它对 do_undo_last_commit 是绿的)。
    """
    ok = _ok_recs(recs)
    if not ok:
        return None
    heads, bad = [], []
    for e in ok:
        b = e["body"] or {}
        h = b.get("head_now")
        if not h:
            bad.append({"t": e["t"], "r": e["r"], "why": "响应里没有 head_now",
                        "body_keys": sorted(b.keys())})
            continue
        heads.append(h)
        p = sb.git("cat-file", "-t", h)
        if p.returncode != 0 or (p.stdout or "").strip() != "commit":
            bad.append({"t": e["t"], "r": e["r"], "why": "head_now 不是一个提交对象",
                        "head_now": h[:12],
                        "git": (p.stderr or p.stdout or "").strip()[:70]})
    return {"bad": bad, "collisions": len(heads) - len(set(heads)),
            "distinct": len(set(heads)), "den": len(heads)}


def revert_head_violations(sb, recs):
    """revert 响应报的 new_head 必须是一个 **revert 提交**(绝对必要条件)。

    为什么这条是硬的: `git revert` 的提交信息由 git 自己写, 形如
    `Revert "<原 subject>"`。new_head 一旦被别人的提交顶替, 它的 subject 就是
    `amend T3 r5` 这类东西 —— 不依赖任何时序假设。

    ⚠ 第一版这里还带了「两次成功响应不得报同一个 new_head」, **已删**, 因为实测
      证伪: 从**同一个 HEAD**、**同一秒**内 revert 同一条提交两次, 两个 new_head
      是同一个 sha(提交是内容+元数据寻址的)。删掉的是一条假判据, 不是放过真缺陷。
    返回 None 表示**测不到**(没有一条成功响应), 不表示「通过」。
    """
    ok = _ok_recs(recs)
    if not ok:
        return None
    bad = []
    for e in ok:
        b = e["body"] or {}
        h = b.get("new_head")
        if not h:
            bad.append({"t": e["t"], "r": e["r"], "why": "响应里没有 new_head",
                        "body_keys": sorted(b.keys())})
            continue
        p = sb.git("show", "-s", "--format=%s", h)
        subj = (p.stdout or "").strip()
        if p.returncode != 0:
            bad.append({"t": e["t"], "r": e["r"], "why": "new_head 解析不出提交",
                        "new_head": h[:12],
                        "git": (p.stderr or p.stdout or "").strip()[:70]})
        elif not subj.startswith('Revert "'):
            bad.append({"t": e["t"], "r": e["r"], "why": "new_head 不是 revert 提交",
                        "new_head": h[:12], "subject": subj[:60]})
    return bad


def reflog_restore_head_violations(recs):
    """reflog 恢复的响应: head_now 必须**等于** restored.sha(绝对判据)。

    为什么这条是硬的: reset 的目标是 `plan_restore_from_reflog` 从 reflog **某一行**
    读出来的 sha。那一行写下之后就不再变 —— 每次 HEAD 移动只是往 reflog **追加**
    一条, 已有行的 sha 是历史事实。所以 plan 之后不管别的请求把 HEAD 挪到哪,
    我的 reset 落点恒为 restored.sha, head_now 必须等于它。
    """
    ok = _ok_recs(recs)
    if not ok:
        return None
    bad = []
    for e in ok:
        b = e["body"] or {}
        want = (b.get("restored") or {}).get("sha")
        got = b.get("head_now")
        if not want or not got:
            bad.append({"t": e["t"], "r": e["r"], "why": "响应缺 restored.sha 或 head_now",
                        "body_keys": sorted(b.keys())})
        elif want != got:
            bad.append({"t": e["t"], "r": e["r"], "why": "head_now 不是 restored.sha",
                        "want": want[:12], "got": got[:12], "mode": b.get("mode"),
                        "restored_short": (b.get("restored") or {}).get("short")})
    return bad


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
        amend_recs = []          # (t, r, 我发的 message, code, body) —— 归属判据要用
        rec_lock = threading.Lock()
        try:
            def body(t, r):
                k = t % 4
                if k == 0:
                    msg = "amend T%d r%d" % (t, r)
                    code, b = srv.post("/api/history/amend",
                                       {"message": msg, "confirmed": True})
                    with rec_lock:
                        amend_recs.append({"t": t, "r": r, "msg": msg,
                                           "code": code, "body": b})
                    return code, b
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

    # ═══════════ 请求级原子性(2026-10-05 新增, 从 C7 移交过来) ═══════════
    #
    # 阳性对照先做: 检查器必须能把「故意不符」的一对判成违例, 必须把相符的判成
    # 干净, 且**空输入必须判 None(测不到)而不是 0(通过)**。不先证明尺子会响,
    # 下面那个 0/0 或者 0/N 就什么都不能说明。
    _bad = amend_subject_violations(
        [{"t": 0, "r": 0, "msg": "MINE", "code": 200,
          "body": {"ok": True, "subject": "SOMEONE-ELSE"}}])
    _okp = amend_subject_violations(
        [{"t": 0, "r": 0, "msg": "MINE", "code": 200,
          "body": {"ok": True, "subject": "MINE"}}])
    _empty = amend_subject_violations([])
    ck(_bad and not _okp,
       "自检失败: 归属检查器坏了 —— 该抓的没抓(%r) 或 不该抓的抓了(%r)"
       % (_bad, _okp))
    ck(_empty is None,
       "自检失败: 空输入被当成「通过」(%r) —— 空集必须判 None(测不到)" % (_empty,))

    # 分母: 归属判据必须有**两条**非空自检, 否则下面的数是 0/0。
    _ok_amends = [e for e in amend_recs
                  if e["code"] == 200 and (e["body"] or {}).get("ok")]
    ck(len(amend_recs) > 0,
       "自检失败: 一条 amend 响应都没收到(负载有问题, 不是产品有问题)")
    ck(len(_ok_amends) > 0,
       "自检失败: %d 条 amend 响应里 0 条 ok=true —— 分母为 0, 这条判据测不到任何东西"
       % len(amend_recs))
    ck(all(e["msg"] for e in amend_recs),
       "自检失败: 有 amend 记录的 msg 是空串 —— 归属比对会恒成立, 白判")

    sub_bad = amend_subject_violations(_ok_amends)
    # tree 那个数: **只测量, 不判据**(理由见本文件 amend_subject_violations
    # 上方那段注释)。分母同样要写清, 不许出现没有分母的百分数。
    tree_n, tree_den = amend_tree_changed(sb, _ok_amends)
    RESULTS.append({"test": "C4-amend-ownership", "ops": len(_ok_amends),
                    "detail": {"amend_requests": len(amend_recs),
                               "amend_ok": len(_ok_amends),
                               "subject_mismatch": len(sub_bad),
                               "samples": sub_bad[:6],
                               "tree_changed_measured_only": tree_n,
                               "tree_changed_denominator": tree_den}})
    say("  C4 amend 归属: 响应 subject 不符 %d/%d (%.1f%%) | "
        "tree 变了 %d/%d (仅测量, 不判据)"
        % (len(sub_bad), len(_ok_amends),
           100.0 * len(sub_bad) / len(_ok_amends), tree_n, tree_den))

    ck(not sub_bad,
       "【响应归属错】%d/%d 条 ok 的 amend 响应, 它报的 subject 不是**本请求**"
       "提交的 message。根因: do_amend 在 `git commit --amend` **之后**才读 "
       "head_sha(), 而锁是每条 git 命令取一次(history_ops._run / server._run_full), "
       "两者之间别的 amend 能插队把 HEAD 再改一次, 于是本请求报出去的是别人的提交。"
       "样例(线程/轮次, 我发的, 实得): %r"
       % (len(sub_bad), len(_ok_amends),
          [(x["t"], x["r"], x["want"], x["got"]) for x in sub_bad[:5]]))

    if cls["git_lock"] or cls["http5xx"]:
        raise Flaky("git 锁 %d/%d, 5xx %d/%d"
                    % (len(cls["git_lock"]), len(ops),
                       len(cls["http5xx"]), len(ops)))
    return None


# ════════════════════════════════════════════════════════ C4B
#
# undo / revert / reflog 恢复三条响应的 HEAD 归属 —— 与 C4 的 amend 归属
# **同一类缺陷**(响应报的 HEAD 是别人的), 但这三处 C4 一条判据都没有。
#
# 为什么单独一个用例而不在 C4 里加: C4 的负载是 `t % 4` 四分(amend/undo/
# revert/stage), 加第五种操作就得改这个分派, 而 `10/18 -> 0/18` 那个已记录的
# 数是**在 t%4 那套负载下量出来的** —— 改了负载, 台账里那条就不可复现了。
# 这里是 `t % 3` 三分, 各自的分母独立报。
#
# 负载只用这三种 history 操作, 互不加 stage: 它们取的是**同一把**锁, 所以
# 「reset 刚放锁 -> head_sha 还没取锁」这个窗口最紧, 命中率最高, 而分母不会被
# stage 的失败(win32 共享冲突之类)搅浑。

@test("C4B", "history-attribution",
      "undo / revert / reflog 恢复三条响应的 HEAD 归属(与 C4 的 amend 归属同类)")
def t_c4b_history_attribution():
    # ── 尺子先证明自己会响 ──────────────────────────────────────────
    # 用一个**独立的一次性仓**做, 不碰主夹具 —— 这样阳性对照的成败与负载跑出
    # 什么数据无关(若拿主夹具的数据做对照, 修好之后就没有「脏」样本可喂了)。
    rb = Sandbox("c4b-ruler")
    rb.write("a.txt", "a\n")
    rb.commit("c1: base")
    r_base = rb.out("rev-list", "--max-count=1", "HEAD").strip()
    # 必须先动一下文件: Sandbox.commit 是 `add -A` + `commit -m`, **不带
    # --allow-empty**, 树没变时 git 直接拒绝空提交, 两次 rev-parse 会拿到同一个 sha。
    rb.write("a.txt", "b\n")
    rb.commit('Revert "selfcheck"')
    r_rev = rb.out("rev-list", "--max-count=1", "HEAD").strip()
    ck(r_base and r_rev and r_base != r_rev,
       "尺子仓自检失败: base=%s rev=%s" % (r_base[:12], r_rev[:12]))

    def _rec(t, r, **body):
        return {"t": t, "r": r, "code": 200, "body": body}

    # undo: 只剩「缺字段」和「不是提交对象」两条绝对判据。撞车那条**故意**喂一个
    # 会撞的样本, 并断言它落在 `collisions` 计数里而 `bad` 为空 —— 这样「混编负载里
    # 不断言撞车」是**被检查的行为**, 不是悄悄放过的。
    _u_dup = undo_head_report(rb, [_rec(0, 0, ok=True, head_now=r_base),
                                  _rec(1, 0, ok=True, head_now=r_base)])
    _u_ok = undo_head_report(rb, [_rec(0, 0, ok=True, head_now=r_base),
                                  _rec(1, 0, ok=True, head_now=r_rev)])
    _u_empty = undo_head_report(rb, [])
    _u_miss = undo_head_report(rb, [_rec(0, 0, ok=True)])
    _u_junk = undo_head_report(rb, [_rec(0, 0, ok=True, head_now="de" + "ad" * 19)])
    ck(_u_dup and not _u_dup["bad"] and _u_dup["collisions"] == 1
       and _u_dup["den"] == 2,
       "自检失败: undo 撞车样本必须**只**进 collisions, 不得进 bad —— 得 %r" % (_u_dup,))
    ck(_u_ok and not _u_ok["bad"] and _u_ok["collisions"] == 0,
       "自检失败: undo 不撞车样本应全清 —— 得 %r" % (_u_ok,))
    ck(_u_junk and _u_junk["bad"], "自检失败: head_now 解析不出提交要抓到")
    ck(_u_miss and _u_miss["bad"], "自检失败: 缺 head_now 要抓到")
    ck(_u_empty is None,
       "自检失败: undo 检查器把空输入判成「通过」(%r) —— 空集必须判 None(测不到)"
       % (_u_empty,))

    # revert: 非 revert 提交要抓到 / 解析不出要抓到 / 合法形态要放过 / 空判 None
    _r_wrong = revert_head_violations(rb, [_rec(0, 0, ok=True, new_head=r_base)])
    _r_junk = revert_head_violations(rb, [_rec(0, 0, ok=True, new_head="de" + "ad" * 19)])
    _r_ok = revert_head_violations(rb, [_rec(0, 0, ok=True, new_head=r_rev)])
    _r_empty = revert_head_violations(rb, [])
    ck(_r_wrong and _r_junk and not _r_ok,
       "自检失败: revert 检查器坏了 —— 非revert没抓=%r / 解析不出没抓=%r / 合法误抓=%r"
       % (_r_wrong, _r_junk, _r_ok))
    ck(_r_empty is None,
       "自检失败: revert 检查器把空输入判成「通过」(%r)" % (_r_empty,))
    say("  C4B 阳性对照: undo 缺字段/非提交/撞车只计数/空=%s/%s/%s/%s | "
        "revert 非revert/解析不出/合法/空=%s/%s/%s/%s"
        % (bool(_u_miss), bool(_u_junk), _u_dup["collisions"] == 1, _u_empty is None,
           bool(_r_wrong), bool(_r_junk), not _r_ok, _r_empty is None))

    # reflog 恢复: 不等要抓到 / 相等要放过 / 空判 None
    _f_bad = reflog_restore_head_violations(
        [_rec(0, 0, ok=True, mode="soft", head_now="b" * 40,
              restored={"sha": "a" * 40, "short": "aaaaaaaa"})])
    _f_ok = reflog_restore_head_violations(
        [_rec(0, 0, ok=True, mode="soft", head_now="a" * 40,
              restored={"sha": "a" * 40, "short": "aaaaaaaa"})])
    _f_empty = reflog_restore_head_violations([])
    ck(_f_bad and not _f_ok,
       "自检失败: reflog 检查器坏了 —— 不等没抓=%r / 相等误抓=%r" % (_f_bad, _f_ok))
    ck(_f_empty is None,
       "自检失败: reflog 检查器把空输入判成「通过」(%r)" % (_f_empty,))

    # ── 负载 ──────────────────────────────────────────────────────
    THR, RND = 6, 8
    sb = fx_history_deep(Sandbox("c4b"))
    n_commit = sb.out("rev-list", "--count", "HEAD").strip()
    ck(n_commit == str(FX_DEEP_N),
       "夹具自检: 提交数 %s(应 %d)" % (n_commit, FX_DEEP_N))
    # revert 目标 = `c3`(动的是**专属**文件 r/r03.txt, 后面没人再改它)。
    # ⚠ 位置必须按**提交数**算: `rev-list` 是最新在前, `--skip=2` 取到的是第 3 个
    #   **最新**的提交(c22, 属深度段), 不是第 3 旧的(c3, 属 fodder 段)。我第一版
    #   就是这么写错的 —— 后果很安静: c22 动的是 w/f06, 而 c14 也动 w/f06,
    #   HEAD 被 undo 退到 c14 之下时逆补丁贴不上 -> 冲突 -> revert_ok 塌到 2/16。
    #   所以这里不只断言「取到了提交」, 还断言 **subject 正是我要的那一条**。
    revert_target = sb.out("rev-list", "--max-count=1",
                           "--skip=%d" % (FX_DEEP_N - 3), "HEAD").strip()
    tgt_subject = sb.out("show", "-s", "--format=%s", revert_target).strip()
    ck(tgt_subject == "c3: revert fodder 3",
       "夹具自检: revert 目标取错了提交 —— 要 'c3: revert fodder 3', 实得 %r"
       % tgt_subject[:60])

    recs = {"undo": [], "revert": [], "reflog": []}
    rec_lock = threading.Lock()

    with Server(sb, "c4b") as srv:
        pr = Probe()
        import server as S, history_ops as H
        pr.install_label(S.Repo, "_run_full")
        pr.install_label(H.HistoryOps, "_run")
        pr.install_lock(srv.mod.REPO, "_lock", "repo")
        pr.install_lock(srv.mod.history_ops(), "_lock", "history")
        pr.install_request(S.Handler, "do_POST", _path_label)
        pr.install_request(S.Handler, "do_GET", _path_label)

        def keep(kind, t, r, code, b):
            with rec_lock:
                recs[kind].append({"t": t, "r": r, "code": code, "body": b})

        try:
            def body(t, r):
                k = t % 3
                if k == 0:
                    # drop_changes=True -> `reset --hard HEAD~1`: 硬复位把工作区收干净,
                    # 否则软复位留下的 staged 改动会让后面的 `git revert` 拒绝执行
                    # (「local changes would be overwritten」), 分母就被自己的夹具掐死。
                    code, b = srv.post("/api/history/undo-last-commit",
                                       {"drop_changes": True, "confirmed": True})
                    keep("undo", t, r, code, b)
                    return code, b
                if k == 1:
                    code, b = srv.post("/api/history/revert",
                                       {"sha": revert_target, "no_edit": True})
                    keep("revert", t, r, code, b)
                    return code, b
                # index 0 = reflog 最新一行 = 此刻的 HEAD, 于是 reset 落点恒为它,
                # head_now 必须等于 restored.sha —— 绝对判据成立的前提。
                # mode=hard: 软复位只挪 HEAD 不动索引, 于是并发 undo 之后索引与
                # HEAD 不一致, 后续 `git revert` 会以「local changes would be
                # overwritten」拒绝执行 —— 分母被自己的复位方式掐死。硬复位把
                # 索引收干净。仓里没有未提交内容, 丢不掉东西。
                code, b = srv.post("/api/history/reflog/restore",
                                   {"index": 0, "mode": "hard", "confirmed": True})
                keep("reflog", t, r, code, b)
                return code, b

            ops, el, slow = run_ops(THR, RND, body, "history")
        finally:
            pr.remove()
        cls = report("C4B", ops, extra={"elapsed_s": round(el, 1),
                                         "git_calls": pr.n(),
                                         "revert_target": revert_target[:12]})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["traceback"], "响应里带 traceback: %r" % cls["traceback"][:3])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["git_lock"],
       "【锁域不一致】%d/%d 次请求的失败原因是 git 锁文件被占, 例 %r"
       % (len(cls["git_lock"]), len(ops), [x[2] for x in cls["git_lock"][:3]]))

    # ── 分母必须非空, 否则下面每一个数都是 0/0 ──────────────────────
    for kind in ("undo", "revert", "reflog"):
        ck(len(recs[kind]) > 0,
           "自检失败: %s 一条响应都没收到(负载有问题, 不是产品有问题)" % kind)
        ck(len(_ok_recs(recs[kind])) > 0,
           "自检失败: %d 条 %s 响应里 0 条 ok=true —— 分母为 0, 判据测不到任何东西"
           % (len(recs[kind]), kind))

    u_rep = undo_head_report(sb, recs["undo"])
    r_bad = revert_head_violations(sb, recs["revert"])
    f_bad = reflog_restore_head_violations(recs["reflog"])
    RESULTS.append({"test": "C4B-attribution",
                    "detail": {"undo_requests": len(recs["undo"]),
                               "undo_ok": len(_ok_recs(recs["undo"])),
                               "undo_bad": len(u_rep["bad"]),
                               "undo_collisions_measured": u_rep["collisions"],
                               "undo_distinct": u_rep["distinct"],
                               "undo_samples": u_rep["bad"][:6],
                               "revert_requests": len(recs["revert"]),
                               "revert_ok": len(_ok_recs(recs["revert"])),
                               "revert_violations": len(r_bad),
                               "revert_samples": r_bad[:6],
                               "reflog_requests": len(recs["reflog"]),
                               "reflog_ok": len(_ok_recs(recs["reflog"])),
                               "reflog_violations": len(f_bad),
                               "reflog_samples": f_bad[:6]}})
    say("  C4B HEAD 归属(判据): revert 越界 %d/%d | reflog 不等 %d/%d | "
        "undo 坏值 %d/%d" % (len(r_bad), len(_ok_recs(recs["revert"])),
                            len(f_bad), len(_ok_recs(recs["reflog"])),
                            len(u_rep["bad"]), u_rep["den"]))
    say("  C4B undo 撞车 %d/%d (distinct %d) —— **只测量不断言**, 该口径已被"
        "单线程阴性对照证伪(见 undo_head_report 的注释)"
        % (u_rep["collisions"], u_rep["den"], u_rep["distinct"]))
    # 违例**为什么**是违例必须打出来。只打计数的话, 剩下几条违例时只能猜:
    # 是没修干净, 还是**判据本身站不住** —— 这两者的处置完全相反(前者继续修,
    # 后者要改判据并说明为什么原判据不成立)。改判据 = 掩盖, 所以先拿事实。
    from collections import Counter as _Ct
    for _tag, _bad in (("undo", u_rep["bad"]), ("revert", r_bad), ("reflog", f_bad)):
        if _bad:
            say("  C4B %-7s 违例构成: %s" % (_tag, dict(_Ct(x["why"] for x in _bad))))
            for _x in _bad[:5]:
                say("           %r" % (_x,))
    for kind in ("undo", "revert", "reflog"):
        rs = fail_reasons(recs[kind])
        say("  C4B %-7s 响应构成(成功 %d / 共 %d): %s"
            % (kind, len(_ok_recs(recs[kind])), len(recs[kind]),
               " | ".join("%s x%d" % (k, v) for k, v in rs)))

    ck(not u_rep["bad"],
       "【undo 响应坏值】%d/%d 条成功的 undo 响应, head_now 缺失或不是一个提交对象。"
       "do_undo_last_commit 报的是**锁内**取的 self.head_sha(), 必然是真提交。"
       "样例: %r" % (len(u_rep["bad"]), u_rep["den"], u_rep["bad"][:5]))

    ck(not r_bad,
       "【revert 响应归属错】%d/%d 条成功的 revert 响应, new_head 不是一个 revert "
       "提交。根因同 C4: do_revert 在 `git revert` **之后**才读 head_sha(), 中间别的 "
       "revert / reset 能插队, 于是报出去的是别人的提交 —— 而 git 自己写的 revert "
       "提交信息必以 `Revert \"` 开头, 顶包的提交藏不住。样例: %r"
       % (len(r_bad), len(_ok_recs(recs["revert"])),
          [(x["t"], x["r"], x["why"], x.get("subject", "")) for x in r_bad[:5]]))

    ck(not f_bad,
       "【reflog 恢复响应归属错】%d/%d 条成功的 reflog 恢复响应, head_now 不等于 "
       "restored.sha。reset 的目标是从 reflog 某一行读出来的 sha, 那行写下后不再变, "
       "所以 head_now 恒等于它。根因同 C4: do_restore_from_reflog 在 `git reset` "
       "**之后**才读 head_sha()。样例(我发/实得/short): %r"
       % (len(f_bad), len(_ok_recs(recs["reflog"])),
          [(x["t"], x["r"], x.get("want", ""), x.get("got", ""),
            x.get("restored_short", "")) for x in f_bad[:5]]))

    if cls["git_lock"] or cls["http5xx"]:
        raise Flaky("git 锁 %d/%d, 5xx %d/%d"
                    % (len(cls["git_lock"]), len(ops),
                       len(cls["http5xx"]), len(ops)))
    return None


# ════════════════════════════════════════════════════════ C4C
#
# undo 的响应归属 —— **undo-only** 负载。
#
# 为什么单独开一个用例: C4B 是 undo+revert+reflog 混编, 那条 HEAD 轨迹**不是单调**
# 的(实测见 undo_head_report 的注释), 于是「两次成功响应不得报同一个 head_now」
# 是假判据, 在混编里只能**只报不判**。本用例把负载收窄成**只有 undo 会动 HEAD**,
# 于是每次成功都把 HEAD 严格往父提交退一格, 所有 head_now 必然互不相同 ——
# 这时「互不相同」才是硬判据, 才真的能判 do_undo_last_commit 的归属。
#
# ⚠ 判据成立的前提是「只有 undo 动 HEAD」。这个前提**必须由用例自己断言**, 不能
#   靠注释: 以后有人往本用例的负载里加一个 revert / reflog 恢复, 判据立刻变假而
#   没人会知道。所以下面 `ck(OP_KINDS == ("undo",), ...)` 那条不许删。

C4C_TH, C4C_RND, C4C_N = 6, 6, 44


@test("C4C", "undo-attribution",
      "undo-only 负载下 undo 响应的 head_now 归属(混编负载里这条判据是假的)")
def t_c4c_undo_attribution():
    # ── 尺子先证明自己会响 ──────────────────────────────────────────
    rb = Sandbox("c4c-ruler")
    rb.write("a.txt", "a\n")
    rb.commit("c1: base")
    r1 = rb.out("rev-list", "--max-count=1", "HEAD").strip()
    rb.write("a.txt", "b\n")
    rb.commit("c2: second")
    r2 = rb.out("rev-list", "--max-count=1", "HEAD").strip()
    ck(r1 and r2 and r1 != r2, "尺子仓自检失败")

    def _rec(t, r, **body):
        return {"t": t, "r": r, "code": 200, "body": body}

    _dup = undo_mono_violations([_rec(0, 0, ok=True, head_now=r1),
                                 _rec(1, 0, ok=True, head_now=r1)])
    _ok = undo_mono_violations([_rec(0, 0, ok=True, head_now=r1),
                                _rec(1, 0, ok=True, head_now=r2)])
    _miss = undo_mono_violations([_rec(0, 0, ok=True)])
    ck(_dup and _miss, "自检失败: undo-mono 检查器坏了 —— 撞车没抓=%r / 缺字段没抓=%r"
       % (_dup, _miss))
    ck(not _ok, "自检失败: 不撞车被误抓=%r" % (_ok,))
    ck(undo_mono_violations([]) is None,
       "自检失败: undo-mono 把空输入判成「通过」—— 空集必须判 None(测不到)")
    say("  C4C 阳性对照: 撞车/合法/缺字段/空=%s/%s/%s/%s"
        % (bool(_dup), not _ok, bool(_miss), undo_mono_violations([]) is None))

    # ── 判据自身的阴性对照: 单线程 undo-only 必须**零撞车** ──────────
    # 单线程下根本没有缝可踩(任何时刻只有一个请求), 所以这里若响, 就是判据在
    # 这个负载形状下不成立, 而不是产品有缺陷。混编那条口径就是因为少了这一步,
    # 才被当成判据写进 C4B —— 这一段不能省。
    sb_s = fx_history_deep(Sandbox("c4c-serial"), n=C4C_N)
    s_recs = []
    s_lock = threading.Lock()
    with Server(sb_s, "c4c-serial") as srv:
        def s_body(t, r):
            code, b = srv.post("/api/history/undo-last-commit",
                               {"drop_changes": True, "confirmed": True})
            with s_lock:
                s_recs.append({"t": t, "r": r, "code": code, "body": b})
            return code, b
        s_ops, s_el, _ = run_ops(1, C4C_RND, s_body, "undo-serial",
                                  barrier_each_round=False)
    s_bad = undo_mono_violations(s_recs)
    ck(s_bad is not None, "自检失败: 单线程对照分母为 0(%d 条响应)" % len(s_recs))
    ck(not s_bad,
       "【判据不成立】单线程 undo-only(%d 次成功响应, %.1fs) 里就撞车了 %d 条 —— "
       "说明「互不相同」在 undo-only 形状下也不是硬判据, 本用例的判据必须重做。"
       "样例: %r" % (len(_ok_recs(s_recs)), s_el, len(s_bad), s_bad[:5]))
    say("  C4C 阴性对照(单线程 undo-only): 成功 %d 条, 撞车 %d 条 -> 判据成立"
        % (len(_ok_recs(s_recs)), len(s_bad)))

    # ── 负载 ──────────────────────────────────────────────────────
    sb = fx_history_deep(Sandbox("c4c"), n=C4C_N)
    n_commit = sb.out("rev-list", "--count", "HEAD").strip()
    ck(n_commit == str(C4C_N), "夹具自检: 提交数 %s(应 %d)" % (n_commit, C4C_N))
    ck(C4C_TH * C4C_RND < C4C_N,
       "负载自检: %d 次 undo 需要超过 %d 层历史, 否则分母会被「HEAD~1 不存在」掐死"
       % (C4C_TH * C4C_RND, C4C_N))

    OP_KINDS = ("undo",)          # 判据成立的前提, 见上面那段注释
    ck(OP_KINDS == ("undo",),
       "本用例的判据要求「只有 undo 动 HEAD」, 而负载里出现了 %r —— "
       "往里加 revert / reflog 恢复会让判据变假" % (OP_KINDS,))

    recs = []
    rec_lock = threading.Lock()
    with Server(sb, "c4c") as srv:
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
                code, b = srv.post("/api/history/undo-last-commit",
                                   {"drop_changes": True, "confirmed": True})
                with rec_lock:
                    recs.append({"t": t, "r": r, "code": code, "body": b})
                return code, b
            ops, el, slow = run_ops(C4C_TH, C4C_RND, body, "undo")
        finally:
            pr.remove()
        cls = report("C4C", ops, extra={"elapsed_s": round(el, 1),
                                         "git_calls": pr.n()})

    fs = fsck_errors(sb)
    ck(fs is None, "git fsck 报对象库不一致: %s" % fs)
    ck(not cls["client_exc"], "客户端侧异常: %r" % cls["client_exc"][:2])
    ck(not cls["traceback"], "响应里带 traceback: %r" % cls["traceback"][:3])
    ck(not cls["http5xx"], "有 %d/%d 次 5xx, 例 %r"
       % (len(cls["http5xx"]), len(ops), cls["http5xx"][:3]))
    ck(not cls["git_lock"],
       "【锁域不一致】%d/%d 次请求的失败原因是 git 锁文件被占, 例 %r"
       % (len(cls["git_lock"]), len(ops), [x[2] for x in cls["git_lock"][:3]]))

    bad = undo_mono_violations(recs)
    RESULTS.append({"test": "C4C-attribution",
                    "detail": {"requests": len(recs),
                               "ok": len(_ok_recs(recs)),
                               "violations": len(bad),
                               "samples": bad[:6]}})
    say("  C4C undo 归属: 撞车 %d/%d | 响应构成: %s"
        % (len(bad), len(_ok_recs(recs)),
           " | ".join("%s x%d" % (k, v) for k, v in fail_reasons(recs))))
    ck(bad is not None and len(_ok_recs(recs)) >= C4C_TH * C4C_RND // 2,
       "自检失败: %d 条成功响应 / %d 条请求 —— 分母太小, 判据说明不了问题"
       % (len(_ok_recs(recs)), len(recs)))
    ck(not bad,
       "【undo 响应归属错】%d/%d 条成功的 undo 响应报了**同一个** head_now —— 本用例"
       "里只有 undo 会动 HEAD, 每次成功都严格退一格, 落点必然互不相同。根因: "
       "do_undo_last_commit 在 `git reset --hard HEAD~1` **之后**才读 head_sha(), "
       "而锁是每条 git 命令各取一次(history_ops._run), 中间别的 undo 能插队再退一格, "
       "于是本请求报出去的是**别人的**落点。样例(线程/轮次): %r"
       % (len(bad), len(_ok_recs(recs)),
          [(x["t"], x["r"], x.get("head_now", "")) for x in bad[:5]]))

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
# ⚠ 这一段原本是 C7 的「三条通路的锁状况」说明, 但它是**过时的副本**:
#   它写着三把锁「不是同一把 —— 这才是本条判的东西」, 还说 Repo._lock 是
#   「每实例 RLock」、HistoryOps._lock 是「每实例 Lock」。这三句都已被实测推翻
#   (三把是**同一把**, 且是**按 root 共享**、不是每实例一把)。
#   权威版本在本文件 C7 段的头注释(搜 `@test("C7"` 上方那一整块), 那里逐行给出了
#   conflicts.py:229 / server.py:484 / history_ops.py:158 三处接线与实测 id。
#   2026-10-05 把这份副本删掉, 只留指针 —— 两份说明各说各话, 下一个人读哪份
#   全看运气, 而两份结论**相反**。

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
    ck(summ["requests_interleavable"] > 0,
       "自检失败: requests_interleavable=0 —— 没有任何请求发过 2 条以上 git, "
       "此时 requests_interleaved 恒为 0 是**没测到**, 不是**没插队**。%r" % summ)
    ck(not inter,
       "【操作不是原子的】%d/%d 个**有资格**的请求(own>=2, 口径见 analyze_requests)"
       "在自己的首尾两条临界区之间被别的请求插过队"
       "(全部请求 %d 个, git 命令条数分布 %r)。"
       "⚠ 本消息里凡是标「已修复」的, 都是 2026-10-04 修掉的**产品缺陷**。"
       "四处都修完了。修之前 C3 读 48~49/50, **每修一处单独量一次**(逐处读数见本"
       "函数末尾的注释), 最后读到上面这个数。**别把下面当现状读。**"
       "  1) ✅已修复 Repo.checkout() 的 show-ref 与 checkout 原是**两次独立取锁**,"
       "中间有 900~1100ms 的缝 —— 那是 verify-then-act TOCTOU(别的请求可以在"
       "缝里把分支删掉)。修法: 两条 git 包进**同一次**持锁, 现在在 server.py:1472,"
       "show-ref 在 :1473-1474、checkout 在 :1476。修后实测 checkout 的外层"
       "持锁 span 把两条 git **包在一起**, 中间无缝。"
       "  2) ✅已修复 status() **自身**的两次独立取锁: `git status`(修前 :653)与"
       "`_head_branch()`(修前 :668) 各取一次锁、中间没有外层持锁包着。打点实测"
       "那道缝最宽 **1578 ms**, 内容是 status() 在**等锁**(那时别的请求正持锁"
       "跑 commit)。修法: 两条 git 包进同一次持锁, 现在在 :668 —— `git status` "
       "在 :669-670、`_head_branch()` 在 :671。修完 1) 读 26/50, 修完这条读 5/50。"
       "⚠ 这一条修之前在本消息里被写成「**已知例外, 不是缺陷**, 待裁定要不要为它"
       "也合并持锁」(理由是 status() 只读、拆成两次取锁不造成危害)。裁定下来要"
       "合并, 于是它现在也是缺陷, 已修 —— 那句「已知例外」已作废, 别再引用。"
       "  3) ✅已修复 /api/commit 的 handler 尾巴原在 commit() **释放锁之后**"
       "又取一次锁做 `REPO.status()`(修前 server.py:1758-1759), 后果是响应里的"
       "status 可能不是自己那次提交。修法: 新增 `Repo.commit_and_status()`"
       "(server.py:1387), commit+status 在同一次持锁里做完, handler 改走它"
       "(server.py:1907)。修后 commit 请求的插队数**归零**。"
       "  4) ✅已修复 /api/checkout 与 /api/branch 的**同一道尾巴缝**。checkout()"
       "原子之后, handler 仍在它释放锁之后才去取 status 的锁。打点实测"
       "(diag_residual.py, THR=6 RND=12): 修完 1)+2) 之后 C3 剩下的 26 条里"
       "**24 条是 /api/checkout、2 条是 /api/branch**, 缝宽 1.2~1.4s, 位置在"
       "操作自己干完(+57~114ms)之后、自己的 status() 开始之前。修法: 新增"
       "`Repo.checkout_and_status()`(:1413)与 `Repo.new_branch_and_status()`"
       "(:1433), handler 改走它们(:1914 / :1917)。"
       "⚠ 这三个(加上 :1387 那个)都走 `Repo._status_quiet()`(:1364), 别拆掉 —— 拆了"
       "等于把 E3 兜底(已做完的操作不许报成失败、否则客户端会重试做两遍)拆掉,"
       "而那条防线在 2026-10-04 之前**零测试覆盖**(test_aigit.py 的 E3 用例是"
       "这轮才加的)。"
       "  5) ⚠ 旧口径(按 HTTP 窗口算插队)多报过一批**假的**: /api/branch 里"
       "另 13 个请求自己只发 1 条 git(own=1), **结构上没有缝隙可插**, 是"
       "「别人的整段操作恰好落在本请求的 HTTP 窗口之内」被算成了插队。"
       "**那个口径 2026-10-04 已就地改正**(见 analyze_requests), own=1 的请求"
       "不再有插队资格。本次运行 own=1 的有 %d 个(一个都没被算进来); 分母 %d "
       "是 own>=2 的请求数。"
       % (inter, summ["requests_interleavable"], summ["requests"],
          summ["git_call_histogram"], summ["requests_with_1_git"],
          summ["requests_interleavable"]))
    # 复现上面那三行归因的探针(私有 TEMP, 不进仓, 用完即删):
    #   照抄本用例的 Sandbox/Server/run_ops/探针安装, 只多两件事:
    #     a) ~~install_request 的标签函数换成从 `self.path` 取接口路径~~
    #        **2026-10-05 已做**(用户授权, 条件是变异与台账一起重跑)。
    #        原 `_path_label` 只在**位置参数**里找 `/api/` 开头的字符串, 而
    #        `do_POST(self)` 的位置参数里只有 handler 实例 ⇒ 它对 do_POST
    #        **一律**返回 "request", 72 条全一个标签, 说不出是谁插的队。
    #        改成读 handler 的 `self.path` 之后, 报表里每个接口各归各的。
    #        ⚠ 前提是**实测确认**它不改判定: `analyze_requests` 只按 `req` 这个
    #        ident 归因、不读 label, 全文也没有拿 label 与 "request" 比较的地方
    #        (只有 domain 被断言)。配套的阳性自检加在 `analyze_requests` 末尾 ——
    #        少了它, 这个退化是**静默**的, 没有任何判据会红。
    #     b) 把 inter 拆成两种成因:
    #        真非原子 = 别人的 span 满足 own[0].t0 < other.t0 且 other.t1 < own[-1].t1;
    #        仅仅包含 = 别人的整段落在本请求 HTTP 窗口内, 但本请求自己的
    #                    临界区一条都没被切开(own 只有 1 条时**必然**如此)。
    #   ⚠ 下面这组数是 **2026-10-04 修 server.py 之前**的归因结果, 留作对照。
    #      **修之后**的数字见上面消息里的 1)~4), 别把这里当现状。
    #   修前实测(72 请求, 旧口径 inter 63): 真非原子 49; 拆分后
    #     /api/checkout 判 24 / 真 24 (own=4, 缝在 show-ref↔checkout)  ✅已修
    #     /api/commit  判 24 / 真 23 (own=11~12, 缝在 commit↔status↔symbolic-ref,
    #                              全在 commit() 释放锁**之后**)          ✅已修
    #     /api/branch  判 15 / 真  2 (own=1 的 22 条结构上不可能真非原子)
    #   修后**逐处**单独量(每次只改一处就跑 `-k C3`, 不是四处改完才量):
    #     修 1) checkout 的两条 git 合并持锁 → C3 48~49/50 → **26/50**
    #     修 2) status 的两条 git 合并持锁   → 26/50 → **5/50**
    #     修 3) commit_and_status            → commit 那 24 条**归零**
    #     修 4) checkout_and_status + new_branch_and_status → **0/72**
    #   hist 每档 +1 = 每处新加的那次外层持锁(TracingLock 每次 __enter__ 记一条)。
    #   分母 50 → **72**: own=1 的请求从 22 个变成 0 个(实测)。也就是说**判据变严
    #   了 22 条**, 结果仍然读 0 —— 分母变大不是放水。
    #   ⚠ 是**哪几个接口**从 own=1 变过来的 —— **未查清**: 探针的 label 全是
    #     "request"(已知限制), 而 diag_fingerprint.py 用的「own 窗口里第一个 POST
    #     标记」这个启发式不可信(排队请求的窗口互相重叠, 它把 /api/checkout 标成
    #     own=13/14, 那是 commit 的指纹)。不编解释。
    #   ⚠ 另有一处**未查清**: 修 2) 之后, diag_residual.py 在**同一份代码**上读到
    #     interleaved **26**, 而 `-k C3` 读到 **5**, 而两者的 own 直方图**逐字
    #     相同**。这与台账 §20.5 记的 26-vs-3 同类, 根因仍未查明(见 §21.6)。
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
#   * repo 域     —— server.Repo._run_full, 锁 = Repo._lock
#   * history 域  —— history_ops.HistoryOps._run, 锁 = HistoryOps._lock
#   * conflicts 域—— conflicts.Git.run, 锁 = conflicts.Git._lock
#
# ⚠⚠ **2026-10-05 就地改正**: 原来这里写的是「Repo._lock(**每实例** RLock)、
#   HistoryOps._lock(**每实例** Lock)」和「三把都**有锁**, 但**不是同一把**
#   —— **这才是本条判的东西**」。这三句**已被实测推翻**, 而且与**同一块下面
#   十几行**(「三条通路现在**共用同一个函数、同一个字典**」)**自相矛盾** ——
#   读的人读到上半段和读到下半段会得到相反的结论。
#   2026-10-05 重新实测(不引用任何既有注释的说法; 阳性对照: 两把新建锁 id 不同、
#   同一把取两次 id 相同, 先证明「id 比较」这把尺子能分辨):
#     server.Repo._lock / HistoryOps._lock / conflicts.Git._lock
#         三者 `id()` **完全相同**, 类型都是 **RLock**(可重入);
#     repolock.repo_git_lock(root) 与上面三者**同一个对象**;传 Path(root) /
#         新建的 Path(root) / str(root) 都返回同一把;
#     取到的 4 个实例(Repo / HistoryOps / conflicts.Git / repo_git_lock)
#         唯一 id 数 = **1**;
#     **另建一个 Repo(root) 实例, 它的 _lock 仍是同一把**
#         —— 所以是「**按 root 共享**」, **不是「每实例一把」**。
#   ⇒ 「三把锁不是同一把」这条**已经修好了**(repolock.py; 接线见下面三行),
#     它**不再是本条判的东西**。本条现在判的是: 同域重叠必须 0(锁有效 + 探针自检),
#     跨域重叠必须 0。
#     ⚠ 跨域这一条**在锁统一之后不再是独立信号** —— 三个域共用一把锁时,
#       「跨域不重叠」几乎由「同域不重叠」推出。它仍值得留着, 但它红了**不能**
#       直接读成「锁不是同一把」, 必须重新实测原因(见下面那句「本条若仍红」)。
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
    # ⚠⚠⚠ **2026-10-05 移交**: 原来这里有一条
    #     ck(not summ["requests_interleaved"], "【操作不是原子的】...")
    #     **删掉了**, 不是因为它变绿了, 而是因为它在**这个负载上根本测不到**它要抓的
    #     东西, 恒红且零信息量。两个实测理由:
    #
    #   (1) **本条的 history 腿是空跑**。夹具 fx_merge_conflict_n(sb, 12) 把仓留在
    #       **合并态**(实测 index 未合并路径 36 条), 而 git 拒绝在合并态 amend:
    #         实测 24/24 条 amend 全部 HTTP 400,
    #         "git commit --amend 失败:fatal: You are in the middle of a merge
    #          -- cannot amend."(code=amend_failed)
    #       ⇒ history 域一次 amend 都没真跑成; requests_interleaved 里它的贡献
    #         只是「插了队但什么也没干」。**分母为 0 的量不进判据。**
    #
    #   (2) **那个数不携带信息**。锁是每条 git 命令取一次, 一次请求的两次 git 调用
    #       之间必然有缝, 缝宽与并发度成正比。实测 836/836 个外来 span **全部**落在
    #       缝里, **真重叠 0**。改成「真重叠 = 0」能把这条洗成绿 —— 但本条要抓的
    #       损害**不需要真重叠**也能发生, 所以那样改就是掩盖。
    #
    #   ⇒ 请求级原子性改由 **C4** 断言(它本来的声明用途就是「history_ops 危险操作
    #     (amend/undo/revert)与 stage 并发」, 夹具是 fx_history, **amend 真跑得成**)。
    #     C4 现在断言: 响应的 subject 必须是自己提交的那条 message
    #     (2026-10-05 实测 16/24 = 66.7% 不符, 是真缺陷)。
    #     tree 变化那个数(实测 9/24 = 37.5%)在 C4 里**只测量不判据** ——
    #     `do_amend` 的 docstring 明写它「用当前 index 重写上次提交」, 那是文档写明的
    #     语义, 断言它就是假判据。
    #
    # ⚠ requests_interleaved / requests_interleavable **仍然照记**在上面的
    #   report(... "requests": summ) 与 RESULTS 里 —— 它们是诊断量, 不是判据。
    ck(summ["requests_interleavable"] > 0,
       "自检失败: requests_interleavable=0 —— 没有请求发过 2 条以上 git, "
       "该量恒 0 是**没测到**。%r" % summ)
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
