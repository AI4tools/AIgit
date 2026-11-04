# -*- coding: utf-8 -*-
"""写端点的输入边界（2026-10-27 巡检产出）。

这份套件只钉**输入校验**，不测业务语义。它的存在理由是一条实测出来的事故：

    `POST /api/unstage {"files": ["a.txt\\0.png"]}` → 200，实际取消了 `a.txt` 的暂存；
    `POST /api/discard {"files": ["a.txt\\0.png"], "confirmed": true}` → 200，
    实际**丢弃了 `a.txt` 的工作区改动**。

根因：Windows 上 `Path("a.txt\0.png").resolve()` **静默截断成 `a.txt`**（不抛异常），
于是「用户请求的文件」在解析后换了人；而 `_run_paths` 用 NUL 分隔的 pathspec 文件，
`a.txt\0.png` 会被拆成**两个** pathspec。两条叠加起来就是路径走私 + 数据丢失。

运行方式：`python -X utf8 test_write_inputs.py`（仓根目录）。
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

import server

ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent
BAD_TEXT = ("Traceback", "TypeError", "ValueError", "KeyError", "AttributeError",
            "IndexError", "invalid literal", "object of type", "NoneType",
            "positional argument")
NUL = "\x00"


def _g(repo, *a):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace")


def _gmust(repo, *a):
    p = _g(repo, *a)
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
    def __init__(self, repo):
        self.repo = Path(repo)
        port = _free_port()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
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

    def post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        rq = urllib.request.Request(self.base + path, data=data,
                                    headers={"Content-Type": "application/json"},
                                    method="POST")
        try:
            r = urllib.request.urlopen(rq, timeout=60)
            code, payload = r.getcode(), r.read()
        except urllib.error.HTTPError as e:
            code, payload = e.code, e.read()
        text = payload.decode("utf-8", "replace")
        try:
            j = json.loads(text)
        except Exception:
            j = None
        return code, text, j


class TestWriteInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_winputs_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    # -- 工具 ------------------------------------------------------------
    def _repo(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        return d

    def _staged(self, d):
        return [x for x in _gmust(d, "diff", "--cached", "--name-only").split() if x]

    def _assert_clean_error(self, code, text, j, what):
        self.assertEqual(code, 400, "%s 应该 400: %s" % (what, text[:120]))
        self.assertIsInstance(j, dict)
        self.assertIs(j.get("ok"), False)
        self.assertTrue(str(j.get("error") or "").strip(),
                        "%s 的 error 不能为空" % what)
        leaked = [t for t in BAD_TEXT if t in text]
        self.assertEqual(leaked, [], "%s 泄漏了 Python 原文: %s" % (what, text[:160]))

    # -- 1. NUL 路径 -----------------------------------------------------
    def test_resolve_rejects_nul_before_path(self):
        """单元层：resolve_in_root 必须在碰 Path 之前拒 NUL。"""
        r = server.Repo(self._repo("unit_nul"))
        for bad in ("a.txt\0.png", "\0", "sub\0/../a.txt"):
            with self.assertRaises(server.GitError):
                r.resolve_in_root(bad)

    def test_unstage_nul_does_not_touch_named_file(self):
        """核心事故：请求 'a.txt\\0.png' 不许动 a.txt。"""
        d = self._repo("nul_unstage")
        (d / "a.txt").write_text("staged\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        self.assertEqual(self._staged(d), ["a.txt"])
        s = _Server(d)
        try:
            code, text, j = s.post("/api/unstage", {"files": ["a.txt" + NUL + ".png"]})
            self._assert_clean_error(code, text, j, "unstage NUL 路径")
            self.assertEqual(self._staged(d), ["a.txt"], "a.txt 的暂存被动了")
        finally:
            s.close()

    def test_discard_nul_does_not_touch_named_file(self):
        d = self._repo("nul_discard")
        (d / "a.txt").write_text("worktree change\n", encoding="utf-8", newline="\n")
        s = _Server(d)
        try:
            code, text, j = s.post("/api/discard",
                                   {"files": ["a.txt" + NUL + ".png"], "confirmed": True})
            self._assert_clean_error(code, text, j, "discard NUL 路径")
            self.assertEqual((d / "a.txt").read_text(encoding="utf-8"), "worktree change\n",
                             "a.txt 的工作区改动被丢弃了")
        finally:
            s.close()

    def test_stage_nul_rejected(self):
        d = self._repo("nul_stage")
        s = _Server(d)
        try:
            code, text, j = s.post("/api/stage", {"files": ["a.txt" + NUL + ".png"]})
            self._assert_clean_error(code, text, j, "stage NUL 路径")
            self.assertEqual(self._staged(d), [])
        finally:
            s.close()

    # -- 2. commit 的 NUL 消息不许留下「前半截」副作用 -------------------
    def test_commit_nul_message_stages_nothing(self):
        d = self._repo("nul_commit")
        (d / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
        s = _Server(d)
        try:
            code, text, j = s.post("/api/commit", {"message": "a" + NUL + "b"})
            self._assert_clean_error(code, text, j, "commit NUL 消息")
            self.assertEqual(self._staged(d), [],
                             "提交被拒了, 但文件已经被暂存 —— 拒绝不许有前半截副作用")
        finally:
            s.close()

    # -- 3. 消息类参数：类型 + NUL ---------------------------------------
    def test_stash_message_type(self):
        d = self._repo("msg_stash")
        (d / "a.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
        s = _Server(d)
        try:
            for bad in (["x"], 5, True):
                code, text, j = s.post("/api/history/stash", {"message": bad})
                self._assert_clean_error(code, text, j, "stash message=%r" % (bad,))
            self.assertEqual(_gmust(d, "stash", "list").strip(), "", "不该真的 stash")
        finally:
            s.close()

    def test_amend_message_type_and_nul(self):
        d = self._repo("msg_amend")
        s = _Server(d)
        try:
            for bad in (5, ["x"], "a" + NUL + "b"):
                code, text, j = s.post("/api/history/amend",
                                       {"message": bad, "confirmed": True})
                self._assert_clean_error(code, text, j, "amend message=%r" % (bad,))
            self.assertEqual(_gmust(d, "rev-list", "--count", "HEAD").strip(), "1")
        finally:
            s.close()

    def test_tag_message_type_and_nul(self):
        d = self._repo("msg_tag")
        s = _Server(d)
        try:
            for bad in (5, ["x"], "a" + NUL + "b"):
                code, text, j = s.post("/api/history/tag/create",
                                       {"name": "v1", "message": bad})
                self._assert_clean_error(code, text, j, "tag message=%r" % (bad,))
            self.assertEqual(_gmust(d, "tag", "-l").strip(), "", "不该真的打标签")
        finally:
            s.close()

    # -- 4. reflog 索引类型 ---------------------------------------------
    def test_reflog_restore_index_type(self):
        d = self._repo("idx_reflog")
        s = _Server(d)
        try:
            for bad in (None, "abc", True, 1.5, -1):
                code, text, j = s.post("/api/history/reflog/restore",
                                       {"index": bad, "confirmed": True})
                self._assert_clean_error(code, text, j, "reflog index=%r" % (bad,))
            self.assertEqual(_gmust(d, "rev-list", "--count", "HEAD").strip(), "1",
                             "拒绝之后不许动 HEAD")
        finally:
            s.close()

    # -- 5. 一组「必须被干净拒绝」的写请求（防 Python 原文泄漏） ----------
    def test_malformed_writes_never_leak_python_text(self):
        d = self._repo("sweep")
        s = _Server(d)
        try:
            cases = [
                ("/api/checkout", {"name": "--help"}),
                ("/api/branch", {"name": "a" + NUL + "b"}),
                ("/api/merge", {"name": ""}),
                ("/api/reset", {"mode": "wat", "confirmed": True}),
                ("/api/history/stash/pop", {"index": "abc", "confirmed": True}),
                ("/api/history/branch/delete", {"name": "nope", "confirmed": True}),
                ("/api/remote/push", {"remote": "-x", "confirmed": True}),
                ("/api/gen", {"kind": "wat"}),
            ]
            for path, body in cases:
                code, text, j = s.post(path, body)
                self.assertLess(code, 500, "%s 500 了: %s" % (path, text[:120]))
                leaked = [t for t in BAD_TEXT if t in text]
                self.assertEqual(leaked, [], "%s 泄漏 Python 原文: %s" % (path, text[:160]))
                if code >= 400:
                    self.assertIsInstance(j, dict, "%s 的 4xx 不是 JSON" % path)
                    self.assertIs(j.get("ok"), False, "%s 的 4xx 不是 {ok:false}" % path)
        finally:
            s.close()


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
