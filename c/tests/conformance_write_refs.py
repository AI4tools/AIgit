# -*- coding: utf-8 -*-
"""M3 引用/历史写第二组一致性判据：C 版 vs Python 版。

覆盖端点
========
    /api/history/revert          /api/history/revert/abort
    /api/history/branch/rename   /api/history/branch/delete
    /api/history/tag/create      /api/history/tag/delete

判据
====
* 同一个 fixture 建**两份相同副本**：A 给 Python 服务、B 给 C 服务；
  两边按**相同顺序**发同一批 POST；
* 每一步比 **HTTP 码 + JSON 深比较**（dict/list 结构逐项相等，含错误文案、
  `code`、`extra`：conflicted / output / unmerged_count / commits / hint / op）；
* 收口再比**最终仓库状态**：
      - `git status --porcelain=v2 --branch -uall`
      - `HEAD`（sha）与 `symbolic-ref HEAD`
      - 全部 refs（`for-each-ref refname|objectname|symref`，含标签对象 sha）
      - 索引（`ls-files -s`，另算 sha256）
      - **reflog**（`.git/logs` 下每个文件的逐行内容，含 branch/tag 操作写的条目）
      - 工作区每个文件的 sha256（不含 .git）
      - revert 中途态（`.git/REVERT_HEAD` / `.git/sequencer`）与 `.git/MERGE_HEAD`
* 身份与日期固定（GIT_AUTHOR_DATE / GIT_COMMITTER_DATE + 仓内 user.*）：
  revert 生成的提交、注解标签对象的 sha、reflog 的时间戳都应当**逐字相同**，
  所以 head / refs / reflog 三项是可比的。

不比的东西
==========
* `X-aigit-git-calls` / `X-aigit-git-cmds` 计数头：C 版不实现 Python 的
  refs 文件快路径（gitrefs.*），spawn 次数本来就会不同；结果必须相同。
* `.git/index` 的**字节**：`git status` 会顺手写索引扩展（TREE/untracked cache），
  两边 status 调用次数不同，字节不同是正常的；比的是 `ls-files -s` 的语义内容。

用法
====
    python -X utf8 c/tests/conformance_write_refs.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import hashlib
import json
import os
import random
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
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m3wr_%d" % (os.getpid() % 100000))

ENDPOINTS = ["/api/history/revert", "/api/history/revert/abort",
             "/api/history/branch/rename", "/api/history/branch/delete",
             "/api/history/tag/create", "/api/history/tag/delete"]

# 固定身份/日期：两边写出的 commit / 注解标签 / reflog 时间戳才能逐字比
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


def rev(repo, r):
    return must(repo, "rev-parse", r).strip()


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

def fx_revert_plain(d):
    """线性三个提交：c1 -> c2(改 a.txt) -> c3(加 b.txt)。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    w(d, "a.txt", "a2\n")
    commit(d, "c2")
    w(d, "b.txt", "b1\n")
    commit(d, "c3")
    return {"c1": rev(d, "HEAD~2"), "c2": rev(d, "HEAD~1"), "c3": rev(d, "HEAD")}


def fx_merge(d):
    """一个 --no-ff merge 提交（两个父提交）。"""
    init_repo(d)
    w(d, "base.txt", "base\n")
    commit(d, "c1")
    must(d, "checkout", "-q", "-b", "side")
    w(d, "s.txt", "s\n")
    commit(d, "s1")
    must(d, "checkout", "-q", "main")
    w(d, "m.txt", "m\n")
    commit(d, "m1")
    must(d, "merge", "--no-ff", "-q", "-m", "merge side", "side")
    return {"c1": rev(d, "HEAD~2"), "merge": rev(d, "HEAD")}


