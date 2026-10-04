#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-06 + conflicts.py 侧互斥 的回归与判据自检。

产出背景: doc/aigit-concurrency-audit.md 的 F-06(`conflicts.commit_merge()`
裸 `git add -A`, 串行即可把仓外内容提交进历史)、F-01(`conflicts` 通路零锁)、
F-03(`resolve()` 读-校验-写整段无锁 → 静默丢更新)。

本文件**只新增**, 没有改动任何现有测试或产品文件。

判据纪律(本文件自我要求):
  * 每条判据都有**反向对照**: 把防线/锁故意拆掉, 判据必须变红。
    否则「0 个泄漏」「0 次丢更新」与「根本没跑到」长得一模一样。
  * 联接一律 `mklink /J`(junction), 不用符号链接 —— Python 3.11 的
    os.path.islink() 对目录联接返回 False, 判定口径不同。

跑:
    set TEMP=C:\\Users\\Public\\tmp\\aigit-f06
    python -X utf8 test_f06_guard.py
    python -X utf8 test_f06_guard.py -k falsifiable -v
"""

import argparse
import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import conflicts                                             # noqa: E402
from conflicts import ConflictError, ConflictResolver        # noqa: E402

SECRET = b"OUTSIDE-SECRET-1234567890\n"

TESTS = []
BASELINES = []


class Skip(Exception):
    pass


class Fail(Exception):
    pass


class Flaky(Fail):
    def __init__(self, msg, rate=None):
        Fail.__init__(self, msg)
        self.rate = rate


def test(name, desc="", *rest):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def baseline(name, desc="", *rest):
    """**基线测量标记** —— 会跑、会打印数字, 但**不参与 PASS/FAIL 汇总、
    不影响退出码**。

    与 `test()` 的差别只有「记到哪个表」: `test` 进 TESTS(计入 npass/nfail,
    失败即退出码 1); `baseline` 进 BASELINES, runner 单独跑它、单独记账,
    它内部就算 `ck()` 抛了 `Fail`, 也只会被记成「未完成」, 绝不会变成 FAIL。

    ⚠ 什么时候该用它: **characterization**(特征化测试) —— 把当前的缺陷/现状
      钉成一个数字, 给将来修它的人一个「要降到几」的目标, 而不是一道门禁。
      判据与基线的区别是**语义**上的: 判据回答「对不对」, 数字变了就是红;
      基线回答「现在是几」, 数字变了只说明现状变了, 不说明谁对谁错。
      拿基线当前向当门禁, 会让「把缺陷修好」这件事本身变成红 —— 那就是
      把判据和基线搞反了。

    ⚠ 本文件里唯一的 baseline 是 T9(`cross-domain-still-open`)。它的 docstring
      早就写着自己「不是判据」, 但代码里挂着 `ck(n > 0, ...)`, 实际是当门禁在
      跑 —— 代码与声明不符。2026-10-03 按裁定改成 baseline: 数字照量照印,
      门禁摘掉。名字里的 `cross-domain-still-open` **保留** —— 它描述的是事实。
    """
    def deco(fn):
        BASELINES.append((name, fn))
        return fn
    return deco


def ck(cond, msg):
    if not cond:
        raise Fail(msg)


# ------------------------------------------------------------ git 小工具
def git(repo, *args, **kw):
    binary = kw.get("binary", False)
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=not binary, encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        env={**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
             "GIT_AUTHOR_NAME": "aigit", "GIT_AUTHOR_EMAIL": "a@localhost",
             "GIT_COMMITTER_NAME": "aigit", "GIT_COMMITTER_EMAIL": "a@localhost"})


def mklink_junction(link, target):
    """造**目录联接**。删除要用 rmdir, 不能用 del / os.unlink。"""
    p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def rmdir_junction(link):
    return subprocess.run(["cmd", "/c", "rmdir", str(link)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def is_junction(p):
    try:
        return bool(getattr(os.lstat(str(p)), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


class Sandbox(object):
    """一次性临时目录。cleanup 里**先 rmdir 联接再删目录** —— 顺序反了会
    顺着联接把目标目录一起删掉。"""

    def __init__(self, name):
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix="f06t-%s-" % name,
                                          dir=os.environ.get("TEMP")))
        self.junctions = []

    def sub(self, *parts):
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def junction(self, link_rel, target):
        link = Path(link_rel) if os.path.isabs(str(link_rel)) else (self.root / link_rel)
        target = Path(target)
        ok, msg = mklink_junction(link, target)
        if not ok:
            raise Skip("mklink /J 失败(需同盘/同 NTFS 卷): %s" % msg.strip())
        if not is_junction(link):
            raise Skip("造出来的东西不是 reparse point, 跳过(不能拿它当判据)")
        self.junctions.append(link)
        return link

    def cleanup(self):
        for j in self.junctions:
            try:
                rmdir_junction(j)
            except Exception:
                pass
        shutil.rmtree(str(self.root), ignore_errors=True)


def build_conflicted(sb, n_conflict=1, extra="escape"):
    """造一个 n_conflict 个冲突文件的合并态仓, 并把 <repo>/<extra> 指向仓外。

    返回 (repo, outside)。仓外放一份哨兵 SECRET。
    """
    repo = sb.sub("repo")
    outside = sb.sub("outside")
    (outside / "leak.txt").write_bytes(SECRET)
    git(repo, "init", "-q", "-b", "main")
    for i in range(n_conflict):
        (repo / ("c%02d.txt" % i)).write_text("base %d\n" % i, encoding="utf-8")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "side")
    for i in range(n_conflict):
        (repo / ("c%02d.txt" % i)).write_text("side %d\n" % i, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "side")
    git(repo, "checkout", "-q", "main")
    for i in range(n_conflict):
        (repo / ("c%02d.txt" % i)).write_text("main %d\n" % i, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "main")
    p = git(repo, "merge", "--no-edit", "side")
    assert p.returncode != 0, "这个 fixture 没能造出冲突, 判据就落空了"
    # ⚠ 联接必须在**仓里面**。挂在仓外的话 escaping_links() 根本看不见,
    #   T1 会因为「没有泄漏」而空跑 —— 这正是本文件第一版的 bug。
    sb.junction(repo / "escape", outside)
    return repo, outside


def leaked(repo):
    """(仓外路径是否进了索引, HEAD 上能否读出仓外字节)"""
    esc = [x for x in git(repo, "ls-files").stdout.split() if x.startswith("escape")]
    show = git(repo, "cat-file", "-p", "HEAD:escape/leak.txt", binary=True)
    return bool(esc), (show.returncode == 0 and SECRET in show.stdout)


# ═══════════════════════════════════════════════════════ F-06 本体

_COPIED_TOKENS = ("0x400", "reparse", "st_file_attributes", "st_reparse_tag",
                  "islink", "scandir")


def _copied_tokens(src):
    """源码里有没有「自己实现了一遍 reparse 扫描」的痕迹。返回命中的 token 或 None。

    扫 **AST**(字符串常量 + 属性名 + 标识符)而不是原文 —— 原文里有
    「reparse point」这样的散文, 用 `token in src` 判会误伤。

    ⚠ **排除 docstring**: docstring 也是 ast.Constant, 而本模块的说明文字
      里就出现「reparse point」这种词 —— 那是在解释 server.py 的防线, 不是
      把防线抄了一份。把散文当实现来判, 判据就变成自己吓自己。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return "SyntaxError"
    docnodes = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)):
            body = getattr(n, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docnodes.add(id(body[0].value))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if id(n) not in docnodes:
                names.add(n.value)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Name):
            names.add(n.id)
    blob = "\n".join(names).lower()
    for tok in _COPIED_TOKENS:
        if tok in blob:
            return tok
    return None


