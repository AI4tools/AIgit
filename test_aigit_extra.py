#!/usr/bin/env python3
"""aigit 无头补充测试套件 —— 只补 test_aigit.py 明确说「没测到」的那些洞。

这份文件存在的理由: doc/aigit-review.md §4 列了一批真实的覆盖空洞
(没有真符号链接、硬链接没测、大小写折叠只是推断、TOCTOU 没测、规模没测、
未出生 HEAD 没测、端口占用没测、ref 没穷举)。本文件逐条去补。

设计立场(和 test_aigit.py 一致, 但更严格):
    断言写的是**正确行为**, 不是当前行为。先假设尺子不准。
    造不出输入的测试一律 SKIP 并写清**为什么造不出**(贴实测错误码),
    绝不标 PASS —— 「SKIP 5 条但每条都有证据」比「全绿」可信。

硬约束:
    * 纯标准库, 不用 pytest, 不开浏览器/窗口。
    * 所有仓建在 %TEMP%\\aigit-test-extra-<pid>\\ 下, 绝不碰任何真实仓库。
    * 只读 server.py。发现 bug 只记录、不修(server.py 是 A1 线的地盘)。
    * 测试仓在 %TEMP% 下, 报告写到 doc/aigit-A3-extra-coverage.md。

分组:
    L  链接: 真符号链接 / 硬链接 / 目录联接(§4.1 §4.2)
    C  大小写折叠 + 8.3 短名(§4.3)
    F  其他没测的(§4.7): 未出生 HEAD / 端口占用 / ref 注入 / 规模 / TOCTOU

跑法:
    python test_aigit_extra.py           # 全部
    python test_aigit_extra.py -v        # 逐条显示细节
    python test_aigit_extra.py -k TOCTOU # 只跑名字含 TOCTOU 的
    python test_aigit_extra.py --list    # 只列测试名
    python test_aigit_extra.py --fast    # 跳过两条 scale 测试(约 2 分钟)
退出码 0 = 全过, 1 = 有 FAIL。

review §4 的哪一条 -> 哪条测试:
    §4.1 没有真正的文件符号链接   L-symlink-file-not-buildable(造不出就 SKIP+证据)
                                  L-junction-escape-explicit(退化方案, 实测)
    §4.2 硬链接逃逸「设计上不可检测」 L-hardlink-outside-content
                                  L-hardlink-git-add-does-not-touch-original
                                  L-hardlink-to-.git-config
                                  L-junction-inside-repo-git-follows(补充)
    §4.3 大小写折叠绕过(只是推断)   C-volume-case-sensitivity / C-casefold-traversal
                                  C-shortname-8dot3-bypass(补充: 8.3 短名)
    §4.6 TOCTOU 没测               L-junction-rmtree-top-vs-parent(原语)
                                  F-toctou-discard-parent-junction(真打)
                                  F-toctou-discard-self-junction(反向)
    §4.7 规模                      F-scale-100k-files / F-scale-stage-batch
    §4.7 未出生 HEAD                F-unborn-head-status
    §4.7 端口被占用                F-port-in-use
    §4.7 ref 没穷举 rev-spec        F-ref-revspec-exhaustive / F-ref-can-read-arb-rev
    §4.7 顺带发现                  F-dir-as-file-arg / F-stage-trails-git-literal
                                  F-api-contract-after-changes(基线 + server.py 指纹)
不在本文件范围(且说清为什么):
    §4.4 非 Windows 平台 —— 本机只有 Windows, 没有第二个平台可测。
    §4.5 前端         —— 无头不开浏览器; 由 doc/aigit-A2-web-audit.md 覆盖。
    §4.7 Ctrl+C / 信号、gen.py 的 CLI —— 需要交互 / 属于 gen.py, 不在本任务书内。
    §4.7 /api/reset 护栏 —— test_aigit.py 的 C-reset-is-unconditional 已覆盖。
"""

import argparse
import ctypes
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request

AIGIT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(AIGIT, "server.py")
# 临时仓根**必须带进程号 + 固定标识**。这不是洁癖: `main()` 开头会 force_rmtree(TESTROOT)
# 把整棵目录删掉, 而这个路径原来是全局固定的 `%TEMP%\aigit-test-extra` ——
# 于是两个 `test_aigit_extra.py` 同时跑, 后启动的那个会把前一个**正在用**的仓删光。
# 症状不是「测试失败」, 是**看起来像产品缺陷的假红**(同批实测, 见 test_history_ops.py):
#   * `fatal: not a git repository` / `fatal: unable to read tree <sha>`
#   * 上一遍还绿的用例这一遍变红(被误伤, 不是回归)
# 只避开 `test_aigit.py` 的 `%TEMP%\aigit-test` **不够** —— 那只挡住了"跨文件"互删,
# 挡不住"同文件两个进程"互删。必须落到 pid 这一级。
# 写法照 `test_aigit.py:54` 的既有约定: 固定标识 + pid。
TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-test-extra-%d" % os.getpid())

GIT_ENV = dict(os.environ)
GIT_ENV.update({
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": os.environ.get("USERPROFILE", os.environ.get("HOME", "")),
})


def tempfile_gettempdir():
    import tempfile as _t
    return _t.gettempdir()


# ════════════════════════════════════════════════════════ 测试登记

TESTS = []


def test(group, name, bug="", note=""):
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


# ════════════════════════════════════════════════════════ 清理(要能穿透 junction)

def _is_reparse_dir(p):
    """是不是 reparse point(目录联接 / 符号链接)。

    判据用 st_file_attributes 的 FILE_ATTRIBUTE_REPARSE_POINT(0x400) 而不是
    os.path.islink —— 实测 Python 3.11 的 ntpath.islink 对 `mklink /J` 造的
    目录联接返回 **False**, ntpath 也没有 _isjunction。所以 islink 在这里
    是个坏尺子, 自己的代码也得绕开它。
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


is_reparse_dir = _is_reparse_dir


def force_rmtree(path):
    """删一棵目录树。

    shutil.rmtree 遇到目录联接会直接抛 "Cannot call rmtree on a symbolic link"
    (Python 3.11, ntpath.islink 对 mklink /J 返回 False 但 rmtree 内部自己查
    reparse point)。所以先自顶向下把联接摘掉, 再删。
    """
    if not os.path.lexists(path):
        return
    for root, dirs, _files in os.walk(path, topdown=True):
        for d in list(dirs):
            full = os.path.join(root, d)
            if _is_reparse_dir(full):
                try:
                    os.rmdir(full)
                except OSError:
                    try:
                        os.unlink(full)
                    except OSError:
                        pass
                dirs.remove(d)
    def onerr(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    for _ in range(3):
        try:
            shutil.rmtree(path, onerror=onerr)
            return
        except Exception:
            time.sleep(0.05)


def force_rmtree_any(p):
    for q in (p, p + ".srvlog"):
        try:
            if os.path.isdir(q) and not os.path.islink(q):
                force_rmtree(q)
            elif os.path.lexists(q):
                os.unlink(q)
        except Exception:
            pass


# ════════════════════════════════════════════════════════ 链接构造原语

def mk_junction(link, target):
    """造目录联接。不需要特权。返回 True/False。"""
    if os.path.lexists(link):
        try:
            os.rmdir(link)
        except OSError:
            return False
    p = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, timeout=30)
    return os.path.lexists(link) and os.path.isdir(link) and p.returncode == 0


def mk_hardlink(link, target):
    """造硬链接。**不需要特权** —— 这是相对 os.symlink 的关键退化优势。"""
    try:
        os.unlink(link)
    except OSError:
        pass
    os.link(target, link)
    return True


# ---- 对手握持(确定性版的 TOCTOU 对手)---------------------------------
#
# 手段来自 E4 补测脚本 verify_e4b.py(收口清单 §2 E4 的复现脚本之一)。
# 它是第一个证明「握不住就拒绝」这条契约**可测**的原型, 这里只搬它需要的
# 那一小段 ctypes,不重写逻辑。
#
# 为什么需要它: F8 的竞态是概率性的(几十毫秒的窗口 vs 几千次换手),
# 「没打穿」不能区分「防线在」和「没赶上」。握着一个带 DELETE 权限的句柄
# 是**确定性**的对手形态 —— 服务端 CreateFileW 时必然撞 sharing violation。

_WIN32_DELETE = 0x00010000
_WIN32_GENERIC_READ = 0x80000000
_WIN32_SYNCHRONIZE = 0x00100000
_WIN32_SHARE_ALL = 0x7
_WIN32_OPEN_EXISTING = 3
_WIN32_FLAG_BACKUP_SEMANTICS = 0x02000000


def open_delete_handle(path):
    """对 `path` 开一个**带 DELETE 权限**的句柄并留着,模拟「对手已经在动它」。

    这是服务端 _pin_dir 的镜像反面: _pin_dir 用 share 模式**不含**
    FILE_SHARE_DELETE 去握, 所以本句柄要求 DELETE 权限时, 服务端必然握不住
    (Windows 的 sharing violation: 已有句柄要的权限, 新句柄的 share 没给)。
    造不出来就抛 Skip —— 不能默默跳过, 那等于「测不了」被记成「测过了」。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError) as e:
        raise Skip("拿不到 kernel32 (%s) —— 确定性握持段在本机测不了" % e)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    h = k32.CreateFileW(str(path),
                        _WIN32_DELETE | _WIN32_GENERIC_READ | _WIN32_SYNCHRONIZE,
                        _WIN32_SHARE_ALL, None, _WIN32_OPEN_EXISTING,
                        _WIN32_FLAG_BACKUP_SEMANTICS, None)
    if not h or h == ctypes.c_void_p(-1).value:
        raise Skip("对 %s 开 DELETE 句柄失败(WinError %s) —— 确定性握持段在本机测不了"
                   % (path, ctypes.get_last_error()))
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return (k32, h)


def close_handle(hk):
    """释放 open_delete_handle 拿到的句柄。失败也不许盖掉真正的结论。"""
    try:
        hk[0].CloseHandle(hk[1])
    except Exception:
        pass


def symlink_capability(ctx):
    """实测本机能不能造**真正的文件符号链接**。三条路全试。

    返回 (made: str|None, evidence: list[str])
    made 是 "os.symlink" / "mklink /D" / "msys-native" 之一; 都失败则 None。
    evidence 不管成败都填满 —— SKIP 的时候要靠它解释原因。
    """
    ev = []
    base = os.path.join(TESTROOT, "_capability")
    force_rmtree(base)
    os.makedirs(base, exist_ok=True)
    target = os.path.join(base, "outside.txt")
    with io.open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write("CAP-SECRET\n")
    link = os.path.join(base, "link.txt")

    # 1) os.symlink (原生, 需 SeCreateSymbolicLinkPrivilege)
    try:
        os.symlink(target, link)
        ev.append("os.symlink -> 成功, islink=%s" % os.path.islink(link))
        os.unlink(link)
        return "os.symlink", ev
    except OSError as e:
        ev.append("os.symlink -> WinError=%s %s"
                  % (getattr(e, "winerror", None), e))

    # 1b) 相对目标的符号链接(报告 §4.1 特别点名没测过的形态)
    try:
        os.symlink("outside.txt", link)
        ev.append("os.symlink(相对目标) -> 成功")
        os.unlink(link)
        return "os.symlink", ev
    except OSError as e:
        ev.append("os.symlink(相对目标) -> WinError=%s %s"
                  % (getattr(e, "winerror", None), e))

    # 2) mklink /D (文件符号链接, 同样需特权)
    p = subprocess.run(["cmd", "/c", "mklink", "/D", link, target],
                       capture_output=True, timeout=30)
    if os.path.lexists(link):
        ev.append("mklink /D -> 成功, islink=%s" % os.path.islink(link))
        return "mklink /D", ev
    ev.append("mklink /D -> rc=%s %s"
              % (p.returncode, (p.stderr or b"").decode("utf-8", "replace").strip()))

    # 3) Git-Bash / MSYS2 的 ln -s。MSYS 默认 winsymlinks:sys 只是写一个
    #    带 "!<symlink>" 头的普通文件; nativestrict 才调真正的 SymlinkPrivilege。
    for ln in _find_msys_ln(ctx):
        for mode in ("", "winsymlinks:nativestrict"):
            try:
                if os.path.lexists(link):
                    os.unlink(link)
            except OSError:
                pass
            env = dict(os.environ)
            if mode:
                env["MSYS"] = mode
            p = subprocess.run([ln, "-s", target, link], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               env=env, timeout=30)
            if os.path.lexists(link):
                isln = os.path.islink(link)
                ev.append("MSYS ln -s (MSYS=%r) -> 造出文件, islink=%s %s"
                          % (mode, isln,
                             "" if isln else "(只是普通文件, 不是真符号链接)"))
                if isln:
                    return "msys-native", ev
                try:
                    os.unlink(link)
                except OSError:
                    pass
            else:
                ev.append("MSYS ln -s (MSYS=%r) -> rc=%s %s"
                          % (mode, p.returncode,
                             (p.stderr or "").strip()[:90]))
    force_rmtree(base)
    return None, ev


def _find_msys_ln(ctx):
    """找 Git for Windows 里的 ln.exe。只用本机已有的 git, 不装任何东西。"""
    found = []
    g = shutil.which("git") or ""
    for base in {os.path.dirname(g), r"C:\Program Files\Git",
                 r"C:\Program Files (x86)\Git", r"D:\cangku\Git"}:
        if not base:
            continue
        for rel in (r"usr\bin\ln.exe", r"bin\ln.exe"):
            p = os.path.join(base, rel)
            if os.path.isfile(p) and p not in found:
                found.append(p)
                ctx.note("找到 MSYS ln: %s" % p)
    return found


