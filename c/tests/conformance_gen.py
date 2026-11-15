# -*- coding: utf-8 -*-
"""M-gen 一致性判据：C 版 `/api/gen` vs Python 版（gen.py::ScenarioGen + server 接线）。

判据分五层
==========
1. **语义（不依赖 Python）**：每种 kind 生成后，用**真 git** 独立复核
     mergeable → 两次 merge 都 rc=0、status 干净、U 列表为空
     unrelated → 同上，且两个分支改的**文件集合不相交**
     adddel    → A 的 diff 只有一个新增文件，B 的 diff 只有一个删除文件（不同路径）
     conflict  → 先合 A 成功，再合 B 必然冲突（U 非空、冲突文件非空）
   同时核对响应里的 verified / verifyDetail 与独立复核**一致**。
2. **确定性**：同 seed 两个全新仓 → 分支名 / 文件集合 / 每个分支的 tree sha 完全相同；
   不同 seed / 不给 seed → 分支前缀与内容都不同。
3. **count**：1 / 3 / 5 的场景数 = count × len(kinds)；0 / 负数 / 非整数 / 超大 → 400。
4. **护栏**：unstaged / staged / untracked / 未出生 HEAD / 合并态 / 无 main / 非仓，
   逐项与 Python 比 **HTTP 码 + error 文案**。
5. **形状**：响应键集与类型（递归 shape）与 Python 一致；值只比**确定性的那些**
   （kind/count/kinds/prefix/branchPrefix/分支名/files/id/expect/verified/verifyDetail），
   随机内容（aEdit/bEdit/gapLines 的值/base/mergeOutput）不比。

用法
====
    python -X utf8 c/tests/conformance_gen.py [--keep]

前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
_PY_CANDS = [os.environ.get("AIGIT_PY_ROOT"),
             os.path.join(os.path.dirname(C_ROOT), "python-ref"),
             r"D:\project\aigit\python-ref"]
PY_ROOT = next((p for p in _PY_CANDS if p and os.path.isdir(p)), _PY_CANDS[1])
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_gen_conf_%d" % (os.getpid() % 100000))

# 固定身份/日期：两边写出的 tree sha 与提交内容才可比
FIXED = {
    "GIT_AUTHOR_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_COMMITTER_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@localhost",
}
ENV = dict(os.environ, **FIXED)


# ---------------------------------------------------------------- 结果收集

class Res:
    def __init__(self):
        self.rows = []          # (label, ok, detail)
        self.npass = 0
        self.nfail = 0
        self.diffs = []         # 已知差异（不在 gen.c 范围内）
        self.matrix = {}        # (kind, fixture) -> (ok, detail)

    def ck(self, label, cond, detail=""):
        ok = bool(cond)
        self.rows.append((label, ok, detail))
        if ok:
            self.npass += 1
        else:
            self.nfail += 1
            print("  FAIL %-52s %s" % (label, detail))
        return ok

    def eq(self, label, got, want, extra=""):
        ok = got == want
        return self.ck(label, ok, "" if ok else "got=%r want=%r %s" % (got, want, extra))

    def known(self, label, cond, detail=""):
        """已知差异：验证 C 的行为就是文档里写的那样，记为 DIFF 而不是 FAIL。"""
        ok = bool(cond)
        self.diffs.append((label, ok, detail))
        if not ok:
            self.nfail += 1
        print("  DIFF %-52s %s" % (label, "" if ok else "（与文档不符！%s）" % detail))
        return ok


# ---------------------------------------------------------------- git 小工具

def sh(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=repo, capture_output=True,
                          encoding="utf-8", errors="replace", env=ENV)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def w(repo, name, text):
    p = os.path.join(repo, name.replace("/", os.sep))
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def init_repo(d, branch="main"):
    os.makedirs(d, exist_ok=True)
    must(d, "init", "-q", "-b", branch)
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    must(d, "config", "core.autocrlf", "false")
    return d


def commit(repo, msg):
    must(repo, "add", "-A")
    must(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- fixtures

def fx_clean(d):
    init_repo(d)
    w(d, "seed.txt", "seed\n")
    commit(d, "init")
    return d


def fx_history(d):
    """有历史 / 有旁支 / 有 tag：确认生成器不依赖「main 只有一次提交」。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "sub/b.txt", "b1\n")
    commit(d, "c1")
    w(d, "a.txt", "a2\n")
    commit(d, "c2")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "f.txt", "feature\n")
    commit(d, "f1")
    must(d, "checkout", "-q", "main")
    must(d, "tag", "v1")
    return d


def fx_detached(d):
    fx_clean(d)
    must(d, "checkout", "-q", "--detach")
    return d


def fx_unborn(d):
    init_repo(d)                      # .git 在，但一个提交都没有
    return d


def fx_nomain(d):
    init_repo(d, branch="trunk")
    w(d, "a.txt", "a\n")
    commit(d, "c1")
    return d


def fx_notrepo(d):
    os.makedirs(d, exist_ok=True)     # 没有 .git；服务启动时自己引导成仓
    return d