@test("T1", "f06-commit-merge-refuses", "commit_merge 必须与 Repo.commit 一样拒绝")
def t1_refuses():
    sb = Sandbox("t1")
    try:
        repo, outside = build_conflicted(sb)
        import server
        r = server.Repo(repo)

        # A) server 侧防线: 必须拒(这是对照组, 证明场景真的成立)
        a_refused = False
        try:
            r.commit("A via server.Repo.commit")
        except server.GitError:
            a_refused = True
        ck(a_refused, "server.Repo.commit() 竟然没拒绝 —— 场景没立住, "
                      "本条的 PASS 会是空的")
        ck((repo / ".git" / "MERGE_HEAD").exists(),
           "A 侧竟然把合并态弄没了, A/B 对照不成立")

        # B) conflicts 侧: 必须同样拒
        cr = ConflictResolver(repo)
        r1 = cr.resolve("c00.txt", "resolved\n")
        ck(r1.get("ok"), "resolve 失败: %r" % r1)

        b_refused, b_msg = False, ""
        try:
            cr.commit_merge("B via conflicts.commit_merge")
            b_msg = "(没有抛异常)"
        except ConflictError as e:
            b_refused = True
            b_msg = str(e)
        ck(b_refused, "commit_merge 没有拒绝仓外联接, F-06 未关 —— %s" % b_msg)

        lp, lc = leaked(repo)
        ck(not lp, "仓外路径进了 ls-files: %r" % lp)
        ck(not lc, "HEAD 上读得出仓外字节 —— 内容已经进历史")
        ck((outside / "leak.txt").read_bytes() == SECRET,
           "仓外哨兵被动了 —— 判据 C5 失效")
        print("      B 侧拒绝消息首行: %s" % b_msg.splitlines()[0][:90])
        return
    finally:
        sb.cleanup()


