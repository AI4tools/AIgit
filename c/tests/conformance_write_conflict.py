# -*- coding: utf-8 -*-
"""M2 一致性判据：冲突**写**端点（C 版 vs Python 版）。

判据（两条，缺一不可）：
  1. 同一 fixture 建**两份相同副本** A / B，同时起服务（A=Python，B=C），
     跑**相同顺序**的 POST 序列，每一步比 **HTTP 码 + JSON 深比较**；
  2. 序列跑完比**最终仓库状态**：
       HEAD / tree / 全部 refs / HEAD 文件 / 索引（ls-files -s，逻辑索引哈希）/
       status --porcelain=v2 -uall / 工作区每个文件的 sha256 /
       MERGE_HEAD、MERGE_MSG、ORIG_HEAD 是否存在（存在则比内容）。

覆盖的端点（5 个）：
    /api/conflict/resolve
    /api/conflict/choose
    /api/conflict/delete
    /api/conflict/commit
    /api/conflict/abort

fixture：
    modify    真冲突（both modified）
    addadd    add/add（两侧都新增同名文件）
    delmod    delete/modify（一侧删、一侧改）
    binary    二进制冲突（两侧都改二进制）
    rich      一个仓里凑齐上面四种 + 中文/空格路径
    clean     非合并态（所有写端点都该拒绝，abort 除外）

计数头（X-aigit-git-calls / cmds）**不比** —— C 版为了简单把跨文件查询拆成
逐文件 spawn，spawn 数本来就会不同（结果必须相同，次数不必）。

用法：
    python -X utf8 c/tests/conformance_write_conflict.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
"""
import base64
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
C_ROOT = os.path.dirname(HERE)                       # ...\c
_PY_CANDS = [os.environ.get("AIGIT_PY_ROOT"),
             os.path.join(os.path.dirname(C_ROOT), "python-ref"),
             r"D:\project\aigit\python-ref"]
PY_ROOT = next((p for p in _PY_CANDS if p and os.path.isdir(p)), _PY_CANDS[1])
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_c_m2wc_%d" % (os.getpid() % 100000))
# F-06 场景的仓外哨兵目录（两个仓指向**同一个**目录，只有仓根不同）
OUTSIDE = os.path.join(BASE, "outside")

