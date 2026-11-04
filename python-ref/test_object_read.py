# -*- coding: utf-8 -*-
"""常驻 `cat-file --batch` 的等价性与回退（2026-10-27 录）。

`Repo.show_file()` / `staged_file()` 命中常驻进程时 0 spawn（`/api/file` 42.8→13.8ms）。
它们共同的结构是「**命中就用，任何不确定就回退到 `git show`**」，所以判据两半都要有：

  1. 与 `git show <ref>:<path>` **逐字节等价**（文本 / 空文件 / 二进制 / CRLF /
     中文与空格路径 / 分支 ref / 缺失路径）；
  2. **tree（目录）不许被当成 blob**：`git show HEAD:sub` 打印的是 tree 清单，
     那是既有行为，快路径必须回退而不是悄悄返回 None 或原始字节；
  3. worktree（`.git` 是文件）→ 常驻进程不接，功能仍然正确。

运行方式：`python -X utf8 test_object_read.py`（仓根目录）。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

import gitcat
import server

ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent


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


class TestObjectRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_objread_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)
        d = cls.tmp / "repo"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "text.txt").write_text("line1\nline2\n", encoding="utf-8", newline="\n")
        (d / "empty.txt").write_bytes(b"")
        (d / "bin.dat").write_bytes(b"\x00\x01\x02hello\x00")
        (d / "crlf.txt").write_bytes(b"a\r\nb\r\n")
        (d / "中文 名.txt").write_text("x\n", encoding="utf-8", newline="\n")
        (d / "sub").mkdir()
        (d / "sub" / "in.txt").write_text("i\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        _gmust(d, "branch", "feature")
        cls.repo = d

    @classmethod
    def tearDownClass(cls):
        # 常驻 cat-file 持有仓句柄, 先关再删（Windows 上否则 WinError 32）
        try:
            import gitcat
            gitcat.close_all()
        except Exception:
            pass
        _rmtree(cls.tmp)

    def _git_show(self, spec):
        p = _g(self.repo, "show", spec)
        return p.stdout if p.returncode == 0 else None

    def _want(self, spec):
        """既有语义：空 stdout → None。"""
        out = self._git_show(spec)
        return None if out in ("", None) else out

    def test_blob_equivalence(self):
        r = server.Repo(self.repo)
        for rel, ref in (("text.txt", "HEAD"), ("empty.txt", "HEAD"),
                         ("bin.dat", "HEAD"), ("crlf.txt", "HEAD"),
                         ("中文 名.txt", "HEAD"), ("sub/in.txt", "HEAD"),
                         ("text.txt", "feature"), ("missing.txt", "HEAD")):
            with self.subTest(rel=rel, ref=ref):
                self.assertEqual(r.show_file(rel, ref),
                                 self._want("%s:%s" % (ref, rel)))

    def test_tree_falls_back(self):
        """目录：必须与 `git show HEAD:sub` 的 tree 清单逐字一致（回退，不是 None）。"""
        r = server.Repo(self.repo)
        want = self._git_show("HEAD:sub")
        self.assertTrue(want and "in.txt" in want, "前置：tree 清单里应有 in.txt")
        self.assertEqual(r.show_file("sub", "HEAD"), want)

    def test_staged_file_equivalence(self):
        d = self.tmp / "staged"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "s.txt").write_text("v1\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        (d / "s.txt").write_text("v2\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        r = server.Repo(d)
        self.assertEqual(r.staged_file("s.txt"), "v2\n")
        self.assertIsNone(r.staged_file("nope.txt"))

    def test_worktree_falls_back(self):
        """`.git` 是文件 → 常驻进程不接，但结果仍然正确。"""
        d = self.tmp / "wtbase"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        wt = self.tmp / "wtlink"
        _gmust(d, "worktree", "add", "-q", str(wt), "-b", "wtb")
        self.assertIsNone(gitcat.read(wt, "HEAD:a.txt"), "worktree 不该接")
        r = server.Repo(wt)
        self.assertEqual(r.show_file("a.txt", "HEAD"), "a\n")

    def test_spec_rejects_nul_and_newline(self):
        self.assertIsNone(gitcat.read(self.repo, "HEAD:a.txt\x00.png"))
        self.assertIsNone(gitcat.read(self.repo, "HEAD:a.txt\nHEAD:b.txt"))

    def test_idle_reap_releases_handles(self):
        """空闲回收后，仓目录必须能被删掉（Windows 上句柄会挡住 rmtree）。

        这是 P4 那条「常驻进程持有句柄 → 删仓 WinError 32」的产品侧修法：
        `gitcat.reap()` 只在拿到会话锁时才关，不会 terminate 正在读的进程。
        """
        d = self.tmp / "reapme"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        self.assertIsNotNone(gitcat.read(d, "HEAD:a.txt"), "前置：应命中常驻进程")
        # 把会话标记成「很久没用」，然后跑一次回收（不用等 60s）
        import gitcat as g
        with g._SESS_MU:
            for s in g._SESS.values():
                s.last = 0.0
        self.assertGreaterEqual(g.reap(), 1, "应当回收至少一个空闲会话")
        _rmtree(d)
        self.assertFalse(d.exists(), "回收后目录应可删除")


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
