#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_aigit.py —— aigit P0：可复现性能基准台。

为什么有这个文件
================
aigit-dist.md §5 里那两个数（102.1 ms / 139.7 ms）的造数脚本已经丢了；
aigit_perf.py 文件头明写「我不是基准，一行计时代码都没有」。于是当前
状态是**没有任何可复现的基线**。本脚本只负责补这一件事：造标准
fixture、起 aigit 自带 server、按固定协议打点，产出两份文件：

  * doc/aigit-perf-baseline.json  机器可读（含环境信息 + 每个数字的完整统计）
  * doc/aigit-perf-baseline.md    人读（方法 + 对照表 + 怎么复跑）

方法（权威版写在 md 里，这里是速记）
====================================
1) fixture 三档规模（1k / 5k / 10k 个已跟踪文件），全部在**系统临时目录**
   里用 git 命令批量造，真实仓库一个字节不碰。每档外加 200 个 untracked
   和 1 个 10000 行大文件。造完 `git add -A && git commit`（单提交）。
2) 起服务仿 D:\\aigit_mut_tmp\\measure_blame.py 的路子：
       subprocess.Popen([sys.executable,'-X','utf8',server,'--root',<tmp>,
                         '--port',<port>])
   然后轮询 /api/health。**不给 --sandbox**：server.py main() 里
   `if a.sandbox and root.exists(): shutil.rmtree(root)` 会把 root 整个
   删掉重建 —— 我们要的是「用已造好的仓」，所以只传 --root。
3) 端点参数照 server.py 路由表写死（2026-10-21 实读，非猜）：
       GET  /api/status
       GET  /api/log?limit=50
       GET  /api/diff?file=<rel>                    （clean 文件 → 空 diff）
       GET  /api/diff?file=<big>
       GET  /api/file?file=<rel>                    （ref 默认 HEAD）
       GET  /api/file?file=<big>
       GET  /api/history/blame?file=<rel>&limit=500 （DEFAULT_BLAME_LINES=500）
       GET  /api/history/blame?file=<big>&limit=500
       GET  /api/history/file?file=<rel>&limit=50   （follow 默认 true）
       POST /api/stage  body={"files":[500 paths]}
       POST /api/stage  body={"files":[3000 paths]}   ← WinError 206 悬崖钉子：
           这批走 server.py `_run_paths` 的 --pathspec-from-file=-（stdin）
           通道，命令行长度与批数无关；它每次 HTTP 200 就说明修复仍被量着。
4) 每项：预热 1 次 + 重复 7 次，记墙钟（毫秒，read 全响应为止）。
   JSON 存 median/min/max + 每档 metadata；md 只放 median 表。
5) **尺子自证**：单独量 20 次「裸起 git rev-parse HEAD 子进程」（候选判据
   A5/E13 的 40 ms 地板，±50% 容差）。median 不在 [30,60]ms 区间 →
   JSON `calibrated:false`、md 挂红字「尺子未校准，本次基线不得用于对比」。

stage 批的语义（为什么可重复）
==============================
批量路径全部来自**已跟踪且干净**的文件：git add 进去是 no-op，样本之间
index/工作区状态零漂移，sampler 无需任何重置即自洽、可重复。3000 > 档
容量时（1k 档）用循环填充到恒 3000 条路径（JSON 里记
body/body_paths_unique），200 个 untracked 与大文件都不进批。

用法
====
    python bench_aigit.py --scales 1k,5k --out doc/aigit-perf-baseline.json
    python bench_aigit.py --scales 10k --out doc/aigit-perf-baseline.json   # 10k 造仓慢，允许单独跑

每次运行**合并写回**：重跑的 scale 覆盖该档结果，其他档保留；md 总是
按合并后的 JSON 整份重生成。控制台只打 ASCII 摘要一行，全量数字在文件里。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 常量 ----
REPO_ROOT = Path(__file__).resolve().parent
SERVER_PY = REPO_ROOT / "server.py"
DEFAULT_OUT = REPO_ROOT / "doc" / "aigit-perf-baseline.json"

SCALES = {  # scale -> (tracked, untracked, big_file_lines)
    "1k": (1000, 200, 10000),
    "5k": (5000, 200, 10000),
    "10k": (10000, 200, 10000),
}
SCALE_ORDER = ["1k", "5k", "10k"]

