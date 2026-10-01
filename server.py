#!/usr/bin/env python3
"""
aigit —— 类 VSCode「源代码管理」面板的本地 git 图形工具 + 随机仓库测试数据生成器。

设计约束:
  * 零第三方依赖,只用 Python 标准库。理由:这工具要在别人机器上直接跑,
    `pip install` 本身就是失败原因。
  * **不开窗口**。启动后只打印一个 URL,由使用者自己打开浏览器。
    理由:本机同时跑着多条开发线,任何自动弹窗都会打断人干活。

安全边界(这是能执行任意 git 操作的工具,必须收紧):
  * 所有文件路径都 resolve 后校验必须在 repo root 内,否则拒绝。
    `discard` 会真的删掉工作区内容,这里放行一个 `..` 就是数据丢失。
  * 所有 git 参数走列表形式,不经 shell。
  * 只 bind 127.0.0.1。

用法:
    python server.py --root C:\\some\\repo
    python server.py --root C:\\tmp\\aigit-demo --sandbox   # 建一个干净的演练仓
"""

import argparse
import contextlib
import difflib
import json
import os
import random
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    # 只用标准库(ctypes 属于标准库), 不引入任何第三方依赖 —— 见文件头的设计约束。
    # 非 Windows 上没有 kernel32, 下面 E4 的「握住目录」会退化到 os.open。
    import ctypes
    from ctypes import wintypes as _wintypes
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
except (ImportError, OSError, AttributeError):      # pragma: no cover - 非 Windows
    ctypes = None
    _wintypes = None
    _K32 = None

# 测试数据生成器已抽成独立模块:它不依赖 HTTP 层,可以不起服务直接单测,
# 也可以被别的工具复用。构造方式与「可合并」的保证见 gen-notes.md。
import gen as _gen
from gen import ScenarioGen, GenError

# 分支前缀的基线值, import 时取一次。Repo.generate 会在调用 gen.py 期间把它
# 临时换成「基线/s<seed>」, 收尾一律还原成**这个常量**(而不是「读到的那个
# 值」)—— 理由见 Repo.generate 的注释。
_GEN_BASE_PREFIX = _gen.BRANCH_PREFIX
# 换前缀必须加锁: 服务是 ThreadingHTTPServer, BRANCH_PREFIX 是进程级全局。
_GEN_LOCK = threading.Lock()

# ---------------------------------------------------------------- 冲突 / 历史
#
# conflicts.py 与 history_ops.py 一直在仓里, 也各有自己的对抗性测试套件
# (test_conflicts.py 14 条 / test_history_ops.py 37 条, 都实跑通过), 但
# **server.py 从来没有 import 过它们** —— 于是 aigit 最有分量的这两块功能
# 在产品里 100% 不可达: 下面挂的这些路由在接线之前全是 404, 只有自测套件
# 在跑。「模块测得再好, 产品里够不着」不算数。
#
# 懒加载: Repo 实例要等 main() 才建, 而这两个对象都需要 root。
import conflicts as _conflicts
import history_ops as _hops

_CONFLICTS = None
_HISTORY = None


def conflict_resolver():
    global _CONFLICTS
    if _CONFLICTS is None:
        _CONFLICTS = _conflicts.ConflictResolver(REPO.root)
    return _CONFLICTS


def history_ops():
    global _HISTORY
    if _HISTORY is None:
        _HISTORY = _hops.HistoryOps(REPO.root)
    return _HISTORY


# -- 查询参数小工具 ----------------------------------------------------------
# int() 解析失败抛 ValueError, 由 do_GET / do_POST 已有的
# `except (ValueError, TypeError, KeyError) -> 400` 兜住, 所以这里不重复包 try。

def _qi(q, key, default):
    """整型查询参数, 缺省用 default。"""
    v = q.get(key, [None])[0]
    return default if v is None or v == "" else int(v)


def _qflag(q, key, default=False):
    v = q.get(key, [None])[0]
    if v is None:
        return default
    return v in ("1", "true", "yes", "on")


def _qopt(q, key):
    """可选的整型参数: 给了就转 int, 没给就是 None(交给下游区分「不限」)。"""
    v = q.get(key, [None])[0]
    return None if v is None or v == "" else int(v)


def _bb(b, key, default=False):
    """请求体里的布尔开关。**只认真 bool**, 其余一律 400。

    为什么这么严: 这些开关里有 `confirmed` —— 一个
    `"confirmed": "false"` 的字符串在 Python 里是**真的**, 而它是「确认执行
    一个不可撤销的操作」的那一位。用 `bool(x)` 收字段等于把
    「前端把 false 序列化成字符串」这个极常见的 bug 变成一次误删。
    """
    v = b.get(key, default)
    if isinstance(v, bool):
        return v
    raise GitError("%s 必须是 true 或 false,收到 %r" % (key, v))


def _history_plan(op, q):
    """GET /api/history/plan/<op> —— 危险操作的只读预演。

    刻意做成一张显式的表而不是 getattr 拼接: op 来自 URL, getattr(ch, op)
    能调到**任何**属性名, 拼错一个字母就从「预演」变成别的意思。
    """
    H = history_ops()
    if op == "stash":
        return H.plan_stash(q.get("message", [None])[0],
                            _qflag(q, "include_untracked"))
    if op == "stash-drop":
        return H.plan_stash_drop(_qi(q, "index", 0))
    if op == "amend":
        return H.plan_amend(q.get("message", [None])[0])
    if op == "undo-last-commit":
        return H.plan_undo_last_commit(_qflag(q, "drop_changes"))
    if op == "reflog-restore":
        return H.plan_restore_from_reflog(_qi(q, "index", 0),
                                          q.get("mode", ["soft"])[0])
    if op == "revert":
        return H.plan_revert(q.get("sha", [None])[0], _qopt(q, "mainline"))
    if op == "branch-rename":
        return H.plan_branch_rename(q.get("old", [None])[0],
                                    q.get("new", [None])[0])
    if op == "branch-delete":
        return H.plan_branch_delete(q.get("name", [None])[0],
                                    _qflag(q, "force"))
    if op == "tag-delete":
        return H.plan_tag_delete(q.get("name", [None])[0])
    raise GitError("没有这个预演:%s" % op)

# ---------------------------------------------------------------- git 封装

class GitError(Exception):
    pass


# 分支名白名单。**首字符必须是 \w** 这半条是关键:
# 少了它,`--detach` / `-f` / `--` / `.` / `-b` 全都由 \w . / - 组成、能过
# `[\w./-]{1,100}`,而 git 会照单全收 —— 游离 HEAD、静默丢弃工作区改动。
# (/api/checkout 与 /api/branch 共用这一套,以前是前者零校验。)
_BRANCH_RE = re.compile(r"[\w][\w./-]{0,99}")
_BRANCH_HINT = ("分支名不合法:只允许字母数字与 _ . / -,"
                "且必须以字母数字或 _ 开头(最长 100)")


def check_branch_name(name):
    """校验分支名。非字符串 / 不合白名单一律拒。"""
    if not isinstance(name, str) or not _BRANCH_RE.fullmatch(name):
        raise GitError(_BRANCH_HINT)
    return name


# `/api/file` 的 ref 白名单(E7)。**首字符不得是 -**, 这半条是关键:
# show_file 把 ref 与路径拼成 argv 的**一个**元素 `git show <ref>:<rel>`,
# 所以 ref="--format=%H" 到 git 那里是 `git show --format=%H:a.txt` ——
# `=` 后面整段(含那个冒号)都被当成选项的值, 选项**真的生效了**。
# 拼 ':' 只能挡住「裸选项」(`-s` / `--stat` 因为认不出选项名而失效),
# 挡不住 `--opt=value` 形态。实测(F4): 注入后 old 从「文件内容」变成
# 「提交 sha + 字面量 :a.txt + 该提交的完整补丁」。
#
# 首字符允许 `@`(要支持 `@{-1}` / `@`)和 `~` / `^`(git 的祖先/派生写法),
# 字符集里没有 `:` —— 那是 `<ref>:<path>` 的分隔符, 出现在 ref 里就说明
# 调用方在越权指定路径。
_REF_RE = re.compile(r"[\w@^~][\w@^~{}+./-]*")
_REF_HINT = ("ref 不合法:只允许 git rev-spec 的常见写法"
             "(字母数字与 _ . / @ ~ ^ { } + -),首字符不能是 -,不能含 : 与空白")


