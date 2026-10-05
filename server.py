#!/usr/bin/env python3
"""
aigit —— 只跑在本机的 git 前端:一个标准库 HTTP 服务 + 基础 Web 视图,
附带随机仓库测试数据生成器。

**别把它当成「VSCode 源代码管理面板」的等价物** —— 网页上只有三个标签页
(改动详情 / 合并 / 提交历史);冲突解决与历史危险操作那一族只有 HTTP 路由,界面上点不到。

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
import time
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
import gitguard

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
import gitops as _gitops
import history_ops as _hops

# 仓库级互斥的唯一实现(2026-03 从 conflicts.py 搬进中立模块)。
# 放在 conflicts 之后、_CONFLICTS 之前: repolock 自己不 import 任何 aigit 模块,
# 所以这里没有新环 —— 但它必须与 conflicts 同为顶层 import, 因为 Repo.__init__
# 要在**模块导入期之外**也能拿到那个函数。
from repolock import repo_git_lock

_CONFLICTS = None
_HISTORY = None
_REMOTE = None


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


def remote_ops():
    """远程能力(**默认关**, 详见 gitops.py 的模块 docstring)。

    这里刻意**不给**它传闸: 让 NetGate 自己按 构造参数 > AIGIT_NET > off
    的顺序自己解析。这样「默认关」这件事的**唯一**决定权在 gitops 手里,
    接线层没有机会把它悄悄打开 —— 接线层能做的只有转发参数。

    ⚠️ 远程写入(push/pull)在 aigit 里是**不可撤销**操作: 推出去撤不回来。
    所以它沿用 history_ops 那套 plan/do 两步 + confirmed 协议, 下面
    两边都挂, 只挂 do_* 等于没有预演。
    """
    global _REMOTE
    if _REMOTE is None:
        # 注入 gitguard:gitops.py 按自己的规矩不 import aigit 模块
        # (test_aigit_net c3 用 ast.walk 查,函数内 import 也算),
        # 所以中和层由这里递进去。不传的话 gitops 会退到 _FallbackGuard,
        # 那档**枚举不到 filter.*/diff.*/merge.* 的子段名**(见 gitops 里的说明)。
        _REMOTE = _gitops.GitOps(REPO.root, guard=gitguard)
    return _REMOTE


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


#: `/api/history/plan/<op>` 的全部合法 URL 段。**表在这里, 响应的 `op` 也
#: 归一到这里的值**(见 `_history_plan` 末尾)。历史上响应的 `op` 是
#: history_ops 内部方法名, 和 URL 段对不上(`reflog-restore` ->
#: `restore-from-reflog`), 前端只好自己硬编码一张映射表绕过去。
PLAN_OPS = ("stash", "stash-drop", "amend", "undo-last-commit", "reflog-restore",
            "revert", "branch-rename", "branch-delete", "tag-delete")


def _history_plan(op, q):
    """GET /api/history/plan/<op> —— 危险操作的只读预演。

    刻意做成一张显式的表而不是 getattr 拼接: op 来自 URL, getattr(ch, op)
    能调到**任何**属性名, 拼错一个字母就从「预演」变成别的意思。

    **A3: 返回前把 `op` 归一到 URL 段。** 以前响应的 `op` 直接是
    history_ops 的方法名, 于是 `GET /api/history/plan/reflog-restore`
    回 `{"op": "restore-from-reflog"}` —— 拿这个 `op` 回去问「对应的预演
    URL 是什么」会得到 400。前端因此被迫在前端硬编码一张
    URL段->op 的映射表, 多一份会过期的东西。

    **兼容**: 旧值不丢, 落在 `legacy_op` 里。于是
      * 按 URL 调的调用方(= 所有真实调用方): 行为不变, 只是多一个字段;
      * 按 `op` 字段调的调用方(= 前端那张映射表): `op` 变了, 但
        `legacy_op` 还在, 映射表改一行即可, 且**不会静默拿错预演**
        —— 因为 `op` 现在是唯一且可回查的。
    """
    H = history_ops()
    if op == "stash":
        d = H.plan_stash(q.get("message", [None])[0],
                         _qflag(q, "include_untracked"))
    elif op == "stash-drop":
        d = H.plan_stash_drop(_qi(q, "index", 0))
    elif op == "amend":
        d = H.plan_amend(q.get("message", [None])[0])
    elif op == "undo-last-commit":
        d = H.plan_undo_last_commit(_qflag(q, "drop_changes"))
    elif op == "reflog-restore":
        d = H.plan_restore_from_reflog(_qi(q, "index", 0),
                                       q.get("mode", ["soft"])[0])
    elif op == "revert":
        d = H.plan_revert(q.get("sha", [None])[0], _qopt(q, "mainline"))
    elif op == "branch-rename":
        d = H.plan_branch_rename(q.get("old", [None])[0],
                                 q.get("new", [None])[0])
    elif op == "branch-delete":
        d = H.plan_branch_delete(q.get("name", [None])[0],
                                 _qflag(q, "force"))
    elif op == "tag-delete":
        d = H.plan_tag_delete(q.get("name", [None])[0])
    else:
        raise GitError("没有这个预演:%s(合法值:%s)"
                       % (op, "/".join(PLAN_OPS)))
    legacy = d.get("op")
    d["op"] = op                      # 归一到 URL 段
    if legacy and legacy != op:
        d["legacy_op"] = legacy       # 旧值留痕, 不静默改语义
    return d


def _remote_plan(op, q):
    """GET /api/remote/plan/<op> —— 远程操作的只读预演(不联网)。

    和 _history_plan 一样用**显式的表**而不是 getattr 拼接: op 来自 URL,
    getattr(ch, op) 能调到任何属性名, 而这里拼错一个字母的后果是
    「预览 push」变成「预览 pull」—— 对一个不可撤销的操作, 这个方向错了
    是会出人命的。
    """
    G = remote_ops()
    remote = q.get("remote", ["origin"])[0]
    branch = q.get("branch", [None])[0]
    if op == "fetch":
        return G.plan_fetch(remote, branch)
    if op == "push":
        return G.plan_push(remote, branch, _qflag(q, "force"),
                           _qflag(q, "delete"))
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


def _try_pin_dir(path, must=False, attempts=3):
    """`_pin_dir` 的容错版: 握不住先重试, 仍然握不住返回 None。

    `must=True`(仓库根)时**不吞异常** —— 整个仓握不住就是 fail closed。

    为什么值得重试: WinError 32(另一个程序正在使用此文件)在 Windows 上多半是
    **瞬时**的 —— 杀毒软件、搜索索引器、编辑器保存时短暂开一下目录。重试 3 次
    (每次 20ms)能把绝大多数这种抖动变成无事发生, 而代价只有几十毫秒。
    """
    last = None
    for i in range(attempts):
        try:
            return _pin_dir(path)
        except GitError as e:
            last = e
            if must:
                raise
            if i + 1 < attempts:
                time.sleep(0.02)
    if must:
        raise last
    return None


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
        # 锁从 repolock 取(2026-10-03): 此前是 `threading.RLock()` **每实例新建**,
        # 与 history_ops / conflicts 各拿一把 —— 同一个仓上三把互不相干的锁,
        # 实测跨域临界区重叠 C1=214 / C4=97 / C7=533 对(同域全 0, 即三把锁
        # 各自都有效, 只是不是同一把)。
        #
        # RLock 而不是 Lock 这条**没变**, 而且它是既有正确性要求、不是兼容措施:
        # 下面 reset() / commit() 要把多条 git 包进同一把锁(并发语义), 而 _run()
        # 内部又各自要取一次锁(commit()@1312 → _run()@507 转发 → _run_full()@483
        # 再取同一把)。实测嵌套 **97** 次, 换普通 Lock 每次都自死锁。
        # ⚠ 那两条静态扫描(test_f06_guard T6「嵌套取锁边 0 条」、concurrency C9
        #   `nested_edges: []`)看不见这个转发器中转的嵌套 —— 假阴性, 别拿来论证无嵌套。
        self._lock = repo_git_lock(self.root)

    # -- 底层 ------------------------------------------------------------
    def _run_full(self, args, check=True, stdin=None):
        """跑一条 git,返回 CompletedProcess。

        要看 returncode / stderr 的地方必须用它 —— 之前 _run 只返回 stdout(str),
        于是 merge() 写 p.returncode 直接 AttributeError 500。

        **所有 git 调用都从这儿过**(E 线查过:改之前本文件零配置覆盖),
        所以配置中和只需要在这里接一次,不存在「漏掉的调用点」。
        覆盖面与实测证据见 gitguard.py 文件头。
        """
        with self._lock:
            p = subprocess.run(
                gitguard.for_root(self.root, args, net_enabled=self._net_enabled()),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", input=stdin,
                env=gitguard.safe_env(net_enabled=self._net_enabled()),
                timeout=120,
            )
        if check and p.returncode != 0:
            raise GitError((p.stderr or p.stdout or "git 失败").strip())
        return p

    def _net_enabled(self):
        """联网能力闸开着吗?开着才保留用户自己的 credential/ssh 配置。

        aigit 的 `/api/*` 本身不碰远程(远程只在 gitops 那边),所以这里恒为 False,
        也就是**用户级配置整个不可达**。留着这个方法是为了两件事:
        1. 语义明确 —— 什么时候松绑一眼看得见;
        2. 将来真接远程时改一个返回值就行,不用改 6 个调用点。
        """
        return False

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
        # 这三行现在**比以前更要紧**:gitguard 把用户级配置整个不可达
        # (GIT_CONFIG_GLOBAL 指到不存在的路径),身份只能来自仓级配置。
        # 没有它们,history_ops 的 `commit --amend` 会因为「no identity」失败。
        self._run(["config", "user.email", "aigit@localhost"])
        self._run(["config", "user.name", "aigit"])
        # 仓级也写一份:对**已经存在**的仓同样生效。
        # (改之前只在这里写,所以「别人给的仓」上 gpgsign=true 依然会 spawn gpg —— E 线实测)
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
        """返回三段状态:已暂存 / 未暂存 / 冲突。

        两条 git(`status` 与 `_head_branch()`)**包进同一次持锁**(2026-10-04 修)。
        修前它们各取一次锁、中间没有外层持锁包着, 别的请求能在那道缝里插队 ——
        打点实测那道缝最宽 **1578 ms**(是 status() 在**等锁**, 那期间别的请求
        正持锁跑 commit), 并发判据 C3 因此读 26/50。修完 commit/checkout 已原子,
        这道缝是 C3 剩下的**唯一**来源(见 doc 台账 §20.3)。

        `self._lock` 是 RLock(repolock.py:97 `threading.RLock()`), 同线程嵌套取锁
        安全 —— `_run_full():497` 内部还会各自再取一次(实测同线程二次 acquire
        **0.0000 s**), 而锁被本线程持有时别的线程仍然拿不到(实测正确互斥)。
        形状与 `commit()` / `checkout()` / `commit_and_status()` 一致, 不是新花样。

        ⚠ 持锁范围只包这两条 git, 不包下面的解析 —— 解析是纯计算, 别把 CPU 时间
          算进锁里。
        """
        with self._lock:
            out = self._run(["status", "--porcelain=v1", "-z",
                             "--untracked-files=all"])
            branch = self._head_branch()
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

    def discard_plan(self, files):
        """discard 的只读预演: 逐个文件说明**会怎么死**。

        为什么必须和 `discard()` 用同一套判定: 预演说「恢复」而实删是
        「永久删除」, 比没有预演更危险 —— 用户照着预演点了确认。
        所以这里的分类逐行照抄 `discard()` 的三分支(已跟踪 -> checkout 恢复 /
        未跟踪文件 -> unlink / 未跟踪目录 -> 整树 rmtree), 包括它那个
        「目录内含已跟踪文件就整条拒绝」的规则。

        额外给一个 `untracked_total`: **不传 files 时** discard 会命中
        工作区里每一个未跟踪文件/目录(`git clean -fd` 的语义)。前端
        「丢弃全部」按钮必须先把这个数摆出来 —— README 自己写着 discard
        「会连带删掉所有未跟踪文件和目录」, 而按需传 files 的接口看不出这一点。
        """
        tracked = self._tracked_set()
        entries, blocked = [], []
        for f in files:
            abs_p = self.resolve_in_root(f)          # 与 discard() 同一条解析路径
            rel = abs_p.relative_to(self.root).as_posix()
            if not abs_p.exists() and not abs_p.is_symlink():
                # 不存在的东西没有「会怎么死」可言。预演说「永久删除」而
                # 实删是个 no-op, 和反过来一样是骗人 —— 让人以为要丢东西。
                entries.append({"path": rel, "action": "missing",
                                "detail": "路径不存在,discard 什么也不会做"})
                continue
            if rel in tracked:
                entries.append({"path": rel, "action": "restore",
                                "detail": "已跟踪文件 -> 从 HEAD 恢复,改动丢失"})
                continue
            if abs_p.is_dir():
                inner = sorted(t for t in tracked if t.startswith(rel + "/"))
                if inner:
                    blocked.append({
                        "path": rel, "action": "blocked",
                        "detail": "目录内含 %d 个已跟踪文件,discard 会整条拒绝"
                                  % len(inner),
                        "tracked_inside": inner[:20]})
                    continue
                entries.append({"path": rel, "action": "delete-tree",
                                "detail": "未跟踪目录 -> 整棵树永久删除,"
                                          "git 里没有它,删了找不回来"})
            else:
                entries.append({"path": rel, "action": "delete-file",
                                "detail": "未跟踪文件 -> 永久删除,"
                                          "git 里没有它,删了找不回来"})
        untracked_total = self._untracked_list()
        return {
            "ok": True, "op": "discard",
            "entries": entries, "blocked": blocked,
            "untracked_total": untracked_total,
            "untracked_count": len(untracked_total),
            "note": "action=restore 的丢的是**未提交的改动**; "
                    "action=delete-* 的是 git 从没记过、删了就找不回的东西。"
                    + ("另有 %d 个未跟踪文件/目录,「丢弃全部」会一并删掉。"
                       % len(untracked_total) if untracked_total else ""),
        }

    def _untracked_list(self):
        """仓里 git 没跟踪的文件/目录(git ls-files --others 减去被忽略的)。

        `--exclude-standard` 让 .gitignore 生效: 被忽略的构建产物不是
        `git clean -fd` 的目标(要 `-x` 才是), 把它们算进去等于虚报损失。
        """
        out = self._run(["ls-files", "--others", "--exclude-standard",
                         "--directory", "--no-empty-directory"], check=False)
        return sorted(p for p in out.splitlines() if p.strip())

    def escaping_links(self, limit=16):
        """列出**在仓里、但指向仓外**的 reparse point(目录联接 / 符号链接 / 挂载点)。

        为什么需要它:`resolve_in_root` 只挡得住**调用方点名**要走的路径。而**曾经**
        的 `commit()` 走的是裸 `git add -A`, 路径根本不由我们提供 —— git 自己会
        **穿过**目录联接。实测(本机 git 2.50.1.windows.1, mklink /J):

            <root>/escape -> <root>/../outside
            git add -A ; git ls-files   ->  a.txt  escape/leak.txt
            git show HEAD:escape/leak.txt -> rc=0, 内容就是仓外那份

        也就是说「显式请求 escape/leak.txt」会被 400 拒掉, 但「提交一次」就把
        仓外文件**复制**进了对象库, 而且永远留在历史里、随 push 一起走。
        git 对被跟踪路径上的联接一律按**普通文件**处理, 没有任何开关能关掉这个行为。

        ⚠ 它现在**仍然**是拒收门, 但它已经不是唯一的一道: `commit()` 改成
        「自己枚举 + 逐个字面路径交给 git」之后, git 压根没有可遍历的目录,
        也就没有可穿越的联接(见 stage_everything_safely)。留着这一道是因为
        (a) 它给的是**人话报错**(哪个联接、指向哪), 而不是「暂存区里出现了
        未经校验的路径」; (b) 少一道就少一层, 而它已经被两个独立来源确认有效
        (仓外哨兵 30/30 存活)。

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

    # -- C6: 「校验 + 落库」合成一步 --------------------------------------
    #
    # 旧实现是两步: escaping_links() 扫一遍工作区, 然后 `git add -A`。两步之间
    # 没有任何锁, 于是「扫的时候联接不在 / add 的时候联接在」就是一个开着的
    # 窗口。实测(test_aigit_concurrency.py C6): 8 次独立尝试里 **7 次**仓外哨兵
    # 进了历史, `git show HEAD:escape/leak.txt` 直接读得出内容。
    #
    # **为什么不是「加一把锁」**(这是本轮最容易被选错的一处):
    # 窗口的另一头是**文件系统上的另一个线程/进程**, 它不取 aigit 的任何一把锁 ——
    # C6 的对手就是直接 `mklink /J` + `rmdir`, 它的整个循环里没有一把 aigit 的锁。
    # 所以「把校验和 add 放进同一个临界区」对它是**完全无效**的: 持锁期间联接照样
    # 能挂上。实测(test_aigit_concurrency.py 的 C7 判据 + C6 的 7/8 复现)证明
    # 锁确实在起作用(同域跨线程重叠 0), 而 C6 在锁全部就位的情况下**照样 7/8
    # 复现** —— 两件事不矛盾, 因为它们量的不是同一个对手。
    # (原先这里引的是 test_f06_guard.py 的 T9; T9 已按裁定改成 @baseline,
    #  量的是**跨域**重叠而不是同域, 引它证明「同域重叠 0」是引错了。)
    #
    # 现在靠三条合起来才成立的措施:
    #   1. **我们自己枚举目录树**, 遇到 reparse point 立刻停住不往下走, 而且
    #      **只把逐个普通文件**交给 git。交目录等于把「add 时再遍历一遍」的权力
    #      交出去, 而那正是 F-06 漏进来的地方 —— git 走目录树时会穿过目录联接
    #      (实测: `git ls-files --others` 也会, 见 _add_scope 的注释)。
    #      实测(probe_semantics.py §5): 枚举之后、add 之前挂上联接, 显式路径表
    #      staged = []; 同一时刻的裸 `add -A` ls-files = [a.txt, escape/leak.txt]。
    #   2. **握住沿途每一个真目录**(复用 _pin_dir 家族)。目录项在手里换不掉, 所以
    #      「某个文件的父目录在 add 读到它的那一刻被换成联接」这个更狠的换法也
    #      做不成。实测(probe_ignore.py §D): 握住期间 rmtree 报 WinError 32、
    #      `mklink /J` rc=1; 不握住的对照组两次都换成功了。
    #   3. **add 之后核对索引**(不是再扫一遍文件系统)。索引就是 `git commit` 的
    #      **输入**, 核对它才是真正「合成一步」; 核对文件系统永远隔着一次 add 的
    #      距离, 那恰恰就是 C6 那个窗口。见 _assert_index_scope。

    def _add_scope(self):
        """(wanted, tracked) —— **由 git 自己**回答「哪些路径该进这次提交」。

        为什么问 git 而不自己判 ignore: 自己实现 ignore 语义必然漂移, 而漂移的
        方向是「把用户 ignore 掉的文件提交进去」—— 那比少提交几个文件坏得多。

        ⚠ `ls-files --others` 是 git **自己的**目录遍历, 它**会**穿过目录联接
        (实测 probe_ignore.py §C: 仓内挂一个 escape -> 仓外 的联接, 它照样把
        `escape/leak.txt` 列出来)。所以这两个集合只能当**过滤器**用:
        最终交给 git 的路径集恒是 `wanted ∩ _walk_add_scope()`, 是**交集**,
        联接里冒出来的名字落进交集时被丢掉 —— 不可能造成泄漏。

        `git diff --name-only` 列「工作区 vs 索引」, 与 `add -A` 覆盖面一致:
        已被跟踪但改了内容的、被删掉的, 都在里面。加 `--no-renames` 是为了让
        重命名展开成「旧路径(删)+ 新路径(增)」两条确定的路径, 不依赖 git 猜。
        """
        tracked = {os.path.normcase(x)
                   for x in self._run(["ls-files", "-z"], check=False).split("\0")
                   if x}
        d = self._run_full(["diff", "--no-renames", "--name-only", "-z"],
                           check=False)
        if d.returncode != 0:
            # 拿不到「哪些改了」就 fail closed —— 宁可拒绝提交, 也不能在
            # 不知道范围的情况下往索引里写东西。
            raise GitError("读不到工作区与索引的差异(%s),已拒绝提交"
                           % ((d.stderr or "").strip()[:160] or "git rc=%d" % d.returncode))
        wanted = {os.path.normcase(x) for x in d.stdout.split("\0") if x}
        u = self._run_full(["ls-files", "--others", "--exclude-standard", "-z"],
                           check=False)
        if u.returncode != 0:
            raise GitError("读不到未跟踪文件清单(%s),已拒绝提交"
                           % ((u.stderr or "").strip()[:160] or "git rc=%d" % u.returncode))
        wanted |= {os.path.normcase(x) for x in u.stdout.split("\0") if x}
        return wanted, tracked

    def _walk_add_scope(self):
        """走一遍工作区, 收出「**可以安全**交给 git 的路径」, 沿途握住每个真目录。

        返回 `(rels, pins)`: rels 是逐个**普通文件**的仓内相对路径(posix),
        pins 是打开的每一个真目录的 `_PinnedDir` —— **必须由调用方 close**。

        规则(逐条都有理由):
          * reparse point 一律**不往下走**。它指向仓外时不收(上面 escaping_links()
            已经把整个提交拒掉了, 这里再拒一道是为了不依赖那个调用点); 指向仓内
            且**不是目录**时收它自己 —— git 对文件符号链接只写链接本身(mode 120000),
            不会读目标的内容, 所以这是安全的; 指向仓内的**目录**联接/符号链接
            一律不收也不下探(那还是等于把遍历权交出去)。
          * 只收普通文件, 不收目录。**唯一的例外是子模块**: 含 `.git` 的目录是
            子模块边界, 只把这个**目录自己**交给 git。实测(probe_semantics.py §2)
            `git add -- vendor` 只暂存 gitlink 指针, new_objects = [], 子模块的
            文件内容**没有**进父仓的对象库 —— 所以这一条不引入新的泄漏面。
          * `.git` 不进。
          * 每个走到的目录都 `_pin_dir` 住。**握不住就整棵子树不收**(而不是
            fail closed), 理由是这一步是**冗余**的: 第一道(逐个字面路径、不给
            git 任何可遍历的目录)已经独立成立, 握不住只是让「父目录被换成联接」
            这个更狠的换法失去阻挡 —— 而那也正是「不收这棵子树」所挡掉的。
            也就是说**跳过 = 安全**, 跳过不是绕过。握不住通常是**瞬时**的
            (杀毒软件 / 索引器 / 编辑器短暂占着目录, WinError 32), 所以先重试。
            root 握不住是另一回事: 整个仓握不住, 直接 fail closed。
        """
        rels, pins, seen, unpinned = [], [], set(), []
        stack = [str(self.root)]
        rk = os.path.normcase(str(self.root))
        while stack:
            d = stack.pop()
            key = os.path.normcase(d)
            if key in seen:
                continue
            seen.add(key)
            pin = _try_pin_dir(d, must=key == rk)
            if pin is None:
                unpinned.append(d)
                continue
            pins.append(pin)
            if pin.reparse():
                continue                       # 自己就是联接: 不往里走
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if e.name == ".git":
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel = Path(e.path).relative_to(self.root).as_posix()
                if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
                    try:
                        Path(os.path.realpath(e.path)).relative_to(self.root)
                    except (ValueError, OSError):
                        continue               # 指向仓外: 不收
                    if e.is_dir(follow_symlinks=False):
                        continue               # 仓内目录联接: 不收也不下探
                    rels.append(rel)           # 仓内文件符号链接: 收它自己
                    continue
                if e.is_dir(follow_symlinks=False):
                    if os.path.exists(os.path.join(e.path, ".git")):
                        rels.append(rel)       # 子模块: 收目录自己, 不下探
                        continue
                    stack.append(e.path)
                else:
                    rels.append(rel)
        return rels, pins, unpinned

    def _ancestors_plain(self, rel):
        """rel 的**各级祖先目录**是不是真目录(逐级 lstat 问)。

        只查祖先, 不查 rel 自己 —— rel 自己可以是仓内的文件符号链接。
        这是 add 之前的**复核**: 枚举那一刻链是干净的, 现在也必须是。
        逐级问而不是 `realpath` 一次问, 原因见 is_reparse_point 的第二个坑
        (祖先是联接时 lstat 会跟着走)。
        """
        cur = self.root
        for part in rel.split("/")[:-1]:
            cur = cur / part
            if is_reparse_point(cur):
                return False
        return True

    def _index_delta(self):
        """索引里「与 HEAD 不一致」的路径集合(normcase 之后)。未出生 HEAD = 整个索引。

        问的是**索引**而不是工作区: 索引就是 `git commit` 的输入, 问它没有窗口。
        """
        if self._run_full(["rev-parse", "--verify", "--quiet", "HEAD"],
                          check=False).returncode != 0:
            return {os.path.normcase(x)
                    for x in self._run(["ls-files", "-z"], check=False).split("\0")
                    if x}
        p = self._run_full(["diff", "--cached", "--no-renames", "--name-only", "-z",
                            "HEAD"], check=False)
        if p.returncode != 0:
            raise GitError("核对暂存区失败(%s),已拒绝提交"
                           % ((p.stderr or "").strip()[:160] or "git rc=%d" % p.returncode))
        return {os.path.normcase(x) for x in p.stdout.split("\0") if x}

    def _assert_index_scope(self, allowed, before):
        """add 之后核对索引。**多出来的路径一律撤下来并拒绝提交。**

        `allowed` 是 normcase 之后的关键字集合(与 `_index_delta()` 同一口径),
        `before` 同理。**只有这两个集合是 normcase 的** —— 真正交给 git 的路径
        一律用磁盘上的原样拼写: 用户若把 `core.ignorecase` 关掉, 小写的
        pathspec 就匹配不上索引里的条目, 于是「提交变空提交」。normcase 只用于
        **集合比对**, 不用于喂 git。

        判据是 `after - before ⊆ allowed` 而不是 `after ⊆ allowed`。加 `before`
        这一层有两个必需的理由:
          * **合并态**: 索引里本来就有 merge 带进来的内容, 那些不是我们这次写进去的;
          * **用户手动 stage 的东西**: 我们没有义务替它做安全判断, 但也**不能**
            把它当成「我们自己引进来的」—— 否则合并态下每次提交都会被自己的防线
            拦死, 那是一个会被误当成「防线生效」的真阳性。

        这一层是**冗余**的: 既然交给 git 的是逐个字面路径, git 没有可遍历的目录,
        原则上不可能暂存我们没点名的路径。它是哨兵 —— 万一上面哪一条被后人改坏,
        这一条会先响, 而且响的方式是「拒绝提交」而不是「写进历史」。
        """
        after = self._index_delta()
        extra = sorted(x for x in (after - before) if x not in allowed)
        if not extra:
            return
        try:
            self._run_paths(["reset", "-q", "HEAD"], extra)
        except Exception:
            pass          # 撤不干净也要报出去 —— 报错优先于收尾
        raise GitError(
            "暂存区里出现了 %d 个未经校验的路径(如 %s),已撤销并拒绝提交。"
            "这是防线自己报的信, 不是操作失败 —— 请把上面这个文件移出工作区后重试。"
            % (len(extra), extra[0]))

    def stage_everything_safely(self):
        """把「工作区里该进索引的东西」暂存进去。**不用 `git add -A`。**

        `Repo.commit()` 是本轮唯一的调用点。**`conflicts.commit_merge()` 没有接过来**
        —— 那条路仍是裸 `add -A`(见那里的一段注释): 修法是现成的, 但
        test_f06_guard.py T3 把「commit_merge 只有 escaping_links 一道防线」写成了
        判据, 只要那边多一道防线 T3 必红, 而那个套件本轮不许退。留给下一条线。

        ⚠ 这里**刻意不调 `escaping_links()`** —— 那是调用点自己那道拒收门, 报错
          文案与 HTTP 状态码都归它管。多调一次还会让 test_f06_guard.py T2 的
          「`server.Repo.escaping_links` 被调了 1 次」判红, 而那不是一条该被满足的
          判据, 是一次调用计数。

        返回暂存涉及的路径数(便于排查; 不做判据用)。有目录握不住时返回
        `(n, skipped_rel_paths)`, skipped 是**没被暂存**的那几棵子树的仓内相对
        路径 —— 宁可少提交几个文件, 也不在没有阻挡的目录上动手。

        ⚠ **已知的行为变更(取舍, 不是 bug)**: 指向**仓内**的**目录**联接/符号链接,
          它们底下的文件不再被 `commit()` 暂存(文件符号链接仍然暂存, 因为 git 对
          它只写链接本身)。理由: 暂存一个目录就等于把「add 时再遍历一遍」的权力
          交出去 —— 那正是 F-06 漏进来的机制, 而 `_PinnedDir` 那套「链上每一级
          都必须是真目录」的判定按设计就会拒掉 reparse point, 没法在这里复用。
          用户仍可以用 `POST /api/stage` 显式暂存这些路径(stage 走的是另一条
          校验, 允许仓内符号链接)。仓内目录联接在 git 仓里本来就不常见。
        """
        wanted, tracked = self._add_scope()
        rels, pins, unpinned = self._walk_add_scope()
        try:
            # 交出去的是 `wanted ∩ 走出来的安全集合` —— 交集。git 从来看不到我们
            # 没点名的路径, 所以联接里冒出来的东西连 pathspec 都拿不到。
            # 键是 normcase(只用于比对), 值是**磁盘上的原样拼写**(喂 git)。
            allow = {}
            for r in rels:
                k = os.path.normcase(r)
                if k in wanted:
                    allow[k] = r
            # 已跟踪但工作区里已经没了的: 这次删除也要进提交。祖先链不干净就
            # 跳过 —— 让 git 去「删」一个此刻指向仓外的路径, 等于让它读仓外。
            for r in sorted(tracked):
                if r in allow or os.path.lexists(str(self.root / r)):
                    continue
                if r in wanted and self._ancestors_plain(r):
                    allow[r] = r
            for p in pins:
                if p.reparse():
                    raise GitError("枚举与暂存之间有目录被换成了联接(%s),已拒绝提交"
                                   % os.path.relpath(p.path, str(self.root)))
            before = self._index_delta()
            paths = sorted(allow.values())
            self._run_paths(["add"], paths)
            self._assert_index_scope(set(allow), before)
            skipped = [Path(d).relative_to(self.root).as_posix() for d in unpinned]
            return len(paths), skipped
        finally:
            for p in pins:
                p.close()

    def commit(self, message):
        if not isinstance(message, str) or not message.strip():
            raise GitError("提交信息不能为空")
        # 整段持锁: 校验 → 暂存 → 核对索引 → commit 之间不能有另一个走 aigit 的
        # 请求插进来改工作区。
        # ⚠ 这把锁管不到 C6 的对手(它直接在文件系统上 mklink/rmdir, 整个循环里
        #   没有一把 aigit 的锁), 所以锁只是那三条措施之外的第一道, 不是唯一一道。
        with self._lock:
            leaks = self.escaping_links()
            if leaks:
                names = ", ".join("%s -> %s" % (p, t) for p, t in leaks[:8])
                raise GitError(
                    "仓里有指向仓外的链接(%s),已拒绝提交 —— 它会把仓外的文件收进"
                    "索引、再写进历史, 而删掉链接也删不掉历史。"
                    "请先把它移出工作区, 或把它移进仓内。共发现 %d 个。" % (names, len(leaks)))
            n, skipped = self.stage_everything_safely()
            out = self._run(["commit", "-m", message], check=False)
            if not out.strip():
                raise GitError("没有可提交的改动(或提交被 hook 拒绝)")
            out = out.strip()
            if skipped:
                # 静默少提交几个文件比报错更难解释, 所以明说。这几棵子树是
                # **握不住**的(通常是别的程序短暂占着目录), 不是校验不过。
                out = ("注意: 有 %d 个目录握不住(通常是别的程序正占着它),"
                       "它们的内容**没有**进这次提交: %s\n%s"
                       % (len(skipped), ", ".join(skipped[:5]), out))
            return out

    def _status_quiet(self):
        """在**当前这次持锁里**取 status; **取不到就返回 (None, 异常) 而不是抛**。

        ## 为什么 status 取不到不能抛(E3, `_ok_with_status` 的注释是原始记录)

        每个 POST 结尾都要报 status, 所以只要 status() 抛错, 一个**已经做完**的
        操作就会被报成 400; 客户端拿到 400 的标准反应就是重试 —— 于是同一个破坏性
        操作被执行两次(stage 两次、commit 两次)。这是 E3 里最严重的一面。

        ⚠ 2026-10-04 修: 上一版的 `commit_and_status` 直接 `return self.commit(m),
          self.status()`, status() 一抛就**穿过**了 `_ok_with_status()` 的
          try/except —— 等于把这条防线对 /api/commit 拆掉了。**那是我引入的回归。**
          现在兜底下沉到这里, 三个 `*_and_status` 都走它。

        返回 (st, None) 成功 / (None, 异常) 失败, 由调用方决定怎么报。
        `self._lock` 是 RLock, 同线程嵌套取锁安全(repolock.py:97,
        实测同线程二次 acquire 0.0000 s)。
        """
        try:
            return self.status(), None
        except Exception as e:
            return None, e

    def commit_and_status(self, message):
        """commit + status 在**同一次持锁**里做完(2026-10-04 新增)。

        ## 为什么需要它

        handler 原来是这么写的:

            out = REPO.commit(msg or "")
            return self._json({"ok": True, "output": out, **REPO.status()})

        `commit()` 释放锁之后又调 `REPO.status()`, 而 `status()` 自己还要**再取
        一次**锁(status() 里 `git status` 与 `_head_branch()` 各一次)。
        中间那道缝的后果是具体的: **响应里的 status 可能不是自己那次提交的** ——
        别的请求可以在缝里提交或改工作区。C3 实测 23/24 个 /api/commit 真非原子。

        ## 为什么是新增方法而不是在 handler 里 `with REPO._lock:`

        handler 里碰 `REPO._lock` 的先例是 **0 处**。锁住在 `Repo` 内部, 是这个
        文件一贯的形状; 破一次封装就留一个「后来的人照抄」的口子。新增方法是
        加法, `commit()` 本身的签名与语义一个字没动。
        """
        with self._lock:
            out = self.commit(message)
            st, err = self._status_quiet()
        return out, st, err

    def checkout_and_status(self, name):
        """checkout + status 在**同一次持锁**里做完(2026-10-04 新增)。

        与 `commit_and_status` 同构。修的是同一类缝的另一处:
        `checkout()` 释放锁之后 handler 再调 `REPO.status()` 取一次锁,
        而 checkout 的响应里那份 status 会因此**不是自己这次 checkout 之后的**
        状态 —— 别的请求可以在缝里提交或改工作区。

        打点实测(diag_residual.py, THR=6 RND=12): 修之前 C3 剩下的插队
        **24/26 条是这个接口**, 缝宽 1.2~1.4 s, 位置在 checkout 自己的活干完
        (+57~114 ms)之后、status() 开始之前。

        `checkout()` 本身在 2026-10-04 已经是原子的(show-ref 与 checkout 已包进
        同一次持锁); 这里补的是它**之后**的那道缝。
        """
        with self._lock:
            out = self.checkout(name)
            st, err = self._status_quiet()
        return out, st, err

    def new_branch_and_status(self, name):
        """new_branch + status 在**同一次持锁**里做完(2026-10-04 新增)。

        与上面两个同构。`new_branch()` 只有一条 git(checkout -b), 它自己本来就
        原子; 缝在它释放锁之后 handler 再取一次锁做 status() 那一步。
        打点实测里它只占 2/26 条(大部分 /api/branch 请求因为分支已存在而失败,
        own=1 被判据正确排除), 但结构一样就一起修 —— 留着就是「已知还有一处」。
        """
        with self._lock:
            out = self.new_branch(name)
            st, err = self._status_quiet()
        return out, st, err

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
        # ⚠ 2026-10-04 修(判据 C3 实测 24/24 个 /api/checkout 真非原子):
        # 下面这两条 git **原来各取一次锁** —— `show-ref` 释放之后、`checkout`
        # 之前有一道 900~1100ms 的缝, 别的请求可以在缝里把那个分支删掉或改名。
        # 于是「我刚确认过它存在」这个前提在真正 checkout 时已经不成立,
        # 这是标准的 verify-then-act TOCTOU。
        # 合成**同一次持锁**之后, 中间不再有缝, 判据量到的正是这个。
        #
        # `self._lock` 是 RLock(repolock.py:97 `threading.RLock()`), 同线程嵌套
        # 取锁安全 —— 这与 `commit()` / `reset()` 已有的用法是同一个模式,
        # 不是新引入的用法。server.py:478-483 记着「实测嵌套 97 次,
        # 换普通 Lock 每次都自死锁」。
        with self._lock:
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
        # 保持旧入口(只回 HEAD 的 hard 重置), 实现收口到 reset()。
        self.reset("hard", "HEAD")

    def reset(self, mode, ref="HEAD"):
        """git reset --<mode> <ref>; hard 额外 clean -fd。mode 是三态显式值。"""
        if mode not in ("soft", "mixed", "hard"):
            raise GitError("mode 必须是 'soft' / 'mixed' / 'hard' 之一,收到 %r"
                           % (mode,))
        check_ref_name(ref)
        # 整体拿锁: hard_reset 是两条 git(reset + clean), 每条各自取一次
        # 锁的话, 两请求会交错(另一请求在 reset 之后、clean 之前进来)。
        # commit() 同理 —— add 与 commit 之间不能让别人改动工作区。
        with self._lock:
            self._run(["reset", "--" + mode, ref])
            if mode == "hard":
                self._run(["clean", "-fd"], check=False)

    def reset_plan(self, mode, ref="HEAD"):
        """reset 预演: 会从当前分支上「拿掉」的提交 + hard 的必毁警告。"""
        if mode not in ("soft", "mixed", "hard"):
            raise GitError("mode 必须是 'soft' / 'mixed' / 'hard' 之一,收到 %r"
                           % (mode,))
        check_ref_name(ref)
        out = self._run(
            ["log", "--format=%H%x1f%h%x1f%s", ref + "..HEAD"], check=False)
        undone = []
        for line in out.splitlines()[:50]:
            parts = line.split("\x1f")
            if len(parts) == 3:
                undone.append({"sha": parts[0], "short": parts[1],
                               "summary": parts[2]})
        d = {"ok": True, "op": "reset", "mode": mode, "ref": ref,
             "undone_commits": undone,
             "note": {
                 "soft": "soft: 移动 HEAD 到 %s,改动留在暂存区,工作区不动" % ref,
                 "mixed": "mixed: 移动 HEAD 到 %s,改动留在工作区(默认 git 行为)" % ref,
                 "hard": "hard: 未提交的改动将永久丢失;git clean -fd"
                         " 会删掉所有未跟踪文件和目录",
             }[mode]}
        if mode == "hard":
            d["warning"] = "未提交的改动将永久丢失"
        return d

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


