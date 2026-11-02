# -*- coding: utf-8 -*-
"""aigit vs 真 git 的常驻差分判据（2026-10-28 录）。

## 这份套件是为什么存在的

这一轮把很多 git 语义搬进了 Python（refs/reflog/索引直读、常驻 cat-file），
而且 `gitguard` 会**屏蔽用户/系统配置**（hooksPath / filter / gpg / ssh …）。
屏蔽是安全需要，但**顺手把 `core.autocrlf` 也屏蔽了**，于是抓到一个真实缺陷：

    工作区是 CRLF、索引是 LF（plain git 在 `core.autocrlf=true` 下检出的正常状态）
    → plain git 说「干净」，aigit 说「已修改」，diff 里却空空如也。
    更严重的是：**通过 aigit 提交会把 CRLF 写进对象库**，与用户 git 的历史不一致。

§12.16 记了完整的事故与修法（只透传「影响结果、不能执行程序」的 core.* 键）。

## 判据两半

1. **配置透传**：同一份仓、同一时刻，aigit 与 plain git 的结论必须一致；
   并且要有**阴性对照**（autocrlf=false 时两边都必须报「已修改」），
   否则「永远报干净」也能让阳性用例全绿。
2. **读端点差分**：status / branches / log / diff / file 的结果与 plain git 逐项相等，
   且**快路径与回退路径都要相等**（回退用 `GIT_DIR` 强制文件直读失效）。

运行方式：`python -X utf8 test_vs_git.py`（仓根目录）。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent


def _g(repo, *a, env=None, binary=False):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding=None if binary else "utf-8", errors=None if binary else "replace",
                          env=env)


def _gmust(repo, *a, env=None):
    p = _g(repo, *a, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                  (p.stderr or p.stdout)[:200]))
    return p.stdout


def _rmtree(p):
    def onerr(func, path, exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass
    shutil.rmtree(str(p), onerror=onerr)


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _Server(object):
    def __init__(self, repo, extra_env=None):
        self.repo = Path(repo)
        port = _free_port()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "server.py",
             "--root", str(self.repo), "--port", str(port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=1).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise AssertionError("server 死了")
                time.sleep(0.25)
        raise AssertionError("server 起太慢")

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)

    def get(self, path):
        try:
            r = urllib.request.urlopen(self.base + path, timeout=60)
            return r.getcode(), json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, None


def _make_repo(root, name, global_cfg=None):
    """建一个「工作区 CRLF、索引 LF」的仓（依赖 plain git 的 autocrlf=true）。"""
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if global_cfg:
        env["GIT_CONFIG_GLOBAL"] = str(global_cfg)
    _gmust(d, "init", "-q", "-b", "main", env=env)
    _gmust(d, "config", "user.name", "bench", env=env)
    _gmust(d, "config", "user.email", "bench@localhost", env=env)
    (d / "a.txt").write_text("line1\nline2\n", encoding="utf-8", newline="\n")
    (d / "sub").mkdir(exist_ok=True)
    (d / "sub" / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
    _gmust(d, "add", "-A", env=env)
    _gmust(d, *ID, "commit", "-q", "-m", "c1", env=env)
    # 让 git 按 autocrlf 重新检出 → 工作区变 CRLF
    (d / "a.txt").unlink()
    _gmust(d, "checkout", "-f", "--", "a.txt", env=env)
    return d


class TestAutocrlfPassthrough(unittest.TestCase):
    """阳性：autocrlf=true → 两边都干净；阴性：autocrlf=false → 两边都「已修改」。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_vsgit_cfg_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    def _cfg(self, name, autocrlf):
        p = self.tmp / name
        p.write_text("[core]\n\tautocrlf = %s\n" % autocrlf,
                     encoding="utf-8", newline="\n")
        return p

    def test_crlf_worktree_is_clean_for_both(self):
        cfg = self._cfg("cfg_true", "true")
        d = _make_repo(self.tmp, "crlf", global_cfg=cfg)
        # 前置条件：工作区确实是 CRLF（否则这条用例什么也没测）
        self.assertIn(b"\r\n", (d / "a.txt").read_bytes(), "前置：工作区应是 CRLF")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg))
        git_status = _gmust(d, "status", "--porcelain", env=env).rstrip()
        self.assertEqual(git_status, "", "前置：plain git 认为干净")
        s = _Server(d, {"GIT_CONFIG_GLOBAL": str(cfg)})
        try:
            code, j = s.get("/api/status")
            self.assertEqual(code, 200)
            self.assertEqual([x["path"] for x in j["staged"]], [], "aigit 不该报暂存改动")
            self.assertEqual([x["path"] for x in j["unstaged"]],
                             [], "aigit 不该报 phantom 改动（autocrlf 被屏蔽的经典症状）")
        finally:
            s.close()

    def test_negative_control_autocrlf_false_reports_modified(self):
        """阴性对照：把 autocrlf 改成 false，两边都必须报「已修改」。

        没有这一条，「aigit 永远报干净」也能让上面那条全绿。
        """
        cfg = self._cfg("cfg_false", "false")
        # 先按 autocrlf=true 建出「CRLF 工作区」，再把配置改成 false —— 这样
        # 工作区 CRLF / 索引 LF 的差异才存在，阴性对照才真的在测东西。
        cfg_true = self._cfg("cfg_true_for_neg", "true")
        d = _make_repo(self.tmp, "crlf_false", global_cfg=cfg_true)
        self.assertIn(b"\r\n", (d / "a.txt").read_bytes(), "前置：工作区应是 CRLF")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg))
        # 注意不能 .strip(): porcelain 的第一列是**索引**状态, 前导空格是语义的一部分
        git_status = _gmust(d, "status", "--porcelain", env=env).rstrip()
        self.assertTrue(git_status.startswith(" M"), "前置：plain git 应报已修改，实际 %r" % git_status)
        s = _Server(d, {"GIT_CONFIG_GLOBAL": str(cfg)})
        try:
            code, j = s.get("/api/status")
            self.assertEqual(code, 200)
            self.assertEqual([x["path"] for x in j["unstaged"]], ["a.txt"],
                             "autocrlf=false 时 aigit 必须与 git 一样报已修改")
        finally:
            s.close()