WARMUP = 1                    # 每项预热次数（不计时）
REPEATS = 7                   # 每项计时样本数（取 median / min / max）
RULER_SAMPLES = 20            # 尺子自证样本数（额外 1 次热身丢弃）
RULER_RANGE_MS = (30.0, 60.0)  # 依据: Windows 单次 git 子进程 ≈40ms 地板
STAGE_BATCHES = (500, 3000)
SAMPLE_FILE = "d000/f000000.txt"       # 电池用的小样本文件
BIG_FILE = "big_10000_lines.txt"
LINES_PER_FILE = 10                    # 小文件行数
HTTP_TIMEOUT = 120                     # 秒, 与 server 内部 git timeout 对齐
MASTER_BRANCH = "main"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def log(msg: str) -> None:
    """控制台只走 ASCII —— cp936 会把中文打乱；判断只看文件与退出码。"""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def fatal(msg: str) -> None:
    log("[bench_aigit] FATAL: " + msg)
    sys.exit(2)


def write_text_lf(path: Path, text: str) -> None:
    """UTF-8 无 BOM + LF，明文保证（管它哪个平台）。"""
    text = text.replace("\r\n", "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def git_run(args, cwd, check=False, timeout=600):
    """临时 fixture 里的裸 git 调用（与 server 的 gitguard 无关）。"""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=check, timeout=timeout)


def git_out(args, cwd) -> str:
    p = git_run(args, cwd)
    if p.returncode != 0:
        raise RuntimeError(
            "git %s 失败 rc=%d: %s"
            % (" ".join(args), p.returncode, (p.stderr or p.stdout)[:300]))
    return p.stdout


# ----------------------------------------------------------- HTTP 客户端 ----
def http_raw(port: int, method: str, path: str, body: bytes | None = None,
             timeout: float = HTTP_TIMEOUT):
    """发一发拉一次。返回 (墙钟ms, http status, raw bytes)。计时含发送与读完。"""
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st = getattr(r, "status", None) or r.getcode()
            raw = r.read()
    except urllib.error.HTTPError as e:
        st = e.code
        raw = e.read()
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return dt_ms, st, raw


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------- fixture ----
def _file_text(rel: str, seed: int, n_lines: int) -> str:
    return "".join(
        "%s line=%02d seed=%06d aigit-bench-fixture\n" % (rel, k, seed)
        for k in range(1, n_lines + 1))


def build_fixture(root: Path, tracked: int, untracked: int,
                  big_lines: int) -> dict:
    """在 root 造一档 fixture：tracked 个已跟踪文件 + 1 大文件 + commit，
    然后 200 个 untracked（在 commit 之后才写，保持 untracked 状态）。

    返回 {"tracked_list": [...排序后...], "head": sha, "build_seconds": x}
    """
    t0 = time.time()
    root.mkdir(parents=True, exist_ok=True)

    # 1) 已跟踪文件 + 大文件（统一 10 行文本itches，非空，内容随文件名异样化
    #    —— 防 git blob 内容去重把 add 变廉价假象）
    names = []
    for i in range(tracked):
        rel = "d%03d/f%06d.txt" % (i % 10, i)
        write_text_lf(root / rel, _file_text(rel, i, LINES_PER_FILE))
        names.append(rel)
    write_text_lf(root / BIG_FILE,
                  _file_text(BIG_FILE, 999999, big_lines))
    names.append(BIG_FILE)

    # 2) git 造（比照 server.py Repo.init 的关键 config）
    git_out(["init", "-b", MASTER_BRANCH], root)
    git_out(["config", "user.email", "bench@localhost"], root)
    git_out(["config", "user.name", "aigit-bench"], root)
    git_out(["config", "core.autocrlf", "false"], root)
    git_out(["config", "commit.gpgsign", "false"], root)
    git_out(["add", "-A"], root)
    git_out(["commit", "-m", "fixture: %d tracked files" % tracked], root)
    head = git_out(["rev-parse", "HEAD"], root).strip()

    # 3) untracked 文件（commit 之后才落盘，保持 untracked 状态）
    for j in range(untracked):
        rel = "untracked_%03d.txt" % j
        write_text_lf(root / rel, _file_text(rel, 500000 + j, LINES_PER_FILE))

    # 4) 自证：状态应恰好 = untracked 个 `??` 行, 无其他
    out = git_out(["status", "--porcelain", "-uall"], root)
    rows = [ln for ln in out.splitlines() if ln.strip()]
    bad = [ln for ln in rows if not ln.startswith("?? ")]
    if bad or len(rows) != untracked:
        raise RuntimeError(
            "fixture 自证失败: 期待 %d 行全为 '??', 实际 %d 行, 非法 %d 行 "
            "如 %s" % (untracked, len(rows), len(bad), bad[:3]))

    ls = sorted(git_out(["ls-files"], root).splitlines())
    if len(ls) != tracked + 1:  # +1 = big file
        raise RuntimeError("ls-files 数量不对: %d != %d+1"
                           % (len(ls), tracked + 1))
    build_seconds = round(time.time() - t0, 2)
    return {"tracked_list": ls, "head": head, "build_seconds": build_seconds}


