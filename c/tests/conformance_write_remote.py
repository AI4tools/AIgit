# -*- coding: utf-8 -*-
"""M2 远程写端点一致性判据：C 版 vs Python 版（同一份 fixture、同一个 bare remote）。

覆盖 3 个端点：
    POST /api/remote/fetch  {"remote","branch","refspec"}
    POST /api/remote/pull   {"remote","branch","confirmed"}
    POST /api/remote/push   {"remote","branch","force","delete","confirmed"}

方法（这是本分片与其它分片最大的不同：这些端点**会改状态**）：
    * 每个 fixture 先建一份模板仓 + 一份 bare remote，再为 Python / C 各复制
      一份**完全相同的仓副本**（A 给 Python、B 给 C）；
    * bare remote 只有**一个**（同一个路径）：每一步先把它恢复成「本步起始
      状态」，跑 Python 侧；再恢复成同一状态，跑 C 侧。于是两侧看到的是
      逐字节相同的远端起点，而不是「Python 推完 C 再来推」；
    * 每步比：HTTP 码 + JSON 深比较（路径归一化）+ 两侧本地仓快照
      （HEAD / 全部 refs / refs/remotes / MERGE_HEAD / status --porcelain）
      + bare 仓 refs；
    * 收口再比一次两侧的 refs / HEAD / .git/refs/remotes/*。

覆盖的能力闸档（只在 gate fixture 上全跑，其余 fixture 跑 write）：
    AIGIT_NET 未设 / off / read / write / WrItE（大小写混杂）/ Raed（读不懂 -> off）

判据：全等才算 PASS；两侧都不许出现 5xx。每个 fixture 必须有阳性（200）结果，
否则算失败（防止「全 400 通过」）。

用法：
    python -X utf8 c/tests/conformance_write_remote.py
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
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
_PY_SIBLING = os.path.join(os.path.dirname(C_ROOT), "python-ref", "server.py")
PY_ROOT = os.path.dirname(_PY_SIBLING) if os.path.isfile(_PY_SIBLING) else r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ.get("TEMP") or "/tmp",
                    "aigit_c_wr_%d" % (os.getpid() % 100000))

# ---------------------------------------------------------------- git 环境

GIT_ENV = dict(os.environ)
GIT_ENV.pop("AIGIT_NET", None)
GIT_ENV.update({
    "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@localhost",
    # 固定时间 -> 两侧的提交 SHA 完全一致（push/pull 的输出里带 SHA）
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+0000",
})


def sh(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=GIT_ENV)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:400]))
    return p.stdout


def rmtree(p):
    if not os.path.isdir(p):
        return
    def onerror(func, path, exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass
    shutil.rmtree(p, onerror=onerror)


def copy_tree(src, dst):
    rmtree(dst)
    shutil.copytree(src, dst)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------------------------------------------------------------- fixture 基础

def new_repo(path):
    os.makedirs(path)
    must(path, "init", "-q", "-b", "main")
    must(path, "config", "user.name", "bench")
    must(path, "config", "user.email", "bench@localhost")
    must(path, "config", "core.autocrlf", "false")
    must(path, "config", "commit.gpgsign", "false")
    # 分叉 pull 走 merge（而不是 rebase / 拒绝），才能测到冲突分支
    must(path, "config", "pull.rebase", "false")
    must(path, "config", "gc.auto", "0")
    return path


def new_bare(path):
    os.makedirs(path)
    must(path, "init", "-q", "--bare", "-b", "main")
    return path


def commit(d, name, text, msg):
    with open(os.path.join(d, name), "wb") as f:
        f.write(text)
    must(d, "add", "--", name)
    must(d, "commit", "-q", "-m", msg)


def make_variant(src_bare, dst_bare, commits=(), refs=()):
    """从 src_bare 复制出 dst_bare，可选地再推进 main / 加 ref（全部在副本上做）。"""
    copy_tree(src_bare, dst_bare)
    if commits:
        work = dst_bare + "_work"
        rmtree(work)
        must(os.path.dirname(dst_bare), "clone", "-q", dst_bare, work)
        for (name, text, msg) in commits:
            commit(work, name, text, msg)
        must(work, "push", "-q", "origin", "main")
        rmtree(work)
    for (ref, sha) in refs:
        must(dst_bare, "update-ref", ref, sha)
    return dst_bare


def fx_root(name):
    d = os.path.join(BASE, name, "tmpl")
    os.makedirs(d)
    return d


# ---------------------------------------------------------------- fixtures

def fx_gate():
    t = fx_root("gate")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    commit(repo, "a.txt", b"a\n", "c1")
    must(repo, "remote", "add", "origin", bare)
    must(repo, "push", "-q", "-u", "origin", "main")
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    return {"repo": repo, "bare": bare, "variants": v}


def fx_fetch():
    t = fx_root("fetch")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    commit(repo, "a.txt", b"a\n", "c1")
    must(repo, "remote", "add", "origin", bare)
    must(repo, "push", "-q", "-u", "origin", "main")
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    v["ff"] = make_variant(v["base"], os.path.join(t, "v", "ff"),
                           commits=[("a.txt", b"a2\n", "c2")])
    c1 = must(v["base"], "rev-parse", "main").strip()
    v["newbr"] = make_variant(v["base"], os.path.join(t, "v", "newbr"),
                              refs=[("refs/heads/newbr", c1)])
    return {"repo": repo, "bare": bare, "variants": v}


def fx_pull():
    t = fx_root("pull")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    commit(repo, "a.txt", b"a\n", "c1")
    must(repo, "remote", "add", "origin", bare)
    must(repo, "push", "-q", "-u", "origin", "main")
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    v["ff"] = make_variant(v["base"], os.path.join(t, "v", "ff"),
                           commits=[("a.txt", b"a2\n", "c2")])
    v["ff3"] = make_variant(v["ff"], os.path.join(t, "v", "ff3"),
                            commits=[("a.txt", b"a3\n", "c3")])
    v["diverge"] = make_variant(v["base"], os.path.join(t, "v", "diverge"),
                                commits=[("a.txt", b"theirs\n", "c2t")])
    return {"repo": repo, "bare": bare, "variants": v}


def fx_push():
    t = fx_root("push")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    commit(repo, "a.txt", b"a\n", "c1")
    must(repo, "remote", "add", "origin", bare)
    must(repo, "push", "-q", "-u", "origin", "main")
    c1 = must(repo, "rev-parse", "HEAD").strip()
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    v["ff"] = make_variant(v["base"], os.path.join(t, "v", "ff"),
                           commits=[("a.txt", b"theirs\n", "c2t")])
    v["feat"] = make_variant(v["base"], os.path.join(t, "v", "feat"),
                             refs=[("refs/heads/feat/x", c1)])
    v["empty"] = new_bare(os.path.join(t, "v", "empty.git"))
    return {"repo": repo, "bare": bare, "variants": v}


def fx_unborn():
    t = fx_root("unborn")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    must(repo, "remote", "add", "origin", bare)
    with open(os.path.join(repo, "u.txt"), "wb") as f:
        f.write(b"u\n")
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    return {"repo": repo, "bare": bare, "variants": v}


def fx_noremote():
    t = fx_root("noremote")
    bare = new_bare(os.path.join(t, "bare.git"))
    repo = new_repo(os.path.join(t, "repo"))
    commit(repo, "a.txt", b"a\n", "c1")
    v = {"base": make_variant(bare, os.path.join(t, "v", "base"))}
    return {"repo": repo, "bare": bare, "variants": v}


# ---------------------------------------------------------------- 步骤

def S(path, body, prep_bare=None, prep_local=None):
    return {"path": path, "body": body, "prep_bare": prep_bare,
            "prep_local": prep_local or []}


def dirty(path, text):
    def f(repo):
        with open(os.path.join(repo, path), "wb") as fh:
            fh.write(text)
    return f


def rename_dotgit(repo):
    g = os.path.join(repo, ".git")
    if os.path.isdir(g):
        os.rename(g, os.path.join(repo, ".git_off"))


F = "/api/remote/fetch"
P = "/api/remote/pull"
U = "/api/remote/push"

GATE_STEPS = [
    S(F, {"remote": "origin"}),
    S(P, {"remote": "origin", "confirmed": True}),
    S(U, {"remote": "origin", "confirmed": True}),
    S(U, {"remote": "origin", "confirmed": False}),      # 闸先于 confirmed
    S(U, {"remote": "a b", "confirmed": True}),          # 名校验先于闸
    S(P, {"remote": "a b", "confirmed": True}),
]

FETCH_STEPS = [
    S(F, {"remote": "origin"}, "base"),
    S(F, {"remote": "origin"}, "ff"),                    # 快进 origin/main
    S(F, {"remote": "origin"}, "base"),                  # 强制 refspec 把跟踪 ref 拨回去
    S(F, {"remote": "origin", "branch": "main"}, "base"),
    S(F, {"remote": "origin", "refspec": "main"}, "base"),
    S(F, {"remote": "origin", "refspec": "refs/heads/main"}, "base"),
    S(F, {"remote": "origin", "refspec": "bad..ref"}, "base"),   # fetch_failed
    S(F, {"remote": "origin", "refspec": "-x"}, "base"),         # bad_ref
    S(F, {"remote": "origin", "refspec": "a:b"}, "base"),        # bad_ref
    S(F, {"remote": "origin", "refspec": ""}, "base"),           # 空串 -> bad_ref
    S(F, {"remote": "origin", "branch": "newbr"}, "newbr"),      # 新分支
    S(F, {"remote": "origin", "branch": "main", "refspec": "main"}, "base"),
    S(F, {"branch": ""}, "base"),                        # 空值 = 没给
    S(F, None, "base"),                                  # 空 body = {}
    S(F, {"remote": None}, "base"),                      # bad_remote: None
    S(F, {"remote": 123}, "base"),                       # bad_remote: 123
    S(F, {"remote": "origin", "refspec": None}, "base"), # null == 没给
    S(F, {"remote": "origin", "branch": 5}, "base"),     # branch 不校验 -> fetch 5
    S(F, {"remote": "nope"}, "base"),                    # fetch_failed
    S(F, {"remote": "a b"}, "base"),                     # bad_remote
    S(F, {"remote": "中文"}, "base"),                    # bad_remote（非 ASCII）
    S(F, {"remote": "ori/gin"}, "base"),                 # bad_remote（含斜杠）
    S(F, {"remote": "a" * 100}, "base"),                 # 边界：100 字符合法 -> fetch_failed
    S(F, {"remote": "a" * 101}, "base"),                 # 边界：101 字符 -> bad_remote
    S(F, {"remote": "origin", "refspec": "a b"}, "base"),   # bad_ref（空白）
    S(F, {"remote": "origin", "refspec": "a" * 201}, "base"),  # bad_ref（超长）
]

PULL_STEPS = [
    S(P, {"remote": "origin", "confirmed": True}, "ff"),      # 快进
    S(P, {"remote": "origin", "confirmed": True}, "ff"),      # 已最新
    S(P, {"remote": "origin", "confirmed": True}, "base"),    # 不倒退
    S(P, {"remote": "origin", "confirmed": False}),           # needs_confirmation
    S(P, {"remote": "origin"}),                               # needs_confirmation
    S(P, {"remote": "origin", "confirmed": "true"}),          # _bb 类型错
    S(P, {"remote": "origin", "confirmed": 1}),               # _bb 类型错
    S(P, {"remote": "a b", "confirmed": True}),               # bad_remote
    S(P, {"remote": "nope", "confirmed": True}),              # pull_failed
    S(P, {"remote": "origin", "confirmed": True}, "ff3",
      prep_local=[dirty("a.txt", b"dirty\n")]),               # 脏工作区
    S(P, {"remote": "origin", "confirmed": True}, "diverge",
      prep_local=[("checkout", "--", "a.txt")]),              # 合并冲突
    S(P, {"remote": "origin", "branch": "main", "confirmed": True}, "base",
      prep_local=[("merge", "--abort")]),
    S(P, {"remote": "origin", "branch": "-x", "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": "a:b", "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": 5, "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": "a b", "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": "a" * 201, "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": 0, "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": "", "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": None, "confirmed": True}, "base"),
    S(P, {"remote": "origin", "branch": "refs/heads/main",
          "confirmed": True}, "base"),
]

PUSH_STEPS = [
    S(U, {"remote": "origin", "confirmed": True}, "base"),    # 已最新
    S(U, {"remote": "origin", "confirmed": True}, "base",
      prep_local=[("commit", "-q", "--allow-empty", "-m", "c2")]),
    S(U, {"remote": "origin", "confirmed": True}),            # 已最新
    S(U, {"remote": "origin", "confirmed": True}, "ff"),      # 非快进拒绝
    S(U, {"remote": "origin", "force": True, "confirmed": True}, "ff"),
    S(U, {"remote": "origin", "branch": "feat/x", "delete": True,
          "confirmed": True}, "feat"),                        # 删远程分支
    S(U, {"remote": "origin", "delete": True, "confirmed": True}, "base"),
    S(U, {"remote": "origin", "confirmed": True}, "empty"),   # 新分支
    S(U, {"remote": "origin", "branch": "feat/new",
          "confirmed": True}, "base", prep_local=[("branch", "feat/new")]),
    S(U, {"remote": "origin", "branch": "nope/nope",
          "confirmed": True}, "base"),                        # 本地没有这个分支
    S(U, {"remote": "nope", "confirmed": True}, "base"),      # push_failed
    S(U, {"remote": "a b", "confirmed": True}, "base"),       # bad_remote
    S(U, {"remote": "origin", "confirmed": False}),           # needs_confirmation
    S(U, {}),                                                 # needs_confirmation
    S(U, {"remote": "origin", "confirmed": "true"}),
    S(U, {"remote": "origin", "force": "yes", "confirmed": True}),
    S(U, {"remote": "origin", "delete": 1, "confirmed": True}),
    S(U, {"remote": "origin", "force": True, "delete": True,
          "confirmed": True}, "base"),                        # delete 优先
    S(U, {"remote": "origin", "branch": "-x", "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": "a:b", "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": 5, "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": "a b", "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": "a" * 201, "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": 0, "confirmed": True}, "base"),
    S(U, {"remote": None, "confirmed": True}, "base"),
    S(U, {"remote": "origin", "branch": "", "confirmed": True}, "base"),
]

UNBORN_STEPS = [
    S(F, {"remote": "origin"}),                               # 200（未出生也能 fetch）
    S(U, {"confirmed": True}),                                # HEAD -> push_failed
    S(U, {"branch": "main", "confirmed": True}),              # push_failed
    S(P, {"confirmed": True}),                                # 无 upstream -> pull_failed
    S(U, {"confirmed": True}, prep_local=[
        ("add", "--", "u.txt"), ("commit", "-q", "-m", "c1")]),  # 阳性：建远程 main
    S(F, {"remote": "origin"}),
    S(P, {"branch": "main", "confirmed": True}),
    S(P, {"confirmed": True}),                                # 仍无 upstream
]

NOREMOTE_STEPS = [
    S(F, {}),                                                 # fetch_failed
    S(P, {"confirmed": True}),                                # pull_failed
    S(U, {"confirmed": True}),                                # push_failed
    S(U, {"remote": "nope", "confirmed": True}),              # push_failed
    S(U, {"confirmed": True}, prep_local=[
        ("remote", "add", "origin", "<BARE>")]),              # 阳性：加 remote 后推成功
    S(F, {}),
    S(P, {"branch": "main", "confirmed": True}),
    S(F, {}, prep_local=[rename_dotgit]),                     # not_a_repo
    S(P, {"confirmed": True}, prep_local=[rename_dotgit]),
    S(U, {"confirmed": True}, prep_local=[rename_dotgit]),
]

FIXTURES = [
    ("gate", fx_gate, GATE_STEPS, [None, "off", "read", "write", "WrItE", "Raed"]),
    ("fetch", fx_fetch, FETCH_STEPS, ["write"]),
    ("pull", fx_pull, PULL_STEPS, ["write"]),
    ("push", fx_push, PUSH_STEPS, ["write"]),
    ("unborn", fx_unborn, UNBORN_STEPS, ["write"]),
    ("noremote", fx_noremote, NOREMOTE_STEPS, ["write"]),
]


# ---------------------------------------------------------------- 服务

def base_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               AIGIT_GIT_COUNT="1")
    env.pop("AIGIT_NET", None)
    return env


def start_py(repo, port, net):
    env = base_env()
    if net is not None:
        env["AIGIT_NET"] = net
    return subprocess.Popen([sys.executable, "-X", "utf8",
                             os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port, net):
    env = base_env()
    if net is not None:
        env["AIGIT_NET"] = net
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
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


def post(port, path, body, tries=3):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for _ in range(tries):
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                        data=data, method="POST")
            if data is not None:
                rq.add_header("Content-Type", "application/json")
            try:
                r = urllib.request.urlopen(rq, timeout=120)
                code, payload, hdr = r.getcode(), r.read(), r.headers
            except urllib.error.HTTPError as e:
                code, payload, hdr = e.code, e.read(), e.headers
            try:
                js = json.loads(payload.decode("utf-8"))
            except Exception as e:
                js = ("<<not json>>", str(e),
                      payload[:300].decode("utf-8", "replace"))
            return {"code": code, "json": js,
                    "calls": hdr.get("X-aigit-git-calls"),
                    "cmds": hdr.get("X-aigit-git-cmds")}
        except Exception as e:
            last = e
            time.sleep(0.2)
    return {"code": "EXC", "json": repr(last), "calls": None, "cmds": None}


# ---------------------------------------------------------------- 状态快照

def refs_of(repo):
    out = {}
    p = sh(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        bits = line.split(" ", 1)
        if len(bits) == 2:
            out[bits[0]] = bits[1]
    return out


def bare_snapshot(bare):
    if not os.path.isdir(bare):
        return {"exists": False}
    refs = refs_of(bare) or {}
    head = sh(bare, "symbolic-ref", "-q", "HEAD").stdout.strip()
    return {"exists": True, "refs": refs, "head": head}


def local_snapshot(repo):
    d = {"dotgit": os.path.isdir(os.path.join(repo, ".git"))}
    if not d["dotgit"]:
        return d
    d["head_sym"] = sh(repo, "symbolic-ref", "-q", "HEAD").stdout.strip()
    p = sh(repo, "rev-parse", "--verify", "-q", "HEAD")
    d["head_sha"] = p.stdout.strip() if p.returncode == 0 else None
    refs = refs_of(repo) or {}
    d["refs"] = refs
    d["remotes"] = {k: v for k, v in refs.items()
                    if k.startswith("refs/remotes/")}
    mh = os.path.join(repo, ".git", "MERGE_HEAD")
    if os.path.isfile(mh):
        with open(mh, "r", encoding="utf-8", errors="replace") as f:
            d["merge_head"] = f.read().strip()
    else:
        d["merge_head"] = None
    d["status"] = sh(repo, "status", "--porcelain").stdout
    return d


def norm(js, repo, bare):
    s = json.dumps(js, ensure_ascii=False, sort_keys=True)
    for p, tag in ((repo, "<REPO>"), (bare, "<BARE>")):
        if not p:
            continue
        # JSON 串里反斜杠是转义过的，要按转义后的形式替换
        for v in (p, p.replace("\\", "/"), p.replace("/", "\\")):
            esc = json.dumps(v, ensure_ascii=False)[1:-1]
            s = s.replace(esc, tag)
    try:
        return json.loads(s)
    except Exception:
        return s


def short(js, n=460):
    s = json.dumps(js, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "...(len=%d)" % len(s)


def endpoint_of(path):
    return path.rsplit("/", 1)[-1]


# ---------------------------------------------------------------- 主流程

def main():
    rmtree(BASE)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    want = set(sys.argv[1:])
    todo = [f for f in FIXTURES if not want or f[0] in want]
    if not todo:
        print("没有匹配的 fixture: %s（可用: %s）"
              % (" ".join(sorted(want)), " ".join(f[0] for f in FIXTURES)))
        return 2
    print("  Python 参考版: %s" % PY_ROOT)
    print("  C 版 exe     : %s" % EXE)

    matrix = {}          # (endpoint, env) -> {fixture: [pass, fail]}
    ok200 = {}           # fixture -> 200 计数
    effects = {}         # fixture -> 「这一步真的改了本地/远端 refs」的步数
    shapes = {}          # endpoint -> {(code, json.code): 次数}（Python 侧）
    fails_total = cases_total = 0
    call_diffs = []
    state_diffs = []
    per_fixture_pass = {}

    try:
        for fname, builder, steps, envs in todo:
            tmpl = builder()
            bare = tmpl["bare"]
            variants = tmpl["variants"]
            print("\n### fixture %s  (bare=%s)" % (fname, bare))
            for ename in envs:
                label = "unset" if ename is None else repr(ename)
                group = os.path.join(BASE, fname, "run_" + str(ename))
                A = os.path.join(group, "A")
                B = os.path.join(group, "B")
                os.makedirs(group, exist_ok=True)
                copy_tree(tmpl["repo"], A)
                copy_tree(tmpl["repo"], B)
                state = os.path.join(group, "state.git")
                copy_tree(variants["base"], state)

                pa, pb = free_port(), free_port()
                while pb == pa:
                    pb = free_port()
                pp = start_py(A, pa, ename)
                pc = start_c(B, pb, ename)
                try:
                    ok_py = wait_health(pa, pp)
                    ok_c = wait_health(pb, pc)
                    if not (ok_py and ok_c):
                        print("  FAIL [%s/%s] 服务没起来 (python=%s c=%s)"
                              % (fname, label, ok_py, ok_c))
                        fails_total += len(steps)
                        cases_total += len(steps)
                        continue
                    print("\n=== %s  net=%s" % (fname, label))
                    for i, st in enumerate(steps):
                        ep = endpoint_of(st["path"])
                        cell = matrix.setdefault((ep, str(label)), {}).setdefault(
                            fname, [0, 0])

                        # ---- 第一步：恢复 bare -> 跑 Python
                        copy_tree(state, bare)
                        if st["prep_bare"]:
                            copy_tree(variants[st["prep_bare"]], bare)
                        for act in st["prep_local"]:
                            if callable(act):
                                act(A)
                                act(B)
                            else:
                                args = [bare if x == "<BARE>" else x for x in act]
                                must(A, *args)
                                must(B, *args)
                        pre_bare = bare_snapshot(bare)
                        pre_local = local_snapshot(A)
                        a = post(pa, st["path"], st["body"])
                        py_bare = bare_snapshot(bare)
                        py_local = local_snapshot(A)
                        nxt = os.path.join(group, "next_%02d.git" % i)
                        copy_tree(bare, nxt)

                        # ---- 第二步：恢复同一状态 -> 跑 C
                        copy_tree(state, bare)
                        if st["prep_bare"]:
                            copy_tree(variants[st["prep_bare"]], bare)
                        b = post(pb, st["path"], st["body"])
                        c_bare = bare_snapshot(bare)
                        c_local = local_snapshot(B)

                        # ---- 比较
                        ja = norm(a["json"], A, bare)
                        jb = norm(b["json"], B, bare)
                        same_resp = (a["code"] == b["code"] and ja == jb)
                        same_bare = (py_bare == c_bare)
                        same_local = (py_local == c_local)

                        if a["code"] == 200:
                            ok200[fname] = ok200.get(fname, 0) + 1
                        changed = (py_bare != pre_bare or py_local != pre_local)
                        if changed:
                            effects[fname] = effects.get(fname, 0) + 1
                        ck = ja.get("code") if isinstance(ja, dict) else "?"
                        if isinstance(ja, dict) and ja.get("ok") is True:
                            ck = "ok"
                        shapes.setdefault(ep, {})
                        shapes[ep][(a["code"], ck)] = \
                            shapes[ep].get((a["code"], ck), 0) + 1
                        cases_total += 1

                        if same_resp and same_bare and same_local:
                            cell[0] += 1
                            per_fixture_pass[fname] = per_fixture_pass.get(fname, 0) + 1
                            print("  PASS %-2d %-11s %-46s code=%-3s calls=%s cmds=%s%s"
                                  % (i, ep, short(st["body"], 46), a["code"],
                                     a["calls"], a["cmds"],
                                     "  [refs 有变化]" if changed else ""))
                        else:
                            cell[1] += 1
                            fails_total += 1
                            print("  FAIL %-2d %-11s %s" % (i, ep, short(st["body"])))
                            if not same_resp:
                                print("       python: code=%s calls=%s cmds=%s json=%s"
                                      % (a["code"], a["calls"], a["cmds"], short(ja)))
                                print("       c     : code=%s calls=%s cmds=%s json=%s"
                                      % (b["code"], b["calls"], b["cmds"], short(jb)))
                            if not same_bare:
                                state_diffs.append((fname, label, i, "bare",
                                                    py_bare, c_bare))
                                print("       bare python=%s" % short(py_bare, 300))
                                print("       bare c     =%s" % short(c_bare, 300))
                            if not same_local:
                                keys = sorted(set(py_local) | set(c_local))
                                bad = [k for k in keys
                                       if py_local.get(k) != c_local.get(k)]
                                state_diffs.append((fname, label, i, "local", bad,
                                                    {k: py_local.get(k) for k in bad},
                                                    {k: c_local.get(k) for k in bad}))
                                print("       local 不一致字段: %s" % bad)
                                for k in bad:
                                    print("         python %s=%s" % (k, short(py_local.get(k), 300)))
                                    print("         c      %s=%s" % (k, short(c_local.get(k), 300)))
                        if (a["calls"], a["cmds"]) != (b["calls"], b["cmds"]):
                            call_diffs.append((fname, label, st["path"],
                                               a["calls"], a["cmds"],
                                               b["calls"], b["cmds"]))
                        for side, r in (("python", a), ("c", b)):
                            if isinstance(r["code"], int) and r["code"] >= 500:
                                print("       !! %s 出现 %d（禁止）" % (side, r["code"]))

                        # ---- 推进：本步结束状态以 Python 侧为准
                        rmtree(state)
                        os.rename(nxt, state)
                finally:
                    stop(pp)
                    stop(pc)
    finally:
        pass

    # ---------------------------------------------------------------- 汇总
    print("\n=== PASS/FAIL 矩阵（端点×闸 × fixture）")
    header = "  %-12s %-9s" % ("endpoint", "net")
    for fname, _, _, _ in todo:
        header += " %-10s" % fname
    header += " 合计"
    print(header)
    eps = ["fetch", "pull", "push"]
    env_order = ["unset", "'off'", "'read'", "'write'", "'WrItE'", "'Raed'"]
    for ep in eps:
        for ename in env_order:
            row = "  %-12s %-9s" % (ep, ename)
            tp = tf = 0
            for fname, _, _, _ in todo:
                p_, f_ = matrix.get((ep, ename), {}).get(fname, [0, 0])
                tp += p_
                tf += f_
                row += " %-10s" % ("%dP/%dF" % (p_, f_) if (p_ + f_) else "-")
            if tp + tf == 0:
                continue
            row += " %d/%d" % (tp, tp + tf)
            print(row)

    print("\n  200 响应数（证明不是全 400 通过）:")
    for fname, _, _, _ in todo:
        n = ok200.get(fname, 0)
        print("    %-10s %d%s" % (fname, n, "" if n else "   <-- 无阳性结果！"))
    print("  真实副作用步数（refs/HEAD/MERGE_HEAD/工作区 有变化）:")
    for fname, _, _, _ in todo:
        print("    %-10s %d" % (fname, effects.get(fname, 0)))

    print("\n  响应形状分布（取自 Python 侧）:")
    for ep in eps:
        items = shapes.get(ep, {})
        if not items:
            continue
        parts = ["%s/%s×%d" % (c, k, n)
                 for (c, k), n in sorted(items.items(), key=lambda x: str(x[0]))]
        print("    %-8s %s" % (ep, "  ".join(parts)))

    if call_diffs:
        print("\n  git 调用计数差异（只报告，不算失败）: %d 条" % len(call_diffs))
        for d in call_diffs[:12]:
            print("    %s/%s %s  python=(%s,%s) c=(%s,%s)" % d)
        if len(call_diffs) > 12:
            print("    ... 其余 %d 条同类" % (len(call_diffs) - 12))
    else:
        print("\n  git 调用计数：两侧完全一致")

    if state_diffs:
        print("\n  状态副作用差异: %d 条" % len(state_diffs))
        for d in state_diffs[:10]:
            print("    %s" % (d[:4],))

    no_pos = [f for f, _, _, _ in todo if ok200.get(f, 0) == 0]
    if no_pos:
        print("\n  FAIL: 以下 fixture 没有阳性结果: %s" % no_pos)

    print("\n  失败 %d / %d" % (fails_total, cases_total))
    rmtree(BASE)
    return 1 if (fails_total or no_pos) else 0


if __name__ == "__main__":
    sys.exit(main())