def check_ref_name(ref):
    """校验 rev-spec。非字符串 / 不合白名单一律拒。

    **报错文案里不回显 ref 的值**: ref 是用户输入, 而 F3 专门测了
    `--output=<仓外绝对路径>` 形态 —— 把值回显进响应就等于把仓外路径
    写回给调用方(F3 有一条断言就是「响应里不许出现仓外绝对路径」)。
    """
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise GitError(_REF_HINT)
    if ".." in ref:      # 只可能是区间或穿越, 单文件读取用不上
        raise GitError(_REF_HINT)
    return ref


# ------------------------------------------- E4:让「校验」与「动手」无法分离
#
# 背景(收口清单 E4 / A3 的 F8, 实测 7/7 命中): resolve_in_root 只在动手**之前**
# 看一眼, 中间还夹着 `git checkout` 那个子进程 —— 几十毫秒的窗口。这段时间里
# 把目标的**父目录**换成目录联接, 随后 shutil.rmtree 只查顶层是不是符号链接
# (实测对 `mklink /J` 造的联接它**自己**会拒绝, 但那只管顶层),
# 于是顺着父目录的联接把仓外的文件删掉。
#
# 「破坏前再校验一次」**不是**修法: 那只是把窗口从几十毫秒缩到几微秒, 窗口还在,
# 而且那条路的实测就是有对手配合时必中。真正让它无法分离的是**握住路径链本身**:
# Windows 上用 CreateFileW 打开祖先目录、share 模式里**不给 FILE_SHARE_DELETE**,
# 这个句柄会挡住对它的 rename/删除 —— 实测 os.replace 直接回
# `WinError 32「另一个程序正在使用此文件」`。于是想换祖先目录必须先关掉我们的
# 句柄, 而我们到破坏结束才关: 校验和动手之间没有可交换的窗口。
#
# 实测(三个对手同时跑, 每个 30 个仓外哨兵):
#   换父目录   -> 仓外 30/30 存活, 换成功 2 次 / 被挡 218 次
#   换目标自己 -> 仓外 30/30 存活, 换成功 0 次 / 被挡 179 次(操作被中止, fail closed)
#   换子目录   -> 仓外 30/30 存活, 换成功 0 次 / 被挡 222 次

_REPARSE_POINT = 0x400        # FILE_ATTRIBUTE_REPARSE_POINT
_GENERIC_READ = 0x80000000
_SHARE_RW = 0x1 | 0x2         # FILE_SHARE_READ | FILE_SHARE_WRITE(**不含 DELETE**)
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x02000000     # 开目录句柄必需
_FLAG_OPEN_REPARSE_POINT = 0x00200000   # 句柄指向**联接本身**而不是它的目标
_FILE_ATTRIBUTE_TAG_INFO = 9            # GetFileInformationByHandleEx 的 info class


def is_reparse_point(path):
    """路径**最后一段**是不是 reparse point(目录联接 / 符号链接 / 挂载点)。

    两个坑, 都实测过:
      * **不能用 os.path.islink 当尺子**: Python 3.11 + `mklink /J` 造的目录联接,
        `os.path.islink()` 返回 **False** —— ntpath.islink 只认真正的符号链接,
        而 ntpath 里没有 `_isjunction`(3.12 才加)。
      * **它只对最后一段有效**: 祖先是联接时它会跟着走。实测
        `<repo>/sub` 是联接、`<repo>/sub/victim` 是联接目标里的真目录时,
        `os.lstat('<repo>/sub/victim').st_file_attributes & 0x400` 是 **False**,
        `islink` 也是 False —— 而 `os.listdir` 同一个路径列出来的是**仓外**的文件。
        所以链上每一级都得**单独问**(lstat 逐级问是问得到的, 但那是某一刻的快照,
        随时会过期)。要防竞态就必须问**句柄** —— 链上那一级用
        `_PinnedDir.reparse()`(见那里), 它和 pin 是同一次打开。
    """
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


class _PinnedDir(object):
    """一个被握住的目录。close() 之前没人能 rename 它 / 把它换成联接(Windows)。

    句柄带 FILE_FLAG_OPEN_REPARSE_POINT 打开,所以它指向**那个目录项本身**
    而不是它的目标 —— 问句柄「你是不是 reparse point」得到的是**不会过期**的
    答案,因为我们没给 FILE_SHARE_DELETE, 没人换得掉这个目录项。
    """
    __slots__ = ("h", "_win", "path")

    def __init__(self, h, win, path):
        self.h = h
        self._win = win
        self.path = path

    def reparse(self):
        """问**句柄**: 这个目录项是不是 reparse point。

        为什么不用路径问 —— 见 is_reparse_point 的第二个坑。实测三条路径同一时刻
        (probe_blind.py, 原始输出 p-blind.txt):
            <root>/JUNC         lstat&0x400=True (0x410)  islink=False  句柄=0x410
            <root>/JUNC/victim  lstat&0x400=False(0x10)   islink=False  句柄=0x10
            <root>/real/victim  lstat&0x400=False(0x10)   islink=False  句柄=0x10
        **句柄和 lstat 每一级都一致** —— 句柄并不「看得更远」。它买的是另外两件事:
          * 答案**不会过期**: 目录项在我们手里换不掉(没给 FILE_SHARE_DELETE),
            所以这个 attrs 就是那个目录项本身的属性, 不是某一刻的快照;
          * 判据和 pin 是**同一次打开**拿到的, 中间没有第二次路径查找能被竞态插队。
        """
        if self._win:
            buf = ctypes.create_string_buffer(8)
            try:
                _K32.GetFileInformationByHandleEx.argtypes = [
                    _wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                    _wintypes.DWORD]
                _K32.GetFileInformationByHandleEx.restype = _wintypes.BOOL
                if _K32.GetFileInformationByHandleEx(
                        self.h, _FILE_ATTRIBUTE_TAG_INFO, buf, 8):
                    return bool(int.from_bytes(buf.raw[:4], "little")
                                & _REPARSE_POINT)
            except Exception:
                pass
        return is_reparse_point(self.path)       # 问不到句柄就退回路径判定

    def close(self):
        try:
            if self._win:
                _K32.CloseHandle(_wintypes.HANDLE(self.h))
            else:
                os.close(self.h)
        except Exception:
            pass                      # 收尾阶段的异常不能盖掉真正的结论


