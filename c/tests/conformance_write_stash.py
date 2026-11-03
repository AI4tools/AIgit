# -*- coding: utf-8 -*-
"""M3 写面第三组一致性判据：C 版 vs Python 版
（history/stash、stash/apply、stash/pop、stash/drop）。

判据
====
* 同一个 fixture 建**两份相同副本**：A 给 Python 服务、B 给 C 服务；
  两边按**相同顺序**发同一批 POST；
* 每一步比 **HTTP 码 + JSON 深比较**（dict/list 结构逐项相等）；
* 收口再比**最终仓库状态**：
      - `git status --porcelain=v2 -uall`
      - `HEAD`（sha）与 `symbolic-ref HEAD`
      - 全部 refs（`for-each-ref refname|objectname`，含 refs/stash）
      - 索引（`ls-files -s`，另算 sha256）
      - 工作区每个文件的 sha256（不含 .git）
      - `git stash list`、`refs/stash` 的 sha、`git reflog show refs/stash`
      - `.git/logs/refs/stash` 的**原始字节**（reflog 时间戳由
        GIT_COMMITTER_DATE 固定，所以两边应当逐字相同）
* 身份与日期固定，所以 stash commit / reflog 也应当逐字相同。

不比的东西
=========
* `X-aigit-git-calls` / `X-aigit-git-cmds`：Python 的 `list_stashes` 走
  `.git/logs/refs/stash` 快路径，C 走 `git stash list`，spawn 次数本来就不同；
  响应与副作用必须相同。

用法
====
    python -X utf8 c/tests/conformance_write_stash.py
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
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m3ws_%d" % (os.getpid() % 100000))

ENDPOINTS = ["/api/history/stash", "/api/history/stash/apply",
             "/api/history/stash/pop", "/api/history/stash/drop"]

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

def fx_mixed(d):
    """一处未暂存 + 一处已暂存 + 一个未跟踪文件。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    w(d, "c.txt", "c1\n")
    commit(d, "c1")
    w(d, "a.txt", "a1-dirty\n")              # 未暂存
    w(d, "b.txt", "b1-staged\n")
    must(d, "add", "--", "b.txt")            # 已暂存
    w(d, "untracked.txt", "u\n")             # 未跟踪
    return d


def fx_staged(d):
    """只有已暂存改动 —— 配 --keep-index 可以反复 stash，树一直是脏的。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    w(d, "b.txt", "b1-staged\n")
    must(d, "add", "--", "b.txt")
    return d


def fx_untracked_only(d):
    """已跟踪文件全干净，只有未跟踪文件 + 未跟踪目录。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    w(d, "untracked.txt", "u\n")
    os.makedirs(os.path.join(d, "newdir"), exist_ok=True)
    w(d, "newdir/inner.txt", "i\n")
    return d


def fx_clean(d):
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    return d


def fx_unborn(d):
    return init_repo(d)


def fx_with_stash(d):
    """一个 stash，工作区干净。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    w(d, "a.txt", "a1-stashed\n")
    must(d, "stash", "push", "-q", "-m", "s-one")
    return d


def fx_two_stashes(d):
    """两个 stash：stash@{0}=s-b（改 b.txt），stash@{1}=s-a（改 a.txt）。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    w(d, "a.txt", "a1-stashed\n")
    must(d, "stash", "push", "-q", "-m", "s-a")
    w(d, "b.txt", "b1-stashed\n")
    must(d, "stash", "push", "-q", "-m", "s-b")
    return d


def fx_apply_conflict(d):
    """stash 的内容与当前 HEAD 改同一行 -> apply 必冲突。"""
    init_repo(d)
    w(d, "c.txt", "base\n")
    commit(d, "base")
    w(d, "c.txt", "stashed\n")
    must(d, "stash", "push", "-q", "-m", "s-conf")
    w(d, "c.txt", "current\n")
    commit(d, "current")
    return d


