#!/usr/bin/env python3
"""
aigit 生成器自测 —— 8 组,全部在 %TEMP% 下的临时仓里跑,绝不碰真实仓库。

关键设计:验证「可合并」时,本脚本**自己**再跑一遍 git merge,
而不是转述 ScenarioGen 自己报的 verified 字段。
生成器自证 + 独立复核,两层都过才算通过。

跑法:
    python selftest.py
    python selftest.py -v      # 附带 git 命令回显
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen import ScenarioGen, GenError, PREFIX, BRANCH_PREFIX  # noqa: E402

TMP = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "aigit-gen-test"
VERBOSE = "-v" in sys.argv

PASS, FAIL = "通过", "未通过"
results = []


def say(s=""):
    print(s, flush=True)


def hdr(t):
    say()
    say("=" * 74)
    say("  " + t)
    say("=" * 74)


def record(group, ok, detail=""):
    results.append((group, ok, detail))
    return ok


def rmtree_force(path, tries=8):
    """Windows 上 git / 杀软会短暂持有 .git/objects 里的句柄,
    直接 rmtree 会 PermissionError。重试 + 放宽只读位,最后兜底忽略。"""
    import time
    path = Path(path)
    for i in range(tries):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            for dp, dns, fns in os.walk(path):
                for fn in fns:
                    try:
                        os.chmod(os.path.join(dp, fn), 0o666)
                    except OSError:
                        pass
            time.sleep(0.3 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def fresh_repo(name):
    """建一个干净的临时仓。已经存在就清掉重来 —— 自测必须是可重复的。"""
    root = TMP / name
    rmtree_force(root)
    root.mkdir(parents=True, exist_ok=True)
    g = subprocess.run(["git", "-C", str(root), "init", "-b", "main"],
                       capture_output=True, text=True)
    assert g.returncode == 0, g.stderr
    for k, v in (("user.email", "selftest@localhost"), ("user.name", "selftest"),
                 ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        subprocess.run(["git", "-C", str(root), "config", k, v], check=True)
    readme = root / "README.md"
    with open(readme, "w", encoding="utf-8", newline="\n") as f:
        f.write("# selftest sandbox\n\n临时仓,可随便删。\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True)
    return root


def git(root, *args, check=False):
    if VERBOSE:
        say("      $ git " + " ".join(args))
    p = subprocess.run(["git", "-C", str(root)] + list(args),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise AssertionError("git %s 失败: %s" % (" ".join(args), p.stderr))
    return p


def independent_verify(root, sc, expect_clean):
    """独立复核:自己跑 git merge,不读生成器的 verified 字段。"""
    base = sc["base"]
    a, b = sc["branches"]
    git(root, "checkout", "-q", "-f", "main")
    git(root, "reset", "--hard", "-q", base)

    if expect_clean:
        r1 = git(root, "merge", "--no-edit", "-q", a)
        r2 = git(root, "merge", "--no-edit", "-q", b)
        status = git(root, "status", "--porcelain").stdout.strip()
        unmerged = [x for x in git(root, "diff", "--name-only",
                                   "--diff-filter=U").stdout.splitlines() if x.strip()]
        ok = (r1.returncode == 0 and r2.returncode == 0
              and status == "" and unmerged == [])
        say("      merge A rc=%d   merge B rc=%d" % (r1.returncode, r2.returncode))
        say("      git status --porcelain -> %s" % (repr(status) if status else "(空,干净)"))
        say("      git diff --diff-filter=U -> %s" % (unmerged if unmerged else "(空)"))
    else:
        # 冲突必须「先合 A(快进成功)、再合 B(撞同一行)」。
        # 只合 A 看不出冲突 —— A 相对 base 是快进,压根不做内容合并。
        r1 = git(root, "merge", "--no-edit", "-q", a)
        r2 = git(root, "merge", "--no-edit", "-q", b)
        unmerged = [x for x in git(root, "diff", "--name-only",
                                   "--diff-filter=U").stdout.splitlines() if x.strip()]
        ok = r1.returncode == 0 and r2.returncode != 0 and len(unmerged) > 0
        say("      merge A rc=%d  (快进,期望 0)" % r1.returncode)
        say("      merge B rc=%d  (期望非 0)" % r2.returncode)
        say("      冲突文件列表 -> %s" % (unmerged if unmerged else "(空) <<< 不符合预期"))
        if unmerged:
            body = git(root, "diff", "--diff-filter=U").stdout
            say("      冲突内容节选:")
            for ln in body.splitlines()[:10]:
                say("        " + ln)

    git(root, "merge", "--abort")
    git(root, "checkout", "-q", "-f", "main")
    git(root, "reset", "--hard", "-q", base)
    return ok


def snapshot(root):
    """仓里所有非 .git 文件的 relpath -> 内容,用于「有没有误伤别的文件」。"""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            p = Path(dp) / fn
            rel = str(p.relative_to(root)).replace("\\", "/")
            with open(p, "rb") as f:
                out[rel] = f.read()
    return out


# ------------------------------------------------------------------ 8 组

def g_mergeable():
    hdr("组 1  mergeable ×3 —— 同一文件的不同区域,必须真合并成功")
    root = fresh_repo("mergeable")
    before = snapshot(root)
    r = ScenarioGen(root).generate("mergeable", count=3, seed=1001)
    say("  生成器自报 allVerified = %s" % r["allVerified"])
    allok = r["allVerified"]
    for sc in r["scenarios"]:
        say("  --- %s  base=%s" % (sc["id"], sc["base"]))
        say("      A: %s" % sc["aEdit"])
        say("      B: %s" % sc["bEdit"])
        say("      间隔行数 = %s" % sc.get("gapLines", "-"))
        allok &= independent_verify(root, sc, True)
    # 组 8 的前半:文件前缀
    after = snapshot(root)
    added = sorted(set(after) - set(before))
    say("  新增文件: %s" % added)
    bad = [p for p in added if not p.startswith(PREFIX)]
    say("  不带 %s 前缀的新增文件: %s" % (PREFIX, bad if bad else "(无)"))
    changed = [p for p in before if p in after and before[p] != after[p]]
    say("  被改动的原有文件: %s" % (changed if changed else "(无)"))
    return record("mergeable ×3 真合并 + 前缀护栏", allok and not bad and not changed)


def g_unrelated():
    hdr("组 2  unrelated ×3 —— 两个分支改完全不同的文件,必须真合并成功")
    root = fresh_repo("unrelated")
    r = ScenarioGen(root).generate("unrelated", count=3, seed=2002)
    say("  生成器自报 allVerified = %s" % r["allVerified"])
    allok = r["allVerified"]
    for sc in r["scenarios"]:
        say("  --- %s  base=%s" % (sc["id"], sc["base"]))
        say("      A: %s" % sc["aEdit"])
        say("      B: %s" % sc["bEdit"])
        allok &= independent_verify(root, sc, True)
    return record("unrelated ×3 真合并", allok)


def g_adddel():
    hdr("组 3  adddel ×3 —— A 新增文件 / B 删除另一个文件,必须真合并成功")
    root = fresh_repo("adddel")
    r = ScenarioGen(root).generate("adddel", count=3, seed=3003)
    say("  生成器自报 allVerified = %s" % r["allVerified"])
    allok = r["allVerified"]
    for sc in r["scenarios"]:
        say("  --- %s  base=%s" % (sc["id"], sc["base"]))
        say("      A: %s" % sc["aEdit"])
        say("      B: %s" % sc["bEdit"])
        allok &= independent_verify(root, sc, True)
    return record("adddel ×3 真合并", allok)


def g_conflict():
    hdr("组 4  conflict ×1 —— 同一文件同一行,必须真冲突")
    root = fresh_repo("conflict")
    r = ScenarioGen(root).generate("conflict", count=1, seed=4004)
    sc = r["scenarios"][0]
    say("  生成器自报 verified = %s" % sc["verified"])
    ok = independent_verify(root, sc, False) and sc["verified"]
    say("  -> %s" % ("对照组按预期冲突" if ok else "对照组未按预期冲突"))
    return record("conflict ×1 真冲突", ok)


def g_determinism_same():
    hdr("组 5  同一 seed 跑两次 -> 产出逐字节相同")
    ra, rb = fresh_repo("det_a"), fresh_repo("det_b")
    ScenarioGen(ra).generate("mergeable", count=3, seed=777)
    ScenarioGen(rb).generate("mergeable", count=3, seed=777)
    sa, sb = snapshot(ra), snapshot(rb)
    # 两个仓的 README 都是自测写的固定内容,只比生成器造的文件
    fa = {k: v for k, v in sa.items() if k.startswith(PREFIX)}
    fb = {k: v for k, v in sb.items() if k.startswith(PREFIX)}
    say("  A 仓生成文件 %d 个, B 仓 %d 个" % (len(fa), len(fb)))
    same_names = sorted(fa) == sorted(fb)
    say("  文件名集合一致: %s" % same_names)
    diffs = []
    for k in sorted(fa):
        if k not in fb or fa[k] != fb[k]:
            diffs.append(k)
    say("  逐字节不同的文件: %s" % (diffs if diffs else "(无)"))
    for k in sorted(fa)[:1]:
        say("  样例 %s  sha256=%s  %d 字节  %d 行"
            % (k, __import__("hashlib").sha256(fa[k]).hexdigest()[:16],
               len(fa[k]), fa[k].count(b"\n")))
    ok = same_names and not diffs and len(fa) > 0
    return record("同 seed 逐字节确定性", ok)


def g_determinism_diff():
    hdr("组 6  不同 seed 跑两次 -> 产出不同")
    ra, rb = fresh_repo("seed_a"), fresh_repo("seed_b")
    ScenarioGen(ra).generate("mergeable", count=3, seed=111)
    ScenarioGen(rb).generate("mergeable", count=3, seed=999)
    fa = {k: v for k, v in snapshot(ra).items() if k.startswith(PREFIX)}
    fb = {k: v for k, v in snapshot(rb).items() if k.startswith(PREFIX)}
    diffs = [k for k in sorted(fa) if k in fb and fa[k] != fb[k]]
    say("  共 %d 个文件,其中内容不同 %d 个" % (len(fa), len(diffs)))
    for k in diffs[:3]:
        la, lb = fa[k].split(b"\n"), fb[k].split(b"\n")
        first = next((i for i in range(min(len(la), len(lb))) if la[i] != lb[i]), None)
        say("    %s 首个不同行 = %s" % (k, first))
        if first is not None:
            say("      seed111: %s" % la[first].decode("utf-8", "replace")[:78])
            say("      seed999: %s" % lb[first].decode("utf-8", "replace")[:78])
    ok = len(diffs) == len(fa) and len(fa) > 0
    return record("不同 seed 产出不同", ok)


def g_dirty_guard():
    hdr("组 7  有未提交改动的仓 -> 拒绝生成并报错")
    root = fresh_repo("dirty")
    say("  先制造一处未提交改动...")
    with open(root / "README.md", "a", encoding="utf-8", newline="\n") as f:
        f.write("别人的未提交改动\n")
    (root / "untracked_note.txt").write_text("未跟踪文件\n", encoding="utf-8")
    st = git(root, "status", "--porcelain").stdout.strip()
    say("  git status --porcelain:")
    for line in st.splitlines():
        say("      " + line)
    try:
        ScenarioGen(root).generate("mergeable", count=1, seed=1)
        say("  !! 生成器没有拒绝 <<< 护栏失效")
        return record("脏工作区拒绝生成", False)
    except GenError as e:
        say("  已拒绝,报错如下:")
        for line in str(e).splitlines():
            say("      " + line)
    # 确认拒绝之后仓没被动过
    after = {k: v for k, v in snapshot(root).items() if k.startswith(PREFIX)}
    say("  拒绝后新增的 %s 文件: %s" % (PREFIX, sorted(after) if after else "(无)"))
    return record("脏工作区拒绝生成", not after)


def g_prefix_guard():
    hdr("组 8  --clean 只删自己造的东西,不碰其他文件")
    root = fresh_repo("clean")
    with open(root / "keep_me.txt", "w", encoding="utf-8", newline="\n") as f:
        f.write("这是用户的文件,不许动\n")
    (root / "userdir").mkdir()
    (root / "userdir" / "notes.txt").write_text("用户的笔记\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "user files")
    before = snapshot(root)
    say("  生成前仓里的文件: %s" % sorted(before))
    r = ScenarioGen(root).generate("all", count=2, seed=555)
    say("  生成后分支数 = %d, 场景数 = %d" % (len(r["branches"]), len(r["scenarios"])))
    gen_files = sorted(k for k in snapshot(root) if k.startswith(PREFIX))
    say("  生成器造的文件(%d 个):" % len(gen_files))
    for k in gen_files:
        say("      " + k)
    nongen = [k for k in gen_files if not k.startswith(PREFIX)]
    say("  不合规的文件: %s" % (nongen if nongen else "(无)"))

    out = ScenarioGen(root).clean()
    say("  clean() 分支删除数 = %d, 文件删除数 = %d"
        % (len(out["removed"]["branches"]), len(out["removed"]["files"])))
    say("  clean() 提交 sha = %s" % out["removed"]["commit"])
    say("  clean() worktree_clean = %s" % out["worktree_clean"])
    say("  删除的文件:")
    for f in out["removed"]["files"]:
        say("      " + f)
    after = snapshot(root)
    say("  clean 后仓里的文件: %s" % sorted(after))
    say("  原文件是否全部存活: %s" % sorted(set(after) & set(before)))
    st = git(root, "status", "--porcelain").stdout.strip()
    say("  clean 后 git status --porcelain: %s" % (st if st else "(空,干净)"))
    ok = (not nongen
          and sorted(after) == sorted(before)
          and st == ""
          and len(out["removed"]["files"]) == len(gen_files)
          and len(out["removed"]["branches"]) == len(r["branches"]))
    return record("--clean 只删自己的东西且仓回到干净", ok)


def g_server_bridge():
    """组 9:server.py 的转发路径。直接调 Repo.generate(不起 HTTP、不开窗口),
    验证接线通了,而且返回给前端的仍是旧形状 branches=[{file,branches,expect}]。"""
    hdr("组 9  server.py 转发到 gen.py,返回形状兼容旧前端")
    root = fresh_repo("bridge")
    import server                      # 只 import,不跑 main()
    say("  已 import server 模块(未启动 HTTP 服务、未开任何窗口)")
    repo = server.Repo(root)
    out = repo.generate("mergeable", 2, 4242)
    say("  Repo.generate('mergeable', 2, 4242) 返回字段: %s" % sorted(out))
    say("  ok=%s allVerified=%s 场景数=%d" % (out["ok"], out["allVerified"],
                                              len(out["scenarios"])))
    say("  旧形状 branches[0] = %s" % out["branches"][0])
    say("  note = %s" % out["note"])
    shape_ok = (isinstance(out["branches"], list) and len(out["branches"]) == 2
                and all(set(("file", "branches", "expect")) <= set(b)
                        for b in out["branches"])
                and all(len(b["branches"]) == 2 for b in out["branches"]))
    say("  旧形状兼容 = %s" % shape_ok)
    say("  分支名样例: %s" % [b["branches"] for b in out["branches"]])
    # 转发出来的分支必须同样经得起独立复核
    verified = True
    for sc in out["scenarios"]:
        verified &= independent_verify(root, sc, sc["kind"] != "conflict")
    say("  独立复核全部通过 = %s" % verified)
    # clean 转发也要能用
    c = repo.clean_generated()
    say("  Repo.clean_generated() 分支=%d 文件=%d worktree_clean=%s"
        % (len(c["removed"]["branches"]), len(c["removed"]["files"]),
           c["worktree_clean"]))
    return record("server.py 转发接线", shape_ok and verified
                  and c["worktree_clean"] is True)


def main():
    if TMP.exists():
        rmtree_force(TMP)
    TMP.mkdir(parents=True, exist_ok=True)
    say("临时仓根目录: %s" % TMP)
    say("(只在这里跑 git;真实仓库全程未被打开)")

    for fn in (g_mergeable, g_unrelated, g_adddel, g_conflict,
               g_determinism_same, g_determinism_diff, g_dirty_guard,
               g_prefix_guard, g_server_bridge):
        try:
            fn()
        except Exception as e:
            say("  !! %s 抛异常: %s: %s" % (fn.__name__, type(e).__name__, e))
            record(fn.__name__, False)

    hdr("汇总")
    for name, ok, _ in results:
        say("  [%s] %s" % (PASS if ok else FAIL, name))
    bad = [n for n, ok, _ in results if not ok]
    say()
    say("  %d / %d 组通过" % (len(results) - len(bad), len(results)))
    if bad:
        say("  未通过: %s" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
