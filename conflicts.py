#!/usr/bin/env python3
"""
aigit 冲突解决 —— 三方对比数据 / 写回 / 完成合并。

server.py 目前只有「触发合并」(`POST /api/merge`) 和「放弃合并」
(`POST /api/merge-abort`):能制造冲突,但**没有任何东西能显示冲突内容、更没有
东西能解决冲突**。本模块补上这一段。

三条设计约束(与 server.py / gen.py 一致):
  * 零第三方依赖,只用标准库。这工具要在别人机器上直接跑,`pip install`
    本身就是失败原因。
  * **不 import server.py,也不 import gen.py。** 所以可以不起服务直接单测,
    也可以被任何别的工具复用。集成方式见文件末尾「集成到 server.py」。
  * **不开窗口。** 本模块只提供数据和函数,渲染是前端的活。

------------------------------------------------------------------------
数据从哪来(为什么不是「解析标记字符串就完事」)
------------------------------------------------------------------------
前端要的是**三方对比**,不是标记字符串。git 自己已经把三方内容准备好了,
躺在索引里 —— `git ls-files -u` 输出三行:

    <mode> <blob-sha> 1\t<path>      ← stage 1 = base   (共同祖先)
    <mode> <blob-sha> 2\t<path>      ← stage 2 = ours   (当前分支)
    <mode> <blob-sha> 3\t<path>      ← stage 3 = theirs (被合入分支)

这三个 sha 是 **git 真正会提交进合并结果的那三个 blob**,不是推测出来的。
所以本模块:

  * 主数据源 = 索引 stage(逐路径精确,天然处理「一边新增」「一边删除」);
  * 交叉校验 = `git show HEAD:<file>` / `git show MERGE_HEAD:<file>` /
    `git merge-base HEAD MERGE_HEAD`(题目要求的三条),逐字节比对;
  * 两者不一致时**以索引为准**并在响应里报出来 —— 索引是提交时用的那份,
    不是 `git show` 猜出来的那份。

⚠ `git merge-base` 在**没有共同祖先**时(两个分支各有独立根提交)返回退出码 1
且输出为空。本模块把它当作正常结果处理(标记 `noBase`),不抛异常。

------------------------------------------------------------------------
每行的 base 是怎么定位的
------------------------------------------------------------------------
一个文件可能有多个冲突块,而 base 文件里**没有标记**。要把「第 k 块」的 base
段落找出来,得知道它在 base 文件里的位置。

做法:冲突块**外面**的行,三方是逐字相同的(能自动合并的部分)。把这些「公共行」
抽出来,用 `difflib.SequenceMatcher` 拿它去和 base 文件做单调对齐,得到
`merged行号 -> base行号` 的映射。第 k 块的 base 段落 = **它前后两个公共锚点
在 base 里之间的那段**:

    base[ base_idx(后一个锚点) : ... ]   ← 前一个锚点的下一行 .. 后一个锚点

公共锚点是 SequenceMatcher 保证逐字相等的行,所以这个区间是精确的,不是猜的。
再对 ours / theirs 各做一次同样的对齐,就能同时拿到每块在三个文件里的行号 ——
而且这三段文本必须逐字等于从标记里切出来的那三段,不等就说明解析错了。
这个自检结果放在每个 region 的 `verified` 字段里,前端和测试都能直接看。

一个文件里所有 region 都 verified == true,才说明整个三方拆分可信。

------------------------------------------------------------------------
接口形态
------------------------------------------------------------------------
`ConflictResolver` 是唯一的入口。返回的 dict 全部可直接 `json.dumps`,字段名见
``aigit-conflicts.md`` 的「字段表」一节(前端照着接)。

    r = ConflictResolver(root)
    r.merge_state()          # 在不在合并态 / 参与分支 / 默认提交信息
    r.list_conflicts()       # 面板列表:每个文件一行摘要
    r.inspect(path)          # 单个文件的三方全量数据(前端主要吃这个)
    r.resolve(path, text)    # 写回 + git add
    r.commit_merge(msg)      # 全部解决后提交
"""

import base64
import difflib
import hashlib
import os
import re
import subprocess
import sys
# ⚠ `import threading` 在 2026-10-03 被移除: 锁工厂搬进 repolock.py 之后,
#   本模块 `threading.` 出现 0 次(实测), 而全仓也没有任何地方写
#   `conflicts.threading`(grep 0 命中) —— 留着就是个纯装饰的死 import。
from pathlib import Path

import gitguard

__all__ = ["ConflictError", "Git", "ConflictResolver", "parse_markers",
           "repo_git_lock", "escaping_links", "install_escaping_guard"]

GIT_TIMEOUT = 120

#: 传给 git 的一次性配置。与 gen.py 同款:**不改仓库的 .git/config**,
#: 用 -c 逐条传,这样在别人的真实仓库上跑不留任何配置副作用。
#:
#: 这一组是**功能**配置(身份 / 换行),不是安全配置 —— 安全那一层是
#: `gitguard`,由 `_argv()` 统一拼。两组都保留,gitguard 的排在后面。
_GIT_CONFIG = [
    "-c", "user.name=aigit",
    "-c", "user.email=aigit@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
]


def _argv(root, args):
    """本模块**唯一**拼 git argv 的地方(两处 subprocess.run 都调它)。

    形状固定为 `[git, -C, root, --literal-pathspecs, ...功能配置, ...安全配置, ...argv]`:
      * `--literal-pathspecs` 与 `_GIT_CONFIG` 是原有行为,原样保留(走 extra_pre);
      * `gitguard` 负责「仓库自带配置指向的程序一个都执行不到」,含在子命令**后面**
        插 `--no-ext-diff --no-textconv` —— 本模块的 `_show()` / `_diff()` / `_log()`
        都要渲染 diff,漏了这一步就会被打穿。这两个 flag **必须同时给**:实测
        `--no-ext-diff` 压不住 `diff.<x>.textconv`,`--no-textconv` 也压不住
        `diff.<x>.command`。
      * 联网能力闸:本模块只做本地操作,恒为关 ⇒ 用户级配置整个不可达。
    """
    return gitguard.build(
        "git", root, args, net_enabled=False,
        extra_pre=["--literal-pathspecs"] + _GIT_CONFIG)


def _env():
    """本模块**唯一**造 env 的地方。

    原来手写的 5 个变量全部保留(语义不许变),`GIT_EDITOR` 那两个也在,
    再叠上 gitguard 的(其中就包括 `GIT_CONFIG_*` 的剥离 —— 这一条必需,
    实测环境注入能带进枚举看不到的 driver)。
    """
    return gitguard.safe_env(net_enabled=False)


class ConflictError(Exception):
    """可预期的失败:不在合并态、路径越界、文件没冲突、二进制……"""


# ---------------------------------------------------------------- 仓库级互斥
#
# 锁工厂已搬到中立模块 repolock.py(server.Repo / history_ops.HistoryOps 现在
# 也从那里取锁, 三条通路共用同一个字典与同一把 RLock)。这里是**再导出**,
# 不是转发封装 —— 目的有三个, 每一个都有实测依据:
#
#   1. `__all__` 里有 "repo_git_lock", 直接删掉会破 `from conflicts import *`;
#   2. test_f06_guard.py:403/405 猴补 `conflicts.repo_git_lock`;
#   3. Git.__init__ 写的是**不带限定**的 `repo_git_lock(self.root)`, 调用时在
#      本模块 globals 里解析 —— 所以下面这一行 import 让猴补**照样生效**,
#      test_f06_guard.py 不需要改。
#
# ⚠ 但搬走之后猴补的**作用面变窄了**, 这一点必须说清楚, 不许当没发生:
#   `conflicts.repo_git_lock = lambda…` 只改 conflicts 这一份 global,
#   改不到 repolock.repo_git_lock。所以同一个测试里:
#       server.Repo        拿到**真锁**
#       history_ops        拿到**真锁**
#       conflicts.Git      拿到 NoopLock
#   对 test_f06_guard.py 的语境这是对的(它就是要单独试 conflicts 那条通路),
#   但代价是**那个测试从此不再覆盖 server / history_ops 的锁行为**。
#   不要去「统一」这个差异 —— 那等于把 NoopLock 传播到其它通路去。
from repolock import repo_git_lock          # noqa: F401  (再导出, 见上)
from repolock import _REPO_LOCKS as _GIT_LOCKS        # noqa: F401  旧名, 别处仍在用
from repolock import _REPO_LOCKS_MU as _GIT_LOCKS_MU  # noqa: F401  同上