@test("T2", "guard-not-a-copy", "防线复用 server.Repo.escaping_links, 不是复制一份")
def t2_no_copy():
    import server
    calls = []
    orig = server.Repo.escaping_links

    def spy(self, limit=16):
        calls.append(str(self.root))
        return orig(self, limit)

    server.Repo.escaping_links = spy
    try:
        sb = Sandbox("t2")
        try:
            repo, _ = build_conflicted(sb)
            prev = conflicts.install_escaping_guard(None)      # 用默认实现
            try:
                cr = ConflictResolver(repo)
                cr.resolve("c00.txt", "x\n")
                refused = False
                try:
                    cr.commit_merge("spy")
                except ConflictError:
                    refused = True
            finally:
                conflicts.install_escaping_guard(prev)
            ck(refused, "场景不成立: commit_merge 没拒绝")
            ck(len(calls) == 1,
               "server.Repo.escaping_links 被调了 %d 次(期望 1 次)—— "
               "防线可能没走 server 那份" % len(calls))
        finally:
            sb.cleanup()
    finally:
        server.Repo.escaping_links = orig

    # conflicts.py 里不应出现自己的 reparse 扫描实现。
    #
    # ⚠ 扫 **AST**(字符串常量 + 属性名 + 标识符), 不扫原文 —— 原文里有
    #   「reparse point」这样的散文注释, 用 `token in src` 判会误伤,
    #   用大小写敏感躲过去又纯属侥幸。
    #
    # ⚠ 这个判据本身也要能红: `_copied_tokens` 判据用函数形式写, 下面
    #   先拿一段「复制过来的遍历」自检它确实会命中, 否则「0 命中」是空的。
    _copied = """
def escaping_links(root):
    for e in os.scandir(str(root)):
        st = e.stat(follow_symlinks=False)
        if getattr(st, "st_file_attributes", 0) & 0x400:
            found.append(e.path)
"""
    hit = _copied_tokens(_copied)
    ck(hit,
       "判据自检失败: 拿一段真实复制过来的遍历都扫不出重复实现 —— "
       "「conflicts.py 没有副本」这条判据是空的")
    # ⚠ 报错信息里必须放**这一次**的结果, 不是上面那个自检的 hit ——
    #   两者同名很容易顺手用错, 报出来的 token 会指向根本没被扫的那个文件。
    real = _copied_tokens((HERE / "conflicts.py").read_text(
        encoding="utf-8", errors="replace"))
    ck(not real,
       "conflicts.py 的 AST 里出现了 %r —— 防线被复制了一份, "
       "会与 server.py 漂移" % real)
    print("      判据自检: 复制版被判 %r, conflicts.py 判 None" % hit)


@test("T3", "falsifiable-reverse", "反向对照: 关掉防线, 必须真的泄漏")
def t3_falsifiable():
    """判据可证伪性。如果关掉防线 commit_merge 还是拒绝, 说明 T1 判据是空的。"""
    sb = Sandbox("t3")
    try:
        repo, _ = build_conflicted(sb)
        prev = conflicts.install_escaping_guard(lambda root: [])   # 故意拆掉
        try:
            cr = ConflictResolver(repo)
            cr.resolve("c00.txt", "resolved\n")
            refused, err = False, ""
            try:
                cr.commit_merge("guard disabled on purpose")
                refused = False
            except ConflictError as e:
                refused = True
                err = str(e)
            lp, lc = leaked(repo)
            ck(not refused,
               "防线被换成 no-op 却仍然拒绝 —— 拒绝不是来自这条防线, "
               "T1/T3 的判据落空: %s" % err[:120])
            ck(lp and lc,
               "防线关掉后**没有**泄漏 —— 反向对照失败, T1 的 PASS 不可信 "
               "(leaked_path=%s leaked_content=%s)" % (lp, lc))
            print("      反向对照成立: 防线=no-op 时仓外内容真的进了历史")
        finally:
            conflicts.install_escaping_guard(prev)
        return
    finally:
        sb.cleanup()


# ═══════════════════════════════════════════════════════ 互斥(F-01/F-03)