def fx_rename(d):
    """已暂存的改名（porcelain v1 -z 的 rename 记录带第二段原始路径 ——
    Python 的解析器会把那一段当独立记录，C 必须逐字复现这个行为）。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "b.txt", "b1\n")
    commit(d, "c1")
    must(d, "mv", "a.txt", "a2.txt")
    w(d, "untracked.txt", "u\n")
    return d


def fx_unicode(d):
    """非 ASCII 路径 + 空格：JSON 编码与 UTF-8 透传。"""
    init_repo(d)
    w(d, "中文 文件.txt", "one\n")
    w(d, "b.txt", "b\n")
    commit(d, "c1")
    w(d, "中文 文件.txt", "two\n")
    w(d, "新 文件.txt", "u\n")
    return d


def fx_unicode_stash(d):
    fx_unicode(d)
    must(d, "stash", "push", "-q", "-u", "-m", "u-中文")
    return d


def fx_many_stashes(d):
    """51 个 stash：验证 stash_count 的 limit=50 截断，以及 _stash_ref 的 200 档。"""
    init_repo(d)
    w(d, "a.txt", "0\n")
    commit(d, "c1")
    for i in range(51):
        w(d, "a.txt", "%d\n" % (i + 1))
        must(d, "stash", "push", "-q", "-m", "s%d" % i)
    return d


def fx_many_stashes_dirty(d):
    fx_many_stashes(d)
    w(d, "a.txt", "dirty\n")
    return d


def fx_unmerged(d):
    """合并冲突未解决（未合并索引项）—— stash push 在 git 里会失败。"""
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
    sh(d, "merge", "side")                   # 期望冲突，留下未合并项
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
    """把响应里的绝对仓路径折成 <ROOT>（两份副本目录不同）。"""
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

    # stash 面：列表 / ref / reflog / reflog 原始字节
    out["stash_list"] = must(repo, "stash", "list")
    p = sh(repo, "rev-parse", "--verify", "--quiet", "refs/stash")
    out["stash_ref"] = p.stdout.strip() if p.returncode == 0 else None
    p = sh(repo, "reflog", "show", "--format=%H %gs", "refs/stash")
    out["stash_reflog"] = p.stdout.replace("\r\n", "\n") if p.returncode == 0 else None
    rl = os.path.join(repo, ".git", "logs", "refs", "stash")
    if os.path.isfile(rl):
        with open(rl, "rb") as f:
            out["stash_log"] = f.read().decode("utf-8", "replace")
    else:
        out["stash_log"] = None

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

S = lambda tag, build, steps, ep: (tag, build, steps, ep)

SCENARIOS = [
    # ---- stash ------------------------------------------------------
    S("stash_mixed", fx_mixed,
      [("/api/history/stash", {"message": "m-one"})], "/api/history/stash"),
    S("stash_include_untracked", fx_mixed,
      [("/api/history/stash", {"message": "m-unt", "include_untracked": True})],
      "/api/history/stash"),
    S("stash_keep_index", fx_mixed,
      [("/api/history/stash", {"message": "m-keep", "keep_index": True})],
      "/api/history/stash"),
    S("stash_untracked_only_false", fx_untracked_only,
      [("/api/history/stash", {}),
       ("/api/history/stash", {"include_untracked": False})],
      "/api/history/stash"),
    S("stash_untracked_only_true", fx_untracked_only,
      [("/api/history/stash", {"include_untracked": True, "message": "u"}),
       ("/api/history/stash", {"include_untracked": True})],
      "/api/history/stash"),
    S("stash_clean", fx_clean,
      [("/api/history/stash", {}),
       ("/api/history/stash", {"include_untracked": True}),
       ("/api/history/stash", {"message": "x"})], "/api/history/stash"),
    S("stash_unborn", fx_unborn,
      [("/api/history/stash", {}),
       ("/api/history/stash", {"include_untracked": True})], "/api/history/stash"),
    S("stash_messages", fx_staged,
      [("/api/history/stash", {"message": "m-one", "keep_index": True}),
       ("/api/history/stash", {"message": "", "keep_index": True}),
       ("/api/history/stash", {"message": "中文说明", "keep_index": True}),
       ("/api/history/stash", {"message": "line1\nline2", "keep_index": True}),
       ("/api/history/stash", {"message": "a\"b\\c\td", "keep_index": True}),
       ("/api/history/stash", {"message": "  spaced  ", "keep_index": True}),
       ("/api/history/stash", {"keep_index": True}),
       ("/api/history/stash", {"message": "final"})], "/api/history/stash"),
    S("stash_msg_bad", fx_mixed,
      [("/api/history/stash", {"message": 5}),
       ("/api/history/stash", {"message": ["x"]}),
       ("/api/history/stash", {"message": True}),
       ("/api/history/stash", {"message": "a\u0000b"}),
       ("/api/history/stash", {"message": None}),
       ("/api/history/stash", {"include_untracked": "false"}),
       ("/api/history/stash", {"include_untracked": 1}),
       ("/api/history/stash", {"include_untracked": None}),
       ("/api/history/stash", {"keep_index": "true"}),
       ("/api/history/stash", {"keep_index": 0}),
       ("/api/history/stash", {"keep_index": 1.0}),
       ("/api/history/stash", {"message": "ok"})], "/api/history/stash"),
    S("stash_body_bad", fx_mixed,
      [("/api/history/stash", None, b"[]"),
       ("/api/history/stash", None, b"{"),
       ("/api/history/stash", None, b"\"x\""),
       ("/api/history/stash", None, b"null"),
       ("/api/history/stash", {"message": "after-bad"})], "/api/history/stash"),
    S("stash_unmerged", fx_unmerged,
      [("/api/history/stash", {"message": "from-conflict"})],
      "/api/history/stash"),
    S("stash_rename", fx_rename,
      [("/api/history/stash", {"message": "ren", "include_untracked": True})],
      "/api/history/stash"),
    S("stash_unicode", fx_unicode,
      [("/api/history/stash", {"message": "中文 stash", "include_untracked": True}),
       ("/api/history/stash", {"include_untracked": True})],
      "/api/history/stash"),
    S("stash_many_cap", fx_many_stashes_dirty,
      [("/api/history/stash", {"message": "m-cap"})], "/api/history/stash"),

    # ---- apply ------------------------------------------------------
    S("apply_no_stash", fx_clean,
      [("/api/history/stash/apply", {}),
       ("/api/history/stash/apply", {"index": 0}),
       ("/api/history/stash/apply", {"index": 1})], "/api/history/stash/apply"),
    S("apply_index_bad", fx_two_stashes,
      [("/api/history/stash/apply", {"index": -1}),
       ("/api/history/stash/apply", {"index": 1.0}),
       ("/api/history/stash/apply", {"index": "0"}),
       ("/api/history/stash/apply", {"index": True}),
       ("/api/history/stash/apply", {"index": None}),
       ("/api/history/stash/apply", {"index": [0]}),
       ("/api/history/stash/apply", {"index": 5}),
       ("/api/history/stash/apply", {"index": 2}),
       ("/api/history/stash/apply", {"index": 0}),
       ("/api/history/stash/apply", {"index": 1})], "/api/history/stash/apply"),
    S("apply_conflict", fx_apply_conflict,
      [("/api/history/stash/apply", {"index": 0}),
       ("/api/history/stash/apply", {"index": 0})], "/api/history/stash/apply"),
    S("apply_untracked_stash", fx_untracked_only,
      [("/api/history/stash", {"include_untracked": True, "message": "u"}),
       ("/api/history/stash/apply", {"index": 0}),
       ("/api/history/stash/apply", {"index": 0})], "/api/history/stash/apply"),
    S("apply_unicode", fx_unicode_stash,
      [("/api/history/stash/apply", {"index": 0})], "/api/history/stash/apply"),
    S("apply_index_50_of_51", fx_many_stashes,
      [("/api/history/stash/apply", {"index": 50})], "/api/history/stash/apply"),

    # ---- pop --------------------------------------------------------
    S("pop_confirm_gate", fx_with_stash,
      [("/api/history/stash/pop", {}),
       ("/api/history/stash/pop", {"confirmed": False}),
       ("/api/history/stash/pop", {"confirmed": "false"}),
       ("/api/history/stash/pop", {"confirmed": 1}),
       ("/api/history/stash/pop", {"confirmed": None}),
       ("/api/history/stash/pop", {"confirmed": True})], "/api/history/stash/pop"),
    S("pop_ok", fx_with_stash,
      [("/api/history/stash/pop", {"confirmed": True})], "/api/history/stash/pop"),
    S("pop_conflict", fx_apply_conflict,
      [("/api/history/stash/pop", {"confirmed": True})], "/api/history/stash/pop"),
    S("pop_no_stash", fx_clean,
      [("/api/history/stash/pop", {"confirmed": True}),
       ("/api/history/stash/pop", {"confirmed": True, "index": 0})],
      "/api/history/stash/pop"),
    S("pop_index_bad", fx_two_stashes,
      [("/api/history/stash/pop", {"confirmed": True, "index": 5}),
       ("/api/history/stash/pop", {"confirmed": True, "index": -1}),
       ("/api/history/stash/pop", {"confirmed": True, "index": 1.0}),
       ("/api/history/stash/pop", {"confirmed": True, "index": "1"}),
       ("/api/history/stash/pop", {"confirmed": True, "index": None}),
       ("/api/history/stash/pop", {"confirmed": True, "index": 1}),
       ("/api/history/stash/pop", {"confirmed": True, "index": 0})],
      "/api/history/stash/pop"),
    S("pop_gate_index_render", fx_two_stashes,
      [("/api/history/stash/pop", {"index": 1}),
       ("/api/history/stash/pop", {"index": "x"}),
       ("/api/history/stash/pop", {"index": 1.5}),
       ("/api/history/stash/pop", {"index": None}),
       ("/api/history/stash/pop", {"index": True}),
       ("/api/history/stash/pop", {"index": [1, "a"]})],
      "/api/history/stash/pop"),

    # ---- drop -------------------------------------------------------
    S("drop_confirm_gate", fx_with_stash,
      [("/api/history/stash/drop", {}),
       ("/api/history/stash/drop", {"confirmed": False}),
       ("/api/history/stash/drop", {"confirmed": "false"}),
       ("/api/history/stash/drop", {"confirmed": 0}),
       ("/api/history/stash/drop", {"confirmed": None}),
       ("/api/history/stash/drop", {"confirmed": True})],
      "/api/history/stash/drop"),
    S("drop_ok", fx_with_stash,
      [("/api/history/stash/drop", {"confirmed": True})], "/api/history/stash/drop"),
    S("drop_last_then_more", fx_with_stash,
      [("/api/history/stash/drop", {"confirmed": True}),
       ("/api/history/stash/drop", {"confirmed": True})], "/api/history/stash/drop"),
    S("drop_out_of_range", fx_two_stashes,
      [("/api/history/stash/drop", {"confirmed": True, "index": 2}),
       ("/api/history/stash/drop", {"confirmed": True, "index": -1}),
       ("/api/history/stash/drop", {"confirmed": True, "index": 1.5}),
       ("/api/history/stash/drop", {"confirmed": True, "index": "1"}),
       ("/api/history/stash/drop", {"confirmed": True, "index": 1}),
       ("/api/history/stash/drop", {"confirmed": True, "index": 0})],
      "/api/history/stash/drop"),
    S("drop_no_stash", fx_clean,
      [("/api/history/stash/drop", {"confirmed": True})], "/api/history/stash/drop"),
    S("drop_two", fx_two_stashes,
      [("/api/history/stash/drop", {"confirmed": True, "index": 1}),
       ("/api/history/stash/drop", {"confirmed": True, "index": 0})],
      "/api/history/stash/drop"),
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
            print("  FAIL %-26s 服务没起来 (py=%s c=%s)" % (tag, pp.poll(), pc.poll()))
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
                print("  PASS %-26s %-12s code=%s" % (tag, label, ca))
            else:
                fails += 1
                print("  FAIL %-26s %-12s" % (tag, label))
                print("       request: %s" % json.dumps(
                    body if raw is None else raw.decode("utf-8", "replace"),
                    ensure_ascii=False))
                print("       python : code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:900]))
                print("       c      : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:900]))
        sa, sb = snapshot(repo_a), snapshot(repo_b)
        bad = diff_snapshots(sa, sb)
        if bad:
            fails += 1
            print("  FAIL %-26s 最终状态 %d 项不一致: %s" % (tag, len(bad), ", ".join(bad)))
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
                elif k in ("stash_list", "stash_reflog", "stash_log", "status"):
                    print("       %s py=%r" % (k, sa.get(k)))
                    print("       %s c =%r" % ("", sb.get(k)))
                else:
                    print("       %-12s py=%r" % (k, sa.get(k)))
                    print("       %-12s c =%r" % ("", sb.get(k)))
        else:
            print("  PASS %-26s 最终状态一致 (stash=%r)" % (
                tag, (sa["stash_list"] or "").splitlines()[:1]))
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
    steps_total = 0
    try:
        for tag, build, steps, ep in SCENARIOS:
            print("[%s]" % tag)
            steps_total += len(steps)
            fails += run_scenario(tag, build, steps, ep, results, logdir)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    print()
    print("汇总（端点 × fixture；reqs=该 fixture 下全部 POST 的码/JSON 是否逐项相等，"
          "state=最终仓库状态）")
    for ep in ENDPOINTS:
        print()
        print("%s" % ep)
        print("  %-26s %-6s %-6s" % ("fixture", "reqs", "state"))
        for tag, build, steps, e2 in SCENARIOS:
            if e2 != ep:
                continue
            print("  %-26s %-6s %-6s" % (tag,
                                         results.get((ep, tag), "-"),
                                         results.get(("STATE", tag), "-")))
    print()
    print("%d 个 fixture / %d 个 POST 步骤，失败 %d" % (len(SCENARIOS), steps_total, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