def _pin_dir(path):
    """握住一个目录。**失败必须抛** —— 调用方要 fail closed。

    句柄带 FILE_FLAG_OPEN_REPARSE_POINT: 这样即使这个目录**此刻是联接**,
    句柄也指向联接本身而不是它的目标, 于是「它自己的名字」也被护住了
    (换掉这个名字需要对**它**开 DELETE 权限, 而我们没给 FILE_SHARE_DELETE)。
    """
    if _K32 is not None:
        try:
            _K32.CreateFileW.restype = _wintypes.HANDLE
            _K32.CreateFileW.argtypes = [
                _wintypes.LPCWSTR, _wintypes.DWORD, _wintypes.DWORD,
                ctypes.c_void_p, _wintypes.DWORD, _wintypes.DWORD, _wintypes.HANDLE]
            h = _K32.CreateFileW(str(path), _GENERIC_READ, _SHARE_RW, None,
                                 _OPEN_EXISTING,
                                 _FLAG_BACKUP_SEMANTICS | _FLAG_OPEN_REPARSE_POINT,
                                 None)
        except OSError as e:
            raise GitError("无法握住目录 %s(%s),已拒绝动手 —— "
                           "握不住就没法保证删除不越界" % (path, e))
        if h == _wintypes.HANDLE(-1).value or h is None:
            # 注意 Windows 会把「正被占用」映射成各种异常类
            # (实测这里出现过 BrokenPipeError), 所以只认 OSError 这一个基类。
            raise GitError("无法握住目录 %s(WinError %s),已拒绝动手 —— "
                           "握不住就没法保证删除不越界"
                           % (path, ctypes.get_last_error()))
        return _PinnedDir(h, True, str(path))
    # POSIX: os.open 目录**挡不住** rename(POSIX 语义就允许), 那边靠
    # _recheck_resolved 的重新校验兜底。这里仍然握住, 因为它能让 O_DIRECTORY
    # 在打开时就拒掉非目录。
    try:
        return _PinnedDir(
            os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
            False, str(path))
    except OSError as e:
        raise GitError("无法握住目录 %s(%s),已拒绝动手" % (path, e))


def as_file_list(files):
    """把请求里的 files 字段收成「字符串数组」。类型不对就 400,别让它流到 git。"""
    if files is None:
        return []
    if not isinstance(files, list):
        raise GitError("files 必须是字符串数组,收到 %s" % type(files).__name__)
    out = []
    for f in files:
        if not isinstance(f, str):
            raise GitError("files 里每一项都必须是字符串,收到 %s"
                           % type(f).__name__)
        out.append(f)
    return out