def fx_dirty_unstaged(d):
    fx_clean(d)
    w(d, "seed.txt", "changed\n")
    return d


def fx_dirty_staged(d):
    fx_clean(d)
    w(d, "seed.txt", "changed\n")
    must(d, "add", "seed.txt")
    return d


def fx_dirty_untracked(d):
    fx_clean(d)
    w(d, "brand_new.txt", "x\n")
    return d


def fx_mergestate(d):
    init_repo(d)
    w(d, "c.txt", "base\n")
    commit(d, "base")
    must(d, "checkout", "-q", "-b", "side")
    w(d, "c.txt", "side\n")
    commit(d, "side")
    must(d, "checkout", "-q", "main")
    w(d, "c.txt", "main\n")
    commit(d, "main2")
    sh(d, "merge", "side")            # 期望冲突，留下合并态
    return d


FIXTURES = {
    "clean": fx_clean,
    "history": fx_history,
    "detached": fx_detached,
    "unborn": fx_unborn,
    "nomain": fx_nomain,
    "notrepo": fx_notrepo,
    "dirty-unstaged": fx_dirty_unstaged,
    "dirty-staged": fx_dirty_staged,
    "dirty-untracked": fx_dirty_untracked,
    "mergestate": fx_mergestate,
}

MATRIX_FIXTURES = ["clean", "history", "detached"]
GUARD_FIXTURES = ["unborn", "nomain", "notrepo", "dirty-unstaged",
                  "dirty-staged", "dirty-untracked", "mergestate"]


def build_fixture(name, root):
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    return FIXTURES[name](root)


# ---------------------------------------------------------------- 服务

def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_server(which, repo, tag):
    port = free_port()
    log = open(os.path.join(BASE, "srv_%s_%s.log" % (which, tag)), "wb")
    if which == "c":
        cmd = [EXE, "--root", repo, "--port", str(port)]
        cwd = C_ROOT
    else:
        cmd = [sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py"),
               "--root", repo, "--port", str(port)]
        cwd = PY_ROOT
    env = dict(ENV, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=log, stderr=log)
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port,
                                   timeout=1).read()
            return p, port
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("%s 服务启动即退出（见 %s）" % (which, log.name))
            time.sleep(0.1)
    raise RuntimeError("%s 服务启动超时" % which)