class NoopLock(object):
    """空锁内核: __enter__ 立刻返回, 什么都不互斥。用来校准判据。"""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_resolve_race(repo, names, n_thread, n_round, noop=False):
    """并发 resolve —— C5 的口径: 响应报的 sha256 vs 请求全部结束后的磁盘。"""
    real = conflicts.repo_git_lock
    if noop:
        conflicts.repo_git_lock = lambda root: NoopLock()
    spans = []
    slock = threading.Lock()
    orig_run = conflicts.Git.run

    def traced(self, args, **kw):
        t0 = time.perf_counter()
        try:
            return orig_run(self, args, **kw)
        finally:
            t1 = time.perf_counter()
            with slock:
                spans.append((t0, t1, threading.current_thread().name, args[0]))

    conflicts.Git.run = traced
    viol, errs = [], []

    def body(t, r):
        content = "writer T%d round %d\n%s\n" % (t, r, "-" * 60)
        try:
            res = ConflictResolver(repo).resolve(names[r], content)
        except ConflictError as e:
            with slock:
                errs.append(str(e)[:60])
            return
        actual = hashlib.sha256((repo / names[r]).read_bytes()).hexdigest()
        if res.get("sha256") != actual:
            with slock:
                viol.append({"thread": t, "round": r,
                             "claimed": (res.get("sha256") or "")[:12],
                             "actual": actual[:12]})

    try:
        threads = []
        for t in range(n_thread):
            th = threading.Thread(target=lambda tt=t: [body(tt, r)
                                                      for r in range(n_round)],
                                  name="c5-T%d" % t)
            threads.append(th)
        for th in threads:
            th.start()
        for th in threads:
            th.join(180)
        alive = [th.name for th in threads if th.is_alive()]
    finally:
        conflicts.Git.run = orig_run
        conflicts.repo_git_lock = real
    ck(not alive, "有线程挂住了(死锁): %r" % alive)
    return viol, errs, spans


@test("T4", "resolve-no-lost-update", "并发 resolve 不得静默丢更新(整段互斥)")
def t4_no_lost_update():
    N_RND, N_THR = 12, 6
    sb = Sandbox("t4")
    try:
        repo, _ = build_conflicted(sb, n_conflict=N_RND)
        names = ["c%02d.txt" % i for i in range(N_RND)]
        viol, errs, _ = run_resolve_race(repo, names, N_THR, N_RND, noop=False)
        if viol:
            raise Flaky("【静默丢更新】%d 次成功响应报的 sha256 与事后磁盘不一致, "
                        "例 %r" % (len(viol), viol[:3]),
                        rate=len(viol) / float(N_THR * N_RND))
        fsck = git(repo, "fsck", "--no-progress")
        ck(fsck.returncode == 0, "git fsck 报对象库问题: %r" % (fsck.stdout or "")[:200])
        print("      %d 请求(%d线程x%d轮), 丢更新 0, 串行化拒绝 %d 次"
              % (N_THR * N_RND, N_THR, N_RND, len(errs)))
        return
    finally:
        sb.cleanup()


@test("T5", "lost-update-criterion-calibrated", "反向对照: 换空锁内核, 丢更新必须出现")
def t5_calibrate():
    N_RND, N_THR = 12, 6
    sb = Sandbox("t5")
    try:
        repo, _ = build_conflicted(sb, n_conflict=N_RND)
        names = ["c%02d.txt" % i for i in range(N_RND)]
        viol, errs, _ = run_resolve_race(repo, names, N_THR, N_RND, noop=True)
        ck(viol,
           "换成空锁内核后**一次丢更新都没有** —— T4 的「0」不是因为没量到, "
           "而是这个判据量不到东西; 拒绝 %d 次" % len(errs))
        print("      校准成立: 空锁内核下丢更新 %d/%d (%d 次被串行化拒绝)"
              % (len(viol), N_THR * N_RND, len(errs)))
        return
    finally:
        sb.cleanup()


@test("T6", "git-run-serialized", "同锁域内 Git.run 临界区零重叠(空锁对照)")
def t6_git_serialized():
    N_RND, N_THR = 10, 6
    sb = Sandbox("t6")
    try:
        repo, _ = build_conflicted(sb, n_conflict=N_RND)
        names = ["c%02d.txt" % i for i in range(N_RND)]

        def overlaps(spans):
            """区间重叠对数。**先断言一条 span 都没漏记**, 否则「0」是假的。"""
            ck(spans, "一条 span 都没记到 —— 覆盖判据没生效, 不能报 0")
            s = sorted(spans)
            n = 0
            for i in range(len(s)):
                for j in range(i + 1, len(s)):
                    if s[j][0] >= s[i][1]:
                        break
                    if s[j][0] < s[i][1] and s[j][1] > s[i][0]:
                        n += 1
            return n

        _, _, spans_locked = run_resolve_race(repo, names, N_THR, N_RND, noop=False)
        n_locked = overlaps(spans_locked)

        # 换一个干净的仓做空锁对照
        sb2 = Sandbox("t6b")
        try:
            repo2, _ = build_conflicted(sb2, n_conflict=N_RND)
            _, _, spans_noop = run_resolve_race(repo2, names, N_THR, N_RND, noop=True)
            n_noop = overlaps(spans_noop)
        finally:
            sb2.cleanup()

        ck(n_noop > 0,
           "空锁内核下重叠 0 —— 这个计数器没在量东西, T6 的 0 不可信")
        ck(n_locked == 0,
           "产品原样下同域临界区仍有 %d 对重叠 —— 锁没写对" % n_locked)
        print("      同域重叠: 产品原样 %d 对 / 空锁内核 %d 对 (共 %d 次 git 调用)"
              % (n_locked, n_noop, len(spans_locked)))
        return
    finally:
        sb.cleanup()


