# -*- coding: utf-8 -*-
"""blame 守卫的故障签名锚点（2026-10-21 录制，惰性化改造**之前**的旧形态）。

背景（doc/aigit-perf.md §12.4）：`/api/history/blame` 每次 spawn **2** 条 git，
其中第 1 条是 `_require_commits()` 守卫用的 rev-parse（Win 子进程地板价 ~40ms）。
P2 候选是把它惰性化（blame 失败时再补守卫），但空仓的错误 code 将随守卫后移
而变化 —— 对外错误形态变了就**不是提速而是改契约**。

因此本文件把改造前的签名逐字钉死。验收判据：**任何改动之后本文件必须原样全过**；
要改签名必须先写明原判据为何不成立，并同步更新本文件与 doc/aigit-perf.md §12.4。

签名来源（[实测]当轮录制）：
  * HTTP 层四例：`D:\\aigit_mut_tmp\\probe_signatures.py`
    -> `blame_guard_signatures.json`
  * 错误路径 spawn 序列 -> `blame_error_spawns.json`（进程内 wrapper 实测）

2026-10-21 惰性化后的新基线：错误路径的 spawn **顺序**变了（守卫从最前面挪到
失败后补跑），成功路径降为**单 spawn**（测试钉住，见 TestLazyGuardPerfContract）。

运行方式：`python -X utf8 -m unittest test_blame_guard_signatures -v`（仓根目录）。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import history_ops

_ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent

# 逐字签名：HTTP 状态码 + 完整 JSON 响应体（文案一字不能变）
SIG_HTTP = [
    ("A_empty", "/api/history/blame?file=README.md", 400,
     {"ok": False, "error": "仓库还没有任何提交,blame无从谈起",
      "code": "no_commits"}),
    ("B_nosuch", "/api/history/blame?file=ghost.txt", 400,
     {"ok": False, "error": "文件不存在,也没有历史:ghost.txt",
      "code": "no_such_file"}),
    ("C_binary", "/api/history/blame?file=b.bin", 400,
     {"ok": False, "error": "这是二进制文件,blame 没有意义。",
      "code": "binary_file"}),
    ("D_emptyfile", "/api/history/blame?file=e.txt", 400,
     {"ok": False, "error": "blame 没有返回任何行(文件可能是空的,或范围选错了)。",
      "code": "blame_empty", "total_lines": 0}),
]


def _force_rmtree(p: Path):
    """rmtree 会卡在 git 的**只读** pack 文件上(WinError 183 的真凶)。"""
    def onerr(func, path, exc):
        try:
            os.chmod(path, 0o666)
            func(path)
        except Exception:
            pass
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)],
                   capture_output=True)
    shutil.rmtree(p, ignore_errors=True, onerror=onerr)


def _fresh(base: Path, sub: str) -> Path:
    d = base / sub
    _force_rmtree(d)
    d.mkdir(parents=True)
    return d


def _g(repo: Path, *args):
    p = subprocess.run(["git"] + list(args), cwd=str(repo), check=False,
                       capture_output=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, "git %s rc=%d: %s" % (
        " ".join(args[:3]), p.returncode, (p.stderr or p.stdout).strip()[:200])


def _make_repo(kind: str, base: Path) -> Path:
    """四种前置状态：A 零提交；B/C/D 各一次提交。"""
    d = _fresh(base, kind)
    _g(d, "init", "-q")
    if kind == "A_empty":
        (d / "README.md").write_text("hello\n", encoding="utf-8")
    elif kind == "B_nosuch":
        (d / "a.txt").write_text("x\n", encoding="utf-8")
        _g(d, "add", "--", "a.txt")
        _g(d, *_ID, "commit", "-q", "-m", "init")
    elif kind == "C_binary":
        (d / "b.bin").write_bytes(bytes(range(256)) * 8 + b"\x00\x01")
        _g(d, "add", "--", "b.bin")
        _g(d, *_ID, "commit", "-q", "-m", "bin")
    elif kind == "D_emptyfile":
        (d / "e.txt").write_text("", encoding="utf-8")
        _g(d, "add", "--", "e.txt")
        _g(d, *_ID, "commit", "-q", "-m", "empty")
    else:
        raise AssertionError(kind)
    return d


def _start_server(port: int, repo: Path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.Popen([sys.executable, "-X", "utf8", "server.py",
                          "--root", str(repo), "--port", str(port)],
                         cwd=str(REPO_ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return p, base
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("server 死了(端口%d)" % port)
            time.sleep(0.25)
    raise RuntimeError("server 起太慢(端口%d)" % port)


def _hit(base: str, path: str):
    try:
        r = urllib.request.urlopen(base + path, timeout=20)
        return r.getcode(), json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestBlameGuardSignatures(unittest.TestCase):
    """惰性化改造前的旧形态锚点。改造后必须原样全过,除非写明判据变更理由。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / \
            ("aigit_sigtest_%d" % (os.getpid() % 100000))
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _force_rmtree(cls.tmp)

    def test_http_signatures(self):
        """四例故障的 HTTP 签名逐字（空仓/无文件/二进制/空文件）。"""
        for case, path, http, body in SIG_HTTP:
            with self.subTest(case=case):
                repo = _make_repo(case, self.tmp)
                server = None
                try:
                    port = None
                    for cand in range(8841, 8890):
                        server, base = _start_server(cand, repo)
                        port = cand
                        break
                    got_http, got_body = _hit(base, path)
                finally:
                    if server is not None:
                        server.terminate()
                        try:
                            server.wait(timeout=5)
                        except Exception:
                            server.kill()
                self.assertEqual(got_http, http,
                                 "HTTP 状态变了: %s != %s" % (got_http, http))
                self.assertEqual(
                    got_body, body,
                    "响应体逐字签名变了:\n  旧: %s\n  新: %s"
                    % (json.dumps(body, ensure_ascii=False),
                       json.dumps(got_body, ensure_ascii=False)))

    def test_error_path_spawns(self):
        """错误路径 spawn 基线（惰性化后此序列可能合法变化;变化时必须先写明
        旧序列为何不成立,再更新本判据）。"""
        # 2026-10-21 惰性化: 守卫从「最前面无条件跑」挪到「失败路径补跑」——
        # 错误路径的 spawn **顺序**因此变化(rev-parse 从第 1 位变最后一位),
        # 固有次数不变; HTTP 文案签名(test_http_signatures)才是契约。
        for case, file, want_code, want_spawns in [
            ("A_empty", "README.md", "no_commits", ["blame", "rev-parse"]),
            ("B_nosuch", "ghost.txt", "no_such_file", ["cat-file", "rev-parse"]),
        ]:
            with self.subTest(case=case):
                repo = _make_repo(case, self.tmp)
                spawns = []
                orig = history_ops.HistoryOps._run

                def counting(self_, args, *a, **kw):
                    name = next((x for x in args
                                 if not str(x).startswith("-")), "?")
                    try:
                        return orig(self_, args, *a, **kw)
                    finally:
                        spawns.append(name)

                history_ops.HistoryOps._run = counting
                try:
                    h = history_ops.HistoryOps(repo)
                    try:
                        h.blame(file, None, None, 500)
                        self.fail("应当抛错")
                    except history_ops.HistoryError as e:
                        self.assertEqual(getattr(e, "code", None), want_code,
                                         "code 变了")
                finally:
                    history_ops.HistoryOps._run = orig
                self.assertEqual(spawns, want_spawns,
                                 "错误路径 spawn 序列变了: %r != %r"
                                 % (spawns, want_spawns))


