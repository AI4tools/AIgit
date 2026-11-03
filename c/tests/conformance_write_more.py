# -*- coding: utf-8 -*-
"""M2 写面第二组一致性判据：C 版 vs Python 版（checkout / branch / merge /
merge-abort / reset）。

判据
====
* 同一个 fixture 建**两份相同副本**：A 给 Python 服务、B 给 C 服务；
  两边按**相同顺序**发同一批 POST；
* 每一步比 **HTTP 码 + JSON 深比较**（dict/list 结构逐项相等）；
* 收口再比**最终仓库状态**：
      - `git status --porcelain=v2 -uall`
      - `HEAD`（sha）与 `symbolic-ref HEAD`
      - 全部 refs（`for-each-ref refname|objectname`）
      - 索引（`ls-files -s`，另算 sha256 便于看差异）
      - 工作区每个文件的 sha256（不含 .git）
      - 合并态（.git/MERGE_HEAD 是否存在/内容）
* 身份与日期固定（GIT_AUTHOR_DATE / GIT_COMMITTER_DATE + 仓内 user.*），
  所以两边写出的 merge commit sha 应当**逐字相同** —— head 是可比的。

不比的东西
==========
* `X-aigit-git-calls` / `X-aigit-git-cmds` 计数头：C 版不实现 Python 那条
  loose-ref 快路径（`_local_branch_exists`），spawn 次数本来就会不同；结果必须相同。
* `.git/index` 的**字节**：`git status` 会顺手写索引扩展（TREE/untracked cache），
  两边 status 调用次数不同，字节不同是正常的；比的是 `ls-files -s` 的语义内容。

用法
====
    python -X utf8 c/tests/conformance_write_more.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
PY_ROOT = r"D:\project\aigit"
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m2wm_%d" % (os.getpid() % 100000))

ENDPOINTS = ["/api/checkout", "/api/branch", "/api/merge",
             "/api/merge-abort", "/api/reset"]

# 固定身份/日期：两边写出的 commit（含 merge commit）sha 才能逐字比
FIXED = {
    "GIT_AUTHOR_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_COMMITTER_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@localhost",
}
ENV = dict(os.environ, **FIXED)


# ---------------------------------------------------------------- git 小工具

def sh(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=ENV)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:3]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def w(repo, name, text):
    path = os.path.join(repo, name)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def init_repo(d):
    os.makedirs(d, exist_ok=True)
    must(d, "init", "-q", "-b", "main")
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    must(d, "config", "core.autocrlf", "false")
    return d


def commit(repo, msg):
    must(repo, "add", "-A")
    must(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- fixtures

def fx_base(d):
    """a.txt / sub/b.txt / del.txt 已提交；分支 feature 上多一次提交，回到 main。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "sub/b.txt", "b1\n")
    w(d, "del.txt", "d1\n")
    commit(d, "c1")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "f.txt", "feature\n")
    commit(d, "f1")
    must(d, "checkout", "-q", "main")
    return d


def fx_plain(d):
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "sub/b.txt", "b1\n")
    commit(d, "c1")
    return d