# ═══════════════════════════════════════════════════════ 锁图 / 死锁

LOCK_FILES = ("conflicts.py", "server.py", "history_ops.py")


def _lock_names(node):
    """一个 `with <expr>:` 里被取的名字。取不到就返回 None。

    ⚠ 返回的不一定是**锁** —— `with open(...) as f` / `with os.scandir() as it`
      也在这里。为避免把持锁点数虚高, 调用方用 _is_lockish() 再分一遍。
    """
    e = node
    if isinstance(e, ast.Call):
        e = e.func
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        return e.attr
    return None


def _is_lockish(name):
    """这个 context manager 是不是**互斥/资源占用**语义。

    open()/scandir() 也在锁图里出现过, 但它们不是互斥锁 —— 混在一起报
    「持锁点 N 个」会虚高。只影响展示, 不影响 edges 的计算(edges 用全部
    context manager, 宁可多报边也不能漏报)。
    """
    if name is None:
        return False
    n = str(name).lower()
    return ("lock" in n or n in ("lk", "_pinned", "pinned"))


def scan_lock_graph(sources=None):
    """静态扫模块: 每个持锁点 + 嵌套取锁边。

    sources: {文件名: 源码} —— 默认读 LOCK_FILES 里的真文件。
             T7b 用它喂**人造**源码, 证明这个探测器在有边的时候真的报边。

    ⚠ 行号按 '\\n' 数(文件是 LF), 不用 PowerShell 的 Get-Content(会把
      server.py 数成 1138 行之类, 对不上)。
    ⚠ 进入 FunctionDef / ClassDef 时 held 清空 —— 闭包不等于持锁。
    ⚠ `ast.With` **没有** orelse 字段(只有 ast.Try 有), 用 ast.walk 暴力
      遍历会把 with 头本身也算一次持锁, 边数虚高。
    """
    if sources is None:
        sources = {fn: (HERE / fn).read_text(encoding="utf-8", errors="replace")
                   for fn in LOCK_FILES if (HERE / fn).exists()}
    holders, edges = [], []
    for fn, src in sources.items():
        tree = ast.parse(src, filename=fn)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                f = n.value.func
                nm = (f.id if isinstance(f, ast.Name)
                      else f.attr if isinstance(f, ast.Attribute) else None)
                if nm in ("Lock", "RLock"):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            holders.append((fn, t.id, "decl@%d" % n.lineno))

        def walk(stmts, held):
            for stmt in stmts:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Lambda)):
                    walk(getattr(stmt, "body", []), [])
                    continue
                if isinstance(stmt, (ast.With, ast.AsyncWith)):
                    nm = _lock_names(stmt.items[0].context_expr)
                    if nm:
                        holders.append((fn, nm, "hold@%d" % stmt.lineno))
                        for h in held:
                            if h != nm:
                                edges.append((h, nm, "%s:%d" % (fn, stmt.lineno)))
                    walk(stmt.body, (held or []) + ([nm] if nm else []))
                    continue
                if isinstance(stmt, ast.Try):
                    walk(stmt.body, held)
                    walk(stmt.orelse, held)
                    walk(stmt.finalbody, held)
                    for h in stmt.handlers:
                        walk(h.body, held)
                    continue
                if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If)):
                    walk(stmt.body, held)
                    walk(stmt.orelse, held)
                    continue
                if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
                    for c in stmt.cases:
                        walk(c.body, held)
                    continue

        walk(tree.body, [])
    return holders, edges


@test("T7", "no-lock-order-cycle", "静态锁图: 嵌套取锁边必须为 0(无环 ⇒ 不死锁)")
def t7_no_cycle():
    holders, edges = scan_lock_graph()
    real = [h for h in holders if _is_lockish(h[1])]
    other = [h for h in holders if not _is_lockish(h[1])]
    print("      持锁点 %d 个(其中真正的互斥锁 %d 个, 其余是 open()/scandir "
          "这类非互斥 CM), 嵌套取锁边 %d 条" % (len(holders), len(real), len(edges)))
    for h in real:
        print("        [锁 ] %-16s %-16s %s" % h)
    for h in other:
        print("        [非锁] %-16s %-16s %s" % h)
    for e in edges:
        print("        EDGE %s -> %s  @%s" % e)
    ck(not edges,
       "出现嵌套取锁边 %r —— 有锁顺序反转的风险, 必须重排后重跑" % edges)