def fx_revert_conflict(d):
    """side 与 main 各自改 a.txt 的同一行 -> revert side 的提交会冲突。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    w(d, "k.txt", "k\n")
    commit(d, "c1")
    must(d, "checkout", "-q", "-b", "side")
    w(d, "a.txt", "side\n")
    commit(d, "side1")
    must(d, "checkout", "-q", "main")
    w(d, "a.txt", "main\n")
    commit(d, "main1")
    return {"side1": rev(d, "side"), "c1": rev(d, "HEAD~1")}


def fx_unborn(d):
    """未出生 HEAD：init 之后一次提交都没有。"""
    init_repo(d)
    return {}


def fx_branches(d):
    """main(c1) + merged1(在 c1) + feature(c1+f1) + side(c1+s1)。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    must(d, "branch", "merged1")
    must(d, "checkout", "-q", "-b", "feature")
    w(d, "f.txt", "f\n")
    commit(d, "f1")
    must(d, "checkout", "-q", "-b", "side", "main")
    w(d, "s.txt", "s\n")
    commit(d, "s1")
    must(d, "checkout", "-q", "main")
    return {"c1": rev(d, "main")}


def fx_onebranch(d):
    """只有一个分支：删除它必须被「唯一分支」那道闸挡下。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    return {}


def fx_detached(d):
    """游离 HEAD，且 feature 与 HEAD 同一个 sha（_is_current_branch 走 sha 比较）。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    must(d, "branch", "feature")
    must(d, "checkout", "-q", "--detach", "HEAD")
    return {}


def fx_packed(d):
    """refs 全部打包进 packed-refs（读 refs 的路径与松散 ref 不同）。"""
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    must(d, "branch", "b1")
    must(d, "branch", "b2")
    must(d, "pack-refs", "--all")
    return {}


def fx_tags(d):
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    w(d, "a.txt", "a2\n")
    commit(d, "c2")
    return {"c1": rev(d, "HEAD~1"), "c2": rev(d, "HEAD")}


def fx_plain(d):
    init_repo(d)
    w(d, "a.txt", "a1\n")
    commit(d, "c1")
    return {}


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
    """把响应里的**绝对仓路径**折成 <ROOT>（两份副本活在不同目录下）。"""
    if isinstance(obj, str):
        return obj.replace(repo, "<ROOT>").replace(repo.replace("\\", "/"), "<ROOT>")
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