def fx_dirty(d):
    """main 上 a.txt 有未提交改动，且 feature 上 a.txt 内容不同 -> checkout 会被拒。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "keep.txt", "k\n")
    commit(d, "c1")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "a.txt", "a-feature\n")
    commit(d, "f1")
    must(d, "checkout", "-q", "main")
    w(d, "a.txt", "a-dirty\n")          # 未暂存改动
    w(d, "staged.txt", "s\n")
    must(d, "add", "--", "staged.txt")
    return d


def fx_unborn(d):
    return init_repo(d)


def fx_detached(d):
    fx_plain(d)
    must(d, "checkout", "-q", "--detach")
    return d


def fx_conflict(d):
    """双方改 c.txt 同一行 -> 真冲突。"""
    init_repo(d)
    w(d, "c.txt", "base\n")
    w(d, "keep.txt", "k\n")
    commit(d, "base")
    must(d, "checkout", "-q", "-b", "side")
    w(d, "c.txt", "side\n")
    commit(d, "side")
    must(d, "checkout", "-q", "main")
    w(d, "c.txt", "main\n")
    commit(d, "main2")
    sh(d, "merge", "side")              # 期望冲突，留下合并态
    return d


def fx_packed(d):
    """refs 全在 packed-refs 里（loose ref 不存在）—— 走快路径的第二档。"""
    fx_base(d)
    must(d, "pack-refs", "--all")
    return d


def fx_badref(d):
    """`refs/heads/broken` 存在但内容不像 ref —— 快路径必须回退 show-ref。"""
    fx_base(d)
    with open(os.path.join(d, ".git", "refs", "heads", "broken"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("not-a-ref\n")
    return d


def fx_conflict_pending(d):
    """双方改 c.txt 同一行，但**还没开始合并** —— 让 /api/merge 自己去撞冲突。"""
    init_repo(d)
    w(d, "c.txt", "base\n")
    w(d, "keep.txt", "k\n")
    commit(d, "base")
    must(d, "checkout", "-q", "-b", "side")
    w(d, "c.txt", "side\n")
    commit(d, "side")
    must(d, "checkout", "-q", "main")
    w(d, "c.txt", "main\n")
    commit(d, "main2")
    return d


def fx_multi(d):
    """多提交 + 混合改动：给 reset soft/mixed/hard 用。"""
    init_repo(d)
    w(d, "a.txt", "l1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    w(d, "a.txt", "l2\n")
    commit(d, "c2")
    w(d, "a.txt", "l3\n")
    w(d, "b.txt", "b2\n")
    commit(d, "c3")
    # 收口状态：一处未暂存、一处已暂存、一个未跟踪文件、一个未跟踪目录
    w(d, "a.txt", "l3-dirty\n")
    w(d, "b.txt", "b2-staged\n")
    must(d, "add", "--", "b.txt")
    w(d, "untracked.txt", "u\n")
    os.makedirs(os.path.join(d, "newdir"), exist_ok=True)
    w(d, "newdir/inner.txt", "i\n")
    return d


def fx_merge_clean(d):
    """可快进：main 落后 feature 一次提交。"""
    return fx_base(d)


def fx_threeway(d):
    """非快进、无冲突：两边各改不同文件 -> 产生真正的 merge commit。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "b.txt", "b-feature\n")
    w(d, "f.txt", "f\n")
    commit(d, "feature")
    must(d, "checkout", "-q", "main")
    w(d, "a.txt", "a-main\n")
    commit(d, "main2")
    return d


# ---------------------------------------------------------------- 服务