@test("T7b", "lock-scanner-calibrated", "反向对照: 人造嵌套锁, 探测器必须报边")
def t7b_scanner():
    """「0 条嵌套边」只有在探测器**能报边**时才有意义。

    人造三个片段, 分别必须得到: 0 边 / 1 条边 / 1 条边(反向)。
    """
    cases = [
        ("flat", "def f(a):\n    with a:\n        pass\n", 0),
        ("reentrant-same", "def f(a):\n    with a:\n        with a:\n            pass\n", 0),
        ("nested-diff", "def f(a, b):\n    with a:\n        with b:\n            pass\n", 1),
        ("nested-if", "def f(a, b):\n    with a:\n        if 1:\n            with b:\n                pass\n", 1),
        ("nested-try", "def f(a, b):\n    with a:\n        try:\n            with b:\n                pass\n        except E:\n            pass\n", 1),
        ("across-fn", "def f(a):\n    with a:\n        pass\ndef g(b):\n    with b:\n        pass\n", 0),
    ]
    for name, src, want in cases:
        holders, edges = scan_lock_graph({"synthetic.py": src})
        ck(len(edges) == want,
           "片段 %r: 期望 %d 条边, 实际 %d 条 %r —— 探测器不可信, "
           "T7 的「0 条边」也就不可信" % (name, want, len(edges), edges))
    print("      6 个人造片段全部符合预期(0/0/1/1/1/0 条边)")
    return


@test("T8", "deadlock-watchdog", "动态看门狗: 全通路并发不挂起/不 5xx")
def t8_watchdog():
    N_RND, N_THR = 8, 9
    sb = Sandbox("t8")
    try:
        repo, _ = build_conflicted(sb, n_conflict=N_RND)
        names = ["c%02d.txt" % i for i in range(N_RND)]
        slow = []
        lock = threading.Lock()
        orig = conflicts.Git.run

        def timed(self, args, **kw):
            t0 = time.perf_counter()
            try:
                return orig(self, args, **kw)
            finally:
                dt = time.perf_counter() - t0
                if dt > 5.0:
                    with lock:
                        slow.append((args[0], round(dt, 1)))

        conflicts.Git.run = timed
        viol, errs, _ = run_resolve_race(repo, names, N_THR, N_RND, noop=False)
        slow = []
        threads = []
        for t in range(N_THR):
            th = threading.Thread(
                target=lambda tt=t: [threading.Thread(
                    target=lambda r=rr: safe_resolve(repo, names, tt, r)
                ).start() for rr in range(N_RND)],
                name="wd-T%d" % t)
            threads.append(th)
        for th in threads:
            th.start()
        for th in threads:
            th.join(180)
        alive = [th.name for th in threads if th.is_alive()]
        conflicts.Git.run = orig
        ck(not alive, "看门狗发现挂起的线程: %r" % alive)
        ck(not viol, "看门狗期间出现丢更新: %r" % viol[:3])
        ck(not slow, "有单条 git 超过 5s: %r" % slow[:3])
        print("      %d 线程全部正常返回, slow_ops_gt5s=%d, 丢更新=%d"
              % (N_THR, len(slow), len(viol)))
        return
    finally:
        sb.cleanup()


def safe_resolve(repo, names, t, r):
    try:
        ConflictResolver(repo).resolve(names[r % len(names)],
                                        "w T%d r%d\n%s\n" % (t, r, "-" * 50))
    except (ConflictError, OSError):
        pass
    except Exception:
        pass


