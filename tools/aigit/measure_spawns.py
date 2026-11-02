#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_spawns.py —— P1:量「每个端点一次请求实际起几个 git 子进程」。

依赖 server.py 的计数器门:设 AIGIT_GIT_COUNT=1 后,每个响应(含 4xx)带
``X-aigit-git-calls``(次数)与 ``X-aigit-git-cmds``(命令名序列,>200 字符截断)。
只碰 ``--sandbox`` 演练仓,不碰任何真实仓库。

流程:
  1. 起一个 AIGIT_GIT_COUNT=1 的 --sandbox 服务,逐个打端点,记响应头与耗时;
  2. 再起一个**不带门**的服务,打同一批端点,核对响应体在开关两种模式下一致:
       * raw_equal    原始字节一致
       * masked_equal 掩掉 sha(7~40 位 hex)与时间戳后一致 —— 两次起服
         沙盒 commit 的 sha / 时间戳天然不同,这是唯一的时变性来源
       * shape_equal  JSON 键集 + 叶值类型 + 列表长度结构一致
  3. 打表:按「预期可省毫秒 = (spawns-1) × 40 ms」从大到小
     (40ms 是 Windows 下 git 子进程的单次地板价,A5/E13 实测 ±2.5ms,
      这里是外推,不是逐端点实测 —— 口径与局限见 doc/aigit-perf-spawns.md)。

POST 一条都没打:本服务的全部 plan 路由(reset/discard/history/remote plan)
都在 GET 侧,POST 侧没有只读 plan 类端点;其余 POST 全是改仓的(stage/
commit/checkout/reset/discard/...),按纪律不碰。

用法:
    python tools/aigit/measure_spawns.py --out tools\aigit\spawns-report.json
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.join(ROOT, "server.py")

# server.py GET 路由表里除了静态文件之外的端点,补了变体(log/reflog 游标、
# reset 三种 mode、discard 带 files、remote 两个名字)。全部只读。
GETS = [
    "/api/health",
    "/api/status",
    "/api/log?limit=30",
    "/api/log?limit=5",
    "/api/branches",
    "/api/diff?file=README.md",
    "/api/diff?file=README.md&staged=1",
    "/api/file?file=README.md",
    "/api/conflict/state",
    "/api/conflict/list",
    "/api/conflict/inspect?file=README.md",
    "/api/history/stashes",
    "/api/history/stash?index=0",
    "/api/history/stash?index=0&stat=1",
    "/api/history/reflog?limit=30",
    "/api/history/reflog?limit=5",
    "/api/history/tags",
    "/api/history/blame?file=README.md",
    "/api/history/file?file=README.md",
    "/api/history/plan/stash?message=p",
    "/api/history/plan/stash-drop?index=0",
    "/api/history/plan/amend?message=p",
    "/api/history/plan/undo-last-commit",
    "/api/history/plan/undo-last-commit?drop_changes=1",
    "/api/history/plan/reflog-restore?index=0",
    "/api/history/plan/reflog-restore?index=0&mode=hard",
    "/api/history/plan/revert?sha=HEAD",
    "/api/history/plan/branch-rename?old=main&new=main2",
    "/api/history/plan/branch-delete?name=hotfix",
    "/api/history/plan/tag-delete?name=v1",
    "/api/reset/plan?mode=soft",
    "/api/reset/plan?mode=mixed",
    "/api/reset/plan?mode=hard",
    "/api/discard/plan",
    "/api/discard/plan?files=README.md",
    "/api/remote/capability",
    "/api/remote/list",
    "/api/remote/heads?remote=origin",
    "/api/remote/heads?remote=upstream",
    "/api/remote/ahead-behind?remote=origin",
    "/api/remote/plan/fetch?remote=origin",
    "/api/remote/plan/push?remote=origin",
]

# ---- ON/OFF 响应体比对用的三种口径 -----------------------------------

_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"
                 r"|\b(?:1[6-9]|20)[0-9]{8}\b"
                 r"|\b(?:1[6-9]|20)[0-9]{11}\b")


def masked(s):
    return _TS.sub("<ts>", _SHA.sub("<sha>", s))


def shape(x):
    """键集 + 叶值类型 + 列表长度的结构快照。"""
    if isinstance(x, dict):
        return {"<keys>": sorted(x.keys()),
                "<vals>": [shape(x[k]) for k in sorted(x.keys())]}
    if isinstance(x, list):
        return {"<len>": len(x), "<item>": shape(x[0]) if x else None}
    if isinstance(x, bool):
        return "bool"
    if isinstance(x, int):
        return "int"
    if isinstance(x, float):
        return "float"
    if isinstance(x, str):
        return "str"
    if x is None:
        return "null"
    return type(x).__name__