# ------------------------------------------------- 仓外联接防线(F-06 的根因)
#
# `git add -A` 的路径**不由我们提供**, 而 git 会**穿过**目录联接, 所以
# 「显式请求某个越界路径」那道 `_abs()` 护栏完全管不住「提交一次」。
# 实测(mklink /J, <root>/escape -> <root>/../outside):
#
#     git add -A ; git ls-files        ->  a.txt  escape/leak.txt
#     git show HEAD:escape/leak.txt    -> rc=0, 内容就是仓外那份
#
# 也就是说仓外的文件被**复制**进了对象库, 永远留在历史里、随 push 一起走。
# git 对被跟踪路径上的联接一律按普通文件处理, **没有任何开关能关掉这个行为**。
#
# 判定逻辑**只有一份**, 在 server.Repo.escaping_links()。本模块只做**取用**,
# 不复制一份 —— 复制就会漂移, 而这正是 F-05/F-06 的成因: 防线写在了
# `POST /api/commit` 一条路上, 「解决冲突 → 完成合并」这条路裸奔。
_ESCAPING_GUARD = None


def install_escaping_guard(fn):
    """注入仓外联接防线。返回原值, 便于还原。

    `fn(root) -> [(路径, 目标), ...]`。传 None 恢复默认实现。
    生产代码不需要调它 —— 默认实现自己去取 server.Repo.escaping_links。
    存在的意义是**可证伪**: 测试要能把防线故意关掉, 证明判据真的会红
    (见 test_conflicts.py 的 t_guard_commit_merge_falsifiable)。
    """
    global _ESCAPING_GUARD
    prev = _ESCAPING_GUARD
    _ESCAPING_GUARD = fn
    return prev


def _default_escaping_guard(root):
    """取 server.Repo.escaping_links —— 防线本体在 server.py, 这里只借用。

    `import server` 必须是**函数内**的: server.py 顶层就 `import conflicts`,
    模块级 import 会成环。这也顺带保证了本模块仍然「不起服务就能单测」
    (server.py 的起服务逻辑在 `if __name__ == "__main__"` 之后)。
    """
    try:
        import server
    except Exception as e:                      # ImportError / 别的线把 server.py 改坏了
        raise ConflictError(
            "仓外联接防线不可用(%s:%s),已拒绝提交 —— 宁可拒绝也不能在拿不到"
            "防线的情况下把仓外内容写进历史。" % (type(e).__name__, e))
    return server.Repo(root).escaping_links()


def escaping_links(root):
    """列出**在仓里、但指向仓外**的 reparse point。同 server.Repo.escaping_links。

    调用点必须**已经持有**该仓的 repo_git_lock(root) —— 扫描与 `add -A` 之间
    有 TOCTOU 窗口, 没有互斥就等于没扫(这正是 F-05 那一类问题)。
    """
    guard = _ESCAPING_GUARD or _default_escaping_guard
    return guard(root)


# ---------------------------------------------------------------- git 封装

class Git:
    """极薄的 git 封装。只做「跑一条命令」。刻意不提供任何写操作 ——
    写回必须经过 ConflictResolver 的护栏,不能让调用方绕过。"""

    def __init__(self, root):
        self.root = Path(root).resolve()
        # 仓库级互斥, 按 realpath 共享同一把(见上面的 repo_git_lock)。
        # Git.run / Git.cat_batch 里的裸 subprocess.run 由它串行化 —— 修复前
        # 本模块全文没有一把锁, 所以「同域内」也能撞 index.lock。
        self._lock = repo_git_lock(self.root)

    def run(self, args, check=True, binary=False):
        try:
            with self._lock:
                p = subprocess.run(
                    _argv(self.root, args),
                    capture_output=True, text=not binary, encoding=None if binary else "utf-8",
                    errors=None if binary else "replace",
                    env=_env(),
                    timeout=GIT_TIMEOUT,
                )
        except subprocess.TimeoutExpired:
            raise ConflictError("git 命令超时(>%ds): %s"
                                % (GIT_TIMEOUT, " ".join(str(a) for a in args[:3])))
        if check and p.returncode != 0:
            msg = ((p.stderr or p.stdout or b"") if binary
                   else (p.stderr or p.stdout or "")).strip()
            # 信息暴露防护: git 的错误信息有时会带仓库绝对路径(Windows 上尤甚),
            # 这条消息最终会原样进 API 响应,必须把绝对路径抹掉。
            msg = msg.replace(str(self.root), "<repo>")
            msg = msg.replace(str(self.root).replace("\\", "/"), "<repo>")
            raise ConflictError("git %s 失败(rc=%d): %s"
                                % (" ".join(str(a) for a in args[:3]), p.returncode, msg))
        return p

    def cat_batch(self, specs):
        """一次 `git cat-file --batch` 进程回答多个对象查询。

        specs: 对象名(sha 或 "HEAD:path" 这类)。返回 {spec: (raw|None, is_binary)}。
        取不到(对象不存在 / 路径不存在)记 (None, False) —— 与 _blob/_show 的
        「不存在不是错误」语义一致。binary 判定口径也与 _blob 相同:
        前 8000 字节里有 NUL 即当二进制。
        """
        if not specs:
            return {}
        try:
            with self._lock:
                p = subprocess.run(
                    _argv(self.root, ["cat-file", "--batch"]),
                    input=("\n".join(str(s) for s in specs) + "\n").encode("utf-8"),
                    capture_output=True, timeout=GIT_TIMEOUT,
                    env=_env(),
                )
        except subprocess.TimeoutExpired:
            raise ConflictError("git cat-file --batch 超时(>%ds)" % GIT_TIMEOUT)
        out = {}
        data = p.stdout or b""
        pos = 0
        for spec in specs:
            nl = data.find(b"\n", pos)
            if nl < 0:
                out[spec] = (None, False)
                break
            header = data[pos:nl]
            pos = nl + 1
            parts = header.rsplit(b" ", 2)
            if len(parts) == 3 and parts[2].isdigit():
                size = int(parts[2])
                raw = data[pos:pos + size]
                pos += size
                if data[pos:pos + 1] == b"\n":
                    pos += 1
                out[spec] = (raw, b"\0" in raw[:8000])
            else:
                # "<spec> missing" 或其他单行错误头,本次没有 body
                out[spec] = (None, False)
        return out

    def out(self, args):
        return self.run(args).stdout

    def rev_parse(self, rev):
        p = self.run(["rev-parse", "--verify", "--quiet", rev], check=False)
        return p.stdout.strip() if p.returncode == 0 else None


# ---------------------------------------------------------------- 标记解析

#: `<<<<<<< HEAD` / `<<<<<<< aigit_gen/conflict/0/b`。至少 7 个 `<`。
_RE_OURS = re.compile(r"^<{7,}[ \t]*(.*?)[ \t]*$")
#: `=======` —— 纯分隔行,后面不带标签。
_RE_SEP = re.compile(r"^={7,}[ \t]*$")
#: `>>>>>>> branch` / `>>>>>>> :file` / `>>>>>>> branch description`
_RE_THEIRS = re.compile(r"^>{7,}[ \t]*:?[ \t]*(.*?)[ \t]*$")
#: `||||||| base` —— diff3 / zdiff3 风格才有,表示 base 段开始。
_RE_BASE = re.compile(r"^\|{7,}[ \t]*(.*?)[ \t]*$")


