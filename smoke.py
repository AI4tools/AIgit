#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke.py —— aigit 端到端冒烟自检。

用途有两个,同一份代码:
  1. 交付前自检:确认 server.py 的关键接口真的能用。
  2. 交给使用者:换台机器 / 升了 Python / 改过 server.py 之后,跑一遍就知道有没有坏。

设计约束(与 server.py 保持一致):
  * 纯标准库,零第三方依赖。装不上 pip 的机器上也要能跑。
  * **不开窗口、不开浏览器**。本机同时跑着多条开发线,任何弹窗都会打断人干活。
  * 端到端走真实 HTTP,不是直接 import 调用。理由:走 HTTP 才测得到路由、
    JSON 编解码、HTTP 状态码这些真正会坏的东西。

安全边界:
  * 测试仓一律建在 %TEMP% 下,绝不用 C:\\poject\\ 下的任何真实仓库。
  * 每次跑都带 --sandbox,server.py 会把该目录整个删掉重建。
  * 子进程无论正常结束、断言失败还是异常中断,都保证回收(try/finally + atexit 双保险),
    不留僵尸进程。

退出码:
    0  全部通过(允许有 SKIP,SKIP 必须是环境原因,不是测试本身没写)
    1  有任何一项失败,或前置条件不满足(找不到 server.py / 起不来)

用法:
    python smoke.py              # 自动挑一个空闲端口
    python smoke.py --port 8777  # 指定端口
    python smoke.py -v           # 每步打印完整响应体
"""

import argparse
import atexit
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
WEB_INDEX = HERE / "web" / "index.html"

HTTP_TIMEOUT = 20.0      # 单个请求超时(秒)
START_TIMEOUT = 30.0     # 等 server 起来的最长时间(秒)
SHORT_BODY = 320         # 正常情况下打印的响应体长度上限
LOG_TAIL_LINES = 12      # 结束时打印的 server 日志行数

# 冲突标记:git 三方合并失败时写进工作区文件的原文。
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class Abort(Exception):
    """前置条件不满足,后面的步骤没有意义。收尾(停子进程 / 删演练仓)照常走。"""


# ------------------------------------------------------------------ 输出

def say(msg=""):
    """打印,顺带处理控制台编码:cp936 上万一碰到无法表示的字符也不许崩。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(msg.encode(enc, "replace").decode(enc, "replace"))
        except Exception:
            print(repr(msg))


def hr(title):
    say()
    say("=" * 74)
    say("  " + title)
    say("=" * 74)


def short(obj, n=SHORT_BODY):
    if isinstance(obj, (dict, list)):
        s = json.dumps(obj, ensure_ascii=False, sort_keys=False)
    else:
        s = str(obj)
    s = " ".join(s.split())          # 换行压平,免得刷屏
    return s if len(s) <= n else s[:n] + " ...(共 %d 字符)" % len(s)


# ------------------------------------------------------------------ 结果记录

class Report(object):
    """收集每一项的结论。不立刻退出 —— 全跑完再汇总,便于一次看到所有问题。"""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []
        self._n = 0

    def _bump(self):
        self._n += 1
        return "%02d" % self._n

    def check(self, name, cond, expected, actual, response=None):
        """cond 为真记 PASS,否则记 FAIL 并打印期望 / 实际。"""
        tag = self._bump()
        if cond:
            self.passed += 1
            say("[PASS] %s  %-34s -> %s" % (tag, name, short(actual)))
            if self.verbose and response is not None:
                say("       完整响应: %s" % short(response, 100000))
        else:
            self.failed += 1
            self.failures.append((tag, name, expected, actual))
            say("[FAIL] %s  %s" % (tag, name))
            say("       期望: %s" % expected)
            say("       实际: %s" % actual)
            if response is not None:
                say("       响应体: %s" % short(response, 100000))
        return bool(cond)

    def skip(self, name, reason):
        self.skipped += 1
        self._bump()
        say("[SKIP] ----  %-34s -- %s" % (name, reason))

    def note(self, name, actual):
        """只打印,不做判定。用于把中间状态摊开给人看。"""
        say("[INFO] ----  %-34s -- %s" % (name, short(actual)))


# ------------------------------------------------------------------ HTTP 客户端

