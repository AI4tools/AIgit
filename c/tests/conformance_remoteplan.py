# -*- coding: utf-8 -*-
"""M1 远程计划面一致性判据：C 版 vs Python 版（同一仓、同一时刻、同一份请求）。

覆盖 2 个端点族 × 7 个 fixture × 3 档能力闸：
    /api/remote/heads?remote=<name>
    /api/remote/plan/fetch|push?remote=&branch=&force=&delete=&confirmed=
    fixture:
      norepo      不是 git 仓（.git 不存在 -> not_a_repo；**这一组按设计全是 400**）
      noremote    有提交、无任何 remote
      untracked   bare remote 可达、本地**没有** origin/<branch> 跟踪 ref
      tracked     bare remote 可达 + origin/main、origin/feat/x 都在，本地领先 2 个提交
      weirdname   remote 名合法但带点（my.remote）可达；同时打空格/横杠/斜杠/中文/NUL 名
      unborn      未出生 HEAD（有 remote，指向不存在的路径）
      baremulti   bare 上有多条分支（含 feat/x 与中文名）+ 一个 tag（--heads 必须过滤掉）
    环境变量（两侧**同时**注入同一份）：
      off（不设 AIGIT_NET）/ read / write

判据（逐条全等，不看「像不像」）：
    * HTTP 状态码
    * JSON 深比较（键名/类型/null/code/extra/数组顺序）
    * 两侧都不许出现 5xx
另外单独统计 X-aigit-git-calls / X-aigit-git-cmds 的差异（只报告，不算失败：
Python 在 net_disabled 时仍记了一次「未 fork 的 ls-remote」，C 版没有那个计数入口）。

用法：
    python -X utf8 c/tests/conformance_remoteplan.py
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
C_ROOT = os.path.dirname(HERE)                 # ...\c_agent_remoteplan\c
EXE = os.path.join(C_ROOT, "build", "aigit.exe")
_PY_SIBLING = os.path.join(os.path.dirname(C_ROOT), "python-ref", "server.py")
PY_ROOT = os.path.dirname(_PY_SIBLING) if os.path.isfile(_PY_SIBLING) else r"D:\project\aigit\python-ref"
BASE = os.path.join(os.environ["TEMP"], "aigit_c_rp_%d" % (os.getpid() % 100000))


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


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def commit(d, name, text, msg):
    write(os.path.join(d, name), text)
    must(d, "add", "-A")
    must(d, "commit", "-q", "-m", msg)


def new_bare(name):
    d = os.path.join(BASE, name + ".git")
    os.makedirs(d)
    sh(d, "init", "-q", "--bare", "-b", "main")
    return d


# ---------------------------------------------------------------- fixtures

def fx_repogone():
    """真仓；服务起来之后把 .git 删掉 -> 两侧都该回 not_a_repo。

    注意：不能直接用一个「空目录」当非仓 fixture —— server.py 的 main() 在
    `not REPO.exists()` 时会 **git init** 那个目录（见 server.py 的 --sandbox
    旁那段），所以空目录在 Python 侧会自己变成仓。删 .git 是唯一能稳定拿到
    not_a_repo 的路径。**这一组按设计全是 400**（见输出里的 200 计数）。"""
    d = new_repo("repogone")
    commit(d, "a.txt", b"a\n", "c1")
    return d


def drop_git(repo):
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", os.path.join(repo, ".git")],
                   capture_output=True)


def fx_noremote():
    d = new_repo("noremote")
    commit(d, "a.txt", b"a\n", "c1")
    return d


def fx_untracked():
    """remote 可达，但本地没有跟踪 ref（推完再把 refs/remotes 删掉）。"""
    b = new_bare("untracked_remote")
    d = new_repo("untracked")
    commit(d, "a.txt", b"a\n", "c1")
    must(d, "remote", "add", "origin", b)
    must(d, "push", "-q", "origin", "main")
    must(d, "update-ref", "-d", "refs/remotes/origin/main")
    return d


def fx_tracked():
    """origin/main 与 origin/feat/x 都在；本地领先 2 个提交。"""
    b = new_bare("tracked_remote")
    d = new_repo("tracked")
    commit(d, "a.txt", b"a\n", "c1")
    must(d, "remote", "add", "origin", b)
    must(d, "push", "-q", "-u", "origin", "main")
    must(d, "branch", "feat/x")
    must(d, "push", "-q", "-u", "origin", "feat/x")
    commit(d, "a.txt", b"a2\n", "c2 two")
    commit(d, "b.txt", b"b\n", "c3 three")
    # 主题里带 \x1f：`git log --format=...` 那一行会拆出 **6** 段，
    # Python 的 `if len(f) == 5` 会跳过它 —— 所以 commits 比 ahead 少一条。
    # 这是 plan_push 里唯一「字段数不对就丢」的分支，必须被覆盖。
    must(d, "commit", "-q", "--allow-empty", "-m", "a\x1fb")
    return d


def fx_weirdname():
    """remote 名带点（合法），bare 可达；查询里另打一批非法名。"""
    b = new_bare("weird_remote")
    d = new_repo("weirdname")
    commit(d, "a.txt", b"a\n", "c1")
    must(d, "remote", "add", "my.remote_1", b)
    must(d, "push", "-q", "my.remote_1", "main")
    return d


def fx_unborn():
    """未出生 HEAD：没有提交，remote 指向不存在的路径。"""
    d = new_repo("unborn")
    write(os.path.join(d, "x.txt"), b"x\n")
    must(d, "remote", "add", "origin", os.path.join(BASE, "nowhere.git"))
    return d


def fx_baremulti():
    """bare 上多条分支（含斜杠与中文）+ tag；--heads 必须把 tag 过滤掉。"""
    b = new_bare("multi_remote")
    d = new_repo("baremulti")
    commit(d, "a.txt", b"a\n", "c1")
    must(d, "remote", "add", "origin", b)
    must(d, "branch", "feat/x")
    must(d, "branch", "中文分支")
    must(d, "tag", "v1")
    must(d, "push", "-q", "origin", "main", "feat/x", "中文分支")
    must(d, "push", "-q", "origin", "v1")
    must(d, "fetch", "-q", "origin")
    return d


# ---------------------------------------------------------------- 用例

HEADS = [
    "/api/remote/heads",
    "/api/remote/heads?remote=origin",
    "/api/remote/heads?remote=",                       # 空值 = 没给 -> origin
    "/api/remote/heads?remote=my%20origin",            # 含空格 -> bad_remote
    "/api/remote/heads?remote=-x",                     # 横杠开头 -> bad_remote
    "/api/remote/heads?remote=a%2Fb",                  # 含斜杠 -> bad_remote
    "/api/remote/heads?remote=%E4%B8%AD",              # 非 ASCII -> bad_remote
    "/api/remote/heads?remote=ori%00gin",              # 内嵌 NUL -> bad_remote
    "/api/remote/heads?remote=a&remote=b",             # 重复参数取第一个
    "/api/remote/heads?remote=nope",                   # 没有这个 remote
    "/api/remote/heads?remote=my.remote_1",            # 合法但多数 fixture 里不存在
    "/api/remote/heads?remote=" + "a" * 100,           # 边界：100 字符**合法**
    "/api/remote/heads?remote=" + "a" * 101,           # 边界：101 字符 -> bad_remote
]

PLAN_FETCH = [
    "/api/remote/plan/fetch",
    "/api/remote/plan/fetch?remote=origin",
    "/api/remote/plan/fetch?branch=main",
    "/api/remote/plan/fetch?branch=nope",              # 没有跟踪 ref -> 200, aheadBehind=null
    "/api/remote/plan/fetch?branch=",                  # 空值 = 没给
    "/api/remote/plan/fetch?branch=bad..ref",          # 无跟踪 ref 时**不**校验
    "/api/remote/plan/fetch?branch=feat%2Fx",
    "/api/remote/plan/fetch?remote=my%20origin",
    "/api/remote/plan/fetch?remote=my.remote_1",        # 合法名（多数 fixture 里不存在）
    "/api/remote/plan/fetch?remote=nope",               # 不存在也不要紧：预演纯本地
    "/api/remote/plan/fetch?confirmed=true",           # confirmed 在 GET 上被忽略
    "/api/remote/plan/fetch?confirmed=1&confirmed=0",
    "/api/remote/plan/fetch?branch=a&branch=b",
]

PLAN_PUSH = [
    "/api/remote/plan/push",
    "/api/remote/plan/push?branch=main",
    "/api/remote/plan/push?branch=main&force=1",
    "/api/remote/plan/push?branch=main&force=true",
    "/api/remote/plan/push?branch=main&force=yes",
    "/api/remote/plan/push?branch=main&force=on",
    "/api/remote/plan/push?branch=main&force=TRUE",    # 大小写敏感 -> false
    "/api/remote/plan/push?branch=main&force=",        # 空值 = 没给
    "/api/remote/plan/push?branch=main&delete=1",
    "/api/remote/plan/push?branch=main&delete=yes",
    "/api/remote/plan/push?branch=main&delete=0",
    "/api/remote/plan/push?branch=main&confirmed=true",
    "/api/remote/plan/push?branch=main&confirmed=0",
    "/api/remote/plan/push?branch=feat%2Fx",
    "/api/remote/plan/push?branch=nope",               # log 失败 -> git_failed
    "/api/remote/plan/push?remote=my%20origin",
    "/api/remote/plan/push?remote=my.remote_1",
    "/api/remote/plan/push?remote=nope",
    "/api/remote/plan/push?branch=-x",                 # bad_ref
    "/api/remote/plan/push?branch=a%3Ab",              # 含冒号 -> bad_ref
    "/api/remote/plan/push?branch=a%20b",              # 含空格 -> bad_ref
    "/api/remote/plan/push?branch=refs%2Fheads%2Fmain",
]

PLAN_OTHER = [
    "/api/remote/plan/nope",
    "/api/remote/plan/",
    "/api/remote/plan",                                # 没有尾斜杠 -> 404（http.c 兜底）
    "/api/remote/plan/fetch/x",                        # 末段是 x -> 没有这个预演
]

ALL_CASES = HEADS + PLAN_FETCH + PLAN_PUSH + PLAN_OTHER

FIXTURES = [
    ("repogone", fx_repogone, ALL_CASES),
    ("noremote", fx_noremote, ALL_CASES),
    ("untracked", fx_untracked, ALL_CASES),
    ("tracked", fx_tracked, ALL_CASES),
    ("weirdname", fx_weirdname, ALL_CASES),
    ("unborn", fx_unborn, ALL_CASES),
    ("baremulti", fx_baremulti, ALL_CASES),
]

# 服务起来**之后**才做的动作（只有 repogone 需要）
POST_START = {
    "repogone": drop_git,
}

ENVS = [
    ("off", None),            # 不设 AIGIT_NET -> source=default, level=off
    ("read", "read"),
    ("write", "write"),
    ("off_env", "off"),       # 显式 off -> source=env:AIGIT_NET（消息里那句会变）
    ("junk", "  Raed  "),     # 读不懂 -> 按 off + gate.note（含原始 repr）
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
    for attempt in range(tries):
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                        method="GET")
            try:
                r = urllib.request.urlopen(rq, timeout=120)
                code, payload, hdr = r.getcode(), r.read(), r.headers
            except urllib.error.HTTPError as e:
                code, payload, hdr = e.code, e.read(), e.headers
            try:
                js = json.loads(payload.decode("utf-8"))
            except Exception as e:
                js = ("<<not json>>", str(e),
                      payload[:200].decode("utf-8", "replace"))
            return {
                "code": code,
                "json": js,
                "calls": hdr.get("X-aigit-git-calls"),
                "cmds": hdr.get("X-aigit-git-cmds"),
            }
        except Exception as e:
            last = e
            time.sleep(0.2)
    return {"code": "EXC", "json": repr(last), "calls": None, "cmds": None}


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

def short(js, n=420):
    s = json.dumps(js, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "...(len=%d)" % len(s)


def endpoint_of(path):
    p = path.split("?")[0]
    if p == "/api/remote/heads":
        return "heads"
    if p.startswith("/api/remote/plan/"):
        seg = p.rsplit("/", 1)[-1]
        return "plan/" + seg if seg else "plan/<空>"
    if p == "/api/remote/plan":
        return "plan(无斜杠)"
    return p


def main():
    if os.path.isdir(BASE):
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.isfile(EXE):
        print("  缺 %s（先跑 mingw32-make -C c）" % EXE)
        return 1
    print("  Python 参考版: %s" % PY_ROOT)
    print("  C 版 exe     : %s" % EXE)

    matrix = {}           # (endpoint, env) -> {fixture: [pass, fail]}
    ok200 = {}            # fixture -> 200 计数（证明不是「全 400 通过」）
    shapes = {}           # endpoint -> {(code, json.code): 次数}（取自 Python 侧）
    fails_total = cases_total = 0
    call_diffs = []
    try:
        for fname, builder, cases in FIXTURES:
            repo = builder()
            for ename, net in ENVS:
                pa, pb = free_port(), free_port()
                while pb == pa:
                    pb = free_port()
                pp = start_py(repo, pa, net)
                pc = start_c(repo, pb, net)
                try:
                    ok_py = wait_health(pa, pp)
                    ok_c = wait_health(pb, pc)
                    if not (ok_py and ok_c):
                        print("  FAIL [%s/%s] 服务没起来 (python=%s c=%s)"
                              % (fname, ename, ok_py, ok_c))
                        fails_total += len(cases)
                        cases_total += len(cases)
                        continue
                    hook = POST_START.get(fname)
                    if hook is not None:
                        hook(repo)
                    print("\n=== fixture: %s  net=%s  (%s)" % (fname, ename, repo))
                    for path in cases:
                        ep = endpoint_of(path)
                        cell = matrix.setdefault((ep, ename), {}).setdefault(
                            fname, [0, 0])
                        a = req(pa, path)
                        b = req(pb, path)
                        same = (a["code"] == b["code"] and a["json"] == b["json"])
                        if a["code"] == 200:
                            ok200[fname] = ok200.get(fname, 0) + 1
                        ck = a["json"].get("code") if isinstance(a["json"], dict) else "?"
                        if isinstance(a["json"], dict) and "ok" in a["json"] \
                                and a["json"]["ok"] is True:
                            ck = "ok"
                        shapes.setdefault(ep, {})
                        key = (a["code"], ck)
                        shapes[ep][key] = shapes[ep].get(key, 0) + 1
                        cases_total += 1
                        if same:
                            cell[0] += 1
                            print("  PASS %-56s code=%s calls=%s cmds=%s"
                                  % (path, a["code"], a["calls"], a["cmds"]))
                        else:
                            cell[1] += 1
                            fails_total += 1
                            print("  FAIL %-56s" % path)
                            print("       python: code=%s calls=%s cmds=%s json=%s"
                                  % (a["code"], a["calls"], a["cmds"], short(a["json"])))
                            print("       c     : code=%s calls=%s cmds=%s json=%s"
                                  % (b["code"], b["calls"], b["cmds"], short(b["json"])))
                        if (a["calls"], a["cmds"]) != (b["calls"], b["cmds"]):
                            call_diffs.append((fname, ename, path,
                                               a["calls"], a["cmds"],
                                               b["calls"], b["cmds"]))
                        for side, r in (("python", a), ("c", b)):
                            if isinstance(r["code"], int) and r["code"] >= 500:
                                print("       !! %s 出现 %d（禁止）" % (side, r["code"]))
                finally:
                    stop(pp)
                    stop(pc)
    finally:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", BASE], capture_output=True)

    # 矩阵：行 = 端点 × 能力闸，列 = fixture
    print("\n=== PASS/FAIL 矩阵（端点×闸 × fixture）")
    header = "  %-18s %-8s" % ("endpoint", "net")
    for fname, _, _ in FIXTURES:
        header += " %-11s" % fname
    header += " 合计"
    print(header)
    eps = ["heads", "plan/fetch", "plan/push", "plan/<空>", "plan(无斜杠)"]
    for ep in eps:
        for ename, _ in ENVS:
            row = "  %-18s %-8s" % (ep, ename)
            tp = tf = 0
            for fname, _, _ in FIXTURES:
                p_, f_ = matrix.get((ep, ename), {}).get(fname, [0, 0])
                tp += p_
                tf += f_
                row += " %-11s" % ("%dP/%dF" % (p_, f_) if (p_ + f_) else "-")
            row += " %d/%d" % (tp, tp + tf)
            print(row)
    print("\n  200 响应数（证明不是全 400 通过）:")
    for fname, _, _ in FIXTURES:
        print("    %-11s %d" % (fname, ok200.get(fname, 0)))

    print("\n  响应形状分布（取自 Python 侧；同一端点覆盖了多种结局）:")
    for ep in eps:
        items = shapes.get(ep, {})
        if not items:
            continue
        parts = ["%s/%s×%d" % (c, k, n)
                 for (c, k), n in sorted(items.items(), key=lambda x: str(x[0]))]
        print("    %-14s %s" % (ep, "  ".join(parts)))

    if call_diffs:
        print("\n  git 调用计数差异（只报告，不算失败）: %d 条" % len(call_diffs))
        for d in call_diffs[:10]:
            print("    %s/%s %s  python=(%s,%s) c=(%s,%s)" % d)
        if len(call_diffs) > 10:
            print("    ... 其余 %d 条同类" % (len(call_diffs) - 10))
    else:
        print("\n  git 调用计数：两侧完全一致")

    print("\n  失败 %d / %d" % (fails_total, cases_total))
    return 1 if fails_total else 0


if __name__ == "__main__":
    sys.exit(main())