def stop(p):
    try:
        p.terminate()
        p.wait(15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def post(port, body=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    rq = urllib.request.Request("http://127.0.0.1:%d/api/gen" % port, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        r = urllib.request.urlopen(rq, timeout=600)
        code, payload = r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = {"__raw__": payload.decode("utf-8", "replace")}
    return code, js


def norm(obj, repo):
    if isinstance(obj, str):
        return obj.replace(repo, "<ROOT>").replace(repo.replace("\\", "/"), "<ROOT>")
    if isinstance(obj, list):
        return [norm(x, repo) for x in obj]
    if isinstance(obj, dict):
        return {k: norm(v, repo) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- 形状

def shape(o):
    """递归「键集 + 类型」快照。list 只看第一个元素（其余同构）。"""
    if isinstance(o, dict):
        return {k: shape(v) for k, v in sorted(o.items())}
    if isinstance(o, list):
        if not o:
            return ["<empty>"]
        return [shape(o[0])]
    return type(o).__name__


def keys(o):
    return sorted(o.keys()) if isinstance(o, dict) else o


# ---------------------------------------------------------------- 语义复核

def restore(repo, base):
    sh(repo, "merge", "--abort")
    sh(repo, "checkout", "-q", "-f", "main")
    sh(repo, "reset", "--hard", "-q", base)


def changed_files(repo, base, ref):
    out = must(repo, "diff", "--name-status", base, ref)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append((parts[0][:1], parts[-1]))
    return rows


def verify_semantics(repo, sc, kind):
    """用真 git 独立复核一个场景。返回 (ok, [问题...], detail_dict)。"""
    problems = []
    base = sc["base"]
    a, b = sc["branches"]
    expect_clean = (kind != "conflict")

    # base 必须能解析（10 位缩写）
    p = sh(repo, "rev-parse", "--verify", base + "^{commit}")
    if p.returncode != 0:
        return False, ["base %r 解析不了" % base], {}

    restore(repo, base)

    # 两个分支都必须以 base 为祖先
    for br in (a, b):
        if sh(repo, "merge-base", "--is-ancestor", base, br).returncode != 0:
            problems.append("%s 不是 base 的后代" % br)

    r1 = sh(repo, "merge", "--no-edit", "-q", a)
    r2 = sh(repo, "merge", "--no-edit", "-q", b)
    status = sh(repo, "status", "--porcelain").stdout.strip()
    unmerged = [x for x in sh(repo, "diff", "--name-only", "--diff-filter=U")
                .stdout.splitlines() if x.strip()]

    detail = {"rc_a": r1.returncode, "rc_b": r2.returncode,
              "status_lines": len(status.splitlines()) if status else 0,
              "unmerged": unmerged}

    if expect_clean:
        if r1.returncode != 0:
            problems.append("合并 A 失败 rc=%d: %s"
                            % (r1.returncode, (r1.stderr or r1.stdout).strip()[:160]))
        if r2.returncode != 0:
            problems.append("合并 B 失败 rc=%d: %s"
                            % (r2.returncode, (r2.stderr or r2.stdout).strip()[:160]))
        if status:
            problems.append("合并后工作区不干净: %r" % status[:160])
        if unmerged:
            problems.append("合并后仍有未合并路径: %r" % unmerged)

        if kind == "unrelated":
            fa = {p for _, p in changed_files(repo, base, a)}
            fb = {p for _, p in changed_files(repo, base, b)}
            detail["files_a"], detail["files_b"] = sorted(fa), sorted(fb)
            if fa & fb:
                problems.append("unrelated 两个分支改了同一个文件: %r" % sorted(fa & fb))
            if not fa or not fb:
                problems.append("unrelated 有分支没改任何文件")
        elif kind == "adddel":
            ca = changed_files(repo, base, a)
            cb = changed_files(repo, base, b)
            detail["diff_a"], detail["diff_b"] = ca, cb
            if len(ca) != 1 or ca[0][0] != "A":
                problems.append("adddel 的 A 侧不是「只新增一个文件」: %r" % ca)
            if len(cb) != 1 or cb[0][0] != "D":
                problems.append("adddel 的 B 侧不是「只删除一个文件」: %r" % cb)
            if ca and cb and ca[0][1] == cb[0][1]:
                problems.append("adddel 新增和删除的是同一个路径")
        elif kind == "mergeable":
            if not sc.get("gapLines"):
                problems.append("mergeable 缺 gapLines")
    else:
        if r1.returncode != 0:
            problems.append("对照组第一次 merge(A) 应当成功却失败 rc=%d" % r1.returncode)
        if r2.returncode == 0:
            problems.append("对照组第二次 merge(B) 应当冲突却成功了")
        if not unmerged:
            problems.append("对照组冲突后 U 列表为空")
        for rel in unmerged:
            full = os.path.join(repo, rel.replace("/", os.sep))
            if os.path.exists(full) and os.path.getsize(full) == 0:
                problems.append("冲突文件 %s 是空的" % rel)
            if "aigit_gen_" not in rel:
                problems.append("冲突文件 %r 不在 aigit_gen_ 前缀下" % rel)

    restore(repo, base)

    # 响应自述必须与独立复核一致
    vd = sc.get("verifyDetail") or {}
    if sc.get("verified") is not True:
        problems.append("响应 verified=%r（期望 True）" % sc.get("verified"))
    if vd.get("first_merge_ok") != (r1.returncode == 0):
        problems.append("first_merge_ok=%r 与实际 rc_a=%d 不符"
                        % (vd.get("first_merge_ok"), r1.returncode))
    if vd.get("merged_ok") != (r1.returncode == 0 and r2.returncode == 0):
        problems.append("merged_ok=%r 与实际不符" % (vd.get("merged_ok"),))
    if vd.get("unmerged") != unmerged:
        problems.append("verifyDetail.unmerged=%r 与实际 %r 不符"
                        % (vd.get("unmerged"), unmerged))
    if expect_clean:
        if vd.get("worktree_clean") is not True:
            problems.append("worktree_clean=%r（期望 True）" % (vd.get("worktree_clean"),))
    else:
        if vd.get("worktree_clean") is not None:
            problems.append("对照组的 worktree_clean 应当是 null，实际 %r"
                            % (vd.get("worktree_clean"),))

    return (not problems), problems, detail


# ---------------------------------------------------------------- 快照

def worktree_files(repo):
    out = []
    for dirpath, dirnames, filenames in os.walk(repo):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            out.append(os.path.relpath(full, repo).replace("\\", "/"))
    return sorted(out)


def tree_of(repo, ref):
    return must(repo, "rev-parse", ref + "^{tree}").strip()


# ---------------------------------------------------------------- 1) kind × fixture

def section_matrix(res):
    print("\n[1] kind × fixture：生成 + 真 git 独立复核")
    for kind in ("mergeable", "unrelated", "adddel", "conflict"):
        for fx in MATRIX_FIXTURES:
            label = "%-9s / %-8s" % (kind, fx)
            repo = os.path.join(BASE, "m_%s_%s" % (kind, fx))
            build_fixture(fx, repo)
            p, port = start_server("c", repo, "m_%s_%s" % (kind, fx))
            try:
                code, j = post(port, {"kind": kind, "count": 1, "seed": 20261104})
            finally:
                stop(p)
            if code != 200:
                res.ck(label, False, "code=%s body=%r" % (code, j))
                continue
            if len(j.get("scenarios") or []) != 1:
                res.ck(label, False, "scenarios=%d" % len(j.get("scenarios") or []))
                continue
            # 生成器的收口状态：回到 main、工作区干净、没有合并态残留
            state = {
                "branch": sh(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                "porcelain": sh(repo, "status", "--porcelain").stdout.strip(),
                "merge_head": os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")),
            }
            res.ck("%-22s 收口：在 main / 干净 / 无合并态" % label,
                   state["branch"] == "main" and state["porcelain"] == ""
                   and not state["merge_head"], repr(state))
            ok, problems, detail = verify_semantics(repo, j["scenarios"][0], kind)
            res.matrix[(kind, fx)] = (ok, "; ".join(problems)[:400])
            res.ck(label, ok, "; ".join(problems)[:400] if problems else "")
            if ok:
                print("  PASS %-22s %s" % (label, detail))
            shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------- 2) 确定性

def gen_in_fresh(res, tag, fixture, body):
    repo = os.path.join(BASE, "det_%s" % tag)
    build_fixture(fixture, repo)
    p, port = start_server("c", repo, "det_%s" % tag)
    try:
        code, j = post(port, body)
    finally:
        stop(p)
    return repo, code, j


def section_determinism(res):
    print("\n[2] 确定性：同 seed 两个全新仓 / 不同 seed / 不给 seed")

    # 同 seed，两个全新仓
    r1, c1, j1 = gen_in_fresh(res, "same1", "clean",
                              {"kind": "mergeable", "count": 2, "seed": 12345})
    r2, c2, j2 = gen_in_fresh(res, "same2", "clean",
                              {"kind": "mergeable", "count": 2, "seed": 12345})
    res.eq("det/同 seed 两次 HTTP 码", (c1, c2), (200, 200))

    s1 = [(s["id"], tuple(s["branches"])) for s in (j1.get("scenarios") or [])]
    s2 = [(s["id"], tuple(s["branches"])) for s in (j2.get("scenarios") or [])]
    res.eq("det/同 seed 分支名相同", s1, s2)
    res.eq("det/同 seed 文件集合相同",
           sorted(j1.get("files") or []), sorted(j2.get("files") or []))
    res.eq("det/同 seed branchPrefix 相同",
           j1.get("branchPrefix"), j2.get("branchPrefix"))

    # 每个分支的 tree sha（内容级判据）
    t1, t2 = [], []
    for s in (j1.get("scenarios") or []):
        for br in s["branches"]:
            t1.append(tree_of(r1, br))
    for s in (j2.get("scenarios") or []):
        for br in s["branches"]:
            t2.append(tree_of(r2, br))
    res.eq("det/同 seed 每个分支 tree sha 相同（内容逐字相同）", t1, t2)
    res.eq("det/同 seed 工作区文件集合相同",
           worktree_files(r1), worktree_files(r2))

    # 不同 seed：分支前缀必须不同，内容必须不同
    r3, c3, j3 = gen_in_fresh(res, "diffseed", "clean",
                              {"kind": "mergeable", "count": 2, "seed": 999})
    res.eq("det/不同 seed HTTP 码", c3, 200)
    res.ck("det/不同 seed 分支前缀不同",
           j3.get("branchPrefix") != j1.get("branchPrefix"),
           "%r vs %r" % (j3.get("branchPrefix"), j1.get("branchPrefix")))
    t3 = []
    for s in (j3.get("scenarios") or []):
        for br in s["branches"]:
            t3.append(tree_of(r3, br))
    res.ck("det/不同 seed 内容不同（tree sha）", t1 != t3, "%r vs %r" % (t1, t3))

    # 不给 seed：两次都成功且结果不同（同一个仓里第二次也不会撞名）
    repo = os.path.join(BASE, "det_noseed")
    build_fixture("clean", repo)
    p, port = start_server("c", repo, "det_noseed")
    try:
        ca, ja = post(port, {"kind": "mergeable", "count": 1})
        cb, jb = post(port, {"kind": "mergeable", "count": 1})
    finally:
        stop(p)
    res.eq("det/不给 seed 两次 HTTP 码", (ca, cb), (200, 200))
    res.ck("det/不给 seed 两次 branchPrefix 不同",
           ja.get("branchPrefix") != jb.get("branchPrefix"),
           "%r vs %r" % (ja.get("branchPrefix"), jb.get("branchPrefix")))
    res.ck("det/不给 seed 会回写一个整数 seed",
           isinstance(ja.get("seed"), int) and 0 <= ja["seed"] < (1 << 30),
           "seed=%r" % (ja.get("seed"),))
    ta, tb = [], []
    for s in (ja.get("scenarios") or []):
        for br in s["branches"]:
            ta.append(tree_of(repo, br))
    for s in (jb.get("scenarios") or []):
        for br in s["branches"]:
            tb.append(tree_of(repo, br))
    res.ck("det/不给 seed 内容不同", ta != tb, "%r vs %r" % (ta, tb))

    # 同 seed 在同一仓里第二次 -> 分支撞名 400（Python 的行为）
    p, port = start_server("c", repo, "det_twice")
    try:
        c1_, j1_ = post(port, {"kind": "unrelated", "count": 1, "seed": 55})
        c2_, j2_ = post(port, {"kind": "unrelated", "count": 1, "seed": 55})
        c3_, j3_ = post(port, {"kind": "unrelated", "count": 1, "seed": 56})
    finally:
        stop(p)
    res.eq("det/同 seed 连发两次：第一次 200", c1_, 200)
    res.eq("det/同 seed 连发两次：第二次 400（撞名保护）", c2_, 400)
    res.ck("det/撞名 400 的文案是「以下分支已存在」",
           str(j2_.get("error", "")).startswith("以下分支已存在,拒绝生成"),
           repr(str(j2_.get("error"))[:120]))
    res.eq("det/换 seed 后 200", c3_, 200)

    for d in (r1, r2, r3, repo):
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 3) count

def section_count(res):
    print("\n[3] count：1 / 3 / 5 的场景数；0 / 负数 / 非整数 / 超大")
    for n, want in ((1, 1), (3, 3), (5, 5)):
        repo = os.path.join(BASE, "cnt_%d" % n)
        build_fixture("clean", repo)
        p, port = start_server("c", repo, "cnt_%d" % n)
        try:
            code, j = post(port, {"kind": "mergeable", "count": n, "seed": 11})
        finally:
            stop(p)
        res.eq("count/%-2d HTTP 码" % n, code, 200)
        res.eq("count/%-2d 场景数" % n, len(j.get("scenarios") or []), want)
        res.eq("count/%-2d 分支数" % n, len(j.get("branches") or []), want)
        res.eq("count/%-2d 回显 count" % n, j.get("count"), n)
        names = []
        for b in (j.get("branches") or []):
            names.extend(b.get("branches") or [])
        res.eq("count/%-2d 分支名无重复" % n, len(set(names)), len(names))
        shutil.rmtree(repo, ignore_errors=True)

    # all：场景数 = count × 4
    repo = os.path.join(BASE, "cnt_all")
    build_fixture("clean", repo)
    p, port = start_server("c", repo, "cnt_all")
    try:
        code, j = post(port, {"kind": "all", "count": 2, "seed": 12})
    finally:
        stop(p)
    res.eq("count/all count=2 HTTP 码", code, 200)
    res.eq("count/all count=2 场景数", len(j.get("scenarios") or []), 8)
    res.eq("count/all count=2 kinds", j.get("kinds"),
           ["mergeable", "unrelated", "adddel", "conflict"])
    shutil.rmtree(repo, ignore_errors=True)

    # 非法 count
    repo = os.path.join(BASE, "cnt_bad")
    build_fixture("clean", repo)
    p, port = start_server("c", repo, "cnt_bad")
    cases = [
        ("0", 0, "count 至少为 1,收到 0"),
        ("-1", -1, "count 至少为 1,收到 -1"),
        ("-100", -100, "count 至少为 1,收到 -100"),
        ("字符串", "notanint", "参数 count 必须是整数,收到 'notanint'"),
        ("null", None, "参数 count 必须是整数,收到 None"),
        ("数组", [1], "参数 count 必须是整数,收到 [1]"),
        ("超大", 100000, "count 过大(最多 100 个场景),收到 100000"),
    ]
    for name, val, want in cases:
        code, j = post(port, {"kind": "mergeable", "count": val, "seed": 13})
        res.eq("count/非法 %-6s HTTP 码" % name, code, 400)
        res.eq("count/非法 %-6s 文案" % name, j.get("error"), want)
        res.eq("count/非法 %-6s ok" % name, j.get("ok"), False)
    stop(p)
    shutil.rmtree(repo, ignore_errors=True)

    # _bi 的边角：float 截断、bool 当 1、数字字符串（与 Python 的 _bi 同义）
    for name, val, want_count in (("2.9", 2.9, 2), ("True", True, 1),
                                  ("\"3\"", "3", 3)):
        repo = os.path.join(BASE, "cnt_edge_%s" % abs(hash(name)))
        build_fixture("clean", repo)
        p, port = start_server("c", repo, "cnt_edge")
        try:
            code, j = post(port, {"kind": "mergeable", "count": val, "seed": 14})
        finally:
            stop(p)
        res.eq("count/边角 %-6s HTTP 码" % name, code, 200)
        res.eq("count/边角 %-6s 回显 count" % name, j.get("count"), want_count)
        res.eq("count/边角 %-6s 场景数" % name,
               len(j.get("scenarios") or []), want_count)
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------- 4) 护栏（C vs Python）

def section_guards(res):
    print("\n[4] 护栏：脏仓 / 未出生 / 无 main / 非仓 —— C vs Python")
    for fx in GUARD_FIXTURES:
        body = {"kind": "mergeable", "count": 1, "seed": 3}
        cdir = os.path.join(BASE, "g_c_%s" % fx)
        pdir = os.path.join(BASE, "g_p_%s" % fx)
        build_fixture(fx, cdir)
        build_fixture(fx, pdir)
        before = None
        if fx.startswith("dirty-"):
            before = (must(cdir, "log", "--oneline"),
                      open(os.path.join(cdir, "seed.txt"), encoding="utf-8").read()
                      if os.path.exists(os.path.join(cdir, "seed.txt")) else None,
                      sorted(worktree_files(cdir)))

        pc, portc = start_server("c", cdir, "g_%s" % fx)
        try:
            cc, jc = post(portc, body)
        finally:
            stop(pc)
        pp, portp = start_server("p", pdir, "g_%s" % fx)
        try:
            cp, jp = post(portp, body)
        finally:
            stop(pp)

        res.eq("guard/%-16s HTTP 码一致" % fx, cc, cp)
        res.eq("guard/%-16s error 文案一致" % fx,
               norm(jc.get("error"), cdir), norm(jp.get("error"), pdir))
        res.eq("guard/%-16s ok 一致" % fx, jc.get("ok"), jp.get("ok"))
        if cc == 200:
            res.eq("guard/%-16s 分支名一致" % fx,
                   [b["branches"] for b in (jc.get("branches") or [])],
                   [b["branches"] for b in (jp.get("branches") or [])])

        # 脏仓被拒之后，用户的东西必须一字不动（D7 的数据安全判据）
        if before is not None:
            after = (must(cdir, "log", "--oneline"),
                     open(os.path.join(cdir, "seed.txt"), encoding="utf-8").read()
                     if os.path.exists(os.path.join(cdir, "seed.txt")) else None,
                     sorted(worktree_files(cdir)))
            res.eq("guard/%-16s 用户改动一字未动" % fx, after, before)

        shutil.rmtree(cdir, ignore_errors=True)
        shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------- 5) 形状 / 键集

def section_shape(res):
    print("\n[5] 形状：响应键集与类型（C vs Python）")
    body = {"kind": "all", "count": 1, "seed": 424242}
    cdir = os.path.join(BASE, "s_c")
    pdir = os.path.join(BASE, "s_p")
    build_fixture("clean", cdir)
    build_fixture("clean", pdir)

    pc, portc = start_server("c", cdir, "s_c")
    try:
        cc, jc = post(portc, body)
    finally:
        stop(pc)
    pp, portp = start_server("p", pdir, "s_p")
    try:
        cp, jp = post(portp, body)
    finally:
        stop(pp)

    res.eq("shape/HTTP 码", (cc, cp), (200, 200))
    res.eq("shape/顶层键集", keys(jc), keys(jp))
    res.eq("shape/顶层类型", shape(jc), shape(jp))

    sc_c = jc.get("scenarios") or []
    sc_p = jp.get("scenarios") or []
    res.eq("shape/场景数", len(sc_c), len(sc_p))
    for i in range(min(len(sc_c), len(sc_p))):
        res.eq("shape/scenarios[%d] 键集" % i, keys(sc_c[i]), keys(sc_p[i]))
        res.eq("shape/scenarios[%d] 类型" % i, shape(sc_c[i]), shape(sc_p[i]))
        res.eq("shape/scenarios[%d].verifyDetail 键集" % i,
               keys(sc_c[i].get("verifyDetail") or {}),
               keys(sc_p[i].get("verifyDetail") or {}))

    res.eq("shape/branches 条目键集",
           keys((jc.get("branches") or [{}])[0]),
           keys((jp.get("branches") or [{}])[0]))

    # 确定性字段：值也应一致（同 seed 同 kind 同 index）
    for field in ("kind", "count", "kinds", "prefix", "branchPrefix", "files"):
        res.eq("shape/确定性字段 %s" % field, jc.get(field), jp.get(field))
    res.eq("shape/分支名一致",
           [b["branches"] for b in (jc.get("branches") or [])],
           [b["branches"] for b in (jp.get("branches") or [])])
    for i in range(min(len(sc_c), len(sc_p))):
        for field in ("id", "kind", "index", "branches", "files", "expect",
                      "verified"):
            res.eq("shape/scenarios[%d].%s" % (i, field),
                   sc_c[i].get(field), sc_p[i].get(field))
        res.eq("shape/scenarios[%d].verifyDetail" % i,
               sc_c[i].get("verifyDetail"), sc_p[i].get("verifyDetail"))
        res.eq("shape/scenarios[%d].gapLines 有无" % i,
               "gapLines" in sc_c[i], "gapLines" in sc_p[i])

    # GET /api/gen：Python 是 404（只有 POST 分派），C 不能 405/500
    def _get(port):
        try:
            r = urllib.request.urlopen("http://127.0.0.1:%d/api/gen" % port, timeout=30)
            return r.getcode(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    pc, portc = start_server("c", cdir, "s_c3")
    try:
        gc, _ = _get(portc)
    finally:
        stop(pc)
    pp, portp = start_server("p", pdir, "s_p3")
    try:
        gp, _ = _get(portp)
    finally:
        stop(pp)
    res.eq("shape/GET /api/gen 都是 404", (gc, gp), (404, 404))

    # 空 body / 缺 kind：两边都按 kind=all + count=3（只比结构，随机 seed 不同）
    pc, portc = start_server("c", cdir, "s_c2")
    try:
        cc2, jc2 = post(portc, raw=b"")
    finally:
        stop(pc)
    pp, portp = start_server("p", pdir, "s_p2")
    try:
        cp2, jp2 = post(portp, raw=b"")
    finally:
        stop(pp)
    res.eq("shape/空 body HTTP 码", (cc2, cp2), (200, 200))
    res.eq("shape/空 body 键集", keys(jc2), keys(jp2))
    res.eq("shape/空 body kind/count", (jc2.get("kind"), jc2.get("count")),
           (jp2.get("kind"), jp2.get("count")))
    res.eq("shape/空 body 场景数", len(jc2.get("scenarios") or []),
           len(jp2.get("scenarios") or []))
    res.ck("shape/空 body 回写整数 seed", isinstance(jc2.get("seed"), int),
           "seed=%r" % (jc2.get("seed"),))

    # 坏 body / 非对象：文案一致
    for name, raw in (("坏 JSON", b"{oops"), ("数组", b"[1,2]"),
                      ("字符串", b'"x"'), ("null", b"null")):
        cdir2 = os.path.join(BASE, "s_bad_c_%s" % name)
        pdir2 = os.path.join(BASE, "s_bad_p_%s" % name)
        build_fixture("clean", cdir2)
        build_fixture("clean", pdir2)
        pc, portc = start_server("c", cdir2, "s_badc")
        try:
            c1, j1 = post(portc, raw=raw)
        finally:
            stop(pc)
        pp, portp = start_server("p", pdir2, "s_badp")
        try:
            c2, j2 = post(portp, raw=raw)
        finally:
            stop(pp)
        res.eq("shape/body %-6s HTTP 码" % name, (c1, c2), (400, 400))
        res.eq("shape/body %-6s 文案" % name,
               norm(j1.get("error"), cdir2), norm(j2.get("error"), pdir2))
        shutil.rmtree(cdir2, ignore_errors=True)
        shutil.rmtree(pdir2, ignore_errors=True)

    shutil.rmtree(cdir, ignore_errors=True)
    shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------- 6) 键集/码/code 逐 fixture

def section_cross(res):
    print("\n[6] 逐 fixture 的码/文案/键集对比（值不比）")
    fixtures = ["clean", "history", "detached", "unborn", "nomain",
                "notrepo", "mergestate"]
    for fx in fixtures:
        body = {"kind": "all", "count": 1, "seed": 777}
        cdir = os.path.join(BASE, "x_c_%s" % fx)
        pdir = os.path.join(BASE, "x_p_%s" % fx)
        build_fixture(fx, cdir)
        build_fixture(fx, pdir)
        pc, portc = start_server("c", cdir, "x_%s" % fx)
        try:
            cc, jc = post(portc, body)
        finally:
            stop(pc)
        pp, portp = start_server("p", pdir, "x_%s" % fx)
        try:
            cp, jp = post(portp, body)
        finally:
            stop(pp)

        res.eq("cross/%-14s HTTP 码" % fx, cc, cp)
        res.eq("cross/%-14s 顶层键集" % fx, keys(jc), keys(jp))
        if cc == 200:
            res.eq("cross/%-14s 场景数" % fx,
                   len(jc.get("scenarios") or []), len(jp.get("scenarios") or []))
            res.eq("cross/%-14s files" % fx, jc.get("files"), jp.get("files"))
            res.eq("cross/%-14s branchPrefix" % fx,
                   jc.get("branchPrefix"), jp.get("branchPrefix"))
            res.eq("cross/%-14s verified/verifyDetail" % fx,
                   [s.get("verified") for s in (jc.get("scenarios") or [])],
                   [s.get("verified") for s in (jp.get("scenarios") or [])])
        else:
            res.eq("cross/%-14s error" % fx,
                   norm(jc.get("error"), cdir), norm(jp.get("error"), pdir))
        shutil.rmtree(cdir, ignore_errors=True)
        shutil.rmtree(pdir, ignore_errors=True)

    # 撞名保护：同 seed 连发两次（Python 的行为就是 400，不是自动换名）
    cdir = os.path.join(BASE, "x_c_twice")
    pdir = os.path.join(BASE, "x_p_twice")
    build_fixture("clean", cdir)
    build_fixture("clean", pdir)
    pc, portc = start_server("c", cdir, "x_twice")
    try:
        c1, j1 = post(portc, {"kind": "all", "count": 2, "seed": 7})
        c2, j2 = post(portc, {"kind": "all", "count": 2, "seed": 7})
    finally:
        stop(pc)
    pp, portp = start_server("p", pdir, "x_twice")
    try:
        p1, k1 = post(portp, {"kind": "all", "count": 2, "seed": 7})
        p2, k2 = post(portp, {"kind": "all", "count": 2, "seed": 7})
    finally:
        stop(pp)
    res.eq("cross/twice 第一次 200", (c1, p1), (200, 200))
    res.eq("cross/twice 同 seed 第二次 400", (c2, p2), (400, 400))
    res.eq("cross/twice 撞名文案逐字一致（含「共 16 个」截断）",
           norm(j2.get("error"), cdir), norm(k2.get("error"), pdir))
    res.ck("cross/twice 撞名文案里有 10 条 + 总数",
           str(j2.get("error", "")).count("aigit_gen/s7/") == 10
           and "共 16 个" in str(j2.get("error", "")),
           "names=%d tail=%r" % (str(j2.get("error", "")).count("aigit_gen/s7/"),
                                 str(j2.get("error"))[-40:]))
    shutil.rmtree(cdir, ignore_errors=True)
    shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------- 7) 敌意输入

