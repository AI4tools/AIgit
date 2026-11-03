# -*- coding: utf-8 -*-
"""M1 历史清单读（history/{tags,reflog,stashes}）一致性判据。

判据：同一个仓、同一时刻、同一份请求，Python 服务与 C 服务的
**HTTP 码 + JSON 深比较**相等。

⚠ 不比 X-aigit-git-calls / X-aigit-git-cmds：C 版这里一律走 git 子进程，
Python 版有「直接读 refs / reflog 文件」的快路径，spawn 数天然不同。
比的是**输出口径**，不是省了几个 spawn。

用法：
    python -X utf8 c/tests/conformance_histlist.py
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)                       # ...\c_agent_histlist\c
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
PY_ROOT = os.environ.get("AIGIT_PY_ROOT") or r"D:\project\aigit"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_histlist_%d" % (os.getpid() % 100000))

TAGS = "/api/history/tags"
REFLOG = "/api/history/reflog"
STASHES = "/api/history/stashes"


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


def new_repo(name, commits=2):
    d = os.path.join(BASE, name)
    os.makedirs(d)
    must(d, "init", "-q", "-b", "main")
    must(d, "config", "user.name", "bench")
    must(d, "config", "user.email", "bench@localhost")
    for i in range(commits):
        write(os.path.join(d, "a.txt"), "line %d\n" % i)
        must(d, "add", "-A")
        must(d, "-c", "user.name=b", "-c", "user.email=b@b",
             "commit", "-q", "-m", "c%d" % (i + 1))
    return d


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
            for k in ("tags", "reflog", "stashes"):
                if k in b["json"]:
                    note = " n=%d" % len(b["json"][k] or [])
        print("  %s %-22s %-22s code=%s%s"
              % (tag, fixture, path, a["code"], note))


def negative_control():
    """阴性对照：两个服务指向**不同的仓**，判据必须报 FAIL。

    没有这一步，一堆 PASS 可能只是「两边都返回了空数组」的假象 ——
    判据本身要先证明它抓得住差异。
    """
    ra = new_repo("neg_a", 1)
    must(ra, "tag", "only-in-a")
    rb = new_repo("neg_b", 2)
    must(rb, "tag", "only-in-b1")
    must(rb, "tag", "only-in-b2")
    n0 = len(RESULTS)
    with Pair(ra, rb) as p:
        p.check("NEGCTL(应当 FAIL)", TAGS)
    caught = any(not r[2] for r in RESULTS[n0:])
    del RESULTS[n0:]
    print("  %s 阴性对照：不同仓（1 个 vs 2 个标签）必须被判据抓住"
          % ("PASS" if caught else "FAIL"))
    return 0 if caught else 1


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

    # ── fixture 1：空仓（无提交）─────────────────────────────────────
    empty = os.path.join(BASE, "empty")
    os.makedirs(empty)
    must(empty, "init", "-q", "-b", "main")
    with Pair(empty) as p:
        p.check("empty-repo", TAGS)
        p.check("empty-repo", REFLOG)
        p.check("empty-repo", STASHES)
        p.check("empty-repo", TAGS + "?limit=abc")
        p.check("empty-repo", STASHES + "?limit=1")

    # ── fixture 2：主仓，逐步长出标签 / stash / reflog ───────────────
    main_repo = new_repo("main")
    with Pair(main_repo) as p:
        # 无标签
        p.check("no-tags", TAGS)
        # 单标签（轻量）
        must(main_repo, "tag", "v1")
        p.check("one-tag-light", TAGS)
        # 注解多行标签
        must(main_repo, "tag", "-a", "v2", "-m", "line one\nline two\n\nbody here")
        p.check("annotated-multiline", TAGS)
        # 中文标签名 + 同一秒内批量建（排序 tie）
        must(main_repo, "tag", "-a", "中文标签", "-m", "中文 注解 标签")
        for t in ("v3", "v10", "zz"):
            must(main_repo, "tag", t)
        p.check("many-tags-sort", TAGS)
        p.check("many-tags-sort", TAGS + "?limit=1")
        p.check("many-tags-sort", TAGS + "?limit=0")      # 夹到 1
        p.check("many-tags-sort", TAGS + "?limit=-5")     # 夹到 1
        p.check("many-tags-sort", TAGS + "?limit=99999")  # 夹到 500
        p.check("many-tags-sort", TAGS + "?limit=")       # 空值 = 缺省
        p.check("many-tags-sort", TAGS + "?limit=%203")   # int(" 3")
        p.check("many-tags-sort", TAGS + "?limit=1_0")    # int("1_0") == 10
        p.check("many-tags-sort", TAGS + "?limit=abc")    # 400
        p.check("many-tags-sort", TAGS + "?limit=1e3")    # 400

        # 无 stash
        p.check("no-stash", STASHES)
        # 1 个 stash
        write(os.path.join(main_repo, "a.txt"), "stash one\n")
        must(main_repo, "stash", "push", "-m", "s one")
        p.check("one-stash", STASHES)
        # 2 个 stash，其中第二个是多行 message
        write(os.path.join(main_repo, "a.txt"), "stash two\n")
        must(main_repo, "stash", "push", "-m", "s two\nsecond line")
        p.check("two-stashes-multiline", STASHES)
        p.check("two-stashes-multiline", STASHES + "?limit=1")
        p.check("two-stashes-multiline", STASHES + "?limit=0")
        p.check("two-stashes-multiline", STASHES + "?limit=99999")  # 夹到 200
        p.check("two-stashes-multiline", STASHES + "?limit=abc")

        # reflog 正常（这时已经攒了 commit / stash / tag 一堆条目）
        p.check("reflog-normal", REFLOG)
        p.check("reflog-normal", REFLOG + "?limit=1")
        p.check("reflog-normal", REFLOG + "?limit=3")
        p.check("reflog-normal", REFLOG + "?limit=0")
        p.check("reflog-normal", REFLOG + "?limit=99999")
        p.check("reflog-normal", REFLOG + "?limit=abc")

    # ── fixture 3：游离 HEAD ────────────────────────────────────────
    det = new_repo("detached", commits=3)
    must(det, "tag", "d1")
    must(det, "checkout", "-q", "--detach", "HEAD~1")
    with Pair(det) as p:
        p.check("detached-head", REFLOG)
        p.check("detached-head", TAGS)
        p.check("detached-head", STASHES)

    # ── fixture 4：core.abbrev=12（验 short 不是写死的 7 位）──────────
    ab = new_repo("abbrev", commits=2)
    must(ab, "config", "core.abbrev", "12")
    with Pair(ab) as p:
        p.check("core-abbrev-12", REFLOG)

    # ── 阴性对照 ────────────────────────────────────────────────────
    neg_fail = negative_control()

    # ── 汇总 ────────────────────────────────────────────────────────
    fails = [r for r in RESULTS if not r[2]]
    print()
    print("  共 %d 项，失败 %d 项" % (len(RESULTS), len(fails)))
    for fixture, path, ok, a, b in fails:
        print("  FAIL %s %s" % (fixture, path))
        print("       python: code=%s json=%s"
              % (a["code"], json.dumps(a["json"], ensure_ascii=False)[:600]))
        print("       c     : code=%s json=%s"
              % (b["code"], json.dumps(b["json"], ensure_ascii=False)[:600]))
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    return 1 if (fails or neg_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
