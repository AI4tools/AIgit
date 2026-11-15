# -*- coding: utf-8 -*-
"""端到端演练 2：冲突合并 —— 「节点出问题」的另一条回退路径。

场景（分支合并里出现冲突，这是最常见的「现场坏了」）：
  1. main 上 f.txt = "base"；
  2. 建分支 side，f.txt 改成 "side 写的"；
  3. 回 main，f.txt 改成 "main 写的"；
  4. merge side -> 冲突；
  5. **回退现场**：merge-abort / conflict/abort -> 回到 main 的干净现场（含文件内容、
     HEAD、工作区三方都复原）；
  6. 再 merge -> conflict/choose 选边 -> conflict/commit -> 合并提交成功。

判据：状态码 + 文件内容逐字节 + HEAD + 工作区干净 + 合并提交确实有 2 个父提交。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(
    os.path.dirname(HERE), "build", "aigit.exe")
BASE = tempfile.mkdtemp(prefix="aigit_conflict_sc_")
FAILS = []
OKS = []


def log(msg):
    print(msg, flush=True)


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
        log("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILS.append(name)
        log("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def api(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse(); raw = r.read().decode("utf-8", "replace"); c.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def w(repo, rel, text):
    with open(os.path.join(repo, rel), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def rd(repo, rel):
    with open(os.path.join(repo, rel), encoding="utf-8") as f:
        return f.read()


def head(repo):
    return sh("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()


def main():
    repo = os.path.join(BASE, "repo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    for k, v in (("user.name", "agent"), ("user.email", "agent@example.com")):
        sh("git", "config", k, v, cwd=repo)
    w(repo, "f.txt", "base\n")
    w(repo, "g.txt", "shared\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    base_sha = head(repo)

    port = free_port()
    proc = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            api(port, "GET", "/api/health"); break
        except Exception:
            time.sleep(0.1)

    try:
        log("\n=== 造两侧改动")
        st, js = api(port, "POST", "/api/branch", {"name": "side"})
        ok("建分支 side 并切过去", st == 200 and js.get("branch") == "side")
        w(repo, "f.txt", "side 写的\n")
        api(port, "POST", "/api/stage", {"files": ["f.txt"]})
        api(port, "POST", "/api/commit", {"message": "feat: side 改 f.txt"})
        side_sha = head(repo)
        ok("side 有自己的一次提交", side_sha != base_sha)

        st, js = api(port, "POST", "/api/checkout", {"name": "main"})
        ok("切回 main", st == 200 and js.get("branch") == "main")
        ok("切回后 f.txt 是 base 内容", rd(repo, "f.txt") == "base\n")
        w(repo, "f.txt", "main 写的\n")
        api(port, "POST", "/api/stage", {"files": ["f.txt"]})
        api(port, "POST", "/api/commit", {"message": "feat: main 改 f.txt"})
        main_sha = head(repo)
        ok("main 有自己的一次提交", main_sha not in (base_sha, side_sha))
        main_content = rd(repo, "f.txt")

        log("\n=== 合并 -> 冲突")
        st, js = api(port, "POST", "/api/merge", {"name": "side"})
        ok("merge 明确报告失败/冲突（不是假 ok）",
           st in (200, 409) and (js.get("ok") is False or js.get("conflict")),
           str(js)[:110])
        st, js = api(port, "GET", "/api/conflict/state")
        # 字段名是 inMerge（不是 merging）
        ok("conflict/state 说正在合并", st == 200 and js.get("inMerge") is True,
           str(js)[:90])
        st, js = api(port, "GET", "/api/conflict/list")
        paths = [c.get("path") for c in (js.get("conflicts") or js.get("files") or [])]
        ok("冲突列表里有 f.txt", any(p == "f.txt" for p in paths), str(paths))
        st, js = api(port, "GET", "/api/conflict/inspect?file=f.txt")
        blob = json.dumps(js, ensure_ascii=False)
        ok("inspect 给出两侧内容", "side 写的" in blob and "main 写的" in blob, blob[:120])
        ok("冲突时工作区里有冲突标记", "<<<<<<<" in rd(repo, "f.txt"))

        log("\n=== 回退现场 A：merge-abort")
        st, js = api(port, "POST", "/api/merge-abort")
        ok("merge-abort 200", st == 200, str(js)[:70])
        st, js = api(port, "GET", "/api/conflict/state")
        ok("state 回到非合并", js.get("inMerge") is False, str(js)[:70])
        ok("HEAD 还是 main 那个提交（没多出半截合并）", head(repo) == main_sha)
        ok("f.txt 逐字节回到 main 的内容", rd(repo, "f.txt") == main_content)
        st, js = api(port, "GET", "/api/status")
        ok("工作区干净", js.get("staged") == [] and js.get("unstaged") == []
           and js.get("conflicted") == [])

        log("\n=== 再合并一次，这次用 conflict/abort 回退")
        api(port, "POST", "/api/merge", {"name": "side"})
        st, js = api(port, "POST", "/api/conflict/abort")
        ok("conflict/abort 200", st == 200, str(js)[:70])
        ok("abort 后 HEAD 与文件都复原",
           head(repo) == main_sha and rd(repo, "f.txt") == main_content)

        log("\n=== 再合并一次，这次解决冲突并提交")
        st, js = api(port, "POST", "/api/merge", {"name": "side"})
        st, js = api(port, "POST", "/api/conflict/choose",
                     {"file": "f.txt", "side": "theirs"})
        ok("choose theirs 200", st == 200, str(js)[:70])
        ok("f.txt 变成 side 的版本", rd(repo, "f.txt") == "side 写的\n")
        st, js = api(port, "POST", "/api/conflict/commit",
                     {"message": "merge: side"})
        ok("conflict/commit 200", st == 200, str(js)[:80])
        parents = sh("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).stdout.split()
        ok("合并提交有 2 个父提交", len(parents) == 3, "parents=%d" % (len(parents) - 1))
        st, js = api(port, "GET", "/api/status")
        ok("合并后工作区干净", js.get("staged") == [] and js.get("unstaged") == []
           and js.get("conflicted") == [])
        st, js = api(port, "GET", "/api/log?limit=5")
        log("    log: " + " | ".join("%s %s" % (c["sha"][:8], c["subject"]) for c in js["log"]))
        ok("log 里有合并提交", any("merge" in c["subject"].lower() for c in js["log"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    log("\n" + "=" * 62)
    log("  通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    for f in FAILS:
        log("   FAIL: " + f)
    if os.environ.get("AIGIT_KEEP_SCENARIO") != "1":
        shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