def read_text(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def snapshot(repo):
    out = {}
    p = sh(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    out["head"] = p.stdout.strip() if p.returncode == 0 else None
    p = sh(repo, "symbolic-ref", "-q", "HEAD")
    out["head_ref"] = p.stdout.strip() if p.returncode == 0 else None
    out["refs"] = sorted(x for x in must(
        repo, "for-each-ref",
        "--format=%(refname)|%(objectname)|%(symref)").splitlines() if x)
    idx = sorted(x for x in must(repo, "ls-files", "-s").splitlines() if x)
    out["index"] = idx
    out["index_sha256"] = hashlib.sha256("\n".join(idx).encode("utf-8")).hexdigest()
    st = must(repo, "status", "--porcelain=v2", "--branch", "-uall")
    out["status"] = st.replace("\r\n", "\n")
    # reflog：.git/logs 下每个文件（HEAD + refs/heads/* + refs/remotes/* …）
    logs = {}
    logs_root = os.path.join(repo, ".git", "logs")
    for dirpath, _dirnames, filenames in os.walk(logs_root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, logs_root).replace("\\", "/")
            logs[rel] = read_text(full)
    out["reflog"] = logs
    # revert 中途态 / 合并态
    out["revert_head"] = read_text(os.path.join(repo, ".git", "REVERT_HEAD"))
    out["merge_head"] = read_text(os.path.join(repo, ".git", "MERGE_HEAD"))
    out["sequencer"] = os.path.isdir(os.path.join(repo, ".git", "sequencer"))
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


def _expand(v, sha_map):
    """`@c1` / `@c1~` / `@c1^{commit}` -> 真实 sha + 后缀。"""
    if not isinstance(v, str) or not v.startswith("@"):
        return v
    body = v[1:]
    for k in sorted(sha_map, key=len, reverse=True):
        if not body.startswith(k):
            continue
        rest = body[len(k):]
        if rest and (rest[0].isalnum() or rest[0] == "_"):
            continue
        return sha_map[k] + rest
    return v


def mk(sha_map, *items):
    """把 fixture 里的 sha 名字替换进请求体（{sha:"@c1"} 这种写法）。"""
    out = []
    for it in items:
        path, body = it[0], it[1]
        raw = it[2] if len(it) > 2 else None
        if isinstance(body, dict):
            b = {}
            for k, v in body.items():
                b[k] = _expand(v, sha_map)
            body = b
        out.append((path, body, raw))
    return out


SCENARIOS = [
    # ---- revert ------------------------------------------------------
    S("revert_linear", fx_revert_plain,
      lambda m: mk(m,
                   ("/api/history/revert", {"sha": "@c2"}),
                   ("/api/history/revert", {"sha": "@c2"}),
                   ("/api/history/revert", {"sha": "@c1"}),
                   ("/api/history/revert", {"sha": "@c3", "status": False}),
                   ("/api/history/revert", {"sha": "@c3", "no_edit": False}),
                   ("/api/history/revert", {"sha": "@c3", "no_edit": "false"}),
                   ("/api/history/revert/abort", {})),
      "/api/history/revert"),
    S("revert_bad", fx_revert_plain,
      lambda m: mk(m,
                   ("/api/history/revert", {"sha": "deadbeef"}),
                   ("/api/history/revert", {"sha": "@c1~"}),
                   ("/api/history/revert", {"sha": "-x"}),
                   ("/api/history/revert", {"sha": ""}),
                   ("/api/history/revert", {}),
                   ("/api/history/revert", {"sha": 5}),
                   ("/api/history/revert", {"sha": None}),
                   ("/api/history/revert", {"sha": ["@c1"]}),
                   ("/api/history/revert", {"sha": "HEAD"}),
                   ("/api/history/revert", {"sha": "a" * 65}),
                   ("/api/history/revert", {"sha": "@c1", "mainline": 1}),
                   ("/api/history/revert", {"sha": "@c1\u0000x"}),
                   ("/api/history/revert", {"sha": "提交"}),
                   ("/api/history/revert", {"sha": "@c1^{commit}"}),
                   ("/api/history/revert", {"sha": "@c1:./a.txt"})),
      "/api/history/revert"),
    S("revert_merge", fx_merge,
      lambda m: mk(m,
                   ("/api/history/revert", {"sha": "@merge"}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": 3}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": 0}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": -1}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": "1"}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": True}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": 1.0}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": None}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": 2}),
                   ("/api/history/revert", {"sha": "@merge", "mainline": 1})),
      "/api/history/revert"),
    S("revert_conflict", fx_revert_conflict,
      lambda m: mk(m,
                   ("/api/history/revert", {"sha": "@side1"}),
                   ("/api/history/revert", {"sha": "@side1"}),
                   ("/api/history/revert", {"sha": "@c1"}),
                   ("/api/history/revert/abort", {}),
                   ("/api/history/revert/abort", {})),
      "/api/history/revert"),
    S("revert_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/revert", {"sha": "HEAD"}),
                   ("/api/history/revert", {}),
                   ("/api/history/revert/abort", {})),
      "/api/history/revert"),

    # ---- revert abort（单独成行，覆盖有/无 revert 中途态） ------------
    S("abort_conflict_then_abort", fx_revert_conflict,
      lambda m: mk(m,
                   ("/api/history/revert/abort", {}),
                   ("/api/history/revert", {"sha": "@side1"}),
                   ("/api/history/revert/abort", {}),
                   ("/api/history/revert/abort", {}),
                   ("/api/history/revert", {"sha": "@side1"}),
                   ("/api/history/revert/abort", {})),
      "/api/history/revert/abort"),
    S("abort_clean", fx_plain,
      lambda m: mk(m,
                   ("/api/history/revert/abort", {}),
                   ("/api/history/revert/abort", {"status": False})),
      "/api/history/revert/abort"),
    S("abort_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/revert/abort", {})),
      "/api/history/revert/abort"),

    # ---- branch rename ----------------------------------------------
    S("rename_basic", fx_branches,
      lambda m: mk(m,
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "side", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "nope", "new": "x", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "a b", "new": "x", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "-x", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": ".x", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "x.", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "a..b", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "a//b", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "a@{b", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "x.lock", "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "x" * 101, "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "feature"}),
                   ("/api/history/branch/rename",
                    {"old": 5, "new": "y", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2"}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2", "confirmed": False}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2", "confirmed": "false"}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2", "confirmed": 1}),
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "main", "new": "main2", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "feature2", "new": "中文分支", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "中文分支", "new": "中" * 101, "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "中文分支", "new": "中" * 100, "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": " " + "中" * 100 + " ", "new": "feature3",
                     "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "main2", "new": "side", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "main2", "new": "main2", "confirmed": True})),
      "/api/history/branch/rename"),
    S("rename_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/branch/rename",
                    {"old": "main", "new": "x", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "a b", "new": "x", "confirmed": True})),
      "/api/history/branch/rename"),
    S("rename_packed", fx_packed,
      lambda m: mk(m,
                   ("/api/history/branch/rename",
                    {"old": "b1", "new": "b3", "confirmed": True}),
                   ("/api/history/branch/rename",
                    {"old": "b3", "new": "b2", "confirmed": True})),
      "/api/history/branch/rename"),
    S("rename_detached", fx_detached,
      lambda m: mk(m,
                   ("/api/history/branch/rename",
                    {"old": "feature", "new": "feature2", "confirmed": True})),
      "/api/history/branch/rename"),

    # ---- branch delete ----------------------------------------------
    S("delete_basic", fx_branches,
      lambda m: mk(m,
                   ("/api/history/branch/delete",
                    {"name": " merged1 ", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "merged1", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": " feature ", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "side", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "side", "force": True, "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "nope", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "a b", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "main", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "force": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "force": "yes", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "force": 1, "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "force": True, "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "feature", "force": True, "confirmed": True})),
      "/api/history/branch/delete"),
    S("delete_onebranch", fx_onebranch,
      lambda m: mk(m,
                   ("/api/history/branch/delete",
                    {"name": "main", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "main", "force": True, "confirmed": True})),
      "/api/history/branch/delete"),
    S("delete_detached", fx_detached,
      lambda m: mk(m,
                   ("/api/history/branch/delete",
                    {"name": "feature", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "main", "confirmed": True})),
      "/api/history/branch/delete"),
    S("delete_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/branch/delete",
                    {"name": "main", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "a b", "confirmed": True})),
      "/api/history/branch/delete"),
    S("delete_packed", fx_packed,
      lambda m: mk(m,
                   ("/api/history/branch/delete",
                    {"name": "b2", "confirmed": True}),
                   ("/api/history/branch/delete",
                    {"name": "b2", "confirmed": True})),
      "/api/history/branch/delete"),

    # ---- tag create --------------------------------------------------
    S("tag_create", fx_tags,
      lambda m: mk(m,
                   ("/api/history/tag/create", {"name": "v1"}),
                   ("/api/history/tag/create", {"name": "v2", "message": "hello tag"}),
                   ("/api/history/tag/create", {"name": "v3", "sha": "@c1"}),
                   ("/api/history/tag/create",
                    {"name": "v4", "sha": "@c1", "message": "m4"}),
                   ("/api/history/tag/create", {"name": "v1"}),
                   ("/api/history/tag/create", {"name": "v1", "force": True}),
                   ("/api/history/tag/create",
                    {"name": "v1", "force": True, "message": "now annotated"}),
                   ("/api/history/tag/create", {"name": "a b"}),
                   ("/api/history/tag/create", {"name": ""}),
                   ("/api/history/tag/create", {}),
                   ("/api/history/tag/create", {"name": 5}),
                   ("/api/history/tag/create", {"name": None}),
                   ("/api/history/tag/create", {"name": "x" * 101}),
                   ("/api/history/tag/create", {"name": "v5", "sha": "nope"}),
                   ("/api/history/tag/create", {"name": "v5", "sha": "-x"}),
                   ("/api/history/tag/create", {"name": "v5", "sha": 5}),
                   ("/api/history/tag/create", {"name": "v5", "message": 5}),
                   ("/api/history/tag/create", {"name": "v5", "message": ""}),
                   ("/api/history/tag/create", {"name": "v6", "sha": ""}),
                   ("/api/history/tag/create",
                    {"name": "v7", "force": "true"}),
                   ("/api/history/tag/create",
                    {"name": "v7", "force": True, "sha": "@c2",
                     "message": "annotated at c2"}),
                   ("/api/history/tag/create", {"name": "rel/v1"}),
                   ("/api/history/tag/create", {"name": "中文标签"}),
                   ("/api/history/tag/create", {"name": "v8", "sha": "@c1~"}),
                   ("/api/history/tag/create", {"name": "v9", "sha": "@c1\u0000x"}),
                   ("/api/history/tag/create", {"name": "v10", "sha": "提交"}),
                   ("/api/history/tag/create",
                    {"name": "v11", "message": "多行\n说明"}),
                   ("/api/history/tag/create",
                    {"name": "v12", "message": "x" * 200})),
      "/api/history/tag/create"),
    S("tag_create_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/tag/create", {"name": "v1"}),
                   ("/api/history/tag/create", {"name": "a b"})),
      "/api/history/tag/create"),

    # ---- tag delete --------------------------------------------------
    S("tag_delete", fx_tags,
      lambda m: mk(m,
                   ("/api/history/tag/create", {"name": "v1"}),
                   ("/api/history/tag/create", {"name": "v2", "message": "two"}),
                   ("/api/history/tag/delete", {"name": "v1", "confirmed": True}),
                   ("/api/history/tag/delete", {"name": "v1", "confirmed": True}),
                   ("/api/history/tag/delete", {"name": "a b", "confirmed": True}),
                   ("/api/history/tag/delete", {"name": "v2"}),
                   ("/api/history/tag/delete", {"name": "v2", "confirmed": False}),
                   ("/api/history/tag/delete",
                    {"name": "v2", "confirmed": "false"}),
                   ("/api/history/tag/delete", {"name": "v2", "confirmed": True}),
                   ("/api/history/tag/delete", {"name": "v2", "confirmed": True})),
      "/api/history/tag/delete"),
    S("tag_delete_unborn", fx_unborn,
      lambda m: mk(m,
                   ("/api/history/tag/delete", {"name": "v1", "confirmed": True}),
                   ("/api/history/tag/delete", {"name": "a b", "confirmed": True})),
      "/api/history/tag/delete"),

    # ---- 坏请求体（共享解析路径，六个端点各来一遍） ------------------
    S("body_bad", fx_plain,
      lambda m: [
          ("/api/history/revert", None, b"[]"),
          ("/api/history/revert", None, b"{"),
          ("/api/history/revert", None, b'"x"'),
          ("/api/history/revert", None, b"123"),
          ("/api/history/revert/abort", None, b"null"),
          ("/api/history/branch/rename", None, b"[1,2]"),
          ("/api/history/branch/delete", None, b"["),
          ("/api/history/tag/create", None, b"true"),
          ("/api/history/tag/delete", None, b"1.5"),
          ("/api/history/revert", {}),
      ],
      "/api/history/revert"),
]