def stage_batches(tracked_list, big_file: str):
    """500 / 3000 路径批量：排序后取前 n；不够则循环填充（恒 n 条）。

    批**不含** big 文件与 untracked —— 500/3000 全是已跟踪干净文件，
    stage 走 server 侧 `git add` 是 no-op，样本间 index 状态零漂移。
    """
    usable = [p for p in tracked_list if p != big_file]
    batches = {}
    for n in STAGE_BATCHES:
        if len(usable) == 0:
            raise RuntimeError("没有可用的 tracked 路径可以组批")
        if len(usable) >= n:
            batches[n] = list(usable[:n])
        else:
            extra = [usable[i % len(usable)] for i in range(n - len(usable))]
            batches[n] = list(usable) + extra
    return {n: {"paths": v, "unique": len(set(v))} for n, v in batches.items()}


def build_specs(scale_batches):
    """端点电池（参数照 server.py 路由表，URL 参数手工 quote）。

    每项是 (label, method, path_q, body_bytes)。label 就是 JSON 里的 endpoint。
    每档两个文件位：SAMPLE_FILE（10 行小文件）与 BIG_FILE（10000 行）。
    """
    quote = urllib.parse.quote
    # 标签(endpoint)用可读的原始路径; 实际 URL 全量 quote(%2F), parse_qs 会还原
    specs = [
        ("/api/status", "GET", "/api/status", None),
        ("/api/log?limit=50", "GET", "/api/log?limit=50", None),
        ("/api/diff?file=%s" % SAMPLE_FILE, "GET",
         "/api/diff?file=%s" % quote(SAMPLE_FILE, safe=""), None),
        ("/api/diff?file=%s" % BIG_FILE, "GET",
         "/api/diff?file=%s" % quote(BIG_FILE, safe=""), None),
        ("/api/file?file=%s" % SAMPLE_FILE, "GET",
         "/api/file?file=%s" % quote(SAMPLE_FILE, safe=""), None),
        ("/api/file?file=%s" % BIG_FILE, "GET",
         "/api/file?file=%s" % quote(BIG_FILE, safe=""), None),
        ("/api/history/blame?file=%s&limit=500" % SAMPLE_FILE, "GET",
         "/api/history/blame?file=%s&limit=500" % quote(SAMPLE_FILE, safe=""),
         None),
        ("/api/history/blame?file=%s&limit=500" % BIG_FILE, "GET",
         "/api/history/blame?file=%s&limit=500" % quote(BIG_FILE, safe=""),
         None),
        ("/api/history/file?file=%s&limit=50" % SAMPLE_FILE, "GET",
         "/api/history/file?file=%s&limit=50" % quote(SAMPLE_FILE, safe=""),
         None),
    ]
    for n in STAGE_BATCHES:
        body = json.dumps({"files": scale_batches[n]["paths"]},
                          ensure_ascii=False).encode("utf-8")
        specs.append(("/api/stage (batch=%d)" % n, "POST", "/api/stage", body))
    return specs


# ----------------------------------------------------------- server 生命周期 ----
def server_stderr_open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "wb")


def read_stderr_tail(path: Path, n=800) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data[-n:].decode("utf-8", "replace")
    except Exception:
        return "(读不到 stderr)"


def start_server(root: Path, stderr_path: Path, scale_label: str):
    """仿 measure_blame.py 的起服务，但**不带 --sandbox**（原因见文件头）。"""
    last_err = ""
    for attempt in range(3):
        port = free_port()
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        with open(stderr_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(SERVER_PY),
                 "--root", str(root), "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=err, env=env,
                cwd=str(REPO_ROOT))
            deadline = time.time() + 45
            while time.time() < deadline:
                if proc.poll() is not None:
                    last_err = read_stderr_tail(stderr_path)
                    break
                try:
                    dt, st, raw = http_raw(port, "GET", "/api/health",
                                           timeout=5)
                except OSError:
                    time.sleep(0.15)
                    continue
                if st == 200:
                    try:
                        obj = json.loads(raw)
                        if obj.get("ok") and obj.get("isRepo"):
                            return proc, port
                    except Exception:
                        pass
                time.sleep(0.1)
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log("[bench_aigit] 起服务尝试 %d 失败: %s"
            % (attempt + 1, last_err.strip().splitlines()[-1:]
               or ["(no stderr)"]))
    raise RuntimeError("server 起 3 次都不行: %s" % last_err[:400])


