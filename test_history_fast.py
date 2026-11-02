# -*- coding: utf-8 -*-
"""`history/tags` 与 `history/reflog` 的快路径判据（2026-10-27 录）。

这两个端点原来各 spawn 一次 git；现在 refs 直读 + 常驻 `cat-file --batch` 解析
commit/tag 对象，0 spawn。它们要复现的是 git 自己的**格式化语义**，所以判据只有
一条站得住：与 `for-each-ref` / `reflog` 的输出**逐字段相等**，且「看不懂就回退」。

覆盖的坑：
  * `%(contents:subject)` = 第一段、行间一个空格（多行 tag 消息）；
  * 注解 tag 的 creatordate 取 **tagger**，轻量 tag 取 **committer**；
  * 非 +0800 的时区（日期必须按记录里的偏移算，不是本机时区）；
  * 同秒创建的多个 tag：git 是「refname 排序后稳定排序」，打平保持字典序；
  * `%h` 宽度 = max(7, 唯一前缀)，`core.abbrev` 可覆盖；
  * 空 reflog / 未出生 HEAD → 空数组，不是错误。

运行方式：`python -X utf8 test_history_fast.py`（仓根目录）。
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

import history_ops

ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent


def _g(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


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


def _git_tags(repo, n=500):
    out = _gmust(repo, "for-each-ref", "--count=%d" % n, "--sort=-creatordate",
                 "--format=%(refname:short)|%(objectname)|%(objecttype)|"
                 "%(contents:subject)|%(creatordate:short)", "refs/tags")
    rows = []
    for line in out.splitlines():
        if line.strip():
            x = line.split("|")
            rows.append({"name": x[0], "sha": x[1], "type": x[2],
                         "subject": x[3], "date": x[4],
                         "annotated": x[2] == "tag"})
    return rows


def _git_reflog(repo, n=30):
    out = _gmust(repo, "reflog", "-%d" % n, "--date=iso",
                 "--pretty=format:%H|%h|%an|%ad|%gs")
    rows = []
    for i, line in enumerate([x for x in out.splitlines() if x.strip()]):
        x = line.split("|")
        rows.append({"index": i, "sha": x[0], "short": x[1],
                     "selector": "HEAD@{%d}" % i, "author": x[2],
                     "date": x[3], "action": x[4]})
    return rows


class TestHistoryFast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_hfast_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        # 常驻 cat-file 持有仓句柄, 先关再删（Windows 上否则 WinError 32）
        try:
            import gitcat
            gitcat.close_all()
        except Exception:
            pass
        _rmtree(cls.tmp)

    def _repo(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "L1\nL2\n\nbody")
        return d

    # -- tags ------------------------------------------------------------
    def _tag_matrix(self, d):
        _gmust(d, "tag", "light")
        _gmust(d, "tag", "-a", "annot", "-m", "first line", "-m", "second para")
        _gmust(d, "tag", "-a", "multi", "-m", "M1\nM2\n\nrest")
        # 非 +0800 时区：用 tagger 日期固定偏移
        env = dict(os.environ, GIT_COMMITTER_DATE="2020-01-02 03:04:05 +0530")
        _gmust(d, "tag", "-a", "tz", "-m", "tz tag", env=env)
        # 中文标签名（git 不允许标签名里带空格）
        _gmust(d, "tag", "-a", "中文标签", "-m", "unicode tag")

    def test_tags_match_git(self):
        d = self._repo("tags")
        self._tag_matrix(d)
        ops = history_ops.HistoryOps(d)
        self.assertEqual(ops._tags_fast(500), _git_tags(d, 500))
        for n in (1, 2, 3, 5):
            self.assertEqual(ops._tags_fast(n), _git_tags(d, n), "limit=%d" % n)

    def test_tags_empty(self):
        d = self._repo("tags_empty")
        self.assertEqual(history_ops.HistoryOps(d)._tags_fast(10), [])
        self.assertEqual(history_ops.HistoryOps(d).list_tags(10), [])

    def test_tags_subject_paragraph_rule(self):
        d = self._repo("tags_subj")
        _gmust(d, "tag", "-a", "m", "-m", "L1\nL2\n\nignored body")
        rows = history_ops.HistoryOps(d).list_tags(10)
        self.assertEqual(rows[0]["subject"], "L1 L2")
        self.assertEqual(rows[0]["type"], "tag")
        self.assertTrue(rows[0]["annotated"])

    # -- reflog ----------------------------------------------------------
    def test_reflog_match_git(self):
        d = self._repo("reflog")
        for i in range(3):
            _gmust(d, "commit", "--allow-empty", "-q", "-m", "c%d" % i)
        _gmust(d, "checkout", "-q", "-b", "side")
        _gmust(d, "checkout", "-q", "main")
        ops = history_ops.HistoryOps(d)
        self.assertEqual(ops._reflog_fast(30), _git_reflog(d, 30))
        for n in (1, 2, 4):
            self.assertEqual(ops._reflog_fast(n), _git_reflog(d, n), "limit=%d" % n)

    def test_reflog_core_abbrev(self):
        d = self._repo("reflog_abbrev")
        _gmust(d, "config", "core.abbrev", "10")
        ops = history_ops.HistoryOps(d)
        fast = ops._reflog_fast(5)
        self.assertEqual(fast, _git_reflog(d, 5))
        self.assertEqual(len(fast[0]["short"]), 10)

    def test_reflog_author_with_space_and_tz(self):
        d = self._repo("reflog_author")
        env = dict(os.environ, GIT_AUTHOR_DATE="2019-05-06 07:08:09 -0700",
                   GIT_COMMITTER_DATE="2019-05-06 07:08:09 -0700")
        _gmust(d, "-c", "user.name=Zhang San", "-c", "user.email=z@x",
               "commit", "--allow-empty", "-q", "-m", "named", env=env)
        fast = history_ops.HistoryOps(d)._reflog_fast(1)
        want = _git_reflog(d, 1)
        self.assertEqual(fast, want)
        self.assertEqual(fast[0]["author"], "Zhang San")
        self.assertTrue(fast[0]["date"].endswith("-0700"), fast[0]["date"])

    def test_reflog_unborn_is_empty(self):
        d = self.tmp / "reflog_unborn"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        self.assertEqual(history_ops.HistoryOps(d).list_reflog(10), [])


class TestHistoryHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_hfast_http_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        # 常驻 cat-file 持有仓句柄, 先关再删（Windows 上否则 WinError 32）
        try:
            import gitcat
            gitcat.close_all()
        except Exception:
            pass
        _rmtree(cls.tmp)

    def _serve(self, repo):
        port = _free_port()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   AIGIT_GIT_COUNT="1")
        p = subprocess.Popen([sys.executable, "-X", "utf8", "server.py",
                              "--root", str(repo), "--port", str(port)],
                             cwd=str(REPO_ROOT), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1).read()
                return p, base
            except Exception:
                if p.poll() is not None:
                    raise AssertionError("server 死了")
                time.sleep(0.25)
        raise AssertionError("server 起太慢")

    def _get(self, base, path):
        try:
            r = urllib.request.urlopen(base + path, timeout=60)
            code, payload, hdr = r.getcode(), r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, payload, hdr = e.code, e.read(), e.headers
        return code, json.loads(payload.decode("utf-8")), hdr

    def test_tags_zero_spawn(self):
        d = self.tmp / "http_tags"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c")
        _gmust(d, "tag", "-a", "v1", "-m", "release")
        proc, base = self._serve(d)
        try:
            code, body, hdr = self._get(base, "/api/history/tags")
            self.assertEqual(code, 200)
            self.assertIsInstance(body, dict)          # 2026-11-02: {ok, tags:[...]}
            self.assertEqual([r["name"] for r in body["tags"]], ["v1"])
            self.assertEqual(hdr.get("X-aigit-git-calls"), "0")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_reflog_zero_spawn(self):
        d = self.tmp / "http_reflog"
        d.mkdir()
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c")
        proc, base = self._serve(d)
        try:
            code, body, hdr = self._get(base, "/api/history/reflog")
            self.assertEqual(code, 200)
            self.assertIsInstance(body, dict)          # 2026-11-02: {ok, reflog:[...]}
            self.assertEqual(body["reflog"][0]["selector"], "HEAD@{0}")
            self.assertEqual(hdr.get("X-aigit-git-calls"), "0")
        finally:
            proc.terminate()
            proc.wait(10)


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
