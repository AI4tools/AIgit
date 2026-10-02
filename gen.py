#!/usr/bin/env python3
"""
aigit 测试数据生成器 —— 独立、可配置、确定性。

设计约束(与 server.py 相同的两条底线):
  * 零第三方依赖,只用标准库。`pip install` 本身就是失败原因。
  * 依赖面只有 `subprocess` 跑 git。**不 import server.py,不知道 HTTP 的存在**,
    所以可以不起服务直接单测,也可以被别的工具复用。

三类场景(外加 conflict 作对照组)的语义,以及「可合并」是怎么被保证的:
见同目录 gen-notes.md。

安全边界 —— 这是会写文件、建分支、跑 merge 的工具,护栏比功能优先:
  * 所有落盘路径强制 PREFIX 前缀。没有前缀的文件**碰都不碰**,更不会被覆盖。
  * 生成前检查工作区:有未提交改动(含未跟踪文件)就拒绝,不在别人的工作区上叠加随机内容。
  * 分支名强制 BRANCH_PREFIX 前缀,`--clean` 只删这个前缀下的东西。
  * `git merge` 只在生成器自己造出来的分支之间跑,且每次跑完立刻回滚到基准提交。
  * 所有 git 参数走列表形式,不经 shell。

确定性:
  * 同一个 (seed, kind, count) 必然产出逐字节相同的内容。
  * 随机源用 sha256 派生,不依赖 `hash()`(它对 str 是每进程加盐的),
    也不依赖 dict 遍历序。

命令行:
    python gen.py --root <repo> --kind mergeable --count 3 --seed 42
    python gen.py --root <repo> --kind all --count 2 --seed 42
    python gen.py --root <repo> --clean
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import gitguard

# ---------------------------------------------------------------- 常量

#: 落盘文件前缀。护栏的核心:没有它,生成器就可能覆盖用户自己的文件。
PREFIX = "aigit_gen_"
#: 分支前缀。`--clean` 只删这个前缀下的分支。
BRANCH_PREFIX = "aigit_gen"
#: 提交信息前缀。便于用户一眼认出哪些提交是生成器造的。
COMMIT_TAG = "aigit"

#: 四类场景。all = 逐类各造 count 个。
KINDS = ("mergeable", "unrelated", "adddel", "conflict")

#: 传给 git 的一次性配置。刻意**不改仓库的 .git/config**:
#: 用 `-c` 逐条传,这样在别人的真实仓库上跑不会留下任何配置副作用。
#:
#: - user.*: 生成提交要作者身份,否则 commit 直接失败。这里覆盖成 aigit,
#:   好处是生成出来的提交在 git log 里自带来源标记。
#: - commit.gpgsign=false: 避免本机配了签名时卡在交互式密码输入上。
#: - core.autocrlf=false: 避免 CRLF 把 diff 搅成整文件重写。
#: - merge.ours.driver / diff.external 等一律不碰,只读原始字节。
#:
#: ⚠️ 上一行是**过时而且方向反了**的注释(gitguard 之前):「不碰」只说明
#: 「不给它传值」,**不等于**「仓库自己写的 merge.<x>.driver 不会执行」。
#: 实测:仓里 `merge.drv.driver` 指向探针时,裸 `git merge --no-edit` 会命中;
#: `filter.<x>.clean` 同理,裸 `git add -A` 就命中。
#: 「不传」不是「不执行」—— 这正是 gitguard 存在的理由。
_GIT_CONFIG = [
    "-c", "user.name=aigit",
    "-c", "user.email=aigit@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
]

GIT_TIMEOUT = 120


class GenError(Exception):
    """生成器可预期的失败(脏工作区、前缀冲突、git 拒绝……)。"""


# ---------------------------------------------------------------- git 封装

class Git:
    """极薄的 git 封装。只做「跑一条命令」和「读工作区状态」两件事。"""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def run(self, args, check=True):
        env = gitguard.safe_env(net_enabled=False)
        p = subprocess.run(
            gitguard.build("git", self.root, args, net_enabled=False,
                           base=env, extra_pre=_GIT_CONFIG),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
            timeout=GIT_TIMEOUT,
        )
        if check and p.returncode != 0:
            raise GenError("git %s 失败(rc=%d): %s"
                           % (" ".join(args[:3]), p.returncode,
                              (p.stderr or p.stdout or "").strip()))
        return p

    def out(self, args):
        return self.run(args).stdout

    # -- 状态 ----------------------------------------------------------
    def is_repo(self):
        return (self.root / ".git").exists()

    def has_head(self):
        return self.run(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0

    def porcelain(self):
        """工作区脏了没有。空字符串 = 干净。"""
        return self.out(["status", "--porcelain=v1", "--untracked-files=all"]).strip()

    def current_branch(self):
        r = self.run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return r.stdout.strip()

    def unmerged(self):
        """冲突文件列表。空 = 没有未合并路径。"""
        return [x for x in self.out(
            ["diff", "--name-only", "--diff-filter=U"]).splitlines() if x.strip()]

    def branch_exists(self, name):
        return self.run(["rev-parse", "--verify", "--quiet",
                         "refs/heads/" + name], check=False).returncode == 0

    def local_branches(self):
        return [x for x in self.out(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"]
        ).splitlines() if x.strip()]

    def tracked_files(self):
        return [x for x in self.out(["ls-files", "-z"]).split("\0") if x]


# ---------------------------------------------------------------- 内容生成

_NOUNS = ["cache", "session", "buffer", "packet", "record", "shard", "token",
          "cursor", "worker", "router", "config", "profile", "ledger", "vector",
          "matrix", "channel", "segment", "handle", "region", "bucket"]
_VERBS = ["load", "store", "flush", "resolve", "commit", "rebuild", "probe",
          "compact", "encode", "decode", "scan", "merge", "purge", "rebalance"]
_QUALS = ["primary", "shadow", "pending", "stale", "warm", "cold", "local",
          "remote", "legacy", "shared"]
_ADJ = ["total", "current", "next", "last", "inner", "outer", "raw", "final"]


def _name(rnd):
    return "%s_%s" % (rnd.choice(_NOUNS), rnd.choice(_VERBS))


def _phrase(rnd):
    return " ".join(rnd.choice(_ADJ + _NOUNS + _VERBS)
                    for _ in range(rnd.randint(3, 7)))


def _stmt(rnd, depth):
    """造一行看起来像代码的语句。长度刻意不均匀(短到长都有),
    这样 diff 视图不会退化成满屏横向滚动。"""
    ind = "    " * depth
    k = rnd.randrange(9)
    a, b = _name(rnd), _name(rnd)
    if k == 0:
        return f"{ind}{a} = {b}({a}, timeout={rnd.randint(1, 90)})"
    if k == 1:
        return f"{ind}if {a}.{rnd.choice(_QUALS)} > {rnd.randint(0, 999)}:"
    if k == 2:
        return f"{ind}{b} = max({a}, {rnd.randint(0, 64)})  # {_phrase(rnd)}"
    if k == 3:
        return f'{ind}log.{rnd.choice(("info", "warn", "debug"))}("{_phrase(rnd)}")'
    if k == 4:
        return f"{ind}for i in range({rnd.randint(2, 32)}):"
    if k == 5:
        return f"{ind}assert isinstance({a}, dict), {_phrase(rnd)}"
    if k == 6:
        return f"{ind}{a}[{rnd.randint(0, 8)}] = {rnd.choice((True, False, None, rnd.randint(0, 999)))}"
    if k == 7:
        return f"{ind}yield {a}"
    return f"{ind}return {a}  # {_phrase(rnd)}"


#: 每个生成文件都带的首尾样板行。作用有两个:
#:   1. 保证文件里**一定有**重复行(搜索场景的素材),不靠随机数碰运气;
#:   2. 让不同文件之间也共享同一段内容 —— 真实代码库就是这样,
#:      多个文件共用同一份 license 头。
_BOILERPLATE = [
    "# aigit generated fixture -- do not edit by hand",
    "# regenerate with: python gen.py --root <repo> --kind <kind>",
]


def _code_block(rnd, n, boilerplate=None):
    """生成 n 行"结构化随机"文本。

    刻意保留旧版 `_random_lines` 的优点(行数固定 → 行号稳定 → diff 干净),
    但升级四处(每一条都有实测支撑,见 gen-notes.md):
      1. 每行长度有变化 —— 实测 76 行里 41 种不同长度、跨度 17~84 字符,
         不再是等长占位符。否则 diff 视图退化成满屏横向滚动。
      2. 约 8% 的行是**前面某行的逐字重复**,加上首尾样板行保底。
         给"在文件里找某一行"这类搜索场景提供素材。
      3. 缩进层级会变化(0/4/8/12 四层),读起来像真的代码,
         而不是 `line 1 alpha beta`。
      4. 首尾各插两行样板,让文件像有 license 头的真实文件。

    注意:样板行会额外加在 n 行**之外**,所以返回值长度是 n + 2*len(boilerplate)。
    调用方传的 n 是"内容行数",调用处依赖这一点算行号区间,改这里要一起改。
    """
    head = list(_BOILERPLATE if boilerplate is None else boilerplate)
    tail = list(_BOILERPLATE if boilerplate is None else boilerplate)
    out = list(head)
    depth = 0
    # 缩进用一个**对称**的随机游走控制:进/出嵌套的概率接近,
    # 深度封顶 3。早先的写法按行型分支决定加不减(进 35% / 退 10%),
    # 结果深度一路涨到顶,80 行里 48 行缩进 12 空格,看起来像
    # `def` 嵌在 `if` 里面 —— 那是假代码。这里改回游走后实测分布
    # 落在 {0:~30, 4:~25, 8:~25, 12:~20} 这个量级。
    up_p, down_p = 0.11, 0.14
    for _ in range(n):
        r = rnd.random()
        if len(out) > len(head) and r < 0.08:
            # 逐字重复行。跳过样板区,否则重复只会堆在开头。
            out.append(rnd.choice(out[len(head):]))
            continue
        u = rnd.random()
        if u < up_p and depth < 3:
            depth += 1
        elif u < up_p + down_p and depth > 0:
            depth -= 1
        if r < 0.15:
            out.append("    " * depth + "# " + _phrase(rnd))   # 注释
        elif r < 0.26:
            out.append("    " * depth + "def %s(%s, *, %s=%d):"
                       % (_name(rnd), rnd.choice(_NOUNS),
                          rnd.choice(_QUALS), rnd.randint(0, 16)))
        elif r < 0.37:
            out.append("    " * depth + "for i in range(%d):" % rnd.randint(2, 32))
        elif r < 0.49:
            out.append("    " * depth + "if %s.%s > %d:"
                       % (_name(rnd), rnd.choice(_QUALS), rnd.randint(0, 999)))
        else:
            out.append(_stmt(rnd, depth))
    out.extend(tail)
    return out


def _rnd_for(seed, kind, index, salt=""):
    """从 (seed, kind, index) 派生独立且稳定的随机源。

    用 sha256 而不是内置 hash():后者对 str 每进程加盐,跨进程不稳定,
    确定性就无从谈起。
    """
    h = hashlib.sha256(("%s|%s|%d|%s" % (seed, kind, index, salt)).encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


# ---------------------------------------------------------------- 生成器

class ScenarioGen:
    """在指定仓库里造可合并 / 无关 / 增删 / 冲突 四类测试场景。

    用法:
        g = ScenarioGen("/path/to/repo")
        result = g.generate("mergeable", count=3, seed=42)
        print(result["summary"])
        g.clean()
    """

    def __init__(self, root, verbose=False):
        self.git = Git(root)
        self.verbose = verbose

    # -- 护栏 ----------------------------------------------------------
    def _log(self, msg):
        if self.verbose:
            print("  [gen] " + msg)

    def _check_repo(self):
        if not self.git.is_repo():
            raise GenError("不是一个 git 仓:%s" % self.git.root)
        if not self.git.has_head():
            raise GenError("仓里还没有任何提交(HEAD 不存在),先建一个初始提交")

    def _check_clean(self):
        """有未提交改动就拒绝。理由:在别人的工作区上叠加随机内容,
        等于把别人的活儿搅成一团,而且事后分不清哪些改动是谁造的。"""
        dirty = self.git.porcelain()
        if dirty:
            lines = dirty.splitlines()
            head = "\n      ".join(lines[:8])
            more = ("\n      ...(共 %d 行)" % len(lines)) if len(lines) > 8 else ""
            raise GenError(
                "工作区不干净,拒绝生成(有 %d 条未提交改动):\n      %s%s\n"
                "    先 commit / stash / checkout 干净,或者在演练仓上跑。"
                % (len(lines), head, more))

    def _assert_prefix(self, rel):
        """所有落盘/删除路径都必须带 PREFIX。这是「绝不碰用户文件」的唯一保证。"""
        rel = str(rel).replace("\\", "/").lstrip("./")
        if not rel.startswith(PREFIX):
            raise GenError("拒绝操作没有 %s 前缀的路径:%s" % (PREFIX, rel))
        if ".." in Path(rel).parts:
            raise GenError("路径含 ..,拒绝:%s" % rel)
        return rel

    def _abs(self, rel):
        rel = self._assert_prefix(rel)
        p = (self.git.root / rel).resolve()
        try:
            p.relative_to(self.git.root)
        except ValueError:
            raise GenError("路径越界,拒绝:%s" % rel)
        return p

    def _write(self, rel, lines):
        p = self._abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 显式 newline="\n":否则 Windows 上默认写 CRLF,seed 文件在别的机器上
        # 生成的同 seed 内容就逐字节不同了,确定性直接失效。
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        return rel

    def _delete(self, rel):
        p = self._abs(rel)
        if p.is_file():
            p.unlink()

    # -- 提交helper ----------------------------------------------------
    def _commit(self, message):
        self.git.run(["add", "-A"])
        self.git.run(["commit", "-q", "-m", message])
        return self.git.out(["rev-parse", "HEAD"]).strip()

    # ================================================================
    # 场景构造
    # ================================================================
    #
    # 四类场景共用同一套骨架,差别只在「两个分支各改了什么」:
    #
    #   1. 造该场景专属的种子文件(名字带 index,场景之间零重叠)
    #   2. 提交,记下这个提交作为 **base**(本场景的基准)
    #   3. 两个分支都从 **base 这个 sha** 拉出来,不是从 main 拉的
    #   4. 两个分支各自从 base 的内容出发改自己的东西
    #   5. 真跑一次 merge 验证语义
    #   6. 把 main 回滚到 base,交给用户手动试
    #
    # 第 3、4 步是旧实现最大的 bug 所在:旧代码用**共享的可变 lines 列表**,
    # 于是分支 B 的内容里混着分支 A 的改动。merge 确实不冲突,但不是因为
    # 「改了不同区域」,而是因为两边把那行改成了**一模一样**的文字。
    # 这里每个分支都从 base 重新读一遍原始内容,互不污染。

    def _seed_files(self, kind, index, rnd):
        """造本场景的种子文件,返回 (相对路径列表, 路径->行数)。"""
        f = {}
        if kind == "mergeable":
            # 一个文件,行数够多才能让「头部块」和「尾部块」离得足够远
            f[PREFIX + "merge_%d.txt" % index] = rnd.randint(56, 104)
        elif kind == "unrelated":
            # 两个文件,两个分支各改一个
            f[PREFIX + "unrel_a_%d.txt" % index] = rnd.randint(18, 40)
            f[PREFIX + "unrel_b_%d.txt" % index] = rnd.randint(18, 40)
        elif kind == "adddel":
            # doomed 会被一个分支删掉;filler 只是为了让提交不至于太小
            f[PREFIX + "adddel_keep_%d.txt" % index] = rnd.randint(14, 30)
            f[PREFIX + "adddel_doomed_%d.txt" % index] = rnd.randint(14, 30)
        elif kind == "conflict":
            f[PREFIX + "clash_%d.txt" % index] = rnd.randint(40, 80)
        else:
            raise GenError("未知 kind:%r" % kind)
        return {k: _code_block(rnd, v) for k, v in f.items()}

    def _branch_names(self, kind, index):
        tag = "%s/%s/%d" % (BRANCH_PREFIX, kind, index)
        return tag + "/a", tag + "/b"

    # -- 四类场景的「两个分支各改什么」 -------------------------------
    def _apply_mergeable(self, rnd, base_lines, a_name, b_name):
        """mergeable:同一文件的**不同区域** —— A 改头部一块,B 改尾部一块。

        保证可合并的做法不是「祈祷」,而是构造出三路合并的经典条件:
        两个改动块之间留足 **>= 20 行**完全未触碰的内容。
        git 的 merge 只在改动**相邻或重叠**时才产生冲突,间隔这么大
        必然落在两个独立的 hunk 上。验证阶段还会真跑一遍 merge 复核。
        """
        rel = next(iter(base_lines))
        seed = base_lines[rel]
        n = len(seed)
        a_len = rnd.randint(3, 7)
        b_len = rnd.randint(3, 7)
        a_start = 0
        b_start = n - b_len
        gap = b_start - (a_start + a_len)
        if gap < 20:                       # 行数不够就加长,而不是让它们贴上
            raise GenError("内部错误:种子行数不足以留出安全间隔(n=%d gap=%d)"
                           % (n, gap))

        a_lines = list(seed)                       # 从 base 重新拷,不复用对方
        a_lines[a_start:a_start + a_len] = _code_block(rnd, a_len)

        b_lines = list(seed)                       # 同上,各自独立
        b_lines[b_start:b_start + b_len] = _code_block(rnd, b_len)

        return {
            "files": [rel],
            "a_edit": "头部第 %d 行起 %d 行" % (a_start + 1, a_len),
            "b_edit": "尾部第 %d 行起 %d 行" % (b_start + 1, b_len),
            "gap_lines": gap,
        }, {rel: a_lines}, {rel: b_lines}

    def _apply_unrelated(self, rnd, base_lines, a_name, b_name):
        """unrelated:两个分支改**完全不同的文件** → 三路合并根本不会碰到同一文件。"""
        rels = list(base_lines.keys())
        a_rel, b_rel = rels[0], rels[1]
        a_lines = _code_block(rnd, rnd.randint(6, 14))
        b_lines = _code_block(rnd, rnd.randint(6, 14))
        return {
            "files": rels,
            "a_edit": "改写 %s" % a_rel,
            "b_edit": "改写 %s" % b_rel,
        }, {a_rel: a_lines}, {b_rel: b_lines}

    def _apply_adddel(self, rnd, base_lines, a_name, b_name):
        """adddel:A **新增**一个文件,B **删除**另一个文件。

        这两个动作落在不同路径上,所以必然干净。git 真正会冲突的组合是:
            同路径 新增/新增、删除/修改、修改/修改(重叠)
        增 + 删(不同路径)不在其中 —— 这正是这个场景要演示的东西。
        """
        rels = list(base_lines.keys())
        keep_rel, doomed_rel = rels[0], rels[1]
        fresh_rel = PREFIX + "adddel_fresh_%d.txt" % int(
            hashlib.sha256(doomed_rel.encode()).hexdigest()[:4], 16)
        fresh = _code_block(rnd, rnd.randint(10, 26))
        return {
            "files": rels + [fresh_rel],
            "a_edit": "新增 %s" % fresh_rel,
            "b_edit": "删除 %s" % doomed_rel,
        }, {fresh_rel: fresh}, {doomed_rel: None}

    def _apply_conflict(self, rnd, base_lines, a_name, b_name):
        """conflict(对照组):两个分支改**同一文件的同一行** → 必然冲突。"""
        rel = next(iter(base_lines))
        seed = base_lines[rel]
        target = rnd.randrange(1, len(seed) - 1)   # 不取首尾,避免空行干扰
        a_lines = list(seed)
        a_lines[target] = "    # A-EDIT " + _phrase(rnd)
        b_lines = list(seed)
        b_lines[target] = "    # B-EDIT " + _phrase(rnd)
        return {
            "files": [rel],
            "a_edit": "改第 %d 行" % (target + 1),
            "b_edit": "改第 %d 行(同一行)" % (target + 1),
        }, {rel: a_lines}, {rel: b_lines}

    # -- 合并验证 ------------------------------------------------------
    def _verify(self, base, a_name, b_name, expect_clean):
        """**真跑一遍 git merge**,不靠推断。

        判定「可合并」的两条硬标准(缺一不可):
          1. 两次 merge 全部 rc == 0
          2. 结束后 `git status --porcelain` 为空(工作区干净)
          3. 结束后 `git diff --diff-filter=U` 为空(没有未合并路径)

        跑完立刻回滚到 base,把干净的分支留给用户手动试。
        """
        def rollback():
            self.git.run(["merge", "--abort"], check=False)
            self.git.run(["checkout", "-q", "-f", "main"], check=False)
            self.git.run(["reset", "--hard", "-q", base], check=False)

        self.git.run(["checkout", "-q", "-f", "main"], check=False)
        self.git.run(["reset", "--hard", "-q", base], check=False)

        rec = {"merged_ok": None, "worktree_clean": None, "unmerged": None,
               "first_merge_ok": None, "merge_output": ""}

        if expect_clean:
            m1 = self.git.run(["merge", "--no-edit", "-q", a_name], check=False)
            m2 = self.git.run(["merge", "--no-edit", "-q", b_name], check=False)
            rec["merge_output"] = ((m1.stdout + m1.stderr) +
                                   (m2.stdout + m2.stderr)).strip()
            rec["first_merge_ok"] = (m1.returncode == 0)
            rec["merged_ok"] = (m1.returncode == 0 and m2.returncode == 0)
            rec["worktree_clean"] = (self.git.porcelain() == "")
            rec["unmerged"] = self.git.unmerged()
        else:
            # 对照组:先合 A(应当成功 —— A 是 base 的直接后代,是快进),
            # 再合 B 才会撞上同一行。**只合 A 是看不出冲突的**,
            # 因为 A 相对 main 是快进,压根不做内容合并。
            m1 = self.git.run(["merge", "--no-edit", "-q", a_name], check=False)
            m2 = self.git.run(["merge", "--no-edit", "-q", b_name], check=False)
            rec["merge_output"] = ((m1.stdout + m1.stderr) +
                                   (m2.stdout + m2.stderr)).strip()
            rec["first_merge_ok"] = (m1.returncode == 0)
            # merged_ok 的含义全文件统一:**两次 merge 是否都无冲突地完成**。
            # 对照组期望它为 False,于是 _verdict 才有一致的判据可写。
            rec["merged_ok"] = (m1.returncode == 0 and m2.returncode == 0)
            rec["unmerged"] = self.git.unmerged()
            # 冲突态下工作区必然不干净,这一项不参与判定
            rec["worktree_clean"] = None

        rollback()
        return rec

    @staticmethod
    def _verdict(rec, expect_clean):
        if expect_clean:
            ok = (rec["merged_ok"] is True
                  and rec["worktree_clean"] is True
                  and rec["unmerged"] == [])
        else:
            ok = (rec["merged_ok"] is False and len(rec["unmerged"]) > 0)
        return ok

    # ================================================================
    # 公开入口
    # ================================================================
    def generate(self, kind, count=3, seed=None):
        """造 count 个场景。kind ∈ mergeable/unrelated/adddel/conflict/all。

        返回 dict,含每个场景的分支、文件、以及 **真跑 merge 得到的验证结果**。
        同一 (seed, kind, count) 必然产出逐字节相同的内容。
        """
        if kind not in KINDS + ("all",):
            raise GenError("kind 只能是 %s 或 all" % " / ".join(KINDS))
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise GenError("count 必须是整数,收到 %r" % (count,))
        if count < 1:
            raise GenError("count 至少为 1,收到 %d" % count)

        self._check_repo()
        self._check_clean()

        if seed is None:
            # 不给 seed 就用真随机,并且**回写**到返回值里,让调用方能复现。
            seed = random.randrange(1 << 30)

        kinds = KINDS if kind == "all" else (kind,)

        # 分支已存在就拒绝,而不是覆盖。防止同一个仓反复生成时互相踩。
        want = []
        for k in kinds:
            for i in range(count):
                want += list(self._branch_names(k, i))
        clash = [b for b in want if self.git.branch_exists(b)]
        if clash:
            raise GenError(
                "以下分支已存在,拒绝生成(先跑 --clean,或换 seed):\n      "
                + "\n      ".join(clash[:10])
                + ("\n      ...(共 %d 个)" % len(clash) if len(clash) > 10 else ""))

        if not self.git.branch_exists("main"):
            raise GenError("仓里没有 main 分支,无法作为场景基准点")

        result = {
            "ok": True, "kind": kind, "seed": seed, "count": count,
            "kinds": list(kinds), "scenarios": [], "branches": [], "files": [],
            "prefix": PREFIX, "branchPrefix": BRANCH_PREFIX,
        }
        all_ok = True

        for k in kinds:
            for i in range(count):
                sc = self._make_scenario(k, i, seed)
                result["scenarios"].append(sc)
                result["branches"] += sc["branches"]
                result["files"] += sc["files"]
                all_ok = all_ok and sc["verified"]
                self._log("%s #%d -> %s  %s" % (k, i, sc["branches"],
                                                "OK" if sc["verified"] else "FAIL"))

        result["allVerified"] = all_ok
        result["ok"] = all_ok          # 验证没过就整体报失败,不假装成功
        result["summary"] = (
            "已生成 %d 个场景(%s),分支前缀 %s/,文件前缀 %s;"
            "真跑 merge 验证:%s。合并完切回 main 再 reset。"
            % (len(result["scenarios"]), "/".join(kinds), BRANCH_PREFIX, PREFIX,
               "全部符合预期" if all_ok else "**有场景未达预期,详见 verified 字段**"))
        return result

    def _make_scenario(self, kind, index, seed):
        rnd = _rnd_for(seed, kind, index)
        a_name, b_name = self._branch_names(kind, index)
        base_lines = self._seed_files(kind, index, rnd)

        for rel, lines in base_lines.items():
            self._write(rel, lines)
        base = self._commit("%s: seed %s #%d (%d file(s))"
                            % (COMMIT_TAG, kind, index, len(base_lines)))

        plan, a_edit, b_edit = self._APPLY[kind](self, rnd, base_lines, a_name, b_name)

        # 分支 A:从 base 这个 sha 拉,不是从 main —— 这样即使 main 后来动了,
        # 本场景的两个分支也永远只差 base 一个提交,验证才有意义。
        self.git.run(["checkout", "-q", "-b", a_name, base])
        self._materialize(a_edit, base_lines)
        self._commit("%s: %s #%d A: %s" % (COMMIT_TAG, kind, index, plan["a_edit"]))

        # 分支 B:同样从 base 拉,内容同样从 base_lines 重新算 —— 不碰 A 的产物。
        self.git.run(["checkout", "-q", "-f", base])
        self.git.run(["checkout", "-q", "-b", b_name, base])
        self._materialize(b_edit, base_lines)
        self._commit("%s: %s #%d B: %s" % (COMMIT_TAG, kind, index, plan["b_edit"]))

        self.git.run(["checkout", "-q", "-f", base])

        expect_clean = (kind != "conflict")
        rec = self._verify(base, a_name, b_name, expect_clean)
        verdict = self._verdict(rec, expect_clean)

        sc = {
            "id": "%s#%d" % (kind, index),
            "kind": kind, "index": index, "base": base[:10],
            "branches": [a_name, b_name],
            "files": sorted(set(plan["files"])),
            "aEdit": plan["a_edit"], "bEdit": plan["b_edit"],
            "expect": ("合并成功:status 干净且 U 为空" if expect_clean
                       else "合并必然冲突:U 列表非空"),
            "verified": verdict,
            "verifyDetail": {
                "merged_ok": rec["merged_ok"],
                "first_merge_ok": rec["first_merge_ok"],
                "worktree_clean": rec["worktree_clean"],
                "unmerged": rec["unmerged"],
            },
        }
        if "gap_lines" in plan:
            sc["gapLines"] = plan["gap_lines"]
        if not verdict:
            sc["mergeOutput"] = rec["merge_output"]
        return sc

    def _materialize(self, edits, base_lines):
        """把「这个分支要产出的文件状态」落到工作区。

        edits 的值是 {相对路径: 行列表 或 None}。None 表示删除该文件。
        """
        for rel, lines in edits.items():
            if lines is None:
                self._delete(rel)
            else:
                self._write(rel, lines)

    _APPLY = {
        "mergeable": _apply_mergeable,
        "unrelated": _apply_unrelated,
        "adddel": _apply_adddel,
        "conflict": _apply_conflict,
    }

    # -- 清理 ----------------------------------------------------------
    def clean(self, commit=True):
        """**只**删生成器自己造出来的东西:BRANCH_PREFIX 的分支 + PREFIX 的文件。

        两道保险:
          * 分支按名字前缀过滤,其余一个不碰。
          * 文件按名字前缀过滤,而且只删工作区里**确实存在**的;
            对已被跟踪的用 `git rm -f`(会同步更新索引),未跟踪的直接 unlink。

        关于 commit 参数(默认 True):
            `git rm` 只把文件从索引里摘掉,HEAD 里那份还在,于是仓的状态是
            「12 个已暂存的删除」—— 文件确实没了,但 `git status` 一片红,
            这不叫「收拾干净」。所以默认把这次删除提交掉,让仓回到干净状态。
            提交内容只有 aigit 自己的文件被删除,不含任何用户改动
            (生成阶段已经要求工作区干净,这里又只按前缀删)。
            不想让生成器产生提交就传 commit=False。
        """
        self._check_repo()
        removed = {"branches": [], "files": [], "commit": None}
        main_exists = self.git.branch_exists("main")
        if main_exists:
            self.git.run(["checkout", "-q", "-f", "main"], check=False)
        self.git.run(["merge", "--abort"], check=False)

        for b in self.git.local_branches():
            if b == BRANCH_PREFIX or b.startswith(BRANCH_PREFIX + "/"):
                self.git.run(["branch", "-D", b], check=False)
                removed["branches"].append(b)

        tracked = set(self.git.tracked_files())
        for rel in list(tracked):
            if rel.startswith(PREFIX):
                self.git.run(["rm", "-q", "-f", "--", rel], check=False)
                removed["files"].append(rel)

        # 未跟踪的:扫工作区(跳过 .git),只删带前缀的
        for dirpath, dirnames, filenames in os.walk(self.git.root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), self.git.root)
                rel = rel.replace("\\", "/")
                if rel.startswith(PREFIX) and rel not in tracked:
                    try:
                        os.unlink(os.path.join(dirpath, fn))
                        removed["files"].append(rel)
                    except OSError:
                        pass
        removed["files"] = sorted(set(removed["files"]))

        if commit and self.git.porcelain():
            self.git.run(["add", "-A"])
            self.git.run(["commit", "-q", "-m",
                          "%s: clean %d generated file(s)" % (COMMIT_TAG,
                                                              len(removed["files"]))])
            removed["commit"] = self.git.out(["rev-parse", "HEAD"]).strip()[:10]

        return {"ok": True, "removed": removed,
                "worktree_clean": self.git.porcelain() == "",
                "summary": "已删除 %d 个分支、%d 个文件(只删 %s / %s 前缀的);"
                           "工作区%s"
                           % (len(removed["branches"]), len(removed["files"]),
                              BRANCH_PREFIX + "/", PREFIX,
                              "干净" if self.git.porcelain() == ""
                              else "仍不干净(有未暂存改动)")}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="aigit 测试数据生成器(可合并 / 无关 / 增删 / 冲突)")
    ap.add_argument("--root", required=True, help="目标仓库路径")
    ap.add_argument("--kind", default="all", help="mergeable/unrelated/adddel/conflict/all")
    ap.add_argument("--count", type=int, default=3, help="每类造几个场景(默认 3)")
    ap.add_argument("--seed", default=None, help="随机种子;不给则真随机并回写")
    ap.add_argument("--clean", action="store_true", help="只删生成器自己造的东西")
    ap.add_argument("--no-commit", action="store_true",
                    help="配合 --clean:不把删除提交掉(默认会提交,让仓回到干净状态)")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    g = ScenarioGen(a.root, verbose=a.verbose)
    try:
        if a.clean:
            out = g.clean(commit=not a.no_commit)
        else:
            out = g.generate(a.kind, a.count, a.seed)
    except GenError as e:
        print("生成失败:%s" % e, file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if a.clean:
            print(out["summary"])
            for b in out["removed"]["branches"]:
                print("  - 分支 %s" % b)
            for f in out["removed"]["files"]:
                print("  - 文件 %s" % f)
        else:
            print(out["summary"])
            for sc in out["scenarios"]:
                print("  [%s] %s" % ("通过" if sc["verified"] else "未通过", sc["id"]))
                print("      分支   %s" % " , ".join(sc["branches"]))
                print("      文件   %s" % ", ".join(sc["files"]))
                print("      A 改动 %s" % sc["aEdit"])
                print("      B 改动 %s" % sc["bEdit"])
                print("      验证   merged_ok=%s clean=%s unmerged=%s"
                      % (sc["verifyDetail"]["merged_ok"],
                         sc["verifyDetail"]["worktree_clean"],
                         sc["verifyDetail"]["unmerged"] or "[]"))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