def _split_lines(text):
    """按 \\n 拆行,末尾换行不算一行。

    返回 (lines, ends_with_newline)。**不**用 splitlines():它会在 lone \\r、
    \\x0b、\\x0c、\\u2028 等处拆行,而文件内容是用户的数据,拆错位置就等于
    改坏了别人的文件。行尾的 \\r 保留在行内容里,这样 CR LF 文件能原样往返。
    """
    if text == "":
        return [], False
    nl = text.endswith("\n")
    lines = text.split("\n")
    if nl:
        lines.pop()
    return lines, nl


def _join_lines(lines, trailing_nl):
    return "\n".join(lines) + ("\n" if trailing_nl else "")


def parse_markers(text):
    """把带冲突标记的文件拆成「公共块 + 冲突块」序列。

    返回 (regions, skeleton)。regions 只含冲突块;skeleton 是
    ``[(kind, payload), ...]``,kind ∈ {"common", "region"}。

    支持三种 style:
      * ``merge``   —— `<<<<<<<` / `=======` / `>>>>>>>`
      * ``diff3``   —— 多一段 `||||||| base`
      * ``zdiff3``  —— base 段内部再用 `||||||| 双方` 劈成两侧

    ⚠ 解析**不依赖** style 是哪种 —— 三种都按同一套状态机走,遇到什么标记
    切到什么状态。所以仓库里有人改了 merge.conflictStyle 也不会解析错。
    """
    lines, _ = _split_lines(text)
    skeleton = []
    regions = []
    common = []
    i = 0
    n = len(lines)
    while i < n:
        m = _RE_OURS.match(lines[i])
        if not m:
            common.append((i + 1, lines[i]))
            i += 1
            continue

        ours_label = m.group(1) or None
        start = i + 1                      # 1-based 的 <<<<<<< 行号
        j = i + 1
        ours, base, base_ours, base_theirs, theirs = [], [], [], [], []
        style = "merge"
        seen_sep = False
        while j < n:
            if _RE_OURS.match(lines[j]):
                break                       # 上一个冲突块没闭合,先收尾
            if _RE_SEP.match(lines[j]) and not seen_sep:
                seen_sep = True
                j += 1
                continue
            if _RE_SEP.match(lines[j]) and seen_sep:
                theirs.append(lines[j])      # 嵌套分隔,当普通内容
                j += 1
                continue
            mb = _RE_BASE.match(lines[j])
            if mb and not seen_sep:
                style = "diff3" if style == "merge" else style
                if not base and not base_ours and not base_theirs:
                    in_base = True
                    j += 1
                    # base 段:一直吃到 ======= 或 >>>>>> 为止
                    while j < n and not _RE_SEP.match(lines[j]) \
                            and not _RE_THEIRS.match(lines[j]):
                        mb2 = _RE_BASE.match(lines[j])
                        if mb2 and base_ours:
                            # zdiff3:第二个 ||||||| 把 base 劈成两侧
                            style = "zdiff3"
                            base_theirs = base
                            base = []
                            in_base = True
                            j += 1
                            continue
                        if mb2 and not base_ours:
                            base = []        # 第一个 ||||||| 是 base 段起点
                            j += 1
                            continue
                        (base_theirs if (style == "zdiff3" and base_ours)
                         else base).append(lines[j])
                        j += 1
                    continue
                j += 1
                continue
            mt = _RE_THEIRS.match(lines[j])
            if mt and seen_sep:
                break
            if seen_sep:
                theirs.append(lines[j])
            else:
                ours.append(lines[j])
            j += 1

        end = j + 1 if j < n else j         # >>>>>>> 的 1-based 行号
        theirs_label = (_RE_THEIRS.match(lines[j]).group(1) or None) if j < n else None

        reg = {
            "index": len(regions),
            "oursMarkerLine": start,
            "sepLine": (start + 1 + len(ours)) if seen_sep else None,
            "theirsMarkerLine": end,
            "oursLabel": ours_label,
            "theirsLabel": theirs_label,
            "style": style,
            "oursText": _join_lines(ours, bool(ours)),
            "theirsText": _join_lines(theirs, bool(theirs)),
            "baseText": _join_lines(base, bool(base)) if style != "merge" else None,
            "baseOursText": _join_lines(base_ours, bool(base_ours)) or None,
            "baseTheirsText": _join_lines(base_theirs, bool(base_theirs)) or None,
            # 工作区里的行号(1-based,闭区间)—— 前端定位/高亮用这个
            "oursStart": start + 1,
            "oursEnd": start + len(ours),
            "theirsStart": (start + 1 + len(ours) + 1) if seen_sep else start + len(ours) + 1,
            "theirsEnd": (start + 1 + len(ours) + 1 + len(theirs) - 1) if seen_sep
                         else start + len(ours) + 1 + len(theirs) - 1,
            "oursLines": len(ours),
            "theirsLines": len(theirs),
        }
        regions.append(reg)
        skeleton.append(("region", len(regions) - 1))
        if common:
            skeleton.append(("common", common))
            common = []
        i = j + 1 if j < n else n
    if common:
        skeleton.append(("common", common))
    return regions, skeleton


def _align(anchor_idx, anchor_lines, target_lines):
    """用公共锚点把「某一方文件的行」单调映射到 target_lines 的行。

    返回 dict{anchor_lineno -> target_lineno},只含**逐字相等**的匹配。
    autojunk=False 必须显式给:默认的启发式会把「看起来像随机内容」的行
    (也就是本工具的测试数据:大量重复的代码行)整段当垃圾丢掉,对齐直接失效。
    """
    sm = difflib.SequenceMatcher(None, anchor_lines, target_lines, autojunk=False)
    out = {}
    for ai, bi, size in sm.get_matching_blocks():
        for k in range(size):
            out[anchor_idx[ai + k]] = bi + k
    return out


def _locate(reg, target_lines, m2t):
    """用锚点映射算出第 reg 块在 target_lines 里的 [start,end] 行号(1-based)。

    返回 (start, end, text) 或 (None, None, None)。start/end 为 None 表示
    没有可用锚点(块贴着文件头/尾,或者这一侧为空)。
    """
    if not target_lines:
        return None, None, None

    # 前后各取**最近的**已匹配锚点。注意不是「紧邻的那个公共行」——
    # 紧邻的那行未必在目标文件里逐字存在(比如 ours 那边同一处也改了)。
    # 这里取「所有已匹配锚点中,位于本块之前/之后的最近一个」,单调,不会错配。
    matched = sorted(m2t)                       # m2t 的键是单调的合并行号
    before = None
    for lineno in reversed(matched):
        if lineno < reg["oursMarkerLine"]:
            before = lineno
            break
    after = None
    for lineno in matched:
        if lineno > reg["theirsMarkerLine"]:
            after = lineno
            break

    # 区间 = 两锚点在这份源文件里之间的那些行。锚点是逐字相等的,所以这个
    # 区间就是本块替换掉的 base/对侧原文 —— 不是估的。
    b_start = (m2t[before] + 1) if before is not None else 0
    b_end = (m2t[after] - 1) if after is not None else len(target_lines) - 1
    if b_end < b_start - 1:
        return None, None, None
    text = _join_lines(target_lines[b_start:b_end + 1],
                       b_end + 1 < len(target_lines))
    return b_start + 1, b_end + 1, text


# ---------------------------------------------------------------- 主体

