# -*- coding: utf-8 -*-
"""M1 计划面一致性判据：C 版 vs Python 版（同一仓、同一时刻、同一份请求）。

覆盖 2 个**只读预演**端点 × 6 个 fixture：
    /api/reset/plan?mode=soft|mixed|hard[&ref=...]
    /api/discard/plan?files=a&files=b
    clean（单提交 + 分支）
    dirty（暂存 + 未暂存 + 未跟踪 + 未跟踪目录 + 删除 + 改名 + 中文空格 + .gitignore）
    unborn（未出生 HEAD）
    detached（游离 HEAD，HEAD~1 落后两个提交）
    stash（有 stash）
    refs（多提交 + 中文分支 + 各种非法 ref）

判据（逐条全等，不看「像不像」）：
    * HTTP 状态码
    * JSON 深比较（键名/类型/null/数组顺序）
spawn 次数/子命令序列**不比**（这两条端点允许实现不同：Python 会走 cat-file 常驻，
C 版直接 spawn）。

用法：
    python -X utf8 c/tests/conformance_plan.py
前置：c/build/aigit.exe 已构建；本机有 python 与 git。
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
C_ROOT = os.path.dirname(HERE)
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
_PY_SIBLING = os.path.join(os.path.dirname(C_ROOT), "python-ref", "server.py")
PY_ROOT = os.path.dirname(_PY_SIBLING) if os.path.isfile(_PY_SIBLING) else r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_plan_%d" % (os.getpid() % 100000))

Q = urllib.parse.quote


def sh(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def must(repo, *a):
    p = sh(repo, *a)
    if p.returncode != 0:
        raise RuntimeError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                (p.stderr or p.stdout)[:300]))
    return p.stdout


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def new_repo(name):
    d = os.path.join(BASE, name)
    os.makedirs(d)
    sh(d, "init", "-q", "-b", "main")
    sh(d, "config", "user.name", "bench")
    sh(d, "config", "user.email", "bench@localhost")
    return d


def write(path, data, mode="wb"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(data)


# ---------------------------------------------------------------- fixtures

def fx_clean():
    d = new_repo("clean")
    write(os.path.join(d, "a.txt"), b"a\n")
    write(os.path.join(d, "sub", "b.txt"), b"b\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 clean")
    must(d, "branch", "dev")
    # 目录联接：指向 .git 的、指向 root 自己的（mklink /J 不需要管理员；
    # 建不出来时这几条退化成普通「不存在路径」，两侧仍然一致，不会假绿）
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "link"),
                    os.path.join(d, ".git")], capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "selflink"), d],
                   capture_output=True)
    return d


def fx_dirty():
    d = new_repo("dirty")
    write(os.path.join(d, "a.txt"), b"a\nb\n")
    write(os.path.join(d, "sub", "b.txt"), b"b\n")
    write(os.path.join(d, "空 格.txt"), "中文\n".encode("utf-8"))
    write(os.path.join(d, ".gitignore"), b"ignored.txt\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1 中文主题")
    # 暂存后又改（a.txt 同时有 staged 与 unstaged）
    write(os.path.join(d, "a.txt"), b"a2\nb\n")
    must(d, "add", "--", "a.txt")
    write(os.path.join(d, "a.txt"), b"a3\nb\n")
    # 已暂存改名（旧的从工作区消失，索引里换成新的）
    must(d, "mv", "sub/b.txt", "sub/c.txt")
    # 工作区删除（索引/HEAD 里还在）—— 不能 add，否则「已跟踪但缺失」这一档就没了
    os.remove(os.path.join(d, "空 格.txt"))
    # 未跟踪：单文件（含中文+空格）/ 整目录 / 被忽略的（不该进 untracked_total）
    write(os.path.join(d, "u.txt"), b"u\n")
    write(os.path.join(d, "新 文件.txt"), "x\n".encode("utf-8"))
    write(os.path.join(d, "newdir", "inner.txt"), b"i\n")
    write(os.path.join(d, "ignored.txt"), b"nope\n")
    return d


def fx_unborn():
    d = new_repo("unborn")
    write(os.path.join(d, "x.txt"), b"x\n")
    must(d, "add", "-A")                       # 只在索引里，没有提交
    write(os.path.join(d, "untracked.txt"), b"u\n")
    return d


def fx_detached():
    d = new_repo("detached")
    for i in (1, 2, 3):
        write(os.path.join(d, "f.txt"), b"v%d\n" % i)
        must(d, "add", "-A")
        must(d, "commit", "-q", "-m", "c%d" % i)
    must(d, "checkout", "-q", "--detach", "HEAD~2")
    write(os.path.join(d, "f.txt"), b"dirty\n")
    write(os.path.join(d, "new.txt"), b"n\n")
    return d


def fx_stash():
    d = new_repo("stash")
    write(os.path.join(d, "a.txt"), b"a\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c1")
    write(os.path.join(d, "a.txt"), b"a2\n")
    must(d, "stash", "push", "-q", "-m", "s1")
    write(os.path.join(d, "b.txt"), b"b\n")
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", "c2")
    must(d, "stash", "apply", "-q")
    return d


def fx_refs():
    d = new_repo("refs")
    for i in (1, 2, 3, 4):
        write(os.path.join(d, "f.txt"), b"v%d\n" % i)
        must(d, "add", "-A")
        must(d, "commit", "-q", "-m", "c%d 第%d次" % (i, i))
    must(d, "branch", "dev")
    must(d, "branch", "中文分支")
    write(os.path.join(d, "f.txt"), b"dirty\n")
    return d


FIXTURES = [
    ("clean", fx_clean, [
        "/api/reset/plan?mode=soft",
        "/api/reset/plan?mode=mixed",
        "/api/reset/plan?mode=hard",
        "/api/reset/plan",
        "/api/reset/plan?mode=",
        "/api/reset/plan?mode=bogus",
        "/api/reset/plan?mode=SOFT",
        "/api/reset/plan?mode=soft&mode=hard",
        "/api/reset/plan?mode=hard&ref=nope",
        "/api/reset/plan?mode=hard&ref=",
        "/api/reset/plan?mode=soft&ref=dev",
        "/api/reset/plan?mode=mixed&ref=main",
        "/api/discard/plan",
        "/api/discard/plan?files=",
        "/api/discard/plan?files=a.txt",
        "/api/discard/plan?files=A.TXT",
        "/api/discard/plan?files=sub/b.txt&files=a.txt",
        "/api/discard/plan?files=a.txt&files=",
        "/api/discard/plan?files=nope.txt",
        "/api/discard/plan?files=sub",
        "/api/discard/plan?files=sub/",
        "/api/discard/plan?files=sub/b.txt",
        "/api/discard/plan?files=.git/config",
        "/api/discard/plan?files=.GIT/config",
        "/api/discard/plan?files=../outside.txt",
        "/api/discard/plan?files=C%3A%2FWindows",
        "/api/discard/plan?files=.",
        "/api/discard/plan?files=./",
        "/api/discard/plan?files=a.txt%00x",
        "/api/discard/plan?files=%FF",
        "/api/discard/plan?files=.git",
        "/api/discard/plan?files=.git/../a.txt",
        "/api/discard/plan?files=a.txt/",
        "/api/discard/plan?files=a.txt%20",
        "/api/discard/plan?files=sub%2Fb.txt",
        "/api/discard/plan?files=&files=",
        "/api/discard/plan?files=%2E%2E%2Fx",
        "/api/reset/plan?mode=soft&ref=HEAD%20",
        "/api/reset/plan?mode=soft&ref=%2E%2E",
        "/api/reset/plan?mode=soft&ref=HEAD%7E1",
        "/api/discard/plan?files=link/config",
        "/api/discard/plan?files=link",
        "/api/discard/plan?files=selflink",
        "/api/discard/plan?files=selflink/sub/b.txt",
    ]),
    ("dirty", fx_dirty, [
        "/api/reset/plan?mode=hard",
        "/api/reset/plan?mode=soft&ref=HEAD~1",
        "/api/discard/plan",
        "/api/discard/plan?files=" + Q("空 格.txt"),
        "/api/discard/plan?files=" + Q("新 文件.txt"),
        "/api/discard/plan?files=u.txt&files=" + Q("新 文件.txt") + "&files=a.txt",
        "/api/discard/plan?files=newdir",
        "/api/discard/plan?files=newdir/",
        "/api/discard/plan?files=sub",
        "/api/discard/plan?files=sub/b.txt",
        "/api/discard/plan?files=sub/c.txt",
        "/api/discard/plan?files=ignored.txt",
        "/api/discard/plan?files=a.txt&files=sub",
        "/api/discard/plan?files=./sub/../a.txt",
    ]),
    ("unborn", fx_unborn, [
        "/api/reset/plan?mode=soft",
        "/api/reset/plan?mode=mixed",
        "/api/reset/plan?mode=hard",
        "/api/reset/plan?mode=hard&ref=HEAD",
        "/api/discard/plan",
        "/api/discard/plan?files=x.txt",
        "/api/discard/plan?files=untracked.txt",
        "/api/discard/plan?files=.",
    ]),
    ("detached", fx_detached, [
        "/api/reset/plan?mode=hard",
        "/api/reset/plan?mode=soft&ref=HEAD~2",
        "/api/reset/plan?mode=mixed&ref=HEAD~1",
        "/api/reset/plan?mode=hard&ref=HEAD~2",
        "/api/discard/plan",
        "/api/discard/plan?files=f.txt",
        "/api/discard/plan?files=new.txt",
    ]),
    ("stash", fx_stash, [
        "/api/reset/plan?mode=mixed",
        "/api/reset/plan?mode=soft&ref=HEAD~1",
        "/api/discard/plan",
        "/api/discard/plan?files=a.txt",
        "/api/discard/plan?files=b.txt&files=a.txt",
    ]),
    ("refs", fx_refs, [
        "/api/reset/plan?mode=soft&ref=HEAD~2",
        "/api/reset/plan?mode=mixed&ref=HEAD~3",
        "/api/reset/plan?mode=hard&ref=dev",
        "/api/reset/plan?mode=soft&ref=" + Q("中文分支"),
        "/api/reset/plan?mode=soft&ref=%40%7B-1%7D",
        "/api/reset/plan?mode=soft&ref=bad..ref",
        "/api/reset/plan?mode=soft&ref=-x",
        "/api/reset/plan?mode=soft&ref=a%3Ab",
        "/api/reset/plan?mode=soft&ref=a%20b",
        "/api/reset/plan?mode=soft&ref=" + Q("。"),
        "/api/reset/plan?mode=soft&ref=HEAD%00x",
        "/api/reset/plan?mode=soft&ref=nope",
        "/api/discard/plan?files=f.txt",
        "/api/discard/plan",
    ]),
]


# ---------------------------------------------------------------- 服务

def start_py(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py"),
                             "--root", repo, "--port", str(port)],
                            env=env, cwd=PY_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_c(repo, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            env=env, cwd=C_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def req(port, path, tries=3):
    last = None
    for _ in range(tries):
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="GET")
            try:
                r = urllib.request.urlopen(rq, timeout=120)
                code, payload = r.getcode(), r.read()
            except urllib.error.HTTPError as e:
                code, payload = e.code, e.read()
            try:
                js = json.loads(payload.decode("utf-8"))
            except Exception as e:
                js = ("<<not json>>", str(e), payload[:120].decode("utf-8", "replace"))
            return {"code": code, "json": js}
        except Exception as e:            # 连接被重置 / 超时：重试一次再判
            last = e
            time.sleep(0.2)
    return {"code": "EXC", "json": repr(last)}


def stop(p):
    try:
        p.terminate()
        p.wait(15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 比较

def short(js, n=400):
    s = json.dumps(js, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "...(len=%d)" % len(s)


def endpoint_of(path):
    parts = path.split("?")[0].rstrip("/").split("/")
    return "/".join(parts[-2:])


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    print("  Python 参考版: %s" % PY_ROOT)
    print("  C 版 exe     : %s" % EXE)

    matrix = {}
    fails_total = 0
    cases_total = 0
    try:
        for fname, builder, cases in FIXTURES:
            repo = builder()
            pa, pb = free_port(), free_port()
            while pb == pa:
                pb = free_port()
            pp = start_py(repo, pa)
            pc = start_c(repo, pb)
            try:
                ok_py = wait_health(pa, pp)
                ok_c = wait_health(pb, pc)
                if not (ok_py and ok_c):
                    print("  FAIL [%s] 服务没起来 (python=%s c=%s)" % (fname, ok_py, ok_c))
                    fails_total += len(cases)
                    cases_total += len(cases)
                    continue
                print("\n=== fixture: %s (%s)" % (fname, repo))
                for path in cases:
                    ep = endpoint_of(path)
                    cell = matrix.setdefault(ep, {}).setdefault(fname, [0, 0])
                    a = req(pa, path)
                    b = req(pb, path)
                    same = (a["code"] == b["code"] and a["json"] == b["json"])
                    cases_total += 1
                    if same:
                        cell[0] += 1
                        print("  PASS %-58s code=%s" % (path, a["code"]))
                    else:
                        cell[1] += 1
                        fails_total += 1
                        print("  FAIL %-58s" % path)
                        print("       python: code=%s json=%s" % (a["code"], short(a["json"])))
                        print("       c     : code=%s json=%s" % (b["code"], short(b["json"])))
            finally:
                stop(pp)
                stop(pc)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    eps = ["reset/plan", "discard/plan"]
    print("\n=== PASS/FAIL 矩阵（端点 × fixture）")
    header = "  %-14s" % "endpoint"
    for fname, _, _ in FIXTURES:
        header += " %-12s" % fname
    header += " %s" % "合计"
    print(header)
    for ep in eps:
        row = "  %-14s" % ("/" + ep)
        tp = tf = 0
        for fname, _, _ in FIXTURES:
            p_, f_ = matrix.get(ep, {}).get(fname, [0, 0])
            tp += p_
            tf += f_
            row += " %-12s" % ("%dP/%dF" % (p_, f_) if (p_ + f_) else "-")
        row += " %d/%d" % (tp, tp + tf)
        print(row)
    print("\n  失败 %d / %d" % (fails_total, cases_total))
    return 1 if fails_total else 0


if __name__ == "__main__":
    sys.exit(main())
