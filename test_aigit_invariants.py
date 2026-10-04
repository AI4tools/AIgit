#!/usr/bin/env python3
"""aigit 护栏不变量测试 —— 专测「守卫被关掉时必须有人喊」。

这个文件是 2026-10-03 变异测试**实测**逼出来的: 全套 15 条变异体里有 2 条
SURVIVED, 判据很硬 —— **变异体确实改变了可观测行为, 却没有任何测试察觉**。
那 2 条都是把一个 `if ...: raise` 直接改成 `if False:`, 于是护栏形同虚设:

  S-discard-pin-plain-off  server.py `_assert_pinned_plain`
      `if p.reparse(): raise GitError` -> `if False:`
      后果: 父目录被偷换成目录联接(junction)时, 本该拒绝的写操作不再拒绝。

  G-assert-prefix-off      gen.py `_assert_prefix`
      `if not rel.startswith(PREFIX): raise GenError` -> `if False:`
      后果: 不带 `aigit_gen_` 前缀的路径不再被拒, 演练仓可以直接写用户文件。

风格照抄 test_conflicts.py: 手写断言 + TAP 输出, 不引 pytest ——
这工具要在别人机器上直接跑, `pip install` 本身就是失败原因。

用法:
    python test_aigit_invariants.py            # 全跑
    python test_aigit_invariants.py -v         # 连断言细节一起打
    python test_aigit_invariants.py -k prefix  # 只跑名字含 prefix 的

**为什么这两条能算数、正向用例也必须存在** ——
只写「应该抛异常」的用例有个致命漏洞: 夹具没搭起来、import 失败、
前置条件本身就坏了, 全都会「抛异常」, 于是测试**因为坏掉而变绿**。
所以每条守卫都必须配一条**反向对照**: 前置条件摆正了但不该抛的时候,
必须**不**抛。两条一起才说明「它是因为该抛而抛」, 不是「它碰巧抛了」。
这是 test_conflicts.py 里 Skip 类存在的同一个道理: 测不了 != 测过了。

纪律(与 test_conflicts.py 一致):
  * 不碰 D:\\poject\\ 下任何真实仓库; 仓库建在 %TEMP%\\aigit-invariants\\ 下。
  * 只**读** server.py / gen.py, 不修改它们 —— 测出问题只记录, 不动代码。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SANDBOX = Path(tempfile.gettempdir()) / "aigit-invariants"
ENV = {**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
       "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no", "LC_ALL": "C"}


# ---------------------------------------------------------------- 断言

class Fail(Exception):
    pass


class Skip(Exception):
    """测不了 —— 必须说清为什么, 不能默默算通过。"""


def ok(cond, what):
    if not cond:
        raise Fail(what)


def raises(exc, fn, what):
    """要求 fn() 抛 exc。**没抛就是 Fail**, 不许「差不多就行」。"""
    try:
        fn()
    except exc as e:
        return str(e)
    except Exception as e:
        raise Fail("%s: 期望 %s,实际抛了 %s: %s"
                   % (what, exc.__name__, type(e).__name__, e))
    raise Fail("%s: 期望抛 %s,实际**没抛** —— 守卫已经失效了"
               % (what, exc.__name__))


def not_raises(exc, fn, what):
    """反向对照: 不该抛的时候必须不抛(夹具摆正了, 行为就该是放行)。"""
    try:
        fn()
    except exc as e:
        raise Fail("%s: 不该抛 %s 却抛了: %s —— 前置条件可能坏了, "
                   "这条用例的绿灯不算数" % (what, exc.__name__, e))
    except Exception as e:
        raise Fail("%s: 不该抛任何东西,却抛了 %s: %s"
                   % (what, type(e).__name__, e))


# ---------------------------------------------------------------- git / 仓库

def g(root, *a, **kw):
    return subprocess.run(["git", "-C", str(root)] + list(a),
                          capture_output=True, encoding="utf-8",
                          errors="replace", env=ENV, **kw)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(text.encode("utf-8"))
    return p


def commit_all(root, msg):
    g(root, "add", "-A")
    g(root, "commit", "-qm", msg)


def mk(name):
    d = SANDBOX / name
    rmtree(d)                      # 有残留就报, 不静默跳过
    d.mkdir(parents=True)
    g(d, "init", "-q", "-b", "main")
    g(d, "config", "user.email", "t@t")
    g(d, "config", "user.name", "t")
    g(d, "config", "commit.gpgsign", "false")
    g(d, "config", "core.autocrlf", "false")
    write(d, "README.md", "# %s\n" % name)
    commit_all(d, "init")
    return d


def rmtree(p):
    """删目录树, **junction 安全**。

    为什么不用 shutil.rmtree: 它跟进联接(WinError 32 / 删除失败), 而且
    `ignore_errors=True` 会把失败**静默吞掉** —— 结果上一轮留下的目录还在,
    下一轮 mkdir 直接 FileExistsError。第一版就踩了这个, 症状是
    「用例失败但报错跟断言毫无关系」。**失败必须响, 不能假装删掉了。**

    `cmd rmdir /s /q` 会把联接本身删掉且**不跟进目标**, 这是本仓探针
    (mutation_runner.PROBES)里已经在用的写法, 行为已知。
    """
    p = Path(p)
    if not os.path.lexists(str(p)):
        return
    r = subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)],
                       capture_output=True, timeout=120)
    if os.path.lexists(str(p)):
        raise RuntimeError(
            "删不掉 %s (rc=%s, stderr=%r) —— 不许当成「已清理」继续往下跑, "
            "下一轮会被残留目录绊倒"
            % (p, r.returncode, (r.stderr or b"")[:200]))


def mk_junction(link, target):
    """造**目录联接**。`mklink /J` 不需要 SeCreateSymbolicLinkPrivilege ——
    本机 `whoami /priv` 里没有那个特权(见 test_aigit_extra.py 的诚实 SKIP),
    所以 symlink 那条路在本机造不出来, junction 是唯一可用的等价物。
    """
    if os.path.lexists(str(link)):
        try:
            os.rmdir(str(link))
        except OSError:
            return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, timeout=30)
    return os.path.lexists(str(link)) and os.path.isdir(str(link)) \
        and r.returncode == 0


# ================================================================= 用例
#
# 每条守卫 = 1 条「必须拒绝」+ 1 条「反向对照: 摆正了必须放行」。
# 反向对照不是凑数 —— 它是防止「因为夹具坏了所以变绿」的唯一手段。

def t_pin_plain_rejects_junction_parent(v):
    """S-discard-pin-plain-off 的对照: 路径链上有 junction 时**必须**拒绝。

    判的行为(一句话): 写文件前如果目标路径的祖先链上挂着目录联接, 引擎必须
    抛 GitError 拒绝动手 —— 否则攻击者把父目录换成联接, 写操作就越出仓库,
    落到仓外文件上。

    握的是**联接目录项本身**(with_self=True), 不是「联接下面的某个文件」。
    这一点是踩出来的: 第一版我 pin 了 `esc/new.txt`, 结果 `_pin_dir` 先
    因文件不存在抛 WinError 2, GitError 是**它**抛的, 守卫根本没被走到 ——
    测试因为坏掉而变绿。反向对照立刻把这件事拆穿了。
    """
    import server
    root = mk("pin_plain_reject")
    outside = root.parent / (root.name + "_outside")
    rmtree(outside)
    outside.mkdir(parents=True)
    write(outside, "victim.txt", "SECRET\n")
    (root / "sub").mkdir()
    if not mk_junction(root / "sub" / "esc", outside):
        raise Skip("造不出目录联接(mklink /J 失败),测不了")

    r = server.Repo(root)
    junction = root / "sub" / "esc"
    ok(os.path.lexists(str(junction)),
       "联接没造出来,夹具坏了(那样这条用例的绿灯不算数)")

    def attempt():
        with r._pinned([(junction, True)]) as pins:
            return r._assert_pinned_plain(pins)

    msg = raises(server.GitError, attempt,
                 "路径链上有 junction 时 _assert_pinned_plain 必须拒绝")
    ok("联接" in msg or "符号链接" in msg,
       "拒绝信息应当点名是联接/符号链接, 实际: %s" % msg)
    v("拒绝信息: %s" % msg)


def t_pin_plain_allows_plain_path(v):
    """反向对照: 路径链上**没有**联接时必须放行。

    不放行的话, 上一条就可能只是「见谁都抛」—— 那种绿灯抓不到任何东西。
    """
    import server
    root = mk("pin_plain_allow")
    (root / "sub").mkdir()
    (root / "sub" / "plain").mkdir()
    r = server.Repo(root)
    plain = root / "sub" / "plain"

    def attempt():
        with r._pinned([(plain, True)]) as pins:
            return r._assert_pinned_plain(pins)

    not_raises(server.GitError, attempt,
               "普通子目录链上没有联接, 必须放行")
    v("放行成功, 说明夹具确实摆正了(不是靠「反正会抛」骗过上一条)")


def t_gen_prefix_rejects_unprefixed(v):
    """G-assert-prefix-off 的对照: 不带 PREFIX 的路径**必须**拒绝。

    判的行为(一句话): 生成器所有落盘/删除路径都必须带 `aigit_gen_` 前缀,
    否则演练仓会把用户真实文件当靶子写坏 —— 这是「绝不碰用户文件」的唯一保证。
    """
    import gen
    obj = gen.ScenarioGen.__new__(gen.ScenarioGen)
    for rel in ("evil.txt", "notes/user.md", "aigit_genX_looksalike/f.txt"):
        msg = raises(gen.GenError, lambda r=rel: obj._assert_prefix(r),
                     "无前缀路径 %r 必须被拒" % rel)
        v("拒绝 %-30r -> %s" % (rel, msg))


def t_gen_prefix_allows_prefixed(v):
    """反向对照: 带 PREFIX 的正常路径必须放行(证明上一条不是见谁都抛)。"""
    import gen
    obj = gen.ScenarioGen.__new__(gen.ScenarioGen)
    for rel in ("aigit_gen_s1/mergeable/0/a.txt", "aigit_gen_s1/clash/1/b.md"):
        got = obj._assert_prefix(rel)
        ok(got.startswith(gen.PREFIX),
           "放行后返回值应保留前缀, 实际 %r" % got)
        v("放行 %-40r -> %r" % (rel, got))


TESTS = [
    ("pin-plain-junction-parent-rejected",
     t_pin_plain_rejects_junction_parent),
    ("pin-plain-plain-path-allowed",
     t_pin_plain_allows_plain_path),
    ("gen-prefix-unprefixed-rejected",
     t_gen_prefix_rejects_unprefixed),
    ("gen-prefix-prefixed-allowed",
     t_gen_prefix_allows_prefixed),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 护栏不变量测试")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印断言细节")
    ap.add_argument("-k", default=None, help="只跑名字含此串的用例")
    a = ap.parse_args(argv)

    SANDBOX.mkdir(parents=True, exist_ok=True)

    picked = [(n, f) for n, f in TESTS if not a.k or a.k in n]
    print("1..%d" % len(picked))

    def v(msg):
        if a.verbose:
            print("    %s" % msg)

    failed = 0
    skipped = []
    for i, (name, fn) in enumerate(picked, 1):
        try:
            fn(v)
        except Skip as e:
            # 测不了 != 测过了。# SKIP 不算 fail,但必须单列 ——
            # 免得「造不出前置条件」被读成「这条测过了、没问题」。
            skipped.append((i, name, str(e)))
            print("ok %d - %s # SKIP %s" % (i, name, e))
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

    for i, name, why in skipped:
        print("# SKIPPED %d - %s: %s" % (i, name, why))
    if failed:
        print("# failed %d among %d test(s) (skipped %d)"
              % (failed, len(picked), len(skipped)))
    else:
        print("# passed %d of %d test(s), skipped %d"
              % (len(picked) - len(skipped), len(picked), len(skipped)))
    rmtree(SANDBOX)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())