HOSTILE = [
    ("seed 对象", b'{"kind":"mergeable","count":1,"seed":{}}'),
    ("seed 数组", b'{"kind":"mergeable","count":1,"seed":[]}'),
    ("seed 带空格", b'{"kind":"mergeable","count":1,"seed":"a b"}'),
    ("seed 中文", '{"kind":"mergeable","count":1,"seed":"中"}'.encode("utf-8")),
    ("kind 数组", b'{"kind":["mergeable"],"count":1}'),
    ("kind 空串", b'{"kind":"","count":1}'),
    ('count "0_1"', b'{"kind":"mergeable","count":"0_1","seed":44}'),
    ('count " 2 "', b'{"kind":"mergeable","count":" 2 ","seed":45}'),
    ("多余键 + status", b'{"kind":"mergeable","count":1,"seed":46,"x":1,"status":false}'),
]


def section_hostile(res):
    print("\n[7] 敌意/边角输入：不许 500，码与文案跟 Python 一致")
    for i, (name, raw) in enumerate(HOSTILE):
        cdir = os.path.join(BASE, "h_c_%d" % i)
        pdir = os.path.join(BASE, "h_p_%d" % i)
        build_fixture("clean", cdir)
        build_fixture("clean", pdir)
        pc, portc = start_server("c", cdir, "h_c_%d" % i)
        try:
            cc, jc = post(portc, raw=raw)
        finally:
            stop(pc)
        pp, portp = start_server("p", pdir, "h_p_%d" % i)
        try:
            cp, jp = post(portp, raw=raw)
        finally:
            stop(pp)
        res.eq("hostile/%-14s HTTP 码" % name, cc, cp)
        res.ck("hostile/%-14s 不是 500" % name, cc != 500 and cp != 500,
               "C=%s P=%s" % (cc, cp))
        if cc == cp == 200:
            res.eq("hostile/%-14s count" % name, jc.get("count"), jp.get("count"))
            res.eq("hostile/%-14s 场景数" % name,
                   len(jc.get("scenarios") or []), len(jp.get("scenarios") or []))
            res.eq("hostile/%-14s branchPrefix" % name,
                   jc.get("branchPrefix"), jp.get("branchPrefix"))
        else:
            res.eq("hostile/%-14s error" % name,
                   norm(jc.get("error"), cdir), norm(jp.get("error"), pdir))
        shutil.rmtree(cdir, ignore_errors=True)
        shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------- main