class Repo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lock = threading.Lock()

    # -- 底层 ------------------------------------------------------------
    def _run_full(self, args, check=True, stdin=None):
        """跑一条 git,返回 CompletedProcess。

        要看 returncode / stderr 的地方必须用它 —— 之前 _run 只返回 stdout(str),
        于是 merge() 写 p.returncode 直接 AttributeError 500。
        """
        with self._lock:
            p = subprocess.run(
                ["git", "-C", str(self.root)] + args,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", input=stdin,
                env={**os.environ, "GIT_PAGER": "cat",
                     "GIT_TERMINAL_PROMPT": "0"},
                timeout=120,
            )
        if check and p.returncode != 0:
            raise GitError((p.stderr or p.stdout or "git 失败").strip())
        return p

    def _run(self, args, check=True, stdin=None):
        """只要 stdout 的场合用它(保持原语义,免得改一堆调用点)。"""
        return self._run_full(args, check=check, stdin=stdin).stdout

    def _run_paths(self, head, rels):
        """把一批路径喂给 git。**从 stdin 读, 不拼命令行**(E8)。

        为什么不能继续拼: `git add -- a b c` 是把文件名**拼在一条命令行**里传的,
        而 Windows 命令行有 32K 上限。实测文件名共 **32999 字节**时整条
        `POST /api/stage` 报 `FileNotFoundError: [WinError 206] 文件名或扩展名太长`
        —— 命令行太长**根本起不来** git, 所以报的是 FileNotFoundError 而不是
        git 的错误。门槛是**字节数不是文件数**: 本次实测的文件名 11 字符所以落在
        3000 个; 路径长一点(`src/main/resources/...`)两三千个就能炸。

        改用 `--pathspec-from-file=- --pathspec-file-nul` 之后命令行长度与批数
        无关, 而且是**一次调用**, 不存在「前几批成功、后一批失败」的半截状态
        (分批会有那个问题, 而 stage 半截成功比整批失败更难解释)。

        两条实测过的语义保证(不是推测, 见 doc/aigit-E3-server-fixes.md §5):
          * `--literal-pathspecs` 在这个模式下**仍然生效** —— 传 `*.txt` 时
            rc=128 `did not match any files`; 去掉它同一个输入 staged 了 3 个文件。
            所以 A1 的 F-06 修复(pathspec magic 挡不住)没有被这次改动削弱。
          * 已跟踪但刚被删掉的文件照样能加回来 —— 与 `add --` 一致。
        """
        if not rels:
            return None
        return self._run_full(
            ["--literal-pathspecs"] + head
            + ["--pathspec-from-file=-", "--pathspec-file-nul"],
            stdin="\0".join(rels) + "\0")

    def exists(self):
        return (self.root / ".git").exists()

    def init(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._run(["init", "-b", "main"])
        self._run(["config", "user.email", "aigit@localhost"])
        self._run(["config", "user.name", "aigit"])
        self._run(["config", "commit.gpgsign", "false"])
        # 避免 CRLF 干扰 diff 展示
        self._run(["config", "core.autocrlf", "false"])

    # -- 路径校验(关键安全点) --------------------------------------------
    def resolve_in_root(self, rel):
        """把仓库相对路径解析成绝对路径,并拒绝任何越界的输入。

        拒绝分三类, 少一类就是一个能丢数据的洞:
          1. 越界(带 .. / 绝对路径 / 顺着符号链接或目录联接走出去);
          2. **解析结果恰好是 root 自己**(rel 为 '' / '.' / './' / '.git/..')。
             它 relative_to(root) 是成功的, 但它是个**目录** —— discard 对目录
             是 shutil.rmtree, 放行等于一个请求把整个仓库连 .git 抹掉;
          3. root/.git 及以下。那不是工作区文件, 是 git 自己的数据;
             discard('.git') 同样是「一个请求毁掉整个仓库」。
        """
        if rel is None:
            raise GitError("路径为空")
        if not isinstance(rel, str):
            raise GitError("路径必须是字符串,收到 %s" % type(rel).__name__)
        try:
            p = (self.root / rel).resolve()
        except (ValueError, OSError) as e:
            # 典型: 内嵌 NUL 字符 -> ValueError: stat: embedded null character
            raise GitError("路径不合法,已拒绝:%r(%s)" % (rel, e))
        try:
            p.relative_to(self.root)
        except ValueError:
            raise GitError(f"路径越界,已拒绝:{rel}")
        if p == self.root:
            raise GitError(f"路径指向仓库根目录本身,已拒绝:{rel}")
        if p.relative_to(self.root).parts[0].lower() == ".git":
            raise GitError(f"不允许操作 .git,已拒绝:{rel}")
        return p

    def rel_in_root(self, rel):
        """resolve_in_root 的姊妹函数: 额外返回**规范化相对路径**(git 认的那种写法)。

        为什么不能直接把请求原串交给 git:
          * 校验用的是 resolve 之后的绝对路径, 而 git 收到的是原串 ——
            同一个文件两种写法, 比对 ls-files 时就判成「未跟踪」(F-02);
          * 原串里的 pathspec magic(`:(glob)` / `:/` / `:(exclude)`)会被 git
            解释成模式而不是文件名, 于是一个请求 stage 掉一整片(F-06)。
        返回的 rel 已经过 resolve 规约, 交给 git 前再加 -- 就能当字面名用。
        """
        return self.resolve_in_root(rel).relative_to(self.root).as_posix()

    def _live_paths(self, rels, tracked):
        """滤掉「既不在工作区、也没被跟踪」的路径。

        这不是洁癖: git 遇到一个都没匹配上的 pathspec 会 rc=128 直接失败,
        整个请求就 500 了。丢掉不存在的路径是「无操作」而不是「报错」,
        也正好挡住 pathspec magic —— magic 串在磁盘上不可能存在。
        但**不能**只按「文件存在」过滤: 已跟踪但刚被删掉的文件必须还能
        stage(那正是要把这次删除暂存上去), 所以 tracked 也要留着。
        """
        return [r for r in rels
                if (self.root / r).exists() or (self.root / r).is_symlink()
                or r in tracked]

    # -- 查询 ------------------------------------------------------------
    def _head_branch(self):
        """报当前分支名。三种状态都必须报得出来, 任何一种失败都不能让接口 400。

        E3 的根因就在这一行: 旧代码用 `rev-parse --abbrev-ref HEAD` 且 check=True。
        未出生 HEAD(用户自己 git init 过但还没 commit —— 很常见的现场)时它
        **rc=128**, 于是: `GET /api/status` 400, 并且**每个 POST 都 400**,
        因为每个 POST 结尾都要 REPO.status() —— 而那时操作**已经做完并生效**
        了(stage 把文件暂存了、commit 把提交写进历史了)。客户端拿到 400 会
        **重试**, 于是同一个操作被执行两次。那是 E3 里最严重的一面。

        三种状态实测(本机 git 2.50.1):
                             symbolic-ref --short HEAD   rev-parse --abbrev-ref HEAD
            未出生 HEAD      rc=0  'main'                 rc=128 fatal: ambiguous argument
            正常             rc=0  'main'                 rc=0    'main'
            游离 HEAD        rc=128 fatal: not symbolic    rc=0    'HEAD'
        所以反过来: 先试 symbolic-ref(它管未出生), 失败再退回 rev-parse
        (它管游离 HEAD), 两个都失败才落到哨兵值。两个都不抛 —— 拿分支名
        失败**不该**让整个状态接口变成错误。
        """
        p = self._run_full(["symbolic-ref", "--short", "HEAD"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
        p = self._run_full(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
        return "(no branch)"

    def status(self):
        """返回 VSCode 风格的三段状态。"""
        out = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        staged, unstaged, conflicted = [], [], []
        for x, y, path in self.porcelain_z_parse(out):
            if x == "?" and y == "?":
                unstaged.append({"path": path, "status": "?",
                                 "staged": False})
                continue
            item = {"path": path, "staged": True}
            if x not in (" ", "?"):
                staged.append(dict(item, status=x))
            if y not in (" ", "?"):
                unstaged.append(dict(item, status=y, staged=False))
            if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
                conflicted.append({"path": path, "status": "U"})

        branch = self._head_branch()
        staged_paths = [i["path"] for i in staged]
        return {
            "branch": branch,
            "root": str(self.root),
            "staged": staged,
            "unstaged": unstaged,
            "conflicted": conflicted,
            "hasStaged": bool(staged_paths),
        }

    def porcelain_z_parse(self, out):
        """解析 `git status --porcelain=v1 -z` 的输出, 返回 (x, y, path) 列表。

        -z 模式下 rename/copy 的**原路径是一个独立的 NUL 分隔字段**:
            'R  new_name.txt\\x00old_name.txt\\x00'
        不是同一行里的 'old -> new' —— 那是**非** -z 模式的写法, 在 -z 输出里
        永远不会出现(所以拿 " -> " 去 split 是死代码)。这里少消费一个字段,
        原路径就会被当成一条全新 entry: e[0]/e[1] 从 'old_name' 里切出状态码,
        路径只剩 '_name.txt', 凭空多一条垃圾记录, 还把 hasStaged 弄成 true。

        顺带说明: 拷贝(C)与重命名(R)同形, 所以一起处理。
        """
        fields = [f for f in (out or "").split("\0") if f]
        rows = []
        i = 0
        while i < len(fields):
            e = fields[i]
            i += 1
            if len(e) < 3:
                continue
            x, y, path = e[0], e[1], e[3:]
            if x in ("R", "C"):
                # 原路径字段紧跟在后面, 一起吃掉
                if i < len(fields):
                    i += 1
            rows.append((x, y, path))
        return rows

    def diff(self, rel, staged=False):
        args = ["--literal-pathspecs", "diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        args += ["--", rel]
        return self._run(args, check=False)

    def show_file(self, rel, ref="HEAD"):
        check_ref_name(ref)
        p = self._run(["show", f"{ref}:{rel}"], check=False)
        return None if p == "" else p

    def staged_file(self, rel):
        """读**索引里**(暂存区)的版本: `git show :<path>`。"""
        p = self._run(["show", f":{rel}"], check=False)
        return p or None

    def log(self, limit=30):
        fmt = "%H\x1f%an\x1f%ad\x1f%s\x1e"
        out = self._run(
            ["log", f"-{limit}", f"--pretty=format:{fmt}", "--date=short"],
            check=False)
        rows = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            parts = rec.split("\x1f")
            if len(parts) == 4:
                rows.append({"sha": parts[0][:8], "author": parts[1],
                             "date": parts[2], "subject": parts[3]})
        return rows

    def branches(self):
        out = self._run(
            ["for-each-ref", "--format=%(refname:short)\x1f%(HEAD)",
             "refs/heads"], check=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name, head = (line.split("\x1f") + [""])[:2]
            rows.append({"name": name, "current": head.strip() == "*"})
        return sorted(rows, key=lambda r: r["name"])

    # -- 变更操作 --------------------------------------------------------
    def _tracked_set(self):
        ls = self._run(["ls-files", "-z"], check=False).split("\0")
        return {x for x in ls if x}

    def stage(self, files):
        if not files:
            return
        tracked = self._tracked_set()
        rels = self._live_paths([self.rel_in_root(f) for f in files], tracked)
        if not rels:
            return
        self._run_paths(["add"], rels)

    def unstage(self, files):
        if not files:
            return
        tracked = self._tracked_set()
        rels = self._live_paths([self.rel_in_root(f) for f in files], tracked)
        if not rels:
            return
        self._run_paths(["reset", "HEAD"], rels)

    # -- E4:校验与破坏合成一步 --------------------------------------------
    @contextlib.contextmanager
    def _pinned(self, targets):
        """握住每个目标的祖先目录链: 从 repo root 往下, 含 root。

        targets 是 `[(路径, 是否连目标自己一起握住), ...]`。yield 出去的是
        按 root → 最深 排好的句柄列表, 供 _assert_pinned_plain 在**握住的状态下**
        逐级判定。

        握的是**路径链本身**, 不是「上次校验的结果」。所以在校验和动手之间
        没有任何东西能把这一段换成联接 —— 想换就得先关掉我们的句柄, 而我们
        到破坏结束才关。
        """
        root = str(self.root)
        rk = os.path.normcase(root)
        pins, seen = [], set()
        try:
            for t, with_self in targets:
                cur = str(t) if with_self else os.path.dirname(str(t))
                chain = []
                while True:
                    chain.append(cur)
                    if os.path.normcase(cur) == rk:
                        break
                    up = os.path.dirname(cur)
                    if up == cur:
                        raise GitError("路径不在仓库内,已拒绝:%s" % t)
                    cur = up
                for d in reversed(chain):
                    k = os.path.normcase(d)
                    if k in seen:
                        continue
                    seen.add(k)
                    pins.append(_pin_dir(d))
            yield pins
        finally:
            for p in reversed(pins):
                p.close()

    def _assert_pinned_plain(self, pins):
        """链上每一级都必须是**真目录**。**问句柄**, 不问路径。

        问路径是不行的 —— 祖先是联接时 lstat / islink 都会跟着走(见
        is_reparse_point 第二个坑, 实测那条路径上 lstat 报 0x400=False)。
        而句柄是带 FILE_FLAG_OPEN_REPARSE_POINT 握住的, 它指向那个目录项本身,
        又因为没给 FILE_SHARE_DELETE 所以没人换得掉它 —— 这个判定**不会过期**。
        """
        root = str(self.root)
        for p in pins:
            if p.reparse():
                raise GitError(
                    "路径上有目录联接/符号链接,已拒绝动手:%s"
                    % os.path.relpath(p.path, root).replace(os.sep, "/"))

    def _recheck_resolved(self, pairs):
        """动手前**重新** resolve 一遍, 要求结果仍是同一批绝对路径。

        这一层抓的是「中途被换成了仓内的**另一个**位置」—— 仍在仓内、不越界,
        但删掉的不是用户点的那一个, 一样该拒。链上每一级是不是真目录由
        _assert_pinned_plain 在握住的状态下判(问句柄), 不在这里判。
        """
        for rel, abs_p in pairs:
            again = self.resolve_in_root(rel)
            if os.path.normcase(str(again)) != os.path.normcase(str(abs_p)):
                raise GitError("路径在校验与动手之间被换掉了,已拒绝:%s" % rel)

    def _delete_tree(self, abs_p):
        """删一棵目录树。**不用 shutil.rmtree**。

        rmtree 有三个问题(E4):
          1. 它只查顶层是不是符号链接, 不查父链;
          2. 它的「查」和「删」之间也有窗口;
          3. 它的检查走的是路径语义, 祖先是联接时根本查不到(见上面两条)。
        做法: 自顶向下, 每下降一级就把那个目录也握住, 在**握住的状态下**用句柄
        判它是不是真目录, 然后把内容逐个删干净; 最后**先松手再 rmdir** 那个已经
        空了的目录 —— 此时它是空的, rmdir 就算作用在一个联接上也只会删掉这个
        **空的目录项本身**, 删不到任何别人的数据。那一下是整个算法里唯一的窗口,
        而它的后果上界就是「删掉一个空目录项」。

        树里出现联接 / 符号链接一律**拒**, 不删。这比 rmtree 严格 —— rmtree 会
        顺手 unlink 掉它遇到的符号链接。能走到这一步的目录必然不含已跟踪文件
        (discard 里那道 inner 检查), 所以被拒的只可能是未跟踪的链接,
        宁可让人手动处理。
        """
        with self._pinned([(abs_p, True)]) as pins:
            self._assert_pinned_plain(pins)
            for name in sorted(os.listdir(str(abs_p))):
                child = os.path.join(str(abs_p), name)
                if is_reparse_point(child):
                    raise GitError("树里有目录联接/符号链接,已拒绝删除:%s" % child)
                if os.path.isdir(child):
                    self._delete_tree(child)
                else:
                    os.unlink(child)
        os.rmdir(str(abs_p))

    def discard(self, files):
        """丢弃工作区改动。这是破坏性操作 —— 先全部校验,再统一动手。

        判「已跟踪 / 未跟踪」用的是 resolve 之后的**规范化相对路径**,
        不是请求原串。原串 './a.txt'、'sub/../a.txt' 与 ls-files 里的 'a.txt'
        对不上, 于是同一个已跟踪文件被判成未跟踪, 走 unlink 分支 ——
        用户点「丢弃更改」得到的是「文件消失」(F-02)。

        E4: 校验与破坏**合成一步**。破坏期间祖先目录链被握住(见 _pinned),
        所以不存在「校验完再动手」那个可交换的窗口; 链上每一级在握住的状态下
        用**句柄**判是不是真目录(见 _assert_pinned_plain)。顺序是:
            全量校验 → git checkout(restore) → **握住祖先链 → 句柄判链 + 重新 resolve** → 破坏
        restore 必须在握住**之前**跑: git 自己要在仓根里 rename 文件, 而被握住的
        目录是不给 FILE_SHARE_DELETE 的。
        """
        if not files:
            return
        tracked = self._tracked_set()
        restore, wipe = [], []
        for f in files:
            # 用 resolve 之后的那个绝对路径本身, 不用 self.root / rel 重新拼 ——
            # 「校验过的」和「要删的」必须是同一个对象, 而不是两个算出来的同名字符串。
            abs_p = self.resolve_in_root(f)
            rel = abs_p.relative_to(self.root).as_posix()
            if rel in tracked:
                restore.append(rel)
                continue
            is_dir = abs_p.is_dir()
            if is_dir:
                # 目录自己不会出现在 ls-files 里, 但里面可能有已跟踪文件。
                # 直接 rmtree 会把它们从工作区删掉(git 视角变成 deleted)。
                # 「丢弃一个目录」不是标准操作, 宁可拒绝, 不能顺手删掉已跟踪文件。
                inner = sorted(t for t in tracked if t.startswith(rel + "/"))
                if inner:
                    raise GitError("目录内含已跟踪文件,拒绝整目录丢弃:%s"
                                   "(%d 个,例如 %s)" % (rel, len(inner), inner[0]))
            wipe.append((rel, abs_p, is_dir))
        if restore:
            self._run_paths(["checkout"], restore)
        if not wipe:
            return
        # 这里只握**祖先目录**, 一律不握目标自己。两个原因, 都是实测出来的:
        #   * 目录: _delete_tree 结尾要对它 os.rmdir, 而被握住的目录项没有
        #     DELETE 权限 -> WinError 32, 整个请求 500。
        #   * 单文件: 接下来要 unlink 它, 同样被自己的 pin 挡住 -> WinError 32。
        # 不握自己并不降低安全性:
        #   * 祖先链被握住 => 没人能顺着联接把删除引到仓外(E4 要挡的就是这个);
        #   * unlink 一个被换成符号链接的文件只删链接本身, 不碰它的目标;
        #   * 目录目标的「握自己 + 判链 + 删子项 + 松手 + rmdir」整套在
        #     _delete_tree 里, 那里握完用完就放, 顺序上正好卡住 rmdir 之前的窗口。
        with self._pinned([(ap, False) for _, ap, _ in wipe]) as pins:
            self._assert_pinned_plain(pins)
            self._recheck_resolved([(r, a) for r, a, _ in wipe])
            for rel, abs_p, is_dir in wipe:
                if is_dir:
                    self._delete_tree(abs_p)
                elif abs_p.exists() or abs_p.is_symlink():
                    abs_p.unlink()

    def escaping_links(self, limit=16):
        """列出**在仓里、但指向仓外**的 reparse point(目录联接 / 符号链接 / 挂载点)。

        为什么需要它:`resolve_in_root` 只挡得住**调用方点名**要走的路径。`commit()`
        走的是裸 `git add -A`, 路径根本不由我们提供 —— 而 git 自己会**穿过**目录联接。
        实测(本机 git 2.50.1.windows.1, mklink /J):

            <root>/escape -> <root>/../outside
            git add -A ; git ls-files   ->  a.txt  escape/leak.txt
            git show HEAD:escape/leak.txt -> rc=0, 内容就是仓外那份

        也就是说「显式请求 escape/leak.txt」会被 400 拒掉, 但「提交一次」就把
        仓外文件**复制**进了对象库, 而且永远留在历史里、随 push 一起走。
        git 对被跟踪路径上的联接一律按**普通文件**处理, 没有任何开关能关掉这个行为。

        遍历方式:
          * 逐个 dirent 问 `st_file_attributes & REPARSE_POINT`, 不走 `os.path.islink`
            —— 原因见 `is_reparse_point` 的 docstring(Python 3.11 对 mklink /J
            返回 False)。
          * **一律不往 reparse point 里递归**: 它可能在仓外(那就等于把遍历本身
            变成读仓外), 也可能成环。
          * 不进 `.git`: 那里没有用户文件, 而且可能很大。
          * 只往下走**普通目录**, 所以链上每一级的祖先都是仓内真目录 ——
            `is_reparse_point`「祖先是联接时会跟着走」那个坑在这里不成立。
        """
        found = []
        stack = [self.root]
        while stack and len(found) < limit:
            d = stack.pop()
            try:
                it = os.scandir(str(d))
            except OSError:
                continue
            with it:
                for e in it:
                    if e.name == ".git":
                        continue
                    try:
                        # follow_symlinks=False: 问的是**这个目录项本身**。
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                        try:
                            tgt = Path(os.path.realpath(e.path))
                            tgt.relative_to(self.root)
                        except ValueError:
                            found.append((e.path, str(tgt)))
                        except OSError:
                            pass
                        continue                      # 联接一律不再往下走
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        return found

    def commit(self, message):
        if not isinstance(message, str) or not message.strip():
            raise GitError("提交信息不能为空")
        leaks = self.escaping_links()
        if leaks:
            names = ", ".join("%s -> %s" % (p, t) for p, t in leaks[:8])
            raise GitError(
                "仓里有指向仓外的链接(%s),已拒绝提交。git add -A 会穿过目录联接,"
                "把仓外的文件收进索引、再写进历史, 且删掉链接也删不掉历史。"
                "请先把它移出工作区, 或把它移进仓内。共发现 %d 个。" % (names, len(leaks)))
        self._run(["add", "-A"])
        out = self._run(["commit", "-m", message], check=False)
        if not out.strip():
            raise GitError("没有可提交的改动(或提交被 hook 拒绝)")
        return out.strip()

    def checkout(self, name):
        check_branch_name(name)
        # 白名单只挡住了**选项**那一半。哈希前缀那一半它挡不住:
        #   'f404472b' 全是 \w, 满足 `[\w][\w./-]{0,99}`, 而
        #   `git checkout f404472b` 会把仓库推到**游离 HEAD** ——
        # 与 F-04 描述的危害一字不差(工作区改动被静默丢弃, aigit 自己的
        #   /api/status 开始报 branch: "HEAD")。
        # 实测(test_aigit_web.py F-04): 40 位全 sha、sha[:8]、sha[:7] 三个值
        # 全部 HTTP 200 且事后 branch 变成 'HEAD'。
        # 规格 doc/aigit-review.md:201 写的是「**这类**值应被拒绝」,
        # 而 :691 的修法表只给了「加白名单」—— 白名单表达不了
        # 「必须是一个真实存在的分支」。所以这里补第二道:
        # /api/checkout 的语义是**切分支**, 那就只接受已存在的本地分支。
        # show-ref --verify 只认 refs/heads/<name> 真的存在, 不做任何 rev 解析,
        # 所以短 sha / 标签 / HEAD@{1} 都不会被它认成分支。
        if self._run_full(["show-ref", "--verify", "--quiet",
                           "refs/heads/" + name], check=False).returncode != 0:
            raise GitError("分支不存在,已拒绝 checkout:%s" % name)
        self._run(["checkout", name])

    def new_branch(self, name):
        check_branch_name(name)
        self._run(["checkout", "-b", name])

    def merge(self, name):
        if not isinstance(name, str) or not name.strip():
            raise GitError("要合并的分支名不能为空")
        p = self._run_full(["merge", "--no-edit", name], check=False)
        return {"ok": p.returncode == 0,
                "output": ((p.stdout or "") + (p.stderr or "")).strip(),
                "conflicted": self._run(
                    ["diff", "--name-only", "--diff-filter=U"],
                    check=False).strip()}

    def abort_merge(self):
        self._run(["merge", "--abort"], check=False)

    def hard_reset(self):
        self._run(["reset", "--hard", "HEAD"], check=False)
        self._run(["clean", "-fd"], check=False)

    # -- 测试数据生成 ----------------------------------------------------
    def generate(self, kind, count=3, seed=None):
        """生成随机仓库状态。kind: mergeable / unrelated / adddel / conflict / all

        实现已搬到 gen.py(不依赖 HTTP 层)。这里只做转发 + 返回值整形:
        旧前端吃的是 ``branches=[{file, branches, expect}]`` 这个形状,
        映射一份回去免得改前端;``scenarios`` 里是更详细的每场景信息。

        分支名里这一段 seed 是这里加的, gen.py 一个字没改(F-09 / D8):
        gen.py 自己算的名字是 ``aigit_gen/<kind>/<index>/<a|b>``, **不含 seed**,
        所以同一个仓连着生成两次必然撞同一堵墙, 而它那句「或换 seed」是条
        **错 advice** —— 用户照做只会再撞一次。
        干净的做法是给 gen.py 的 ``_branch_names`` 加 seed 参数, 但那要改 gen.py;
        这里用 gen.py 已经留出来的口子: ``BRANCH_PREFIX`` 是模块级常量
        (gen.py:45), 而 ``_branch_names`` 在**每次调用时**重新读它
        (gen.py:370) —— 所以在调用前后临时换掉它, 就等于换掉了分支命名规则。

        三处不能省的细节:
          * **seed 必须在这里定**: gen.py 是在 generate() **内部**才随机
            (gen.py:533), 那时 _branch_names 已经算完了, 事后无从得知前缀。
          * **加锁是必需的, 不是装饰**: 服务是 ThreadingHTTPServer
            (server.py:1004), 两个请求会同时进来。BRANCH_PREFIX 是**进程级**
            全局, 不锁就会串味。
          * **还原时写死回 ``_GEN_BASE_PREFIX``, 不能写回「读到的那个值」**:
            读操作必须也在锁内, 否则先到的请求还原成自己那版时会把后到请求
            的前缀覆盖掉, 攒出 ``aigit_gen/s1/s2/...`` 这种套娃分支名
            (实测踩过: 见 doc/aigit-close-3-failures.md §2.4)。
            还原成固定的基线常量, 无论读到过什么都是幂等的。
        """
        if seed is None:
            seed = random.randrange(1 << 30)
        with _GEN_LOCK:
            _gen.BRANCH_PREFIX = "%s/s%s" % (_GEN_BASE_PREFIX, seed)
            try:
                out = ScenarioGen(self.root).generate(kind, count, seed)
            finally:
                _gen.BRANCH_PREFIX = _GEN_BASE_PREFIX
        out["branches"] = [
            {"file": ", ".join(sc["files"]),
             "branches": list(sc["branches"]),
             "expect": sc["expect"],
             "id": sc["id"],
             "verified": sc["verified"]}
            for sc in out["scenarios"]
        ]
        out["note"] = out["summary"]
        return out

    def clean_generated(self):
        """收拾掉生成器自己造的东西(只删 aigit_gen/ 分支与 aigit_gen_ 文件)。"""
        return ScenarioGen(self.root).clean()


# ---------------------------------------------------------------- HTTP

REPO: Repo = None
WEB: Path = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[aigit] " + (fmt % a) + "\n")

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        b = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(b, dict):
            # 合法 JSON 但不是对象("x" / 123 / null / [1,2]) —— 一样是坏请求
            raise GitError("请求体必须是 JSON 对象,收到 %s" % type(b).__name__)
        return b

    def _q(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _ok_with_status(self):
        """操作成功之后的响应。**取不到 status 时不能报失败**(E3)。

        这是 E3 的一般形态, 不只是「未出生 HEAD」那一种: 每个 POST 结尾都要
        REPO.status(), 所以只要 status() 抛错, 一个**已经做完**的操作就会被
        报成 400。客户端拿到 400 的标准反应就是重试 —— 于是同一个破坏性操作
        被执行两次(stage 两次、commit 两次)。

        根因(E3 本身)已经修在 `Repo._head_branch`: 三种 HEAD 状态都报得出来。
        这一层是防线: 万一将来 status() 又因为别的原因失败, 宁可回一个
        明确标注「状态取不到」的 200, 也不要把已完成的动作报成失败。
        """
        try:
            st = REPO.status()
        except Exception as e:
            return self._json({"ok": True,
                               "branch": None,
                               "staged": [], "unstaged": [], "conflicted": [],
                               "hasStaged": False,
                               "root": str(REPO.root),
                               "statusError": "%s: %s" % (type(e).__name__, e)},
                              200)
        return self._json({"ok": True, **st}, 200)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._q()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            # 静态文件**只按白名单发**, 不拼路径也不扫目录。
            # 冲突演示页以前只存在于 test_aigit_web.py 自带的那个静态服务器里,
            # 产品里打不开 —— 那些 `/api/conflict/*` 接口接上之后它才有意义,
            # 接上却不给人看等于没接。
            if path == "/conflicts-demo.html":
                return self._send(200, (WEB / "conflicts-demo.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if path == "/virtuallist.js":
                return self._send(200, (WEB / "virtuallist.js").read_bytes(),
                                  "application/javascript; charset=utf-8")
            if path == "/api/status":
                return self._json({"ok": True, **REPO.status()})
            if path == "/api/log":
                return self._json({"ok": True,
                                   "log": REPO.log(int(q.get("limit", ["30"])[0]))})
            if path == "/api/branches":
                return self._json({"ok": True, "branches": REPO.branches()})
            if path == "/api/diff":
                f = q.get("file", [None])[0]
                if f is None:
                    raise GitError("缺少 file 参数")
                staged = q.get("staged", ["0"])[0] in ("1", "true", "yes")
                rel = REPO.rel_in_root(f)
                text = REPO.diff(rel, staged=staged)
                return self._json({"ok": True, "path": rel, "staged": staged,
                                   "diff": text})
            if path == "/api/file":
                # 读文件当前内容,用于左右分栏视图
                f = q.get("file", [None])[0]
                if f is None:
                    raise GitError("缺少 file 参数")
                rel = REPO.rel_in_root(f)
                ap = REPO.root / rel
                # E23: 目录不是文件。`git show HEAD:<dir>` 会打印一棵 **tree 清单**
                # (实测 old 变成 'tree HEAD:sub\n\nb.txt\ndeep/\n'), 于是 old 字段里
                # 出现的是目录列表而不是文件内容 —— 前端拿它做左右分栏会渲染出
                # `tree HEAD:sub` 这种东西, 而且接口对「这不是文件」**完全没有
                # 类型判断**。请求一个目录就该 400, 不该拿 tree 清单当「旧版内容」。
                if ap.is_dir():
                    raise GitError("不是文件(是目录),已拒绝:%s" % rel)
                cur = ap.read_text(encoding="utf-8", errors="replace") \
                    if ap.exists() and ap.is_file() else None
                ref = q.get("ref", ["HEAD"])[0]
                old = REPO.show_file(rel, ref)
                if old is None and ref == "HEAD":
                    # HEAD 里没有(比如刚 add 还没 commit),退而读暂存区的版本
                    old = REPO.staged_file(rel)
                return self._json({"ok": True, "path": rel,
                                   "old": old, "new": cur})
            if path == "/api/health":
                return self._json({"ok": True, "root": str(REPO.root),
                                   "isRepo": REPO.exists()})
            # -- 冲突解决(接线自 conflicts.py)-------------------------
            #
            # 路径参数一律先过 `rel_in_root`(即 resolve_in_root):冲突文件是
            # 用户点出来的, 但「点出来」不等于「可以越界」——.git 拦截与联接
            # 拦截在这里同样生效, 不能因为换了个模块就少一道。
            if path == "/api/conflict/state":
                return self._json(conflict_resolver().merge_state())
            if path == "/api/conflict/list":
                return self._json(conflict_resolver().list_conflicts())
            if path == "/api/conflict/inspect":
                f = q.get("file", [None])[0]
                if f is None:
                    raise GitError("缺少 file 参数")
                rel = REPO.rel_in_root(f)
                return self._json(conflict_resolver().inspect(rel))
            # -- 历史 / 危险操作(接线自 history_ops.py)----------------
            if path == "/api/history/stashes":
                return self._json(history_ops().list_stashes(
                    _qi(q, "limit", 50)))
            if path == "/api/history/stash":
                return self._json(history_ops().stash_show(
                    _qi(q, "index", 0), _qflag(q, "stat")))
            if path == "/api/history/reflog":
                return self._json(history_ops().list_reflog(
                    _qi(q, "limit", 30)))
            if path == "/api/history/tags":
                return self._json(history_ops().list_tags(
                    _qi(q, "limit", 100)))
            if path == "/api/history/blame":
                f = q.get("file", [None])[0]
                if f is None:
                    raise GitError("缺少 file 参数")
                rel = REPO.rel_in_root(f)
                return self._json(history_ops().blame(
                    rel, _qopt(q, "start"), _qopt(q, "end"),
                    _qi(q, "limit", _hops.DEFAULT_BLAME_LINES)))
            if path == "/api/history/file":
                f = q.get("file", [None])[0]
                if f is None:
                    raise GitError("缺少 file 参数")
                rel = REPO.rel_in_root(f)
                return self._json(history_ops().file_history(
                    rel, _qi(q, "limit", 50), _qflag(q, "follow", True)))
            # -- plan: 只读的「这一步会做什么」预演 ---------------------
            #
            # history_ops 的每个危险操作都是 plan_* / do_* 两步设计, 破坏性的
            # 那一步要 `confirmed=True`。**只挂 do_* 会让这道设计形同虚设** ——
            # 调用方不必先看预演就能直接执行。所以两边都挂。
            if path.startswith("/api/history/plan/"):
                return self._json(_history_plan(path.rsplit("/", 1)[-1], q))
            return self._err("没有这个接口", 404)
        except _conflicts.ConflictError as e:
            # 「现在不在合并态」「这个路径没冲突」「三方不一致」都是**用法**
            # 问题, 走 400 —— 前端要能靠状态码区分「操作没做成」和「服务炸了」。
            return self._err(e, 400)
        except _hops.HistoryError as e:
            return self._err(e, 400)
        except GitError as e:
            return self._err(e, 400)
        except (ValueError, TypeError, KeyError) as e:
            # 查询参数类型不对(limit=abc 之类)是客户端传错, 走 400。
            # 不回异常类名 —— 那是服务端内部细节。
            return self._err("请求参数不合法:%s" % (str(e) or "类型不对"), 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if path == "/api/stage":
                REPO.stage(as_file_list(b.get("files")))
            elif path == "/api/unstage":
                REPO.unstage(as_file_list(b.get("files")))
            elif path == "/api/discard":
                REPO.discard(as_file_list(b.get("files")))
            elif path == "/api/commit":
                out = REPO.commit(b.get("message") or "")
                return self._json({"ok": True, "output": out, **REPO.status()})
            elif path == "/api/checkout":
                REPO.checkout(b.get("name", ""))
            elif path == "/api/branch":
                REPO.new_branch(b.get("name", ""))
            elif path == "/api/merge":
                # ok 必须来自 merge 自己: 外层硬编码 ok:True 会把「合并出冲突」
                # 这类结果盖成成功, 前端就没法靠 ok 判断成败了。
                return self._json(REPO.merge(b.get("name", "")))
            elif path == "/api/merge-abort":
                REPO.abort_merge()
            # -- 冲突解决(接线自 conflicts.py)-------------------------
            elif path == "/api/conflict/resolve":
                rel = REPO.rel_in_root(b.get("file", ""))
                # `content` **必须显式给**。理由: `resolve()` 内部写的是
                # `(content or "").encode()` —— 于是「漏传字段」与
                # 「用户故意把文件清空」在模块里是**同一个结果**: 落一个 0 字节文件。
                # 空文件是合法结果(两边都删), 所以不该在模块里拒; 但**漏传字段**
                # 是客户端的 bug, 让它变成一次静默的数据丢失是接线引入的坑。
                # 这里把两者分开: 缺键 -> 400; 给了空串 -> 照原样当「清空」。
                if "content" not in b:
                    raise GitError("缺少 content 字段(要清空文件请显式传空串)")
                content = b.get("content")
                # base64 传进来的也是 str, 所以这一条把三种情况(缺键/null/数字/
                # 对象)全挡在外面了 —— 模块里 `(content or "")` 会把 None 悄悄
                # 当成空串, 那样一个数字就会变成「空文件」而不是报错。
                if not isinstance(content, str):
                    raise GitError("content 必须是字符串,收到 %s"
                                   % type(content).__name__)
                return self._json(conflict_resolver().resolve(
                    rel, content, b.get("expect_sha256"),
                    b.get("encoding", "utf-8")))
            elif path == "/api/conflict/choose":
                rel = REPO.rel_in_root(b.get("file", ""))
                return self._json(conflict_resolver().choose(
                    rel, b.get("side", "")))
            elif path == "/api/conflict/delete":
                rel = REPO.rel_in_root(b.get("file", ""))
                return self._json(conflict_resolver().delete_file(
                    rel, b.get("expect_sha256")))
            elif path == "/api/conflict/commit":
                # commit_merge 自己在 ok 里说成败, 不能被 _ok_with_status 盖掉
                return self._json(conflict_resolver().commit_merge(
                    b.get("message")))
            elif path == "/api/conflict/abort":
                return self._json(conflict_resolver().abort())
            # -- 历史 / 危险操作(接线自 history_ops.py)----------------
            #
            # 破坏性的几步都要客户端把预演里拿到的 confirmed 原样送回来。
            # 这里**不**代替客户端判断: 模块里的 _require_confirm 才是那道
            # 护栏, 我们只负责把参数原样递过去。
            elif path == "/api/history/stash":
                return self._json(history_ops().do_stash(
                    b.get("message"), _bb(b, "include_untracked"),
                    _bb(b, "keep_index")))
            elif path == "/api/history/stash/apply":
                return self._json(history_ops().do_stash_apply(
                    b.get("index", 0)))
            elif path == "/api/history/stash/pop":
                return self._json(history_ops().do_stash_pop(b.get("index", 0)))
            elif path == "/api/history/stash/drop":
                return self._json(history_ops().do_stash_drop(
                    b.get("index", 0), _bb(b, "confirmed")))
            elif path == "/api/history/amend":
                return self._json(history_ops().do_amend(
                    b.get("message"), _bb(b, "confirmed")))
            elif path == "/api/history/undo-last-commit":
                return self._json(history_ops().do_undo_last_commit(
                    _bb(b, "drop_changes"), _bb(b, "confirmed")))
            elif path == "/api/history/reflog/restore":
                return self._json(history_ops().do_restore_from_reflog(
                    b.get("index", 0), b.get("mode", "soft"),
                    _bb(b, "confirmed")))
            elif path == "/api/history/revert":
                return self._json(history_ops().do_revert(
                    b.get("sha"), b.get("mainline"), _bb(b, "no_edit", True)))
            elif path == "/api/history/revert/abort":
                return self._json(history_ops().do_revert_abort())
            elif path == "/api/history/branch/rename":
                return self._json(history_ops().do_branch_rename(
                    b.get("old"), b.get("new"), _bb(b, "confirmed")))
            elif path == "/api/history/branch/delete":
                return self._json(history_ops().do_branch_delete(
                    b.get("name"), _bb(b, "force"), _bb(b, "confirmed")))
            elif path == "/api/history/tag/create":
                return self._json(history_ops().do_tag_create(
                    b.get("name"), b.get("sha"), b.get("message"),
                    _bb(b, "force")))
            elif path == "/api/history/tag/delete":
                return self._json(history_ops().do_tag_delete(
                    b.get("name"), _bb(b, "confirmed")))
            elif path == "/api/reset":
                REPO.hard_reset()
            elif path == "/api/gen":
                # 同上: gen.py 自己在 allVerified=False 时把 ok 置 false,
                # 这里不能再用 ok:True 盖掉。
                return self._json(REPO.generate(b.get("kind", "all"),
                                                int(b.get("count", 3)),
                                                b.get("seed")))
            else:
                return self._err("没有这个接口", 404)
            return self._ok_with_status()
        except _conflicts.ConflictError as e:
            return self._err(e, 400)
        except _hops.HistoryError as e:
            return self._err(e, 400)
        except GitError as e:
            return self._err(e, 400)
        except GenError as e:
            # 生成器的护栏拒绝(工作区不干净、分支已存在等)是**用法问题**,
            # 不是服务端故障,所以走 400。报错文案自带「怎么办」,直接透传显示。
            return self._err(e, 400)
        except (ValueError, TypeError, KeyError) as e:
            # 请求体坏 JSON / 类型不对 / 缺键 —— 那是**客户端传错了**,
            # 该是 400 而不是 500, 否则前端分不清「我传错」和「服务炸了」。
            # 不回异常类名: 那是服务端内部实现细节, 对用户没意义。
            return self._err("请求参数不合法:%s" % (str(e) or "类型不对"), 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)


def _bind_error(port, exc):
    """bind 失败时给用户看的话。**写到 stderr, 不抛**(E22)。

    旧代码直接让 ThreadingHTTPServer 的异常冒出去, 于是用户看到的是 13 行裸
    traceback, 混在 socketserver/http.server 的内部帧里, 全文**既没有端口号
    也没有一句「地址已被使用」**。

    而且**不能指望 strerror 说人话**, 这条有实测原因: http.server.TCPServer 设了
    `allow_reuse_address = 1`, 而 Windows 上带 SO_REUSEADDR 的重复 bind 失败
    **一律回 WSAEACCES(10013, 「访问权限被禁止」而不是 10048「地址已被使用」)**。
    所以旧报错天然把人带偏 —— 用户只会以为要加管理员权限或查防火墙。

    10013 确实有两种可能(端口被占 / 该端口在保留段里没有 bind 权限), 所以两种
    都写出来让人自己对号入座, 而不是替用户猜一个。两种情况下**端口号**都必须出现。
    """
    winerr = getattr(exc, "winerror", None)
    detail = "Windows 错误码 %s, 底层 %s" % (winerr if winerr is not None else "无",
                                           type(exc).__name__)
    lines = [
        "=" * 56,
        "  aigit 启动失败:端口 %d 绑不上。" % port,
        "",
        "  底层错误: %s" % exc,
        "  %s" % detail,
        "",
        "  两种可能,按顺序排查:",
        "    1) 这个端口**已经被别的程序占用了**(地址已被使用)。",
        "       换一个端口重跑:",
        "           python server.py --root <仓库路径> --port %d" % (port + 1),
        "    2) 这个端口在 Windows 的保留段里,普通用户没有 bind 权限",
        "       (所以报的是「访问权限被禁止」而不是「地址已被使用」)。",
        "       同样换一个高端口,比如 --port %d。" % (port + 1),
        "",
        "  注意:上面报出来的 Windows 错误码可能是 10013「访问权限被禁止」,",
        "  而**不是** 10048「地址已被使用」—— 因为 http.server 设了",
        "  allow_reuse_address=1, 而 Windows 上带 SO_REUSEADDR 的重复 bind",
        "  失败一律回 10013。所以别被「权限」两个字带偏。",
        "=" * 56,
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def main():
    global REPO, WEB
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="仓库路径")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--sandbox", action="store_true",
                    help="建一个干净的演练仓(清空已有内容)")
    a = ap.parse_args()

    WEB = Path(__file__).parent / "web"
    root = Path(a.root)

    if a.sandbox and root.exists():
        import shutil
        shutil.rmtree(root)
    REPO = Repo(root)
    if not REPO.exists():
        REPO.init()
        (root / "README.md").write_text(
            "# aigit sandbox\n\n这是一个演练仓,可以随便删。\n",
            encoding="utf-8", newline="\n")
        REPO._run(["add", "-A"])
        REPO._run(["commit", "-m", "init: sandbox"])

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        _bind_error(a.port, e)
        sys.exit(1)
    print("=" * 56)
    print("  aigit 已启动(**没有**自动打开浏览器)")
    print(f"  仓库   {root}")
    print(f"  打开   http://127.0.0.1:{a.port}/")
    print("  停止   Ctrl+C")
    print("=" * 56)
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\naigit 已停止")


if __name__ == "__main__":
    main()