# ---- 起服 / 打请求 ----------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(count_env):
    """起 --sandbox 服务。count_env=True 时带 AIGIT_GIT_COUNT=1。"""
    tmp = tempfile.mkdtemp(prefix="aigit_spawns_")
    port = free_port()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if count_env:
        env["AIGIT_GIT_COUNT"] = "1"
    elif "AIGIT_GIT_COUNT" in env:
        del env["AIGIT_GIT_COUNT"]
    err = open(os.path.join(tmp, "e.log"), "wb")
    p = subprocess.Popen(
        [sys.executable, "-X", "utf8", SERVER, "--root",
         os.path.join(tmp, "root"), "--port", str(port), "--sandbox"],
        stdout=subprocess.DEVNULL, stderr=err, env=env)
    for _ in range(80):
        try:
            call(port, "/api/health")
            return p, err, port
        except Exception:
            time.sleep(0.25)
    p.terminate()
    err.close()
    raise RuntimeError("服务 20s 内没起来(端口 %d)" % port)


def call(port, path):
    """GET 一个端点,返回 (code, headers, body_text, elapsed_ms)。4xx 也吃。"""
    url = "http://127.0.0.1:%d%s" % (port, path)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            code, h, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        code, h, body = e.code, e.headers, e.read()
    ms = (time.perf_counter() - t0) * 1000.0
    return code, h, body.decode("utf-8", "replace"), ms


# ---- 报表 ------------------------------------------------------------

def report(on, off):
    out = []
    out.append("== P1 git 子进程计数:按「可省 ms=(spawns-1)*40」从大到小 ==")
    out.append("%-46s %4s %6s   %-28s %s"
               % ("endpoint", "HTTP", "spawns", "cmds(截断前<=32)", "elapsed_ms"))
    for r in sorted(on, key=lambda x: (-(x["spawns"] - 1), x["path"])):
        save = (r["spawns"] - 1) * 40
        out.append("%-46s %4d %6d   %-28s %6.0f   save=%d"
                   % (r["path"], r["code"], r["spawns"],
                      ",".join(r["cmds"])[:28], r["ms"], save))
    out.append("")
    out.append("== ON/OFF 响应体核对(rule:两种模式下同一个端点) ==")
    out.append("%-46s %4s %4s %4s"
               % ("endpoint", "raw", "mask", "shape"))
    n_ok = 0
    for r_on, r_off in zip(on, off):
        raw = r_on["body"] == r_off["body"]
        mask = masked(r_on["body"]) == masked(r_off["body"])
        st = (shape(json.loads(r_on["body"])) ==
              shape(json.loads(r_off["body"])))
        if raw and mask and st and r_on["code"] == r_off["code"]:
            n_ok += 1
        out.append("%-46s %4s %4s %4s   (%d/%d)"
                   % (r_on["path"], "Y" if raw else "N",
                      "Y" if mask else "N", "Y" if st else "N",
                      r_on["code"], r_off["code"]))
    out.append("")
    out.append("响应体一致的口径:raw=Y 或 (mask=Y 且 shape=Y 且 HTTP 码相同)。"
               " sha 与时间戳在两次起服之间必然不同,所以 mask 是主口径。")
    out.append("ON/OFF 响应体一致端点数: %d/%d" % (n_ok, len(on)))
    return "\n".join(out), n_ok


def main():
    ap_sys = sys.argv[1:]
    out_path = None
    if "--out" in ap_sys:
        out_path = ap_sys[ap_sys.index("--out") + 1]

    recs = {}
    for tag, count_env in (("on", True), ("off", False)):
        p, err, port = start_server(count_env)
        try:
            rows = []
            for path in GETS:
                code, h, body, ms = call(port, path)
                n = h.get("X-aigit-git-calls")
                if tag == "on" and n is None:
                    raise RuntimeError(
                        "门开了却没有 %s 头(端点 %s)" % ("X-aigit-git-calls", path))
                cmds = h.get("X-aigit-git-cmds") or ""
                rows.append({"path": path, "code": code,
                             "spawns": int(n) if n else 0,
                             "cmds": [c for c in cmds.split(",") if c],
                             "ms": ms, "body": body})
            recs[tag] = rows
        finally:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            err.close()

    text, n_ok = report(recs["on"], recs["off"])
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
    print(text)
    print("EXIT_OK" if n_ok == len(GETS) else "EXIT_MISMATCH(%d/%d)" % (n_ok, len(GETS)))


if __name__ == "__main__":
    main()