# 「没传 status」与「传了 None(= 取过但取不到)」的区分标记。
# 用 None 当缺省会撞车, 撞了就等于把 E3 兜底又拆一次(见 `_ok_with_status` 的注释)。
_STATUS_UNSET = object()


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

    def _err(self, msg, code=400, err_code=None, extra=None):
        """错误响应统一形状。err_code / extra 是**前端能用的**那部分
        (HistoryError.code / HistoryError.extra), 只传可读文案和类型,
        绝对路径与 traceback 进服务端日志, 不进响应体。"""
        d = {"ok": False, "error": str(msg)}
        if err_code is not None:
            d["code"] = err_code
        if extra:
            d.update(extra)
        self._json(d, code)

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

    def _ok_with_status(self, st=_STATUS_UNSET, err=None):
        """操作成功之后的响应。**取不到 status 时不能报失败**(E3)。

        这是 E3 的一般形态, 不只是「未出生 HEAD」那一种: 每个 POST 结尾都要
        REPO.status(), 所以只要 status() 抛错, 一个**已经做完**的操作就会被
        报成 400。客户端拿到 400 的标准反应就是重试 —— 于是同一个破坏性操作
        被执行两次(stage 两次、commit 两次)。

        根因(E3 本身)已经修在 `Repo._head_branch`: 三种 HEAD 状态都报得出来。
        这一层是防线: 万一将来 status() 又因为别的原因失败, 宁可回一个
        明确标注「状态取不到」的 200, 也不要把已完成的动作报成失败。

        ## `st` 参数(2026-10-04)

        传进来的是「**已经取好的** status」—— `/api/commit` / `/api/checkout` /
        `/api/branch` 现在走 `Repo.*_and_status()`, 那里 status 是在操作的同一次
        持锁里取的, 所以这里不能再取第二次(那会把刚消掉的缝又打开)。

        ⚠ 三态必须区分, 不能拿 `None` 当缺省:
          * `st` 省略(`_STATUS_UNSET`)→ 本方法自己去取(原有调用点只有一处);
          * `st is None` 且 `err` 有值 → **取过, 取不到** → 走 E3 兜底回 200;
          * `st` 是 dict → 正常报。
          用 None 当缺省会撞车: `_status_quiet()` 取不到时返回的正是 None,
          而 `st=None` 必须走兜底、不能被当成「没传」而再去取一次。
        """
        if st is _STATUS_UNSET:
            try:
                st, err = REPO.status(), None
            except Exception as e:
                st, err = None, e
        if err is not None:
            return self._json({"ok": True,
                               "branch": None,
                               "staged": [], "unstaged": [], "conflicted": [],
                               "hasStaged": False,
                               "root": str(REPO.root),
                               "statusError": "%s: %s" % (type(err).__name__, err)},
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
            # 两个懒加载面板(UI2/UI3)。白名单仍是刻意收紧的: 只放行
            # index.html 实际会 import()/引用到的文件, 其余一律 404。
            if path == "/history-panel.js":
                return self._send(200, (WEB / "history-panel.js").read_bytes(),
                                  "application/javascript; charset=utf-8")
            if path == "/conflicts-panel.js":
                return self._send(200, (WEB / "conflicts-panel.js").read_bytes(),
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
            # -- reset / discard 的只读预演(A2)--------------------------
            #
            # 这两条是全项目最不可逆的操作: discard 连带删未跟踪文件,
            # reset --hard 连带 clean -fd。闸门已经要求 confirmed=true,
            # 而且闸门的 400 文案明确让用户「先 GET 预演看清要丢什么」——
            # 但这两个预演路由以前**根本不存在**, 那句指路的话指向 404。
            # 结果是两步协议对最该有预演的两条恰好是空的: 唯一挡在
            # 误删前面的东西是一个把人引向 404 的提示。
            #
            # 两条都只读, 不取写锁, 也不碰工作区。
            if path == "/api/reset/plan":
                mode = q.get("mode", [None])[0]
                if mode is None:
                    raise GitError("缺少 mode 参数(soft / mixed / hard)")
                return self._json(REPO.reset_plan(
                    mode, q.get("ref", ["HEAD"])[0]))
            if path == "/api/discard/plan":
                # files 走 query 而不是 body: 预演是 GET, 必须可粘贴/可收藏。
                # 缺省 = 空 = 「只报仓里有多少未跟踪的东西」, 因为前端那个
                # 「丢弃全部」按钮根本不传 files, 它要的是 untracked_total。
                return self._json(REPO.discard_plan(
                    as_file_list(q.get("files", None))))
            # -- 远程(接线自 gitops.py, **能力默认关**)------------------
            #
            # 这一组路由挂上**不代表远程是开的**: NetGate 默认 off, 所以
            # ls-remote / fetch / push / pull 一律 400 + code=net_disabled。
            # 想开就设环境变量 AIGIT_NET=read 或 write(见 gitops.py)。
            # /api/remote/capability 与 /api/remote/list 是纯本地的, 任何时候
            # 都能问 —— 用户要开远程, 得先知道自己有没有远程可开。
            if path == "/api/remote/capability":
                return self._json({"ok": True, "op": "capability",
                                   "gate": remote_ops().capability()})
            if path == "/api/remote/list":
                # 纯本地(git remote -v 只读 .git/config), 不需要任何能力级别
                return self._json(remote_ops().list_remotes())
            if path == "/api/remote/heads":
                return self._json(remote_ops().ls_remote(
                    q.get("remote", ["origin"])[0]))
            if path == "/api/remote/ahead-behind":
                return self._json(remote_ops().ahead_behind(
                    q.get("remote", ["origin"])[0],
                    q.get("branch", [None])[0]))
            if path.startswith("/api/remote/plan/"):
                return self._json(_remote_plan(path.rsplit("/", 1)[-1], q))
            return self._err("没有这个接口", 404)
        except _conflicts.ConflictError as e:
            # 「现在不在合并态」「这个路径没冲突」「三方不一致」都是**用法**
            # 问题, 走 400 —— 前端要能靠状态码区分「操作没做成」和「服务炸了」。
            return self._err(e, 400)
        except _hops.HistoryError as e:
            # to_dict() 里的 code / extra 必须透出去, 否则前端只能 match 中文文案。
            return self._err(e.message, 400, err_code=e.code, extra=e.extra)
        except _gitops.NetError as e:
            # 能力没开(net_disabled)、没确认(needs_confirmation)、远程不可达
            # (unreachable)—— 全是**用法/配置**问题, 不是服务故障, 走 400。
            # 用 to_dict() 而不是 _err(): _err 只回 {"ok","error"}, 把 code
            # 丢了; 而这层的规矩正是「让前端靠 code 分支, 不要 match 中文文案」。
            # 不改 _err —— 它是所有路由共用的, 动它会波及别人的接口形状。
            return self._json(e.to_dict(), 400)
        except GitError as e:
            return self._err(e, 400)
        except (ValueError, TypeError, KeyError) as e:
            # 查询参数类型不对(limit=abc 之类)是客户端传错, 走 400。
            # 不回异常类名 —— 那是服务端内部细节。
            return self._err("请求参数不合法:%s" % (str(e) or "类型不对"), 400)
        except Exception as e:
            # 内部细节(绝对路径 / traceback)只进服务端日志, 不进响应体 ——
            # 否则一次 500 就把 Windows 绝对路径吐给前端(UI2 实测过)。
            import traceback
            sys.stderr.write("[aigit] 500: %s: %s\n" % (type(e).__name__, e))
            traceback.print_exc()
            return self._err("服务端内部错误,详情见服务端日志", 500,
                             err_code="internal_error")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if path == "/api/stage":
                REPO.stage(as_file_list(b.get("files")))
            elif path == "/api/unstage":
                REPO.unstage(as_file_list(b.get("files")))
            elif path == "/api/discard":
                # 破坏性: 必须 confirmed=true(只认 bool, 用 _bb)。
                # 「真删了哪些」由 GET /api/discard/plan 预演给出, 这里不再猜。
                if _bb(b, "confirmed") is not True:
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "discard",
                        "error": "discard 是破坏性操作,需要 confirmed=true;"
                                 "先 GET /api/discard/plan 看清要丢什么。"}, 400)
                REPO.discard(as_file_list(b.get("files")))
            elif path == "/api/commit":
                # message 必须是字符串或缺省: 5/true/对象 这种畸形输入
                # 以前打成 500(AttributeError: 'int' object has no attribute 'strip'),
                # 前端会误以为是服务端坏了。
                msg = b.get("message")
                if msg is not None and not isinstance(msg, str):
                    raise GitError("message 必须是字符串,收到 %s"
                                   % type(msg).__name__)
                # ⚠ 2026-10-04 修(判据 C3 实测 23/24 真非原子): 原来 commit()
                # 释放锁之后又调 REPO.status() 取一次锁, 中间那道缝会让**响应里的
                # status 可能不是自己那次提交的**。合成一次持锁, 缝就没了。
                out, st, err = REPO.commit_and_status(msg or "")
                return self._ok_with_status(st, err)   # **必须**走这条, E3 兜底在里头
            elif path == "/api/checkout":
                # ⚠ 2026-10-04 修(与上面 commit 同构): 原来先 checkout(释放锁)
                # 再由下面的 `return self._ok_with_status()` 取一次锁做 status,
                # 中间那道缝让**响应里的 status 可能不是自己这次 checkout 之后的**。
                # 打点实测那 1.2~1.4s 的缝就在这里(§20.3)。
                _, st, err = REPO.checkout_and_status(b.get("name", ""))
                return self._ok_with_status(st, err)
            elif path == "/api/branch":
                _, st, err = REPO.new_branch_and_status(b.get("name", ""))
                return self._ok_with_status(st, err)
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
                # commit_merge 自己在 ok 里说成败, 不能被 _ok_with_status 盖掉。
                # message 类型: None 合法(用 merge 默认信息), 非字符串 -> 400
                # (以前 {"message": 5} 打成 500: 'int' object has no attribute 'strip')。
                cmsg = b.get("message")
                if cmsg is not None and not isinstance(cmsg, str):
                    raise GitError("message 必须是字符串,收到 %s"
                                   % type(cmsg).__name__)
                return self._json(conflict_resolver().commit_merge(cmsg))
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
                # pop = apply **+ drop**: 成功后 stash 条目没了, 恢复路径
                # 就断了。旁边的 /api/history/stash/drop 要 confirmed,
                # pop 却不要求 —— 同一个不可逆后果, 两套规矩。
                # (do_stash_pop 的签名里没有 confirmed, 护栏只能在接线层加;
                #  模块本身不许改。)
                # 预演用 plan/stash-drop: pop 的「删掉那一条」正是它。
                if _bb(b, "confirmed") is not True:
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "stash-pop",
                        "error": "stash pop 会把条目删掉(不可撤销),需要 "
                                 "confirmed=true;先 GET "
                                 "/api/history/plan/stash-drop?index=%s "
                                 "看清要丢什么。" % b.get("index", 0)}, 400)
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
            # -- 远程(接线自 gitops.py, **能力默认关**)------------------
            #
            # push / pull 写仓库之外, **不可撤销** —— 所以:
            #   * confirmed 只认真 bool(用 _bb), "confirmed": "false" 这种
            #     字符串在 Python 里是真的, 收下就等于放行一次误推。
            #   * 和 history 一样两边都挂(plan 在 GET 那边), 只挂 do_ 等于
            #     没有预演。
            #   * 闸默认 off, 所以默认全部 400 + code=net_disabled。
            elif path == "/api/remote/fetch":
                # fetch 只写本地 ref, 不需要 confirmed(撤销 = reset --hard)
                return self._json(remote_ops().do_fetch(
                    b.get("remote", "origin"), b.get("branch"),
                    b.get("refspec")))
            elif path == "/api/remote/push":
                return self._json(remote_ops().do_push(
                    b.get("remote", "origin"), b.get("branch"),
                    _bb(b, "force"), _bb(b, "delete"), _bb(b, "confirmed")))
            elif path == "/api/remote/pull":
                return self._json(remote_ops().do_pull(
                    b.get("remote", "origin"), b.get("branch"),
                    _bb(b, "confirmed")))
            elif path == "/api/reset":
                # 破坏性: 必须 confirmed=true, 且 mode 必须显式给 ——
                # soft/mixed/hard 影响「工作区改动活不活」, 默认值就是
                # 替用户做这个决定, 所以缺省一律 400。
                mode = b.get("mode")
                if mode not in ("soft", "mixed", "hard"):
                    raise GitError(
                        "mode 必须是 'soft' / 'mixed' / 'hard' 之一(缺省是 400,"
                        "绝不猜一个默认值),收到 %r" % (mode,))
                if _bb(b, "confirmed") is not True:
                    return self._json({
                        "ok": False, "code": "needs_confirmation",
                        "op": "reset",
                        "error": "reset 是破坏性操作,需要 confirmed=true;"
                                 "先 GET /api/reset/plan?mode=... 看清要丢什么。"}, 400)
                ref = b.get("ref") or "HEAD"
                if not isinstance(ref, str):
                    raise GitError("ref 必须是字符串")
                check_ref_name(ref)
                REPO.reset(mode, ref)
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
            return self._err(e.message, 400, err_code=e.code, extra=e.extra)
        except _gitops.NetError as e:
            return self._json(e.to_dict(), 400)
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
            import traceback
            sys.stderr.write("[aigit] 500: %s: %s\n" % (type(e).__name__, e))
            traceback.print_exc()
            return self._err("服务端内部错误,详情见服务端日志", 500,
                             err_code="internal_error")


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
        # 点名这一个文件, 而不是 `add -A` —— 这里是仓自己的引导, 没有并发,
        # 但把全仓最后一个 `git add -A` 执行面去掉, 「仓里没有裸 add -A」这句话
        # 才是真的(否则 grep 'add", "-A"' 永远有一处命中, 下一个人会以为看漏了)。
        REPO._run(["add", "--", "README.md"])
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