class Client(object):
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, timeout=HTTP_TIMEOUT):
        """返回 (http_status, parsed)。HTTP 4xx/5xx 也算正常返回,不抛异常。"""
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, self._parse(raw)
        except urllib.error.HTTPError as e:
            # 4xx/5xx 是被测接口的正常应答,照样把 body 解出来
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return e.code, self._parse(raw)

    @staticmethod
    def _parse(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8", "replace")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def force_rmtree(path, attempts=6):
    """删掉一整个 git 仓库目录,返回是否真删干净了。

    为什么不直接用 shutil.rmtree:git 的 object 文件在 Windows 上是**只读**的
    (-ar---),os.unlink 碰到只读文件抛 PermissionError,rmtree 直接罢工。
    (git 自己删没事,是因为 git 内部会先清掉只读位。)
    所以这里自己走一遍:每个文件先 chmod 去掉只读位再删,目录自底向上删,
    整轮失败(比如刚 terminate 完还有句柄没放)就重试。
    """
    for i in range(attempts):
        if not path.exists():
            return True
        try:
            for root, dirs, files in os.walk(str(path), topdown=False):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        os.chmod(fp, stat.S_IREAD | stat.S_IWRITE)
                    except OSError:
                        pass
                    os.unlink(fp)
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(str(path))
        except OSError:
            time.sleep(0.3 * (i + 1))
            continue
        if not path.exists():
            return True
    return not path.exists()


# ------------------------------------------------------------------ server 子进程

class ServerProc(object):
    """起一个 server.py 子进程,并保证一定把它收掉。"""

    def __init__(self, port, root, log_path, python_exe=None):
        self.port = port
        self.root = root
        self.log_path = log_path
        self.python_exe = python_exe or sys.executable
        self.proc = None
        self.log_file = None
        atexit.register(self.stop)

    def start(self):
        self.log_file = open(str(self.log_path), "wb")
        cmd = [self.python_exe, str(SERVER),
               "--root", str(self.root),
               "--port", str(self.port),
               "--sandbox"]
        say("启动: %s" % " ".join(cmd))
        if os.name == "nt":
            flags = 0x00000200 | 0x00000100   # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            flags = 0
        self.proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdin=subprocess.DEVNULL,
            stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=flags)
        say("子进程 pid=%d  日志=%s" % (self.proc.pid, self.log_path))

    def wait_ready(self, timeout=START_TIMEOUT):
        """轮询 /api/health 直到应答。返回 (是否就绪, 最后一次的实际返回)。"""
        cli = Client(self.base)
        deadline = time.time() + timeout
        last = "尚未发起请求"
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False, "子进程已退出,returncode=%s" % self.proc.returncode
            try:
                status, obj = cli.call("GET", "/api/health", timeout=3.0)
                last = "%s %s" % (status, short(obj))
                if status == 200 and isinstance(obj, dict) and obj.get("ok"):
                    return True, last
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
            time.sleep(0.25)
        return False, last

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def log_text(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "(读不到日志)"

    def stop(self):
        """按 terminate -> kill -> taskkill 逐级升级,最后确认进程真的没了。"""
        p = self.proc
        if self.log_file is not None:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None
        if p is None:
            return
        if p.poll() is not None:
            self.proc = None
            return

        for step in ("terminate", "kill", "taskkill"):
            try:
                if step == "terminate":
                    p.terminate()
                elif step == "kill":
                    p.kill()
                else:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                say("       server 子进程 %s 后仍在,升级处理" % step)
            except Exception:
                pass

        alive = p.poll() is None
        if alive:
            say("       警告:子进程 pid=%d 仍未退出" % p.pid)
        else:
            say("       server 子进程已回收 (pid=%d, returncode=%s)"
                % (p.pid, p.returncode))
        self.proc = None


# ------------------------------------------------------------------ 断言小工具

def paths_of(items):
    return [i.get("path") for i in (items or [])]


def merge_and_check(rep, cli, sandbox, tag, file_hint, a, b, expect_clean):
    """切到分支 a,合并 b,然后按「文件里有没有冲突标记」判断结果。

    为什么必须先 checkout(a) 再 merge(b):生成器把两个分支都从 base 拉出来,
    再把 main 回滚到 base。所以在 main 上直接 merge(a) 是**快进**,git 压根
    不做内容合并,什么都验证不到。切到 a 之后 a 和 b 是兄弟分支,merge(b)
    才是真正的三方合并 —— 冲突与否才有意义。gen-notes.md 第 2 节记的就是
    这个坑:旧自测只合 A,于是"永远符合预期"。

    判定分两层,顺序不能反:
      1) 端点本身有没有跑通(响应里不能带 error 字段);
      2) 跑通之后,再看合并结果对不对。
    只做第 2 层会出假通过:端点挂掉时 ok=false + 文件里真有冲突标记,
    恰好和「对照组应当冲突」的期望吻合,于是一个坏掉的端点被记成 PASS。
    """
    st, obj = cli.call("POST", "/api/checkout", {"name": a})
    if not rep.check("%s 切到分支 A" % tag, st == 200 and obj.get("ok") is True,
                     "200 且 ok=true", "%s %s" % (st, short(obj)), obj):
        return False

    st, obj = cli.call("POST", "/api/merge", {"name": b})
    err = obj.get("error") if isinstance(obj, dict) else None
    endpoint_ok = err is None
    if not endpoint_ok:
        rep.note("诊断 %s" % tag, _diagnose_merge_error(err))
    rep.check("%s merge 端点自身不报错" % tag, endpoint_ok,
              "HTTP 200 且响应里没有 error 字段(说明真的执行到了 merge 逻辑)",
              "HTTP %s  error=%s" % (st, short(err)), obj)

    got_clean = endpoint_ok and obj.get("ok") is True and not obj.get("conflicted")

    # 真正下判据的依据是文件内容,不是接口自报的 ok。
    text, hit_file = read_any(sandbox, file_hint)
    has_marker = any(m in text for m in CONFLICT_MARKERS) if text else None

    if expect_clean:
        good = got_clean and has_marker is False
        rep.check("%s 合并 B 进 A(应干净)" % tag, good,
                  "ok=true、conflicted 为空、文件里没有冲突标记",
                  "ok=%s conflicted=%r 冲突标记=%s 命中文件=%r"
                  % (obj.get("ok"), obj.get("conflicted"), has_marker, hit_file), obj)
    else:
        good = endpoint_ok and (not got_clean) and bool(obj.get("conflicted")) \
            and has_marker is True
        rep.check("%s 合并 B 进 A(对照组:应冲突)" % tag, good,
                  "端点无异常、ok=false、conflicted 非空、文件里出现 <<<<<<< 冲突标记",
                  "端点ok=%s ok=%s conflicted=%r 冲突标记=%s 命中文件=%r"
                  % (endpoint_ok, obj.get("ok"), obj.get("conflicted"),
                     has_marker, hit_file), obj)

    # 收尾:冲突用 merge-abort,合并成功的用 reset,都回到 main。
    cli.call("POST", "/api/merge-abort")
    cli.call("POST", "/api/reset")
    st, obj = cli.call("POST", "/api/checkout", {"name": "main"})
    rep.check("%s 复位回 main" % tag, st == 200 and obj.get("branch") == "main",
              "200 且 branch=main", "%s %s" % (st, short(obj)), obj)
    return good


def _diagnose_merge_error(err):
    """把已知的 server.py 缺陷翻译成人能直接行动的一句话。"""
    if "returncode" in (err or ""):
        return ("已定位:server.py 里 Repo.merge() 写的是 p = self._run(...) 然后 p.returncode,"
                "但 _run() 返回的是 p.stdout(字符串),所以每一次 /api/merge 都会抛 "
                "AttributeError —— 干净合并根本走不到判 ok 那一步。"
                "缺陷在 server.py,不属于本线所有,只报告不修改。")
    return "未识别的错误原文:%s" % err


def read_any(sandbox, hints):
    """在工作区里挨个找提示到的文件,返回第一个命中的内容。

    hints 是文件名列表。gen.py 的 scenarios[].files 是列表;server.py 的兼容
    字段 branches[].file 是这些名字用 ", " 拼起来的字符串,调用前已拆开。
    adddel 场景有 fresh / doomed 两个文件,只按第一个找就够了 —— 冲突标记
    一定落在被改的那个文件上,而 conflict 场景只有一个文件。
    """
    for n in (hints or []):
        p = sandbox / n
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace"), p.name
        except OSError:
            pass
    return None, None


def files_changed(text):
    return [ln for ln in text.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")]


# ------------------------------------------------------------------ 主体

def run_scenarios(rep, cli, sandbox):
    """三类可合并场景 + 一档冲突对照。每档都用固定随机种子,结果可复现。

    四档的构造理由(细节见 gen-notes.md)在这里各留一句,方便对着失败信息判断
    是「生成器没造对」还是「合并端点坏了」——这两者的现象很像,别混。

    返回形状:优先 scenarios(带 files/verified 的详细结构),退回 branches
    (server.py 为老前端保留的兼容层,file 是逗号拼的字符串)。
    """
    kinds = [
        ("mergeable", True,  "可合并:A 改文件头部、B 改尾部,中间隔 >= 20 行未触碰"),
        ("unrelated", True,  "无关:A 改一个文件、B 改另一个,三方合并碰不到同一文件"),
        ("adddel",    True,  "增删:A 新增一个文件、B 删除另一个,不同路径所以干净"),
        ("conflict",  False, "冲突(对照组):A、B 改同一行,必须失败"),
    ]
    for kind, expect_clean, desc in kinds:
        tag = kind
        say()
        say("-- 场景 %s : %s" % (kind, desc))
        st, obj = cli.call("POST", "/api/gen",
                           {"kind": kind, "count": 1, "seed": 20260930 + len(kind)})
        if not rep.check("gen %s" % kind, st == 200 and obj.get("ok") is True,
                         "200 且 ok=true", "%s %s" % (st, short(obj)), obj):
            continue
        # 契约:gen.py 返回 scenarios(每场景带 files/branches/expect/verified),
        # server.py 另外把老前端要的形状映射回 branches=[{file,branches,expect}],
        # 其中 file 是 files 用 ", " 拼起来的。优先读 scenarios,读不到再退回
        # branches —— 这样 server.py 哪天把兼容层删了,这里也不会瞎猜。
        scenes = obj.get("scenarios")
        shape = "scenarios"
        if not isinstance(scenes, list) or not scenes:
            scenes = obj.get("branches")
            shape = "branches(兼容层)"
        if not rep.check("gen %s 返回场景对象" % kind,
                         isinstance(scenes, list) and len(scenes) >= 1
                         and isinstance(scenes[0], dict)
                         and isinstance(scenes[0].get("branches"), list),
                         "场景列表,元素里带 branches 字段",
                         "形状=%s 收到 %s 个元素" % (shape, len(scenes or [])), obj):
            continue

        names = []
        for sc in scenes:
            names.extend(sc.get("branches") or [])
        if not rep.check("gen %s 至少两个待合并分支" % kind, len(names) >= 2,
                         "展平后 >= 2 个分支名", "展平得到 %s" % short(names), obj):
            continue

        # 生成器自己就真跑过一次 merge 并把结论写进 verified(见 gen-notes.md 第 3 节)。
        # 这一条与下面的 HTTP 侧验证互为独立证据:一边是生成器自报,一边是我这边
        # 重新 checkout+merge+读文件内容。两边都过,才说明"可合并"是真的。
        unverified = [sc.get("id") for sc in scenes if sc.get("verified") is not True]
        rep.check("gen %s 生成器自检 verified" % kind, not unverified,
                  "每个场景 verified=true", "未通过的场景 id=%s" % short(unverified), obj)
        if "allVerified" in obj:
            rep.check("gen %s allVerified" % kind, obj.get("allVerified") is True,
                      "allVerified=true", "allVerified=%r" % obj.get("allVerified"), obj)

        first = scenes[0]
        # files 是列表;兼容层里是逗号拼的字符串。两种都要能取到文件名。
        files = first.get("files")
        if isinstance(files, str):
            files = [x.strip() for x in files.split(",") if x.strip()]
        if not files:
            files = [x.strip() for x in (first.get("file") or "").split(",")
                     if x.strip()]
        rep.note("gen %s 场景" % kind,
                 {"id": first.get("id"), "files": files, "branches": names,
                  "expect": first.get("expect")})
        merge_and_check(rep, cli, sandbox, tag, files,
                        names[0], names[1], expect_clean)


def run_stage_commit(rep, cli, sandbox):
    """stage -> diff -> commit -> log -> 干净。走完整条提交链路。"""
    hr("提交链路 stage / diff / commit")
    name = "smoke_edit.txt"
    p = sandbox / name
    p.write_text("smoke line 1\nsmoke line 2\n", encoding="utf-8", newline="\n")
    rep.note("手工改文件", str(p))

    st, obj = cli.call("POST", "/api/stage", {"files": [name]})
    rep.check("stage 暂存文件", st == 200 and obj.get("ok") is True
              and name in paths_of(obj.get("staged")),
              "200、ok=true、staged 含 %s" % name,
              "%s staged=%s" % (st, short(paths_of(obj.get("staged")))), obj)

    st, obj = cli.call("GET", "/api/status")
    rep.check("status 反映已暂存", st == 200 and obj.get("hasStaged") is True
              and name in paths_of(obj.get("staged")),
              "hasStaged=true 且 staged 含 %s" % name,
              "%s hasStaged=%s staged=%s"
              % (st, obj.get("hasStaged"), short(paths_of(obj.get("staged")))), obj)

    st, obj = cli.call("GET", "/api/diff?file=%s&staged=1" % name)
    added = files_changed(obj.get("diff", "")) if isinstance(obj, dict) else []
    rep.check("diff 看已暂存改动", st == 200 and len(added) >= 2,
              "200 且新增行 >= 2", "%s 新增行=%s" % (st, short(added)), obj)

    st, obj = cli.call("POST", "/api/commit", {"message": "smoke: commit"})
    rep.check("commit 提交", st == 200 and obj.get("ok") is True
              and bool((obj.get("output") or "").strip()),
              "200、ok=true、output 非空",
              "%s output=%s" % (st, short(obj.get("output"))), obj)

    st, obj = cli.call("GET", "/api/log?limit=5")
    log = obj.get("log") if isinstance(obj, dict) else None
    top = (log or [{}])[0].get("subject")
    rep.check("log 出现新提交", st == 200 and top == "smoke: commit",
              "首条 subject == 'smoke: commit'",
              "%s 首条 subject=%r 共 %s 条" % (st, top, len(log or [])), obj)

    st, obj = cli.call("GET", "/api/status")
    rep.check("提交后工作区干净", st == 200 and not obj.get("staged")
              and not obj.get("unstaged") and not obj.get("conflicted"),
              "staged/unstaged/conflicted 全空",
              "%s staged=%s unstaged=%s conflicted=%s"
              % (st, short(paths_of(obj.get("staged"))),
                 short(paths_of(obj.get("unstaged"))),
                 short(paths_of(obj.get("conflicted")))), obj)
    return name


def run_unstage_discard(rep, cli, sandbox, tracked):
    """unstage / discard / file 三视图。discard 会真删文件 —— 只在 %TEMP% 的演练仓里做。"""
    hr("撤销链路 unstage / discard / file")
    (sandbox / tracked).write_text("discarded content\n", encoding="utf-8", newline="\n")
    untracked = "smoke_untracked.txt"
    (sandbox / untracked).write_text("junk\n", encoding="utf-8", newline="\n")
    rep.note("造两处改动", "改了已跟踪 %s,新增未跟踪 %s" % (tracked, untracked))

    st, obj = cli.call("POST", "/api/stage", {"files": [tracked]})
    rep.check("stage 已跟踪文件", st == 200 and tracked in paths_of(obj.get("staged")),
              "staged 含 %s" % tracked,
              "%s staged=%s" % (st, short(paths_of(obj.get("staged")))), obj)

    st, obj = cli.call("POST", "/api/unstage", {"files": [tracked]})
    rep.check("unstage 退回暂存区", st == 200 and obj.get("ok") is True
              and tracked not in paths_of(obj.get("staged"))
              and tracked in paths_of(obj.get("unstaged")),
              "staged 不含 %s、unstaged 含 %s" % (tracked, tracked),
              "%s staged=%s unstaged=%s"
              % (st, short(paths_of(obj.get("staged"))),
                 short(paths_of(obj.get("unstaged")))), obj)

    st, obj = cli.call("POST", "/api/discard", {"files": [tracked, untracked]})
    rep.check("discard 丢弃改动", st == 200 and obj.get("ok") is True,
              "200 且 ok=true", "%s %s" % (st, short(obj)), obj)
    rep.check("discard 后文件已还原",
              (sandbox / tracked).is_file() and not (sandbox / untracked).exists(),
              "%s 存在且内容还原,%s 已删除" % (tracked, untracked),
              "%s exists=%s / %s exists=%s"
              % (tracked, (sandbox / tracked).is_file(),
                 untracked, (sandbox / untracked).exists()))

    st, obj = cli.call("GET", "/api/status")
    rep.check("discard 后工作区干净", st == 200 and not obj.get("staged")
              and not obj.get("unstaged"),
              "staged/unstaged 全空",
              "%s staged=%s unstaged=%s"
              % (st, short(paths_of(obj.get("staged"))),
                 short(paths_of(obj.get("unstaged")))), obj)

    st, obj = cli.call("GET", "/api/file?file=%s" % tracked)
    rep.check("file 左右分栏", st == 200 and obj.get("old") is not None
              and obj.get("new") is not None,
              "old 与 new 都非空",
              "%s old=%s new=%s" % (st, short(obj.get("old")), short(obj.get("new"))), obj)


def run_branches(rep, cli):
    hr("分支 branch / checkout")
    st, obj = cli.call("GET", "/api/branches")
    names = [b.get("name") for b in (obj.get("branches") or [])] if isinstance(obj, dict) else []
    rep.check("branches 列出分支", st == 200 and "main" in names,
              "200 且含 main", "%s 分支=%s" % (st, short(names)), obj)

    br = "smoke/tmp-branch"
    st, obj = cli.call("POST", "/api/branch", {"name": br})
    rep.check("新建分支", st == 200 and obj.get("ok") is True
              and obj.get("branch") == br,
              "200、ok=true、当前分支=%s" % br,
              "%s branch=%s" % (st, obj.get("branch")), obj)

    st, obj = cli.call("POST", "/api/branch", {"name": "bad name!"})
    rep.check("非法分支名被拒", st == 400 and obj.get("ok") is False
              and "分支名" in (obj.get("error") or ""),
              "400 且 error 提到分支名规则",
              "%s %s" % (st, short(obj)), obj)

    st, obj = cli.call("POST", "/api/checkout", {"name": "main"})
    rep.check("切回 main", st == 200 and obj.get("branch") == "main",
              "200 且 branch=main", "%s branch=%s" % (st, obj.get("branch")), obj)


def run_safety(rep, cli, sandbox):
    """路径越界与 shell 注入。这两条是这工具唯一能伤到人的地方,必须守住。"""
    hr("安全边界 路径越界 / 参数注入")
    escape = sandbox.parent / "aigit_smoke_escape.txt"
    existed = escape.exists()
    st, obj = cli.call("POST", "/api/stage", {"files": ["../aigit_smoke_escape.txt"]})
    rep.check("拒绝 .. 越界路径", st == 400 and obj.get("ok") is False
              and "越界" in (obj.get("error") or ""),
              "400 且 error 含「越界」", "%s %s" % (st, short(obj)), obj)
    rep.check("越界请求没有落盘",
              escape.exists() == existed,
              "%s 的存在性不变(False)" % escape.name,
              "exists=%s" % escape.exists())

    # ---- 名字里带 shell 元字符的**真实文件** ------------------------------
    #
    # ⚠ 这条断言是被重写过的,因为原来那条**测不到任何东西**。原写法:
    #     cli.call("POST", "/api/stage", {"files": ["a.txt; echo pwned > x"]})
    #     断言 st >= 400 and not (sandbox / "x").exists()
    # 两半都不成立,所以它出厂就是红的,而且红了也说明不了任何事:
    #   * `>` 是 NTFS 的**禁用**文件名字符, `a.txt; echo pwned > x` 在磁盘上根本
    #     造不出来 —— 实测 `OSError: [Errno 22] Invalid argument`。于是
    #     `x 不存在` 是**恒真**的:写什么都创建不出它,这一半是空断言。
    #   * `st >= 400` 要求服务端有一条仓库里查不到规格的策略。实测
    #     `POST /api/stage {"files":["nosuch.txt"]}` 同样返回 200 + staged=[],
    #     即「文件不存在」本来就不算 4xx(`..` 越界才 400,见上面那条)。
    #     所以 200 与契约**一致**,这一半是凭空加的严格性要求,不是缺陷。
    # ⇒ 原来那条不是「太严」,是**既测不到安全、也测不到功能**。改成下面这条:
    #   用**造得出来的**名字(Windows 合法:不含 `< > : " / \ | ? *`),
    #   建 4 个真实文件,让服务端去 stage,并断言三件事:
    #     (1) 200 —— 不要因为名字里带 ; & ` $ 就误伤正常功能(双向都要钉住);
    #     (2) 响应 staged 里出现的是**逐字节相同的字面名** —— git 拿到的是
    #         整个路径,而不是被 shell 拆开的那一段;
    #     (3) 仓目录清单没有多出任何文件 —— 确实没有第二个进程跑起来。
    # 若哪天服务端改成 shell=True + 字符串拼串,`git add "smoke; touch pwned"`
    # 会变成 `git add smoke` + `touch pwned`:(2) 少了字面名、(3) 多出 pwned,两条都红。
    meta = ["smoke; touch pwned", "smoke&&calc", "smoke`id`", "smoke$(id)"]
    for n in meta:
        (sandbox / n).write_text("payload %s\n" % n, encoding="utf-8", newline="\n")
    before = set(os.listdir(str(sandbox)))
    st, obj = cli.call("POST", "/api/stage", {"files": meta})
    got_staged = sorted(x.get("path") for x in (obj.get("staged") or [])
                        if isinstance(x, dict))
    added = sorted(set(os.listdir(str(sandbox))) - before)
    rep.check("含 shell 元字符的文件名进不了 shell",
              st == 200 and got_staged == sorted(meta) and not added,
              "200、staged 逐字节等于 %r、仓里不多出文件" % (sorted(meta),),
              "status=%s  staged=%r  仓里新增=%r  %s"
              % (st, got_staged, added, short(obj)), obj)
    rep.note("shell 元字符文件名实测",
             "status=%s staged=%r 仓里新增=%r" % (st, got_staged, added))

    st, obj = cli.call("GET", "/api/file?file=../../etc/passwd")
    rep.check("读接口也拒绝越界", st == 400 and "越界" in (obj.get("error") or ""),
              "400 且 error 含「越界」", "%s %s" % (st, short(obj)), obj)

    st, obj = cli.call("GET", "/api/nope")
    rep.check("未知接口回 404", st == 404 and obj.get("ok") is False,
              "404 且 ok=false", "%s %s" % (st, short(obj)), obj)


def run_frontend(rep, cli):
    """web/index.html 由前端那条线并行开发。文件不在就如实标 SKIP,不去猜。"""
    hr("前端首页 /")
    if not WEB_INDEX.is_file():
        rep.skip("GET /", "web/index.html 尚未落地(前端那条线的交付物),"
                          "本项与服务端逻辑无关,跳过")
        return
    st, obj = cli.call("GET", "/")
    text = obj if isinstance(obj, str) else ""
    rep.check("首页可打开", st == 200 and "<html" in text.lower(),
              "200 且是 HTML", "status=%s 长度=%d" % (st, len(text)), text[:200])


# ------------------------------------------------------------------ 入口

def main():
    ap = argparse.ArgumentParser(description="aigit 端到端冒烟自检")
    ap.add_argument("--port", type=int, default=0, help="server 端口,0 = 自动挑空闲端口")
    ap.add_argument("-v", "--verbose", action="store_true", help="每步打印完整响应体")
    ap.add_argument("--keep", action="store_true", help="保留演练仓(调试用),默认删除")
    a = ap.parse_args()

    if not SERVER.is_file():
        say("[FAIL] 找不到 server.py:%s" % SERVER)
        say("       smoke.py 必须和 server.py 放在同一个目录里。")
        return 1

    port = a.port or free_port()
    sandbox = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") \
        / ("aigit-smoke-%d" % os.getpid())
    log_path = sandbox.with_name(sandbox.name + ".log")

    rep = Report(verbose=a.verbose)
    srv = ServerProc(port, sandbox, log_path)

    hr("环境")
    say("  python      %s (%s)" % (sys.version.split()[0], sys.executable))
    say("  server.py   %s" % SERVER)
    say("  演练仓      %s" % sandbox)
    say("  端口        %d" % port)
    say("  web/index.html 存在? %s" % ("是" if WEB_INDEX.is_file() else "否"))

    say()
    say("注意:演练仓建在 TEMP 下(%s),不在任何真实仓库里。" % sandbox.parent)
    say("      本脚本不会打开浏览器,只打印 URL 供你自己决定要不要开。")

    try:
        srv.start()
        ready, detail = srv.wait_ready()
        if not rep.check("server 起来并应答 health", ready,
                         "%d 端口上 /api/health 返回 ok=true" % port,
                         detail):
            raise Abort()

        cli = Client(srv.base)

        hr("只读接口 health / status / branches / log")
        st, obj = cli.call("GET", "/api/health")
        rep.check("health", st == 200 and obj.get("ok") is True
                  and obj.get("isRepo") is True,
                  "200、ok=true、isRepo=true", "%s %s" % (st, short(obj)), obj)

        st, obj = cli.call("GET", "/api/status")
        rep.check("status 初始干净", st == 200 and obj.get("ok") is True
                  and obj.get("branch") == "main" and not obj.get("unstaged"),
                  "200、branch=main、unstaged 为空",
                  "%s %s" % (st, short(obj)), obj)

        st, obj = cli.call("GET", "/api/log?limit=3")
        rep.check("log 非空", st == 200 and len(obj.get("log") or []) >= 1,
                  "至少 1 条提交", "%s 条数=%s" % (st, len(obj.get("log") or [])), obj)

        run_scenarios(rep, cli, sandbox)
        tracked = run_stage_commit(rep, cli, sandbox)
        run_unstage_discard(rep, cli, sandbox, tracked)
        run_branches(rep, cli)
        run_safety(rep, cli, sandbox)
        run_frontend(rep, cli)

    except Abort:
        say()
        say("前置条件不满足,后续接口未测。收尾照常执行。")
    except KeyboardInterrupt:
        say()
        say("[FAIL] 被 Ctrl+C 中断。")
        rep.failed += 1
        rep.failures.append(("--", "整体", "跑完全部步骤", "Ctrl+C 中断"))
    except Exception as e:
        say()
        say("[FAIL] 冒烟脚本自身抛异常:%s: %s" % (type(e).__name__, e))
        import traceback
        traceback.print_exc()
        rep.failed += 1
        rep.failures.append(("--", "冒烟脚本", "不抛异常", "%s: %s" % (type(e).__name__, e)))
    finally:
        hr("收尾")
        srv.stop()
        if log_path.exists():
            say()
            say("server 日志末尾 %d 行:" % LOG_TAIL_LINES)
            for line in log_path.read_text(encoding="utf-8", errors="replace") \
                    .splitlines()[-LOG_TAIL_LINES:]:
                say("  | " + line)
        if not a.keep:
            removed = force_rmtree(sandbox)
            say("演练仓 %s %s"
                % (sandbox, "已删除" if removed
                   else "删除失败(可手工删:在 TEMP 下找 aigit-smoke-*)"))
            if log_path.exists():
                try:
                    os.unlink(str(log_path))
                    say("日志 %s 已删除" % log_path)
                except OSError as e:
                    say("日志 %s 删除失败:%s" % (log_path, e))
        else:
            say("演练仓 %s 按 --keep 保留" % sandbox)
            say("日志   %s" % log_path)

    hr("结论")
    say("  通过 %d    失败 %d    跳过 %d" % (rep.passed, rep.failed, rep.skipped))
    if rep.failures:
        say()
        say("  失败明细:")
        for tag, name, expected, actual in rep.failures:
            say("   * [%s] %s" % (tag, name))
            say("       期望 %s" % expected)
            say("       实际 %s" % actual)
        say()
        say("  退出码 1")
        return 1
    say()
    say("  全部通过,退出码 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