class TestLazyGuardPerfContract(unittest.TestCase):
    """惰性化的收益契约: 成功路径必须只剩 1 次 spawn(2026-10-21)。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / \
            ("aigit_sigtest2_%d" % (os.getpid() % 100000))
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _force_rmtree(cls.tmp)

    def _spawns(self, fn):
        spawns = []
        orig = history_ops.HistoryOps._run

        def counting(self_, args, *a, **kw):
            name = next((x for x in args if not str(x).startswith("-")), "?")
            try:
                return orig(self_, args, *a, **kw)
            finally:
                spawns.append(name)

        history_ops.HistoryOps._run = counting
        try:
            out = fn()
        finally:
            history_ops.HistoryOps._run = orig
        return spawns, out

    def test_blame_happy_path_single_spawn(self):
        repo = _make_repo("B_nosuch", self.tmp)      # 有 1 次提交 + a.txt
        h = history_ops.HistoryOps(repo)
        spawns, rows = self._spawns(lambda: h.blame("a.txt", None, None, 500))
        self.assertTrue(rows)
        self.assertEqual(spawns, ["blame"],
                         "成功路径应只剩 1 次 spawn: %r" % spawns)

    def test_reflog_lazy_semantics(self):
        # 2026-11-02 判据更新：原判据是「成功路径只剩 reflog 一次 spawn」（惰性守卫
        # 落地时录的，见 doc §12.7.2）。P4 常驻 cat-file 之后，reflog 的**业务调用
        # 本身**也不再 spawn —— 走 `.git/logs/HEAD` + commit 对象解析，成功路径是
        # **0 次**。所以这里的期望从 ["reflog"] 改成 []。
        #
        # 为什么这不是「改判据掩盖回归」：这条测试要守的是**守卫不许多余 spawn**；
        # 现在连业务 spawn 都没了，是更强的形态。错误路径的 HTTP 签名与 spawn 顺序
        # 由本文件其它用例继续钉住（那几条没有改）。
        repo = _make_repo("B_nosuch", self.tmp)
        h = history_ops.HistoryOps(repo)
        spawns, rows = self._spawns(lambda: h.list_reflog(30))
        self.assertTrue(isinstance(rows, list) and rows)
        self.assertEqual(spawns, [],
                         "P4 之后 reflog 成功路径应 0 spawn（原为 ['reflog']）: %r" % spawns)
        # 空仓: 仍返回 [] (与惰性化之前、以及 P4 之前的可观察行为一致), 也是 0 次 spawn
        empty = _make_repo("A_empty", self.tmp)
        h2 = history_ops.HistoryOps(empty)
        spawns2, rows2 = self._spawns(lambda: h2.list_reflog(30))
        self.assertEqual(rows2, [])
        self.assertEqual(spawns2, [])


if __name__ == "__main__":
    unittest.main()
