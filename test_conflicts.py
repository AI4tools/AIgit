#!/usr/bin/env python3
"""aigit 冲突解决的验收标准 —— 只测 conflicts.py。

覆盖任务书要求的那 8 条,外加若干容易漏的边界(delete/modify、并发覆盖、
带 U 提交、非冲突路径写入……)。风格照抄 test_aigit.py:手写断言 + TAP 输出,
不引 pytest —— 这工具要在别人机器上直接跑,`pip install` 本身就是失败原因。

用法:
    python test_conflicts.py            # 全跑
    python test_conflicts.py -v         # 连断言细节一起打
    python test_conflicts.py -k nobase  # 只跑名字含 nobase 的

纪律(与 test_aigit.py 一致):
  * **不碰 C:\\poject\\ 下任何真实仓库**,测试仓一律建在
    %TEMP%\\aigit-conflict-test\\ 下,每个用例一个子目录,跑完即删。
  * 只读 conflicts.py / gen.py,不修改它们 —— 测出问题只记录,不动代码。

数据来源说明:
  * `conflict` 场景优先用 **gen.py**(aigit2 那条线)真造,不自己仿造。
  * 但 gen.py 的 conflict 场景**只改同一行**,造不出「一个文件多个冲突块」
    和「双方新增同名文件(无祖先)」这两种形态 —— 那两条用例自己搭最小仓库,
    否则测不到。这是刻意的分工,不是绕过。
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from conflicts import ConflictError, ConflictResolver, parse_markers   # noqa: E402

SANDBOX = Path(tempfile.gettempdir()) / "aigit-conflict-test"
ENV = {**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
       "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no", "LC_ALL": "C"}


# ---------------------------------------------------------------- 断言

class Fail(Exception):
    pass


def ok(cond, what):
    if not cond:
        raise Fail(what)


def eq(want, got, what):
    if want != got:
        raise Fail("%s: 期望 %r,实际 %r" % (what, want, got))


def contains(hay, needle, what):
    if needle not in hay:
        raise Fail("%s: %r 里找不到 %r" % (what, hay, needle))


# ---------------------------------------------------------------- git / 仓库

def g(root, *args, check=True, binary=False):
    p = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True,
                       text=not binary, encoding=None if binary else "utf-8",
                       errors=None if binary else "replace", env=ENV, timeout=120)
    if check and p.returncode != 0:
        raise Fail("git %s rc=%d: %s" % (" ".join(args[:3]), p.returncode,
                                        (p.stderr or p.stdout or b"")))
    return p


def rmtree(path):
    """删目录树。**必须先放开只读位** —— Windows 上 git 的 object 文件
    (.git/objects/xx/yyyy)落盘就是只读的,直接 shutil.rmtree 会 PermissionError。
    这是这个测试在 Windows 上唯一的平台性坑,处理掉它测试就能跨平台跑。
    """
    if not path.exists():
        return

    def onerror(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=onerror)


def mk(name):
    """建一个干净仓库,返回路径。每次调用前先删同名目录。"""
    root = SANDBOX / name
    rmtree(root)
    root.mkdir(parents=True)
    g(root, "init", "-b", "main")
    g(root, "config", "user.name", "aigit")
    g(root, "config", "user.email", "aigit@localhost")
    g(root, "config", "commit.gpgsign", "false")
    g(root, "config", "core.autocrlf", "false")
    return root


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(text.encode("utf-8"))
    return rel


def commit_all(root, msg):
    g(root, "add", "-A")
    g(root, "commit", "-q", "-m", msg)
    return g(root, "rev-parse", "HEAD").stdout.strip()


def lines_of(root, rev, rel):
    """直接问 git 拿一份原文,用作逐字节比对的基准。"""
    p = g(root, "show", "%s:%s" % (rev, rel), binary=True)
    return p.stdout if p.returncode == 0 else None


def unmerged(root):
    return [x for x in g(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if x.strip()]


def porcelain(root):
    return g(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()


# ---------------------------------------------------------------- 夹具

def fixture_gen_conflict():
    """用 **gen.py** 造 conflict 场景(aigit2 那条线),再真跑一次 merge 制造冲突态。"""
    from gen import ScenarioGen
    root = mk("gen_conflict")
    write(root, "README.md", "# gen conflict fixture\n")
    commit_all(root, "init")
    res = ScenarioGen(root).generate("conflict", 1, seed=20261001)
    ok(res["ok"], "gen.py 自己报失败: %s" % res.get("summary"))
    sc = res["scenarios"][0]
    a, b = sc["branches"]
    g(root, "merge", "--no-edit", a)          # base 的直系后代,快进
    m = g(root, "merge", "--no-edit", b, check=False)
    ok(m.returncode != 0, "gen.py 的 conflict 场景竟然合干净了,用例失去意义")
    return root, sc


def fixture_multi_region():
    """一个文件、**两个**冲突块。gen.py 造不出来(它只改同一行)。"""
    root = mk("multi_region")
    base = "\n".join("line %03d original" % i for i in range(1, 61)) + "\n"
    write(root, "multi.txt", base)
    commit_all(root, "seed multi")

    def variant(tag):
        ls = base.split("\n")
        ls[9] = "    # OURS  line 010 %s" % tag       # 第 10 行
        ls[39] = "    # OURS  line 040 %s" % tag      # 第 40 行
        return "\n".join(ls)

    g(root, "checkout", "-q", "-b", "feature-ours")
    write(root, "multi.txt", variant("side-ours"))
    commit_all(root, "ours edits two spots")

    g(root, "checkout", "-q", "-b", "feature-theirs", "main")
    write(root, "multi.txt", variant("side-theirs"))
    commit_all(root, "theirs edits same two spots")

    g(root, "checkout", "-q", "main")
    m = g(root, "merge", "--no-edit", "feature-ours")
    eq(0, m.returncode, "合 ours 应当成功")
    m = g(root, "merge", "--no-edit", "feature-theirs", check=False)
    ok(m.returncode != 0, "合 theirs 应当冲突")
    return root


def fixture_no_base():
    """双方**新增同名文件** → 没有共同祖先 → add/add 冲突。

    两个根提交彼此无关联,`git merge-base` 会返回**非零退出码**且输出为空。
    本用例专门盯这条路径:不能抛异常崩掉,要标成 noBase 让前端改用两栏。
    """
    root = mk("no_base")
    write(root, "common.txt", "kept\n")
    commit_all(root, "root 1")
    root_a = g(root, "rev-parse", "HEAD").stdout.strip()

    g(root, "checkout", "-q", "--orphan", "unrelated")
    g(root, "rm", "-q", "-rf", ".")
    write(root, "fresh.txt", "THEIRS side content\n")
    write(root, "kept2.txt", "only on theirs\n")
    commit_all(root, "root 2 (unrelated)")
    root_b = g(root, "rev-parse", "HEAD").stdout.strip()

    g(root, "checkout", "-q", "main")
    write(root, "fresh.txt", "OURS side content\n")
    commit_all(root, "ours adds fresh.txt")

    ok(root_a != root_b, "两个根提交应当不同")
    m = g(root, "merge", "--no-edit", "--allow-unrelated-histories",
          "unrelated", check=False)
    ok(m.returncode != 0, "add/add 且无祖先,应当冲突")

    # ⚠ 必须在**合并之后**才量 merge-base 的退出码。合并之前 MERGE_HEAD
    # 还不存在,那时 git 报的是 128(fatal: 不是合法的对象名),不是 1 ——
    # 量错时机会得出「无祖先 = rc 128」这种错误结论,后面模块报 rc=1
    # 反而看着像不一致。
    p = g(root, "merge-base", "HEAD", "MERGE_HEAD", check=False)
    eq(1, p.returncode, "没有共同祖先时 merge-base 应返回 1(不是 128)")
    eq("", p.stdout.strip(), "没有共同祖先时输出应为空")
    return root, p.returncode


def fixture_delete_modify():
    """一边删、一边改 → modify/delete 冲突(有一侧在 base 之上不存在)。

    ⚠ **哪一侧是 ours 取决于先合谁,不是取决于分支名。** 这里的合并次序是
    先 feature-del(干净快进,main 就此删掉文件),再 feature-mod → 撞车时
    **ours = main = 已删除**,theirs = feature-mod = 改过。git 的 status
    给的是 `DU`(deleted by us)。所以下面断言的是 ours 缺失。
    反过来先合 mod 再合 del,得到的是 `UD`(ours 改、theirs 删)。
    两种方向都测,免得只对其中一种成立。
    """
    root = mk("delete_modify")
    write(root, "doomed.txt", "line one\nline two\nline three\n")
    commit_all(root, "seed doomed")

    g(root, "checkout", "-q", "-b", "feature-del")
    g(root, "rm", "-q", "doomed.txt")
    commit_all(root, "theirs deletes doomed.txt")

    g(root, "checkout", "-q", "-b", "feature-mod", "main")
    write(root, "doomed.txt", "line one\nline two EDITED\nline three\n")
    commit_all(root, "ours modifies doomed.txt")

    g(root, "checkout", "-q", "main")
    g(root, "merge", "--no-edit", "feature-del")          # 快进,main 删掉了文件
    m = g(root, "merge", "--no-edit", "feature-mod", check=False)
    ok(m.returncode != 0, "modify/delete 应当冲突")
    return root


def fixture_delete_modify_reversed():
    """同上但反向:先合 mod(干净),再合 del → `UD`,ours 改、theirs 删。"""
    root = mk("delete_modify_rev")
    write(root, "doomed.txt", "line one\nline two\nline three\n")
    commit_all(root, "seed doomed")

    g(root, "checkout", "-q", "-b", "feature-mod", "main")
    write(root, "doomed.txt", "line one\nline two EDITED\nline three\n")
    commit_all(root, "ours modifies doomed.txt")

    g(root, "checkout", "-q", "-b", "feature-del", "main")
    g(root, "rm", "-q", "doomed.txt")
    commit_all(root, "theirs deletes doomed.txt")

    g(root, "checkout", "-q", "main")
    g(root, "merge", "--no-edit", "feature-mod")          # 快进,main 改了文件
    m = g(root, "merge", "--no-edit", "feature-del", check=False)
    ok(m.returncode != 0, "modify/delete(反向)应当冲突")
    return root


# ---------------------------------------------------------------- 测试用例

TESTS = []


def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


# ---- 1. 造出冲突仓库并识别出冲突文件 ------------------------------------

@test("identify: 用 gen.py 造冲突场景,并识别出冲突文件")
def t_identify(v):
    root, sc = fixture_gen_conflict()
    v("gen.py 场景: %s  文件=%s  分支=%s"
      % (sc["id"], sc["files"], sc["branches"]))

    r = ConflictResolver(root)
    st = r.merge_state()
    eq(True, st["inMerge"], "应当处于合并态")
    ok(st["mergeHead"], "MERGE_HEAD 应可读")
    eq(1, st["remaining"], "应剩 1 个未解决文件")

    lst = r.list_conflicts()
    eq(1, lst["count"], "冲突文件数")
    eq(sc["files"][0], lst["files"][0]["path"], "冲突文件路径")
    eq(False, lst["files"][0]["noBase"], "该场景有 base")
    eq(1, lst["files"][0]["regionCount"], "gen.py 只改一行 → 1 个冲突块")

    v("识别到冲突文件: %s  blocks=%d  columns=%d  verified=%s"
      % (lst["files"][0]["path"], lst["files"][0]["regionCount"],
         lst["files"][0]["columns"], lst["files"][0]["verified"]))

    # 分支名必须来自 .git/MERGE_MSG,不是编的
    msg = (root / ".git" / "MERGE_MSG").read_text(encoding="utf-8")
    contains(msg, "Merge branch", "MERGE_MSG 原文")
    eq([sc["branches"][1]], st["branches"], "分支名应从 MERGE_MSG 抽出")
    eq(msg.splitlines()[0].strip(), st["defaultMessage"], "默认提交信息应等于 MERGE_MSG 首行")
    v("MERGE_MSG 首行: %r  →  branches=%r" % (st["defaultMessage"], st["branches"]))


# ---- 2. 三方数据逐字节正确 --------------------------------------------

@test("threeway: ours/theirs/base 逐字节等于 git show 的输出")
def t_threeway(v):
    root, sc = fixture_gen_conflict()
    rel = sc["files"][0]
    d = ConflictResolver(root).inspect(rel)

    # git 自己给的三个 stage
    stages = d["stages"]
    eq(["1", "2", "3"], sorted(stages), "索引应有 stage 1/2/3")
    v("stages: 1(base)=%s  2(ours)=%s  3(theirs)=%s"
      % (stages["1"]["sha"][:10], stages["2"]["sha"][:10], stages["3"]["sha"][:10]))

    mb = g(root, "merge-base", "HEAD", "MERGE_HEAD").stdout.strip()
    ok(mb, "本场景应当有 merge-base")
    for key, rev in (("ours", "HEAD"), ("theirs", "MERGE_HEAD"), ("base", mb)):
        want = lines_of(root, rev, rel)               # 直接问 git
        eq(want.decode("utf-8"), d["sides"][key]["text"], "%s 侧逐字节" % key)
        eq(True, d["crossCheck"][key]["matchesIndex"], "%s 侧索引交叉校验" % key)
        eq(stages[{"ours": "2", "theirs": "3", "base": "1"}[key]]["sha"],
           d["sides"][key]["sha"], "%s 侧 sha 应等于对应 stage" % key)
        v("%-6s 逐字节 == git show %-11s  len=%d  sha=%s"
          % (key, rev, len(want), d["sides"][key]["sha"][:10]))

    eq([], d["crossCheckMismatch"], "三处来源应当一致")
    eq(True, d["verified"], "每个 region 都应自检通过")
    v("三方交叉校验: 无不一致, region 自检 verified=%s" % d["verified"])


# ---- 3. 一个文件多个冲突块 --------------------------------------------

@test("multi: 多冲突块的文件,块数与起止行号正确")
def t_multi(v):
    root = fixture_multi_region()
    rel = "multi.txt"
    r = ConflictResolver(root)
    eq([rel], unmerged(root), "应当 1 个冲突文件")

    d = r.inspect(rel)
    eq(2, d["regionCount"], "应当有 2 个冲突块")
    eq(True, d["verified"], "两个块都应自检通过")

    raw = (root / rel).read_text(encoding="utf-8")
    wt = raw.split("\n")
    if wt and wt[-1] == "":
        wt.pop()

    # 每块:标记行必须真的是标记;起止行号必须真的框住对应的文本
    for reg in d["regions"]:
        om, sm, tm = reg["oursMarkerLine"], reg["sepLine"], reg["theirsMarkerLine"]
        contains(wt[om - 1], "<<<<<<<", "ours 标记行")
        eq("=======", wt[sm - 1].strip(), "分隔行")
        contains(wt[tm - 1], ">>>>>>>", "theirs 标记行")
        ok(om < sm < tm, "标记行号必须递增")

        # 工作区行号:oursStart..oursEnd 必须逐字等于 oursText
        got_ours = "\n".join(wt[reg["oursStart"] - 1:reg["oursEnd"]])
        eq(reg["oursText"].rstrip("\n"), got_ours, "region#%d 工作区 ours 行号" % reg["index"])
        got_theirs = "\n".join(wt[reg["theirsStart"] - 1:reg["theirsEnd"]])
        eq(reg["theirsText"].rstrip("\n"), got_theirs, "region#%d 工作区 theirs 行号" % reg["index"])

        # 源文件行号:在 ours / theirs / base 三份里定位到的段,
        # 必须逐字等于**那个源文件自己的 base 位置上的内容**。
        #
        # ⚠ merge 风格(`<<<<<<</=======/>>>>>>>`)的工作区文件里**没有 base 段**
        # —— base 只能靠对齐算出来,拿 reg["baseText"] 比是错的(那个字段此时是
        # None,只有 diff3/zdiff3 风格才有值)。所以这里比的是「按算出来的行号
        # 去 base 文件里切」的结果是否自洽,以及 ours/theirs 两段是否与标记一致。
        for key in ("ours", "theirs"):
            s, e = reg[key + "LineStart"], reg[key + "LineEnd"]
            ok(s is not None and e is not None,
               "region#%d 的 %s 侧应能定位" % (reg["index"], key))
            side_text = d["sides"][key]["text"].split("\n")
            if side_text and side_text[-1] == "":
                side_text.pop()
            seg = "\n".join(side_text[s - 1:e])
            eq(reg[key + "Text"].rstrip("\n"), seg,
               "region#%d 在 %s 文件里的行号" % (reg["index"], key))

        # base 侧:行号必须指得出,且切出来的东西要与模块自报的 baseLocatedText 一致
        bs, be = reg["baseLineStart"], reg["baseLineEnd"]
        ok(bs is not None and be is not None,
           "region#%d 的 base 侧应能定位" % reg["index"])
        base_text = d["sides"]["base"]["text"].split("\n")
        if base_text and base_text[-1] == "":
            base_text.pop()
        base_seg = "\n".join(base_text[bs - 1:be])
        eq(reg["baseLocatedText"].rstrip("\n"), base_seg,
           "region#%d 的 base 段行号自洽" % reg["index"])
        eq(reg["baseText"], None, "merge 风格没有 baseText(该字段只对 diff3 有值)")

        v("region#%d  标记 L%s/L%s/L%s   工作区 ours=[%s,%s] theirs=[%s,%s]"
          "   源文件 ours=[%s,%s] theirs=[%s,%s] base=[%s,%s]"
          % (reg["index"], om, sm, tm,
             reg["oursStart"], reg["oursEnd"], reg["theirsStart"], reg["theirsEnd"],
             reg["oursLineStart"], reg["oursLineEnd"],
             reg["theirsLineStart"], reg["theirsLineEnd"],
             reg["baseLineStart"], reg["baseLineEnd"]))
        v("           ours  =%r" % reg["oursText"])
        v("           theirs=%r" % reg["theirsText"])
        v("           base  =%r" % reg["baseLocatedText"])

    # 原始内容确实是被两边各改了两处
    contains(d["sides"]["ours"]["text"], "OURS  line 010 side-ours", "ours 侧内容")
    contains(d["sides"]["ours"]["text"], "OURS  line 040 side-ours", "ours 侧内容")
    contains(d["sides"]["theirs"]["text"], "OURS  line 010 side-theirs", "theirs 侧内容")


# ---- 4. 无 base 的冲突 -------------------------------------------------

@test("nobase: 双方新增同名文件 → 正确标记 no-base(merge-base 返回非零)")
def t_nobase(v):
    root, mb_rc = fixture_no_base()
    eq(1, mb_rc, "merge-base 无共同祖先时 rc=1")
    v("merge-base 退出码 = %d,输出为空(非 128 —— 128 是「MERGE_HEAD 不存在」"
      "才会给的码,量错时机就会看错)" % mb_rc)

    r = ConflictResolver(root)
    d = r.inspect("fresh.txt")
    eq(True, d["noBase"], "应当标记 no-base")
    eq(2, d["columns"], "无 base 时前端应改用两栏")
    ok(d["noBaseReason"], "应说明原因")
    ok("1" not in d["stages"], "无 base 时索引里不应有 stage 1,实际 %r" % sorted(d["stages"]))
    eq(["2", "3"], sorted(d["stages"]), "只应有 stage 2/3")

    # 两边都在,内容各自正确
    eq(lines_of(root, "HEAD", "fresh.txt").decode(), d["sides"]["ours"]["text"], "ours 逐字节")
    eq(lines_of(root, "MERGE_HEAD", "fresh.txt").decode(), d["sides"]["theirs"]["text"], "theirs 逐字节")
    eq(False, d["sides"]["base"]["exists"], "base 侧应不存在")
    eq(None, d["sides"]["base"]["text"], "base 侧文本应为 None")

    # 仍然要能给出冲突块,只是没有 base 段
    eq(1, d["regionCount"], "add/add 应当有 1 个冲突块")
    ok(d["worktree"]["hasMarkers"], "工作区应有标记")
    v("noBase=%s columns=%d reason=%r" % (d["noBase"], d["columns"], d["noBaseReason"]))
    v("stages=%r  (无 stage 1)" % sorted(d["stages"]))
    v("region#0 base 相关字段: baseText=%r baseLineStart=%r"
      % (d["regions"][0]["baseText"], d["regions"][0]["baseLineStart"]))
    v("ours=%r / theirs=%r"
      % (d["regions"][0]["oursText"], d["regions"][0]["theirsText"]))

    # 面板列表也要如实报出来
    lst = r.list_conflicts()
    fb = [f for f in lst["files"] if f["path"] == "fresh.txt"][0]
    eq(True, fb["noBase"], "列表项也要标 no-base")
    eq(2, fb["columns"], "列表项也要给 columns")


# ---- 4b. modify/delete -------------------------------------------------

@test("delmod: 一边删一边改(两个方向)→ 缺失侧标 deleted,不能取空内容去 resolve")
def t_delmod(v):
    # 方向 A:先合删 → main 删了文件,再合改 → `DU`,ours 缺失
    root = fixture_delete_modify()
    r = ConflictResolver(root)
    d = r.inspect("doomed.txt")

    # modify/delete **有 base**,所以仍是三栏 —— 缺的只是某一侧。
    # noBase 说的是「取不到祖先内容」,不是「有一侧不存在」。这两件事别混。
    eq(3, d["columns"], "modify/delete 有 base → 仍是三栏")
    eq(False, d["noBase"], "modify/delete 不是 no-base")
    eq(["1", "3"], sorted(d["stages"]), "DU:有 stage 1/3,没有 stage 2(ours)")
    eq(False, d["sides"]["ours"]["exists"], "DU:ours(已删)应不存在")
    eq(True, d["sides"]["ours"]["deleted"], "DU:ours 应标 deleted")
    eq(True, d["sides"]["theirs"]["exists"], "DU:theirs(改过)应存在")
    eq(True, d["sides"]["base"]["exists"], "DU:base 应存在")
    # git show 与索引必须一致:HEAD 上文件确实已经没了
    eq(False, d["crossCheck"]["ours"]["exists"], "DU:git show HEAD 也应读不到")
    eq([], d["crossCheckMismatch"], "DU:无不一致")
    v("方向A(DU): ours.exists=%s ours.deleted=%s | theirs.exists=%s | base.exists=%s"
      % (d["sides"]["ours"]["exists"], d["sides"]["ours"]["deleted"],
         d["sides"]["theirs"]["exists"], d["sides"]["base"]["exists"]))

    # 取一个不存在的一侧 → 必须拒绝,不能悄悄写空文件
    reason = None
    try:
        r.choose("doomed.txt", "ours")
    except ConflictError as e:
        reason = str(e)
    else:
        raise Fail("choose 一个不存在的一侧,应当拒绝却成功了")
    contains(reason, "ours", "拒绝理由要指明是哪一侧")
    v("choose(ours) 被拒: %s" % reason.split("——")[0].strip())

    # 走 delete_file 才是正确动作
    res = r.delete_file("doomed.txt")
    eq([], unmerged(root), "标记删除后不应再有 U")
    eq(0, res["remainingCount"], "应当全部解决完")
    v("方向A delete_file 后 remaining=%r" % res["remaining"])

    # 方向 B:先合改 → main 改了文件,再合删 → `UD`,theirs 缺失
    root2 = fixture_delete_modify_reversed()
    d2 = ConflictResolver(root2).inspect("doomed.txt")
    eq(["1", "2"], sorted(d2["stages"]), "UD:有 stage 1/2,没有 stage 3(theirs)")
    eq(True, d2["sides"]["ours"]["exists"], "UD:ours(改过)应存在")
    eq(False, d2["sides"]["theirs"]["exists"], "UD:theirs(已删)应不存在")
    eq(True, d2["sides"]["theirs"]["deleted"], "UD:theirs 应标 deleted")
    eq(False, d2["crossCheck"]["theirs"]["exists"], "UD:git show MERGE_HEAD 也应读不到")
    eq([], d2["crossCheckMismatch"], "UD:无不一致")
    v("方向B(UD): ours.exists=%s | theirs.exists=%s theirs.deleted=%s"
      % (d2["sides"]["ours"]["exists"], d2["sides"]["theirs"]["exists"],
         d2["sides"]["theirs"]["deleted"]))

    reason2 = None
    try:
        ConflictResolver(root2).choose("doomed.txt", "theirs")
    except ConflictError as e:
        reason2 = str(e)
    else:
        raise Fail("UD 方向 choose(theirs) 应当拒绝")
    contains(reason2, "theirs", "拒绝理由要指明是哪一侧")
    v("方向B choose(theirs) 被拒: %s" % reason2.split("——")[0].strip())


# ---- 5. resolve 写回 --------------------------------------------------

@test("resolve: 写回后 git add 成功,文件不再被标记 U")
def t_resolve(v):
    root, sc = fixture_gen_conflict()
    rel = sc["files"][0]
    r = ConflictResolver(root)
    d = r.inspect(rel)

    # 模拟用户编辑:把两侧都手写进去,而不是丢掉一段。用工作区的真实标记
    # 结构做手术 —— 把 ours 段和 theirs 段都留下,换成自己的一行,
    # 这样「多块 + 手改」这条路径也被覆盖到。
    wt = d["worktree"]["text"]
    reg0 = d["regions"][0]
    lines = wt.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    om, sm, tm = reg0["oursMarkerLine"], reg0["sepLine"], reg0["theirsMarkerLine"]
    kept = (lines[:om - 1]
            + ["# RESOLVED BY HAND: took both sides"]
            + lines[tm:])
    body = "\n".join(kept) + "\n"

    # 写回的东西不该再含任何冲突标记,否则 git 下次还是会当它冲突
    for mark in ("<<<<<<<", "=======", ">>>>>>>", "|||||||"):
        ok(mark not in body, "写回内容不该含标记 %s" % mark)
    ok("# RESOLVED BY HAND" in body, "写回内容应含我加的那行")
    ok(reg0["oursText"].strip() and reg0["theirsText"].strip(),
       "两侧原本都该有内容")
    v("手工编辑: %d 行 → %d 行(原 ours %d 行 + theirs %d 行,替换成 1 行)"
      % (len(lines), len(kept), reg0["oursLines"], reg0["theirsLines"]))

    res = r.resolve(rel, body, expect_sha256=d["worktree"]["sha256"])
    eq(True, res["staged"], "应当已 staged")
    eq(False, res["stillUnmerged"], "该文件不应再是 U")
    eq([], unmerged(root), "git 视角:没有未合并路径了")
    v("resolve 返回: staged=%s stillUnmerged=%s remaining=%r"
      % (res["staged"], res["stillUnmerged"], res["remaining"]))
    v("索引状态: %s" % porcelain(root))
    v("落盘内容含我加的行: %s"
      % ("# RESOLVED BY HAND" in (root / rel).read_text(encoding="utf-8")))

    # 写进去的必须逐字节对上
    eq(body, (root / rel).read_text(encoding="utf-8"), "落盘内容")

    r.commit_merge()
    v("合并已收尾")


@test("resolve-sha: expect_sha256 不匹配时拒绝覆盖(乐观并发)")
def t_resolve_sha(v):
    # 单独一个仓做这条 —— resolve 成功后路径就不再是未合并路径,
    # 拿同一个仓复测会被「不是未合并路径」这道更靠前的护栏拦下,
    # 根本走不到 sha256 那一步,测的就不是想测的东西了。
    root, _sc = fixture_gen_conflict()
    rel = unmerged(root)[0]
    r = ConflictResolver(root)
    d = r.inspect(rel)

    before = (root / rel).read_bytes()
    reason = None
    try:
        r.resolve(rel, "别人的版本\n", expect_sha256="0" * 64)
    except ConflictError as e:
        reason = str(e)
    else:
        raise Fail("sha256 不匹配应当拒绝")
    contains(reason, "拒绝覆盖", "拒绝理由")
    eq(before, (root / rel).read_bytes(), "被拒后文件内容不能变")
    eq([rel], unmerged(root), "被拒后仍是冲突态")
    v("错 sha256 被拒: %s" % reason.split("。")[0])
    v("被拒后文件未变=%s 仍在冲突=%s"
      % (before == (root / rel).read_bytes(), unmerged(root) == [rel]))

    # 顺带验证:带对 sha256 就能写成
    res = r.resolve(rel, "对的版本\n", expect_sha256=d["worktree"]["sha256"])
    eq(False, res["stillUnmerged"], "对摘要应当写成")
    eq("对的版本\n", (root / rel).read_text(encoding="utf-8"), "落盘内容")
    v("对 sha256 正常写回: stillUnmerged=%s" % res["stillUnmerged"])


# ---- 6. 完成合并 -------------------------------------------------------

@test("commit: 全部解决后能完成合并,最终仓库干净,提交信息含分支名")
def t_commit(v):
    root, _sc = fixture_gen_conflict()
    rel = unmerged(root)[0]
    r = ConflictResolver(root)

    # 还有 U 时提交必须被拒
    try:
        r.commit_merge()
        raise Fail("还有未解决文件时应当拒绝提交")
    except ConflictError as e:
        contains(str(e), "没解决", "拒绝理由")
        v("未解决时提交被拒: %s" % str(e).splitlines()[0])
    ok(unmerged(root), "被拒后不应改变仓库状态")

    # 用 choose 整份取 ours(真实用户动作),再收尾
    r.choose(rel, "ours")
    eq([], unmerged(root), "取 ours 后应当解决完")
    v("choose(ours) 之后 unmerged=%r" % unmerged(root))

    st = r.merge_state()
    eq(True, st["inMerge"], "还在合并态")
    out = r.commit_merge()
    eq(True, out["committed"], "应当提交成功")
    eq("MERGE_MSG", out["messageSource"], "默认信息应来自 MERGE_MSG")
    contains(out["message"], "Merge branch", "提交信息")
    ok(out["branches"], "应列出参与分支")
    v("提交 %s  message=%r  branches=%r"
      % (out["shortCommit"], out["message"], out["branches"]))

    # 最终仓库干净
    eq("", porcelain(root), "仓库应当干净")
    eq([], unmerged(root), "不应有未合并路径")
    ok(not (root / ".git" / "MERGE_HEAD").exists(), "MERGE_HEAD 应已被清掉")
    parents = g(root, "log", "-1", "--pretty=%P").stdout.split()
    eq(2, len(parents), "应当是一个双亲的合并提交")
    v("最终 porcelain=%r  提交双亲数=%d  status=%s"
      % (porcelain(root), len(parents), porcelain(root)))

    # 提交信息里含分支名 —— 从 git log 里读回来核对
    subject = g(root, "log", "-1", "--pretty=%s").stdout.strip()
    for b in out["branches"]:
        contains(subject, b, "git log 的标题里应含分支名")
    v("git log 标题: %r" % subject)

    # 自定义提交信息时也要把参与分支补上 —— 换一个全新仓,别在刚提交完的
    # 仓上做(scan 是最省事的正确做法,操作错了也不会污染上面的断言)
    root2, _sc2 = fixture_gen_conflict()
    rel2 = unmerged(root2)[0]
    r2 = ConflictResolver(root2)
    r2.choose(rel2, "theirs")
    out2 = r2.commit_merge("我自己写的提交信息")
    eq("caller", out2["messageSource"], "自定义信息应标明来源是 caller")
    ok(out2["branches"], "仍应从 MERGE_MSG 读出分支")
    for b in out2["branches"]:
        contains(out2["message"], b, "自定义信息也应补上分支名")
    contains(out2["message"], "我自己写的提交信息", "用户原文要保留")
    eq("", porcelain(root2), "第二个仓也应当干净")
    v("自定义信息: %r" % out2["message"].replace("\n", "\\n"))
    v("       branches=%r  (从 MERGE_MSG 读,非臆造)" % out2["branches"])
    body2 = g(root2, "log", "-1", "--pretty=%B").stdout
    contains(body2, "Merged:", "git log 全文里应有补的分支行")
    v("git log 全文: %r" % body2.strip().replace("\n", " | "))


# ---- 7. 合并已结束时 resolve 被拒绝 -----------------------------------

@test("guard-closed: 合并已结束时 resolve 被拒绝,不动一个已完成的仓库")
def t_guard_closed(v):
    root, _sc = fixture_gen_conflict()
    rel = unmerged(root)[0]
    r = ConflictResolver(root)

    before_head = g(root, "rev-parse", "HEAD").stdout.strip()

    r.choose(rel, "ours")
    r.commit_merge()

    # ⚠ 快照必须在**合并完成之后**取。要证明的是「被拒绝的调用没有改动
    #   仓库」,所以基准是合并刚完那一刻的状态,不是冲突态 —— 拿冲突态的
    #   文件内容当基准,自然对不上(choose 本来就改了它)。
    after_head = g(root, "rev-parse", "HEAD").stdout.strip()
    settled_bytes = (root / rel).read_bytes()
    settled_porcelain = porcelain(root)
    ok(before_head != after_head, "应当产生了新提交")
    eq("", settled_porcelain, "合并刚完成时仓库应当是干净的")

    # 现在合并已经结束 —— resolve 必须拒绝。
    # ⚠ `except ... as e` 在块结束时会把 e 删掉(Python 的作用域规则),
    #   所以拒绝理由要在块**里面**存下来,不能在块外面引用 e。
    for fn, label in ((lambda: r.resolve(rel, "写坏你\n"), "resolve"),
                      (lambda: r.choose(rel, "theirs"), "choose"),
                      (lambda: r.delete_file(rel), "delete_file"),
                      (lambda: r.commit_merge(), "commit_merge")):
        reason = None
        try:
            fn()
        except ConflictError as e:
            reason = str(e)
        else:
            raise Fail("%s 在合并已结束时应当被拒绝,却成功了" % label)
        contains(reason, "不在合并态", "%s 的拒绝理由" % label)
        v("%-12s 被拒: %s" % (label, reason.split("——")[0].strip()))

    # 四次被拒之后,仓库必须与「刚合并完那一刻」逐字节一致
    eq(after_head, g(root, "rev-parse", "HEAD").stdout.strip(), "HEAD 不能动")
    eq(settled_bytes, (root / rel).read_bytes(), "文件内容不能动")
    eq(settled_porcelain, porcelain(root), "工作区状态不能动")
    ok(not (root / ".git" / "MERGE_HEAD").exists(), "不该凭空多出 MERGE_HEAD")
    v("四次拒绝后校验: HEAD 未动=%s  文件未动=%s  porcelain=%r  MERGE_HEAD 不存在=%s"
      % (after_head == g(root, "rev-parse", "HEAD").stdout.strip(),
         settled_bytes == (root / rel).read_bytes(),
         porcelain(root),
         not (root / ".git" / "MERGE_HEAD").exists()))


# ---- 8. 路径越界 -------------------------------------------------------

@test("guard-path: 路径越界被拒绝(各种花样)")
def t_guard_path(v):
    root, _sc = fixture_gen_conflict()
    rel = unmerged(root)[0]
    r = ConflictResolver(root)

    bad = [
        "../../../Windows/System32/drivers/etc/hosts",
        "..%s..%s..%sWindows%ssystem.ini" % ("\\", "\\", "\\", "\\"),
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "subdir/../../outside.txt",
        "",
        "   ",
    ]
    for p in bad:
        try:
            r.resolve(p, "pwned\n")
            raise Fail("越界路径 %r 竟然被接受" % p)
        except ConflictError as e:
            ok("路径" in str(e) or "空" in str(e), "拒绝理由要讲路径: %s" % e)
        v("resolve(%r) 被拒" % p)

    # 越界也必须在 inspect 上被拒 —— 不能只保护写路径
    try:
        r.inspect("../outside.txt")
        raise Fail("inspect 越界竟然被接受")
    except ConflictError:
        v("inspect(../outside.txt) 被拒")

    # .git 内的路径也要拒
    try:
        r.resolve(".git/config", "evil\n")
        raise Fail(".git 内路径竟然被接受")
    except ConflictError as e:
        contains(str(e), ".git", "拒绝理由")
    v("resolve(.git/config) 被拒")

    # 合法路径仍然能通过
    r.resolve(rel, "ok\n")
    v("合法路径 %r 正常写回" % rel)


# ---- 9. 非冲突路径写入被拒 --------------------------------------------

@test("guard-notconflict: 对不是冲突的路径写回被拒绝")
def t_not_conflict(v):
    root, _sc = fixture_gen_conflict()
    r = ConflictResolver(root)
    write(root, "innocent.txt", "我什么也没做错\n")
    reason = None
    try:
        r.resolve("innocent.txt", "改了\n")
    except ConflictError as e:
        reason = str(e)
    else:
        raise Fail("对非冲突路径写回应当被拒绝")
    contains(reason, "不是未合并路径", "拒绝理由")
    v("resolve(innocent.txt) 被拒: %s" % reason)
    eq("我什么也没做错\n", (root / "innocent.txt").read_text(encoding="utf-8"), "内容不能变")


# ---- 10. 标记解析器单测(不依赖 git) ----------------------------------

@test("parse: 标记解析器 —— merge / diff3 / zdiff3 / 空块")
def t_parse(v):
    # 普通 merge 风格
    txt = ("head line\n"
           "<<<<<<< HEAD\n"
           "ours a\nours b\n"
           "=======\n"
           "theirs a\n"
           ">>>>>>> feature\n"
           "tail line\n")
    regs, skel = parse_markers(txt)
    eq(1, len(regs), "merge 风格 1 块")
    r0 = regs[0]
    eq("merge", r0["style"], "style")
    eq(2, r0["oursMarkerLine"], "<<<<<<< 在第 2 行")
    eq(5, r0["sepLine"], "======= 在第 5 行")
    eq(7, r0["theirsMarkerLine"], ">>>>>>> 在第 7 行")
    eq("ours a\nours b\n", r0["oursText"], "ours 段")
    eq("theirs a\n", r0["theirsText"], "theirs 段")
    eq("HEAD", r0["oursLabel"], "ours 标签")
    eq("feature", r0["theirsLabel"], "theirs 标签")
    v("merge 风格: marker L%s/L%s/L%s ours=%r theirs=%r"
      % (r0["oursMarkerLine"], r0["sepLine"], r0["theirsMarkerLine"],
         r0["oursText"], r0["theirsText"]))

    # diff3 风格:多一段 |||||||
    txt3 = ("<<<<<<< HEAD\n"
            "ours\n"
            "||||||| base\n"
            "base\n"
            "=======\n"
            "theirs\n"
            ">>>>>>> feature\n")
    regs3, _ = parse_markers(txt3)
    eq(1, len(regs3), "diff3 风格 1 块")
    eq("diff3", regs3[0]["style"], "style 应是 diff3")
    eq("base\n", regs3[0]["baseText"], "base 段")
    eq("ours\n", regs3[0]["oursText"], "ours 段")
    eq("theirs\n", regs3[0]["theirsText"], "theirs 段")
    v("diff3 风格: style=%s base=%r" % (regs3[0]["style"], regs3[0]["baseText"]))

    # ours 侧为空
    empt = ("<<<<<<< HEAD\n"
            "=======\n"
            "theirs only\n"
            ">>>>>>> f\n")
    re_, _ = parse_markers(empt)
    eq(1, len(re_), "空 ours 块")
    eq("", re_[0]["oursText"], "ours 段应为空串")
    eq("theirs only\n", re_[0]["theirsText"], "theirs 段")
    v("空 ours 块: ours=%r theirs=%r" % (re_[0]["oursText"], re_[0]["theirsText"]))

    # 两个空侧
    both = "<<<<<<< HEAD\n=======\n>>>>>>> f\n"
    rb, _ = parse_markers(both)
    eq(1, len(rb), "双空块")
    eq("", rb[0]["oursText"], "ours 空")
    eq("", rb[0]["theirsText"], "theirs 空")
    v("双空块: line=%s..%s" % (rb[0]["oursMarkerLine"], rb[0]["theirsMarkerLine"]))

    # 没有标记
    regs0, _ = parse_markers("just\nplain\ntext\n")
    eq(0, len(regs0), "无标记应 0 块")

    # 多个块
    multi = ("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n"
             "mid\n"
             "<<<<<<< HEAD\nc\n=======\nd\n>>>>>>> y\n")
    rm, _ = parse_markers(multi)
    eq(2, len(rm), "多块")
    # 第 1 行 <<<,2 a,3 ===,4 b,5 >>>,6 mid,7 <<<  → 第二块从第 7 行起
    eq([1, 7], [x["oursMarkerLine"] for x in rm], "两个块的起始行")
    eq([5, 11], [x["theirsMarkerLine"] for x in rm], "两个块的结束行")
    eq(["a\n", "c\n"], [x["oursText"] for x in rm], "两个块的 ours")
    v("多块: 起始行=%r ours=%r" % ([x["oursMarkerLine"] for x in rm],
                                   [x["oursText"] for x in rm]))


# ---- 11. 非合并态的只读查询不炸 --------------------------------------

@test("read-nomerge: 不在合并态时只读接口不崩,写接口一律拒绝")
def t_nomerge(v):
    root = mk("no_merge")
    write(root, "a.txt", "hi\n")
    commit_all(root, "only commit")

    r = ConflictResolver(root)
    st = r.merge_state()
    eq(False, st["inMerge"], "不该是合并态")
    eq(0, st["remaining"], "没有剩余")
    lst = r.list_conflicts()
    eq(0, lst["count"], "没有冲突文件")
    v("不在合并态: inMerge=%s remaining=%s count=%d"
      % (st["inMerge"], st["remaining"], lst["count"]))

    for fn, label in ((lambda: r.commit_merge(), "commit_merge"),
                      (lambda: r.choose("a.txt", "ours"), "choose"),
                      (lambda: r.delete_file("a.txt"), "delete_file")):
        try:
            fn()
            raise Fail("%s 在非合并态应当拒绝" % label)
        except ConflictError as e:
            contains(str(e), "不在合并态", "%s 拒绝理由" % label)
        v("%-12s 在非合并态被拒" % label)


# ---------------------------------------------------------------- 运行

def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 冲突解决测试")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印断言细节")
    ap.add_argument("-k", default=None, help="只跑名字含此串的用例")
    a = ap.parse_args(argv)

    rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    picked = [(n, f) for n, f in TESTS if not a.k or a.k in n]
    print("1..%d" % len(picked))

    def v(msg):
        if a.verbose:
            print("    %s" % msg)

    failed = 0
    for i, (name, fn) in enumerate(picked, 1):
        try:
            fn(v)
        except Exception as e:
            failed += 1
            print("not ok %d - %s" % (i, name))
            print("    %s: %s" % (type(e).__name__, e))
            if not isinstance(e, Fail):
                tb = traceback.format_exc().rstrip().splitlines()
                for ln in tb[-6:]:
                    print("    | %s" % ln)
        else:
            print("ok %d - %s" % (i, name))

    if failed:
        print("# failed %d among %d test(s)" % (failed, len(picked)))
    else:
        print("# passed all %d test(s)" % len(picked))
    rmtree(SANDBOX)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