def devmode_evidence():
    """开发者模式 / 特权 的实测证据。给 SKIP 提供可核查的原因。"""
    out = []
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock")
        try:
            v = winreg.QueryValueEx(k, "AllowDevelopmentWithoutDevLicense")[0]
            out.append("HKLM AppModelUnlock\\AllowDevelopmentWithoutDevLicense=%r" % v)
        except FileNotFoundError:
            out.append("HKLM AppModelUnlock 键存在但无 AllowDevelopmentWithoutDevLicense "
                       "值(开发者模式未开启)")
        winreg.CloseKey(k)
    except Exception as e:
        out.append("读 AppModelUnlock 失败: %s: %s" % (type(e).__name__, e))
    p = subprocess.run(["whoami", "/priv"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    txt = p.stdout or ""
    if "SeCreateSymbolicLinkPrivilege" in txt:
        out.append("whoami /priv: 令牌里有 SeCreateSymbolicLinkPrivilege")
    else:
        out.append("whoami /priv: 令牌里**没有** SeCreateSymbolicLinkPrivilege")
    try:
        import ctypes as _c
        admin = bool(_c.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = None
    out.append("当前是管理员: %s" % admin)
    return out


# ════════════════════════════════════════════════════════ 测试仓 + 无头服务

class TestRepo:
    def __init__(self, name):
        self.dir = os.path.join(TESTROOT, name)
        force_rmtree(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "aigit-extra-test@localhost")
        self.git("config", "user.name", "aigit-extra-test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, **kw):
        p = subprocess.run(["git", "-C", self.dir] + list(args),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=180)
        if kw.get("check") and p.returncode != 0:
            raise RuntimeError("git %s 失败: %s"
                               % (" ".join(args), (p.stderr or p.stdout).strip()))
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

    def abspath(self, rel):
        return os.path.join(self.dir, rel.replace("/", os.sep))

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-m", msg)

    def server(self):
        return HeadlessServer(self)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HeadlessServer:
    def __init__(self, repo):
        self.repo = repo
        self.log = repo.dir + ".srvlog"
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

    @staticmethod
    def _url(port, path):
        return "http://127.0.0.1:%d%s" % (port, path)

    def _raw(self, port, method, path, body=None, timeout=180):
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(self._url(port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def raw(self, method, path, body=None, **kw):
        return self._raw(self.port, method, path, body, **kw)

    def jget(self, path, **kw):
        code, txt = self.raw("GET", path, **kw)
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("GET %s 返回非 JSON (code=%s): %r" % (path, code, txt[:200]))

    def jpost(self, path, obj, **kw):
        code, txt = self.raw("POST", path, json.dumps(obj), **kw)
        try:
            return code, json.loads(txt)
        except Exception:
            raise Fail("POST %s 返回非 JSON (code=%s): %r" % (path, code, txt[:200]))

    def alive(self):
        code, j = self.jget("/api/health")
        return code == 200 and j.get("ok") is True

    def pid(self):
        return self.proc.pid if self.proc else None

    def peak_mb(self):
        return peak_working_set_mb(self.pid())

    def mem_mb(self):
        """(峰值工作集, 当前工作集), 单位 MB。"""
        return proc_mem_mb(self.pid())

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def peak_working_set_mb(pid):
    """PeakWorkingSetSize, 纯 ctypes 调 psapi —— 没有 psutil, 也不想装。

    只返回峰值。**注意**: 实测这个值会往下掉(内核裁剪工作集之后
    Process->PeakWorkingSet 会跟着回落), 所以它不是单调的, 只能当采样值看。
    要看当前占用用 proc_mem_mb()[1]。
    """
    return proc_mem_mb(pid)[0]


class _PMC(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS, 字段布局照抄 winbase.h。

    **前两个字段是 DWORD(4 字节), 不是指针宽度**, 从第 3 个起才是 SIZE_T。
    全部写成 c_size_t 会把后面整体挪 8 字节 —— 于是「峰值」读到的是
    WorkingSetSize、「当前」读到的是 QuotaPeakPagedPoolUsage(约 0.1 MB)。
    数字看着还挺像那么回事, 但全是错的: 第一版就踩了这个坑, 报出去的
    「峰值 41.8 MB」其实是当前工作集(所以它会往下掉 —— 当时我误判成
    内核裁剪, 其实是读错字段)。下面三条 assert 在 import 时就验布局, 错了立刻炸。
    """

    _fields_ = [("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong)] + [
        (n, ctypes.c_size_t) for n in (
            "PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage")]


assert ctypes.sizeof(_PMC) == 72, ("PROCESS_MEMORY_COUNTERS sizeof=%d (应 72)"
                                  % ctypes.sizeof(_PMC))
assert _PMC.PeakWorkingSetSize.offset == 8, ("PeakWorkingSetSize offset=%d (应 8)"
                                            % _PMC.PeakWorkingSetSize.offset)
assert _PMC.WorkingSetSize.offset == 16, ("WorkingSetSize offset=%d (应 16)"
                                          % _PMC.WorkingSetSize.offset)


def proc_mem_mb(pid):
    """返回 (PeakWorkingSetSize, WorkingSetSize), 单位 MB; 失败给 nan。"""
    if not pid:
        return float("nan"), float("nan")
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_uint]
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000 | 0x0400, False, int(pid))
        if not h:
            return float("nan"), float("nan")
        ok = psapi.GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(c), c.cb)
        k.CloseHandle(ctypes.c_void_p(h))
        if not ok:
            return float("nan"), float("nan")
        return c.PeakWorkingSetSize / 1048576.0, c.WorkingSetSize / 1048576.0
    except Exception:
        return float("nan"), float("nan")


def read_text(p):
    """读一个可能已经被删掉的文件的文本。**关掉句柄** —— 本文件里
    到处都在 Windows 上跟只读/被删的文件较劲,漏一个 close 就多一个
    WinError 32 的来源。文件不在就返回明确的标记,而不是抛。"""
    try:
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        return "<读不到:%s>" % e


def outside_marker(tag, sub="_outside"):
    """在仓库外放哨兵。越界成功 = 哨兵被删/被改。"""
    base = os.path.join(TESTROOT, sub)
    os.makedirs(base, exist_ok=True)
    p = os.path.join(base, tag + ".canary")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("CANARY:" + tag)
    return p


def outside_intact(p):
    if not os.path.exists(p):
        return False
    tag = os.path.basename(p).rsplit(".", 1)[0]
    with io.open(p, "r", encoding="utf-8") as f:
        return f.read() == "CANARY:" + tag


def q(s):
    return urllib.parse.quote(s, safe="")


# ════════════════════════════════════════════════════════ L 组:链接(§4.1 §4.2)

@test("L", "symlink-file-not-buildable", bug="L1-§4.1")
def t_l1(ctx):
    """L1 §4.1:真正的**文件符号链接**在本机造不出来 —— 造不出就 SKIP 并给证据。

    先说清楚:这不是「测不了」的空话。本测试穷尽三条造法(原生 os.symlink /
    mklink /D / MSYS ln -s 含 nativestrict),并把每一条的实测错误贴出来,
    再加上开发者模式注册表值 + whoami /priv 里到底有没有那个特权。
    只要任何一条造得出真符号链接, 本测试就**继续跑真正的越界断言**,
    而不是 SKIP —— 否则哪天环境变了就会静悄悄退化成假通过。
    """
    made, ev = symlink_capability(ctx)
    for line in ev:
        ctx.note("  造法: " + line)
    for line in devmode_evidence():
        ctx.note("  环境: " + line)

    outside = os.path.join(TESTROOT, "_outside_target")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    secret = outside_marker("l1")

    if made:
        ctx.note("造出了真符号链接(%s) -> 继续做越界断言" % made)
        link = ctx.repo.abspath("link.txt")
        try:
            os.symlink(secret, link)
        except OSError:
            ctx.note("os.symlink 这次又失败了, 走已经验证过能造的那条")
        if not os.path.islink(link):
            raise Skip("能力探测说有 %s 但实际建出来不是符号链接, 判定逻辑有问题" % made)
        # pathlib 必须跟随它, 否则后面的断言测不出东西
        import pathlib
        rp = str(pathlib.Path(link).resolve())
        follows = os.path.normcase(os.path.realpath(secret)) in os.path.normcase(rp)
        ctx.note("  pathlib.resolve -> %s  跟随=%s"
                 % (rp.replace(ctx.repo.dir, "<ROOT>"), follows))
        ck(follows, "造出来的符号链接 pathlib 不跟随, 这条测不出东西")
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": ["link.txt"],
                                            "confirmed": True}))
        ctx.note("  discard link.txt -> %s %r" % (code, txt[:90]))
        ck(outside_intact(secret), "仓外哨兵被删 —— 符号链接越界成功")
        code, txt = ctx.srv.raw("GET", "/api/file?file=" + q("link.txt"))
        ck("CANARY:l1" not in txt, "/api/file 读出了仓外内容")
        ck_eq(code, 400, "符号链接越界期望 400")
        return

    raise Skip(
        "本机造不出真正的文件符号链接(三条造法全失败, 见 notes), "
        "§4.1 的「仓内文件符号链接能不能读出仓外内容」实测不了。\n"
        "        退化方案: L2 用硬链接(不需要特权)覆盖了「仓内条目指向仓外 inode」"
        "这一类; L5 用目录联接覆盖「仓内目录指向仓外」。\n"
        "        仍然空着的是: **指向相对目标的文件符号链接**, 以及符号链接与"
        "硬链接在 relative_to 上的行为差异。")


@test("L", "hardlink-outside-content", bug="L2-§4.2")
def t_l2(ctx):
    """L2 §4.2:仓内硬链接指向仓外 inode —— **这是可测的**, 报告说「测不了」是错的。

    硬链接不需要特权(os.link / mklink /H 都能建), 所以 §4.2 那条「没测」
    是可以补上的。

    断言分三档, 别混:
      1. resolve_in_root **必须**放行(硬链接和普通文件在设计上无法区分,
         这是事实, 不是漏洞) —— 但必须**记录**下来, 因为它决定了后面两条;
      2. /api/file **读得出**硬链接的内容 -> 这是「仓内文件的内容就是它自己的内容」,
         不是越界(攻击者要造硬链接必须已经有本机写权限)。所以这条只记录不断言;
      3. **真正的不变量**: discard 一个仓内硬链接**不得**删掉仓外那个文件。
         os.unlink 只减链接计数, 仓外文件必须完好 —— 这条是硬断言。
    """
    r = ctx.repo
    # 先有 HEAD: 否则每个 POST 都会因为 F1(未出生 HEAD) 先回 400,
    # 而**副作用已经发生**(见 F1), 于是「响应是失败」和「操作没做」分不开。
    r.write("seed.txt", "seed\n")
    r.commit("seed")
    outside = os.path.join(TESTROOT, "_outside_hl")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    secret = outside_marker("l2", sub="_outside_hl")
    with io.open(secret, "w", encoding="utf-8") as f:
        f.write("CANARY:l2")

    link = r.abspath("inrepo.txt")
    ok = mk_hardlink(link, secret)
    ck(ok, "os.link 失败 —— 硬链接 fallback 在本机不可用")
    st_link = os.stat(link)
    st_sec = os.stat(secret)
    same = st_link.st_ino == st_sec.st_ino
    ctx.note("硬链接建好: 同一 inode=%s  nlink=%d  islink(仓内那份)=%s"
             % (same, st_link.st_nlink, os.path.islink(link)))
    ck(same, "造出来的不是硬链接(st_ino 不同) —— 这条测不出东西")

    # (1) resolve_in_root 会放行 —— 记录, 并把后果说清楚
    import pathlib
    rp = pathlib.Path(r.dir).resolve()
    resolved = str((rp / "inrepo.txt").resolve()).replace(str(rp), "<ROOT>")
    ctx.note("resolve_in_root('inrepo.txt') -> %s  (它就是仓内一个普通文件)" % resolved)

    # (2) /api/file 读硬链接 -> 能读出来。这不是越界(内容是攻击者自己放进去的),
    #     但它是「hardlink 逃逸确实存在」的直接实测证据。
    code, j = ctx.srv.jget("/api/file?file=inrepo.txt")
    ctx.note("GET /api/file?file=inrepo.txt -> %s new=%r" % (code, (j.get("new") or "")[:30]))
    ck_eq(code, 200, "读仓内硬链接应当 200(它就是仓内一个普通文件)")
    ck("CANARY:l2" in (j.get("new") or ""),
       "读不到硬链接内容 —— 尺子不准(硬链接应当和普通文件无异)")
    ctx.note("  => 仓内硬链接的内容可被 /api/file 读出。威胁模型判断: "
             "造硬链接需要对本机有写权限, 属于「用户自己的仓」范围, 低危。"
             "这是设计事实 + 实测, 不是 bug。")

    # (3) 硬不变量: discard 不得牵连仓外
    code, txt = ctx.srv.raw("POST", "/api/discard",
                            json.dumps({"files": ["inrepo.txt"], "confirmed": True}))
    ctx.note("POST /api/discard {'files':['inrepo.txt']} -> %s %s"
             % (code, txt[:70].replace("\n", " ")))
    ck_eq(code, 200, "discard 一个未跟踪的仓内文件应 200")
    ck(not os.path.exists(link), "仓内硬链接没被删")
    ck(outside_intact(secret),
       "仓外哨兵内容变了或没了 —— discard 一个仓内硬链接竟然牵连到了仓外文件")
    ctx.note("  => discard 后仓外哨兵完好(nlink 从 2 减到 1, unlink 只摘目录项)")
    ck(ctx.srv.alive(), "discard 之后服务不健康")


@test("L", "hardlink-git-add-does-not-touch-original", bug="L3-§4.2")
def t_l3(ctx):
    """L3 §4.2 的另一半:把**仓外文件硬链接成仓内文件**再让 aigit 动它,
    仓外那份的内容会不会被改。

    这是 L2 的镜像方向:  L2 是「仓外 -> 仓内」(读得到 / 删不掉),
    这条是「仓内文件被 aigit 操作之后仓外那份还在不在、内容有没有被动」。

    真正决定答案的是 git 写工作区文件的两种方式:
      * 原地改(truncate + write) -> 顺着硬链接把仓外那份也改了
        —— 用户在仓外放的文件被 aigit 的「丢弃更改」静默还原了;
      * 写临时文件 + rename      -> 只换掉仓内那个目录项, 仓外那份原样保留。

    **测法上的坑(第一版就踩了, 结论是假的, 记在这里)**:
    硬链接两端是同一个 inode, 谁写都一起变。所以要让工作区变脏, 唯一不改
    inode 的入口是**从仓外那个路径写**; 如果测试自己 open(仓内路径,'w') 去写,
    改的是共享 inode, 仓外当然跟着变, 最后根本分不清是 git 干的还是测试干的。
    下面 (C) 段的判据只覆盖 git 自己写的那一次, 并且先记下改之前的仓外内容。
    """
    r = ctx.repo
    # 先有一个 HEAD: 空仓时每个 POST 都会因为 F1(未出生 HEAD) 返回 400,
    # 那会把本条真正要测的东西盖掉。
    r.write("seed.txt", "seed\n")
    r.commit("seed")
    outside = os.path.join(TESTROOT, "_outside_hl3")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    outside_file = os.path.join(outside, "shared.txt")

    def rd(p):
        with io.open(p, encoding="utf-8") as f:
            return f.read()

    with io.open(outside_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("V1-OUTSIDE\n")
    link = r.abspath("shared.txt")
    ck(mk_hardlink(link, outside_file), "os.link 失败")
    ino0 = os.stat(link).st_ino
    ck(ino0 == os.stat(outside_file).st_ino, "硬链接没建成")
    ctx.note("初始: 仓内读到 %r / 仓外读到 %r  同一 inode=%d  nlink=%d"
             % (r.read("shared.txt"), rd(outside_file),
                ino0, os.stat(link).st_nlink))

    # (A) stage + commit —— 这两步只该**读**工作区文件, 不该写。
    code, _j = ctx.srv.jpost("/api/stage", {"files": ["shared.txt"]})
    ck_eq(code, 200, "stage 硬链接")
    code, _j = ctx.srv.jpost("/api/commit", {"message": "hardlink commit"})
    ck_eq(code, 200, "commit 硬链接")
    ck(os.path.exists(outside_file),
       "stage/commit 把仓外文件搞没了 —— 尺子/环境不对")
    ck_eq(rd(outside_file), "V1-OUTSIDE\n",
          "stage/commit 顺着硬链接改了仓外文件的内容")
    ck_eq(os.stat(outside_file).st_ino, ino0,
          "stage/commit 之后仓外的 inode 变了(git 替换了文件)")
    ck_eq(r.out("show", "HEAD:shared.txt"), "V1-OUTSIDE\n",
          "commit 存进对象库的内容不对 —— 尺子坏了")
    ctx.note("  => A(stage+commit): 仓外那份一字未动, inode 未变。")

    # (B) 从**仓外**路径写, 把工作区弄脏 —— 同一 inode, 仓内看到的内容跟着变。
    with io.open(outside_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("V2-DIRTY\n")
    ctx.note("从仓外路径改成 V2-DIRTY 之后: 仓内工作区读到 %r, git status=%r"
             % (r.read("shared.txt"), r.out("status", "--porcelain").strip()))
    ck_eq(r.read("shared.txt"), "V2-DIRTY\n",
          "工作区读到的不是同一个 inode 的内容 —— 硬链接没建对, 这条测不出东西")

    # (C) 唯一会**写**工作区文件的 aigit 操作: 丢弃一个已修改的已跟踪文件
    #     (discard 对 tracked 走 `git --literal-pathspecs checkout -- <rel>`)。
    before_out = rd(outside_file)
    before_ino = os.stat(outside_file).st_ino
    code, txt = ctx.srv.raw("POST", "/api/discard",
                            json.dumps({"files": ["shared.txt"], "confirmed": True}))
    ctx.note("POST /api/discard ['shared.txt'] -> %s %s"
             % (code, txt[:70].replace("\n", " ")))
    ck_eq(code, 200, "discard 一个已修改的已跟踪文件应 200")
    ck_eq(r.read("shared.txt"), "V1-OUTSIDE\n",
          "discard 之后工作区没回到 HEAD —— 尺子坏了")
    after_ino = os.stat(outside_file).st_ino if os.path.exists(outside_file) else -1
    after_out = rd(outside_file) if os.path.exists(outside_file) else None
    ctx.note("  => C(discard): 改之前仓外 = %r (inode %d), 改之后仓外 = %r (inode %d)"
             % (before_out, before_ino, after_out, after_ino))
    # 正确行为: 「丢弃工作区改动」是按**路径**做的操作, 不该改到别的路径上
    # (git checkout 的实现就是 写临时文件 + rename, 所以这条应该成立)。
    ck(os.path.exists(outside_file),
       "discard 一个仓内硬链接, 仓外那个文件竟然没了")
    ck_eq(after_out, before_out,
          "discard 顺着硬链接把仓外文件的内容改了: %r -> %r —— "
          "git 原地改写了共享 inode, 用户在仓外的文件被静默还原"
          % (before_out, after_out))
    ck_eq(after_ino, before_ino,
          "discard 把仓外文件的 inode 换了(git 写临时文件 + rename 了仓内那个目录项)")
    ctx.note("  => 实测 git 对工作区文件是「写临时文件 + rename」, "
             "硬链接在仓内这一侧被断开, 仓外那份一字未动。")


@test("L", "hardlink-to-.git-config", bug="L4-§4.2")
def t_l4(ctx):
    """L4 硬链接的真正危险面:**指向 .git 内部的硬链接**。

    server.py 的 resolve_in_root 拒了 `.git`(parts[0]=='.git')。这条测:
      * 走 file 参数的接口(.git/config)被拒;
      * 但**硬链接把 .git/config 换了个名字放到工作区**, resolve 看不出来
        —— 判它是不是「仓内普通文件」, 那就是普通文件。stage/commit 它会怎样?
    这条是硬断言: 即使被当成普通文件, 也不能把仓库搞坏。
    """
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    cfg = r.abspath(os.path.join(".git", "config"))
    leak = r.abspath("stolen_config.txt")
    mk_hardlink(leak, cfg)
    ctx.note(".git/config 被硬链接成工作区文件 stolen_config.txt, "
             "同一 inode=%s" % (os.stat(leak).st_ino == os.stat(cfg).st_ino))
    ctx.note("仓外能不能通过它读到 .git/config? /api/file 会返回配置内容")

    code, j = ctx.srv.jget("/api/file?file=" + q(".git/config"))
    ctx.note("GET /api/file?file=.git/config -> %s %r" % (code, str(j)[:100]))
    ck_eq(code, 400, ".git/config 必须被拒")

    # 硬链接换名后走 API: 能读(它就是工作区一个普通文件), 但 discard 不能毁仓
    code, j = ctx.srv.jget("/api/file?file=stolen_config.txt")
    ctx.note("GET /api/file?file=stolen_config.txt -> %s (读到 %d 字节配置)"
             % (code, len(j.get("new") or "")))
    ck_eq(code, 200, "换名后的硬链接就是工作区文件, 应当能读")

    code, txt = ctx.srv.raw("POST", "/api/discard",
                            json.dumps({"files": ["stolen_config.txt"],
                                        "confirmed": True}))
    ctx.note("discard stolen_config.txt -> %s %s" % (code, txt[:70].replace("\n", " ")))
    ck_eq(code, 200, "discard 一个工作区未跟踪文件应 200")
    ck(not os.path.exists(leak), "换名后的硬链接没被删")
    ck(os.path.exists(cfg), "discard 硬链接把 .git/config 一起弄没了 —— 仓库被毁")
    ck_eq(r.out("status", "--porcelain").strip(), "",
          "discard 之后 git status 不干净")
    ck(ctx.srv.alive(), "服务不健康")
    ctx.note("  => discard 只摘了工作区那个目录项, .git/config 与 inode 同在。")


@test("L", "junction-escape-explicit", bug="L5-§4.1")
def t_l5(ctx):
    """L5 §4.1 的退化方案**显式验证**:目录联接确实被 resolve() 跟随, 所以它挡得住。

    上一条 L1 造不出文件符号链接, 唯一能造的是目录联接。这条不假装它是符号链接,
    而是明确量出联接这条路的实际行为, 并把「为什么它挡得住」的机制钉死:
    resolve() 在 relative_to **之前**就把联接解析掉了。
    """
    r = ctx.repo
    outside = os.path.join(TESTROOT, "_outside_junction")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    secret = outside_marker("l5", sub="_outside_junction")
    with io.open(secret, "w", encoding="utf-8") as f:
        f.write("CANARY:l5")

    link = r.abspath("escape")
    if not mk_junction(link, outside):
        raise Skip("mklink /J 也造不出来(需要同卷), 本机退化方案失效")
    import pathlib
    resolved = str(pathlib.Path(link).resolve())
    follows = os.path.normcase(os.path.realpath(outside)) in os.path.normcase(resolved)
    ctx.note("联接 %s -> %s" % (link.replace(r.dir, "<ROOT>"),
                                resolved.replace(r.dir, "<ROOT>")))
    ctx.note("pathlib.resolve 是否跟随联接: %s" % follows)
    ck(follows, "pathlib 不跟随目录联接 —— 那 resolve_in_root 也挡不住, "
                "B6/B6-crit 的结论就是空的")

    for files in (["escape/secret.txt"], ["escape"], ["escape/"],
                  ["./escape/secret.txt"], ["escape/../escape/secret.txt"]):
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": files, "confirmed": True}))
        ctx.note("discard %-26s -> %-4s %s"
                 % (files, code, txt[:74].replace("\n", " ")))
        ck_eq(code, 400, "联接越界 %r 期望 400" % (files,))
        ck(outside_intact(secret), "仓外哨兵被动了 —— 联接越界成功")
    for files in (["escape/secret.txt"], ["escape"]):
        code, _txt = ctx.srv.raw("POST", "/api/stage", json.dumps({"files": files}))
        ck_eq(code, 400, "stage 联接越界 %r 期望 400" % (files,))
    for ep in ("/api/file?file=" + q("escape/secret.txt"),
               "/api/diff?file=" + q("escape/secret.txt")):
        code, txt = ctx.srv.raw("GET", ep)
        ck("CANARY:l5" not in txt, "GET %s 读出了仓外内容" % ep)
        ck_eq(code, 400, "GET %s 期望 400" % ep)
    ck(ctx.srv.alive(), "服务不健康")
    ctx.note("  => 联接这条路 resolve_in_root 挡得住。符号链接这条路本机没测到。")


@test("L", "junction-rmtree-top-vs-parent", bug="L6-TOCTOU原语")
def t_l6(ctx):
    """L6 TOCTOU 的**文件系统原语**:shutil.rmtree 跟不跟随联接?

    这一条不测 aigit, 测的是**丢掉 TOCTOU 能不能成立所依赖的那个前提**。
    报告 §4.6 说「resolve_in_root 校验和破坏性动作是两步」。这条把第二步
    (shutil.rmtree / Path.unlink) 的实际语义量出来:

      * ap 本身是联接  -> rmtree 拒绝(Cannot call rmtree on a symbolic link)
      * ap 的**父目录**是联接 -> rmtree **不检查**, 直接删到联接外面去

    第二个是重点: 它意味着只要校验完到动手之间有人把父目录换成联接,
    就能越界删除。F 组的 TOCTOU 那两条就是冲这个去的。
    """
    import pathlib
    base = os.path.join(TESTROOT, "_rmtree_primitive")
    force_rmtree(base)
    os.makedirs(base, exist_ok=True)

    outside = os.path.join(base, "outside")
    os.makedirs(os.path.join(outside, "victim"))
    canaries = []
    for i in range(20):
        p = os.path.join(outside, "victim", "c%02d.txt" % i)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("CANARY%02d\n" % i)
        canaries.append(p)

    # 情形 A: ap 自己就是联接
    top = os.path.join(base, "toplevel")
    os.makedirs(os.path.join(base, "real_a"))
    mk_junction(top, os.path.join(outside, "victim"))
    err_a = None
    try:
        shutil.rmtree(top)
    except Exception as e:
        err_a = "%s: %s" % (type(e).__name__, e)
    alive_a = sum(1 for c in canaries if os.path.exists(c))
    ctx.note("[A] ap 自己是联接: rmtree -> %s   仓外哨兵存活 %d/%d"
             % (err_a or "(没报错)", alive_a, len(canaries)))
    ck_eq(alive_a, len(canaries), "ap 本身是联接时, rmtree 把仓外内容删了")
    ctx.note("    => 顶层是联接时 shutil.rmtree 拒绝动手(CPython 的 "
             "'Cannot call rmtree on a symbolic link'), 这一路是安全的。")

    # 情形 B: ap 的**父目录**是联接 —— 关键
    parent = os.path.join(base, "parent_j")
    mk_junction(parent, outside)
    ap_b = os.path.join(parent, "victim")
    islink_top = os.path.islink(ap_b)
    isdir_top = os.path.isdir(ap_b)
    entries = os.listdir(ap_b) if os.path.isdir(ap_b) else []
    ctx.note("[B] 父目录是联接: islink(ap)=%s  isdir(ap)=%s  listdir(ap)=%r"
             % (islink_top, isdir_top, entries[:3]))
    err_b = None
    try:
        shutil.rmtree(ap_b)
    except Exception as e:
        err_b = "%s: %s" % (type(e).__name__, e)
    alive_b = sum(1 for c in canaries if os.path.exists(c))
    ctx.note("[B] rmtree(ap) -> %s   **仓外哨兵存活 %d/%d**"
             % (err_b or "(没报错)", alive_b, len(canaries)))

    # 这里**只断言**我们真正依赖的那条安全性不变量, 另一侧只记录不断言:
    #   顶层是联接 -> rmtree 自己拒绝      <- 断言(这是 F9 的回归哨兵)
    #   父目录是联接 -> rmtree **不检查**  <- 只记录。
    # 为什么不对后者断言: 如果哪天 CPython 改成也检查父目录(那是个安全性改进),
    # 这条测试会误报失败。所以它只是 F8 结论的前提说明, 由 F8 自己去撞。
    ck_eq(alive_a, len(canaries),
          "ap 本身是联接时 rmtree 把仓外内容删了 —— CPython 的 "
          "'Cannot call rmtree on a symbolic link' 防线没了")
    if alive_b == len(canaries):
        ctx.note("  => 本次实测: **父目录是联接时 rmtree 也挡住了**(存活 %d/%d)。"
                 "那 F8 的竞态即使成立也删不到仓外, 结论按「打不穿」解读。"
                 % (alive_b, len(canaries)))
    else:
        ctx.note("  => 本次实测: **父目录是联接时 rmtree 照删不误**(存活 %d/%d)。"
                 "shutil.rmtree 只检查**顶层**是不是 reparse point, 不看父目录。"
                 "所以 discard 的两步(先 resolve_in_root 再 rmtree)之间若有人"
                 "把父目录换成联接, 越界删除就成立 —— F8 就是去撞这个的。"
                 % (alive_b, len(canaries)))
    force_rmtree(base)


@test("L", "junction-inside-repo-git-follows", bug="L7-§4.2补充")
def t_l7(ctx):
    """L7 §4.2 报告完全没提的一面:**git 自己会跟着目录联接走进仓外**。

    server.py 的 resolve_in_root 会挡住你显式请求 `escape/...`;
    但 commit() 走的是 `git add -A`, **不过 resolve_in_root**。
    所以仓内放一个指向仓外目录的联接, commit 一次就把仓外文件收进索引,
    再 commit 就进了对象库 —— 仓外内容被复制进了仓的历史, 且永远留在那。

    ⚠ 判据方向改过(原来是 bug 冻结断言): 原来的
    `ck("escape/leak.txt" in ls-files, "…尺子坏了")` **要求这个泄漏真的发生**。
    现在要求的是「**不许**泄漏」, 所以这条在 server.py 修好之前是**红的** ——
    那是对的, 它报的是产品的缺陷。详见下面判据处的注释。
    """
    r = ctx.repo
    r.write("keep.txt", "keep\n")
    r.commit("c1")
    outside = os.path.join(TESTROOT, "_outside_add")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    secret = os.path.join(outside, "leak.txt")
    with io.open(secret, "w", encoding="utf-8", newline="\n") as f:
        f.write("LEAKED-INTO-REPO\n")

    link = r.abspath("escape")
    if not mk_junction(link, outside):
        raise Skip("mklink /J 造不出来, 本条测不了")

    st = r.out("status", "--porcelain", "--untracked-files=all")
    ctx.note("放好联接后 git status 看见: %r" % st.split())
    code, j = ctx.srv.jpost("/api/commit", {"message": "with junction"})
    ctx.note("POST /api/commit -> %s staged=%d"
             % (code, len(j.get("staged") or [])))
    ctx.note("commit 后 git ls-files: %r"
             % [x for x in r.out("ls-files").split() if x])
    blob = r.git("show", "HEAD:escape/leak.txt", check=False)
    ctx.note("git show HEAD:escape/leak.txt -> rc=%s %r"
             % (blob.returncode, (blob.stdout or "")[:40]))
    ctx.note("仓外 secret.txt 内容 = %r" % read_text(secret))
    ctx.note("  => 实测: git add -A **穿过目录联接**, 仓外文件被 commit 进仓内历史。"
             "resolve_in_root 完全没参与这条路径(commit 不校验路径)。")

    # ══════════════════════════════════════════════════════════════════
    # ⚠ 判据方向:正确行为是「**不许**泄漏」。
    #
    # 原来写的是:
    #     ck("escape/leak.txt" in r.out("ls-files"), "…与实测不符, 尺子坏了")
    # 也就是**要求漏洞真的发生** —— 两条断言里这一条是「bug 冻结断言」。
    # 它绿,是因为洞真的在; 一旦 server.py 在 commit 路径上补上路径校验,
    # 它会立刻变红,变成「修复即失败」的反向门禁,而且报错(「尺子坏了」)
    # 会**引导人去改测试而不是改产品**。所以判据必须反过来。
    #
    # 审计第一版说「没有一条把 bug 固化成期望值」, 第二版自我更正指向了这条
    # (wave3 §1.4 / N8)。我独立复核过, **第二版是对的**:
    #   `git add -A` 在本机实测会把 `escape/leak.txt` 收进索引
    #   (git status -uall 看见 `?? escape/leak.txt`),
    #   commit 之后 `git show HEAD:escape/leak.txt` rc=0 内容就是仓外那份。
    #
    # ⇒ 这条**红着是对的**。它报的是 server.py 的真缺陷, 不是测试的毛病。
    #   修法在 server.py 的 commit()(本文件不归这条线管, 不在这里动)。
    #   威胁模型要说清: 需要在仓工作区里有并发写权限(同一个本地用户),
    #   所以不跨权限边界 —— 但它会把「同一个用户碰得到、却与本仓无关」的文件
    #   **复制**进仓的历史, 而仓是要 push 出去的。
    # ══════════════════════════════════════════════════════════════════
    ck("escape/leak.txt" not in [x for x in r.out("ls-files").split() if x],
       "git add -A **穿过目录联接**, 把联接指向的**仓外**文件收进了索引 —— "
       "仓外内容被复制进了仓的历史, 而且永远留在那。\n"
       "实测: git show HEAD:escape/leak.txt rc=%s 内容=%r\n"
       "机制: server.py 的 commit() 直接跑 `git add -A`, 不经过 resolve_in_root, "
       "所以现有的路径校验在这条路径上完全没参与。\n"
       "修法在 server.py(不在本文件的责任范围, 这里只把判据摆正)。"
       % (blob.returncode, (blob.stdout or "")[:60]))

    # 仓外那个文件**本身**不能被改 —— 这条原来就对, 只把措辞改准。
    # 原措辞「commit 顺手改了仓外文件」读起来像在要求它被改, 实际要求的是没被改。
    ck_eq(read_text(secret), "LEAKED-INTO-REPO\n",
          "commit 改了联接指向的**仓外**文件的内容 —— 这一条是要求它**没**被改")


# ════════════════════════════════════════════════════════ C 组:大小写折叠(§4.3)

@test("C", "volume-case-sensitivity", bug="C1-§4.3")
def t_c1(ctx):
    """C1 §4.3 的前置:先把**卷的大小写敏感性**实测出来。

    报告说「大小写折叠的实际行为依赖卷配置」但没查。这一条查:
      * 直接实验: 建一个 MiXeD.TxT, 看 mixed.txt 能不能命中
      * 看 listdir 返回的**存储名**
      * fsutil fsinfo ntfsinfo / queryCaseSensitiveInfo(需要管理员, 记录结果)
    fsutil 拿不到时用实验兜底, 不能因此 SKIP —— 实验本身就是结论。
    """
    base = os.path.join(TESTROOT, "_case")
    force_rmtree(base)
    os.makedirs(base, exist_ok=True)
    real = os.path.join(base, "MiXeD.TxT")
    with io.open(real, "w", encoding="utf-8", newline="\n") as f:
        f.write("x\n")
    stored = os.listdir(base)
    hit_lower = os.path.exists(os.path.join(base, "mixed.txt"))
    hit_upper = os.path.exists(os.path.join(base, "MIXED.TXT"))
    ctx.note("存储名 = %r" % stored)
    ctx.note("exists('mixed.txt') = %s   exists('MIXED.TXT') = %s" % (hit_lower, hit_upper))
    ck_eq(stored, ["MiXeD.TxT"], "listdir 返回的不是我建的那个名字, 尺子坏了")
    if hit_lower or hit_upper:
        ctx.note("  => 实测: 本卷**不区分大小写**(小写/全大写都能命中 MiXeD.TxT)。")
    else:
        ctx.note("  => 实测: 本卷**区分大小写**。")
        raise Skip("本卷区分大小写, 大小写折叠的形态与 NTFS 默认不同, "
                   "C2 的结论不能按 NTFS 语义解读")

    # fsutil 需要管理员, 如实记录而不是跳过
    drive = os.path.splitdrive(TESTROOT)[0] or "C:"
    for args in (["fsutil", "fsinfo", "ntfsinfo", drive],
                 ["fsutil", "file", "queryCaseSensitiveInfo", base]):
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        ctx.note("  %s -> rc=%s %r" % (" ".join(args), p.returncode, out[:110]))

    # resolve() 返回磁盘真实大小写还是用户敲的?
    import pathlib
    rp = pathlib.Path(base).resolve()
    typed = str((rp / "mixed.txt").resolve())
    ctx.note("resolve(root/'mixed.txt') -> %r" % os.path.basename(typed))
    ck_eq(os.path.basename(typed), "MiXeD.TxT",
          "resolve() 没有把大小写规范成磁盘上的真实名字")
    ctx.note("  => Path.resolve() 对**已存在**的路径返回磁盘上的真实大小写; "
             "这是 C2 里 relative_to 能挡住大小写折叠的直接原因。")


@test("C", "casefold-traversal", bug="C2-§4.3")
def t_c2(ctx):
    """C2 §4.3:构造靠大小写绕过 relative_to 的形态, **实测**而不是推断。

    报告原文:「推断是 Path.resolve() 在 relative_to 之前就规范了大小写,
    所以应该挡得住 —— 但这是推断, 不是实测」。这条就是那个实测。

    向量分三类:
      A 纯大小写折叠:  ../../ETC、../.. /Windows、../../windows/System32
      B 大小写 + 反斜杠: ..\\..\\EtC\\hosts
      C 大小写 + 8.3 短名(见 C3)
    判据只有一条, 而且是**正确行为**: 必须拒绝, 且读不到仓外内容。
    「被接受」不等于越界 —— 所以每条都同时看 HTTP 状态码和仓外哨兵。
    """
    r = ctx.repo
    r.write("a.txt", "x\n")
    r.commit("c1")
    canary = outside_marker("c2")

    # 找一个真实存在的仓外可读文件当越界判据
    real_outside = os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"),
                                "System32", "drivers", "etc", "hosts")
    ctx.note("仓外真实文件(存在性=%s): %s" % (os.path.exists(real_outside), real_outside))

    vectors = [
        "../../ETC", "../.. /Windows".replace(" ", ""), "../../windows/System32",
        "..\\..\\EtC\\hosts", "..\\..\\WINDOWS\\System32",
        "../../../../Windows/System32/drivers/etc/hosts",
        "..\\..\\Windows\\System32\\drivers\\etc\\hosts",
        ".././../../Windows", "./../../ETC/hosts",
        "../../ETC/../Windows", "../../ETC\\hosts",
        "../../windows/system32/drivers/etc/hosts",
    ]
    escaped, accepted, leaked = [], [], []
    for v in vectors:
        try:
            code, txt = ctx.srv.raw("GET", "/api/file?file=" + q(v))
        except Exception as e:
            code, txt = -1, "%s: %s" % (type(e).__name__, e)
        ctx.note("%-52r -> %-4s %s" % (v, code, txt[:66].replace("\n", " ")))
        if code == 200:
            accepted.append(v)
            # 200 也可能是「接受成仓内某个普通文件名」—— 用文件数判断
            try:
                j = json.loads(txt)
                if (j.get("new") or "").strip():
                    leaked.append((v, (j.get("new") or "")[:40]))
            except Exception:
                pass
        elif code != 400 and code != 404 and code != 500:
            escaped.append((v, code))
        ck("CANARY:c2" not in txt, "向量 %r 把仓外哨兵内容吐出来了" % v)
        for name in ("aigit", "PASS", "SERVER="):
            ck(name not in txt, "向量 %r 读出了仓外的 %s" % (v, name))
        ck(outside_intact(canary), "向量 %r 动了仓外哨兵" % v)

    for ep in ("/api/diff?file=", "/api/file?file="):
        for v in vectors[:5]:
            code, txt = ctx.srv.raw("GET", ep + q(v))
            ck(code != 500, "%s%r 返回 500: %s" % (ep, v, txt[:100]))
    for v in vectors:
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": [v], "confirmed": True}))
        ck(code != 500, "discard %r 返回 500: %s" % (v, txt[:100]))
        ck(os.path.exists(r.abspath(".git")), "discard %r 把 .git 弄没了" % v)
        ck(outside_intact(canary), "discard %r 动了仓外哨兵" % v)

    ck(not leaked, "这些向量被接受且读出了内容(可能是仓内普通文件名, 需要人工看): %r"
       % leaked)
    ck(not escaped, "这些向量得到了意外的状态码: %r" % escaped)
    if accepted:
        ctx.note("注意: %d 个向量返回了 200 —— 逐个看是不是被当成仓内普通文件名"
                 % len(accepted))
        for v in accepted:
            ctx.note("   200 的向量 %r" % v)
    ck(ctx.srv.alive(), "服务不健康")


@test("C", "shortname-8dot3-bypass", bug="C3-§4.3补充")
def t_c3(ctx):
    """C3 §4.3 报告完全没提的一个形态:**8.3 短名**(PROGRA~1 / AIGIT~1)。

    NTFS 默认给长文件名分配 8.3 别名, 这些别名里的 `~` 和大小写折叠一样,
    可能让基于字符串比较的检查失效。resolve_in_root 用的是
    `p.relative_to(root)`, 是**字符串**比较 —— 短名不展开的话就能绕过。
    这条把「resolve 到底展不展开短名」量出来, 然后真打一遍。
    """
    import ctypes as _c
    # 建一个长到一定出短名的目录
    long_dir = os.path.join(TESTROOT, "_shortname_probe_directory_with_long_name_abc")
    force_rmtree(long_dir)
    os.makedirs(long_dir, exist_ok=True)
    _c.windll.kernel32.GetShortPathNameW.restype = _c.c_uint
    _c.windll.kernel32.GetShortPathNameW.argtypes = [
        _c.c_wchar_p, _c.c_wchar_p, _c.c_uint]
    buf = _c.create_unicode_buffer(1024)
    _c.windll.kernel32.GetShortPathNameW(_c.c_wchar_p(long_dir), buf, 1024)
    short = buf.value
    ctx.note("长名 = %s" % long_dir)
    ctx.note("短名 = %s" % short)
    if not short or short == long_dir:
        raise Skip("这个目录没被分配 8.3 短名(可能该卷禁用了 8.3), 造不出短名向量")
    ctx.note("短名与长名不同, 短名向量可构造")

    # 短名放进 rel: resolve_in_root 会怎么处理?
    import pathlib
    rp = pathlib.Path(ctx.repo.dir).resolve()
    # 先在仓内造一个真实存在的东西, 再用短名去指它
    ctx.repo.write("deep/inner/file.txt", "inner\n")
    inside_long = ctx.repo.abspath("deep")
    buf2 = _c.create_unicode_buffer(1024)
    _c.windll.kernel32.GetShortPathNameW(_c.c_wchar_p(inside_long), buf2, 1024)
    inside_short = buf2.value.rsplit("\\", 1)[-1]
    ctx.note("仓内 deep/ 的短名 = %r" % inside_short)

    if inside_short and inside_short != "deep":
        rel_short = inside_short + "/inner/file.txt"
        p_short = (rp / rel_short).resolve()
        p_long = (rp / "deep/inner/file.txt").resolve()
        ctx.note("用短名  -> %s" % str(p_short).replace(str(rp), "<ROOT>"))
        ctx.note("用长名  -> %s" % str(p_long).replace(str(rp), "<ROOT>"))
        same = os.path.normcase(str(p_short)) == os.path.normcase(str(p_long))
        ctx.note("两者指向同一处? %s" % same)
        ck(same, "短名和长名 resolve 到不同地方, 这条测不出东西")
        code, j = ctx.srv.jget("/api/file?file=" + q(rel_short))
        ctx.note("GET /api/file?file=%r -> %s" % (rel_short, code))
        ck_eq(code, 200, "用短名指仓内文件应当能读到")

    # 越界形态: 用**仓外**目录的短名 + ../
    canary = outside_marker("c3")
    sib_long = os.path.join(TESTROOT, "_outside_sibling_with_a_long_name_abcdefghij")
    force_rmtree(sib_long)
    os.makedirs(sib_long, exist_ok=True)
    with io.open(os.path.join(sib_long, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("CANARY:C3-SIBLING\n")
    buf3 = _c.create_unicode_buffer(1024)
    _c.windll.kernel32.GetShortPathNameW(_c.c_wchar_p(sib_long), buf3, 1024)
    sib_short = buf3.value
    ctx.note("仓外兄弟目录 长名=%s 短名=%s" % (sib_long, sib_short))
    sib_short_base = os.path.basename(sib_short)

    vectors = [
        "../%s/secret.txt" % sib_short_base,
        "../%s/secret.txt" % os.path.basename(sib_long),
        "..\\%s\\secret.txt" % sib_short_base,
        "../%s/../%s/secret.txt" % (os.path.basename(long_dir), sib_short_base),
    ]
    for v in vectors:
        code, txt = ctx.srv.raw("GET", "/api/file?file=" + q(v))
        ctx.note("%-56r -> %-4s %s" % (v, code, txt[:60].replace("\n", " ")))
        ck("CANARY:C3-SIBLING" not in txt, "短名向量 %r 读出了仓外文件" % v)
        ck_eq(code, 400, "短名越界向量 %r 期望 400" % v)
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": [v], "confirmed": True}))
        ck_eq(code, 400, "discard 短名越界 %r 期望 400" % v)
        ck(os.path.exists(os.path.join(sib_long, "secret.txt")),
           "短名向量 %r 把仓外文件删了" % v)
    ck(outside_intact(canary), "仓外哨兵被动了")
    ck(os.path.exists(ctx.repo.abspath(".git")), ".git 没了")
    ck(ctx.srv.alive(), "服务不健康")


# ════════════════════════════════════════════════════════ F 组:§4.7

@test("F", "unborn-head-status", bug="F1-§4.7")
def t_f1(ctx):
    """F1 §4.7:未出生的 HEAD。报告说「main() 会 init 一个空仓, 但没测过无 HEAD 时
    /api/status 的行为」。先说清楚前提: main() 在建仓后会写 README 并 commit,
    所以 aigit 自己 init 出来的仓**一定**有 HEAD。真正的触发条件是
    **用户自己 git init 过但还没 commit** —— 这是很常见的现场。

    判据是正确行为: 无 HEAD 时 /api/status 应给出可用的状态
    (git 自己就能报: status 成功、只是 rev-parse HEAD 失败)。
    现在 status() 把 rev-parse 的 check 设成 True, 整个接口就 400 了。
    """
    r = ctx.repo
    r.write("untracked.txt", "x\n")     # 仓里有活儿, 但没有 commit
    code, txt = ctx.srv.raw("GET", "/api/status")
    ctx.note("无 HEAD 时 GET /api/status -> %s %r" % (code, txt[:150]))
    ctx.note("git rev-parse --abbrev-ref HEAD -> %r"
             % r.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip())
    ctx.note("git status --porcelain=v1 -z -> %r"
             % r.git("status", "--porcelain=v1", "-z").stdout)

    for ep in ("/api/branches", "/api/log", "/api/diff?file=untracked.txt",
               "/api/file?file=untracked.txt"):
        c, t = ctx.srv.raw("GET", ep)
        ctx.note("  GET %-38s -> %-4s %s" % (ep, c, t[:100].replace("\n", " ")))
        ck_eq(c, 200, "无 HEAD 时 GET %s 应仍可用(git 本身能处理), 实测 %s" % (ep, c))

    # (2) 更严重的一面: 每个 POST 接口结尾都要 REPO.status(), 所以无 HEAD 时
    #     **每个 POST 都返回 400, 但操作其实已经做完了**。这比「主界面看不到」
    #     严重: 客户端以为失败会重试, 于是同一操作被执行两次。
    code, txt = ctx.srv.raw("POST", "/api/stage",
                            json.dumps({"files": ["untracked.txt"]}))
    staged = r.out("diff", "--cached", "--name-only").split()
    ctx.note("无 HEAD 时 POST /api/stage -> %s %r" % (code, txt[:90]))
    ctx.note("  但 git 说文件**已经被 stage 了**: %r" % staged)
    ck_eq(staged, ["untracked.txt"],
          "前置: stage 之后 git 应该有暂存内容, 否则这条测不出东西")
    ck_eq(code, 200,
          "无 HEAD 时 POST /api/stage 返回 %s, 但**文件其实已经被 stage 了**。"
          "客户端以为失败 -> 重试 -> 重复执行。git 状态实测=%r, "
          "aigit 响应=%r" % (code, staged, txt[:120]))

    code, txt = ctx.srv.raw("POST", "/api/commit", json.dumps({"message": "first"}))
    log = r.out("log", "--oneline").strip()
    ctx.note("无 HEAD 时 POST /api/commit -> %s" % code)
    ctx.note("  但 git 说**提交已经产生了**: %r" % log)
    ck(log.startswith(("1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
                       "a", "b", "c", "d", "e", "f")),
       "前置: commit 之后应该有提交, 否则这条测不出东西")
    ck_eq(code, 200,
          "无 HEAD 时 POST /api/commit 返回 %s, 但提交**已经写进历史了**。"
          "客户端拿到 400 会重试 -> 重复提交。git log=%r" % (code, log))

    # (3) 有了 HEAD 之后一切正常 -> 确认根因就是 rev-parse check=True
    code, j = ctx.srv.jget("/api/status")
    ctx.note("有 HEAD 后 GET /api/status -> %s branch=%r"
             % (code, j.get("branch")))
    ck_eq(code, 200, "有了第一个提交之后 /api/status 应正常")
    ck_eq(j.get("branch"), "main", "有 HEAD 后 branch 应是 main")

    # (4) 根因量出来: git 自己能报未出生分支
    p = r.git("symbolic-ref", "--short", "HEAD")
    ctx.note("git symbolic-ref --short HEAD (空仓) -> rc=%s out=%r"
             % (p.returncode, p.stdout.strip()))
    ctx.note("git rev-parse --abbrev-ref HEAD (空仓) -> rc=%s out=%r stderr=%r"
             % (r.git("rev-parse", "--abbrev-ref", "HEAD").returncode,
                r.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                r.git("rev-parse", "--abbrev-ref", "HEAD").stderr.strip()[:60]))
    ctx.note("  => 根因: status() 用 rev-parse --abbrev-ref HEAD 且 check=True。"
             "未出生 HEAD 时它 rc=128, 于是: /api/status 400, 并且**每个 POST "
             "都 400**(它们结尾都要 REPO.status()), 尽管操作已经做完。"
             "git symbolic-ref 就能正确报出 main。")


@test("F", "port-in-use", bug="F2-§4.7")
def t_f2(ctx):
    """F2 §4.7:端口被占用。报告说「main() 会抛异常退出, 未测」。

    判据是正确行为: 报错的**内容**必须让一个没读过源码的人知道
    「端口被占了, 换一个」。裸 Python traceback(尤其混在一堆
    socketserver/http.server 内部帧里)不算。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    env = dict(os.environ)
    # 子进程按系统 ANSI 代码页(cp936)写 stderr, 父进程按 utf-8 解 -> 全是 U+FFFD。
    # 强制子进程也用 utf-8, 否则报错原文根本读不出来。
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(
            [sys.executable, SERVER_PY, "--root", ctx.repo.dir, "--port", str(port)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=AIGIT, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        err = p.stderr or ""
        ctx.note("占住端口 %d 后再起一个: rc=%s" % (port, p.returncode))
        ctx.note("stdout = %r" % (p.stdout or "")[:120])
        for line in err.strip().splitlines():
            ctx.note("stderr | " + line)
        ck(p.returncode != 0, "端口被占用时进程应当非零退出, 实测 rc=%s" % p.returncode)
        has_traceback = "Traceback (most recent call last)" in err
        ctx.note("是裸 traceback? %s" % has_traceback)
        # 人看得懂: 要么出现端口号, 要么明确说「地址已被使用」。
        # 注意 mentions_inuse **刻意不含 10013** —— 第一版把它算进去了, 那是个坏尺子:
        # 10013 是 WSAEACCES「访问权限被禁止」, 恰恰是这条报错**在误导人**的那个码,
        # 把它当「提到了地址被占用」等于让这条断言永远通过。改掉之后本条实测为 False。
        mentions_port = str(port) in err
        mentions_inuse = ("10048" in err
                          or "Only one usage of each socket address" in err
                          or "每个 TCP 协议地址" in err
                          or "地址已被使用" in err)
        ctx.note("提到端口号? %s   明确说了「地址已被使用」? %s"
                 % (mentions_port, mentions_inuse))
        ctx.note("  => 顺带量到一件有用的事: 报的是 WSAEACCES(10013, "
                 "「访问权限被禁止」)而不是 WSAEADDRINUSE(10048, 「地址已被使用」)。"
                 "原因是 http.server 的 TCPServer 设了 allow_reuse_address=1, "
                 "Windows 上带 SO_REUSEADDR 的重复 bind 失败一律回 10013 —— "
                 "所以这条报错在 Windows 上天然会把人带偏。")
        # 先断**实质性**的一条: 报错要让用户看出是端口的问题。
        # (本机实测: 最后一行是 PermissionError: [WinError 10013] 访问权限被禁止 ——
        #  10013 是 WSAEACCES, 指向「权限」而不是「端口被占」, 而且全文没出现端口号,
        #  用户只会以为是防火墙/管理员权限问题。)
        ck(mentions_port,
           "端口 %d 被占用时报错里找不到这个端口号 —— 最后一行是 %r, "
           "用户看不出是端口被占, 只会当成权限问题(WSAEACCES 10013)"
           % (port, (err.strip().splitlines() or [""])[-1][:80]))
        ck(mentions_inuse, "报错里没有「地址已被使用」这类说明")
        # 裸 traceback 单独记一条: 即使上面两条修好了, 也不该把内部帧倒给用户。
        ck(not has_traceback,
           "端口被占用时抛的是裸 traceback(共 %d 行), 没有一句给用户看的话 —— "
           "报告 §4.7 这条实测结果是「不可用」" % len(err.strip().splitlines()))
        ctx.note("  (ck 短路: 实测停在第一条。所以下面那条 mentions_inuse 的实测值"
                 "同时也是「就算端口号补上了, 报错仍然在误导人」的证据 —— "
                 "两条要一起修才算修好)")
    finally:
        s.close()


@test("F", "ref-revspec-exhaustive", bug="F3-§4.7")
def t_f3(ctx):
    """F3 §4.7:`/api/file` 的 ref 参数。报告说「拼了 : 所以裸选项注入不了,
    但没有穷举 rev-spec」。这条穷举, 并且**改了判据**。

    判据不设成「必须报错」—— ref 本来就该支持任意合法 rev-spec。
    真正的正确行为只有两条底线:
      1. 不能读出**仓外**任何内容(用仓外可读文件当判据);
      2. 不能**写**仓外任何东西(git show 有 --output= 这类会落盘的选项)。
    中间那一大堆 200 且 old 有内容, 属于「ref 没被校验」——
    报不报 bug 取决于威胁模型, 但**必须记下实测**。
    """
    r = ctx.repo
    r.write("a.txt", "V1\n")
    r.write("sub/b.txt", "B-V1\n")
    r.commit("c1")
    r.git("tag", "-a", "v1", "-m", "tagmsg")
    r.write("a.txt", "V2-DIRTY\n")
    r.git("checkout", "-q", "-b", "topic")
    r.git("checkout", "-q", "main")

    outside = os.path.join(TESTROOT, "_outside_ref")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    probe = os.path.join(outside, "PROBE_REF_CANARY.txt")
    with io.open(probe, "w", encoding="utf-8", newline="\n") as f:
        f.write("CANARY-REF-OUTSIDE\n")
    abs_out = os.path.normcase(probe)
    ctx.note("仓外判据文件: %s" % probe)

    refs = [
        "HEAD", "HEAD^{}", "HEAD^{commit}", "HEAD^{tree}", "HEAD^{/a.txt}",
        "HEAD^{/does/not/exist}", "HEAD~1", "HEAD~99", "HEAD^", "HEAD^2",
        "HEAD@{0}", "HEAD@{1}", "HEAD@{2}", "HEAD@{99}", "HEAD@{}",
        "master@{yesterday}", "master@{1 day ago}", "HEAD@{now}",
        "@{-1}", "@{-99}", "@", ":", "::", ":/", ":/a.txt",
        ":/../../../etc/passwd", "HEAD:", ":a.txt", "HEAD:a.txt",
        "refs/heads/main", "refs/heads/topic", "refs/tags/v1", "v1",
        "master", "main", "topic", "does-not-exist",
        "--output=%s" % probe, "--output=%s" % (os.path.join(TESTROOT, "PWNED.txt")),
        "-o%s" % probe, "--help", "-h", "--paginate", "-p", "--stat",
        "--name-only", "--oneline", "--no-patch", "--notes", "--abbrev-commit",
        "--encoding=UTF-8", "--encoding=BOGUS-ENC", "--format=%H",
        "--format=%H%n%an%n%ae%n%s", "--format=%(objectname)", "--format=%x00",
        "--ext-diff", "--textconv", "--", "-", "-s", "--all", "--branches",
        "%", "%(objectname)", "HEAD --no-patch", "HEAD ", " HEAD", "HEAD\t",
        "HEAD\nHEAD", "../../../../etc", "../../../etc/passwd",
        "HEAD:../../../etc/passwd", "HEAD:../a.txt", "HEAD::a.txt",
        ":/../../../../Windows/win.ini", ":\\", "HEAD:../..",
        " --literal-pathspecs", "--end-of-options",
        "a.txt", "sub", "sub/", "HEAD:/", ":/ ", "-z", "--no-textconv",
    ]
    ok, none, err400, err500 = [], [], [], []
    writes = []
    for ref in refs:
        url = "/api/file?file=%s&ref=%s" % (q("a.txt"), q(ref))
        try:
            code, txt = ctx.srv.raw("GET", url, timeout=30)
        except Exception as e:
            code, txt = -1, "%s: %s" % (type(e).__name__, e)
        note = "ref=%-34r -> %-4s" % (ref, code)
        if code == 200:
            try:
                j = json.loads(txt)
                old = j.get("old")
            except Exception:
                old = None
            if old:
                first = old.splitlines()[0][:44] if isinstance(old, str) else repr(old)
                ok.append((ref, first))
                note += " old 首行=%r" % first
            else:
                none.append(ref)
                note += " old=None"
        elif code == 400:
            err400.append(ref)
        elif code == 500:
            err500.append((ref, txt[:70]))
            note += " 500 %s" % txt[:70]
        ctx.note(note)
        # 底线 1: 不许读出仓外
        ck("CANARY-REF-OUTSIDE" not in txt, "ref=%r 读出了仓外文件内容" % ref)
        for marker in ("drivers =", "[fonts]", "root:x:", "<html"):
            ck(marker not in txt, "ref=%r 读出了疑似仓外文件(命中 %r)" % (ref, marker))
        ck(abs_out not in txt, "ref=%r 响应里出现了仓外绝对路径" % ref)
        # 底线 2: 不许写仓外
        if os.path.exists(probe):
            with io.open(probe, encoding="utf-8") as f:
                if f.read() != "CANARY-REF-OUTSIDE\n":
                    writes.append(ref)
        ck(not os.path.exists(os.path.join(TESTROOT, "PWNED.txt")),
           "ref=%r 通过 --output 之类在仓外创建了文件" % ref)

    ctx.note("返回 200 且 old 有内容的 %d 个: %r" % (len(ok), [x[0] for x in ok]))
    ctx.note("返回 200 但 old=None 的 %d 个: %r" % (len(none), none))
    ctx.note("返回 400 的 %d 个" % len(err400))
    ctx.note("返回 500 的: %r" % err500)

    with io.open(probe, encoding="utf-8") as f:
        ck_eq(f.read(), "CANARY-REF-OUTSIDE\n",
              "有 ref 把仓外的 %s 改掉了 —— 逐个查: %r" % (probe, writes))
    ck(os.path.exists(ctx.repo.abspath(".git")), ".git 没了")
    ck(ctx.srv.alive(), "穷举之后服务不健康")


@test("F", "ref-can-read-arb-rev", bug="F4-§4.7")
def t_f4(ctx):
    """F4 §4.7:ref 必须过 rev-spec 白名单 —— 以及为什么这条要求是合理的。

    F3 量了「有没有底线被突破」(没有, 因为仓外读不到、仓外写不了)。
    F4 量的是**为什么必须加白名单**, 以及白名单加没加上。

    分成三段, 各自量各自的东西(⚠ 这三段是重写过的, 原因见下面 2) 的注释):
      1) 裸选项(`-s` / `--stat`)现在在进 git 之前就被拒了;
      2a) `--opt=value` 形态**确实危险** —— 这直接问 git 量, 不经 server,
          所以它是 git 的事实, 不随 server.py 的漏洞修没修而变;
      2b) 服务端必须拒这些形态(期望 **400**), 且合法 rev-spec 必须照常能读;
      3) ref 能读任意 ref 的内容, 这是设计如此(前端要用), 但因此 ref 是可信输入。

    ⚠ 这条原来**永远不可能变绿**: 对同一个 `code` 既断言 200 又断言 400。
    详见下面 2) 段的开头。
    """
    r = ctx.repo
    r.write("a.txt", "SECRET-FILE-BODY\n")
    r.write("b.txt", "OTHER\n")
    r.commit("c1")
    r.write("a.txt", "SECRET-FILE-BODY-DIRTY\n")

    # 1) 裸选项(`-s` / `--stat`)。
    #    ⚠ 下面这段 notes 是**实测重写过的**: 白名单(E7)修好之前, 裸选项是
    #    「拼了 ':' 让 git 认不出来」而失效的(响应 200 + old=null); 白名单修好之后
    #    它们在**进 git 之前**就被拒了(响应 400)。两次都不是 200 + 有内容 ——
    #    这条判据两种情况下都成立,所以不用改判据, 只把 notes 改成实测的样子。
    bare_ok = True
    bare_400 = 0
    for ref in ("-s", "--stat", "--oneline", "--name-only", "--paginate"):
        code, j = ctx.srv.jget("/api/file?file=%s&ref=%s" % (q("a.txt"), q(ref)))
        old = j.get("old")
        ctx.note("ref=%-14r -> %s old=%r" % (ref, code, str(old)[:44]))
        if old is not None:
            bare_ok = False
        if code == 400:
            bare_400 += 1
    ck(bare_ok,
       "裸选项 ref(带 ':' 后缀)居然产出了内容 —— 与实测不符, "
       "说明白名单被绕过了或 git 那边的行为变了, 本条的结论要重估")
    ctx.note("  => 实测 %d/5 个裸选项在**进 git 之前**就被白名单拒了(400); "
             "剩下的是 200 + old=null。" % bare_400)
    ctx.note("  => 旧行为(白名单之前)是「拼了 ':' 让 git 报 unrecognized argument」,"
             "响应 200 + old=null —— 与「该 ref 下没这个文件」**完全一样**,"
             "客户端区分不了。现在 400 把这两种情况分开了, 这是白名单带来的改善。")

    # ══════════════════════════════════════════════════════════════════
    # 2) `--opt=value` 形态。
    #
    # ⚠ 这里原来有一处**永不变绿**的缺陷(同一条测试里对同一个 `code` 断言了
    #   两个方向相反的值):
    #       原 `:1532`  ck_eq(code, 200, "--format=%H 是合法选项, git 会成功")
    #       原 `:1544`  ck_eq(code, 400, "ref **没有白名单**...")
    #   `ck_eq` 一不等就抛 Fail, 所以不存在任何 code 值能让这条通过。
    #   端到端实测(server.py 已按收口清单 E7 加了 rev-spec 白名单):
    #       FAIL -> "--format=%H 是合法选项, git 会成功: 期望 200,实际 400"
    #   —— 报的方向还是**反的**: 它说「期望 200」, 而 200 恰恰是错的行为。
    #   原 `:1533`/`:1536` 那两条「尺子」更糟: 它们要求 old 开头是
    #   `<sha>:a.txt`, 也就是**要求漏洞真的发生**, 钉的是 bug 而不是正确行为。
    #
    # 现在拆成两段, 各量各的:
    #   2a 「这个形态确实危险」是 **git 的事实** —— 直接问 git, 不经 server。
    #      它不依赖 server.py 有没有洞, 所以永远不会因为「洞被修好」而失效。
    #   2b 「服务端必须拒」是**服务的正确行为** —— 量 HTTP 状态码, 期望 400。
    # ══════════════════════════════════════════════════════════════════
    sha = r.out("rev-parse", "HEAD").strip()
    code0, j0 = ctx.srv.jget("/api/file?file=%s" % q("a.txt"))
    base_old = j0.get("old")
    ck_eq(code0, 200, "基线: 不带 ref 读 a.txt 应 200")
    ck_eq(base_old, "SECRET-FILE-BODY\n",
          "基线 old 不是 HEAD 里的文件内容 —— 尺子坏了")

    # --- 2a) 尺子: 直接问 git(server 不参与) ---
    # 机制: show_file 拼的是 argv 的**一个**元素 `git show <ref>:<rel>`, 所以
    # ref="--format=%H" 到 git 那里变成 `git show "--format=%H:a.txt"`,
    # `=` 后面整段(含那个冒号)都被当成 --format 的值 —— 选项真的生效了。
    raw = r.git("show", "--format=%H:" + "a.txt")
    ctx.note("直接问 git: git show '--format=%%H:a.txt' rc=%s 首行=%r"
             % (raw.returncode, raw.stdout.splitlines()[:1]))
    ck_eq(raw.returncode, 0, "git show '--format=%H:a.txt' 竟然失败了 —— "
                             "git 那边的行为变了, 本条的立论要重估")
    ck(raw.stdout.startswith(sha + ":a.txt"),
       "git show '--format=%H:a.txt' 的输出开头不是 `<sha>:a.txt`(而是 "
       + repr(raw.stdout[:60]) + ") —— git 那边的行为变了, 本条的立论要重估")
    ck(raw.stdout != base_old,
       "git show '--format=%H:a.txt' 的输出竟然等于文件内容 —— "
       "那这个形态就不危险了, 白名单也就没必要, 本条的立论要重撤")
    ctx.note("  => 这三条量的是 **git**: 它证明 `--opt=value` 形态真的会改写语义, "
             "所以 ref 必须过白名单。它**不要求**服务端有洞 —— 洞修好了它照样成立。")

    # --- 2b) 正确行为: 选项形态的 ref 必须被白名单拒(400) ---
    # 期望写死 400。返回 200 才是错的行为: 那意味着 old 的语义被调用方完全控制了。
    opt_refs = ["--format=%H", "--format=%H%n%an", "--encoding=UTF-8",
                "--no-patch", "-s", "--stat"]
    for ref in opt_refs:
        code, j = ctx.srv.jget("/api/file?file=%s&ref=%s" % (q("a.txt"), q(ref)))
        ctx.note("ref=%-22r -> %s old=%r" % (ref, code, str(j.get("old"))[:44]))
        ck_eq(code, 400,
              "ref=%r 是选项形态, 必须被 rev-spec 白名单拒(期望 400)。返回 200 = "
              "ref 没有白名单: 拼了 ':' 只挡得住**裸**选项, `--opt=value` 形态照样生效"
              "(git 收到 `git show %s:a.txt`, `=` 后面整段含冒号都被当成选项值),"
              "old 字段的语义被调用方完全控制。实测没有造成越权读/越权写(见 F3), "
              "但那是运气, 不是防线。" % (ref, ref))
        ck(j.get("old") is None,
           "ref=%r 被拒时 old 仍然是 %r —— 拒了却还把内容吐出来, "
           "那这个「拒」没有意义" % (ref, repr(str(j.get("old"))[:60])))

    # --- 反向也钉住: **合法** rev-spec 必须照常能读 ---
    # 只堵洞不堵正常功能那不叫修好, 叫一刀切(那也是缺陷)。双向都要钉。
    for ref in ("HEAD", "HEAD^{tree}", "HEAD~0", "does-not-exist"):
        code, j = ctx.srv.jget("/api/file?file=%s&ref=%s" % (q("a.txt"), q(ref)))
        ctx.note("合法 ref=%-18r -> %s old=%r" % (ref, code, str(j.get("old"))[:36]))
        ck_eq(code, 200,
              "合法 rev-spec %r 被白名单误伤了(期望 200)。"
              "白名单收得太紧会让前端读历史版本全废 —— 那和漏一个是两头的问题。"
              % ref)
    ctx.note("  => 合法 rev-spec(HEAD / HEAD^{tree} / HEAD~0 / 不存在的 ref)全部 200,"
             "白名单既堵住了选项形态也没砍掉正常功能。")

    # 3) rev-spec 宽到能读**别的分支/别的 ref** 的版本
    r.git("checkout", "-q", "-b", "side")
    r.write("a.txt", "SIDE-BRANCH-BODY\n")
    r.commit("side commit")
    r.git("checkout", "-q", "main")
    code, j = ctx.srv.jget("/api/file?file=a.txt")
    ctx.note("main 分支的 a.txt old=%r" % str(j.get("old"))[:40])
    ck("SIDE-BRANCH-BODY" not in str(j.get("old")),
       "在 main 上却读到 side 分支的内容")
    code, j = ctx.srv.jget("/api/file?file=a.txt&ref=" + q("side"))
    ctx.note("ref=side 的 a.txt old=%r" % str(j.get("old"))[:40])
    ck("SIDE-BRANCH-BODY" in str(j.get("old")),
       "ref=side 读不到 side 的内容 —— 尺子坏了")
    ctx.note("  => ref 能读任意 ref 的内容, 这是设计如此(前端要用), 不是洞。"
             "但它意味着 ref 必须被视为**可信输入**, 因为没有白名单。")


@test("F", "dir-as-file-arg", bug="F5-§4.7")
def t_f5(ctx):
    """F5 §4.7:`/api/file?file=<目录>` 会怎样。

    这是我在穷举 ref 时顺带发现的形态: ref 合法时, file 传一个**目录**,
    git show HEAD:<dir> 会打印一棵 **tree 清单**, 于是 old 字段里出现
    目录列表而不是文件内容。判据: 请求一个目录应当 400(不是文件),
    而不是拿 tree 清单当「旧版内容」返回。
    """
    r = ctx.repo
    r.write("sub/b.txt", "B\n")
    r.write("sub/deep/c.txt", "C\n")
    r.write("a.txt", "A\n")
    r.commit("c1")
    # 先把证据全采完再断 —— 断言在第一条上就炸的话, 后面的形态就看不见了。
    seen = []
    for target in ("sub", "sub/", "sub/deep", "sub/deep/",
                   ".", "no-such-file.txt", "", "sub/deep/c.txt/../../.."):
        code, j = ctx.srv.jget("/api/file?file=" + q(target))
        old = j.get("old")
        ctx.note("file=%-24r -> %-4s new=%r old=%r"
                 % (target, code, str(j.get("new"))[:26], str(old)[:52]))
        seen.append((target, code, old))
    ctx.note("  => 实测: **目录与仓根一律 400**(old 不出现); 只有「仓内不存在的路径」"
             "返回 200 且 old=null。也就是说 old 字段现在只在**真的是文件**时才有值。")
    for target, code, old in seen[:4]:
        # ══════════════════════════════════════════════════════════════
        # ⚠ 判据方向:这条原来是**互斥**的两行, 不是产品缺陷, 是尺子坏了。
        #
        #     ck("tree " in str(old), "…尺子坏了…")   # 要求 old 里有 tree 清单
        #     ck_eq(code, 400, "请求目录 %r 期望 400")   # 要求返回 400
        #
        # 第二行成立时 `old` 必然是 `None`(400 的响应体没有 old 字段),
        # 于是第一行必然失败 —— 两条断言描述的是两个互斥的世界, 不存在能让
        # 它们同时为真的实现。这不是「server.py 偏离规格」, 是这条测试自己
        # 把两种尺子写在了同一个 for 循环里。
        #
        # 本函数自己的 docstring 已经把正确行为写清楚了:
        #     「判据: 请求一个目录应当 400(不是文件),
        #       而不是拿 tree 清单当「旧版内容」返回。」
        # 所以留下 `ck_eq(code, 400)`, 删掉要求 tree 清单的那行 ——
        # 后者只在**缺陷还在**的世界里成立, 留着就是「修复即失败」的反向门禁,
        # 而且报错文案(「尺子坏了」)会**引导人去改 server.py**, 而 server.py
        # 现在做的正是这个 docstring 要的事。
        #
        # 顺带把 old 也钉住: 返回 400 时不许带出任何内容, 否则又变成
        # 「old 的语义不唯一」那个真问题。
        ck_eq(code, 400, "请求目录 %r 期望 400(它不是文件)" % target)
        ck(old is None, "file=%r 返回 400 却带出了 old=%r —— 400 的响应体"
                        "不该有任何内容字段" % (target, str(old)[:60]))
    # 正常文件不能受影响
    code, j = ctx.srv.jget("/api/file?file=sub/b.txt")
    ck_eq(code, 200, "正常文件应仍可读")
    ck_eq((j.get("old") or "").strip(), "B", "正常文件内容错了")
    ck(ctx.srv.alive(), "服务不健康")


@test("F", "scale-100k-files", bug="F6-§4.7")
def t_f6(ctx):
    """F6 §4.7:规模。报告说「仓最大只有几个文件, 10 万文件的仓上 status() 的
    表现、内存、时间全未测」。这条实测。

    建仓很慢(约 40-60 秒), 所以单独成条, 可以用 --fast 跳过。
    判据是正确行为:
      * /api/status 必须**正确**: 条目数 == 磁盘上的文件数, 不多不少不乱序;
      * 不能超时(服务里 git 的 timeout 是 120 秒)。
    耗时和内存只记录不断言 —— 那是取舍不是对错。
    """
    r = ctx.repo
    r.write("seed.txt", "seed\n")
    r.commit("seed")
    N = 100000
    ctx.note("开始造 %d 个未跟踪文件..." % N)
    t0 = time.time()
    for i in range(N):
        with io.open(r.abspath("f%05d.txt" % i), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("x\n")
    t1 = time.time()
    ctx.note("造 %d 个文件: %.1fs" % (N, t1 - t0))
    pk0, cur0 = ctx.srv.mem_mb()
    ctx.note("服务进程内存(造仓后): 峰值 %.1f MB / 当前 %.1f MB" % (pk0, cur0))

    t2 = time.time()
    code, txt = ctx.srv.raw("GET", "/api/status", timeout=300)
    t3 = time.time()
    status_bytes = len(txt)
    ctx.note("GET /api/status: code=%s %d 字节 %.2fs"
             % (code, len(txt), t3 - t2))
    pk1, cur1 = ctx.srv.mem_mb()
    ctx.note("服务进程内存(一次 status 后): 峰值 %.1f MB / 当前 %.1f MB"
             % (pk1, cur1))
    ctx.note("  => 一次 /api/status 的真实代价: **峰值工作集从 %.1f MB 冲到 %.1f MB**"
             "(请求结束后回落到 %.1f MB)。服务端要先把这 10 万条路径解析成 "
             "list/dict, 再拼成一个 5.6 MB 的字符串 —— 中间态就是那 %.1f MB。"
             "不是失控, 但也不是「零成本」: 文件数翻 4 倍, 峰值也翻 4 倍。"
             % (pk0, pk1, cur1, pk1 - pk0))

    t4 = time.time()
    git_t = time.time()
    p = r.git("status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=300)
    git_t = time.time() - git_t
    ctx.note("对照 git status(同样参数, CLI): %.2fs, 条目 %d"
             % (git_t, len([e for e in p.stdout.split("\0") if e])))
    ctx.note("aigit 的 status 是 git status 的 %.1f 倍"
             % ((t3 - t2) / git_t if git_t else float("nan")))

    ck_eq(code, 200, "10 万文件时 /api/status 应仍返回 200(git 本身 rc=0)")
    j = json.loads(txt)
    entries = j.get("unstaged") or []
    ck_eq(len(entries), N, "status 返回的条目数应等于磁盘上的文件数")
    paths = [e["path"] for e in entries]
    ck_eq(len(set(paths)), len(paths), "status 返回了重复路径")
    expected = sorted("f%05d.txt" % i for i in range(N))
    ck_eq(paths[:3], expected[:3], "前 3 条不对")
    ck_eq(paths[-3:], expected[-3:], "后 3 条不对")
    ck_eq(sorted(paths), expected, "10 万条路径的集合与磁盘不一致")
    ck_eq(j.get("branch"), "main", "branch 不对")
    ck(not j.get("hasStaged"), "全是未跟踪, hasStaged 应为 false")

    # diff 在 10 万文件的仓上还准不准。
    # **尺子**: 未跟踪文件本来就没有 diff(git diff 不看未跟踪文件), 所以要断
    #   diff 就必须改一个**已跟踪**的文件; 拿未跟踪文件去断会量出一个假的失败
    #   (第一版就是这么写的: 把 f00001.txt 改了就期待 +CHANGED, 而它是未跟踪的)。
    r.write("seed.txt", "CHANGED-SEED\n")
    code, txt = ctx.srv.raw("GET", "/api/diff?file=seed.txt", timeout=120)
    ctx.note("/api/diff?file=seed.txt(已跟踪) -> %s %r" % (code, txt[:150]))
    ck_eq(code, 200, "diff 一个已跟踪文件应 200")
    ck("+CHANGED-SEED" in txt, "已跟踪文件的 diff 丢了新内容")
    ck("-seed" in txt, "已跟踪文件的 diff 丢了旧内容")
    p = r.git("diff", "--", "seed.txt", timeout=120)
    ck_eq(json.loads(txt).get("diff"), p.stdout,
          "diff 输出与 git CLI 不一致")
    # 未跟踪文件: 正确行为是「空 diff」, 与 git 一致 —— 也记一笔, 因为
    # 客户端容易误以为「没 diff」= 「服务坏了」。
    code, txt2 = ctx.srv.raw("GET", "/api/diff?file=f00001.txt", timeout=120)
    p2 = r.git("diff", "--", "f00001.txt", timeout=120)
    ctx.note("/api/diff?file=f00001.txt(未跟踪, 10 万文件的仓) -> %s %r"
             % (code, txt2[:100]))
    ctx.note("  git CLI 对同一文件: %r" % p2.stdout[:60])
    ck_eq(code, 200, "diff 一个未跟踪文件也应 200")
    ck_eq(json.loads(txt2).get("diff"), p2.stdout,
          "未跟踪文件的 diff 与 git CLI 不一致")
    ctx.note("  => 10 万文件并不会让 diff 变慢或变错; 剩下的是响应体大小"
             "(status 一次要吐 %d 字节), 那是传输成本不是正确性问题。" % status_bytes)
    pk2, cur2 = ctx.srv.mem_mb()
    ctx.note("服务进程内存(全部测完): 峰值 %.1f MB / 当前 %.1f MB" % (pk2, cur2))
    ck(pk2 >= pk1, "峰值工作集倒退了(%.1f -> %.1f) —— 内存测量本身有问题"
       % (pk1, pk2))


@test("F", "scale-stage-batch", bug="F7-§4.7")
def t_f7(ctx):
    """F7 §4.7:规模,第二问 —— 一次 stage 一大批文件会怎样。

    从 10 万文件那场实测里挖出来的: `git add -- <files...>` 是把文件名
    拼成一条命令行传的, Windows 命令行有 32K 上限, 于是大批量 stage
    撞 WinError 206「文件名或扩展名太长」, 整个请求 500。
    这条把阈值量出来 —— 顺便量出**这个错误在响应里长什么样**。
    """
    r = ctx.repo
    # 先有 HEAD: 否则每个 POST 都因为 F1(未出生 HEAD) 回 400,
    # 会把「批量 stage 的规模阈值」整个盖掉(第一版就盖掉了, 500 个文件就报 400)。
    r.write("seed.txt", "seed\n")
    r.commit("seed")
    N = 4000
    t0 = time.time()
    for i in range(N):
        with io.open(r.abspath("f%05d.txt" % i), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("x\n")
    ctx.note("造 %d 个未跟踪文件: %.1fs" % (N, time.time() - t0))

    first_bad = None
    for n_files in (500, 1000, 2000, 3000, 4000):
        files = ["f%05d.txt" % i for i in range(n_files)]
        # 命令行总长: git add 的文件名是**拼在一条命令行**里传的
        argbytes = len(" ".join(files).encode("utf-8"))
        t = time.time()
        code, txt = ctx.srv.raw("POST", "/api/stage",
                                json.dumps({"files": files}), timeout=300)
        dt = time.time() - t
        ctx.note("stage %5d 个 -> code=%-4s %5.1fs  文件名共 %6d 字节 %r"
                 % (n_files, code, dt, argbytes, txt[:100].replace("\n", " ")))
        if code == 200:
            r.git("reset", "-q", "HEAD", "--")
        else:
            first_bad = (n_files, code, txt, argbytes)
            break
    ck(first_bad is None,
       "stage %d 个文件(拼出来的文件名共 %d 字节)就 %s 了: %r\n"
       "        (Windows 命令行 32K 上限; git add 的文件名是拼在一条命令行里的)"
       % (first_bad[0], first_bad[3], first_bad[1], first_bad[2][:150])
       if first_bad else "")
    ck(ctx.srv.alive(), "批量 stage 之后服务不健康")
    # 一次把全部 N 个 stage 掉, 和 CLI 的 git add -A 对拍耗时
    t = time.time()
    code, txt = ctx.srv.raw("POST", "/api/stage",
                            json.dumps({"files": ["f%05d.txt" % i
                                                  for i in range(N)]}),
                            timeout=600)
    ctx.note("stage 全部 %d 个 -> %s %.1fs %r"
             % (N, code, time.time() - t, txt[:80].replace("\n", " ")))
    t = time.time()
    r.git("reset", "-q", "HEAD", "--", timeout=600)
    r.git("add", "-A", timeout=600)
    ctx.note("对照 CLI: git reset + git add -A %d 个文件 %.1fs"
             % (N, time.time() - t))


@test("F", "toctou-discard-parent-junction", bug="F8-§4.6")
def t_f8(ctx):
    """F8 §4.6:真正的 TOCTOU 竞态尝试 —— 报告说没测, 说「需要另一个进程配合时序」。

    机制(server.py discard 的两步之间):
        1. 校验: resolve_in_root(rel) -> ap, 顺带判断 ap.is_dir()
        2. 动手: shutil.rmtree(ap) / ap.unlink()
    L6 实测过: **ap 的父目录是目录联接时, rmtree 不检查, 直接删到联接外面去。**
    所以这条起 N 个线程把 ap 的父目录反复换成联接, 主线程狂发 /api/discard。

    判据是正确行为, 只有一条: **仓外哨兵必须全部存活。**
    打穿了就是 FAIL, 打不穿也是结论 —— 但必须诚实写「试了 X 次, 命中 Y 次」,
    不能写成「测不了」。这是本条存在的全部意义。

    ⚠ 这条被改过两处, 都是因为它**测不到东西**而不是因为它太严:
      1) 原来的失败消息里写了 `hits[0][0]`, 而 Python 先求值实参 —— 于是
         竞态**没**打穿(= 期望的正确结果)时先炸 IndexError, 断言压根没跑,
         **通过路径不可达**。E4 修好之后这条只会报 ERROR。
         已改成惰性消息(照抄同文件 `:1744` 已写对的 `if first_bad else ""`)。
      2) 收口清单 §2 E4 修完之后实现的正确行为已经**变了**(握住路径链,
         握不住就 400), 所以补了第二段**确定性**判据: 对手预先握着 DELETE
         句柄时必须 400 且一个字节都没少, 放开之后必须 200 且真删掉。
         竞态是概率性的, 握持是确定性的 —— 后者才是这条契约的真判据。
    """
    r = ctx.repo
    r.write("tracked.txt", "v1\n")
    r.commit("c1")
    r.write("tracked.txt", "v2-dirty\n")

    outside = os.path.join(TESTROOT, "_outside_toctou")
    force_rmtree(outside)
    os.makedirs(os.path.join(outside, "victim"), exist_ok=True)
    canaries = []
    for i in range(40):
        p = os.path.join(outside, "victim", "c%02d.txt" % i)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("CANARY%02d\n" % i)
        canaries.append(p)
    ctx.note("仓外哨兵 %d 个, 全在 %s" % (len(canaries), outside))

    sub = r.abspath("sub")
    tpl_real = r.dir + ".tpl_real"
    tpl_junc = r.dir + ".tpl_junc"
    force_rmtree(tpl_real)
    if os.path.lexists(tpl_junc):
        try:
            os.rmdir(tpl_junc)
        except OSError:
            pass
    os.makedirs(os.path.join(tpl_real, "victim"))
    for i in range(5):
        with io.open(os.path.join(tpl_real, "victim", "in%d.txt" % i),
                     "w", encoding="utf-8", newline="\n") as f:
            f.write("inside%d\n" % i)
    if not mk_junction(tpl_junc, outside):
        raise Skip("mklink /J 造不出来(需要同卷), 本条测不了")
    os.replace(tpl_real, sub)
    ctx.note("互换靠 rename 预先造好的两个模板, 不靠 mklink: mklink /J 每次要起一个 "
             "cmd 子进程(实测 ~100ms), 25 秒只换得下百来次, 竞态根本跑不起来; "
             "预造好之后 os.replace 单次约 0.4ms。")

    stop = [False]
    stats = {"j": 0, "r": 0, "err": 0}

    def flipper():
        while not stop[0]:
            try:
                if not os.path.lexists(sub):
                    os.replace(tpl_junc, sub)
                    stats["j"] += 1
                elif is_reparse_dir(sub):
                    os.replace(sub, tpl_junc)
                    os.replace(tpl_real, sub)
                    stats["r"] += 1
                else:
                    os.replace(sub, tpl_real)
                    os.replace(tpl_junc, sub)
                    stats["j"] += 1
                    time.sleep(0.0015)      # 联接在位的停留时间
            except Exception:
                stats["err"] += 1
                time.sleep(0.0005)

    threads = [threading.Thread(target=flipper, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.4)

    # 占空比自检 —— 不然「没打穿」可能只是 flipper 压根没在跑
    duty = 0
    for _ in range(1500):
        if is_reparse_dir(sub):
            duty += 1
    ctx.note("flipper 占空比自检: 采样 1500 次, sub 是联接 %d 次 (%.0f%%)"
             % (duty, duty * 100.0 / 1500))

    attempts = 0
    codes = {}
    hits = []
    # confirmed 必须有: 判据是「仓外哨兵一个都不能少」, 不带的话闸门先把每次请求
    # 都挡成 400, 竞态窗口根本不存在, 这条就变成测「闸门在不在」的空转
    payload = json.dumps({"files": ["sub/victim", "tracked.txt"],
                          "confirmed": True})
    deadline = time.time() + 25.0
    while time.time() < deadline:
        attempts += 1
        try:
            code, txt = ctx.srv.raw("POST", "/api/discard", payload, timeout=20)
        except Exception as e:
            codes["exc"] = codes.get("exc", 0) + 1
            continue
        codes[code] = codes.get(code, 0) + 1
        lost = sum(1 for c in canaries if not os.path.exists(c))
        if lost:
            hits.append((attempts, lost, code, txt[:110].replace("\n", " ")))
            ctx.note("!!! 第 %d 次请求命中: 仓外 %d/%d 个哨兵被删掉了"
                     % (attempts, lost, len(canaries)))
            ctx.note("    该请求的响应: %s" % txt[:110].replace("\n", " "))
            for c in canaries:      # 复原, 让后面的断言有东西可查
                if not os.path.exists(c):
                    with io.open(c, "w", encoding="utf-8", newline="\n") as f:
                        f.write("CANARY\n")
            break

    stop[0] = True
    for t in threads:
        t.join(timeout=3)
    alive = sum(1 for c in canaries if os.path.exists(c))
    ctx.note("竞态结果: 发 %d 次 /api/discard, 换联接 %d 次, 换真目录 %d 次, flipper 异常 %d"
             % (attempts, stats["j"], stats["r"], stats["err"]))
    ctx.note("HTTP 状态码分布: %r" % codes)
    ctx.note("命中 %d 次: %r" % (len(hits), hits))
    ctx.note("仓外哨兵存活 %d/%d (复原之后)" % (alive, len(canaries)))
    ctx.note("威胁模型判定(别把这条吹大): 换联接需要在**仓工作区里有并发写权限**,"
             "也就是同一个本地用户。所以这个洞不跨权限边界 —— 被删的永远是"
             "「同一个用户本来就能删的文件」。它的真实意义是三条: "
             "(1) 校验-动手两步之间确实没有再校验, 与 §4.6 的描述一致; "
             "(2) 一个并发的本地进程可以把「删仓外目录」这件事**栽到 aigit 头上**"
             "(aigit 是 127.0.0.1 上的无鉴权服务, 谁都能发这个请求); "
             "(3) 它说明 discard 的「已校验」是假的, 后续任何基于它的推理都要打折。")

    for p_ in (sub, tpl_real, tpl_junc):
        try:
            if os.path.lexists(p_):
                if is_reparse_dir(p_):
                    os.rmdir(p_)
                else:
                    force_rmtree(p_)
        except Exception:
            pass

    # ⚠ 这里原来**永远不可能通过**,不是「可能红」:
    #   Python 先求值实参再调 ck, 所以 hits == [] (竞态**没**打穿 = 期望的正确结果)
    #   时, hits[0][0] 先炸成 IndexError, ck 根本没被调用。
    #   ⇒ E4 修好之后这条只会报 ERROR, 谁看到 ERROR 的第一反应都是「测试坏了」
    #     -> 去改测试 -> 很可能改成「让 hits 非空」, 那就把洞放回去了。
    #   同一个文件同一种形状的代码, `:1744`(F-scale-stage-batch)已经写对了:
    #     % (...) if first_bad else ""
    # 这里照抄那个写法 —— 消息**惰性**求值, 通过路径上的 hits[0] 根本不会被碰到。
    hit_msg = ("TOCTOU 打穿: 发 %d 次 /api/discard, 第 %d 次就命中了 —— "
               "仓外 %d/%d 个文件被 **/api/discard** 删掉(响应 %s)。"
               "机制: resolve_in_root 校验通过之后, discard 先跑 git checkout "
               "(已跟踪文件那条 restore 分支) —— 那个子进程撑出一个几十毫秒的窗口; "
               "这段时间里把 ap 的**父目录**换成目录联接, 随后的 shutil.rmtree 只检查"
               "顶层是不是 reparse point, 于是顺着联接删到仓外。"
               "占空比 %.0f%%, flipper 换联接 %d 次, 状态码分布 %r"
               % (attempts, hits[0][0], hits[0][1], len(canaries), hits[0][3],
                  duty * 100.0 / 1500, stats["j"], codes)) if hits else ""
    ck(not hits, hit_msg
       or "竞态没打穿时 `not hits` 为真, 这条不该失败 —— 说明判据被写反了。"
          "请连同上面那段惰性消息一起看: 消息为空时不能让它变成一条没内容的失败。")
    ck_eq(alive, len(canaries), "仓外哨兵没被复原干净(测试自身的问题)")
    ck(os.path.exists(r.abspath(".git")), ".git 没了")
    ck(os.path.exists(r.abspath("tracked.txt")), "tracked.txt 被误删")

    # ══════════════════════════════════════════════════════════════════
    # 第二段: 确定性版 —— 把「握不住就拒绝」这条**新契约**单独钉住。
    #
    # 收口清单 §2 E4 修完之后, 实现的正确行为已经**变了**:
    #   旧: resolve_in_root 校验通过 -> git checkout(几十毫秒窗口) -> rmtree
    #   新: 动破坏性动作之前先**握住路径链**(CreateFileW + share 模式**不含**
    #       FILE_SHARE_DELETE), 握不住就 **400 拒绝**, 不动手。
    # 上面那段竞态是概率性的: 「没打穿」可能只是没赶上。所以这里补一条**确定性**的:
    # 用 CreateFileW 对目标目录预先开一个**带 DELETE 权限**的句柄 ——
    # 这是「对手已经在动这个东西」的极端形态, 服务端握它必然失败(sharing violation)。
    #   * 握着期间: 必须 400,且目录/目录里的文件/子目录/.git/已跟踪文件一个都不少;
    #   * 放开之后: 同一个请求必须 200 且真的删掉 —— 否则就成了「一律拒绝」,
    #     那也是缺陷(前端就废了)。**双向都要钉。**
    # 握持手段来自 E4 补测脚本 verify_e4b.py, 不重写一遍 —— 这里只把它需要的那
    # 一小段 ctypes 搬进来(它是本项目里第一个证明这条契约可测的原型)。
    # ══════════════════════════════════════════════════════════════════
    pin_dir = r.abspath("pin_target")
    force_rmtree(pin_dir)
    os.makedirs(os.path.join(pin_dir, "deep"), exist_ok=True)
    for n in ("x.txt", os.path.join("deep", "y.txt")):
        with io.open(os.path.join(pin_dir, n), "w", encoding="utf-8", newline="\n") as f:
            f.write("PINNED\n")
    handle = open_delete_handle(pin_dir)
    try:
        ctx.note("确定性段: 对手预先用 DELETE 句柄握住 %s (handle=%s)" % (pin_dir, handle))
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": ["pin_target"],
                                            "confirmed": True}), timeout=30)
        ctx.note("  对手握持期间 POST /api/discard -> %s  %s" % (code, txt[:150]))
        ck_eq(code, 400,
              "对手握着 DELETE 句柄时 /api/discard 必须 **400 拒绝**(fail closed), "
              "实际返回 %s: %s。\n"
              "新契约: 破坏性动作之前先握住路径链, 握不住就不动手。"
              "返回 200 = 「校验完再动手」那个可交换的窗口又回来了(E4 未修)。\n"
              "返回 500 也**不行** —— 那是异常逃出去了, 不是有意识的拒绝。"
              % (code, txt[:160]))
        ck(os.path.isdir(pin_dir), "握持期间目录不该被删 —— 它没删成, 说明拒绝生效了")
        ck(os.path.exists(os.path.join(pin_dir, "x.txt")), "握持期间目录里的文件不该被删")
        ck(os.path.exists(os.path.join(pin_dir, "deep", "y.txt")),
           "握持期间子目录里的文件不该被删")
        ck(os.path.exists(r.abspath(".git")), "握持期间 .git 不该被动")
        ck(os.path.exists(r.abspath("tracked.txt")), "握持期间已跟踪文件不该被动")
        ck(ctx.srv.alive(), "握持期间服务不该挂")
    finally:
        close_handle(handle)
    # 反向: 放开手柄之后同一个请求必须真能删 —— 挡住「一律拒绝」这种假修法
    code, txt = ctx.srv.raw("POST", "/api/discard",
                            json.dumps({"files": ["pin_target"], "confirmed": True}),
                            timeout=30)
    ctx.note("  放开句柄后同一个请求 -> %s   真删掉了=%s" % (code, not os.path.exists(pin_dir)))
    ck_eq(code, 200,
          "放开句柄后同一个 /api/discard 必须 200 —— 返回 %s: %s。"
          "如果「握不住」被写成了「一律拒绝」, 前端的丢弃功能就全废了, "
          "那和没修是一个级别的缺陷(而且更难发现)。" % (code, txt[:160]))
    ck(not os.path.exists(pin_dir), "放开句柄后目录应当真被删掉")
    ck(os.path.exists(r.abspath(".git")), "这一段之后 .git 还在")
    ck(ctx.srv.alive(), "这一段之后服务还活着")


@test("F", "toctou-discard-self-junction", bug="F9-§4.6")
def t_f9(ctx):
    """F9 §4.6 的另一个形态:把 **ap 自己**换成联接(不是父目录)。

    L6 实测过: 顶层是联接时 shutil.rmtree 自己会拒绝
    ("Cannot call rmtree on a symbolic link"), 所以这一路预期打不穿。
    但「预期打不穿」和「实测打不穿」是两回事 —— 这条去把它撞出来,
    并且看服务端在撞到的时候会返回什么(预期是 500, 带上 Python 的异常文案)。

    这一条的价值: 如果哪天有人把 rmtree 换成别的实现(比如 os.scandir 递归
    unlink), 这条会立刻变红。**它是一条回归哨兵, 不是一次性的观测。**
    """
    r = ctx.repo
    r.write("tracked.txt", "v1\n")
    r.commit("c1")
    r.write("tracked.txt", "v2\n")

    outside = os.path.join(TESTROOT, "_outside_toctou2")
    force_rmtree(outside)
    os.makedirs(outside, exist_ok=True)
    canaries = []
    for i in range(40):
        p_ = os.path.join(outside, "self%02d.txt" % i)
        with io.open(p_, "w", encoding="utf-8", newline="\n") as f:
            f.write("CANARY%02d\n" % i)
        canaries.append(p_)

    target = r.abspath("victim")
    tpl_real = r.dir + ".tpl_real2"
    tpl_junc = r.dir + ".tpl_junc2"
    force_rmtree(tpl_real)
    if os.path.lexists(tpl_junc):
        try:
            os.rmdir(tpl_junc)
        except OSError:
            pass
    os.makedirs(tpl_real)
    if not mk_junction(tpl_junc, outside):
        raise Skip("mklink /J 造不出来, 本条测不了")
    os.replace(tpl_real, target)

    stop = [False]
    stats = {"j": 0, "r": 0, "err": 0}

    def flipper():
        while not stop[0]:
            try:
                if not os.path.lexists(target):
                    os.replace(tpl_junc, target)
                    stats["j"] += 1
                elif is_reparse_dir(target):
                    os.replace(target, tpl_junc)
                    os.replace(tpl_real, target)
                    stats["r"] += 1
                else:
                    os.replace(target, tpl_real)
                    os.replace(tpl_junc, target)
                    stats["j"] += 1
                    time.sleep(0.0015)
            except Exception:
                stats["err"] += 1
                time.sleep(0.0005)

    threads = [threading.Thread(target=flipper, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.4)

    duty = 0
    for _ in range(1500):
        if is_reparse_dir(target):
            duty += 1
    ctx.note("flipper 占空比自检: 采样 1500 次, victim 是联接 %d 次 (%.0f%%)"
             % (duty, duty * 100.0 / 1500))

    attempts = 0
    codes = {}
    errs = {}
    deadline = time.time() + 20.0
    while time.time() < deadline:
        attempts += 1
        try:
            code, txt = ctx.srv.raw("POST", "/api/discard",
                                    json.dumps({"files": ["victim", "tracked.txt"],
                                                "confirmed": True}),
                                    timeout=20)
        except Exception:
            codes["exc"] = codes.get("exc", 0) + 1
            continue
        codes[code] = codes.get(code, 0) + 1
        if code not in (200, 400):
            key = txt[:70].replace("\n", " ")
            errs[key] = errs.get(key, 0) + 1
        lost = sum(1 for c in canaries if not os.path.exists(c))
        if lost:
            break

    stop[0] = True
    for t in threads:
        t.join(timeout=3)
    alive = sum(1 for c in canaries if os.path.exists(c))
    ctx.note("竞态: 发 %d 次请求, 换联接 %d 次, 状态码分布 %r"
             % (attempts, stats["j"], codes))
    for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:5]:
        ctx.note("  非 200/400 响应 x%d: %s" % (v, k))
    ctx.note("仓外哨兵存活 %d/%d" % (alive, len(canaries)))

    for p_ in (target, tpl_real, tpl_junc):
        try:
            if os.path.lexists(p_):
                if is_reparse_dir(p_):
                    os.rmdir(p_)
                else:
                    force_rmtree(p_)
        except Exception:
            pass

    ck_eq(alive, len(canaries),
         "把 ap **自己**换成联接竟然删到了仓外 %d 个文件(状态码分布 %r)"
         % (len(canaries) - alive, codes))
    ck(os.path.exists(r.abspath(".git")), ".git 没了")
    ck(ctx.srv.alive(), "竞态之后服务不健康")


@test("F", "stage-trails-git-literal", bug="F10-§4.7补充")
def t_f10(ctx):
    """F10 我在穷举 ref / 看 git 行为时发现的: pathspec magic 现在被挡住了,
    但**只对 stage/unstage/discard/diff 挡住**, 对 `/api/file` 的 file 参数没有
    单独说过 —— 这条量一下 file 参数上的 magic 现在是什么下场。

    判据: 请求一个不是文件的东西应当 400 或返回空, 不能变成「stage 了一大片」。
    (server.py 已用 --literal-pathspecs + rel_in_root 修过 F-06, 这里钉住它。)
    """
    r = ctx.repo
    for n in ["a.txt", "b.txt", "deep/c.txt", "keep.md"]:
        r.write(n, "v1\n")
    r.commit("c1")
    for n in ["a.txt", "b.txt", "deep/c.txt"]:
        r.write(n, "modified\n")

    for magic in [":(glob)**/*.txt", ":/", ":(top)", ":(exclude)a.txt",
                  ":(icase)A.TXT", "*.txt", "a.txt"]:
        r.git("reset", "-q", "HEAD", "--")
        code, j = ctx.srv.jpost("/api/stage", {"files": [magic]})
        st = sorted(r.git("diff", "--cached", "--name-only").stdout.split())
        ctx.note("stage %-22r -> %s  实际暂存 %r" % (magic, code, st))
        # 判据: 请求**一个**东西, 最多只能有一个文件进暂存区。
        # magic 串当前被 rel_in_root 当成普通文件名(Windows 上带 ':' 根本不合法,
        # 解析后不存在 -> _live_paths 把它滤掉), 所以正确下场是「无操作」。
        # 唯一不可接受的是「一个 magic 串把整片目录 stage 进去」。
        ck(len(st) <= 1,
           "只要求 stage %r 这**一个**东西, git 实际暂存了 %d 个: %r"
           % (magic, len(st), st))
        ck(not ({"a.txt", "b.txt", "deep/c.txt"} <= set(st)),
           "一个 magic 串 %r 把整片目录都 stage 了: %r" % (magic, st))
        r.git("reset", "-q", "HEAD", "--")

    for magic in [":(glob)**/*.txt", ":/"]:
        code, txt = ctx.srv.raw("GET", "/api/file?file=" + q(magic))
        ctx.note("GET /api/file?file=%r -> %s %r" % (magic, code, txt[:90]))
        ck("v1" not in txt or code == 200, "尺子异常")
        ck(code != 500, "GET /api/file?file=%r 返回 500" % magic)
    ck(os.path.exists(r.abspath(".git")), ".git 没了")
    ck(ctx.srv.alive(), "服务不健康")


@test("F", "api-contract-after-changes", bug="F11-§4.7")
def t_f11(ctx):
    """F11 §4.7 顺带钉一条:并发修改期间 server.py 被换过一版, 这条是**基线**。

    报告里 F-01 / F-02 / F-06 等已被 A1 线修掉(本文件写作时 server.py
    已经带 rel_in_root / .git 拦截 / --literal-pathspecs)。这条测这些新行为
    是否真的成立, 并且**记录 server.py 的 sha256**, 让这份报告的所有结论
    都挂在一个明确的版本上。

    判据是正确行为: 那些形态必须被拒, 且正常路径必须仍然能用
    (别为了堵洞把正常功能堵了 —— report §4.7 提过 reset 护栏, 一起钉住)。
    """
    import hashlib
    with io.open(SERVER_PY, "rb") as f:
        data = f.read()
    ctx.note("server.py sha256=%s size=%d mtime=%s"
             % (hashlib.sha256(data).hexdigest(), len(data),
                time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(os.path.getmtime(SERVER_PY)))))

    r = ctx.repo
    r.write("sub/a.txt", "v1\n")
    r.commit("c1")
    r.write("sub/a.txt", "v2\n")
    r.write("junk.txt", "junk\n")

    # 1) 空串 / 点 必须被拒(F-01 的修法: 解析结果 == root 就拒)
    for f in ["", ".", "./", "..", ".git", ".git/", ".git/config",
              ".Git/Config", "./.git/config", "sub/../.git"]:
        code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": [f], "confirmed": True}))
        ctx.note("discard %-18r -> %-4s %s" % (f, code, txt[:70].replace("\n", " ")))
        ck_eq(code, 400, "discard %r 期望 400" % f)
        ck(os.path.exists(r.abspath(".git")), "discard %r 把 .git 弄没了" % f)
    # .git 大小写折叠也要被拒
    for f in [".GIT", ".GIT/config", ".Git"]:
        code, _txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": [f], "confirmed": True}))
        ck_eq(code, 400, "discard %r 期望 400(大小写折叠的 .git)" % f)

    # 2) 正常路径仍然能用(F-02/F-06 的修法别把功能堵了)
    code, j = ctx.srv.jpost("/api/stage", {"files": ["./sub/a.txt"]})
    ck_eq(code, 200, "stage './sub/a.txt' 应 200(相对写法是正常的)")
    st = sorted(r.git("diff", "--cached", "--name-only").stdout.split())
    ctx.note("stage './sub/a.txt' 实际暂存 %r" % st)
    ck_eq(st, ["sub/a.txt"], "stage './sub/a.txt' 暂存了别的")
    code, j = ctx.srv.jpost("/api/unstage", {"files": ["sub/../sub/a.txt"]})
    ck_eq(code, 200, "unstage 相对写法应 200")
    code, j = ctx.srv.jpost("/api/discard",
                             {"files": ["junk.txt"], "confirmed": True})
    ck_eq(code, 200, "discard 一个未跟踪文件应 200")
    ck(not r.exists("junk.txt"), "junk.txt 没被删")

    # 3) 已跟踪文件用相对写法 discard, 必须是 restore 而不是 unlink
    code, j = ctx.srv.jpost("/api/discard",
                             {"files": ["./sub/a.txt"], "confirmed": True})
    ck_eq(code, 200, "discard 已跟踪文件应 200")
    ck(r.exists("sub/a.txt"), "discard 已跟踪文件把它删了而不是恢复")
    ck_eq(r.read("sub/a.txt"), "v1\n", "discard 已跟踪文件后内容没回到 HEAD")
    ck_eq(r.out("status", "--porcelain").strip(), "", "discard 之后工作区不干净")

    # 4) 目录里含已跟踪文件时, 整目录 discard 必须被拒
    r.write("sub/extra.txt", "new\n")
    code, txt = ctx.srv.raw("POST", "/api/discard",
                                json.dumps({"files": ["sub"], "confirmed": True}))
    ctx.note("discard 含已跟踪文件的目录 sub -> %s %s" % (code, txt[:90].replace("\n", " ")))
    ck_eq(code, 400, "整目录 discard(含已跟踪文件)期望 400")
    ck(r.exists("sub/extra.txt"), "sub/extra.txt 被删了")

    # 5) reset 护栏仍在
    # mode 填 "hard": 判据是下一行「sub/extra.txt 这个未跟踪文件被清掉」——
    # clean -fd 只有 reset --hard 会跑, soft/mixed 都不会删未跟踪文件
    code, j = ctx.srv.jpost("/api/reset",
                             {"confirmed": True, "mode": "hard", "ref": "HEAD"})
    ck_eq(code, 200, "/api/reset 应 200")
    ck(not r.exists("sub/extra.txt"), "/api/reset 没清掉未跟踪文件")
    ck(os.path.exists(r.abspath(".git")), ".git 没了")
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


def _slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def run_one(t, verbose, keep):
    repo = None
    srv = None
    ctx = Ctx()
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
    elif status == "FAIL":
        out = ("FAIL", ctx, ctx.detail)
    else:
        out = ("ERROR", ctx, ctx.detail + "\n" + tb)
    try:
        if srv:
            srv.stop()
        if repo and not keep:
            force_rmtree_any(repo.dir)
    except Exception:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 无头补充测试套件(补 §4 的覆盖空洞)")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条显示细节")
    ap.add_argument("-k", "--filter", default="", help="只跑名字/编号含该串的")
    ap.add_argument("--keep", action="store_true", help="保留临时仓(调试用)")
    ap.add_argument("--list", action="store_true", help="只列出测试名")
    ap.add_argument("--fast", action="store_true",
                    help="跳过两条 scale 测试(建 10 万/4 千个文件, 约 2 分钟)")
    a = ap.parse_args(argv)

    sel = [t for t in TESTS if a.filter.lower() in (t["id"] + " " + t["name"]).lower()]
    if a.fast:
        sel = [t for t in sel if "scale" not in t["name"]]
    if a.list:
        for t in sel:
            print("  %-16s %s%s" % (t["id"], t["name"],
                                     ("   [%s]" % t["bug"]) if t["bug"] else ""))
        return 0
    if not sel:
        print("没有测试匹配 %r" % a.filter)
        return 2

    force_rmtree(TESTROOT)
    os.makedirs(TESTROOT, exist_ok=True)
    import hashlib
    with io.open(SERVER_PY, "rb") as f:
        _sd = f.read()
    print("=" * 78)
    print("  aigit 无头补充测试套件 —— 补 doc/aigit-review.md §4 的覆盖空洞")
    print("  临时仓根: %s" % TESTROOT)
    print("  aigit 源: %s" % AIGIT)
    print("  server.py sha256=%s size=%d" % (hashlib.sha256(_sd).hexdigest(),
                                              len(_sd)))
    print("  python  %s" % sys.version.split()[0])
    print("  git     %s" % (subprocess.run(["git", "--version"],
                                          capture_output=True, text=True).stdout.strip()
                             or "(取不到)"))
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
        line = "  %s %-16s %s" % (mark, t["id"], t["name"])
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
        print("  失败清单(每条都对应 server.py 的一个缺陷):")
        for t, st, d in bad:
            print("    [%s] %-16s %-32s bug=%s"
                  % (st, t["id"], t["name"], t["bug"] or "-"))
            print("           %s" % d.split("\n")[0][:170])
    skips = [(t, d) for t, st, d, _ in results if st == "SKIP"]
    if skips:
        print()
        print("  跳过清单(每条都必须能解释为什么造不出输入):")
        for t, d in skips:
            print("    [SKIP] %-16s %s" % (t["id"], t["name"]))
            for line in d.split("\n")[:6]:
                print("           %s" % line)
    print("=" * 78)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