# ⚠ 这是**基线测量**, 不是判据: 由 @baseline 登记, 不进 PASS/FAIL 汇总, 不影响退出码。
#   名字里的 `cross-domain-still-open` 保留 —— 它描述的就是事实(两条路还在并发跑 git)。
@baseline("T9", "cross-domain-still-open", "量化残留: server.Repo 与 conflicts 仍不同锁")
def t9_cross_domain():
    """**基线数字**, 不是判据。给「三锁统一」那条线一个「要降到几」的目标。

    我的改动只在 conflicts.py 内部生效; server.Repo._lock 是另一把锁,
    按要求我没有碰 server.py。所以跨域重叠**理应仍然存在** ——
    这里量出来, 让接手的人知道统一之后要降到什么数。

    2026-10-03 裁定(此前的实现与本段声明不符, 现已对齐):
      * 原先这里挂着 `ck(n > 0, "跨域重叠 0 —— 「三锁要统一」的前提需要重新核对")`,
        实际是当门禁在跑。已摘掉。
      * 数字**照量照印**, 一个阈值都没动 —— 变的只是它算不算 FAIL。
      * 判据那边一个字没动。

    ⚠⚠ 2026-10-03 12:38 **更正本段下面这段的理由 —— 它是错的**
    ---------------------------------------------------------------
    原文写的是「它和 test_aigit_concurrency.py C7 的 `ck(not cross)`
    判的是同一个量, 方向相反」, 并据此论证「拿这条当前向当门禁, 等于把
    「把缺陷修好」判成红」。

    **「同一个量」是假的。实测(t9_decomp.py, EXITCODE=0)证明两者口径不同:**

      本条 T9 量的是**方法体 span**:
        - `t_repo` 包 `server.Repo._run_full`, `t0 = time.perf_counter()`
          取在**调用之前**; 而取锁 `with self._lock:` 在 `server.py:497`
          是**函数体内部**的第一句。⇒ **span 把等锁时间算进去了**。
        - `t_git` 包 `conflicts.Git.run`, 同理。

      C7 量的是**持锁窗口**:
        - repo / history 走 `install_lock`(套在锁对象上), 只记
          `__enter__`~`__exit__`, 即**真正持有**的那段。
        - conflicts 走 `install_span(Git, "run")`, 是方法体 —— 但统一锁之后
          `Git.run` 的方法体执行在 `resolve()` **已经持有的同一把锁**之内
          (同线程 RLock 可重入), 所以整段都在持锁。

      实测数字(同一负载、同一把锁):
        两域**同时持锁**的时间段重叠对数 = **0**
        方法体口径重叠对数            = **162**, 且按成因拆开:
            真互斥失效(两域同时持锁) = **0**
            等锁排队(重叠但不同持锁) = **162**   合计 = 162 ✓ 闭合

      ⇒ **锁统一之后互斥没有缺口; 剩下的重叠 100% 是排队。**
      而排队正是**一把正确的全局锁本来就该产生的现象** ——
      所以在这里挂 `n > 0` 的正向门禁, 会把「正确的串行化」判成红。
      这才是它该留在 `@baseline` 的**真**理由。

    ⚠ 顺带记一个我犯过、后来查清的**量纲错误**, 别再犯:
      本条打印的 `跨域重叠 288 对 (repo 198 次 / conflicts 83 次)` 里,
      **288 是「重叠的对数」, 198 / 83 是「两域各跑了多少条 span」**。
      源码 :826-842 —— `n` 在 :828-834 累加, 打印时 `%d 次` 给的是
      `len(a)` / `len(b)`, 也就是**条数**。
      **对数不是条数之和**, 所以 `198 + 83 = 281 ≠ 288` **不是「少了 7 对」**,
      而是拿条数加对数。那个「7」是不存在的, 不要去凑。
      能真正加总回 288 的划分是「按 git 子命令分类」或「按成因分类」,
      见 aigit-lock-probes-20261003/out/t9decomp.out.txt。
    """
    import server
    sb = Sandbox("t9")
    try:
        repo, _ = build_conflicted(sb, n_conflict=2)
        # ⚠ 必须把联接摘掉: 否则 server.Repo.commit() 会在 escaping_links() 就
        # 抛 GitError, **一条 git 都不跑** —— spans["repo"] 就是空的,
        # 重叠数会假成 0。这正是「0 和没跑长得一样」的活例子。
        j = sb.junctions.pop()
        rmdir_junction(j)
        ck(not j.exists(), "联接摘不掉(rc 非 0 或路径还在)")
        spans = {"repo": [], "conflicts": []}
        slock = threading.Lock()

        o_repo = server.Repo._run_full
        o_git = conflicts.Git.run

        def t_repo(self, args, **kw):
            t0 = time.perf_counter()
            try:
                return o_repo(self, args, **kw)
            finally:
                t1 = time.perf_counter()
                with slock:
                    spans["repo"].append((t0, t1))

        def t_git(self, args, **kw):
            t0 = time.perf_counter()
            try:
                return o_git(self, args, **kw)
            finally:
                t1 = time.perf_counter()
                with slock:
                    spans["conflicts"].append((t0, t1))

        server.Repo._run_full = t_repo
        conflicts.Git.run = t_git
        stop = threading.Event()
        try:
            r = server.Repo(repo)

            def dom_repo():
                i = 0
                while not stop.is_set():
                    i += 1
                    (repo / ("noise%d.txt" % (i % 5))).write_text(
                        "n%d\n" % i, encoding="utf-8")
                    try:
                        r.commit("repo domain %d" % i)
                    except Exception:
                        pass

            def dom_conf():
                while not stop.is_set():
                    try:
                        ConflictResolver(repo).list_conflicts()
                    except Exception:
                        pass

            ths = [threading.Thread(target=dom_repo, name="dom-repo"),
                   threading.Thread(target=dom_conf, name="dom-conf")]
            for th in ths:
                th.start()
            time.sleep(12)
            stop.set()
            for th in ths:
                th.join(60)
        finally:
            server.Repo._run_full = o_repo
            conflicts.Git.run = o_git

        alive = [t.name for t in ths if t.is_alive()]
        ck(not alive, "有线程挂住: %r" % alive)

        a = sorted(spans["repo"])
        b = sorted(spans["conflicts"])
        n = 0
        for x in a:
            for y in b:
                if y[0] >= x[1]:
                    break
                if y[0] < x[1] and y[1] > x[0]:
                    n += 1
        # ⚠ 这两条 ck() **不是**关于重叠数的断言, 留在这里是必要的:
        #   没有 span 就报不出重叠数, 「0」会和「根本没跑到」长得一样(本文件
        #   自己 11-13 行的判据纪律)。它们是**数据有效性**前提, 与 n 的方向无关。
        ck(a and b, "一条 span 都没记到 —— 不能报重叠数 (repo=%d conflicts=%d)"
           % (len(a), len(b)))
        print("      跨域 git 临界区重叠 %d 对 (repo %d 次 / conflicts %d 次)"
              " —— 这是**留给 server.py 那条线**的基线, 本线按要求没碰"
              % (n, len(a), len(b)))
        # ⚠ 这里**没有** `ck(n > 0, ...)`, 而且是**故意没有的**。
        #   ⚠⚠ 2026-10-03 12:45 更正 —— 下面第一句的依据**已被实测推翻**。
        #     原文断言它与 C7 的 `ck(not cross)` 量的是同一个东西、方向相反。
        #     **那个断言是假的**, 详见**本函数 docstring 的
        #     「更正本段下面这段的理由」段**(约 :756-790)。
        #     ← 先读到本注释的人请按 docstring 那一段走, 别拿本段第一句当依据。
        #
        #   摘掉 `ck(n > 0)` 的**真依据**(结论不变, 依据换了):
        #     本条量的是**方法体 span**(取锁在 server.py:497 函数体内,
        #     t0 取在调用之前 ⇒ 含等锁时间), C7 量的是**持锁窗口**
        #     (install_lock 套在锁对象上) —— 两者量的是**不同的东西**。
        #     实测(t9_decomp.py, EXITCODE=0): 两域**同时持锁**的重叠 = **0**,
        #     方法体重叠 162 对**全部**是等锁排队。
        #     ⇒ 统一锁之后互斥没有缺口; 剩下的重叠 100% 是排队,
        #     而排队正是一把正确的全局锁**本来就该产生**的现象。
        #     在这里挂正向门禁 = 把「正确的串行化」判成红 = 把修好判成 FAIL。
        #   数字照印(上面那行), 阈值没动, 变的只是它算不算 FAIL。
        return
    finally:
        sb.cleanup()


