# -*- coding: utf-8 -*-
"""M1 历史计划面（history/stash、history/plan/*）一致性判据。

判据：同一个仓、同一时刻、同一份请求，Python 服务与 C 服务的
**HTTP 码 + JSON 深比较**相等。

⚠ 不比 X-aigit-git-calls / X-aigit-git-cmds：C 版这里一律走 git 子进程，
Python 版有「直接读 refs / reflog 文件」的快路径，spawn 数天然不同。
比的是**输出口径**，不是省了几个 spawn。

用法：
    python -X utf8 c/tests/conformance_histplan.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
Python 参考版位置默认 D:\\project\\aigit，可用 AIGIT_PY_ROOT 覆盖。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)                       # ...\c_agent_histplan\c
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
PY_ROOT = os.environ.get("AIGIT_PY_ROOT") or r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_histplan_%d" % (os.getpid() % 100000))

STASH = "/api/history/stash"
PLAN = "/api/history/plan/"


# ---------------------------------------------------------------- git 小工具

def sh(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace")


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_crlf(path, text):
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def new_repo(name, commits=2):
    d = os.path.join(BASE, name)
    os.makedirs(d)
    must(d, "init", "-q", "-b", "main")
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    must(d, "config", "core.autocrlf", "false")
    for i in range(commits):
        write(os.path.join(d, "a.txt"), "line %d\n" % i)
        must(d, "add", "-A")
        must(d, "-c", "user.name=b", "-c", "user.email=b@b",
             "commit", "-q", "-m", "c%d" % (i + 1))
    return d


def commit(repo, msg, path="a.txt", text=None):
    write(os.path.join(repo, path), text if text is not None else msg + "\n")
    must(repo, "add", "-A")
    must(repo, "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- 服务

def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_py(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("AIGIT_GIT_COUNT", None)
    return subprocess.Popen([sys.executable, "-X", "utf8",
                             os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("AIGIT_GIT_COUNT", None)
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_health(port, proc, timeout=30.0):
    base = "http://127.0.0.1:%d/api/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(base, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.1)
    return False


def req(port, path):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="GET")
    try:
        r = urllib.request.urlopen(rq, timeout=60)
        code, payload = r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    try:
        js = json.loads(payload.decode("utf-8"))
    except Exception:
        js = None
    return {"code": code, "json": js, "raw": payload}


# ---------------------------------------------------------------- 判据

RESULTS = []          # (fixture, path, ok, a, b)


class Pair:
    """一个仓上的一对服务（默认两边同一个仓）。"""

    def __init__(self, repo, repo_c=None):
        self.repo = repo
        self.pa, self.pb = free_port(), free_port()
        self.pp = start_py(repo, self.pa)
        self.pc = start_c(repo_c or repo, self.pb)
        self.ok = False

    def __enter__(self):
        self.ok = wait_health(self.pa, self.pp) and wait_health(self.pb, self.pc)
        if not self.ok:
            print("  FAIL 服务没起来：%s" % self.repo)
        return self

    def __exit__(self, *exc):
        for p in (self.pp, self.pc):
            try:
                p.terminate()
                p.wait(10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        return False

    def check(self, fixture, path):
        if not self.ok:
            RESULTS.append((fixture, path, False, {"code": "no-server"},
                            {"code": "no-server"}))
            return
        a = req(self.pa, path)
        b = req(self.pb, path)
        ok = (a["code"] == b["code"] and a["json"] == b["json"])
        RESULTS.append((fixture, path, ok, a, b))
        tag = "PASS" if ok else "FAIL"
        note = ""
        if ok and isinstance(b["json"], dict):
            if "text" in b["json"]:
                note = " text=%d chars" % len(b["json"]["text"] or "")
            elif "op" in b["json"]:
                note = " op=%s safe=%s" % (b["json"].get("op"),
                                           b["json"].get("safe"))
            elif "code" in b["json"]:
                note = " code=%s" % b["json"].get("code")
        print("  %s %-24s %-52s code=%s%s"
              % (tag, fixture, path, a["code"], note))


def negative_control():
    """阴性对照：两个服务指向**不同的仓**，判据必须报 FAIL。"""
    ra = new_repo("neg_a", 1)
    rb = new_repo("neg_b", 2)
    write(os.path.join(rb, "a.txt"), "dirty\n")
    n0 = len(RESULTS)
    with Pair(ra, rb) as p:
        p.check("NEGCTL(应当 FAIL)", PLAN + "stash")
        p.check("NEGCTL(应当 FAIL)", STASH + "?index=0")
    caught = any(not r[2] for r in RESULTS[n0:])
    del RESULTS[n0:]
    print("  %s 阴性对照：不同仓（HEAD/脏状态不同）必须被判据抓住"
          % ("PASS" if caught else "FAIL"))
    return 0 if caught else 1


# ---------------------------------------------------------------- fixtures

def fx_empty():
    """未出生 HEAD（没有提交）—— 每个端点都应有确定答案，不许 500。"""
    d = os.path.join(BASE, "empty")
    os.makedirs(d)
    must(d, "init", "-q", "-b", "main")
    with Pair(d) as p:
        p.check("unborn-head", STASH + "?index=0")
        p.check("unborn-head", STASH + "?index=3")
        p.check("unborn-head", STASH + "?index=-1")
        p.check("unborn-head", STASH + "?index=abc")
        p.check("unborn-head", PLAN + "stash")
        p.check("unborn-head", PLAN + "stash-drop")
        p.check("unborn-head", PLAN + "amend")
        p.check("unborn-head", PLAN + "undo-last-commit")
        p.check("unborn-head", PLAN + "reflog-restore")
        p.check("unborn-head", PLAN + "revert")
        p.check("unborn-head", PLAN + "branch-rename?old=main&new=x")
        p.check("unborn-head", PLAN + "branch-delete?name=main")
        p.check("unborn-head", PLAN + "tag-delete?name=v1")
        p.check("unborn-head", PLAN + "nope")
        p.check("unborn-head", PLAN)
        p.check("unborn-head", "/api/history/plan")
        p.check("unborn-head", "/api/history/stash/extra")


def fx_stash():
    """stash 主场景：1 个 / 多个 / 多行 message / stat / 手改过的 stash / 中文空格路径。"""
    d = new_repo("stashy", 2)
    must(d, "config", "core.autocrlf", "false")
    # 一个 CRLF 文件，进 stash 后 diff 里带 \r —— 验 universal newlines
    write_crlf(os.path.join(d, "crlf.txt"), "one\r\ntwo\r\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-q", "-m", "crlf")

    with Pair(d) as p:
        p.check("no-stash", STASH + "?index=0")
        p.check("no-stash", STASH + "?index=1")
        p.check("no-stash", STASH + "?index=-1")
        p.check("no-stash", STASH + "?index=abc")
        p.check("no-stash", PLAN + "stash")
        p.check("no-stash", PLAN + "stash-drop?index=0")

        # 1 个 stash（含中文 + 空格路径 + CRLF 改动）
        write(os.path.join(d, "中文 文件.txt"), "内容 一\n")
        write(os.path.join(d, "crlf.txt"), "one\r\nTWO\r\n")
        must(d, "add", "-A")
        write(os.path.join(d, "中文 文件.txt"), "内容 二\n")
        must(d, "stash", "push", "-m", "第一个 stash")
        p.check("one-stash", STASH + "?index=0")
        p.check("one-stash", STASH + "?index=0&stat=1")
        p.check("one-stash", STASH + "?index=0&stat=true")
        p.check("one-stash", STASH + "?index=0&stat=on")
        p.check("one-stash", STASH + "?index=0&stat=0")
        p.check("one-stash", STASH + "?index=0&stat=")
        p.check("one-stash", STASH + "?index=1")
        p.check("one-stash", STASH + "?index=%2B0")     # int("+0")
        p.check("one-stash", STASH + "?index=%200")     # int(" 0")
        p.check("one-stash", STASH + "?index=")         # 空值 = 缺省
        p.check("one-stash", STASH + "?index=1_0")      # int("1_0")=10 -> 越界
        p.check("one-stash", PLAN + "stash-drop?index=0")
        p.check("one-stash", PLAN + "stash-drop?index=1")
        p.check("one-stash", PLAN + "stash-drop?index=-2")
        p.check("one-stash", PLAN + "stash-drop?index=zz")
        p.check("one-stash", PLAN + "reflog-restore?index=0")
        p.check("one-stash", PLAN + "reflog-restore?index=1&mode=hard")
        p.check("one-stash", PLAN + "reflog-restore?index=99999")
        p.check("one-stash", PLAN + "reflog-restore?index=0&mode=nope")
        p.check("one-stash", PLAN + "reflog-restore?index=abc")
        p.check("one-stash", PLAN + "reflog-restore?index=0&mode=")
        p.check("one-stash", PLAN + "undo-last-commit")
        p.check("one-stash", PLAN + "undo-last-commit?drop_changes=1")
        p.check("one-stash", PLAN + "amend")
        p.check("one-stash", PLAN + "amend?message=")
        p.check("one-stash", PLAN + "amend?message=hello")
        p.check("one-stash", PLAN + "revert")
        p.check("one-stash", PLAN + "revert?sha=HEAD")
        p.check("one-stash", PLAN + "revert?sha=HEAD~1")
        p.check("one-stash", PLAN + "revert?sha=HEAD~1&mainline=")
        p.check("one-stash", PLAN + "revert?sha=HEAD%5E%7Bcommit%7D")
        p.check("one-stash", PLAN + "revert?sha=@")
        p.check("one-stash", PLAN + "revert?sha=deadbeef")
        p.check("one-stash", PLAN + "revert?sha=HEAD&mainline=1")
        p.check("one-stash", PLAN + "revert?sha=--format=x")

        # 2 个 stash，第二个 message 多行
        write(os.path.join(d, "a.txt"), "stash two\n")
        must(d, "stash", "push", "-m", "s two\nsecond line")
        p.check("two-stashes-multiline", STASH + "?index=0")
        p.check("two-stashes-multiline", STASH + "?index=1")
        p.check("two-stashes-multiline", STASH + "?index=2")
        p.check("two-stashes-multiline", STASH + "?index=0&stat=1")
        p.check("two-stashes-multiline", STASH + "?index=1&stat=1")
        p.check("two-stashes-multiline", PLAN + "stash-drop?index=1")

        # 手改过的 stash：直接改 stash@{0} 的树，内容与当前索引不同
        must(d, "stash", "apply", "--index", "stash@{1}")
        write(os.path.join(d, "a.txt"), "index 里的样子\n")
        must(d, "add", "-A")
        # 现在工作区/索引与 stash 内容不同
        p.check("touched-stash", STASH + "?index=0")
        p.check("touched-stash", STASH + "?index=1")
        p.check("touched-stash", STASH + "?index=0&stat=1")
        p.check("touched-stash", PLAN + "stash")
        p.check("touched-stash", PLAN + "stash?include_untracked=1")
        p.check("touched-stash", PLAN + "amend")
        p.check("touched-stash", PLAN + "undo-last-commit")

        # 脏工作区：staged + unstaged + untracked 都有
        write(os.path.join(d, "a.txt"), "unstaged\n")
        write(os.path.join(d, "b.txt"), "staged\n")
        must(d, "add", "b.txt")
        write(os.path.join(d, "新 文件.txt"), "untracked\n")
        p.check("dirty-mixed", PLAN + "stash")
        p.check("dirty-mixed", PLAN + "stash?include_untracked=1")
        p.check("dirty-mixed", PLAN + "stash?include_untracked=yes")
        p.check("dirty-mixed", PLAN + "amend")
        p.check("dirty-mixed", PLAN + "undo-last-commit")
        p.check("dirty-mixed", PLAN + "undo-last-commit?drop_changes=true")
        p.check("dirty-mixed", STASH + "?index=0")

        # 带未跟踪文件的 stash（-u）：diff 里会多出「新增文件」那一段
        must(d, "stash", "push", "-u", "-m", "带未跟踪")
        p.check("stash-with-untracked", STASH + "?index=0")
        p.check("stash-with-untracked", STASH + "?index=0&stat=1")
        p.check("stash-with-untracked", STASH + "?index=1")
        p.check("stash-with-untracked", PLAN + "stash-drop?index=0")
        p.check("stash-with-untracked", PLAN + "stash")


def fx_rich():
    """分支 / 标签 / 远端跟踪 / merge 提交 —— 覆盖 plan 的分支与 revert 分支。"""
    d = new_repo("rich", 3)
    # feature：2 个未合并提交
    must(d, "checkout", "-q", "-b", "feature")
    commit(d, "feat 1")
    commit(d, "feat 2")
    must(d, "checkout", "-q", "main")
    # merged：指向 main~1，0 个未合并
    must(d, "branch", "merged", "main~1")
    # 标签
    must(d, "tag", "v1")
    must(d, "tag", "-a", "中文标签", "-m", "注解 标签\n第二行")
    # 本地远端（纯文件路径，不联网）
    bare = os.path.join(BASE, "rich_remote.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True,
                   capture_output=True)
    must(d, "remote", "add", "origin", bare)
    must(d, "push", "-q", "origin", "main")
    must(d, "fetch", "-q", "origin")
    must(d, "remote", "set-head", "origin", "main")
    # merge 提交
    must(d, "checkout", "-q", "-b", "side")
    commit(d, "side 1", "side.txt", "side\n")
    must(d, "checkout", "-q", "main")
    commit(d, "main 4")
    must(d, "-c", "user.name=b", "-c", "user.email=b@b",
         "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge_sha = must(d, "rev-parse", "HEAD").strip()

    with Pair(d) as p:
        p.check("rich", PLAN + "amend")
        p.check("rich", PLAN + "undo-last-commit")
        p.check("rich", PLAN + "undo-last-commit?drop_changes=1")
        p.check("rich", PLAN + "branch-delete?name=feature")
        p.check("rich", PLAN + "branch-delete?name=feature&force=1")
        p.check("rich", PLAN + "branch-delete?name=merged")
        p.check("rich", PLAN + "branch-delete?name=main")
        p.check("rich", PLAN + "branch-delete?name=nope")
        p.check("rich", PLAN + "branch-delete?name=bad..name")
        p.check("rich", PLAN + "branch-delete?name=-rf")
        p.check("rich", PLAN + "branch-delete?name=")
        p.check("rich", PLAN + "branch-delete?name")
        p.check("rich", PLAN + "branch-delete?name=.hidden")
        p.check("rich", PLAN + "branch-delete?name=x.lock")
        p.check("rich", PLAN + "branch-delete?name=a//b")
        p.check("rich", PLAN + "branch-delete?name=a@{1}")
        p.check("rich", PLAN + "branch-delete?name=@")
        p.check("rich", PLAN + "branch-delete?name=%E4%B8%AD%E6%96%87")
        p.check("rich", PLAN + "branch-rename?old=feature&new=feature2")
        p.check("rich", PLAN + "branch-rename?old=feature&new=main")
        p.check("rich", PLAN + "branch-rename?old=feature&new=feature")
        p.check("rich", PLAN + "branch-rename?old=nope&new=x")
        p.check("rich", PLAN + "branch-rename?old=main&new=%E4%B8%AD%E6%96%87%20%E5%88%86%E6%94%AF")
        p.check("rich", PLAN + "branch-rename")
        p.check("rich", PLAN + "branch-rename?old=main")
        p.check("rich", PLAN + "branch-rename?old=&new=x")
        p.check("rich", PLAN + "branch-rename?old&new=x")
        p.check("rich", PLAN + "tag-delete?name=v1")
        p.check("rich", PLAN + "tag-delete?name=" + urllib.parse.quote("中文标签", safe=""))
        p.check("rich", PLAN + "tag-delete?name=nope")
        p.check("rich", PLAN + "tag-delete")
        p.check("rich", PLAN + "revert?sha=" + merge_sha)
        p.check("rich", PLAN + "revert?sha=" + merge_sha + "&mainline=1")
        p.check("rich", PLAN + "revert?sha=" + merge_sha + "&mainline=2")
        p.check("rich", PLAN + "revert?sha=" + merge_sha + "&mainline=9")
        p.check("rich", PLAN + "revert?sha=" + merge_sha + "&mainline=x")
        p.check("rich", PLAN + "revert?sha=HEAD~1&mainline=1")
        p.check("rich", PLAN + "revert?sha=HEAD~1")
        p.check("rich", PLAN + "reflog-restore?index=2&mode=mixed")
        p.check("rich", PLAN + "reflog-restore?index=0&mode=merge")
        p.check("rich", STASH + "?index=0")


def fx_detached():
    """游离 HEAD：branch-delete 的 current 判定改比 sha；undo/amend 仍可用。"""
    d = new_repo("detached", 3)
    must(d, "checkout", "-q", "--detach", "HEAD~1")
    write(os.path.join(d, "a.txt"), "detached dirty\n")
    must(d, "stash", "push", "-m", "detached stash")
    with Pair(d) as p:
        p.check("detached-head", STASH + "?index=0")
        p.check("detached-head", STASH + "?index=0&stat=1")
        p.check("detached-head", PLAN + "undo-last-commit")
        p.check("detached-head", PLAN + "amend")
        p.check("detached-head", PLAN + "stash")
        p.check("detached-head", PLAN + "stash-drop?index=0")
        p.check("detached-head", PLAN + "branch-delete?name=main")
        p.check("detached-head", PLAN + "branch-rename?old=main&new=main2")
        p.check("detached-head", PLAN + "reflog-restore?index=0")


def fx_conflict():
    """冲突未解决：stash 的冲突提示、revert 的 blocked、undo/amend 的 conflicted 分支。"""
    d = new_repo("conflicted", 2)
    must(d, "checkout", "-q", "-b", "left")
    commit(d, "left 改", "a.txt", "left\n")
    must(d, "checkout", "-q", "main")
    commit(d, "main 改", "a.txt", "main\n")
    sh(d, "-c", "user.name=b", "-c", "user.email=b@b",
       "merge", "left")          # 必定冲突，rc != 0
    with Pair(d) as p:
        p.check("conflict", PLAN + "stash")
        p.check("conflict", PLAN + "stash?include_untracked=1")
        p.check("conflict", PLAN + "undo-last-commit")
        p.check("conflict", PLAN + "undo-last-commit?drop_changes=1")
        p.check("conflict", PLAN + "amend")
        p.check("conflict", PLAN + "revert?sha=HEAD")
        p.check("conflict", PLAN + "revert?sha=HEAD~1")
        p.check("conflict", STASH + "?index=0")


def fx_no_untracked_note():
    """有改动但没有未跟踪文件：include_untracked 的提示分支。"""
    d = new_repo("note", 1)
    write(os.path.join(d, "a.txt"), "dirty\n")
    with Pair(d) as p:
        p.check("no-untracked", PLAN + "stash")
        p.check("no-untracked", PLAN + "stash?include_untracked=1")
        p.check("no-untracked", PLAN + "amend")
        p.check("no-untracked", PLAN + "undo-last-commit")


def fx_trunk():
    """没有 main/master 的仓（default_branch -> None）+ 中文/带斜杠分支名。"""
    d = os.path.join(BASE, "trunk")
    os.makedirs(d)
    must(d, "init", "-q", "-b", "trunk")
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    must(d, "config", "core.autocrlf", "false")
    write(os.path.join(d, "a.txt"), "one\n")
    must(d, "add", "-A")
    must(d, "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-q", "-m", "t1")
    must(d, "checkout", "-q", "-b", "side/deep")
    commit(d, "side 1")
    must(d, "checkout", "-q", "trunk")
    must(d, "branch", "中文分支")
    with Pair(d) as p:
        p.check("trunk-default", PLAN + "branch-delete?name=side%2Fdeep")
        p.check("trunk-default", PLAN + "branch-delete?name=side%2Fdeep&force=on")
        p.check("trunk-default", PLAN + "branch-delete?name=" + urllib.parse.quote("中文分支", safe=""))
        p.check("trunk-default", PLAN + "branch-delete?name=trunk")
        p.check("trunk-default", PLAN + "branch-rename?old=side%2Fdeep&new=x%2Fy")
        p.check("trunk-default", PLAN + "branch-rename?old=trunk&new=" + urllib.parse.quote("中文分支", safe=""))
        p.check("trunk-default", PLAN + "branch-delete?name=" + "a" * 101)
        p.check("trunk-default", PLAN + "branch-delete?name=" + urllib.parse.quote("中" * 60, safe=""))
        p.check("trunk-default", PLAN + "branch-delete?name=" + urllib.parse.quote("中" * 101, safe=""))
        p.check("trunk-default", PLAN + "branch-delete?name=a%00b")
        p.check("trunk-default", PLAN + "branch-delete?name=a%09b")
        p.check("trunk-default", PLAN + "branch-delete?name=%20%20")
        p.check("trunk-default", PLAN + "amend")


def fx_abbrev():
    """core.abbrev=12：reflog-restore 的 short 宽度要跟 git / Python 快路径一致。"""
    d = new_repo("abbrev", 2)
    must(d, "config", "core.abbrev", "12")
    with Pair(d) as p:
        p.check("core-abbrev-12", PLAN + "reflog-restore?index=0")
        p.check("core-abbrev-12", PLAN + "undo-last-commit")
        p.check("core-abbrev-12", PLAN + "amend")


def snapshot(repo):
    """只读性快照：HEAD / stash / 工作区 / 索引内容 / reflog / 全部 ref / 文件内容。"""
    def raw(*a):
        p = subprocess.run(["git"] + list(a), cwd=repo, capture_output=True)
        return p.returncode, p.stdout
    snap = {}
    snap["head"] = raw("rev-parse", "HEAD")
    snap["stash"] = raw("stash", "list")
    snap["status"] = raw("status", "--porcelain=v1", "-z")
    snap["index"] = raw("ls-files", "-s")
    snap["reflog"] = raw("reflog", "--format=%H")
    snap["refs"] = raw("for-each-ref", "--format=%(refname) %(objectname)")
    with open(os.path.join(repo, "a.txt"), "rb") as f:
        snap["a.txt"] = f.read()
    return snap


def fx_readonly():
    """只读性：一批 GET 前后仓状态必须逐项不变（HEAD/index/stash/reflog/refs/文件）。"""
    d = new_repo("readonly", 2)
    write(os.path.join(d, "b.txt"), "untracked\n")
    write(os.path.join(d, "a.txt"), "stashed change\n")
    must(d, "stash", "push", "-m", "ro stash")
    write(os.path.join(d, "a.txt"), "dirty now\n")
    must(d, "add", "a.txt")
    write(os.path.join(d, "a.txt"), "staged + unstaged\n")
    must(d, "branch", "other", "main")
    must(d, "tag", "v1")

    paths = [
        STASH + "?index=0", STASH + "?index=0&stat=1", STASH + "?index=9",
        PLAN + "stash", PLAN + "stash?include_untracked=1",
        PLAN + "stash-drop?index=0", PLAN + "amend",
        PLAN + "undo-last-commit", PLAN + "undo-last-commit?drop_changes=1",
        PLAN + "reflog-restore?index=0", PLAN + "reflog-restore?index=1&mode=hard",
        PLAN + "revert?sha=HEAD", PLAN + "branch-rename?old=main&new=z",
        PLAN + "branch-delete?name=other", PLAN + "branch-delete?name=main",
        PLAN + "tag-delete?name=v1", PLAN + "nope",
    ]
    with Pair(d) as p:
        before = snapshot(d)
        for path in paths:
            p.check("read-only", path)
        after = snapshot(d)
        ok = before == after
        RESULTS.append(("read-only", "状态快照（%d 个 GET 前后）" % len(paths), ok,
                        before, after))
        print("  %s %-24s %-52s %s"
              % ("PASS" if ok else "FAIL", "read-only",
                 "快照前后相等（%d 个 GET）" % len(paths),
                 "" if ok else "差异：" + ", ".join(
                     k for k in before if before[k] != after.get(k))))


def fx_no_repo():
    """不是 git 仓库：两边都在服务起来之后把 .git 删掉。

    ⚠ 不能在启动前就给一个空目录 —— Python 版 main() 发现不是仓会**自动
    git init 并提交一个 init: sandbox**（server.py:2609），那个目录就变成仓了。
    所以这里先建真仓、起服务、再删 .git，两边看到的是同一时刻的「没有 .git」。
    """
    d = new_repo("plain", 1)
    with Pair(d) as p:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", os.path.join(d, ".git")],
                       capture_output=True)
        p.check("not-a-repo", STASH + "?index=0")
        p.check("not-a-repo", PLAN + "stash")
        p.check("not-a-repo", PLAN + "amend")
        p.check("not-a-repo", PLAN + "undo-last-commit")
        p.check("not-a-repo", PLAN + "branch-delete?name=main")
        p.check("not-a-repo", PLAN + "tag-delete?name=v1")


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    if not os.path.isfile(os.path.join(PY_ROOT, "server.py")):
        print("  缺 Python 参考版：%s" % PY_ROOT)
        return 1
    os.makedirs(BASE)

    fx_empty()
    fx_stash()
    fx_rich()
    fx_detached()
    fx_conflict()
    fx_no_untracked_note()
    fx_trunk()
    fx_abbrev()
    fx_readonly()
    fx_no_repo()
    neg_fail = negative_control()

    fails = [r for r in RESULTS if not r[2]]
    print()
    print("  共 %d 项，失败 %d 项" % (len(RESULTS), len(fails)))
    for fixture, path, ok, a, b in fails:
        print("  FAIL %s %s" % (fixture, path))
        print("       python: code=%s json=%s"
              % (a["code"], json.dumps(a["json"], ensure_ascii=False)[:900]))
        print("       c     : code=%s json=%s"
              % (b["code"], json.dumps(b["json"], ensure_ascii=False)[:900]))
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    return 1 if (fails or neg_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
