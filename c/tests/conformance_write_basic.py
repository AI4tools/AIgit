# -*- coding: utf-8 -*-
"""M2 写面第一组一致性判据：C 版 vs Python 版 vs 真 git。

覆盖 4 个 POST 端点：
    /api/stage   /api/unstage   /api/discard   /api/commit

做法（与 cmp_write_vs_git.py 同构）：
    * 同一个 fixture 建三份**完全相同**的副本：py（Python 服务）、c（C 服务）、
      git（真 git 操作，只用于 stage/unstage/discard/commit 四个对照场景）；
    * 同时对 py / c 发**相同顺序**的 POST 序列，逐条比较
      HTTP 状态码 + JSON 深比较（响应里的 root 绝对路径先归一）；
    * 序列跑完后比较最终仓库状态：
        - git status --porcelain=v2 -uall（逐条排序）
        - rev-parse HEAD / for-each-ref
        - 索引：git ls-files -s（语义）+ .git/index 原始哈希 + 屏蔽
          ctime/mtime/dev/ino 之后的索引哈希（跨副本唯一可比的那个）
        - 工作区：每个文件内容的 sha256 + 目录清单
    * 四个对照场景再与真 git 的同操作结果比较。

跨副本说明：两份副本的文件 ctime 必然不同，而 git 索引条目记录 ctime/mtime，
所以 `.git/index` 的**原始字节**哈希不可能相等。判据用 index_norm（把
ctime/mtime/dev/ino 抹成 0 之后的哈希）+ ls-files -s 作为权威比较，
原始哈希作为 INFO 打印出来。

用法：
    python -X utf8 c/tests/conformance_write_basic.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import hashlib
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
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
_PY_SIBLING = os.path.join(os.path.dirname(C_ROOT), "python-ref", "server.py")
PY_ROOT = os.path.dirname(_PY_SIBLING) if os.path.isfile(_PY_SIBLING) else r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_wb_%d" % (os.getpid() % 100000))

# 固定身份与时间：让两边写出来的 commit sha 可以直接比
FIXED = {
    "GIT_AUTHOR_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_COMMITTER_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@localhost",
}
ENV = dict(os.environ, **FIXED)


# ---------------------------------------------------------------- 基础

def sh(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True, env=ENV)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout.decode("utf-8", "replace")


def w(repo, name, data):
    p = os.path.join(repo, name)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def init(d):
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")


# ---------------------------------------------------------------- fixtures

def fx_clean(d):
    init(d)
    w(d, "a.txt", "a\n")
    w(d, "sub/b.txt", "b\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 clean")


def fx_dirty(d):
    init(d)
    w(d, "a.txt", "a\n")
    w(d, "sub/b.txt", "b\n")
    w(d, "del.txt", "d\n")
    w(d, "空 格.txt", "中文\n")
    w(d, ".gitignore", "ignored.txt\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 dirty")
    w(d, "a.txt", "a2\n")                  # 已跟踪、已改
    os.remove(os.path.join(d, "del.txt"))  # 已跟踪、已删
    w(d, "u.txt", "u\n")                   # 未跟踪文件
    w(d, "新 文件.txt", "x\n")              # 未跟踪（中文+空格）
    w(d, "newdir/inner.txt", "i\n")        # 未跟踪目录
    w(d, "ignored.txt", "nope\n")          # 被忽略


def fx_staged(d):
    init(d)
    w(d, "a.txt", "a\n")
    w(d, "sub/b.txt", "b\n")
    w(d, "del.txt", "d\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 staged")
    w(d, "a.txt", "a2\n")
    w(d, "sub/b.txt", "b2\n")
    w(d, "del.txt", "d2\n")
    must(d, "add", "-A")                   # 全部已暂存
    w(d, "del.txt", "d3\n")                # 只有这一条是「已暂存+又改」


def fx_discard(d):
    init(d)
    w(d, "a.txt", "a\n")
    w(d, "sub/b.txt", "b\n")
    w(d, "del.txt", "d\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 discard")
    w(d, "a.txt", "a2\n")                  # 已跟踪、已改 -> restore
    must(d, "mv", "sub/b.txt", "sub/c.txt")  # 已暂存改名
    os.remove(os.path.join(d, "del.txt"))  # 已跟踪、工作区已删 -> restore
    w(d, "u.txt", "u\n")                   # 未跟踪文件 -> unlink
    w(d, "newdir/inner.txt", "i\n")        # 未跟踪目录 -> 删树


def fx_unborn(d):
    init(d)
    w(d, "x.txt", "x\n")
    must(d, "add", "-A")                   # 只在索引里，没有提交
    w(d, "untracked.txt", "u\n")


def fx_empty(d):
    init(d)


def fx_unborn_untracked(d):
    init(d)
    w(d, "only.txt", "o\n")                # 没进过索引，也没有 HEAD


def fx_detached(d):
    init(d)
    for i in (1, 2, 3):
        w(d, "f.txt", "v%d\n" % i)
        must(d, "add", "-A")
        must(d, "commit", "-q", "-m", "c%d" % i)
    must(d, "checkout", "-q", "--detach", "HEAD~2")
    w(d, "f.txt", "dirty\n")
    w(d, "new.txt", "n\n")


def fx_crlf(d):
    init(d)
    sh(d, "config", "core.autocrlf", "true")
    with open(os.path.join(d, "a.txt"), "wb") as f:
        f.write(b"a\r\nb\r\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 crlf")
    with open(os.path.join(d, "a.txt"), "wb") as f:
        f.write(b"a2\r\nsecond\r\n")


def fx_large(d):
    init(d)
    w(d, "a.txt", "a\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    big = "".join("line %06d abcdefghij\n" % i for i in range(50000))  # ~1.1 MB
    w(d, "big.txt", big)


def fx_binary(d):
    init(d)
    w(d, "a.txt", "a\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    w(d, "bin.dat", b"\x00\x01\x02hello\x00\xff\xfe")


# ---------------------------------------------------------------- 服务

def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_py(repo, port):
    return subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=ENV, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port):
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=ENV, cwd=C_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def stop(p):
    try:
        p.terminate()
        p.wait(15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def post(port, path, body, raw=None):
    if raw is not None:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    else:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        r = urllib.request.urlopen(rq, timeout=180)
        code, payload = r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    except Exception as e:                       # 连接被重置：明确记成 EXC，不掩盖
        return {"code": "EXC", "json": repr(e)}
    try:
        return {"code": code, "json": json.loads(payload.decode("utf-8"))}
    except Exception:
        return {"code": code, "json": ("<<not json>>", payload[:200].decode("utf-8", "replace"))}


# ---------------------------------------------------------------- 状态快照

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def index_norm_hash(path):
    """把索引条目里的 ctime/mtime/dev/ino 抹 0 再哈希（v2/v3；v4 返回 None）。

    跨副本唯一可比的索引哈希：两份副本的 stat 时间戳必然不同，语义部分相同。
    """
    try:
        data = bytearray(open(path, "rb").read())
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    ver = int.from_bytes(data[4:8], "big")
    n = int.from_bytes(data[8:12], "big")
    if ver not in (2, 3):
        return None
    off = 12
    for _ in range(n):
        if off + 62 > len(data):
            return None
        data[off:off + 8] = b"\0" * 8          # ctime
        data[off + 8:off + 16] = b"\0" * 8     # mtime
        data[off + 16:off + 20] = b"\0" * 4    # dev
        data[off + 20:off + 24] = b"\0" * 4    # ino
        flags = int.from_bytes(data[off + 60:off + 62], "big")
        namelen = flags & 0xFFF
        p = off + 62
        if namelen < 0xFFF:
            p += namelen
        else:
            while p < len(data) and data[p] != 0:
                p += 1
        entlen = p - off
        entlen = (entlen + 7) & ~7             # v2/v3 条目 8 字节对齐
        off += entlen
    # 末尾 20 字节是整个索引的 SHA-1 校验和：ctime/mtime 不同它必然不同，
    # 一起抹掉（否则等于没归一）。
    if len(data) >= 20:
        data[-20:] = b"\0" * 20
    return sha256_bytes(bytes(data))


def walk_worktree(repo):
    files, dirs = {}, []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel_dir = os.path.relpath(dirpath, repo).replace("\\", "/")
        if rel_dir != ".":
            dirs.append(rel_dir)
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo).replace("\\", "/")
            with open(full, "rb") as f:
                files[rel] = sha256_bytes(f.read())
    return files, sorted(dirs)


def state(repo):
    out = {}
    p = sh(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    out["head"] = p.stdout.decode().strip() if p.returncode == 0 else None
    out["refs"] = sorted(x for x in must(repo, "for-each-ref",
                                         "--format=%(refname)|%(objectname)").splitlines() if x)
    out["status"] = sorted(x for x in
                           sh(repo, "status", "--porcelain=v2", "-uall", "-z")
                           .stdout.decode("utf-8", "replace").split("\0") if x)
    out["index_sem"] = sha256_bytes(sh(repo, "ls-files", "-s", "-z").stdout)
    idx = os.path.join(repo, ".git", "index")
    out["index_raw"] = sha256_bytes(open(idx, "rb").read()) if os.path.isfile(idx) else None
    out["index_norm"] = index_norm_hash(idx) if os.path.isfile(idx) else None
    files, dirs = walk_worktree(repo)
    out["files"] = files
    out["dirs"] = dirs
    return out


def norm_json(v, repo):
    """响应里的绝对路径（root）归一，否则两份副本永远不相等。"""
    if isinstance(v, str):
        return v.replace(repo.replace("\\", "/"), "<ROOT>").replace(repo, "<ROOT>")
    if isinstance(v, list):
        return [norm_json(x, repo) for x in v]
    if isinstance(v, dict):
        return {k: norm_json(x, repo) for k, x in v.items()}
    return v


def diff_states(a, b):
    bad = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            bad.append(k)
    return bad


def show_diff(k, va, vb):
    if k == "files":
        for f in sorted(set(va) | set(vb)):
            if va.get(f) != vb.get(f):
                print("       files[%s]: py=%s c=%s" % (f, (va.get(f) or "-")[:12],
                                                        (vb.get(f) or "-")[:12]))
    elif k == "dirs":
        print("       dirs py-only=%r" % [x for x in va if x not in vb][:5])
        print("       dirs c-only =%r" % [x for x in vb if x not in va][:5])
    elif k == "status":
        print("       status py-only=%r" % [x for x in va if x not in vb][:4])
        print("       status c-only =%r" % [x for x in vb if x not in va][:4])
    else:
        print("       %-10s py=%r" % (k, va))
        print("       %-10s c =%r" % ("", vb))


# ---------------------------------------------------------------- 场景

STAGE_STEPS = [
    ("/api/stage", {}),
    ("/api/stage", {"files": []}),
    ("/api/stage", {"files": ["a.txt"]}),
    ("/api/stage", {"files": ["u.txt", "a.txt"]}),
    ("/api/stage", {"files": ["nope.txt"]}),
    ("/api/stage", {"files": ["../outside.txt"]}),
    ("/api/stage", {"files": [".git/config"]}),
    ("/api/stage", {"files": [".GIT/config"]}),
    ("/api/stage", {"files": ["newdir"]}),
    ("/api/stage", {"files": ["newdir/"]}),
    ("/api/stage", {"files": ["u.txt", 1]}),
    ("/api/stage", {"files": "a.txt"}),
    ("/api/stage", {"files": None}),
    ("/api/stage", {"files": ["del.txt"]}),
    ("/api/stage", {"files": ["ignored.txt"]}),
    ("/api/stage", {"files": ["a.txt"], "status": False}),
    ("/api/stage", {"files": ["空 格.txt"]}),
    ("/api/stage", {"files": ["A.TXT"]}),
    ("/api/stage", {"files": ["sub/../a.txt"]}),
    ("/api/stage", {"files": ["a.txt/"]}),
    ("/api/stage", {"files": ["."]}),
    ("/api/stage", {"files": [""]}),
    ("/api/stage", {"files": ["a.txt\u0000x"]}),
    ("/api/stage", {"files": ["C:\\Windows"]}),
    ("/api/stage", {"files": ["sub"]}),
    ("/api/stage", None, b""),                     # 空 body -> {}
    ("/api/stage", None, b"{bad"),                 # 非法 JSON
    ("/api/stage", None, b"[1,2]"),                # 合法 JSON 但不是对象
    ("/api/stage", None, b'"x"'),
]

UNSTAGE_STEPS = [
    ("/api/unstage", {}),
    ("/api/unstage", {"files": []}),
    ("/api/unstage", {"files": ["a.txt"]}),
    ("/api/unstage", {"files": ["nope.txt"]}),
    ("/api/unstage", {"files": ["../x"]}),
    ("/api/unstage", {"files": [".git/config"]}),
    ("/api/unstage", {"files": ["sub"]}),
    ("/api/unstage", {"files": ["./a.txt"]}),
    ("/api/unstage", {"files": ["A.TXT"]}),
    ("/api/unstage", {"files": [1]}),
    ("/api/unstage", {"files": ["del.txt"]}),
    ("/api/unstage", {"files": ["a.txt"], "status": False}),
    ("/api/unstage", {"files": None}),
    ("/api/unstage", {"files": ["a.txt", "sub/b.txt"]}),
]

DISCARD_STEPS = [
    ("/api/discard", {"files": ["u.txt"]}),
    ("/api/discard", {"files": ["u.txt"], "confirmed": False}),
    ("/api/discard", {"files": ["u.txt"], "confirmed": "false"}),
    ("/api/discard", {"files": ["u.txt"], "confirmed": 1}),
    ("/api/discard", {"files": ["."], "confirmed": True}),
    ("/api/discard", {"files": ["../x"], "confirmed": True}),
    ("/api/discard", {"files": [".git/config"], "confirmed": True}),
    ("/api/discard", {"files": "x", "confirmed": True}),
    ("/api/discard", {"files": ["sub"], "confirmed": True}),      # 目录内含已跟踪 -> 400
    ("/api/discard", {"files": ["u.txt"], "confirmed": True}),    # 删未跟踪文件
    ("/api/discard", {"files": ["newdir"], "confirmed": True}),   # 删未跟踪目录
    ("/api/discard", {"files": ["new.txt"], "confirmed": True}),  # 不存在 -> no-op
    ("/api/discard", {"files": ["a.txt"], "confirmed": True}),    # 已跟踪已改 -> restore
    ("/api/discard", {"files": ["sub/c.txt"], "confirmed": True}),
    ("/api/discard", {"files": ["sub/b.txt"], "confirmed": True}),  # 改名后旧名 -> no-op
    ("/api/discard", {"files": ["del.txt"], "confirmed": True}),  # 已跟踪已删 -> restore
    ("/api/discard", {"files": [], "confirmed": True}),
    ("/api/discard", {"files": None, "confirmed": True}),
    ("/api/discard", {"files": ["a.txt"], "confirmed": True, "status": False}),
]

COMMIT_MSG_STEPS = [
    ("/api/commit", {}),
    ("/api/commit", {"message": ""}),
    ("/api/commit", {"message": "   \n\t"}),
    ("/api/commit", {"message": None}),
    ("/api/commit", {"message": 5}),
    ("/api/commit", {"message": True}),
    ("/api/commit", {"message": ["x"]}),
    ("/api/commit", {"message": "a\u0000b"}),       # NUL -> 400
    ("/api/commit", {"message": "\u3000"}),         # 全角空格：Python strip 后为空
    ("/api/commit", {"message": "ok 最终"}),
    ("/api/commit", {"message": "again"}),        # 无可提交 -> 400
]

COMMIT_DIRTY_STEPS = [
    ("/api/commit", {"message": "中文 提交\n第二行 body"}),
]

COMMIT_OPT_STEPS = [
    ("/api/commit", {"message": "opt out", "status": False}),
]

COMMIT_NOTHING_STEPS = [
    ("/api/commit", {"message": "nothing"}),
]

# ---- 联接（mklink /J 不需要管理员；建不出来时退化成普通场景，不会假绿）----

def _mklink(dst, target):
    subprocess.run(["cmd", "/c", "mklink", "/J", dst, target], capture_output=True)


def hook_escape(repo, root):
    """仓里挂一个指向仓外的目录联接 -> commit 必须拒收。"""
    outside = os.path.join(root, "outside")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "leak.txt"), "w", encoding="utf-8") as f:
        f.write("secret\n")
    _mklink(os.path.join(repo, "escape"), outside)


def hook_inner_junction(repo, root):
    """未跟踪目录里挂一个**指向仓内**的目录联接 -> discard 整目录拒删；
    commit 收 inner.txt、跳过联接本身。"""
    os.makedirs(os.path.join(repo, "newdir"), exist_ok=True)
    with open(os.path.join(repo, "newdir", "inner.txt"), "w", encoding="utf-8") as f:
        f.write("i\n")
    _mklink(os.path.join(repo, "newdir", "link"), os.path.join(repo, "sub"))


# ---------------------------------------------------------------- 主流程

SCENARIOS = [
    # name, fixture, steps, git_ops(可选，第三份副本上跑的真 git)
    ("stage", fx_dirty, STAGE_STEPS, None),
    ("unstage", fx_staged, UNSTAGE_STEPS, None),
    ("discard", fx_discard, DISCARD_STEPS, None),
    ("commit_msg", fx_dirty, COMMIT_MSG_STEPS, None),
    ("commit_dirty", fx_dirty, COMMIT_DIRTY_STEPS, None),
    ("commit_large", fx_large, [("/api/commit", {"message": "big commit"})], None),
    ("commit_binary", fx_binary, [("/api/commit", {"message": "binary commit"})], None),
    ("commit_crlf", fx_crlf, [("/api/commit", {"message": "crlf commit"})], None),
    ("commit_staged_only", fx_staged, [("/api/commit", {"message": "staged only"})], None),
    ("commit_unborn", fx_unborn, [("/api/commit", {"message": "first commit"})], None),
    ("commit_detached", fx_detached, [("/api/commit", {"message": "detached commit"})], None),
    ("commit_nothing", fx_clean, COMMIT_NOTHING_STEPS, None),
    ("commit_empty_repo", fx_empty, [("/api/commit", {"message": "x"})], None),
    ("commit_unborn_untracked", fx_unborn_untracked,
     [("/api/commit", {"message": "first"})], None),
    ("unborn_discard", fx_unborn,
     [("/api/unstage", {"files": ["x.txt"]}),        # 未出生 HEAD -> git reset 400
      ("/api/unstage", {"files": ["nope.txt"]}),     # 不存在 -> 无操作 200
      ("/api/discard", {"files": ["x.txt"], "confirmed": True}),
      ("/api/discard", {"files": ["untracked.txt"], "confirmed": True}),
      ("/api/commit", {"message": "after discard"})], None),
    ("commit_optout", fx_dirty, COMMIT_OPT_STEPS, None),
    # 与真 git 对照（第三份副本）
    ("git_stage", fx_dirty, [("/api/stage", {"files": ["a.txt", "u.txt"]})],
     [["add", "--", "a.txt", "u.txt"]]),
    ("git_unstage", fx_staged, [("/api/unstage", {"files": ["a.txt"]})],
     [["reset", "-q", "HEAD", "--", "a.txt"]]),
    ("git_commit", fx_dirty, [("/api/commit", {"message": "msg one"})],
     [["add", "-A"], ["commit", "-q", "-m", "msg one"]]),
    ("git_discard", fx_discard,
     [("/api/discard", {"files": ["a.txt", "u.txt", "newdir"], "confirmed": True})],
     None),   # discard 的 git 等价操作在下面手工做（含 rm）
    # 联接（mklink /J）：仓外联接拒收 / 仓内联接在 walk 里跳过
    ("commit_escape", fx_dirty, [("/api/commit", {"message": "leak"})], None, hook_escape),
    ("junction_discard", fx_dirty,
     [("/api/discard", {"files": ["newdir"], "confirmed": True}),   # 树里有联接 -> 400
      ("/api/commit", {"message": "with junction"})],               # 收 inner.txt，跳过联接
     None, hook_inner_junction),
]


def git_equivalent_discard(repo):
    must(repo, "checkout", "--", "a.txt")
    for p in ("u.txt",):
        fp = os.path.join(repo, p)
        if os.path.isfile(fp):
            os.remove(fp)
    nd = os.path.join(repo, "newdir")
    if os.path.isdir(nd):
        shutil.rmtree(nd)


def copy_repo(src, dst):
    shutil.copytree(src, dst)


def run_scenario(name, fixture, steps, git_ops, hook=None):
    root = os.path.join(BASE, name)
    os.makedirs(root, exist_ok=True)
    src = os.path.join(root, "src")
    fixture(src)
    py = os.path.join(root, "py")
    cc = os.path.join(root, "c")
    copy_repo(src, py)
    copy_repo(src, cc)
    gg = None
    if git_ops is not None or name == "git_discard":
        gg = os.path.join(root, "git")
        copy_repo(src, gg)
    if hook:
        hook(py, root)
        hook(cc, root)
        if gg:
            hook(gg, root)

    pa, pb = free_port(), free_port()
    while pb == pa:
        pb = free_port()
    pp = start_py(py, pa)
    pc = start_c(cc, pb)
    results = []
    try:
        ok_py = wait_health(pa, pp)
        ok_c = wait_health(pb, pc)
        if not (ok_py and ok_c):
            for path, body in steps:
                results.append((path, False, "服务没起来 py=%s c=%s" % (ok_py, ok_c)))
            return results, None
        print("\n=== 场景: %s (%s)" % (name, src))
        for step in steps:
            path, body = step[0], step[1]
            raw = step[2] if len(step) > 2 else None
            a = post(pa, path, body, raw=raw)
            b = post(pb, path, body, raw=raw)
            ja = norm_json(a["json"], py)
            jb = norm_json(b["json"], cc)
            same = (a["code"] == b["code"] and ja == jb)
            desc = json.dumps(body, ensure_ascii=False) if raw is None else repr(raw)
            if len(desc) > 70:
                desc = desc[:70] + "..."
            if same:
                results.append((path, True, ""))
                print("  PASS %-14s %-72s code=%s" % (path, desc, a["code"]))
            else:
                results.append((path, False, ""))
                print("  FAIL %-14s %-72s" % (path, desc))
                print("       py: code=%s json=%s" % (a["code"],
                                                      json.dumps(ja, ensure_ascii=False)[:600]))
                print("       c : code=%s json=%s" % (b["code"],
                                                      json.dumps(jb, ensure_ascii=False)[:600]))
    finally:
        stop(pp)
        stop(pc)

    # 最终状态
    if git_ops is not None:
        for op in git_ops:
            p = sh(gg, *op)
            if p.returncode != 0:
                print("  FAIL 真 git 对照操作失败: git %s -> %s"
                      % (" ".join(op), (p.stderr or p.stdout)[:200]))
    if name == "git_discard":
        git_equivalent_discard(gg)

    sa, sb = state(py), state(cc)
    bad = diff_states(sa, sb)
    raw_only = False
    # git racily-clean 会把 size 临时抹 0：raw/norm 都可能变，语义相同就不算差异。
    if set(bad) <= {"index_raw", "index_norm"} and sa["index_sem"] == sb["index_sem"]:
        raw_only = True
        bad = []
    if bad:
        print("  FAIL 最终状态 py vs c: %d 项不一致" % len(bad))
        for k in bad:
            show_diff(k, sa.get(k), sb.get(k))
        results.append(("__state__", False, ""))
    else:
        results.append(("__state__", True, ""))
        print("  PASS 最终状态 py vs c（head=%s）%s"
              % ((sa["head"] or "-")[:10],
                 " [index 原始哈希差异仅 ctime/mtime]" if raw_only else ""))

    if gg:
        sg = state(gg)
        bad = diff_states(sa, sg)
        raw_only = False
        if set(bad) <= {"index_raw", "index_norm"} and sa["index_sem"] == sg["index_sem"]:
            raw_only = True
            bad = []
        if bad:
            print("  FAIL 最终状态 py vs 真 git: %d 项不一致" % len(bad))
            for k in bad:
                show_diff(k, sa.get(k), sg.get(k))
            results.append(("__vs_git__", False, ""))
        else:
            results.append(("__vs_git__", True, ""))
            print("  PASS 最终状态 py vs 真 git（head=%s）%s"
                  % ((sa["head"] or "-")[:10],
                     " [index 原始哈希差异仅 ctime/mtime]" if raw_only else ""))
    return results, name


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    print("  Python 参考版: %s" % PY_ROOT)
    print("  C 版 exe     : %s" % EXE)

    matrix = {}
    fails = 0
    total = 0
    try:
        for sc in SCENARIOS:
            name, fixture, steps, git_ops = sc[0], sc[1], sc[2], sc[3]
            hook = sc[4] if len(sc) > 4 else None
            results, _ = run_scenario(name, fixture, steps, git_ops, hook)
            for path, ok, _msg in results:
                if path in ("__state__", "__vs_git__"):
                    ep = path
                else:
                    ep = path.rsplit("/", 1)[-1]
                cell = matrix.setdefault(ep, {}).setdefault(name, [0, 0])
                total += 1
                if ok:
                    cell[0] += 1
                else:
                    cell[1] += 1
                    fails += 1
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    eps = ["stage", "unstage", "discard", "commit", "__state__", "__vs_git__"]
    names = [s[0] for s in SCENARIOS]
    print("\n=== PASS/FAIL 矩阵（端点 × 场景）")
    header = "  %-12s" % "endpoint"
    for n in names:
        header += " %-17s" % n
    print(header)
    for ep in eps:
        if ep not in matrix:
            continue
        row = "  %-12s" % ep
        for n in names:
            c = matrix.get(ep, {}).get(n)
            row += " %-17s" % ("-" if not c else ("PASS %d" % c[0] if not c[1]
                                                  else "FAIL %d/%d" % (c[1], c[0] + c[1])))
        print(row)
    print("\n  合计 %d 项检查，失败 %d，通过 %d" % (total, fails, total - fails))
    print("  %s" % ("全部 PASS" if fails == 0 else "有 FAIL"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