def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_py(repo, port, log):
    env = dict(ENV, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([sys.executable, "-X", "utf8",
                             os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT, stdout=log, stderr=log)


def start_c(repo, port, log):
    env = dict(ENV, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT, stdout=log, stderr=log)


def wait_health(port, proc, timeout=25.0):
    url = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
    return False


def norm(obj, repo):
    """把响应里的**绝对仓路径**折成 <ROOT>。

    两份副本本来就活在不同目录下，而 `/api/*` 的成功响应里带 `root`（还有
    少数错误文案会回显路径）。这不算差异 —— 折掉之后比的是语义。
    """
    if isinstance(obj, str):
        return obj.replace(repo, "<ROOT>")
    if isinstance(obj, list):
        return [norm(x, repo) for x in obj]
    if isinstance(obj, dict):
        return {k: norm(v, repo) for k, v in obj.items()}
    return obj


def post(port, path, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        r = urllib.request.urlopen(rq, timeout=120)
        code, payload = r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = {"__raw__": payload.decode("utf-8", "replace")}
    return code, js


# ---------------------------------------------------------------- 状态快照

def snapshot(repo):
    out = {}
    p = sh(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    out["head"] = p.stdout.strip() if p.returncode == 0 else None
    p = sh(repo, "symbolic-ref", "-q", "HEAD")
    out["head_ref"] = p.stdout.strip() if p.returncode == 0 else None
    out["refs"] = sorted(x for x in must(
        repo, "for-each-ref", "--format=%(refname)|%(objectname)").splitlines() if x)
    idx = sorted(x for x in must(repo, "ls-files", "-s").splitlines() if x)
    out["index"] = idx
    out["index_sha256"] = hashlib.sha256("\n".join(idx).encode("utf-8")).hexdigest()
    st = must(repo, "status", "--porcelain=v2", "--branch", "-uall")
    out["status"] = st.replace("\r\n", "\n")
    mh = os.path.join(repo, ".git", "MERGE_HEAD")
    out["merge_head"] = open(mh, encoding="utf-8").read() if os.path.isfile(mh) else None
    files = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo).replace("\\", "/")
            with open(full, "rb") as f:
                files[rel] = hashlib.sha256(f.read()).hexdigest()
    out["files"] = files
    return out


def diff_snapshots(a, b):
    bad = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            bad.append(k)
    return bad


# ---------------------------------------------------------------- 场景

def S(tag, build, steps, ep):
    return (tag, build, steps, ep)


SCENARIOS = [
    # ---- checkout ---------------------------------------------------
    S("checkout_ok", fx_base,
      [("/api/checkout", {"name": "feature"})], "/api/checkout"),
    S("checkout_missing", fx_base,
      [("/api/checkout", {"name": "nope"}),
       ("/api/checkout", {"name": "HEAD"}),
       ("/api/checkout", {"name": "a..b"}),
       ("/api/checkout", {"name": "中文分支"})], "/api/checkout"),
    S("checkout_dirty", fx_dirty,
      [("/api/checkout", {"name": "feature"}),
       ("/api/checkout", {"name": "feature", "status": False})], "/api/checkout"),
    S("checkout_unborn", fx_unborn,
      [("/api/checkout", {"name": "main"}),
       ("/api/checkout", {"name": "x"})], "/api/checkout"),
    S("checkout_detached", fx_detached,
      [("/api/checkout", {"name": "main"}),
       ("/api/checkout", {"name": "HEAD"})], "/api/checkout"),
    S("checkout_status_false", fx_base,
      [("/api/checkout", {"name": "feature", "status": False}),
       ("/api/checkout", {"name": "main", "status": False})], "/api/checkout"),
    S("checkout_names", fx_base,
      [("/api/checkout", {"name": ""}),
       ("/api/checkout", {"name": "a b"}),
       ("/api/checkout", {"name": "-x"}),
       ("/api/checkout", {"name": ".x"}),
       ("/api/checkout", {"name": "x" * 101}),
       ("/api/checkout", {"name": "x" * 100}),
       ("/api/checkout", {"name": 5}),
       ("/api/checkout", {"name": None}),
       ("/api/checkout", {}),
       ("/api/checkout", {"name": ["a"]}),
       ("/api/checkout", {"name": "feature "}),
       ("/api/checkout", {"name": "a\u0000b"})], "/api/checkout"),
    S("checkout_packed", fx_packed,
      [("/api/checkout", {"name": "feature"}),
       ("/api/checkout", {"name": "main"})], "/api/checkout"),
    S("checkout_badref", fx_badref,
      [("/api/checkout", {"name": "broken"}),
       ("/api/checkout", {"name": "main"})], "/api/checkout"),

    # ---- branch -----------------------------------------------------
    S("branch_ok", fx_plain,
      [("/api/branch", {"name": "feature2"}),
       ("/api/branch", {"name": "中文分支"}),
       ("/api/branch", {"name": "sub/nested-1"}),
       ("/api/branch", {"name": "y" * 100})], "/api/branch"),
    S("branch_bad", fx_plain,
      [("/api/branch", {"name": ""}),
       ("/api/branch", {"name": "a b"}),
       ("/api/branch", {"name": "-b"}),
       ("/api/branch", {"name": "x" * 101}),
       ("/api/branch", {"name": 5}),
       ("/api/branch", {}),
       ("/api/branch", {"name": "main"}),
       ("/api/branch", {"name": "main", "status": False})], "/api/branch"),
    S("branch_unborn", fx_unborn,
      [("/api/branch", {"name": "first"}),
       ("/api/branch", {"name": "first"})], "/api/branch"),

    # ---- merge ------------------------------------------------------
    S("merge_ff", fx_merge_clean,
      [("/api/merge", {"name": "feature"})], "/api/merge"),
    S("merge_threeway", fx_threeway,
      [("/api/merge", {"name": "feature"})], "/api/merge"),
    S("merge_uptodate", fx_plain,
      [("/api/merge", {"name": "main"})], "/api/merge"),
    S("merge_conflict", fx_conflict,
      [("/api/merge", {"name": "side"})], "/api/merge"),
    S("merge_conflict_new", fx_conflict_pending,
      [("/api/merge", {"name": "side"}),
       ("/api/merge", {"name": "side"})], "/api/merge"),
    S("merge_missing", fx_plain,
      [("/api/merge", {"name": "nope"}),
       ("/api/merge", {"name": "main"})], "/api/merge"),
    S("merge_names", fx_plain,
      [("/api/merge", {"name": ""}),
       ("/api/merge", {"name": "   "}),
       ("/api/merge", {}),
       ("/api/merge", {"name": 5}),
       ("/api/merge", {"name": None}),
       ("/api/merge", {"name": "main"}),
       ("/api/merge", {"name": "main", "status": False}),
       ("/api/merge", {"name": "a\u0000b"})], "/api/merge"),

    # ---- merge-abort ------------------------------------------------
    S("abort_in_merge", fx_conflict,
      [("/api/merge-abort", {})], "/api/merge-abort"),
    S("abort_clean", fx_plain,
      [("/api/merge-abort", {}),
       ("/api/merge-abort", {"status": False})], "/api/merge-abort"),

    # ---- reset ------------------------------------------------------
    S("reset_soft", fx_multi,
      [("/api/reset", {"mode": "soft", "confirmed": True, "ref": "HEAD~1"})],
      "/api/reset"),
    S("reset_mixed", fx_multi,
      [("/api/reset", {"mode": "mixed", "confirmed": True})], "/api/reset"),
    S("reset_hard_untracked", fx_multi,
      [("/api/reset", {"mode": "hard", "confirmed": True})], "/api/reset"),
    S("reset_hard_ref", fx_multi,
      [("/api/reset", {"mode": "hard", "confirmed": True, "ref": "HEAD~2"})],
      "/api/reset"),
    S("reset_ref_bad", fx_multi,
      [("/api/reset", {"mode": "soft", "confirmed": True, "ref": "nope"}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": "a:b"}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": "a..b"}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": "-x"}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": "a\u0000b"}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": 123}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": None}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": ""}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": 0}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": False}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": []}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": {}}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "ref": "HEAD~1"})],
      "/api/reset"),
    S("reset_mode_bad", fx_multi,
      [("/api/reset", {}),
       ("/api/reset", {"mode": "soft "}),
       ("/api/reset", {"mode": 5}),
       ("/api/reset", {"mode": 5.0}),
       ("/api/reset", {"mode": True}),
       ("/api/reset", {"mode": ["soft"]}),
       ("/api/reset", {"mode": "hard"}),
       ("/api/reset", {"mode": "soft", "confirmed": "false"}),
       ("/api/reset", {"mode": "soft", "confirmed": 1}),
       ("/api/reset", {"mode": "soft", "confirmed": False}),
       ("/api/reset", {"mode": "soft", "confirmed": True, "status": False}),
       ("/api/reset", {"mode": "mixed", "confirmed": True})], "/api/reset"),
    S("reset_body_bad", fx_plain,
      [("/api/reset", None, b"[]"),
       ("/api/reset", None, b"{")], "/api/reset"),
]