# 固定身份与时间：让两边写出来的 commit 可以直接比 sha
FIXED = {
    "GIT_AUTHOR_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_COMMITTER_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@localhost",
}
ID = ["-c", "user.name=bench", "-c", "user.email=bench@localhost"]


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a, env=None):
    p = sh(repo, *a, env=env)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def w(repo, name, text):
    path = os.path.join(repo, name)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def wb(repo, name, data):
    path = os.path.join(repo, name)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def init_repo(d, env):
    os.makedirs(d, exist_ok=True)
    must(d, "init", "-q", "-b", "main", env=env)
    must(d, "config", "user.name", "bench", env=env)
    must(d, "config", "user.email", "bench@localhost", env=env)
    return d


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_py(repo, port, env):
    e = dict(env, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([sys.executable, "-X", "utf8",
                             os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=e, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port, env):
    e = dict(env, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=e, cwd=C_ROOT,
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


def post(port, path, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
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

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def read_maybe(path):
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\r\n", b"\n")
    except OSError:
        return None


def state(repo, env):
    """可比较的仓库状态快照。"""
    out = {}
    p = sh(repo, "rev-parse", "--verify", "--quiet", "HEAD", env=env)
    out["head"] = p.stdout.strip() if p.returncode == 0 else None
    p = sh(repo, "rev-parse", "HEAD^{tree}", env=env)
    out["tree"] = p.stdout.strip() if p.returncode == 0 else None
    out["refs"] = sorted(x for x in must(repo, "for-each-ref",
                                         "--format=%(refname)|%(objectname)|%(symref)",
                                         env=env).splitlines() if x)
    idx = sorted(x for x in must(repo, "ls-files", "-s", env=env).splitlines() if x)
    out["index"] = idx
    # 逻辑索引哈希：不受 inode / 时间戳影响（两份副本的 stat 数据本来就不同）
    out["index_sha"] = sha256_bytes("\n".join(idx).encode("utf-8"))
    out["status"] = sorted(x for x in
                           must(repo, "status", "--porcelain=v2", "-uall", env=env)
                           .replace("\r\n", "\n").split("\n") if x)
    gd = os.path.join(repo, ".git")
    out["head_file"] = (read_maybe(os.path.join(gd, "HEAD")) or b"").decode("utf-8", "replace")
    out["merge_head"] = read_maybe(os.path.join(gd, "MERGE_HEAD"))
    out["merge_msg"] = read_maybe(os.path.join(gd, "MERGE_MSG"))
    out["orig_head"] = read_maybe(os.path.join(gd, "ORIG_HEAD"))
    out["merge_rr"] = read_maybe(os.path.join(gd, "MERGE_RR"))
    files = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo).replace("\\", "/")
            try:
                with open(full, "rb") as f:
                    files[rel] = sha256_bytes(f.read())
            except OSError:
                files[rel] = "UNREADABLE"
    out["files"] = files
    return out


def diff_state(sa, sb):
    return [k for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]


# ---------------------------------------------------------------- fixtures

def _merge(d, env, branch="feature"):
    p = sh(d, "merge", branch, env=env)
    if p.returncode == 0:
        raise RuntimeError("期望冲突但 merge 成功了")


def fx_modify(d, env):
    """both modified：a.txt 两侧都改。"""
    init_repo(d, env)
    w(d, "a.txt", "l1\nl2\nl3\nl4\nl5\n")
    w(d, "keep.txt", "k\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "base", env=env)
    must(d, "checkout", "-q", "-b", "feature", env=env)
    w(d, "a.txt", "l1\nl2 feature\nl3\nl4\nl5\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "feature", env=env)
    must(d, "checkout", "-q", "main", env=env)
    w(d, "a.txt", "l1\nl2 main\nl3\nl4\nl5\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "main2", env=env)
    _merge(d, env)
    return d


def fx_addadd(d, env):
    """add/add：两侧都新增 new.txt（内容不同）。"""
    init_repo(d, env)
    w(d, "keep.txt", "k\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "base", env=env)
    must(d, "checkout", "-q", "-b", "feature", env=env)
    w(d, "new.txt", "new-feature\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "feature", env=env)
    must(d, "checkout", "-q", "main", env=env)
    w(d, "new.txt", "new-main\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "main2", env=env)
    _merge(d, env)
    return d


def fx_delmod(d, env):
    """delete/modify：feature 删掉 del.txt，main 改了它。"""
    init_repo(d, env)
    w(d, "del.txt", "d1\nd2\nd3\n")
    w(d, "keep.txt", "k\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "base", env=env)
    must(d, "checkout", "-q", "-b", "feature", env=env)
    os.remove(os.path.join(d, "del.txt"))
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "feature", env=env)
    must(d, "checkout", "-q", "main", env=env)
    w(d, "del.txt", "d1\nd2 main\nd3\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "main2", env=env)
    _merge(d, env)
    return d


def fx_binary(d, env):
    """二进制冲突：bin.dat 两侧都改（含 NUL）。"""
    init_repo(d, env)
    wb(d, "bin.dat", b"\x00\x01base\n")
    w(d, "keep.txt", "k\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "base", env=env)
    must(d, "checkout", "-q", "-b", "feature", env=env)
    wb(d, "bin.dat", b"\x00\x01feature\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "feature", env=env)
    must(d, "checkout", "-q", "main", env=env)
    wb(d, "bin.dat", b"\x00\x01main\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "main2", env=env)
    _merge(d, env)
    return d


def fx_rich(d, env):
    """一个仓凑齐：both-modified / add-add / delete-modify / 二进制 / 中文空格。"""
    init_repo(d, env)
    w(d, "a.txt", "l1\nl2\nl3\nl4\nl5\n")
    w(d, "keep.txt", "k\n")
    w(d, "del.txt", "d1\nd2\nd3\n")
    w(d, "中文 空格.txt", "x1\nx2\nx3\n")
    wb(d, "bin.dat", b"\x00\x01base\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "base", env=env)
    must(d, "checkout", "-q", "-b", "feature", env=env)
    w(d, "a.txt", "l1\nl2 feature\nl3\nl4\nl5\n")
    os.remove(os.path.join(d, "del.txt"))
    w(d, "中文 空格.txt", "x1\nx2 feature\nx3\n")
    wb(d, "bin.dat", b"\x00\x01feature\n")
    w(d, "addnew.txt", "new-feature\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "feature", env=env)
    must(d, "checkout", "-q", "main", env=env)
    w(d, "a.txt", "l1\nl2 main\nl3\nl4\nl5\n")
    w(d, "del.txt", "d1\nd2 main\nd3\n")
    w(d, "中文 空格.txt", "x1\nx2 main\nx3\n")
    wb(d, "bin.dat", b"\x00\x01main\n")
    w(d, "addnew.txt", "new-main\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "main2", env=env)
    _merge(d, env)
    return d


def fx_clean(d, env):
    init_repo(d, env)
    w(d, "a.txt", "1\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "c1", env=env)
    w(d, "a.txt", "2\n")
    must(d, "add", "-A", env=env)
    must(d, *ID, "commit", "-q", "-m", "c2", env=env)
    return d


FIXTURES = [
    ("modify", fx_modify),
    ("addadd", fx_addadd),
    ("delmod", fx_delmod),
    ("binary", fx_binary),
    ("rich", fx_rich),
    ("clean", fx_clean),
]

R = "/api/conflict/resolve"
CH = "/api/conflict/choose"
D = "/api/conflict/delete"
M = "/api/conflict/commit"
A = "/api/conflict/abort"

B64 = lambda b: base64.b64encode(b).decode("ascii")

# expect_sha256 用占位符表示「这个仓里该文件当前的 sha256」：
# 场景在两个仓里各算一次，保证两边拿到的期望值一样。
FILESHA = "@FILE_SHA@"


def expand(body, repo):
    """把 FILESHA 占位符换成 repo 里该文件的 sha256。"""
    if not isinstance(body, dict):
        return body
    v = body.get("expect_sha256")
    if isinstance(v, str) and v.startswith(FILESHA):
        p = os.path.join(repo, v[len(FILESHA):])
        try:
            with open(p, "rb") as f:
                return dict(body, expect_sha256=sha256_bytes(f.read()))
        except OSError:
            return dict(body, expect_sha256="0" * 64)
    return body


# 建目录联接（不需要管理员权限；F-06 防线要用）
JUNCTION = "!junction"


def make_junction(repo, name, target):
    path = os.path.join(repo, name)
    if os.path.isdir(path):
        subprocess.run(["cmd", "/c", "rd", path], capture_output=True)
    p = subprocess.run(["cmd", "/c", "mklink", "/J", path, target],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("mklink /J 失败: " + (p.stdout + p.stderr).decode("mbcs", "replace")[:200])


def normalize(js, repo):
    """把响应里的仓绝对路径换成 <REPO>（两个仓的根不同，只有外链报错会带绝对路径）。

    ⚠ 必须在**解析后的对象**上替换：json.dumps 会把 `\\` 转义成 `\\\\`，
    在序列化后的文本上替换原路径永远匹配不上（第一版就踩了这个坑）。"""
    real = os.path.realpath(repo)
    pats = (repo, repo.replace("\\", "/"), real, real.replace("\\", "/"))

    def walk(o):
        if isinstance(o, str):
            for p in pats:
                o = o.replace(p, "<REPO>")
            return o
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o

    return walk(js)


# 每个 fixture 的场景：[(场景名, [(path, body[, raw]), ...]), ...]
# CPython 补丁版本之间，同一段 Python 代码吐出的**异常文案**会变。实测：
#   * 3.11.0 / 3.12.4：`dict[:12]` → TypeError("unhashable type: 'slice'")
#   * 3.12.10（CI 上）：`dict[:12]` → KeyError，str() 是 "slice(None, 12, None)"
# C 只能对齐其中一种口径，而这一族文案在 Python 侧自己就注释成「服务端内部
# 细节，不回异常类名」。所以只对**已知等价**的这几个文案折叠后再比，且折叠
# 时打 NOTE（不静默）。
_EXC_PFX = "请求参数不合法:"
_EXC_EQUIV = {
    "unhashable type: 'slice'": "<PY_VERSION_SPECIFIC:dict-slice>",
    "slice(None, 12, None)": "<PY_VERSION_SPECIFIC:dict-slice>",
}
_FOLDED_EXC = []


def fold_exc(js):
    """把异常文案族里「同一语义、不同 CPython 版本」的文案折成同一个标记。"""
    if isinstance(js, str):
        if js.startswith(_EXC_PFX):
            tail = js[len(_EXC_PFX):]
            if tail in _EXC_EQUIV:
                if tail not in _FOLDED_EXC:
                    _FOLDED_EXC.append(tail)
                return _EXC_PFX + _EXC_EQUIV[tail]
        return js
    if isinstance(js, list):
        return [fold_exc(x) for x in js]
    if isinstance(js, dict):
        return {k: fold_exc(v) for k, v in js.items()}
    return js


def _b64_is_lenient():
    """本机参考解释器是否「接受多余填充」。3.11.0/3.12.4 接受 "abcd="，
    3.12.10 起拒绝（Excess padding not allowed）。C 跟新口径，所以参考版
    宽松时那条场景只能跳过 —— 否则比的是两个 Python 版本的差别。"""
    try:
        base64.b64decode("abcd=")
        return True
    except Exception:
        return False


B64_LENIENT = _b64_is_lenient()
SKIP_SCENARIOS = set()
if B64_LENIENT:
    SKIP_SCENARIOS.add("resolve base64 无填充（abcd=）→ commit")

# 每个 fixture 的场景：[(场景名, [(path, body[, raw]), ...]), ...]]
SCENARIOS = {
    "modify": [
        ("resolve 参数校验（全部 400，不改状态）", [
            (R, {"file": "a.txt"}),
            (R, {"file": "a.txt", "content": None}),
            (R, {"file": "a.txt", "content": 5}),
            (R, {"file": "a.txt", "content": 1.5}),
            (R, {"file": "a.txt", "content": True}),
            (R, {"file": "a.txt", "content": [1]}),
            (R, {"file": "a.txt", "content": {}}),
            (R, {"file": "a.txt", "content": "x", "encoding": "latin-1"}),
            (R, {"file": "a.txt", "content": "x", "encoding": 5}),
            (R, {"file": "a.txt", "content": "x", "encoding": 5.0}),
            (R, {"file": "a.txt", "content": "x", "encoding": True}),
            (R, {"file": "a.txt", "content": "x", "encoding": {"a": 1}}),
            (R, {"file": "a.txt", "content": "x", "encoding": "utf-16"}),
            (R, {"file": "a.txt", "content": "x", "expect_sha256": "0" * 64}),
            (R, {"file": "a.txt", "content": "x", "expect_sha256": 12345}),
            (R, {"file": "a.txt", "content": "x", "expect_sha256": True}),
            (R, {"file": "a.txt", "content": "x", "expect_sha256": [1, 2]}),
            (R, {"file": "a.txt", "content": "x", "expect_sha256": {"a": 1}}),
            (R, {"file": "a.txt", "content": "@@@@", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "abcde", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "QQ==QQ==", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "QQ==\n", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "QQ=", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "=QQ=", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "AA=A", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "A", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "A===", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "abc===", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "\u00e9", "encoding": "base64"}),
            (R, {"file": "a.txt", "content": "Q Q==", "encoding": "base64"}),
            (R, {"content": "x"}),
            (R, {"file": None, "content": "x"}),
            (R, {"file": 5, "content": "x"}),
            (R, {"file": "", "content": "x"}),
            (R, {"file": ".", "content": "x"}),
            (R, {"file": "a.txt/..", "content": "x"}),
            (R, {"file": " ", "content": "x"}),
            (R, {"file": "a.txt\u0000x", "content": "x"}),
            (R, {"file": "keep.txt", "content": "x"}),
            (R, {"file": ".git/config", "content": "x"}),
            (R, {"file": "../out.txt", "content": "x"}),
            (R, {"file": "C:/Windows/x.txt", "content": "x"}),
            (R, None, b"{oops"),
            (R, None, b"[1]"),
            (R, None, b"null"),
            (R, None, b""),
        ]),
        ("resolve encoding=text → commit", [
            (R, {"file": "a.txt", "content": "t\n", "encoding": "text"}),
            (M, {}),
        ]),
        ("resolve encoding=null → commit", [
            (R, {"file": "a.txt", "content": "n\n", "encoding": None}),
            (M, {}),
        ]),
        ("resolve expect_sha256=null → commit", [
            (R, {"file": "a.txt", "content": "s\n", "expect_sha256": None}),
            (M, {}),
        ]),
        ("resolve expect_sha256=空串（falsy，不校验）→ commit", [
            (R, {"file": "a.txt", "content": "e\n", "expect_sha256": ""}),
            (M, {}),
        ]),
        ("resolve base64 空串（清空）→ commit", [
            (R, {"file": "a.txt", "content": "", "encoding": "base64"}),
            (M, {}),
        ]),
        ("resolve base64 无填充（abcd=）→ commit", [
            (R, {"file": "a.txt", "content": "abcd=", "encoding": "base64"}),
            (M, {}),
        ]),
        ("resolve 成功 → 再 resolve → commit → 再 commit → abort", [
            (R, {"file": "a.txt", "content": "resolved\n"}),
            (R, {"file": "a.txt", "content": "again\n"}),
            (M, {}),
            (M, {"message": "again"}),
            (A, {}),
        ]),
        ("resolve expect_sha256 错 → 对 → commit", [
            (R, {"file": "a.txt", "content": "x\n", "expect_sha256": "0" * 64}),
            (R, {"file": "a.txt", "content": "x\n",
                 "expect_sha256": FILESHA + "a.txt"}),
            (M, {"message": "sha guard"}),
        ]),
        ("resolve 空串清空文件", [
            (R, {"file": "a.txt", "content": ""}),
            (M, {}),
        ]),
        ("resolve base64 写二进制", [
            (R, {"file": "a.txt", "content": B64(b"bin\x00ary\n"), "encoding": "base64"}),
            (M, {"message": "b64"}),
        ]),
        ("choose ours → commit", [
            (CH, {"file": "a.txt", "side": "ours"}),
            (M, {}),
        ]),
        ("choose theirs → commit", [
            (CH, {"file": "a.txt", "side": "theirs"}),
            (M, {"message": "take theirs"}),
        ]),
        ("choose base → commit", [
            (CH, {"file": "a.txt", "side": "base"}),
            (M, {"message": "take base"}),
        ]),
        ("choose 非法值（全部 400）", [
            (CH, {"file": "a.txt", "side": "mine"}),
            (CH, {"file": "a.txt", "side": 5}),
            (CH, {"file": "a.txt", "side": 1.5}),
            (CH, {"file": "a.txt", "side": 5.0}),
            (CH, {"file": "a.txt", "side": True}),
            (CH, {"file": "a.txt", "side": None}),
            (CH, {"file": "a.txt", "side": ["ours"]}),
            (CH, {"file": "a.txt", "side": {"a": 1}}),
            (CH, {"file": "a.txt"}),
            (CH, {"side": "ours"}),
            (CH, {"file": "keep.txt", "side": "ours"}),
            (CH, None, b"{bad"),
            (CH, None, b"[1]"),
            (CH, {"file": "a.txt", "side": "ours"}),
        ]),
        ("delete 冲突文件 → commit（Python 契约：commit 会 400）", [
            (D, {"file": "a.txt"}),
            (D, {"file": "a.txt"}),
            (M, {"message": "del merge"}),
        ]),
        ("delete 非冲突文件 / sha 不符（全部 400）", [
            (D, {"file": "keep.txt"}),
            (D, {"file": "a.txt", "expect_sha256": "0" * 64}),
            (D, {"file": "a.txt", "expect_sha256": 7}),
            (D, {"file": "a.txt", "expect_sha256": None}),
            (D, {"file": "a.txt", "expect_sha256": ""}),
            (D, {}),
            (D, None, b"[1]"),
        ]),
        ("commit 有冲突未解决 / 参数错 / 无合并态", [
            (M, {}),
            (M, {"message": 5}),
            (M, {"message": 1.5}),
            (M, {"message": True}),
            (M, {"message": ["x"]}),
            (M, {"message": None}),
            (M, None, b'"x"'),
            (M, None, b""),
            (A, {}),
            (A, {}),
            (M, {}),
        ]),
        ("abort 合并态 → 再 abort → resolve 拒绝", [
            (A, {}),
            (A, {}),
            (R, {"file": "a.txt", "content": "x"}),
        ]),
    ],
    "addadd": [
        ("add-add：choose base 被拒绝，choose ours 成功", [
            (CH, {"file": "new.txt", "side": "base"}),
            (CH, {"file": "new.txt", "side": "ours"}),
            (M, {}),
        ]),
        ("add-add：choose theirs → commit", [
            (CH, {"file": "new.txt", "side": "theirs"}),
            (M, {"message": "take theirs"}),
        ]),
        ("add-add：resolve → commit", [
            (R, {"file": "new.txt", "content": "merged\n"}),
            (M, {}),
        ]),
        ("add-add：delete → commit", [
            (D, {"file": "new.txt"}),
            (M, {}),
        ]),
        ("add-add：abort 合并态 → 写端点全拒绝", [
            (A, {}),
            (A, {}),
            (R, {"file": "new.txt", "content": "x"}),
            (M, {}),
        ]),
    ],
    "delmod": [
        ("delete-modify：choose theirs 被拒绝，ours 成功", [
            (CH, {"file": "del.txt", "side": "theirs"}),
            (CH, {"file": "del.txt", "side": "ours"}),
            (M, {}),
        ]),
        ("delete-modify：choose base → commit", [
            (CH, {"file": "del.txt", "side": "base"}),
            (M, {"message": "base"}),
        ]),
        ("delete-modify：delete → commit", [
            (D, {"file": "del.txt"}),
            (M, {}),
        ]),
        ("delete-modify：resolve 复活文件 → commit", [
            (R, {"file": "del.txt", "content": "revived\n"}),
            (M, {}),
        ]),
        ("delete-modify：abort 合并态 → 写端点全拒绝", [
            (A, {}),
            (R, {"file": "del.txt", "content": "x"}),
            (CH, {"file": "del.txt", "side": "ours"}),
            (M, {}),
        ]),
    ],
    "binary": [
        ("二进制：choose 全被拒绝，resolve base64 成功", [
            (CH, {"file": "bin.dat", "side": "ours"}),
            (CH, {"file": "bin.dat", "side": "theirs"}),
            (CH, {"file": "bin.dat", "side": "base"}),
            (R, {"file": "bin.dat", "content": B64(b"\x00\x01merged\n"),
                 "encoding": "base64"}),
            (M, {}),
        ]),
        ("二进制：resolve 文本 → commit", [
            (R, {"file": "bin.dat", "content": "plain text\n"}),
            (M, {"message": "text over binary"}),
        ]),
        ("二进制：delete → commit", [
            (D, {"file": "bin.dat"}),
            (M, {}),
        ]),
        ("二进制：abort 合并态 → 写端点全拒绝", [
            (A, {}),
            (R, {"file": "bin.dat", "content": "x"}),
            (D, {"file": "bin.dat"}),
            (M, {}),
        ]),
    ],
    "rich": [
        ("rich：全部 resolve → commit（默认消息）", [
            (R, {"file": "a.txt", "content": "r\n"}),
            (R, {"file": "del.txt", "content": "r\n"}),
            (R, {"file": "中文 空格.txt", "content": "r\n"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "r\n"}),
            (M, {}),
        ]),
        ("rich：choose 组合 → commit（自带消息 + Merged: 分支）", [
            (CH, {"file": "a.txt", "side": "ours"}),
            (CH, {"file": "del.txt", "side": "ours"}),
            (CH, {"file": "中文 空格.txt", "side": "theirs"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "r\n"}),
            (M, {"message": "my merge"}),
        ]),
        ("rich：commit 前有未解决 → 400", [
            (M, {}),
            (R, {"file": "a.txt", "content": "r\n"}),
            (M, {"message": "still left"}),
        ]),
        ("rich：空白 message（messageSource=MERGE_MSG）+ 分支名已在消息里", [
            (R, {"file": "a.txt", "content": "r\n"}),
            (R, {"file": "del.txt", "content": "r\n"}),
            (R, {"file": "中文 空格.txt", "content": "r\n"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "r\n"}),
            (M, {"message": "   "}),
        ]),
        ("rich：message 已含分支名 → 不补 Merged:", [
            (R, {"file": "a.txt", "content": "r\n"}),
            (R, {"file": "del.txt", "content": "r\n"}),
            (R, {"file": "中文 空格.txt", "content": "r\n"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "r\n"}),
            (M, {"message": "feature already merged"}),
        ]),
        ("rich：delete 一个冲突文件后 commit（Python 契约：400）", [
            (D, {"file": "addnew.txt"}),
            (R, {"file": "a.txt", "content": "r\n"}),
            (R, {"file": "del.txt", "content": "r\n"}),
            (R, {"file": "中文 空格.txt", "content": "r\n"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (M, {"message": "del merge"}),
        ]),
        ("rich：路径变体（空格/点斜杠/上跳/制表符，每个文件一次）", [
            (R, {"file": " a.txt ", "content": "q\n"}),
            (R, {"file": "./del.txt", "content": "q\n"}),
            (R, {"file": "sub/../中文 空格.txt", "content": "q\n"}),
            (R, {"file": "\tbin.dat\n", "content": B64(b"q\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "q\n"}),
            (M, {}),
        ]),
        ("rich：abort 后所有写端点都拒绝", [
            (A, {}),
            (R, {"file": "a.txt", "content": "x"}),
            (CH, {"file": "a.txt", "side": "ours"}),
            (D, {"file": "a.txt"}),
            (M, {}),
            (A, {}),
        ]),
        ("rich：仓外目录联接 → commit 被 F-06 防线拒绝", [
            (R, {"file": "a.txt", "content": "r\n"}),
            (R, {"file": "del.txt", "content": "r\n"}),
            (R, {"file": "中文 空格.txt", "content": "r\n"}),
            (R, {"file": "bin.dat", "content": B64(b"r\n"), "encoding": "base64"}),
            (R, {"file": "addnew.txt", "content": "r\n"}),
            (JUNCTION, "escape"),
            (M, {"message": "leak merge"}),
            (M, {}),
        ]),
    ],
    "clean": [
        ("非合并态：写端点全部拒绝（abort 除外）", [
            (R, {"file": "a.txt", "content": "x"}),
            (CH, {"file": "a.txt", "side": "ours"}),
            (D, {"file": "a.txt"}),
            (M, {}),
            (M, {"message": "x"}),
            (A, {}),
            (A, {}),
            (R, {"file": "a.txt", "content": "x"}),
        ]),
        ("非合并态：参数错误仍然逐字一致", [
            (R, {"file": "a.txt"}),
            (R, {"file": 5, "content": "x"}),
            (CH, {"file": "a.txt", "side": "nope"}),
            (M, {"message": 5}),
            (R, None, b"{oops"),
            (M, None, b"[]"),
            (D, None, b""),
        ]),
    ],
}


# ---------------------------------------------------------------- 跑

def restore(tpl, dst):
    if os.path.isdir(dst):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", dst], capture_output=True)
    shutil.copytree(tpl, dst)


def run_scenario(fixture, name, steps, env, results):
    """恢复两份副本 → 起两个服务 → 逐步比对 → 比最终状态。"""
    normalize_abs = any(s[0] == JUNCTION for s in steps)
    tpl = os.path.join(BASE, fixture, "tpl")
    ra = os.path.join(BASE, fixture, "a")
    rb = os.path.join(BASE, fixture, "b")
    restore(tpl, ra)
    restore(tpl, rb)
    pa, pb = free_port(), free_port()
    pp = start_py(ra, pa, env)
    pc = start_c(rb, pb, env)
    fails = 0
    positives = 0
    try:
        if not wait_health(pa, pp) or not wait_health(pb, pc):
            print("  FAIL %-8s %-46s 服务没起来" % (fixture, name))
            for step in steps:
                results[(fixture, step[0])] = "FAIL"
            results[(fixture, "state")] = "FAIL"
            return 1, 0
        for i, step in enumerate(steps):
            path = step[0]
            raw = step[2] if len(step) > 2 else None
            if path == JUNCTION:
                # 不是请求，是布景：给两个仓各挂一个指向**同一个**仓外目录的目录联接
                make_junction(ra, step[1], OUTSIDE)
                make_junction(rb, step[1], OUTSIDE)
                print("  STEP %-8s #%02d 建目录联接 %s -> %s" % (fixture, i, step[1], OUTSIDE))
                continue
            body_a = expand(step[1], ra)
            body_b = expand(step[1], rb)
            label = "%s %s" % (path, json.dumps(step[1], ensure_ascii=False)[:56])
            try:
                ca, ja = post(pa, path, body_a, raw)
            except Exception as e:
                ca, ja = "EXC", {"__err__": str(e)}
            try:
                cb, jb = post(pb, path, body_b, raw)
            except Exception as e:
                cb, jb = "EXC", {"__err__": str(e)}
            if ca == 200:
                positives += 1
            if normalize_abs:
                ja = normalize(ja, ra)
                jb = normalize(jb, rb)
            ok = (ca == cb and fold_exc(ja) == fold_exc(jb)
                  and ca != 500 and cb != 500)
            if ok:
                print("  PASS %-8s #%02d %-60s code=%s" % (fixture, i, label[:60], ca))
                if results.get((fixture, path)) != "FAIL":
                    results[(fixture, path)] = "PASS"
            else:
                fails += 1
                results[(fixture, path)] = "FAIL"
                print("  FAIL %-8s #%02d %-60s" % (fixture, i, label[:60]))
                print("       python: code=%s %s" % (ca, json.dumps(ja, ensure_ascii=False)[:500]))
                print("       c     : code=%s %s" % (cb, json.dumps(jb, ensure_ascii=False)[:500]))
        sa = state(ra, env)
        sb = state(rb, env)
        bad = diff_state(sa, sb)
        if bad:
            fails += 1
            results[(fixture, "state")] = "FAIL"
            print("  FAIL %-8s 最终状态 %d 项不一致（场景「%s」）" % (fixture, len(bad), name))
            for k in bad:
                if k == "files":
                    for f in sorted(set(sa["files"]) | set(sb["files"])):
                        if sa["files"].get(f) != sb["files"].get(f):
                            print("       files[%s] py=%s c=%s"
                                  % (f, (sa["files"].get(f) or "-")[:12],
                                     (sb["files"].get(f) or "-")[:12]))
                elif k in ("index", "refs", "status"):
                    onlya = [x for x in (sa[k] or []) if x not in (sb[k] or [])][:4]
                    onlyb = [x for x in (sb[k] or []) if x not in (sa[k] or [])][:4]
                    print("       %s py-only=%r" % (k, onlya))
                    print("       %s c-only =%r" % (" " * len(k), onlyb))
                else:
                    print("       %-10s py=%r" % (k, sa.get(k)))
                    print("       %-10s c =%r" % ("", sb.get(k)))
        else:
            print("  PASS %-8s 最终状态一致 head=%s（场景「%s」）"
                  % (fixture, (sa.get("head") or "-")[:10], name))
            if results.get((fixture, "state")) != "FAIL":
                results[(fixture, "state")] = "PASS"
        # 原始 .git/index 字节**不**参与判定：它记录 ino/dev/时间戳，两份副本
        # 本来就是不同的文件，比字节必然不同。这里只报一句，说明用的是
        # 「逻辑索引」（ls-files -s 的哈希）。
        ia = sha256_bytes(read_maybe(os.path.join(ra, ".git", "index")) or b"")
        ib = sha256_bytes(read_maybe(os.path.join(rb, ".git", "index")) or b"")
        if ia != ib:
            print("  NOTE %-8s 原始 .git/index 字节不同（ino/dev/mtime 不同，"
                  "逻辑索引已比平）" % fixture)
        if positives == 0:
            print("  NOTE %-8s 场景「%s」全是 4xx（该 fixture 另有阳性场景即可）"
                  % (fixture, name))
        return fails, positives
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


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    if not os.path.isfile(os.path.join(PY_ROOT, "server.py")):
        print("  缺 Python 参考版 %s" % PY_ROOT)
        return 1
    # 干净的 global config：两边看到的 core.* 语义键都一样（空）
    cfg = os.path.join(BASE, "cfg_empty")
    with open(cfg, "w", encoding="utf-8", newline="\n") as f:
        f.write("")
    env = dict(os.environ, **FIXED)
    env["GIT_CONFIG_GLOBAL"] = cfg

    # F-06 场景的仓外哨兵目录（两个仓指向**同一个**目录，只有仓根不同）
    os.makedirs(OUTSIDE, exist_ok=True)
    with open(os.path.join(OUTSIDE, "leak.txt"), "w", encoding="utf-8") as f:
        f.write("OUTSIDE-SECRET\n")

    results = {}
    fails = 0
    try:
        for tag, fn in FIXTURES:
            tpl = os.path.join(BASE, tag, "tpl")
            fn(tpl, env)
            print("[fixture] %-8s %s" % (tag, tpl))
        for tag, _ in FIXTURES:
            print("[%s]" % tag)
            tot = 0
            for name, steps in SCENARIOS.get(tag, []):
                if name in SKIP_SCENARIOS:
                    print("  NOTE %-8s 跳过「%s」：本机参考版 Python 的 "
                          "b64decode 接受多余填充，C 跟新版（3.12.10）严格口径"
                          % (tag, name))
                    continue
                f, pos = run_scenario(tag, name, steps, env, results)
                fails += f
                tot += pos
            if tot == 0:
                fails += 1
                print("  FAIL %-8s 整个 fixture 没有阳性结果（全是 4xx）" % tag)
        print()
        print("汇总（行=端点, 列=fixture；PASS = 该 fixture 里这个端点的每一步")
        print("      都比平 + 该 fixture 的最终状态也比平）")
        eps = [R, CH, D, M, A, "state"]
        cols = [t for t, _ in FIXTURES]
        print("%-28s %s" % ("endpoint", " ".join("%-9s" % c for c in cols)))
        for ep in eps:
            row = "%-28s" % ep
            for c in cols:
                row += " %-9s" % results.get((c, ep), "-")
            print(row)
        print("失败 %d" % fails)
        if _FOLDED_EXC:
            print("  NOTE 异常文案折过：%r —— CPython 补丁版本之间这几个文案不同"
                  "（3.11.0/3.12.4 是 TypeError(unhashable)，3.12.10 是 "
                  "KeyError(slice)），C 只能对齐一种口径。"
                  % (_FOLDED_EXC,))
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
