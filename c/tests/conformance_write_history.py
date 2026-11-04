# -*- coding: utf-8 -*-
"""M3 历史写第一组一致性判据：C 版 vs Python 版
（/api/history/amend、/api/history/undo-last-commit、/api/history/reflog/restore）。

判据
====
* 同一个 fixture 建**两份相同副本**：A 给 Python 服务、B 给 C 服务；
  两边按**相同顺序**发同一批 POST；
* 每一步比 **HTTP 码 + JSON 深比较**（dict/list 结构逐项相等，含错误响应的
  `error` / `code` / `op`）；
* 收口再比**最终仓库状态**：
      - `git status --porcelain=v2 --branch -uall`
      - `HEAD`（sha）与 `symbolic-ref HEAD`
      - 全部 refs（`for-each-ref refname|objectname`）
      - 索引（`ls-files -s`，另算 sha256）
      - 工作区每个文件的 sha256（不含 .git）
      - **reflog**（`reflog --format=%H|%gs`）—— 这三个端点都改写历史，
        reflog 是「副作用是否一致」最直接的证据
* 身份与日期固定（GIT_AUTHOR_DATE / GIT_COMMITTER_DATE + 仓内 user.*），
  所以两边写出的 commit sha 应当**逐字相同**，`output` 里的短 sha 也相同。

不比的东西
==========
* `X-aigit-git-calls` / `X-aigit-git-cmds` 计数头：C 版没实现 Python 的
  loose-ref / cat-file 快路径，spawn 次数本来就不同；结果必须相同。
* `.git/index` 的**字节**：`git status` 会顺手写索引扩展，比的是 `ls-files -s`
  的语义内容。

用法
====
    python -X utf8 c/tests/conformance_write_history.py
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
PY_ROOT = r"D:\project\aigit\python-ref"
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m3wh_%d" % (os.getpid() % 100000))

AMEND = "/api/history/amend"
UNDO = "/api/history/undo-last-commit"
RESTORE = "/api/history/reflog/restore"
ENDPOINTS = [AMEND, UNDO, RESTORE]

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

def fx_clean(d):
    """一次提交，工作区干净。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "sub/b.txt", "b1\n")
    commit(d, "c1")
    return d


def fx_staged(d):
    """一处已暂存 + 一处未暂存 + 一个未跟踪文件。"""
    fx_clean(d)
    w(d, "a.txt", "a2\n")
    must(d, "add", "--", "a.txt")
    w(d, "sub/b.txt", "b2\n")
    w(d, "untracked.txt", "u\n")
    return d


def fx_unstaged_only(d):
    """只有未暂存改动（amend 只能改信息）。"""
    fx_clean(d)
    w(d, "a.txt", "a-dirty\n")
    w(d, "untracked.txt", "u\n")
    return d


def fx_unborn(d):
    return init_repo(d)


def fx_detached(d):
    fx_clean(d)
    w(d, "a.txt", "a2\n")
    commit(d, "c2")
    must(d, "checkout", "-q", "--detach")
    return d


def fx_multi(d):
    """三次提交 + 混合脏状态（未暂存 / 已暂存 / 未跟踪）。"""
    init_repo(d)
    w(d, "a.txt", "l1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    w(d, "a.txt", "l2\n")
    commit(d, "c2")
    w(d, "a.txt", "l3\n")
    w(d, "b.txt", "b2\n")
    commit(d, "c3")
    w(d, "a.txt", "l3-dirty\n")
    w(d, "b.txt", "b2-staged\n")
    must(d, "add", "--", "b.txt")
    w(d, "untracked.txt", "u\n")
    return d


def fx_root(d):
    """只有根提交（HEAD~1 不存在）。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "only")
    return d


def fx_merge(d):
    """HEAD 是 merge 提交。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "base")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "b.txt", "b-feature\n")
    commit(d, "feature")
    must(d, "checkout", "-q", "main")
    w(d, "a.txt", "a-main\n")
    commit(d, "main2")
    must(d, "merge", "--no-edit", "feature")
    return d


def fx_reflog(d):
    """三次提交 + 一次 reset：reflog 有多条不同 sha 的条目。"""
    init_repo(d)
    w(d, "a.txt", "r1\n")
    commit(d, "r1")
    w(d, "a.txt", "r2\n")
    commit(d, "r2")
    w(d, "a.txt", "r3\n")
    commit(d, "r3")
    must(d, "reset", "-q", "--hard", "HEAD~1")
    w(d, "a.txt", "dirty\n")          # mode=hard 要把它抹掉
    return d


def fx_reflog_clean(d):
    """同上但工作区干净（mode=merge 的阳性路径）。"""
    init_repo(d)
    w(d, "a.txt", "r1\n")
    commit(d, "r1")
    w(d, "a.txt", "r2\n")
    commit(d, "r2")
    w(d, "a.txt", "r3\n")
    commit(d, "r3")
    must(d, "reset", "-q", "--hard", "HEAD~1")
    return d


def fx_conflict(d):
    """停在「冲突未解决」的合并态。"""
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
    sh(d, "merge", "side")            # 期望冲突，留下合并态
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