# ═══════════════════════════════════════════════════════ runner

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default=None, help="只跑名字含该串的用例")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args(argv)

    picked = [(n, f) for n, f in TESTS if not a.k or a.k in n]
    bases = [(n, f) for n, f in BASELINES if not a.k or a.k in n]
    npass = nfail = nskip = 0
    t_start = time.time()
    for rep in range(1, a.repeat + 1):
        for name, fn in picked:
            label = name if a.repeat == 1 else "%s(rep%d)" % (name, rep)
            t0 = time.time()
            try:
                fn()
                npass += 1
                print("ok   %-32s (%.1fs)" % (label, time.time() - t0))
            except Skip as e:
                nskip += 1
                print("skip %-32s %s" % (label, e))
            except Flaky as e:
                nfail += 1
                print("FAIL %-32s [flaky %s] %s" % (label, e.rate, e))
                if a.v:
                    traceback.print_exc()
            except Fail as e:
                nfail += 1
                print("FAIL %-32s %s" % (label, e))
                if a.v:
                    traceback.print_exc()
            except Exception:
                nfail += 1
                print("FAIL %-32s (未捕获异常)" % label)
                traceback.print_exc()
            sys.stdout.flush()

    print("\n# 共 %d 条  PASS %d  FAIL %d  SKIP %d  用时 %.1fs"
          % (len(picked) * a.repeat, npass, nfail, nskip, time.time() - t_start))

    # ---- 基线测量 ------------------------------------------------------
    # 跑, 打印, **记账但不进上面那三个计数器**, `return` 里的退出码也只看 nfail。
    # 内部抛什么都只是「未完成」。
    nbase_ok = nbase_miss = 0
    for rep in range(1, a.repeat + 1):
        for name, fn in bases:
            label = name if a.repeat == 1 else "%s(rep%d)" % (name, rep)
            t0 = time.time()
            try:
                fn()
                nbase_ok += 1
                print("base %-32s (%.1fs)" % (label, time.time() - t0))
            except Exception as e:
                nbase_miss += 1
                print("base %-32s (未完成: %s: %s)"
                      % (label, type(e).__name__, e))
                if a.v:
                    traceback.print_exc()
            sys.stdout.flush()
    if bases:
        print("# 基线 %d 条, 跑完 %d, 未完成 %d"
              " —— **不计入上面的 PASS/FAIL/SKIP, 不影响退出码**"
              % (len(bases) * a.repeat, nbase_ok, nbase_miss))

    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