class ConflictResolver:
    """冲突的读 / 写 / 收尾。root 是一个 git 仓库路径。"""

    def __init__(self, root):
        self.git = Git(root)

    # -- 护栏 ----------------------------------------------------------
    def _abs(self, rel):
        """仓库相对路径 → 绝对路径,拒绝一切越界。

        这是能**改写工作区文件**的接口,放行一个 `..` 或一个符号链接逃逸
        就是往仓库外写数据。四道检查:
          1. 非空、不是绝对路径、不含盘符;
          2. 路径字面量里不含 `..`;
          3. resolve() 之后仍在 root 内(这一道专治符号链接);
          4. 不落在 .git 里。
        """
        if rel is None or str(rel).strip() == "":
            raise ConflictError("路径为空")
        rel = str(rel).replace("\\", "/").strip()
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise ConflictError("拒绝绝对路径:%s" % rel)
        if ".." in Path(rel).parts:
            raise ConflictError("路径含 ..,拒绝:%s" % rel)
        p = (self.git.root / rel).resolve()
        try:
            p.relative_to(self.git.root)
        except ValueError:
            raise ConflictError("路径越界,已拒绝:%s" % rel)
        gitdir = (self.git.root / ".git").resolve()
        if p == gitdir or gitdir in p.parents:
            raise ConflictError("拒绝操作 .git 内的路径:%s" % rel)
        return p

    def _gd(self, name):
        """.git 下某个状态文件(可能 .git 是文件 → gitdir 指向别处)。"""
        gd = self.git.run(["rev-parse", "--git-dir"]).stdout.strip()
        base = Path(gd)
        if not base.is_absolute():
            base = self.git.root / base
        return (base / name).resolve()

    def _read_gd(self, name):
        p = self._gd(name)
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None

    def require_merge(self):
        """.git/MERGE_HEAD 存在才算是合并态。不在就拒绝写。"""
        mh = self._read_gd("MERGE_HEAD")
        if not mh:
            raise ConflictError(
                "当前不在合并态(.git/MERGE_HEAD 不存在) —— "
                "合并可能已经完成、被 --abort 撤销,或压根没开始。"
                "已拒绝写入,不会动工作区。")
        return mh.split()[0].strip() if mh.split() else None

    # -- 查询:合并态 ---------------------------------------------------
    def _branches_from_merge_msg(self, msg):
        """从 MERGE_MSG 抽分支名。**不自己编。**

        MERGE_MSG 的首行长这样:
            Merge branch 'feature-x'
            Merge branches 'a' and 'b'
            Merge remote-tracking branch 'origin/main'
            Merge commit 'deadbeef...'
        抽出来的原样返回;抽不到就是空 list,绝不猜。
        """
        if not msg:
            return []
        head = msg.splitlines()[0] if msg.splitlines() else ""
        m = re.match(r"^Merge\s+(.+?):?\s*$", head)
        if not m:
            return []
        body = m.group(1)
        names = re.findall(r"'([^']+)'", body)
        return [x for x in names if x and not re.fullmatch(r"[0-9a-fA-F]{7,40}", x)]

    def merge_state(self):
        """当前合并态的完整快照。面板顶部那条横幅吃这个。"""
        merge_head = self._read_gd("MERGE_HEAD")
        msg = self._read_gd("MERGE_MSG")
        in_merge = bool(merge_head and merge_head.split())
        first_line = ""
        if msg:
            for ln in msg.splitlines():
                if ln.strip():
                    first_line = ln.strip()
                    break
        conflicts = self.list_conflicts() if in_merge else {
            "ok": True, "inMerge": False, "files": [], "count": 0}
        return {
            "ok": True,
            "inMerge": in_merge,
            "mergeHead": (merge_head or "").split()[0] if in_merge else None,
            "origHead": (self._read_gd("ORIG_HEAD") or "").split()[:1][0]
                        if self._read_gd("ORIG_HEAD") else None,
            "head": self.git.rev_parse("HEAD"),
            "currentBranch": (self.git.run(["rev-parse", "--abbrev-ref", "HEAD"],
                                           check=False).stdout.strip() or None),
            # 从 MERGE_MSG 读出来的参与分支
            "branches": self._branches_from_merge_msg(msg),
            "defaultMessage": first_line,
            "mergeMsgPath": str(self._gd("MERGE_MSG")),
            "remaining": conflicts["count"],
            "files": conflicts["files"],
        }

    # -- 查询:三方数据 -------------------------------------------------
    def _stages(self, path):
        """索引里的三个 stage。返回 {1: {...}, 2: {...}, 3: {...}}。

        ⚠ 这是**权威**来源:这三个 blob 就是合并结果要提交的内容。
        stage 缺失 = 那一侧在这个路径上不存在(新增/删除混合冲突)。
        """
        p = self.git.run(["ls-files", "-u", "-z", "--", path], check=False)
        out = {}
        for rec in p.stdout.split("\0"):
            if not rec.strip():
                continue
            meta, _, name = rec.partition("\t")
            parts = meta.split()
            if len(parts) != 3:
                continue
            mode, sha, stage = parts
            out[int(stage)] = {"mode": mode, "sha": sha, "path": name}
        return out

    def _blob(self, sha):
        """读一个 blob 的原始字节,顺便判二进制。"""
        p = self.git.run(["cat-file", "blob", sha], check=False, binary=True)
        if p.returncode != 0:
            return None, True
        raw = p.stdout
        return raw, (b"\0" in raw[:8000])

    def _show(self, rev, path):
        """`git show <rev>:<path>`。文件不存在返回 None(不是错误)。"""
        p = self.git.run(["show", "%s:%s" % (rev, path)], check=False, binary=True)
        if p.returncode != 0:
            return None, False
        return p.stdout, (b"\0" in p.stdout[:8000])

    def _merge_base(self):
        """`git merge-base HEAD MERGE_HEAD`。

        ⚠ **没有共同祖先时返回退出码 1 且输出为空**(两个分支各有独立根提交,
        或 --allow-unrelated-histories 被禁用)。这是正常结果,标记成
        (None, reason) 而不是抛异常 —— 前端要用两栏而不是三栏显示。
        """
        p = self.git.run(["merge-base", "HEAD", "MERGE_HEAD"], check=False)
        out = p.stdout.strip()
        if p.returncode == 0 and out:
            return out, None
        return None, ("no-merge-base(rc=%d)" % p.returncode)

    def inspect(self, path):
        """单个冲突文件的三方全量数据。前端主要吃这个函数的返回。"""
        path = str(path).replace("\\", "/").strip()
        self._abs(path)                              # 越界在这里就被拒
        stages = self._stages(path)
        if not stages:
            raise ConflictError("该路径没有未合并的索引条目,不是冲突文件:%s" % path)

        # --- 1. 权威来源:索引 stage -----------------------------------
        idx = {}
        for stage, key in ((1, "base"), (2, "ours"), (3, "theirs")):
            if stage not in stages:
                idx[key] = {"exists": False, "sha": None, "raw": None,
                            "binary": False, "stage": stage, "mode": None}
                continue
            raw, binary = self._blob(stages[stage]["sha"])
            idx[key] = {"exists": raw is not None, "sha": stages[stage]["sha"],
                        "raw": raw, "binary": binary, "stage": stage,
                        "mode": stages[stage]["mode"]}

        # --- 2. 交叉校验:题目要求的那三条路径 -------------------------
        mb, mb_reason = self._merge_base()
        xcheck = {}
        for key, rev in (("ours", "HEAD"), ("theirs", "MERGE_HEAD")):
            raw, binary = self._show(rev, path)
            xcheck[key] = {
                "ref": rev,
                "exists": raw is not None,
                "binary": binary,
                "matchesIndex": (raw == idx[key]["raw"]),
            }
        if mb:
            raw, binary = self._show(mb, path)
            xcheck["base"] = {"ref": mb, "exists": raw is not None,
                              "binary": binary, "matchesIndex": raw == idx["base"]["raw"]}
        else:
            xcheck["base"] = {"ref": None, "exists": False, "binary": False,
                              "matchesIndex": idx["base"]["raw"] is None,
                              "reason": mb_reason}

        # 索引为准。show 读不出来但 stage 有 → 这是新增/删除混合,正常;
        # 反过来(show 有、stage 空)才是真不一致,要报出来。
        mismatch = [k for k, v in xcheck.items()
                    if v.get("exists") and not idx[k]["exists"]]

        no_base = (idx["base"]["raw"] is None)
        binary = any(v["binary"] for v in idx.values())

        # --- 3. 工作区当前内容 ----------------------------------------
        ap = self._abs(path)
        try:
            wt_raw = ap.read_bytes() if ap.is_file() else b""
        except OSError as e:
            # e.filename 是绝对路径,不能原样进错误信息
            raise ConflictError("读取工作区文件失败 —— %s: %s"
                                % (path, e.strerror or type(e).__name__)) from e
        wt_text = None if binary else wt_raw.decode("utf-8", errors="replace")

        # --- 4. 拆块 --------------------------------------------------
        regions = []
        if wt_text is not None:
            regions, skeleton = parse_markers(wt_text)
            regions = self._attach_bases(regions, skeleton, idx)

        def side(key, label):
            v = idx[key]
            return {
                "key": key,
                "ref": {"ours": "HEAD", "theirs": "MERGE_HEAD", "base": mb}[key],
                "label": label,
                "exists": v["exists"],
                "deleted": not v["exists"],
                "binary": v["binary"],
                "mode": v["mode"],
                "sha": v["sha"],
                "stage": v["stage"],
                "text": None if (v["raw"] is None or v["binary"]) else
                        v["raw"].decode("utf-8", errors="replace"),
                "lineCount": (len(_split_lines(v["raw"].decode("utf-8", "replace"))[0])
                              if (v["raw"] and not v["binary"]) else None),
            }

        theirs_label = regions[0]["theirsLabel"] if regions else None
        ours_label = regions[0]["oursLabel"] if regions else None

        return {
            "ok": True,
            "path": path,
            "inMerge": True,
            "noBase": no_base,
            "noBaseReason": mb_reason if not mb else None,
            "binary": binary,
            "mode": stages.get(2, stages.get(3, {})).get("mode"),
            "mergeBase": mb,
            "theirsLabel": theirs_label,
            "oursLabel": ours_label,
            "columns": 2 if no_base else 3,
            "stages": {str(k): v for k, v in sorted(stages.items())},
            "sides": {
                "ours": side("ours", ours_label or "HEAD"),
                "theirs": side("theirs", theirs_label),
                "base": side("base", None if no_base else mb[:10]),
            },
            "worktree": {
                "exists": ap.is_file(),
                "text": wt_text,
                "lineCount": len(_split_lines(wt_text)[0]) if wt_text is not None else None,
                "sha256": hashlib.sha256(wt_raw).hexdigest(),
                "byteLength": len(wt_raw),
                "hasMarkers": bool(regions),
                # 标记被人手删掉了但索引还是 U —— 前端要提示「已手工编辑」
                "manuallyEdited": (not regions) and bool(stages),
            },
            "regions": regions,
            "regionCount": len(regions),
            "verified": all(r.get("verified") for r in regions) if regions else None,
            "crossCheck": xcheck,
            "crossCheckMismatch": mismatch,
        }

    def _attach_bases(self, regions, skeleton, idx):
        """给每个 region 补上 base 段落与三方行号,并做逐字自检。"""
        if not regions:
            return regions
        # 公共锚点:(merged 行号, 文本)
        anchor_idx, anchor_txt = [], []
        for kind, payload in skeleton:
            if kind == "common":
                for lineno, text in payload:
                    anchor_idx.append(lineno)
                    anchor_txt.append(text)

        sides = {}
        for key in ("ours", "theirs", "base"):
            raw = idx[key]["raw"]
            if raw is None or idx[key]["binary"]:
                sides[key] = None
                continue
            sides[key] = _split_lines(raw.decode("utf-8", "replace"))[0]
            if not sides[key]:
                sides[key] = []

        maps = {}
        for key, lines in sides.items():
            maps[key] = _align(anchor_idx, anchor_txt, lines) if lines else {}

        for r in regions:
            got = {}
            # ⚠ 字段名分成两组,**不能混**:
            #   oursStart / oursEnd / theirsStart / theirsEnd
            #       = 在**工作区文件**(带标记那份)里的行号,parse_markers 算的,
            #         前端高亮用它。这里**绝不许覆盖** —— 早期版本这两组共用
            #         一个名字,工作区行号被对齐结果悄悄顶掉,前端会高亮错位置。
            #   oursLineStart / oursLineEnd / baseLineStart / ...
            #       = 该段在**源文件**(ours/theirs/base 三方各自那份)里的行号,
            #         由公共锚点对齐算出。
            # 两组都是 1-based 闭区间。
            for key in ("ours", "theirs", "base"):
                lines = sides.get(key)
                if lines is None:
                    r[key + "LineStart"] = r[key + "LineEnd"] = None
                    r[key + "LocatedText"] = None
                    got[key] = None
                    continue
                s, e, text = _locate(r, lines, maps[key])
                r[key + "LineStart"], r[key + "LineEnd"] = s, e
                r[key + "LocatedText"] = text
                got[key] = text
            # 自检:定位出来的那段必须逐字等于从标记里切出来的那段
            ok = True
            for key in ("ours", "theirs"):
                if got[key] is None:
                    ok = False
                    break
                if got[key].rstrip("\n") != (r[key + "Text"].rstrip("\n")):
                    ok = False
                    break
            if ok and idx["base"]["raw"] is not None and not idx["base"]["binary"]:
                # base 段没有从标记里直接切出来(diff3 才有),只在两侧都对齐时
                # 视为可信 —— 没有 base 就不检查它。
                if got.get("base") is None:
                    ok = False
            r["verified"] = ok
        return regions

    def list_conflicts(self):
        """冲突文件清单(面板左栏)。每项一行摘要,不展开全文。

        性能契约:这里**不能**逐文件调 inspect() —— inspect 每文件要做
        ls-files / merge-base / 3 个 git show / 3 个 cat-file,40 个文件
        就是 ~200 个 git 进程(实测 ~29s)。本函数把所有跨文件查询批成
        常数个 git 进程:
          1. diff 拿未合并清单;
          2. 一次 ls-files -u 拿全仓 stage;
          3. 一次 merge-base;
          4. 一次 cat-file --batch 拿全部 stage blob;
          5. 一次 cat-file --batch 拿全部 HEAD:/MERGE_HEAD:/mb: 内容。
        每个文件剩下的就只是磁盘读 + 标记解析 + 锚点对齐(纯 CPU)。
        字段名与 inspect 的摘要字段保持同名同义,UI 依赖不变。
        """
        p = self.git.run(["diff", "--name-only", "--diff-filter=U", "-z"],
                         check=False)
        paths = [x for x in p.stdout.split("\0") if x.strip()]
        in_merge = bool(self._read_gd("MERGE_HEAD"))
        if not paths:
            return {"ok": True, "inMerge": in_merge, "count": 0, "files": []}

        stages_by_path = {}
        p = self.git.run(["ls-files", "-u", "-z"], check=False)
        for rec in p.stdout.split("\0"):
            if not rec.strip():
                continue
            meta, _, name = rec.partition("\t")
            parts = meta.split()
            if len(parts) != 3:
                continue
            mode, sha, stage = parts
            stages_by_path.setdefault(name, {})[int(stage)] = {
                "mode": mode, "sha": sha, "path": name}

        mb, _mb_reason = self._merge_base()
        shas = sorted({s["sha"] for st in stages_by_path.values()
                       for s in st.values()})
        blobs = self.git.cat_batch(shas)

        specs = []
        for rel in paths:
            specs.append("HEAD:" + rel)
            specs.append("MERGE_HEAD:" + rel)
            if mb:
                specs.append(mb + ":" + rel)
        heads = self.git.cat_batch(specs)

        files = []
        for rel in paths:
            item = {"path": rel, "name": rel.rsplit("/", 1)[-1]}
            try:
                stages = stages_by_path.get(rel)
                if not stages:
                    raise ConflictError(
                        "该路径没有未合并的索引条目,不是冲突文件:%s" % rel)

                idx = {}
                for stage, key in ((1, "base"), (2, "ours"), (3, "theirs")):
                    if stage not in stages:
                        idx[key] = {"exists": False, "sha": None, "raw": None,
                                    "binary": False, "stage": stage, "mode": None}
                        continue
                    raw, binary = blobs.get(stages[stage]["sha"], (None, False))
                    idx[key] = {"exists": raw is not None, "sha": stages[stage]["sha"],
                                "raw": raw, "binary": binary, "stage": stage,
                                "mode": stages[stage]["mode"]}

                # crossCheckMismatch:只复算 inspect 同一公式,不要完整 xcheck 表
                mismatch = []
                ours_raw = heads.get("HEAD:" + rel, (None, False))[0]
                theirs_raw = heads.get("MERGE_HEAD:" + rel, (None, False))[0]
                base_raw = heads.get(mb + ":" + rel, (None, False))[0] if mb else None
                if ours_raw is not None and not idx["ours"]["exists"]:
                    mismatch.append("ours")
                if theirs_raw is not None and not idx["theirs"]["exists"]:
                    mismatch.append("theirs")
                if base_raw is not None and not idx["base"]["exists"]:
                    mismatch.append("base")

                no_base = (idx["base"]["raw"] is None)
                binary = any(v["binary"] for v in idx.values())

                try:
                    ap = self._abs(rel)
                    wt_raw = ap.read_bytes() if ap.is_file() else b""
                except (ConflictError, OSError) as e:
                    raise ConflictError("读取工作区文件失败:%s" % rel) from e
                wt_text = None if binary else wt_raw.decode("utf-8", errors="replace")

                regions = []
                if wt_text is not None:
                    regions, skeleton = parse_markers(wt_text)
                    regions = self._attach_bases(regions, skeleton, idx)

                item.update({
                    "noBase": no_base, "binary": binary,
                    "regionCount": len(regions),
                    "manuallyEdited": (not regions) and bool(stages),
                    "verified": (all(r.get("verified") for r in regions)
                                 if regions else None),
                    "hasMarkers": bool(regions),
                    "columns": 2 if no_base else 3,
                    "oursLabel": regions[0]["oursLabel"] if regions else None,
                    "theirsLabel": regions[0]["theirsLabel"] if regions else None,
                    "baseDeleted": not idx["base"]["exists"],
                    "oursDeleted": not idx["ours"]["exists"],
                    "theirsDeleted": not idx["theirs"]["exists"],
                    "crossCheckMismatch": mismatch,
                })
            except (ConflictError, OSError) as e:
                item["error"] = str(e)
            files.append(item)
        return {"ok": True, "inMerge": in_merge, "count": len(files), "files": files}

    # -- 写:解决冲突 ---------------------------------------------------
    def resolve(self, path, content, expect_sha256=None, encoding="utf-8"):
        """把用户编辑后的内容写回工作区并 `git add`。

        写之前三道检查:
          1. 仍在合并态(.git/MERGE_HEAD 存在)—— 合并已结束就**拒绝**,
             绝不去动一个已经完成的仓库;
          2. 路径不越界(_abs);
          3. 这个路径确实是冲突文件(索引里有 U 条目)。

        ⚠ **整个方法体在同一把 `self.git._lock` 里**(读-校验-写整段)。
        `expect_sha256` 是**乐观并发护栏**, 它比的是「我写进去的时候」的磁盘状态;
        一旦校验与写入之间能被插队, 这道护栏在并发下等于不存在 —— 两个请求
        都能校验通过、都写、都 `git add`, 后写的把先写的整个吃掉, 而**两个
        请求都返回 ok:true 与各自的 sha256**, 没有任何一方知道自己被覆盖
        (实测 doc/aigit-concurrency-audit.md F-03: 每遍 15–23 次, 即 21%–32%)。
        只锁 `open()` 那一下是没用的 —— 窗口在「校验」到「打开」之间。
        """
        with self.git._lock:
            return self._resolve_locked(path, content, expect_sha256, encoding)

    def _resolve_locked(self, path, content, expect_sha256=None, encoding="utf-8"):
        merge_head = self.require_merge()
        ap = self._abs(path)
        rel = str(path).replace("\\", "/").strip()
        stages = self._stages(rel)
        if not stages:
            raise ConflictError("该路径不是未合并路径,拒绝写入:%s" % rel)

        if encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except Exception as e:
                raise ConflictError("base64 解码失败:%s" % e)
        elif encoding in ("utf-8", "text", None):
            raw = (content or "").encode("utf-8")
        else:
            raise ConflictError("不支持的 encoding:%r(只支持 utf-8 / base64)" % encoding)

        if expect_sha256:
            cur = ap.read_bytes() if ap.is_file() else b""
            actual = hashlib.sha256(cur).hexdigest()
            if actual != expect_sha256:
                raise ConflictError(
                    "工作区已被改动(期望 sha256=%s,实际 %s),拒绝覆盖。"
                    "请重新读取文件再提交" % (expect_sha256[:12], actual[:12]))

        # 二进制逐字节写,绝不经过文本转码 —— Windows 上 open(...,"w") 会
        # 把 \\n 变成 \\r\\n,那等于替用户改内容。
        try:
            ap.parent.mkdir(parents=True, exist_ok=True)
            with open(ap, "wb") as f:
                f.write(raw)
        except OSError as e:
            # e.filename 是绝对路径,不能原样塞进会回给客户端的错误信息
            raise ConflictError("写回工作区失败 —— %s: %s"
                                % (rel, e.strerror or type(e).__name__)) from e
        self.git.run(["add", "--", rel])
        remaining = self.list_conflicts()
        still = any(f["path"] == rel for f in remaining["files"])
        return {
            "ok": True, "path": rel, "staged": True,
            "stillUnmerged": still,
            "remainingCount": remaining["count"],
            "remaining": [f["path"] for f in remaining["files"]],
            "mergeHead": merge_head,
            "byteLength": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def choose(self, path, side):
        """一键取一侧:把 ours 或 theirs 的内容整份写回。

        side ∈ {"ours", "theirs", "base"}。base 侧不存在时拒绝 ——
        悄悄用祖先覆盖等于把一边的改动抹掉,必须让调用方自己决定。
        """
        self.require_merge()
        rel = str(path).replace("\\", "/").strip()
        d = self.inspect(rel)
        if side not in d["sides"]:
            raise ConflictError("side 只能是 ours/theirs/base,收到 %r" % side)
        s = d["sides"][side]
        if s["deleted"] or s["text"] is None:
            raise ConflictError(
                "%s 侧在 base 之上不存在(%s),无法整份取用 —— "
                "要删掉这个文件请用 delete 动作。" % (side, "被删除" if s["deleted"] else "二进制"))
        return self.resolve(rel, s["text"])

    def delete_file(self, path, expect_sha256=None):
        """把冲突文件标记为删除并 git add(用于「一边删一边改」)。

        ⚠ 与 resolve 同理: 校验 sha256 → unlink → `git rm` → `git add`
        四步必须整段互斥, 只锁其中一步等于没锁。
        """
        with self.git._lock:
            return self._delete_file_locked(path, expect_sha256)

    def _delete_file_locked(self, path, expect_sha256=None):
        merge_head = self.require_merge()
        ap = self._abs(path)
        rel = str(path).replace("\\", "/").strip()
        if not self._stages(rel):
            raise ConflictError("该路径不是未合并路径,拒绝删除:%s" % rel)
        if expect_sha256:
            cur = ap.read_bytes() if ap.is_file() else b""
            if hashlib.sha256(cur).hexdigest() != expect_sha256:
                raise ConflictError("工作区已被改动,拒绝删除。请重新读取:%s" % rel)
        try:
            if ap.is_file():
                ap.unlink()
        except OSError as e:
            raise ConflictError("删除工作区文件失败 —— %s: %s"
                                % (rel, e.strerror or type(e).__name__)) from e
        self.git.run(["rm", "-q", "--", rel], check=False)
        self.git.run(["add", "--", rel], check=False)
        remaining = self.list_conflicts()
        return {"ok": True, "path": rel, "deleted": True,
                "remainingCount": remaining["count"],
                "remaining": [f["path"] for f in remaining["files"]],
                "mergeHead": merge_head}

    def unresolve(self, path, expect_sha256=None):
        """把一个**已解决**的文件退回未解决态(取消「已解决」标记)。

        机制:`git checkout --merge -- <path>` —— 它会重新做这条路径的三方
        合并,把未合并的索引 stage 重建回来,并把带标记的内容写回工作区。
        没有它,前端只能把「已解决」的文件记在 sessionStorage 里,回滚按钮
        永远是 disabled —— 这正是之前缺的这条路。

        保护:
          1. 仍须在合并态;
          2. 该路径必须是**已解决**态(索引里没有 U 条目)—— 还没解决就
             取消就不构成「取消」,直接报错;
          3. expect_sha256 对上才动 —— 已解决之后你又改过手,这里的再合并会
             覆盖工作区内容,不校验等于在暗地里丢用户的编辑。

        ⚠ 语义说明:退回的是「重新合并出来的冲突态」,工作区内容是标准标记
        的冲突块,不是你解决之前手工编辑过的中间态 —— 那份内容不再值得
        保护,它是要被替换的目标。

        ⚠ 与 resolve 同理: 校验 → `checkout --merge`(重写工作区)→ 重新校验
        整段互斥, 否则并发下「我刚校验过的内容」会被这次 checkout 覆盖掉。
        """
        with self.git._lock:
            return self._unresolve_locked(path, expect_sha256)

    def _unresolve_locked(self, path, expect_sha256=None):
        merge_head = self.require_merge()
        ap = self._abs(path)
        rel = str(path).replace("\\", "/").strip()
        if self._stages(rel):
            raise ConflictError("该路径还没解决,无需「取消解决」:%s" % rel)
        if expect_sha256:
            cur = ap.read_bytes() if ap.is_file() else b""
            if hashlib.sha256(cur).hexdigest() != expect_sha256:
                raise ConflictError(
                    "工作区已被改动(与期望 sha256 不符),拒绝覆盖。请重新读取:%s" % rel)
        p = self.git.run(["checkout", "--merge", "--", rel], check=False)
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or "").strip()
            msg = msg.replace(str(self.git.root), "<repo>")
            raise ConflictError("退回冲突态失败(rc=%d): %s" % (p.returncode, msg))
        if not self._stages(rel):
            # git 没报错但也没重建 stage —— 比如该路径的冲突态无从重建
            # (一方远端不存在且 merge-base 也缺这条),当作明确失败而不是静默成功
            raise ConflictError("无法重建该路径的冲突态:%s" % rel)
        remaining = self.list_conflicts()
        return {"ok": True, "path": rel, "unmerged": True,
                "remainingCount": remaining["count"],
                "remaining": [f["path"] for f in remaining["files"]],
                "mergeHead": merge_head}

    # -- 写:完成合并 ---------------------------------------------------
    def _merge_add_scope(self):
        """这次合并提交该收进索引的路径(显式清单, 仓库根相对)。

        三条来源, 全部走 git 自己的「已跟踪」视图, **不自己 os.walk**:
          * ``diff --cached``         —— 已进索引的。冲突解决正是 resolve() /
            choose() 一路 ``git add -- <路径>`` 把它收进去的, 所以这一条
            才是合并提交的主体。
          * ``diff``                 —— 已跟踪且工作区有改动的。
          * ``diff --diff-filter=D``  —— 已跟踪且被删的。

        **未跟踪的一律不进**, 这就是收紧点: 目录联接下的仓外文件属于未跟踪,
        裸 ``add -A`` 会顺着联接把它们收进去, 显式清单不会(实测 §B)。

        三条都带 ``-z``, 按 NUL 切分而不是按行 —— 路径里有空格/引号/换行时
        按行切会切错, 而切错等于路径注入。
        """
        paths = set()
        for args in (["diff", "--cached", "--name-only", "-z"],
                     ["diff", "--name-only", "-z"],
                     ["diff", "--diff-filter=D", "--name-only", "-z"]):
            # check=False: 这三条里, 「没有差异」时 diff 仍返回 0, 但仓库
            # 若处在异常态返回非 0 —— 那时退化成空清单(少收), 不该抛异常
            # 把整个合并提交打断。少收路径是保守方向, 宁可让人看见多出来的
            # 未跟踪文件, 也不要把看不见的东西塞进历史。
            p = self.git.run(args, check=False)
            paths.update(x for x in (p.stdout or "").split("\0") if x)
        return sorted(paths)

    def commit_merge(self, message=None):
        """提交合并结果。

        三道检查:
          1. 仍在合并态;
          2. **没有未合并路径了** —— 有就必须先解决,不能带 U 提交;
          3. 提交信息里有参与分支名(从 .git/MERGE_MSG 读,不自己编)。

        ⚠ **第四道(F-06): 仓里没有指向仓外的联接**, 与 `server.Repo.commit()`
        用的是**同一份**判定逻辑(`escaping_links`)。
        修复前这里是一个裸的 `git add -A`, 前面**一把锁都没有、一次扫描都没有**,
        于是「解决冲突 → 完成合并」这条路把仓外文件复制进了对象库且永久留在
        历史里 —— 同一次运行里 `POST /api/commit` 正确拒绝、这条路静默提交,
        **串行即可复现, 不需要任何竞态**(实测见 f06_repro.py)。
        **现在这里是第五道**: 作用域受限的显式 `add -- <路径>`,
        只收 git「已跟踪」视图里的路径, 未跟踪的一律不进(见 `_merge_add_scope`
        与下面 add 那段的实测清单)。第四道挡「已跟踪却指向仓外」, 第五道挡
        「未跟踪、靠联接被 `add -A` 顺手收进来」—— 两道挡的是不同东西, 不是重复。

        ⚠ 整个方法体在同一把 `self.git._lock` 里。`escaping_links` 是**纯文件
        系统扫描**, 不碰 git, 所以锁**管不到「扫描完到 add 之间」**。
        第五道(作用域受限 add)把这条窗口也收窄了: 联接即便在扫描之后才挂上,
        它下面的路径也是未跟踪的, 不在显式清单里, 收不进去。
        但这**不是**临界区意义上的关闭 —— 真要彻底关掉, 还得让「扫描 + add」
        本身原子(与 server.Repo.commit() 走 option (c) 的做法同源:
        路径白名单快照 + 目录钉住 + 索引复核, 那三样这里都还没有)。
        """
        with self.git._lock:
            return self._commit_merge_locked(message)

    def _commit_merge_locked(self, message=None):
        merge_head = self.require_merge()
        msg_raw = self._read_gd("MERGE_MSG") or ""
        branches = self._branches_from_merge_msg(msg_raw)
        default = ""
        for ln in msg_raw.splitlines():
            if ln.strip():
                default = ln.strip()
                break
        if not default:
            raise ConflictError(".git/MERGE_MSG 缺失或为空,拿不到默认提交信息,已拒绝提交")

        remaining = self.list_conflicts()
        if remaining["count"]:
            raise ConflictError(
                "还有 %d 个文件没解决,已拒绝提交:\n      %s"
                % (remaining["count"], "\n      ".join(f["path"] for f in remaining["files"])))

        # F-06 防线。取用 server.Repo.escaping_links 的同一份判定逻辑, 不复制。
        # 必须**在 add -A 之前**、且**在持锁状态里**。
        leaks = escaping_links(self.git.root)
        if leaks:
            names = ", ".join("%s -> %s" % (p, t) for p, t in leaks[:8])
            raise ConflictError(
                "仓里有指向仓外的链接(%s),已拒绝提交。git add -A 会穿过目录联接,"
                "把仓外的文件收进索引、再写进历史, 且删掉链接也删不掉历史。"
                "请先把它移出工作区, 或把它移进仓内。共发现 %d 个。" % (names, len(leaks)))

        text = (message or "").strip() or default
        # 用户自带信息时,把参与分支补上 —— 要求是「提交信息里自动列出分支名」。
        # 从 MERGE_MSG 抽,抽不到就只在原文里找一次,绝不臆造。
        if message and branches and not any(b in text for b in branches):
            text = "%s\n\nMerged: %s" % (text, ", ".join(branches))

        # ── 作用域受限的 add(替代修复前的裸 `add -A`)────────────────────
        # 裸 `add -A` 的路径**不由我们提供**, 而 git 会**穿过**目录联接, 于是
        # 「解决冲突 → 完成合并」这条路把仓外文件复制进了对象库、永久留在历史里。
        #
        # 实测(litematica-preview/doc/aigit-lock-graph-20261003.md §B, 探针
        # addscope.py, 同一批输入; 该文件在 litematica-preview/doc/ 下, 而
        # litematica-preview 本身不是 git 仓, 所以它不进任何被测仓):
        #   裸 `add -A`     -> 索引 6 条, 其中 escape/leak.txt、escape/sub/deep.txt
        #                      两条的**内容就是仓外那份**,
        #                      `git show HEAD:escape/leak.txt` 读出 OUTSIDE-SECRET-v1;
        #   作用域受限 add   -> 索引 2 条(a.txt / b.txt), 泄漏 **0** 条。
        # 收紧掉的正是那 4 条**未跟踪**路径(含 2 条仓外内容)。
        #
        # 作用域取 git 自己「已跟踪」视图里的三类路径(见 _merge_add_scope)。
        # 「已跟踪却指向仓外」那种由**上面那道 escaping_links 防线**挡 ——
        # 它在本行之前跑, 已实测会在有联接时拒绝(同文档 A 段)。这里不复制第二份判定。
        #
        # ⚠ **代价, 必须说清楚, 不许粉饰**: 这**改变了 test_f06_guard.py T3 的前提**。
        #   T3「反向对照: 关掉防线, 必须真的泄漏」把「commit_merge 只有
        #   escaping_links 一道防线」写成了判据: 它装上 no-op 防线后断言
        #   **不拒绝**且**真的泄漏**。现在这条路上有第二道(本处的作用域受限 add),
        #   所以 no-op 防线下不再泄漏, T3 的 `ck(lp and lc, ...)` 会**红**。
        #   那是 T3 的**前提过时**(它要证明的是「T1 的判据可证伪」, 而 T1 依然可证伪:
        #   防线在位时 A 段实测确实拒绝), 不是产品行为变坏。
        #   改 T3 是下一条线的事, 本轮不碰它 —— 但本轮**不能**假装 T3 还绿。
        scope = self._merge_add_scope()
        if scope:
            self.git.run(["add", "--"] + scope)

        p = self.git.run(["commit", "-m", text], check=False, binary=True)
        if p.returncode != 0:
            raise ConflictError(
                "git commit 失败(rc=%d): %s"
                % (p.returncode, (p.stderr or p.stdout or b"").decode("utf-8", "replace").strip()))
        head = self.git.rev_parse("HEAD")
        return {
            "ok": True,
            "committed": True,
            "commit": head,
            "shortCommit": (head or "")[:10],
            "message": text,
            "messageSource": ("MERGE_MSG" if not (message or "").strip() else "caller"),
            "defaultMessage": default,
            "branches": branches,
            "mergeHead": merge_head,
            "subject": (self.git.run(["log", "-1", "--pretty=%s"], check=False).stdout
                        .strip()),
        }

    def abort(self):
        """放弃合并。等价于 `git merge --abort`,保留在这里是为了让前端
        不必再回 server.py 那边(集成时这个可以走已有接口)。"""
        self.git.run(["merge", "--abort"], check=False)
        return {"ok": True, "aborted": True,
                "inMerge": bool(self._read_gd("MERGE_HEAD"))}