def main():
    keep = "--keep" in sys.argv
    only = ""
    for a in sys.argv:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
    os.makedirs(BASE, exist_ok=True)
    if not os.path.exists(EXE):
        print("缺少 %s —— 先 mingw32-make" % EXE)
        return 2
    print("C exe  :", EXE)
    print("Python :", PY_ROOT)
    print("临时目录:", BASE)

    t0 = time.time()
    res = Res()
    sections = [("1", section_matrix), ("2", section_determinism),
                ("3", section_count), ("4", section_guards),
                ("5", section_shape), ("6", section_cross),
                ("7", section_hostile)]
    try:
        for num, fn in sections:
            if only and num not in only.split(","):
                continue
            fn(res)
    finally:
        if not keep:
            shutil.rmtree(BASE, ignore_errors=True)

    print("\n" + "=" * 78)
    print("kind × fixture 语义矩阵（真 git 独立复核）")
    print("-" * 78)
    header = "%-13s" % "kind\\fixture"
    for fx in MATRIX_FIXTURES:
        header += "%-11s" % fx
    print(header)
    for kind in ("mergeable", "unrelated", "adddel", "conflict"):
        line = "%-13s" % kind
        for fx in MATRIX_FIXTURES:
            ok, _ = res.matrix.get((kind, fx), (None, "未执行"))
            line += "%-11s" % ("SKIP" if ok is None else ("PASS" if ok else "FAIL"))
        print(line)
    print("-" * 78)
    print("汇总：PASS %d / FAIL %d / DIFF %d   （%.1fs）"
          % (res.npass, res.nfail, len(res.diffs), time.time() - t0))
    if res.diffs:
        print("\n已知差异（不在 gen.c 范围内，见交付说明）：")
        for label, ok, detail in res.diffs:
            print("  - %-46s %s%s" % (label, detail, "" if ok else "  ← 与文档不符！"))
    if res.nfail:
        print("\n失败项：")
        for label, ok, detail in res.rows:
            if not ok:
                print("  - %-46s %s" % (label, detail[:200]))
    print("=" * 78)
    return 0 if res.nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