class TestReadEndpointsVsGit(unittest.TestCase):
    """读端点差分：快路径与回退路径都要与 plain git 相等。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_vsgit_rd_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)
        d = cls.tmp / "repo"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        (d / "sub").mkdir()
        (d / "sub" / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        (d / "a.txt").write_text("a2\n", encoding="utf-8", newline="\n")
        (d / "sub" / "b.txt").write_text("b2\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "--", "sub/b.txt")
        (d / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
        _gmust(d, "branch", "feature")
        cls.repo = d

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    def _pass(self, mode):
        extra = {"GIT_DIR": str(self.repo / ".git")} if mode == "fallback" else {}
        env = dict(os.environ, **extra)
        s = _Server(self.repo, extra)
        try:
            code, st = s.get("/api/status")
            self.assertEqual(code, 200)
            # 期望值：用 v1 porcelain 独立算
            out = _gmust(self.repo, "status", "--porcelain", "-z", env=env)
            staged, unstaged = set(), set()
            parts = out.split("\0")
            i = 0
            while i < len(parts):
                e = parts[i]
                i += 1
                if not e:
                    continue
                x, y, rest = e[0], e[1], e[3:]
                if x == "?":
                    unstaged.add(rest)
                    continue
                if x == "!":
                    continue
                if x in ("R", "C"):
                    i += 1
                if x != " ":
                    staged.add(rest)
                if y != " ":
                    unstaged.add(rest)
            self.assertEqual({d["path"] for d in st["staged"]}, staged, mode)
            self.assertEqual({d["path"] for d in st["unstaged"]}, unstaged, mode)
            # branches
            code, br = s.get("/api/branches")
            self.assertEqual([b["name"] for b in br["branches"]],
                             [x for x in _gmust(self.repo, "for-each-ref", "--format=%(refname:short)",
                                                "refs/heads", env=env).splitlines() if x], mode)
            # log（sha 是 8 位契约）
            code, lg = s.get("/api/log?limit=5")
            want = []
            for line in _gmust(self.repo, "log", "-5", "--date=short",
                               "--pretty=format:%H\x1f%an\x1f%ad\x1f%s", env=env).splitlines():
                if line.strip():
                    p = line.split("\x1f")
                    want.append((p[0][:8], p[1], p[2], p[3]))
            self.assertEqual([(r["sha"], r["author"], r["date"], r["subject"]) for r in lg["log"]],
                             want, mode)
            # diff（工作区 & 暂存）
            for staged in (0, 1):
                code, df = s.get("/api/diff?file=a.txt&staged=%d" % staged)
                args = ["diff", "--no-color", "--no-ext-diff"] + (["--cached"] if staged else [])
                args += ["--", "a.txt"]
                self.assertEqual(df.get("diff"), _gmust(self.repo, *args, env=env), mode)
            # file：old 与 new
            code, fl = s.get("/api/file?file=a.txt")
            self.assertEqual(fl["old"], "a\n", mode)
            self.assertEqual(fl["new"], "a2\n", mode)
        finally:
            s.close()

    def test_fast_path_matches_git(self):
        self._pass("fast")

    def test_fallback_path_matches_git(self):
        self._pass("fallback")


if __name__ == "__main__":
    import io

    class _Reporter(unittest.TextTestResult):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._t0 = {}

        def startTest(self, test):
            self._t0[test.id()] = time.time()
            super().startTest(test)

        def _line(self, mark, test):
            dt = time.time() - self._t0.get(test.id(), time.time())
            print("  %s %-52s (%.1fs)" % (mark, test.id().split(".")[-1], dt))

        def addSuccess(self, test):
            super().addSuccess(test)
            self._line("  ok  ", test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._line(" FAIL ", test)

        def addError(self, test, err):
            super().addError(test, err)
            self._line("ERROR ", test)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._line(" skip ", test)

    _t0 = time.time()
    _suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    _res = unittest.TextTestRunner(resultclass=_Reporter, verbosity=0,
                                   stream=io.StringIO()).run(_suite)
    _total = _res.testsRun
    print("汇总: 共 %d 条   PASS %d   FAIL %d   SKIP %d   ERROR %d   用时 %.1fs"
          % (_total,
             _total - len(_res.failures) - len(_res.errors) - len(_res.skipped),
             len(_res.failures), len(_res.skipped), len(_res.errors),
             time.time() - _t0))
    sys.exit(0 if _res.wasSuccessful() else 1)
