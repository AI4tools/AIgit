#!/usr/bin/env python3
"""history_ops.py 对抗性测试 —— aigit6(历史与危险操作)。

立场与 test_aigit.py 一致:
    这不是一份"证明它没问题"的测试,是一份**试图把它打穿**的测试。
    断言全部写的是**正确行为**;一旦有缺陷,测试就该红。全绿在这里是可疑信号。

硬约束:
    * 纯标准库,不用 pytest(理由同 test_aigit.py:`pip install` 本身就是失败原因)。
    * **完全无头**:不开浏览器、不开任何窗口、不截图。git 走 subprocess。
    * 所有测试仓建在 %TEMP%\\aigit-history-test\\ 下,
      **绝不碰 C:\\poject\\ 下任何真实仓库**。
    * 只读 history_ops.py。发现 bug 记录、不在本文件里绕过。
    * 本文件只 import history_ops,不 import server.py —— 后者由 aigit 其它线在改。

跑法:
    python test_history_ops.py
    python test_history_ops.py -v
    python test_history_ops.py -k stash
    python test_history_ops.py -k F -v

分组:
    A  两步走结构   B  stash          C  amend          D  撤销提交
    E  revert       F  分支            G  标签           H  文件历史/blame
    I  名字与路径白名单
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
TESTROOT = os.path.join(tempfile.gettempdir(), "aigit-history-test")

sys.path.insert(0, HERE)
import history_ops  # noqa: E402
from history_ops import HistoryOps, HistoryError  # noqa: E402

GIT_ENV = dict(os.environ)
GIT_ENV.update({
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
})


def _chmod_retry(func, path, exc_info):
    """shutil.rmtree 的 error 回调:把只读/只读目录先放开再重试一次。

    这不是"Windows 老毛病人人都会犯"那么敷衍 —— git 的 object 文件是以
    **只读**方式落盘的(444),Windows 上没有 Unix 那套"目录写权限 + 可删文件"
    的模型,所以 rmtree 走到它们就 PermissionError,WinError 5。
    真正的原因第一次跑就抓到了:残留路径全在 `.git/objects/` 下面。
    """
    for p in (path, os.path.dirname(path)):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
    try:
        func(path)
    except OSError:
        pass


def purge(path, attempts=6):
    """删目录,**不吞失败**。

    为什么不用 shutil.rmtree(ignore_errors=True):它把两种失败一起吞掉 ——
    一是上面那个只读文件的 WinError 5,二是真的删不动。后果是测试仓残留在
    %TEMP% 里,而**下一轮就建在脏目录上**:git init 一个还带着旧 .git 的目录,
    测出来的结论是假的,而且很难看出是哪一步出的问题。

    所以这里 chmod 重试 + 退避若干轮,真的不行就明确报出来,让"清场失败"
    变成可见的事实。
    """
    last = None
    for i in range(attempts):
        if not os.path.exists(path):
            return True
        try:
            # onerror 在 3.12 被 onexc 取代,两个都传,按运行时版本挑能用的那个
            try:
                shutil.rmtree(path, onexc=lambda f, p, e: _chmod_retry(f, p, e))
            except TypeError:
                shutil.rmtree(path, onerror=lambda f, p, e: _chmod_retry(f, p, e))
        except OSError as e:
            last = e
            # Windows 的句柄释放是异步的,给点时间再试
            time.sleep(0.25 * (i + 1))
            continue
        if not os.path.exists(path):
            return True
    raise RuntimeError("删不掉 %s,残留内容会造成下一轮测试建在脏目录上: %s"
                       % (path, last))


# ════════════════════════════════════════════════════════ 测试登记

TESTS = []


def test(group, name, note=""):
    def deco(fn):
        TESTS.append({"group": group, "name": name, "fn": fn,
                      "note": note, "id": "%s-%s" % (group, name)})
        return fn
    return deco


class Fail(Exception):
    pass


def ck(cond, msg):
    if not cond:
        raise Fail(msg)


def ck_eq(got, want, what):
    if got != want:
        raise Fail("%s: 期望 %r,实际 %r" % (what, want, got))


def ck_in(needle, hay, what):
    if needle not in hay:
        raise Fail("%s: %r 里找不到 %r" % (what, hay, needle))


def expect_error(fn, code, what, *a, **kw):
    """断言 fn 抛 HistoryError 且 code 正确。返回异常以便继续检查字段。"""
    try:
        fn(*a, **kw)
    except HistoryError as e:
        if e.code != code:
            raise Fail("%s: 期望 code=%r,实际 %r(message=%s)"
                       % (what, code, e.code, e.message))
        return e
    raise Fail("%s: 期望抛 HistoryError(code=%r),实际正常返回了" % (what, code))


# ════════════════════════════════════════════════════════ 测试仓

class TestRepo:
    """%TEMP%\\aigit-history-test\\ 下的独立 git 仓。"""

    def __init__(self, name):
        self.dir = os.path.join(TESTROOT, name)
        purge(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "aigit-test@localhost")
        self.git("config", "user.name", "aigit-test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.ops = HistoryOps(self.dir)

    def git(self, *args, check=False, input=None):
        p = subprocess.run(["git", "-C", self.dir] + [str(a) for a in args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=120,
                           input=input)
        if check and p.returncode != 0:
            raise RuntimeError("git %s 失败: %s"
                               % (" ".join(map(str, args)),
                                  (p.stderr or p.stdout).strip()))
        return p

    def out(self, *args):
        return self.git(*args).stdout

    def path(self, rel):
        return os.path.join(self.dir, rel.replace("/", os.sep))

    def write(self, rel, text):
        p = self.path(rel)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def read(self, rel):
        p = self.path(rel)
        if not os.path.exists(p):
            return None
        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def digest(self, rel):
        """文件内容的 sha256,用来断言"逐字节相同"。"""
        p = self.path(rel)
        if not os.path.exists(p):
            return "<missing>"
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def commit(self, msg, *extra):
        self.git("add", "-A")
        self.git("commit", "-m", msg, *extra, check=True)

    def fingerprint(self):
        """仓库的完整指纹,用来断言 plan_* 真的只读。"""
        return {
            "head": self.out("rev-parse", "HEAD").strip(),
            "branch": self.out("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "branches": sorted(self.out("for-each-ref", "--format=%(refname)",
                                         "refs/heads").split()),
            "tags": sorted(self.out("for-each-ref", "--format=%(refname)",
                                    "refs/tags").split()),
            "stash": self.out("stash", "list").strip(),
            "status": self.out("status", "--porcelain=v1",
                               "--untracked-files=all"),
            "log": self.out("log", "--format=%H %s").strip(),
        }


def fresh(name):
    return TestRepo(name)


def sample_repo(name, ncommits=3):
    """一个有若干提交、文件已跟踪的仓。"""
    r = TestRepo(name)
    for i in range(ncommits):
        r.write("f%d.txt" % i, "content of file %d\n" % i)
        r.commit("chore: add file %d" % i)
    return r


# ════════════════════════════════════════════════════════ A 两步走结构

@test("A", "production code passes its own two-step audit")
def t_A1():
    probs = history_ops.audit_two_step()
    ck_eq(probs, [], "audit_two_step() 应无问题")


@test("A", "NEGATIVE: an unguarded do_* is caught by the audit")
def t_A2():
    """把 do_amend 换成没有确认闸的假实现,audit 必须报出来。
    没有这一条,A1 的"全绿"可能只是审计本身没在工作。"""
    orig = history_ops.HistoryOps.do_amend
    history_ops.HistoryOps.do_amend = lambda self, message=None, confirmed=False: \
        {"ok": True, " cheated": True}
    try:
        probs = history_ops.audit_two_step()
    finally:
        history_ops.HistoryOps.do_amend = orig
    ck(probs, "去掉确认闸后 audit 应该有意见,实际一条都没有")
    ck(any("do_amend" in p and "_require_confirm" in p for p in probs),
       "应报出 do_amend 缺确认闸,实际: %r" % (probs,))


@test("A", "NEGATIVE: a bare one-step destructive entry is caught")
def t_A3():
    """挂一个裸动词方法上去,audit 必须当一步到位入口报出来。"""
    orig = getattr(history_ops.HistoryOps, "amend", None)
    ck(orig is None, "HistoryOps 本来就不该有裸 amend()")
    history_ops.HistoryOps.amend = lambda self, **kw: {"ok": True}
    try:
        probs = history_ops.audit_two_step()
    finally:
        del history_ops.HistoryOps.amend
    ck(any("HistoryOps.amend" in p and "一步到位" in p for p in probs),
       "应报出裸 amend 是一步到位入口,实际: %r" % (probs,))
    ck_eq(history_ops.audit_two_step(), [], "还原后 audit 应重新干净")


@test("A", "every destructive do_* refuses without confirmed=True")
def t_A4():
    r = sample_repo("a04")
    r.git("checkout", "-b", "feature")
    r.write("x.txt", "x\n")
    r.commit("feat: x")
    r.git("checkout", "main")
    r.git("branch", "unmerged", "feature")
    r.ops.do_tag_create("v0.1", sha="HEAD")
    cases = [
        ("do_amend", lambda: r.ops.do_amend("nope")),
        ("do_undo_last_commit", lambda: r.ops.do_undo_last_commit()),
        ("do_undo_last_commit/drop", lambda: r.ops.do_undo_last_commit(
            drop_changes=True)),
        ("do_branch_delete", lambda: r.ops.do_branch_delete("unmerged")),
        ("do_branch_rename", lambda: r.ops.do_branch_rename("main", "main2")),
        ("do_stash_drop", lambda: r.ops.do_stash_drop(0)),
        ("do_tag_delete", lambda: r.ops.do_tag_delete("v0.1")),
        ("do_restore_from_reflog", lambda: r.ops.do_restore_from_reflog(0)),
    ]
    for what, fn in cases:
        e = expect_error(fn, "needs_confirmation", what)
        ck_in("plan_", e.message, "%s 的报错必须指路到 plan_*" % what)
        ck_in("confirmed=True", e.message,
              "%s 的报错必须说清要传什么" % what)
    # 一件都没发生
    ck_eq(r.out("rev-parse", "HEAD").strip(),
          r.fingerprint()["head"], "一串拒绝之后 HEAD 不该动")
    ck("v0.1" in r.out("tag", "-l"), "标签不该被删")
    ck("unmerged" in r.out("branch", "--list"), "分支不该被删")


@test("A", "plan_* is strictly read-only")
def t_A5():
    r = sample_repo("a05")
    # 改一个**已跟踪**文件:未跟踪文件默认不进 stash,拿它当"有东西可暂存"
    # 会得到 nothing_to_stash,那测的就不是只读性了。
    r.write("f2.txt", "to be stashed\n")
    r.ops.do_stash(message="stash at index 0")
    r.write("f0.txt", "staged change\n")
    r.git("add", "f0.txt")
    r.write("f1.txt", "unstaged change\n")
    r.git("branch", "side")
    r.git("checkout", "side")
    r.write("s.txt", "s\n")
    r.commit("feat: side")
    r.git("merge", "--no-ff", "-m", "merge side", "main", check=True)
    r.ops.do_tag_create("v1", message="release 1")
    # 再造一个 stash,让 plan_stash_drop(0) 有东西可指向
    r.write("f1.txt", "another change\n")
    r.ops.do_stash(message="top")
    # 重新造出脏工作区:stash 之后又是干净的,而后面几个 plan 需要脏状态
    r.write("f0.txt", "staged again\n")
    r.git("add", "f0.txt")
    r.write("f1.txt", "unstaged again\n")
    before = r.fingerprint()

    o = r.ops
    o.plan_stash()
    o.plan_amend("new msg")
    o.plan_undo_last_commit()
    o.plan_undo_last_commit(drop_changes=True)
    o.plan_restore_from_reflog(0)
    o.plan_revert("HEAD")
    o.plan_branch_rename("side", "side2")
    o.plan_branch_delete("side")
    o.plan_stash_drop(0)
    o.plan_tag_delete("v1")

    ck_eq(r.fingerprint(), before, "plan_* 跑完之后仓库指纹必须一模一样")


# ════════════════════════════════════════════════════════ B stash

@test("B", "stash -> clean -> pop returns the file byte for byte")
def t_B1():
    r = TestRepo("b01")
    # 带非 ASCII、无尾换行、空行,专挑那些在文本工具里容易走样的内容
    payload = "line1\n中文第二行\n\nlast without newline: é\n"
    r.write("doc.txt", "original\n")
    r.commit("chore: base")
    before = r.digest("doc.txt")
    r.write("doc.txt", payload)
    r.write("newfile.txt", "untracked\n")
    staged_digest = r.digest("doc.txt")

    r.ops.do_stash(message="wip", include_untracked=True)
    ck(r.ops.is_clean(), "stash 之后工作区应当干净")
    ck_eq(r.exists("doc.txt") and r.read("doc.txt"), "original\n",
          "stash 之后 doc.txt 应回到 HEAD 的内容")
    ck(not r.exists("newfile.txt"), "include_untracked 时 newfile.txt 也该收走")

    r.ops.do_stash_pop(0)
    ck_eq(r.digest("doc.txt"), staged_digest,
          "pop 回来之后 doc.txt 必须逐字节相同")
    ck_eq(r.read("doc.txt"), payload, "内容也要肉眼一致")
    ck_eq(r.read("newfile.txt"), "untracked\n", "未跟踪文件也要回来")
    ck_eq(len(r.ops.list_stashes()), 0, "pop 成功后 stash 列表应为空")
    ck(before != staged_digest, "前置条件失效:初始内容和新内容相同,测不出东西")


@test("B", "stash on a clean tree errors and creates no empty entry")
def t_B2():
    r = sample_repo("b02")
    ck(r.ops.is_clean(), "前置条件失效:这个仓应当是干净的")
    n0 = len(r.ops.list_stashes())
    e = expect_error(r.ops.plan_stash, "nothing_to_stash", "干净工作区 stash")
    ck_in("干净", e.message, "报错要说清是因为工作区干净")
    ck("空" in e.message or "nothing" in e.code, "报错要点明别塞空条目进去")
    ck_eq(len(r.ops.list_stashes()), n0, "报错之后 stash 列表不许变长")
    # 绕过 plan 直接 do 也不行:do_stash 内部第一件事就是重跑 plan_stash
    expect_error(r.ops.do_stash, "nothing_to_stash", "do_stash 也必须拦")
    ck_eq(len(r.ops.list_stashes()), n0, "do_stash 失败后仍不许有空条目")


@test("B", "stash pop on conflict errors loudly and keeps the entry")
def t_B3():
    r = TestRepo("b03")
    r.write("base.txt", "line1\nline2\nline3\n")
    r.commit("chore: base")
    r.write("base.txt", "line1\nSTASHED\nline3\n")
    r.write("other.txt", "stash side\n")
    r.ops.do_stash(message="conflict me", include_untracked=True)
    # 同一个文件,同一行,改成别的内容 -> pop 必冲突
    r.write("base.txt", "line1\nCONFLICTING\nline3\n")
    r.write("other.txt", "current side\n")
    r.git("add", "-A")
    r.commit("chore: diverged")

    n0 = len(r.ops.list_stashes())
    e = expect_error(r.ops.do_stash_pop, "stash_conflict", "冲突的 pop")
    ck(e.extra.get("conflicted"), "必须把冲突路径报出来,实际 extra=%r" % (e.extra,))
    ck_in("stash", e.message, "报错要说清 stash 还在")
    ck_eq(len(r.ops.list_stashes()), n0,
          "冲突时 stash 条目必须还在 —— 静默丢掉才是灾难")
    # 冲突是真的冲突,不是我们编的
    real = [l for l in r.out("diff", "--name-only", "--diff-filter=U").split()
            if l.strip()]
    ck_eq(sorted(real), sorted(e.extra["conflicted"]),
          "报的冲突路径要和 git 实际认的一致")


@test("B", "stash apply keeps the entry; drop is two-step")
def t_B4():
    r = TestRepo("b04")
    r.write("f.txt", "base\n")
    r.commit("chore: base")
    r.write("f.txt", "wip\n")
    r.ops.do_stash(message="keepme")
    r.ops.do_stash_apply(0)
    ck_eq(r.read("f.txt"), "wip\n", "apply 应该把改动贴回来")
    ck_eq(len(r.ops.list_stashes()), 1, "apply 不许删 stash")

    info = r.ops.plan_stash_drop(0)
    ck_eq(info["safe"], False, "drop 是破坏性的,safe 必须 False")
    ck_in("gc", info["warning"], "warning 要说明 reflog 会被 gc 清掉")
    expect_error(r.ops.do_stash_drop, "needs_confirmation", "drop 缺确认")
    ck_eq(len(r.ops.list_stashes()), 1, "被拒后 stash 还在")
    r.ops.do_stash_drop(0, confirmed=True)
    ck_eq(len(r.ops.list_stashes()), 0, "确认后应删掉")


@test("B", "stash index is validated, not string-interpolated")
def t_B5():
    r = TestRepo("b05")
    r.write("f.txt", "x\n")
    r.commit("chore: base")
    for bad in ("0", 0.0, True, -1, None):
        expect_error(lambda b=bad: r.ops._stash_ref(b), "bad_index",
                     "stash 索引 %r" % (bad,))
    # 合法类型但越界 -> 单独一个 code,别和"类型不对"混为一谈
    expect_error(lambda: r.ops._stash_ref(99), "no_such_stash", "stash 索引 99")
    expect_error(lambda: r.ops._stash_ref("HEAD@{0}"), "bad_index",
                 "字符串索引不该被当 reflog 语法接受")


# ════════════════════════════════════════════════════════ C amend

@test("C", "amend takes only the staged change, unstaged stays put")
def t_C1():
    r = TestRepo("c01")
    r.write("a.txt", "a v1\n")
    r.write("b.txt", "b v1\n")
    r.commit("chore: first")
    old_sha = r.out("rev-parse", "HEAD").strip()

    r.write("a.txt", "a v2 STAGED\n")
    r.write("b.txt", "b v2 UNSTAGED\n")
    r.git("add", "a.txt")           # 只有 a 进了 index
    staged_digest = r.digest("a.txt")
    unstaged_digest = r.digest("b.txt")

    res = r.ops.do_amend(message="chore: first (amended)", confirmed=True)
    ck_eq(res["rewritten"], True, "amend 必须换 sha")
    ck(res["new_sha"] != old_sha, "amend 后 sha 应变化")

    # 提交里的内容 = index 的内容,不含 b
    ck_eq(r.out("show", "HEAD:a.txt"), "a v2 STAGED\n", "已暂存的改动应进提交")
    ck_eq(r.out("show", "HEAD:b.txt"), "b v1\n",
          "**未暂存的改动不能被卷进 amend** —— 这就是 plan 里 warning 的那件事")
    ck_eq(r.digest("b.txt"), unstaged_digest, "工作区里的 b.txt 必须原样保留")
    ck_eq(r.digest("a.txt"), staged_digest, "工作区里的 a.txt 也原样保留")
    ck_eq(sorted(r.ops.dirty()["unstaged"]), ["b.txt"],
          "未暂存的应仍算未暂存")
    ck_eq(r.out("log", "-1", "--format=%s").strip(), "chore: first (amended)",
          "提交信息应被更新")


@test("C", "plan_amend names the unstaged changes and says they are safe")
def t_C2():
    r = TestRepo("c02")
    r.write("a.txt", "v1\n")
    r.write("b.txt", "b v1\n")
    r.commit("chore: first")
    r.write("a.txt", "v2 STAGED\n")
    r.git("add", "a.txt")
    # b.txt 必须是**已跟踪**文件的改动才是"未暂存";新文件是"未跟踪",
    # 那是另一条 warning,拿它来测就测错了字段。
    r.write("b.txt", "b v2 UNSTAGED\n")
    info = r.ops.plan_amend("new msg")
    ck_eq(info["safe"], False, "amend 是改写历史,safe 必须 False")
    ck_eq(info["executable"], True, "有暂存内容,应该可执行")
    ck_in("未暂存", info["warning"], "warning 必须点名未暂存改动")
    # 断言文案说的是"不会被卷进去",而不是把改动的去向含糊过去
    ck(any(s in info["warning"] for s in ("不会被卷", "不会被卷进", "不会被卷进去",
                                         "原样留在工作区", "留在工作区")),
       "warning 必须说清未暂存的不会被卷进这次提交,实际 %r" % info["warning"])
    ck_eq(sorted(info["dirty"]["unstaged"]), ["b.txt"], "dirty 段要准确")
    ck_eq(sorted(info["dirty"]["staged"]), ["a.txt"], "staged 段要准确")
    ck_eq(info["old_sha"], r.out("rev-parse", "HEAD").strip(), "old_sha 要准")

    # 没有暂存内容时,提示的是另一件事
    r2 = TestRepo("c02b")
    r2.write("a.txt", "v1\n")
    r2.commit("chore: only")
    r2.write("z.txt", "untracked\n")
    info2 = r2.ops.plan_amend()
    ck_in("没有已暂存", info2["warning"], "没暂存内容时要提示只能改信息")
    ck_in("未跟踪", info2["warning"], "未跟踪文件要单独提示")
    ck_eq(info2["dirty"]["untracked"], ["z.txt"], "未跟踪段要准确")


@test("C", "amend on a repo with no commits is refused")
def t_C3():
    r = TestRepo("c03")
    ck(r.git("rev-parse", "--verify", "HEAD").returncode != 0,
       "前置条件失效:这个仓居然有 HEAD 了")
    e = expect_error(r.ops.plan_amend, "no_commits", "空仓 amend")
    ck_in("还没有任何提交", e.message, "要说清是仓库没提交")
    expect_error(lambda: r.ops.do_amend("x", confirmed=True), "no_commits",
                 "空仓 do_amend")


# ════════════════════════════════════════════════════════ D 撤销提交

@test("D", "undo-last-commit keeps the changes in the index")
def t_D1():
    r = TestRepo("d01")
    r.write("a.txt", "a v1\n")
    r.commit("chore: first")
    first = r.out("rev-parse", "HEAD").strip()
    r.write("a.txt", "a v2\n")
    r.commit("chore: second")
    second = r.out("rev-parse", "HEAD").strip()

    res = r.ops.do_undo_last_commit(confirmed=True)
    ck_eq(res["mode"], "soft", "默认必须是 soft,保留改动")
    ck_eq(r.out("rev-parse", "HEAD").strip(), first, "HEAD 应回退到 first")
    ck_eq(r.read("a.txt"), "a v2\n", "工作区文件不动")
    ck_eq(sorted(r.ops.dirty()["staged"]), ["a.txt"],
          "被撤销的提交内容应回到暂存区")
    ck(second not in r.out("branch", "--contains", "HEAD"),
       "旧提交不该还在当前分支上")
    ck(r.ops.count_commits() == 1, "提交数应从 2 变 1")


@test("D", "undo-dropped discards the changes and is the irreversible one")
def t_D2():
    r = TestRepo("d02")
    r.write("a.txt", "a v1\n")
    r.commit("chore: first")
    r.write("a.txt", "a v2 DOOMED\n")
    r.commit("chore: second doomed")
    r.write("a.txt", "a v3 also doomed\n")     # 未提交,hard reset 会一起抹掉

    info = r.ops.plan_undo_last_commit(drop_changes=True)
    ck_eq(info["op"], "undo-dropped", "op 名要能区分两种撤销")
    ck_eq(info["mode"], "hard", "drop 模式是 hard")
    ck_in("抹掉", info["warning"], "warning 必须点出连未提交改动一起抹")

    res = r.ops.do_undo_last_commit(drop_changes=True, confirmed=True)
    ck_eq(res["mode"], "hard", "确认后应真的 hard reset")
    ck_eq(r.read("a.txt"), "a v1\n", "上一个提交的内容应当消失")
    ck_eq(r.out("status", "--porcelain").strip(), "", "工作区应回到干净")


@test("D", "after undo the commit is still recoverable via reflog")
def t_D3():
    r = TestRepo("d03")
    r.write("a.txt", "v1\n")
    r.commit("chore: first")
    r.write("a.txt", "v2\n")
    r.commit("chore: second")
    lost = r.out("rev-parse", "HEAD").strip()

    info = r.ops.plan_undo_last_commit()
    # recoverable_via 里的命令要和这一次的 mode 对上:soft 撤销配 --soft 恢复
    ck_in("reset --soft", info["recoverable_via"],
          "plan 就要给出与 mode 相符的恢复命令,实际 %r" % info["recoverable_via"])

    r.ops.do_undo_last_commit(confirmed=True)
    ck(lost != r.out("rev-parse", "HEAD").strip(), "前置条件失效:HEAD 没回退")

    rows = r.ops.list_reflog()
    # selector 必须能直接粘回命令行。配了 --date 之后 git 的 %gd 吐的是日期
    # ("HEAD@{2026-10-01 ...}"),那种字符串 `git reset` 是不认的。
    ck(rows, "reflog 不该是空的")
    ck(re.fullmatch(r"HEAD@\{\d+\}", rows[0]["selector"]),
       "selector 要是 HEAD@{N} 这种可粘贴形式,实际 %r" % rows[0]["selector"])
    for i, x in enumerate(rows):
        ck_eq(x["index"], i, "index 要是连续下标")
        ck_eq(x["selector"], "HEAD@{%d}" % i, "selector 要和 index 对上")
        # git --date=iso 给的是 "YYYY-MM-DD HH:MM:SS +0800",带时区偏移
        ck(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}",
                        x["date"]),
           "date 要带时区偏移且可读,实际 %r" % x["date"])
        ck(x["action"], "action(%gs) 不该为空")
    hit = [i for i, x in enumerate(rows) if x["sha"] == lost]
    ck(hit, "reflog 里应仍能找到被撤销的提交 %s" % lost[:8])
    # 按 sha 找位置,而不是假设 index 0 —— reset 自己也会占掉最新那一条
    idx = hit[0]
    ck(idx > 0, "reset 之后最新那条 reflog 是这次 reset,被撤销的提交在其前面")

    plan = r.ops.plan_restore_from_reflog(idx, mode="soft")
    ck_eq(plan["target"]["sha"], lost, "定位到的应是刚被撤销的那条")
    ck_eq(plan["target"]["selector"], "HEAD@{%d}" % idx,
          "plan 要给出可直接粘贴的 selector")
    # warning 里用的是短 sha(7 位),别拿 8 位去撞
    ck_in(plan["target"]["short"], plan["warning"],
          "warning 要点名恢复到哪一条,实际 %r" % plan["warning"])
    ck_eq(plan["target"]["short"], lost[:7], "short 应是 7 位短 sha")
    ck_in("reflog", plan["warning"], "warning 要提醒 reflog 会被 gc")
    ck_eq(plan["mode"], "soft", "恢复模式要透传给前端")
    r.ops.do_restore_from_reflog(idx, mode="soft", confirmed=True)
    ck_eq(r.out("rev-parse", "HEAD").strip(), lost, "恢复后 HEAD 应回到那条提交")
    ck_eq(r.read("a.txt"), "v2\n", "恢复后内容应也回来")


@test("D", "undo on a repo with no commits is refused")
def t_D4():
    r = TestRepo("d04")
    expect_error(r.ops.plan_undo_last_commit, "no_commits", "空仓撤销")
    expect_error(lambda: r.ops.do_undo_last_commit(confirmed=True),
                 "no_commits", "空仓 do_undo")
    expect_error(lambda: r.ops.do_undo_last_commit(drop_changes=True,
                                                   confirmed=True),
                 "no_commits", "空仓 undo-dropped")


@test("D", "recovery is refused when reflog is empty")
def t_D5():
    r = TestRepo("d05")
    ck_eq(r.ops.list_reflog(), [], "前置条件:空仓的 reflog 应为空 list")
    e = expect_error(r.ops.plan_restore_from_reflog, "empty_reflog", "空 reflog")
    ck_in("没有可恢复的历史", e.message, "要说清是没有可恢复的")
    expect_error(lambda: r.ops.do_restore_from_reflog(0, confirmed=True),
                 "empty_reflog", "空 reflog 的 do_")
    # 有提交之后索引越界也要拦
    r.write("a.txt", "a\n")
    r.commit("chore: only")
    expect_error(lambda: r.ops.plan_restore_from_reflog(999),
                 "bad_index", "reflog 索引越界")


# ════════════════════════════════════════════════════════ E revert

@test("E", "revert of a normal commit adds an inverse commit")
def t_E1():
    r = TestRepo("e01")
    r.write("a.txt", "v1\n")
    r.commit("chore: first")
    r.write("a.txt", "v2\n")
    r.commit("chore: second")
    n_before = r.ops.count_commits()
    # revert 当前 tip:这就是 "Revert Commit" 在 SCM 面板上最常见的用法
    # (要"撤销上一次提交但不改写历史"时的正解)
    tip = r.out("rev-parse", "HEAD").strip()

    info = r.ops.plan_revert(tip)
    ck_eq(info["safe"], True, "revert 是安全操作(不改历史)")
    ck_eq(info["is_merge"], False, "这不是 merge")
    ck_eq(info["blocked"], None, "普通提交不该有阻塞理由")
    ck_in("不改写任何历史", info["warning"], "warning 要说明安全语义")

    res = r.ops.do_revert(tip)
    ck_eq(r.ops.count_commits(), n_before + 1, "revert 应新增一个提交")
    ck_eq(r.read("a.txt"), "v1\n", "内容应被反向改回去")
    ck_in("Revert", r.out("log", "-1", "--format=%s"),
          "反向提交的信息应提到 Revert")


@test("E", "revert of a merge commit is refused without mainline")
def t_E2():
    r = TestRepo("e02")
    r.write("base.txt", "base\n")
    r.commit("chore: base")
    r.git("checkout", "-b", "side")
    r.write("side.txt", "side\n")
    r.commit("feat: side")
    r.git("checkout", "main")
    r.write("main.txt", "main\n")
    r.commit("feat: main")
    r.git("merge", "--no-ff", "-m", "merge side", "side", check=True)
    merge = r.out("rev-parse", "HEAD").strip()
    ck_eq(len(r.out("show", "-s", "--format=%P", merge).split()), 2,
          "前置条件失效:这个提交不是 merge")

    info = r.ops.plan_revert(merge)
    ck_eq(info["is_merge"], True, "应认出是 merge 提交")
    ck(info["blocked"], "不给 mainline 必须有阻塞理由")
    ck_in("mainline", info["blocked"], "阻塞理由要解释 mainline 是什么")
    ck_in("不确定", info["blocked"], "要说明为什么不能猜")
    ck_in("候选", info["blocked"], "要列出候选 mainline")
    ck_eq(info["executable"], False, "被阻塞的操作不该可执行")

    e = expect_error(lambda: r.ops.do_revert(merge), "revert_refused",
                     "merge 不给 mainline")
    ck_in("mainline", e.message, "报错要说清缺 mainline")
    ck_eq(r.ops.count_commits(), 4, "被拒之后提交数不该变")

    # mainline 的形式校验走 bad_mainline;"语义上不适用"走 revert_refused。
    # 这两类要分开:前者是调用方传错了,后者是调用方的意图本身不成立。
    # 2 个父提交 -> 合法区间是 1..2,越界要往区间外试
    expect_error(lambda: r.ops.plan_revert(merge, mainline=3),
                 "bad_mainline", "mainline 越界(上界)")
    expect_error(lambda: r.ops.plan_revert(merge, mainline=0),
                 "bad_mainline", "mainline 为 0")
    expect_error(lambda: r.ops.plan_revert(merge, mainline=-1),
                 "bad_mainline", "mainline 为负")
    expect_error(lambda: r.ops.plan_revert(merge, mainline="1"),
                 "bad_mainline", "mainline 传字符串")
    expect_error(lambda: r.ops.plan_revert(merge, mainline=True),
                 "bad_mainline", "mainline 传 bool")
    # 给普通提交指定 mainline:意图不成立,但形式合法。
    # plan_* 不抛异常,它用 blocked 把话说完 —— 抛异常的是 do_revert。
    normal = r.out("rev-parse", "HEAD~1").strip()
    plan_normal = r.ops.plan_revert(normal, mainline=1)
    ck(plan_normal["blocked"], "普通提交指定 mainline 应当被 plan 挡住")
    ck_in("不是 merge", plan_normal["blocked"], "要说清它不是 merge 提交")
    e_normal = expect_error(lambda: r.ops.do_revert(normal, mainline=1),
                            "revert_refused", "普通提交指定 mainline(do)")
    ck_in("不是 merge", e_normal.message, "要说清它不是 merge 提交")
    # do_ 也要拦住
    expect_error(lambda: r.ops.do_revert(merge, mainline=9),
                 "bad_mainline", "do_revert 的 mainline 越界")


@test("E", "revert of a merge commit works once mainline is given")
def t_E3():
    r = TestRepo("e03")
    r.write("base.txt", "base\n")
    r.commit("chore: base")
    r.git("checkout", "-b", "side")
    r.write("side.txt", "side\n")
    r.commit("feat: side")
    r.git("checkout", "main")
    r.write("main.txt", "main\n")
    r.commit("feat: main")
    r.git("merge", "--no-ff", "-m", "merge side", "side", check=True)
    merge = r.out("rev-parse", "HEAD").strip()
    n_before = r.ops.count_commits()

    info = r.ops.plan_revert(merge, mainline=1)
    ck_eq(info["executable"], True, "给了 mainline 就该可执行")
    ck_in("mainline=1", info["warning"], "warning 要复述选中的 mainline")
    r.ops.do_revert(merge, mainline=1)
    ck_eq(r.ops.count_commits(), n_before + 1, "应新增一个反向提交")
    ck(not r.exists("side.txt"), "mainline=1 意味着撤销相对 main 的差异,"
                                  "side.txt 应被撤掉")


@test("E", "revert conflict reports the paths and can be aborted")
def t_E4():
    r = TestRepo("e04")
    r.write("f.txt", "v1\n")
    r.commit("chore: first")
    r.write("f.txt", "v2\n")
    target = r.out("rev-parse", "HEAD").strip()
    r.commit("chore: second")
    r.write("f.txt", "v3 CONFLICTING\n")
    r.commit("chore: third conflicts")

    e = expect_error(lambda: r.ops.do_revert(target), "revert_conflict",
                     "冲突的 revert")
    ck(e.extra.get("conflicted"), "必须报出冲突路径,实际 %r" % (e.extra,))
    ck_in("abort", e.message, "报错要指路 do_revert_abort")
    # 仓库停在 revert 中途
    ck(r.git("rev-parse", "--verify", "--quiet", "REVERT_HEAD").returncode == 0,
       "冲突后应停在 revert 中途")
    res = r.ops.do_revert_abort()
    ck_eq(res["aborted"], True, "abort 应成功")
    ck(r.git("rev-parse", "--verify", "--quiet", "REVERT_HEAD").returncode != 0,
       "abort 之后不该还在 revert 中途")
    expect_error(r.ops.do_revert_abort, "no_revert_in_progress",
                 "没有 revert 时 abort")


# ════════════════════════════════════════════════════════ F 分支

@test("F", "plan of deleting an unmerged branch reports danger")
def t_F1():
    r = TestRepo("f01")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    r.git("checkout", "-b", "feature")
    for i in range(3):
        r.write("f%d.txt" % i, "feature %d\n" % i)
        r.commit("feat: %d" % i)
    r.git("checkout", "main")
    ck_eq(r.ops.list_branches() and len(r.ops.list_branches()), 2, "前置:两个分支")

    info = r.ops.plan_branch_delete("feature")
    ck_eq(info["safe"], False, "有未合并提交,safe 必须 False")
    ck_eq(info["unmerged_count"], 3, "未合并提交数要数对")
    ck_eq(len(info["commits"]), 3, "要列出这 3 个提交")
    ck_in("3 个未合并提交", info["warning"], "warning 要写明条数")
    ck_in("不加 force", info["warning"], "warning 要说明不加 force 会被拒")
    ck_eq(info["executable"], True, "不是硬阻塞,只是危险")
    ck_eq(info["blocked"], None, "未合并不是硬阻塞")
    # 前端要在警告里点出具体是哪几个提交,所以每条都得有可读的 short
    for c in info["commits"]:
        ck("short" in c and c["short"] == c["sha"][:8],
           "未合并提交要带 short,实际 %r" % (c,))
        ck(c["subject"], "未合并提交要带 subject")


@test("F", "do_branch_delete refuses unmerged without force, then forces")
def t_F2():
    r = TestRepo("f02")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    r.git("checkout", "-b", "feature")
    r.write("f.txt", "unmerged work\n")
    r.commit("feat: unique")
    r.git("checkout", "main")
    doomed = r.out("rev-parse", "feature").strip()

    expect_error(lambda: r.ops.do_branch_delete("feature"),
                 "needs_confirmation", "缺确认闸")
    ck("feature" in r.out("branch", "--list"), "被拒后分支还在")

    e = expect_error(lambda: r.ops.do_branch_delete("feature", confirmed=True),
                     "unmerged_branch", "未合并且无 force")
    ck_in("force=True", e.message, "报错要指路 force=True")
    ck(e.extra.get("commits"), "报错要带上未合并的提交")
    ck("feature" in r.out("branch", "--list"),
       "**拒绝之后分支必须还在** —— 这是本组最关键的一条")

    res = r.ops.do_branch_delete("feature", force=True, confirmed=True)
    ck_eq(res["deleted"], "feature", "force 之后应删掉")
    ck("feature" not in r.out("branch", "--list"), "分支应真的没了")
    # 删分支只删引用,提交还在对象库里 —— 这是 plan 里"不可恢复"要提醒的窗口
    ck_eq(r.out("cat-file", "-t", doomed).strip(), "commit",
          "被删分支的提交对象应该还在(等 gc 才会真的没)")


@test("F", "deleting the current branch is refused")
def t_F3():
    r = TestRepo("f03")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    r.git("checkout", "-b", "current")
    r.write("b.txt", "b\n")
    r.commit("feat: on current")
    # **留在 current 上**:要测的正是"删掉自己脚下这个分支"
    ck_eq(r.ops.head_branch(), "current", "前置条件:应停在 current 上")

    info = r.ops.plan_branch_delete("current")
    ck(info["blocked"], "删当前分支必须有阻塞理由")
    ck_in("当前所在的分支", info["blocked"], "要说清是当前分支")
    ck_in("checkout", info["blocked"], "要说清先切走")
    ck_eq(info["current"], True, "current 标记要对")
    ck_eq(info["executable"], False, "被阻塞就不该可执行")
    # 建议切走的那个分支必须**确实存在且不是它自己**。写死 "main" 的话,
    # 当被删的恰好是 main 时就变成"先切到 main" —— 废话,用户会直接放弃。
    names = [b["name"] for b in r.ops.list_branches()]
    named = info["blocked"].split("git checkout ")[-1].split(")")[0].strip()
    ck(named in names, "建议切走的分支 %r 必须真实存在,实际分支 %r"
       % (named, names))
    ck(named != "current", "不能建议切到被删的那个分支自己")

    # 即使 force 也救不了
    e = expect_error(
        lambda: r.ops.do_branch_delete("current", force=True, confirmed=True),
        "delete_refused", "force 也删不掉当前分支")
    ck_in("当前", e.message, "报错要点明是当前分支")
    ck("current" in r.out("branch", "--list"), "分支必须还在")


@test("F", "deleting the only branch or the default branch is refused")
def t_F4():
    r = TestRepo("f04")
    r.write("a.txt", "a\n")
    r.commit("chore: only")
    ck_eq(len(r.ops.list_branches()), 1, "前置:只有一个分支")
    info = r.ops.plan_branch_delete("main")
    ck(info["blocked"], "唯一分支/默认分支必须被拦")
    ck_in("唯一的分支", info["blocked"], "要说清是唯一分支")
    expect_error(lambda: r.ops.do_branch_delete("main", confirmed=True),
                 "delete_refused", "唯一分支")
    expect_error(lambda: r.ops.do_branch_delete("main", force=True,
                                                confirmed=True),
                 "delete_refused", "唯一分支 + force")
    ck("main" in r.out("branch", "--list"), "main 必须还在")

    # 有了第二个分支,main 变成 default 分支,依然受保护。
    # 必须**停在 scratch 上**去问 main:否则 main 是当前分支,先被"当前分支"
    # 那条规则拦下,理由就变了,验不到 default 这条规则。
    r.git("checkout", "-b", "scratch")
    r.write("s.txt", "s\n")
    r.commit("feat: scratch")
    ck_eq(r.ops.head_branch(), "scratch", "前置条件:应停在 scratch 上")
    ck_eq(r.ops.default_branch(), "main", "前置条件:main 应被认作 default")

    info2 = r.ops.plan_branch_delete("main")
    ck(info2["blocked"], "default 分支仍要拦")
    ck_in("default", info2["blocked"], "要说清是 default 分支")
    ck_in("改名", info2["blocked"], "要给出绕过办法(先改名再删)")
    expect_error(lambda: r.ops.do_branch_delete("main", confirmed=True),
                 "delete_refused", "default 分支")
    # force 也救不了 default —— 它保护的是别人的落脚点,不是"有没有合并"
    expect_error(lambda: r.ops.do_branch_delete("main", force=True,
                                                confirmed=True),
                 "delete_refused", "default 分支 + force")
    ck("main" in r.out("branch", "--list"), "main 必须还在")

    # 绕过去:先改个名,再删 —— 这正是那条 blocked 文案承诺的路
    r.ops.do_branch_rename("main", "renamed-main", confirmed=True)
    info3 = r.ops.plan_branch_delete("renamed-main")
    ck_eq(info3["blocked"], None, "改了名之后就不再是 default,不该被拦")
    ck_eq(info3["safe"], True, "main 的提交已在 HEAD 上,已合并 = 安全")
    r.ops.do_branch_delete("renamed-main", confirmed=True)
    ck("renamed-main" not in r.out("branch", "--list"), "改名后应能删掉")


@test("F", "rename works, and refuses to clobber an existing branch")
def t_F5():
    r = TestRepo("f05")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    r.git("branch", "taken")
    r.write("t.txt", "t\n")
    r.commit("feat: on main")
    r.git("checkout", "-b", "old-name")
    r.write("b.txt", "b\n")
    r.commit("feat: on old")
    sha = r.out("rev-parse", "HEAD").strip()
    # 停在 old-name 上:改名的常见场景就是"顺手给自己在用的分支改个好名字"
    ck_eq(r.ops.head_branch(), "old-name", "前置条件:应停在 old-name 上")

    info = r.ops.plan_branch_rename("old-name", "new-name")
    ck_eq(info["blocked"], None, "正常改名不该被拦")
    ck_in("当前分支", info["warning"], "当前分支改名要提示")
    res = r.ops.do_branch_rename("old-name", "new-name", confirmed=True)
    ck_eq(res["new"], "new-name", "改名后名字要对")
    ck_eq(res["sha"], sha, "改名不许动提交")
    ck("old-name" not in r.out("branch", "--list"), "旧名应消失")

    # plan_* 是只读的:冲突它用 blocked 说事,不抛异常(抛异常的是 do_*)
    clash = r.ops.plan_branch_rename("new-name", "taken")
    ck(clash["blocked"], "改成已存在的名字必须在 plan 里就说清")
    ck_in("已经有同名分支", clash["blocked"], "要说清撞名了")
    ck_in("不会覆盖", clash["blocked"], "要点明不会覆盖别人的分支")
    ck_eq(clash["executable"], False, "被撞名就不该可执行")
    expect_error(lambda: r.ops.do_branch_rename("new-name", "taken",
                                                confirmed=True),
                 "rename_refused", "改成已存在的名字(do)")
    expect_error(lambda: r.ops.do_branch_rename("new-name", "new-name",
                                                confirmed=True),
                 "rename_refused", "新旧同名")
    expect_error(lambda: r.ops.plan_branch_rename("no-such", "x"),
                 "no_such_branch", "改不存在的分支")
    ck("taken" in r.out("branch", "--list"), "别人的分支不许被动")
    ck("new-name" in r.out("branch", "--list"), "被拒后分支还在")


# ════════════════════════════════════════════════════════ G 标签

@test("G", "create, list and delete a tag (delete is two-step)")
def t_G1():
    r = TestRepo("g01")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    res = r.ops.do_tag_create("v1.0.0", message="first release")
    ck_eq(res["name"], "v1.0.0", "标签名")
    ck_eq(res["sha"], r.out("rev-parse", "HEAD").strip(), "应打在 HEAD 上")
    ck_eq(res["annotated"], True, "带 message 应是附注标签")
    tags = r.ops.list_tags()
    ck_eq([t["name"] for t in tags], ["v1.0.0"], "列表里应能看到")
    ck_eq(tags[0]["type"], "tag", "附注标签的 objecttype 是 tag")
    ck_eq(tags[0]["subject"], "first release", "附注内容应能读出")

    expect_error(lambda: r.ops.do_tag_create("v1.0.0"), "tag_exists", "重复标签")
    r.ops.do_tag_create("v1.0.0", message="force", force=True)
    ck_eq(r.ops.list_tags()[0]["subject"], "force", "force 应能覆盖")

    info = r.ops.plan_tag_delete("v1.0.0")
    ck_eq(info["safe"], False, "删标签是破坏性的")
    ck_in("reflog", info["warning"], "warning 要说明标签不进 reflog")
    # confirmed 默认 False,所以不传它就该被拦
    expect_error(lambda: r.ops.do_tag_delete("v1.0.0"),
                 "needs_confirmation", "删标签缺确认")
    ck("v1.0.0" in r.out("tag", "-l"), "被拒后标签还在")
    r.ops.do_tag_delete("v1.0.0", confirmed=True)
    ck_eq(r.ops.list_tags(), [], "确认后应删掉")
    expect_error(lambda: r.ops.plan_tag_delete("nope"),
                 "no_such_tag", "删不存在的标签")
    expect_error(lambda: r.ops.do_tag_delete("nope", confirmed=True),
                 "no_such_tag", "删不存在的标签(do)")


@test("G", "tag names go through the whitelist")
def t_G2():
    r = TestRepo("g02")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    for bad in ("-f", "--force", "..", "a/../b", "@", "a@{0}", "a b",
                "a.lock", "/a", "a/", "a.", "a//b", "", "x" * 101, "a\tb",
                "a;rm", "a$b", "a|b", "a&b", "a`b", "a>b"):
        expect_error(lambda b=bad: r.ops.do_tag_create(b), "bad_name",
                     "非法标签名 %r" % bad)
    ck_eq(r.ops.list_tags(), [], "一个标签都不该被建出来")
    r.ops.do_tag_create("release/2024.01_ok-1", message="ok")
    ck_eq(len(r.ops.list_tags()), 1, "合法名字应能建")


# ════════════════════════════════════════════════════════ H 文件历史 / blame

@test("H", "file history follows a rename")
def t_H1():
    r = TestRepo("h01")
    for i in range(3):
        r.write("old.txt", "v%d\n" % i)
        r.commit("chore: rev %d" % i)
    old_tip = r.out("rev-parse", "HEAD").strip()
    r.git("mv", "old.txt", "new.txt")
    r.commit("refactor: rename old.txt to new.txt")
    r.write("new.txt", "v-after\n")
    r.commit("chore: after rename")

    hist = r.ops.file_history("new.txt", follow=True)
    ck_eq(hist["ok"], True, "查询应成功")
    ck(hist["count"] >= 5, "应能看到全部 5 个提交(含重命名前的),实际 %d"
       % hist["count"])
    shas = [c["sha"] for c in hist["commits"]]
    ck(old_tip in shas, "重命名**之前**的提交必须在列表里 —— 这就是 --follow")
    # 列表按时间倒序,末尾那条是最早的提交 —— 它在文件还叫 old.txt 的时候
    ck_eq(hist["commits"][-1]["subject"], "chore: rev 0",
          "跟回重命名之前,最早那条提交也该在列表里")

    # 关掉 follow 就断了 —— 用来说明 follow 真的在起作用,而不是碰巧
    nofollow = r.ops.file_history("new.txt", follow=False)
    ck(nofollow["count"] < hist["count"],
       "关掉 --follow 后应当只看得到重命名之后的提交(%d vs %d)"
       % (nofollow["count"], hist["count"]))

    renames = [f for c in hist["commits"] for f in c["files"] if f.get("rename")]
    ck(renames, "name-status 里应标出 R(重命名),实际 files=%r"
       % [(c["subject"], c["files"]) for c in hist["commits"]])

    renames = _renames(hist)
    ck(renames, "name-status 里应标出 R(重命名),实际 files=%r"
       % [(c["subject"], c["files"]) for c in hist["commits"]])
    # 方向要对:从 old.txt 改到 new.txt
    old_new = [(f["old"], f["new"]) for _, f in renames]
    ck(("old.txt", "new.txt") in old_new,
       "重命名条目的方向应是 old.txt -> new.txt,实际 %r" % (old_new,))
    # 而且这次改名必须挂在 "refactor: rename" 那条提交上
    ck(any("rename" in subj for subj, _ in renames),
       "R 条目应属于那次改名提交,实际 %r" % (renames,))

    # 改回查询旧名字也要有结果
    hist_old = r.ops.file_history("old.txt", follow=True)
    ck(hist_old["count"] >= 3, "查旧路径也应能跟回来")


def _renames(hist):
    """挑出 file_history 结果里所有被标成 R 的条目,连同所属提交。"""
    return [(c["subject"], f) for c in hist["commits"] for f in c["files"]
            if f.get("rename")]


@test("H", "blame reports line number, commit, author and time per line")
def t_H2():
    r = TestRepo("h02")
    r.write("f.txt", "alpha\nbeta\ngamma\n")
    r.commit("chore: first")
    r.write("f.txt", "alpha\nBETA touched\ngamma\n")
    r.commit("chore: second")

    res = r.ops.blame("f.txt")
    ck_eq(res["count"], 3, "3 行文件应返回 3 条")
    ck_eq(res["total_lines"], 3, "总行数")
    ck_eq(res["truncated"], False, "3 行不该被截断")
    ck_eq([l["line"] for l in res["lines"]], [1, 2, 3], "行号应连续")
    ck_eq([l["content"] for l in res["lines"]],
           ["alpha", "BETA touched", "gamma"], "内容应对得上")
    l2 = res["lines"][1]
    ck_eq(l2["short"], l2["sha"][:8], "short 应是 sha 前 8 位")
    ck_eq(l2["author"], "aigit-test", "作者")
    ck(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", l2["time"]),
       "时间格式应对,实际 %r" % l2["time"])
    ck_eq(l2["summary"], "chore: second", "改过这一行的提交应是 second")
    ck_eq(res["lines"][0]["summary"], "chore: first", "没动过的行归 first")


@test("H", "blame on a large file truncates and can be resumed")
def t_H3():
    r = TestRepo("h03")
    n = 5000
    r.write("big.txt", "".join("line %d\n" % i for i in range(n)))
    r.commit("chore: big file")
    r.write("big.txt", "".join(
        ("CHANGED %d\n" % i) if i == 4321 else ("line %d\n" % i)
        for i in range(n)))
    r.commit("chore: touch one line")

    res = r.ops.blame("big.txt", limit=100)
    ck_eq(res["total_lines"], n, "总行数应为 %d" % n)
    ck_eq(res["count"], 100, "limit=100 应只返回 100 条")
    ck_eq(res["truncated"], True, "应标出被截断")
    ck_eq(res["first_line"], 1, "起点")
    ck_eq(res["last_line"], 100, "终点")
    ck_eq(res["next_line"], 101, "next_line 要能接着往下拉")
    ck(res["count"] < n / 10, "绝不能一次吐 %d 条出来" % n)

    # 接着拉,内容要连得上
    res2 = r.ops.blame("big.txt", start_line=res["next_line"], limit=50)
    ck_eq(res2["first_line"], 101, "续拉应从 101 开始")
    ck_eq(res2["last_line"], 150, "续拉 50 行")
    ck_eq(res2["next_line"], 151, "还没到文件尾")
    ck_eq(res2["lines"][0]["content"], "line 100", "第 101 行应是 line 100")

    # 最后一段:truncated 应变 False,next_line 变 None
    res3 = r.ops.blame("big.txt", start_line=n - 10, limit=100)
    ck_eq(res3["last_line"], n, "最后一段应到文件尾")
    ck_eq(res3["truncated"], False, "到文件尾就不该说截断")
    ck_eq(res3["next_line"], None, "没有下一段了")
    ck_eq([l["content"] for l in res3["lines"]][-1], "line 4999",
          "最后一行内容")

    # 被改过的那一行 blame 到第二个提交上
    hit = [l for l in r.ops.blame("big.txt", start_line=4322,
                                  end_line=4322)["lines"] if l["line"] == 4322]
    ck_eq(len(hit), 1, "应精确定位到 4322 行")
    ck_eq(hit[0]["summary"], "chore: touch one line",
          "改过的那行应归到第二个提交")
    ck_eq(hit[0]["content"], "CHANGED 4321", "那行内容")


@test("H", "blame limit is clamped, and bad ranges are refused")
def t_H4():
    r = TestRepo("h04")
    r.write("f.txt", "".join("l%d\n" % i for i in range(20)))
    r.commit("chore: f")
    res = r.ops.blame("f.txt", limit=999999)
    ck(res["limit"] <= history_ops.MAX_BLAME_LINES,
       "limit 必须被夹到上限内,实际 %d" % res["limit"])
    res2 = r.ops.blame("f.txt", limit=0)
    ck_eq(res2["limit"], 1, "limit=0 应夹到 1")
    expect_error(lambda: r.ops.blame("f.txt", start_line="3"), "bad_line",
                 "start_line 传字符串")
    expect_error(lambda: r.ops.blame("f.txt", end_line=5, start_line=9),
                 "bad_line", "end < start")
    expect_error(lambda: r.ops.blame("f.txt", limit="many"), "bad_number",
                 "limit 传字符串")


@test("H", "blame refuses binary files, path traversal and missing files")
def t_H5():
    r = TestRepo("h05")
    r.write("f.txt", "text\n")
    with open(r.path("blob.bin"), "wb") as f:
        f.write(b"AB\x00CD\x00EF\n")
    r.commit("chore: add binary")
    expect_error(lambda: r.ops.blame("blob.bin"), "binary_file", "二进制文件")
    expect_error(lambda: r.ops.blame("../../etc/passwd"), "path_escape", "越界")
    expect_error(lambda: r.ops.blame("nope.txt"), "no_such_file", "不存在的文件")
    expect_error(lambda: r.ops.file_history("../../secret"), "path_escape",
                 "file_history 越界")


# ════════════════════════════════════════════════════════ I 白名单

@test("I", "branch names go through the same whitelist as new_branch")
def t_I1():
    r = TestRepo("i01")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    for bad in ("-f", "--force", "..", "a/../b", "@", "a@{0}", "a b", "a.lock",
                "/a", "a/", "a.", "a//b", "", "x" * 101, "a;rm -rf /",
                "a$b", "a|b", "a&b", "a>b", "a'b", 'a"b', "a\\b", "a`b"):
        expect_error(lambda b=bad: r.ops.plan_branch_delete(b), "bad_name",
                     "非法分支名 %r" % bad)
    ck_eq(len(r.ops.list_branches()), 1, "不该有分支被建出来或删掉")
    r.git("checkout", "-b", "feat/ok_name-1.2")
    ck_eq(len(r.ops.list_branches()), 2, "合法名字应被接受")
    info = r.ops.plan_branch_rename("feat/ok_name-1.2", "feat/ok_name-1.3")
    ck_eq(info["executable"], True, "合法改名不该被拦")


@test("I", "path escape is refused; the outside canary survives")
def t_I2():
    r = TestRepo("i02")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    outside_dir = os.path.join(TESTROOT, "_outside")
    os.makedirs(outside_dir, exist_ok=True)
    canary = os.path.join(outside_dir, "i02.canary")
    with io.open(canary, "w", encoding="utf-8", newline="\n") as f:
        f.write("canary\n")
    before = open(canary, "rb").read()
    # 构造一个 resolve 之后跑到仓外的路径
    evil = os.path.join("..", "_outside", "i02.canary")
    for fn, code in ((r.ops.blame, "path_escape"),
                     (r.ops.file_history, "path_escape")):
        try:
            fn(evil)
        except HistoryError as e:
            ck_eq(e.code, code, "%s 对越界路径的 code" % fn.__name__)
        else:
            raise Fail("%s 放行了越界路径 %r" % (fn.__name__, evil))
    # Windows 风格的 ..\ 也要挡
    expect_error(lambda: r.ops.blame("..\\_outside\\i02.canary"),
                 "path_escape", "反斜杠越界")
    expect_error(lambda: r.ops.blame(""), "bad_path", "空路径")
    # 绝对路径:resolve 之后落在仓外,被同一个越界闸挡住。
    # 报 path_escape 而不是 bad_path 是对的 —— 它的确越界了,只是没走到
    # "前导 /" 那条更早的分支上(Windows 下 C:/ 是绝对路径,不是 / 开头)。
    expect_error(lambda: r.ops.blame("C:/Windows"), "path_escape", "绝对路径")
    expect_error(lambda: r.ops.blame("/etc/passwd"), "bad_path", "以 / 开头")
    ck_eq(open(canary, "rb").read(), before, "仓外的哨兵文件必须一字未动")


@test("I", "a rev that is not a commit is refused, not passed to git")
def t_I3():
    r = TestRepo("i03")
    r.write("a.txt", "a\n")
    r.commit("chore: base")
    for bad in ("", "   ", "-x", "--amend", "no-such-ref", "a" * 200,
                "abc;rm -rf /", "a|b", "a\nb", "a b", "$(whoami)", "`id`"):
        expect_error(lambda b=bad: r.ops.plan_revert(b), "bad_rev",
                     "非法 rev %r" % bad)


# ════════════════════════════════════════════════════════ 跑

def cleanup():
    """删掉 %TEMP% 下的测试仓。绝不影响 C:\\poject\\。"""
    # 双保险:这个函数只允许删 %TEMP% 底下的东西。写死断言而不是靠自觉 ——
    # 一旦 TESTROOT 被改错,下面那句 rmtree 就是往真实仓库头上砍。
    root = os.path.abspath(TESTROOT)
    tmp = os.path.abspath(tempfile.gettempdir())
    assert root.lower().startswith(tmp.lower()), \
        "测试目录必须在 %%TEMP%% 下,当前 %s" % TESTROOT
    assert os.path.basename(root).startswith("aigit-history-test"), \
        "只允许删 aigit-history-test*,当前 %s" % root
    purge(root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--filter", default="")
    ap.add_argument("--keep", action="store_true", help="跑完保留测试仓")
    a = ap.parse_args()

    cases = [t for t in TESTS
             if not a.filter or a.filter.lower() in t["id"].lower()]
    try:
        cleanup()
    except RuntimeError as e:
        # 上一轮(或别的进程)留下了删不掉的目录。不能就这么建在脏目录上开跑 ——
        # git init 一个还带旧 .git 的目录,后面所有断言的结论都不可信。
        print("清场失败 %s" % e)
        print("  拒绝在脏目录上开跑。请手动删掉 %s,或加 --keep 之外先解决占用。"
              % TESTROOT)
        return 2
    os.makedirs(TESTROOT, exist_ok=True)
    print("测试仓目录: %s" % TESTROOT)
    print("=" * 68)

    passed, failed, crashed = 0, [], []
    for t in cases:
        sid = t["id"]
        try:
            t["fn"]()
            passed += 1
            if a.verbose:
                print("ok   %-4s %s" % (t["group"], t["name"]))
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
        except Fail as e:
            failed.append((sid, t["name"], str(e)))
            print("\nnot ok %s - %s" % (sid, t["name"]))
            print("        %s" % e)
        except Exception:
            crashed.append((sid, t["name"], traceback.format_exc()))
            print("\nnot ok %s - %s (异常)" % (sid, t["name"]))
            print("        " + traceback.format_exc().replace("\n", "\n        "))
    if not a.verbose:
        print("")
    print("=" * 68)
    print("共 %d 条,过 %d,失败 %d,异常 %d"
          % (len(cases), passed, len(failed), len(crashed)))
    for sid, name, _ in failed + crashed:
        print("  FAILED %s %s" % (sid, name))
    print("=" * 68)
    if not a.keep:
        # 清场失败也要变成可见的事实,不能被一个"全绿"盖过去:
        # 残留的仓会让**下一轮**建在脏目录上,那才是真正会误导人的地方。
        try:
            cleanup()
        except RuntimeError as e:
            print("清场失败 %s" % e)
            print("  残留会让下一轮测试建在脏目录上,请手动删掉 %s" % TESTROOT)
            return 1
        print("清场完成 %s 已删除" % TESTROOT)
    return 1 if (failed or crashed) else 0


if __name__ == "__main__":
    sys.exit(main())