def fuzz_phase(results):
    """随机/畸形 body：两边灌同一批，逐条比码 + JSON。

    **故意不放进语料的两类**（差异来自共享的 bodyjson 层，不在本文件范围）：
      * 非法 UTF-8 的原始 body（`b"\\xff\\xfe"`）：两边都 400，但 decode 错误文案
        不同（Python 报 byte/position，C 报 "invalid data"）；
      * JSON 里的孤立代理 `"\\ud800"`：Python 在回写响应时二次抛错、**连接直接
        断开**（无响应），C 回 400 但文案里带非法 UTF-8 字节。
    两条都只影响「非 Unicode 标量」的输入，正常 JSON 到不了。
    """
    base = os.path.join(BASE, "fuzz")
    repo_a = os.path.join(base, "py")
    repo_b = os.path.join(base, "c")
    ctx_a = fx_branches(repo_a)
    ctx_b = fx_branches(repo_b)
    if ctx_a != ctx_b:
        print("  FAIL %-20s fixture 两边 sha 不一致" % "fuzz")
        results[("FUZZ", "random_bodies")] = "FAIL"
        return 1
    pa, pb = free_port(), free_port()
    la = open(os.path.join(BASE, "logs", "fuzz_py.log"), "wb")
    lb = open(os.path.join(BASE, "logs", "fuzz_c.log"), "wb")
    pp = start_py(repo_a, pa, la)
    pc = start_c(repo_b, pb, lb)
    rnd = random.Random(20261103)
    bad = 0
    n = 0
    try:
        if not (wait_health(pa, pp) and wait_health(pb, pc)):
            print("  FAIL %-20s 服务没起来" % "fuzz")
            results[("FUZZ", "random_bodies")] = "FAIL"
            return 1
        cases = []
        for raw in (b"", b"{", b"}", b"[]", b"null", b"1e999", b'"x"',
                    b'{"a":}', b'{"sha":}', b'{"name":' + b"[" * 40,
                    b'{"x":1}{"y":2}'):
            for ep in ENDPOINTS:
                cases.append((ep, raw))
        vals = [None, True, False, 0, 1, -1, 2, 1.5, 1e300, "", " ", "main",
                "feature", "HEAD", "HEAD~1", "-x", ".x", "x.", "a b", "a..b",
                "a//b", "a@{b", "@", "x.lock", "x" * 101, "中" * 40,
                "中" * 101, "\u0000", "a\u0000b", "refs/heads/main",
                "v1", "rel/v1", "deadbeef",
                "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
                ["a"], {"a": 1}, 0.0, ctx_a["c1"], "merged1", "side"]
        keys = ["sha", "mainline", "no_edit", "old", "new", "name", "force",
                "confirmed", "message", "status"]
        for _ in range(400):
            ep = rnd.choice(ENDPOINTS)
            body = {}
            for k in rnd.sample(keys, rnd.randint(0, 4)):
                body[k] = rnd.choice(vals)
            cases.append((ep, json.dumps(body).encode("utf-8")))
        for ep, raw in cases:
            n += 1
            try:
                ca, ja = post(pa, ep, None, raw)
                ja = norm(ja, repo_a)
            except Exception as e:
                ca, ja = "EXC", {"__err__": str(e)}
            try:
                cb, jb = post(pb, ep, None, raw)
                jb = norm(jb, repo_b)
            except Exception as e:
                cb, jb = "EXC", {"__err__": str(e)}
            if ca not in (200, 400) or cb not in (200, 400):
                bad += 1
                print("  FAIL fuzz 状态码越界 py=%s c=%s %s %r"
                      % (ca, cb, ep, raw[:70]))
            elif ca != cb or ja != jb:
                bad += 1
                print("  FAIL fuzz %s %r" % (ep, raw[:90]))
                print("       python : code=%s %s"
                      % (ca, json.dumps(ja, ensure_ascii=False)[:400]))
                print("       c      : code=%s %s"
                      % (cb, json.dumps(jb, ensure_ascii=False)[:400]))
            if pp.poll() is not None or pc.poll() is not None:
                bad += 1
                print("  FAIL fuzz 进程退出 py=%s c=%s (after %s %r)"
                      % (pp.poll(), pc.poll(), ep, raw[:70]))
                break
        sa, sb = snapshot(repo_a), snapshot(repo_b)
        diff = diff_snapshots(sa, sb)
        if diff:
            bad += 1
            print("  FAIL fuzz 最终状态 %d 项不一致: %s" % (len(diff), ", ".join(diff)))
        else:
            print("  PASS %-20s %d 条随机/畸形 body 全部逐项相等" % ("fuzz", n))
        results[("FUZZ", "random_bodies")] = "PASS" if bad == 0 else "FAIL"
        return bad
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