def run_scenario(tag, build, steps, ep, results, logdir):
    repo_a = os.path.join(BASE, tag, "py")
    repo_b = os.path.join(BASE, tag, "c")
    build(repo_a)
    build(repo_b)
    pa, pb = free_port(), free_port()
    la = open(os.path.join(logdir, "%s_py.log" % tag), "wb")
    lb = open(os.path.join(logdir, "%s_c.log" % tag), "wb")
    pp = start_py(repo_a, pa, la)
    pc = start_c(repo_b, pb, lb)
    fails = 0
    try:
        if not (wait_health(pa, pp) and wait_health(pb, pc)):
            print("  FAIL %-20s 服务没起来 (py=%s c=%s)" % (tag, pp.poll(), pc.poll()))
            results[(ep, tag)] = "FAIL"
            return 1
        for i, step in enumerate(steps):
            path, body = step[0], step[1]
            raw = step[2] if len(step) > 2 else None
            try:
                ca, ja = post(pa, path, body, raw)
                ja = norm(ja, repo_a)
            except Exception as e:
                ca, ja = "EXC", {"__err__": str(e)}
            try:
                cb, jb = post(pb, path, body, raw)
                jb = norm(jb, repo_b)
            except Exception as e:
                cb, jb = "EXC", {"__err__": str(e)}
            label = "%s #%d" % (path, i + 1)
            if ca == cb and ja == jb:
                print("  PASS %-20s %-16s code=%s" % (tag, label, ca))
            else:
                fails += 1
                print("  FAIL %-20s %-16s" % (tag, label))
                print("       request: %s" % json.dumps(
                    body if raw is None else raw.decode("utf-8", "replace"),
                    ensure_ascii=False))
                print("       python : code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:600]))
                print("       c      : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:600]))
        sa, sb = snapshot(repo_a), snapshot(repo_b)
        bad = diff_snapshots(sa, sb)
        if bad:
            fails += 1
            print("  FAIL %-20s 最终状态 %d 项不一致: %s" % (tag, len(bad), ", ".join(bad)))
            for k in bad:
                if k == "files":
                    ks = sorted(set(sa[k]) | set(sb[k]))
                    for f in ks:
                        if sa[k].get(f) != sb[k].get(f):
                            print("       files[%s]: py=%s c=%s" % (
                                f, (sa[k].get(f) or "-")[:12], (sb[k].get(f) or "-")[:12]))
                elif k in ("index", "refs"):
                    oa = [x for x in sa[k] if x not in sb[k]][:3]
                    ob = [x for x in sb[k] if x not in sa[k]][:3]
                    print("       %s py-only=%r c-only=%r" % (k, oa, ob))
                else:
                    print("       %-10s py=%r" % (k, sa.get(k)))
                    print("       %-10s c =%r" % ("", sb.get(k)))
        else:
            print("  PASS %-20s 最终状态一致 (head=%s ref=%s)" % (
                tag, (sa["head"] or "-")[:10], sa["head_ref"]))
        results[(ep, tag)] = "PASS" if fails == 0 else "FAIL"
        results[("STATE", tag)] = "PASS" if not bad else "FAIL"
        return fails
    finally:
        for p in (pp, pc):
            try:
                p.terminate()
                p.wait(10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        la.close()
        lb.close()


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    os.makedirs(BASE, exist_ok=True)
    logdir = os.path.join(BASE, "logs")
    os.makedirs(logdir, exist_ok=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    if not os.path.isfile(os.path.join(PY_ROOT, "server.py")):
        print("  缺 Python 参考版 %s" % PY_ROOT)
        return 1

    results = {}
    fails = 0
    try:
        for tag, build, steps, ep in SCENARIOS:
            print("[%s]" % tag)
            fails += run_scenario(tag, build, steps, ep, results, logdir)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    # 汇总表：按端点分组，行 = fixture
    print()
    print("汇总（端点 × fixture；reqs=该 fixture 下全部 POST 的码/JSON 是否逐项相等，"
          "state=最终仓库状态）")
    fails_by_ep = {}
    for tag, build, steps, ep in SCENARIOS:
        fails_by_ep.setdefault(ep, []).append(tag)
    for ep in ENDPOINTS:
        print()
        print("%s" % ep)
        print("  %-24s %-6s %-6s" % ("fixture", "reqs", "state"))
        for tag in fails_by_ep.get(ep, []):
            print("  %-24s %-6s %-6s" % (tag,
                                         results.get((ep, tag), "-"),
                                         results.get(("STATE", tag), "-")))
    print()
    print("失败 %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