def stop_server(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
    except Exception:
        pass


# --------------------------------------------------------------- 尺子自证 ----
def calibrate_ruler(cwd: Path) -> dict:
    """量一条「裸起 git rev-parse HEAD」子进程的墙钟，样本 %d 次取 median。

    经验地板：Windows 每次 git 子进程 ≈ 40 ms（A5/E13 实测，闭合过 status 账，
    ±2.5 ms）。40 ±12.5ms 的容差就是这个区间 [%.0f, %.0f]。median 出区间
    就把 `calibrated:false` 落到 JSON 并在 md 挂红字。
    """
    git_out(["rev-parse", "HEAD"], cwd)  # 1 次热身（丢弃, 磁盘缓存对齐）
    xs = []
    for _ in range(RULER_SAMPLES):
        t0 = time.perf_counter()
        p = git_run(["rev-parse", "HEAD"], cwd)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if p.returncode != 0:
            raise RuntimeError("尺子 git rev-parse HEAD 失败: %s"
                               % (p.stderr or p.stdout)[:300])
        xs.append(dt_ms)
    med = statistics.median(xs)
    lo, hi = RULER_RANGE_MS
    return {
        "name": "bare git subprocess spawn, `git rev-parse HEAD`",
        "samples": RULER_SAMPLES,
        "median_ms": round(med, 2),
        "min_ms": round(min(xs), 2),
        "max_ms": round(max(xs), 2),
        "expected_range_ms": [lo, hi],
        "calibrated": bool(lo <= med <= hi),
        "note": "Windows 上每次 git 子进程 ≈40ms 地板(A5/E13 闭合过 status "
                "账,误差 ±2.5ms)。中位数不在 [30,60] 区间一律视为未校准。",
    }


# ------------------------------------------------------------ 电池计时器 ----
def run_row(scale: str, spec, port: int, warmup: int, repeats: int) -> dict:
    label, method, path, body = spec
    row = {
        "scale": scale,
        "endpoint": label,
        "method": method,
        "ok": True,
        "error": None,
        "http_status": None,
        "payload_bytes": None,
        "median_ms": None,
        "min_ms": None,
        "max_ms": None,
        "samples": repeats,
        "warmup": warmup,
    }
    if body is not None:
        row["body_paths"] = len(json.loads(body.decode("utf-8"))["files"])
        row["body_paths_unique"] = len(set(json.loads(
            body.decode("utf-8"))["files"]))
        row["body_bytes"] = len(body)
    try:
        # 预热（兼做正确性验证，ok 键必须真）
        dt, st, raw = http_raw(port, method, path, body)
        row["http_status"] = st
        obj = None
        if st == 200:
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                obj = None
        if st != 200 or not isinstance(obj, dict) or obj.get("ok") is not True:
            row["ok"] = False
            row["error"] = "预热就败: HTTP %s body=%s" % (
                st, raw[:200].decode("utf-8", "replace"))
            return row
        row["payload_bytes"] = len(raw)
        xs = []
        for i in range(repeats):
            dt, st, raw = http_raw(port, method, path, body)
            if st != 200:
                row["ok"] = False
                row["http_status"] = st
                row["error"] = "第 %d 个样本 HTTP %s" % (i + 1, st)
                return row
            xs.append(dt)
        row["median_ms"] = round(statistics.median(xs), 2)
        row["min_ms"] = round(min(xs), 2)
        row["max_ms"] = round(max(xs), 2)
        return row
    except Exception as e:
        row["ok"] = False
        row["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        return row


# ------------------------------------------------------------ md 渲染器 ----
def md_ruler_line(baseline: dict) -> str:
    cal = baseline.get("calibration", {})
    if not cal or not cal.get("samples"):
        return "-（本轮没有量尺数据）"
    calib = baseline.get("calibrated", False)
    ok_phrase = "**已校准，可对比**" if calib else \
        "**尺子未校准，本次基线不得用于对比**"
    note = "" if calib else (
        " 排查建议：后台全量测试、杀毒实时扫描或热驱动盘正在加载，都拉高子进程"
        "挡板；等空闲时段重跑一次拿到校准的基线。")
    return ("- 量法：单独 20 次「裸起新 git rev-parse HEAD 子进程」的墙钟，取 median（"
            "另 1 次热身丢弃，临时 fixture 仓里跑）。\n"
            "- 本轮：median=%s ms（min %s / max %s，n=%s）→ 落在 [%s, %s] ms 区间 → %s%s\n"
            "- 判据出处：Windows 上每次 git 子进程 ≈40 ms 地板（A5/E13 实测，"
            "闭合 status 账过 ±2.5 ms）；此区间即它 ±50%% 的容差。"
            % (cal.get("median_ms"), cal.get("min_ms"), cal.get("max_ms"),
               cal.get("samples"), cal.get("expected_range_ms", ["?"])[0],
               cal.get("expected_range_ms", ["?"])[1], ok_phrase, note))


def render_md(baseline: dict, out_json_path: Path) -> str:
    scales = baseline.get("scales", {})
    cols = [s for s in SCALE_ORDER if s in scales]
    if not cols:
        cols = sorted(scales.keys())
    lines = []
    lines.append("# aigit 性能基线（P0 可复现基准台）\n")
    lines.append("> 本文件由 `bench_aigit.py` **整份重写**（手工改会在下一次"
                 "运行时被覆盖）；机器读的完整统计在同文件目录" +
                 out_json_path.with_suffix(".json").name + " 里。")
    lines.append(">")
    lines.append("> 生成本份的运行时间: %s" % baseline.get("created"))

    lines.append("\n## 尺子自证\n")
    lines.append(md_ruler_line(baseline))

    lines.append("\n## 环境\n")
    e = baseline.get("environment", {})
    lines.append("- git: `%s`" % e.get("git_version", "?"))
    lines.append("- python: `%s`（%s）" % (e.get("python", "?"),
                                           e.get("python_executable", "?")))
    lines.append("- cpu: %s（%s 核）" % (
        e.get("cpu", "?"), e.get("cpu_cores", "?")))
    lines.append("- os: %s" % e.get("os", "?"))

    lines.append("\n## 方法\n")
    lines.append("1. **fixture**（系统临时目录，真实仓库不碰）：每档造 %s 个"
                 "已跟踪文件（每个 %d 行文本）、200 个 untracked、1 个 10000 行"
                 "大文件；`git add -A && git commit` 单提交，随后挂 untracked"
                 "（自证列恰 200 行 `??`）。"
                 % ("1k / 5k / 10k", LINES_PER_FILE))
    lines.append("2. 起服务：仿 `measure_blame.py` —— "
                 "`subprocess.Popen([python,'-X','utf8',server.py,'--root',<tmp>,"
                 "'--port',<随机>])`，轮询 `/api/health` 到 `isRepo:true`。"
                 "**不传 `--sandbox`**：那会 `rmtree(root)` 重建（server.py:2178），"
                 "与我们预造仓的意图相反。")
    lines.append("3. 端点对每档规模跑：`/api/status`、`/api/log?limit=50`、"
                 "`/api/diff`、`/api/file`、`/api/history/blame?limit=500`、"
                 "`/api/history/file?limit=50`（GET），加 "
                 "`POST /api/stage` 两档批量（500 / 3000 路径，JSON `files` 数组）。"
                 "sampled 小文件位均为 10 行干净文件位或大文件位变体（~10 行位、"
                 "10000 行位各一）。")
    lines.append("4. **协议**：每项预热 1 次 + 重复 7 次，墙钟"
                 "（perf_counter，含 HTTP 请求发送 + 全响应读取），单位 ms；"
                 "min / max 见 JSON，md 表只展示 median。")
    lines.append("5. **stage 批语义**：批量路径全部来自已跟踪且干净的 fixture "
                 "文件（不含大文件、不含 untracked）—— server 侧 "
                 "`git add --pathspec-from-file=-` 进去是 no-op，样本之间 "
                 "index/工作区状态零漂移，sampler 无需任何重置即自洽、可重复；"
                 "3000 > 档容量时（1k 档）用循环填充到恒 3000 条路径"
                 "（JSON 里 `body_paths_unique` 记录 unique 数）。")
    lines.append("6. **温度**：单机发热条件（timeout=120 s、quiet server、无并发"
                 "客户端），单机跑。")
    lines.append("7. **WinError 206 悬崖**：每次 `stage3000` 批都走 server 的 "
                 "stdin pathspec 通道（`--pathspec-from-file=-`，server.py "
                 "`_run_paths`/E8 修复）；它每次 HTTP 200 即说明 32K 命令行"
                 "悬崖的修复仍在生效 —— 这颗钉子由本基准长期量着"
                 "（JSON `results[].http_status`）。")

    lines.append("\n## 三档规模——中位数（ms）\n")
    head = "| # | endpoint | method | %s |\n" % " | ".join(cols)
    lines.append(head)
    lines.append("|--:|---|---|%s\n" % ("---|" * len(cols)))

    # 以第一档的行序为基准
    ref = scales[cols[0]]["results"] if cols else []
    seen = set()
    for idx, rrow in enumerate(ref, 1):
        ep = rrow["endpoint"]
        if ep in seen:
            continue
        seen.add(ep)
        by_scale = {}
        for s in cols:
            for x in scales[s]["results"]:
                if x["endpoint"] == ep:
                    by_scale[s] = x
                    break
        cells = []
        for s in cols:
            rrow_s = by_scale.get(s)
            if rrow_s is None or rrow_s.get("ok") is not True:
                cells.append("—")
            else:
                cells.append("%.1f" % rrow_s["median_ms"])
        lines.append("| %d | `%s` | %s | %s |\n"
                     % (idx, ep, rrow.get("method", "?"), " | ".join(cells)))
    lines.append("\n")
    prov = "、".join(
        "%s=%s" % (k, scales[k].get("measured_at", "?"))
        for k in cols if isinstance(scales.get(k), dict))
    lines.append("> 各档数据实测时间戳：%s。每档数据来自它测的那一刻的"
                 "尺子校准（JSON `runs[]` 里有每轮的尺子中位数）。"
                 % (prov or "—"))
    lines.append("> median：7 样本中位数（先丢弃 1 预热）。min/max 及样本明细见"
                 " JSON 里 `results[]` 的 `min_ms / max_ms / samples`；"
                 "`payload_bytes / body_paths / body_bytes` 也在同处，大文件"
                 "位（~500KB 响应体）别看错了它与其他行的结论。")

    lines.append("\n## 有什么会失败 / 失败怎么算\n")
    lines.append("- 任何 endpoint 非 HTTP 200 或响应缺 `ok:true` 记一行"
                 "`ok:false`，进 JSON `failures[]`，退出码 2；其他该档依然照测。")
    lines.append("- 尺子不在 [30,60] ms → `calibrated:false` + md 红字，"
                 "**但不挡电池继续跑**（数字仍然落盘，只是不能对比）。")
    lines.append("- server 起不来 3 次、fixture 构错 → 那一档直接记失败跳过，"
                 "其他档照跑。")

    lines.append("\n## 怎么复跑\n")
    lines.append("```powershell")
    lines.append("# Windows 控制台 cp936，先设环境变量再跑（否则中文乱码 —— 判断只靠文件）:")
    lines.append("$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'")
    lines.append("python bench_aigit.py --scales 1k,5k --out doc/aigit-perf-baseline.json")
    lines.append("python bench_aigit.py --scales 10k --out doc/aigit-perf-baseline.json   # 10k 造仓慢，单独跑")
    lines.append("```")
    lines.append("- 参数: `--scales` 取 1k/5k/10k 之一或多个；`--out` JSON 路径"
                 "（md 默认同名换 `.md` 后缀）；`--repeats` / `--warmup` / `--keep`"
                 "（中可查） 另有可调项。")
    lines.append("- 跑完后脚本打印一行 ASCII 汇总；`failures=N` 非 0 时退出码 2，"
                 "但 md/JSON 照常落盘（问题数字会被标出来）。")
    lines.append("- 结果合并规则：**重跑的 scale 覆盖该档，其他档保留**；"
                 "mulitiple runs 会写进 JSON `runs[]` 追加历史。")
    lines.append("- **对比纪律**：旧基线与新基线对比前提是双方尺子都在 [30,60]ms"
                 "区间内且环境同 git/python/机器。机器换了、git 换大版本 → 只拿"
                 "同环境前后对比，否则别下结论。")

    f = baseline.get("failures", [])
    lines.append("\n## 本轮失败项\n")
    if not f:
        lines.append("（本轮 0 个失败。）")
    else:
        for x in f:
            lines.append("- `%s` %s: %s"
                         % (x.get("scale", "?"), x.get("endpoint", ""),
                            (x.get("error") or "")[:200]))
    lines.append("\n## package 约束\n")
    lines.append("- 本基准只**读**真实仓库的 `server.py` 作为被测件，"
                 "真实仓库一个字节不碰（fixture 在临时目录）。")
    lines.append("- 结论纪律：不要抄道——'status ~ 43ms' 这类细节只在本文件"
                 "与小 JSON 里，配合 git_version/python/cpu 才能比。")
    return "".join(ln + "\n" if not ln.endswith("\n") else ln for ln in lines)


# ---------------------------------------------------------------- 合并 ----
def merge_baseline(out_json: Path, new_meta: dict,
                   new_scales: dict, failures: list, ruler: dict) -> dict:
    """读旧 JSON（有就记成 runs[]），覆盖本轮 scale，重生成全量。"""
    old = None
    if out_json.exists():
        try:
            old = json.loads(out_json.read_text(encoding="utf-8"))
        except Exception as e:
            bak = out_json.with_suffix(".corrupt-%d.json" % time.time())
            try:
                out_json.rename(bak)
                log("[bench_aigit] 已有 JSON 解析失败(%s)，已存成 %s 重来"
                    % (e, bak.name))
            except Exception:
                old = None
    base = old if isinstance(old, dict) else {}
    ts = new_meta["created"]

    env = new_meta["environment"]
    ruler_new = dict(ruler)
    calibrated_new = ruler_new.get("calibrated", False)

    # 合并 scales 元数据 + 行（本轮覆盖，未跑档保留）
    scales_old = base.get("scales", {}) or {}
    merged_scales = {}
    for k in SCALE_ORDER:
        if k in new_scales:
            merged_scales[k] = new_scales[k]
        elif k in scales_old:
            merged_scales[k] = scales_old[k]
    for k, v in scales_old.items():
        if k not in merged_scales and k not in SCALE_ORDER:
            merged_scales[k] = v
    results = []
    for k in SCALE_ORDER:
        if k in merged_scales:
            results.extend(merged_scales[k]["results"])
    for k, v in merged_scales.items():
        if k not in SCALE_ORDER and isinstance(v, dict):
            results.extend(v.get("results", []))

    runs = (base.get("runs") or [])
    runs.append({
        "ts": ts,
        "scales_run": list(new_scales.keys()),
        "ruler_median_ms": ruler_new.get("median_ms"),
        "calibrated": calibrated_new,
        "environment": env,
        "failures": len(failures),
    })
    if len(runs) > 20:
        runs = runs[-20:]

    base.update({
        "tool": "bench_aigit.py",
        "schema": "results[] 扁平: 每项带 scale/endpoint/median_ms/samples；"
                  "min_ms/max_ms/payload_bytes 等在同行。scales[] 是每档元数据。"
                  "md 由脚本从 JSON 整份重生成。",
        "created": ts,
        "calibrated": calibrated_new,
        "calibration": ruler_new,
        "environment": env,
        "protocol": {
            "warmup": WARMUP,
            "repeat_samples": REPEATS,
            "hammer": "墙钟 time.perf_counter()，毫秒，含 HTTP 发送 + 读全响应",
            "ruler_samples": RULER_SAMPLES,
            "fixture": {
                "tracked_at_scale": {k: v["tracked_files"]
                                     for k, v in merged_scales.items()
                                     if isinstance(v, dict)},
                "untracked_files": UNTRACKED,
                "big_file_lines": BIG_LINES,
                "lines_per_file": LINES_PER_FILE,
                "big_file": BIG_FILE,
            },
        },
        "scales": merged_scales,
        "results": results,
        "runs": runs,
        "failures": failures,
    })
    return base


UNTRACKED = 200
BIG_LINES = 10000


def main() -> int:
    ap = argparse.ArgumentParser(
        description="aigit P0: 可复现性能基准台（纯 stdlib, 不碰真实仓库）")
    ap.add_argument("--scales", default="1k,5k,10k",
                    help="逗号分隔, 其一: 1k,5k,10k（默认全跑；"
                         "10k 造仓慢可单独跑）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="JSON 路径（默认 doc/aigit-perf-baseline.json）")
    ap.add_argument("--md", default=None,
                    help="md 路径（默认与 --out 同名改 .md 后缀）")
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    ap.add_argument("--keep", action="store_true",
                    help="保留 fixture 临时目录（排查用, 默认删）")
    a = ap.parse_args()

    scales = [x.strip() for x in a.scales.split(",") if x.strip()]
    unknown = [x for x in scales if x not in SCALES]
    if unknown or not scales:
        fatal("--scales 不合法: %r（可用: %s）" % (scales, SCALE_ORDER))
    repeats = max(1, a.repeats)
    warmup = max(0, a.warmup)

    out_json = Path(a.out).expanduser().resolve()
    out_md = (Path(a.md).expanduser().resolve() if a.md
              else out_json.with_suffix(".md"))

    # 环境自报
    try:
        git_version = git_out(["--version"], cwd=REPO_ROOT).strip()
    except Exception as e:
        fatal("git 不可用: %s" % e)
    env_info = {
        "git_version": git_version,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "cpu": platform.processor() or "?",
        "cpu_cores": os.cpu_count(),
        "os": platform.platform(),
        "console_stdout_encoding": getattr(sys.stdout, "encoding", "?"),
    }
    log("[bench_aigit] git=%s python=%s" % (git_version, sys.version.split()[0]))

    tmpbase = Path(tempfile.mkdtemp(prefix="aigit_bench_"))
    ruler = None
    scale_meta = {}
    failures = []
    wall0 = time.time()
    try:
        for scale in scales:
            tracked_n, untracked_n, big_n = SCALES[scale]
            root = tmpbase / ("repo_" + scale)
            log("[bench_aigit] scale=%s %s building fixture..." % (scale, tracked_n))
            try:
                fix = build_fixture(root, tracked_n, untracked_n, big_n)
            except Exception as e:
                failures.append({"scope": "fixture", "scale": scale,
                                 "error": str(e)[:500]})
                log("[bench_aigit] scale=%s fixture FAILED: %s" % (scale, e))
                continue

            if ruler is None:
                try:
                    ruler = calibrate_ruler(root)
                    ruler["fixture_repo"] = scale
                    log("[bench_aigit] ruler=%sms calibrated=%s"
                        % (ruler["median_ms"], ruler["calibrated"]))
                except Exception as e:
                    fatal("尺子 calibration 也失败: %s" % e)

            stderr_path = tmpbase / ("server_%s.err.log" % scale)
            try:
                proc, port = start_server(root, stderr_path, scale)
            except Exception as e:
                failures.append({"scope": "server", "scale": scale,
                                 "error": str(e)[:500]})
                log("[bench_aigit] scale=%s server start FAILED" % scale)
                continue

            try:
                batches = stage_batches(fix["tracked_list"], BIG_FILE)
                specs = build_specs(batches)
                rows = []
                for idx, spec in enumerate(specs, 1):
                    log("[bench_aigit] scale=%s (%d/%d) %s"
                        % (scale, idx, len(specs), spec[0]))
                    row = run_row(scale, spec, port, warmup, repeats)
                    rows.append(row)
                    if not row["ok"]:
                        failures.append({"scope": "endpoint", "scale": scale,
                                         "endpoint": row["endpoint"],
                                         "error": row["error"],
                                         "http_status": row["http_status"]})
            finally:
                stop_server(proc)

            # 状态漂移自证：跑完电池后 status 应仍恰 200 个 untracked
            post = git_run(["status", "--porcelain", "-uall"], root)
            post_rows = [ln for ln in post.stdout.splitlines() if ln.strip()]
            if post.returncode != 0 or \
                    len(post_rows) != untracked_n or \
                    any(not ln.startswith("?? ") for ln in post_rows):
                failures.append({"scope": "drift", "scale": scale,
                                 "error": "电池跑完 fixture 状态与开局不同: "
                                          "%d 行 %s"
                                          % (len(post_rows), post_rows[:3])})

            scale_meta[scale] = {
                "measured_at": now_iso(),
                "tracked_files": tracked_n,
                "untracked_files": untracked_n,
                "big_file_lines": big_n,
                "lines_per_file": LINES_PER_FILE,
                "big_file": BIG_FILE,
                "git_head": fix["head"].strip()[:12],
                "fixture_build_seconds": fix["build_seconds"],
                "results": rows,
            }
            ok_n = sum(1 for r in rows if r["ok"])
            log("[bench_aigit] scale=%s battery done ok=%d/%d"
                % (scale, ok_n, len(rows)))
    finally:
        if a.keep:
            log("[bench_aigit] tmp kept: %s" % tmpbase)
        else:
            shutil.rmtree(tmpbase, ignore_errors=True)

    baseline = merge_baseline(
        out_json,
        new_meta={"created": now_iso(), "environment": env_info},
        new_scales=scale_meta, failures=failures,
        ruler=ruler or {"name": "未量", "samples": 0, "calibrated": False})
    write_text_lf(out_json, json.dumps(baseline, ensure_ascii=False, indent=2))
    md = render_md(baseline, out_json)
    write_text_lf(out_md, md)

    wall_total = round(time.time() - wall0, 1)
    nfail = len(failures)
    status_part = " ".join(
        "%s=%sms" % (k, med_of(scale_meta, k, "/api/status"))
        for k in SCALE_ORDER if k in scale_meta)
    stage500_part = " ".join(
        "%s=%sms" % (k, med_of(scale_meta, k, "/api/stage (batch=500)"))
        for k in SCALE_ORDER if k in scale_meta)
    stage3000_part = " ".join(
        "%s=%sms" % (k, med_of(scale_meta, k, "/api/stage (batch=3000)"))
        for k in SCALE_ORDER if k in scale_meta)
    cal_txt = "YES" if baseline["calibrated"] else "NO"
    log("[BENCH] %s scales=%s ruler=%sms calibrated=%s | status_med_ms %s | "
        "stage500 %s | stage3000 %s | failures=%d | json=%s | md=%s | wall=%ss"
        % ("OK" if not nfail else "HAS-FAILURES", ",".join(scales),
           baseline["calibration"].get("median_ms"), cal_txt,
           status_part, stage500_part, stage3000_part,
           nfail, str(out_json), str(out_md), wall_total))
    return 2 if nfail else 0


def med_of(scale_meta: dict, scale: str, endpoint: str):
    try:
        for r in scale_meta[scale]["results"]:
            if r["endpoint"] == endpoint:
                return r["median_ms"]
    except Exception:
        pass
    return "MISSING"


if __name__ == "__main__":
    code = main()
    sys.exit(code)