def run_scenario(tag, build, steps_fn, ep, results, logdir):
    repo_a = os.path.join(BASE, tag, "py")
    repo_b = os.path.join(BASE, tag, "c")
    ctx_a = build(repo_a)
    ctx_b = build(repo_b)
    if ctx_a != ctx_b:
        print("  FAIL %-20s fixture 两边 sha 不一致: %r vs %r"
              % (tag, ctx_a, ctx_b))
        results[(ep, tag)] = "FAIL"
        return 1
    steps = steps_fn(ctx_a)
    pa_port, pb_port = free_port(), free_port()
    la = open(os.path.join(logdir, "%s_py.log" % tag), "wb")
    lb = open(os.path.join(logdir, "%s_c.log" % tag), "wb")
    pp = start_py(repo_a, pa_port, la)
    pc = start_c(repo_b, pb_port, lb)
    fails = 0
    bad = []
    try:
        if not (wait_health(pa_port, pp) and wait_health(pb_port, pc)):
            print("  FAIL %-20s 服务没起来 (py=%s c=%s)"
                  % (tag, pp.poll(), pc.poll()))
            results[(ep, tag)] = "FAIL"
            return 1
        for i, step in enumerate(steps):
            path, body = step[0], step[1]
            raw = step[2] if len(step) > 2 else None
            try:
                ca, ja = post(pa_port, path, body, raw)
                ja = norm(ja, repo_a)
            except Exception as e:
                ca, ja = "EXC", {"__err__": str(e)}
            try:
                cb, jb = post(pb_port, path, body, raw)
                jb = norm(jb, repo_b)
            except Exception as e:
                cb, jb = "EXC", {"__err__": str(e)}
            label = "%s #%d" % (path, i + 1)
            if ca == cb and ja == jb:
                print("  PASS %-20s %-34s code=%s" % (tag, label, ca))
            else:
                fails += 1
                print("  FAIL %-20s %-34s" % (tag, label))
                print("       request: %s" % json.dumps(
                    body if raw is None else raw.decode("utf-8", "replace"),
                    ensure_ascii=False))
                print("       python : code=%s %s"
                      % (ca, json.dumps(ja, ensure_ascii=False)[:900]))
                print("       c      : code=%s %s"
                      % (cb, json.dumps(jb, ensure_ascii=False)[:900]))
        sa, sb = snapshot(repo_a), snapshot(repo_b)
        bad = diff_snapshots(sa, sb)
        if bad:
            fails += 1
            print("  FAIL %-20s 最终状态 %d 项不一致: %s"
                  % (tag, len(bad), ", ".join(bad)))
            for k in bad:
                if k == "files":
                    ks = sorted(set(sa[k]) | set(sb[k]))
                    for f in ks:
                        if sa[k].get(f) != sb[k].get(f):
                            print("       files[%s]: py=%s c=%s" % (
                                f, (sa[k].get(f) or "-")[:12],
                                (sb[k].get(f) or "-")[:12]))
                elif k in ("index", "refs"):
                    oa = [x for x in sa[k] if x not in sb[k]][:3]
                    ob = [x for x in sb[k] if x not in sa[k]][:3]
                    print("       %s py-only=%r c-only=%r" % (k, oa, ob))
                elif k == "reflog":
                    ks = sorted(set(sa[k]) | set(sb[k]))
                    for f in ks:
                        if sa[k].get(f) != sb[k].get(f):
                            print("       reflog[%s]:" % f)
                            print("         py=%r" % (sa[k].get(f),))
                            print("         c =%r" % (sb[k].get(f),))
                else:
                    print("       %-10s py=%r" % (k, sa.get(k)))
                    print("       %-10s c =%r" % ("", sb.get(k)))
        else:
            print("  PASS %-20s 最终状态一致 (head=%s ref=%s)"
                  % (tag, (sa["head"] or "-")[:10], sa["head_ref"]))
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
        for tag, build, steps_fn, ep in SCENARIOS:
            print("[%s]" % tag)
            fails += run_scenario(tag, build, steps_fn, ep, results, logdir)
        print("[fuzz]")
        fails += fuzz_phase(results)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    # 汇总表：按端点分组，行 = fixture
    print()
    print("汇总（端点 × fixture；reqs=该 fixture 下全部 POST 的码/JSON 是否逐项相等，"
          "state=最终仓库状态）")
    by_ep = {}
    for tag, build, steps_fn, ep in SCENARIOS:
        by_ep.setdefault(ep, []).append(tag)
    for ep in ENDPOINTS:
        print()
        print("%s" % ep)
        print("  %-24s %-6s %-6s" % ("fixture", "reqs", "state"))
        for tag in by_ep.get(ep, []):
            print("  %-24s %-6s %-6s" % (tag,
                                         results.get((ep, tag), "-"),
                                         results.get(("STATE", tag), "-")))
    # body_bad 一个 fixture 覆盖了六个端点，单独列一行
    print()
    print("  %-24s %-6s %-6s" % ("body_bad (六个端点共享)",
                                 results.get(("/api/history/revert", "body_bad"), "-"),
                                 results.get(("STATE", "body_bad"), "-")))
    print("  %-24s %-6s %-6s" % ("fuzz (随机畸形 body)",
                                 results.get(("FUZZ", "random_bodies"), "-"), "-"))
    print()
    print("失败 %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