# ---------------------------------------------------------------- CLI
# 起服务不方便时,这个 CLI 能直接把 JSON 打出来。它也是 test_conflicts.py
# 之外的第二条独立验证路径 —— 两者走的是同一套函数。

def _print(obj):
    import json
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="aigit 冲突解决:三方对比 / 写回 / 完成合并")
    ap.add_argument("--root", required=True, help="仓库路径")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("state", help="当前合并态")
    p_list = sub.add_parser("list", help="冲突文件清单")
    p_list.add_argument("--detail", action="store_true", help="逐个展开三方全文")
    p_ins = sub.add_parser("inspect", help="单文件三方数据")
    p_ins.add_argument("path")
    p_res = sub.add_parser("resolve", help="写回并 git add")
    p_res.add_argument("path")
    p_res.add_argument("--from-file", required=True, help="要写回的内容从这个文件读")
    p_res.add_argument("--encoding", default="utf-8", choices=["utf-8", "base64"])
    p_ch = sub.add_parser("choose", help="整份取用某一侧")
    p_ch.add_argument("path")
    p_ch.add_argument("side", choices=["ours", "theirs", "base"])
    sub.add_parser("abort", help="放弃合并")
    p_un = sub.add_parser("unresolve", help="取消某个文件的「已解决」状态")
    p_un.add_argument("path")
    p_un.add_argument("--expect-sha256", default=None)
    p_cmt = sub.add_parser("commit", help="完成合并")
    p_cmt.add_argument("--message", default=None)

    a = ap.parse_args(argv)
    r = ConflictResolver(a.root)
    try:
        if a.cmd == "state":
            _print(r.merge_state())
        elif a.cmd == "list":
            out = r.list_conflicts()
            if a.detail:
                out["details"] = [r.inspect(f["path"]) for f in out["files"]]
            _print(out)
        elif a.cmd == "inspect":
            _print(r.inspect(a.path))
        elif a.cmd == "resolve":
            if a.encoding == "base64":
                content = Path(a.from_file).read_text(encoding="ascii").strip()
            else:
                content = Path(a.from_file).read_text(encoding="utf-8")
            _print(r.resolve(a.path, content, encoding=a.encoding))
        elif a.cmd == "choose":
            _print(r.choose(a.path, a.side))
        elif a.cmd == "abort":
            _print(r.abort())
        elif a.cmd == "unresolve":
            _print(r.unresolve(a.path, a.expect_sha256))
        elif a.cmd == "commit":
            _print(r.commit_merge(a.message))
        else:
            ap.print_help()
            return 2
    except ConflictError as e:
        _print({"ok": False, "error": str(e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