def wait_health(port, proc, timeout=30.0):
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
    """把响应里的**绝对仓路径**折成 <ROOT>（两份副本本来就在不同目录）。"""
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
    p = sh(repo, "reflog", "--format=%H|%gs")
    out["reflog"] = [] if p.returncode != 0 else [
        x for x in p.stdout.replace("\r\n", "\n").split("\n") if x]
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
    return [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


# ---------------------------------------------------------------- 场景

SCENARIOS = [
    # ---- amend -------------------------------------------------------
    ("amend_staged", fx_staged,
     [(AMEND, {"message": "修正信息", "confirmed": True}),
      (AMEND, {"confirmed": True})]),
    ("amend_unstaged", fx_unstaged_only,
     [(AMEND, {"message": "only message", "confirmed": True})]),
    ("amend_clean", fx_clean,
     [(AMEND, {"confirmed": True}),
      (AMEND, {"message": "second", "confirmed": True})]),
    ("amend_unborn", fx_unborn,
     [(AMEND, {"confirmed": True}),
      (AMEND, {"message": "x", "confirmed": True})]),
    ("amend_detached", fx_detached,
     [(AMEND, {"message": "detached amend", "confirmed": True})]),
    ("amend_msgs", fx_clean,
     [(AMEND, {"message": "", "confirmed": True}),
      (AMEND, {"message": "line1\nline2\nline3", "confirmed": True}),
      (AMEND, {"message": "中文提交信息 ok", "confirmed": True}),
      (AMEND, {"message": 5, "confirmed": True}),
      (AMEND, {"message": None, "confirmed": True}),
      (AMEND, {"message": ["a"], "confirmed": True}),
      (AMEND, {"message": "a\u0000b", "confirmed": True}),
      (AMEND, {"message": "final", "confirmed": True})]),
    ("amend_confirm", fx_clean,
     [(AMEND, {"message": "x"}),
      (AMEND, {"message": "x", "confirmed": False}),
      (AMEND, {"message": "x", "confirmed": "false"}),
      (AMEND, {"message": "x", "confirmed": 1}),
      (AMEND, {"message": "x", "confirmed": None}),
      (AMEND, {"message": "x", "confirmed": True, "status": False})]),
    ("amend_body_bad", fx_clean,
     [(AMEND, None, b"[]"),
      (AMEND, None, b"null"),
      (AMEND, None, b"{"),
      (AMEND, {"message": "ok", "confirmed": True})]),
    ("amend_edge", fx_clean,
     [(AMEND, {"message": " ", "confirmed": True}),
      (AMEND, {"message": "  padded  ", "confirmed": True}),
      (AMEND, {"message": True, "confirmed": True}),
      (AMEND, {"message": 5.0, "confirmed": True}),
      (AMEND, {"message": {}, "confirmed": True}),
      (AMEND, {"message": "emoji \U0001F600 tail", "confirmed": True}),
      (AMEND, {"message": "x", "confirmed": 1.0}),
      (AMEND, {"message": "x", "confirmed": 0.5}),
      (AMEND, {"message": "x", "confirmed": {}}),
      (AMEND, {"message": "x", "confirmed": []}),
      (AMEND, {"message": "x", "confirmed": {"a": [1, 2], "b": "c"}}),
      (AMEND, {"message": "x", "confirmed": True})]),
    ("amend_unborn_msg", fx_unborn,
     [(AMEND, {"message": 5, "confirmed": True}),
      (AMEND, {"message": "x", "confirmed": True})]),
    ("amend_conflict", fx_conflict,
     [(AMEND, {"message": "during merge", "confirmed": True})]),

    # ---- undo-last-commit --------------------------------------------
    ("undo_soft", fx_multi,
     [(UNDO, {"confirmed": True}),
      (UNDO, {"confirmed": True})]),
    ("undo_drop", fx_multi,
     [(UNDO, {"drop_changes": True, "confirmed": True})]),
    ("undo_root", fx_root,
     [(UNDO, {"confirmed": True}),
      (UNDO, {"drop_changes": True, "confirmed": True})]),
    ("undo_merge", fx_merge,
     [(UNDO, {"confirmed": True})]),
    ("undo_unborn", fx_unborn,
     [(UNDO, {"confirmed": True}),
      (UNDO, {"drop_changes": True, "confirmed": True})]),
    ("undo_confirm", fx_multi,
     [(UNDO, {"drop_changes": True}),
      (UNDO, {}),
      (UNDO, {"confirmed": "true"}),
      (UNDO, {"drop_changes": "yes", "confirmed": True}),
      (UNDO, {"drop_changes": 1, "confirmed": True}),
      (UNDO, {"drop_changes": True, "confirmed": True})]),
    ("undo_body_bad", fx_multi,
     [(UNDO, None, b"[]"),
      (UNDO, None, b"{"),
      (UNDO, {"confirmed": True})]),
    ("undo_conflict", fx_conflict,
     [(UNDO, {"confirmed": True})]),
    ("undo_conflict_drop", fx_conflict,
     [(UNDO, {"drop_changes": True, "confirmed": True})]),

    # ---- reflog restore ----------------------------------------------
    ("restore_soft", fx_reflog,
     [(RESTORE, {"index": 3, "mode": "soft", "confirmed": True}),
      (RESTORE, {"index": 0, "mode": "soft", "confirmed": True})]),
    ("restore_mixed", fx_reflog,
     [(RESTORE, {"index": 2, "mode": "mixed", "confirmed": True})]),
    ("restore_hard", fx_reflog,
     [(RESTORE, {"index": 2, "mode": "hard", "confirmed": True})]),
    ("restore_default_mode", fx_reflog,
     [(RESTORE, {"index": 1, "confirmed": True})]),
    ("restore_bad_index", fx_reflog,
     [(RESTORE, {"index": 99, "confirmed": True}),
      (RESTORE, {"index": -1, "confirmed": True}),
      (RESTORE, {"index": "1", "confirmed": True}),
      (RESTORE, {"index": 1.5, "confirmed": True}),
      (RESTORE, {"index": 5.0, "confirmed": True}),
      (RESTORE, {"index": True, "confirmed": True}),
      (RESTORE, {"index": None, "confirmed": True}),
      (RESTORE, {"index": 0, "confirmed": True})]),
    ("restore_bad_mode", fx_reflog,
     [(RESTORE, {"mode": "softly", "confirmed": True}),
      (RESTORE, {"mode": 5, "confirmed": True}),
      (RESTORE, {"mode": None, "confirmed": True}),
      (RESTORE, {"mode": "HARD", "confirmed": True}),
      (RESTORE, {"mode": "soft", "confirmed": True})]),
    ("restore_empty", fx_unborn,
     [(RESTORE, {"confirmed": True})]),
    ("restore_confirm", fx_reflog,
     [(RESTORE, {"index": 0}),
      (RESTORE, {"index": 0, "confirmed": False}),
      (RESTORE, {"index": 0, "confirmed": "false"}),
      (RESTORE, {"index": 0, "confirmed": True})]),
    ("restore_body_bad", fx_reflog,
     [(RESTORE, None, b"[]"),
      (RESTORE, None, b"{"),
      (RESTORE, {"index": 0, "confirmed": True})]),
    ("restore_merge", fx_reflog_clean,
     [(RESTORE, {"index": 3, "mode": "merge", "confirmed": True})]),
    ("restore_merge_dirty", fx_reflog,
     [(RESTORE, {"index": 3, "mode": "merge", "confirmed": True})]),
    ("restore_edge_index", fx_reflog_clean,
     [(RESTORE, {"index": 1e20, "confirmed": True}),
      (RESTORE, {"index": -0.0, "confirmed": True}),
      (RESTORE, {"index": [], "confirmed": True}),
      (RESTORE, {"index": {}, "confirmed": True}),
      (RESTORE, {"index": 1, "mode": "soft", "confirmed": True})]),
]


def run_scenario(tag, build, steps, results, logdir):
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
            for ep in set(s[0] for s in steps):
                results[(ep, tag)] = "FAIL"
            results[("STATE", tag)] = "FAIL"
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
            label = "%s #%d" % (path.rsplit("/", 1)[-1], i + 1)
            if ca == cb and ja == jb:
                print("  PASS %-20s %-24s code=%s" % (tag, label, ca))
            else:
                fails += 1
                print("  FAIL %-20s %-24s" % (tag, label))
                print("       request: %s" % json.dumps(
                    body if raw is None else raw.decode("utf-8", "replace"),
                    ensure_ascii=False))
                print("       python : code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:700]))
                print("       c      : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:700]))
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
                elif k in ("index", "refs", "reflog"):
                    oa = [x for x in sa[k] if x not in sb[k]][:4]
                    ob = [x for x in sb[k] if x not in sa[k]][:4]
                    print("       %s py-only=%r" % (k, oa))
                    print("       %s c-only =%r" % (k, ob))
                else:
                    print("       %-10s py=%r" % (k, sa.get(k)))
                    print("       %-10s c =%r" % ("", sb.get(k)))
        else:
            print("  PASS %-20s 最终状态一致 (head=%s ref=%s reflog=%d)" % (
                tag, (sa["head"] or "-")[:10], sa["head_ref"], len(sa["reflog"])))
        for ep in set(s[0] for s in steps):
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
        for tag, build, steps in SCENARIOS:
            print("[%s]" % tag)
            fails += run_scenario(tag, build, steps, results, logdir)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    print()
    print("汇总（端点 × fixture；reqs=该 fixture 下全部 POST 的码/JSON 是否逐项相等，"
          "state=最终仓库状态含 reflog）")
    for ep in ENDPOINTS:
        print()
        print("%s" % ep)
        print("  %-24s %-6s %-6s" % ("fixture", "reqs", "state"))
        for tag, build, steps in SCENARIOS:
            if not any(s[0] == ep for s in steps):
                continue
            print("  %-24s %-6s %-6s" % (tag,
                                         results.get((ep, tag), "-"),
                                         results.get(("STATE", tag), "-")))
    print()
    print("失败 %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
