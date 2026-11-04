# -*- coding: utf-8 -*-
"""`gitrefs` 四个快路径的常驻判据（2026-10-27 录）。

背景（doc/aigit-perf.md §12.13）：`branches` / `history/stashes` / `conflict/list`
/ `conflict/state` 原来各 spawn 1–2 次 git，现在改为直接读 `.git` 里的
引用 / reflog / 索引。四处的共同结构是「**快路径 + 回退**」，所以判据必须两半都有：

  1. **该走快路径的**：结果与 git 自己的输出逐项相等（否则一个永远回退的实现也能全绿）；
  2. **该回退的**（worktree、`.git` 是文件）：必须回退，且结果仍然正确。

外加 HTTP 层：这四处**不许再 spawn**（用 `X-aigit-git-calls` 直接钉住），
但「有冲突时」`conflict/list` 必须走原来的多进程批量路径（那是正确性，不是性能）。

运行方式：`python -X utf8 test_refstore.py`（仓根目录）。
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

import gitrefs
import server

ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
FS = "\x1f"
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


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _serve(repo):
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


def _get(base, path):
    try:
        r = urllib.request.urlopen(base + path, timeout=60)
        code, payload, hdr = r.getcode(), r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, payload, hdr = e.code, e.read(), e.headers
    try:
        j = json.loads(payload.decode("utf-8"))
    except Exception:
        j = {}
    return {"http": code, "json": j, "calls": hdr.get("X-aigit-git-calls"),
            "cmds": hdr.get("X-aigit-git-cmds")}


class TestRefstoreDiff(unittest.TestCase):
    """四项快路径 vs git 自己：逐项相等，该退的必须退。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_refst_%d" % (os.getpid() % 100000))
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

    def _repo(self, name, commit=True):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        if commit:
            _gmust(d, "add", "-A")
            _gmust(d, *ID, "commit", "-q", "-m", "c1")
        return d

    # -- 分支 ------------------------------------------------------------
    def _assert_branches(self, d):
        want = {}
        for line in _gmust(d, "for-each-ref",
                           "--format=%(refname:short)" + FS + "%(objectname)",
                           "refs/heads").splitlines():
            if line.strip():
                p = line.split(FS)
                want[p[0]] = p[1]
        got = gitrefs.head_shas(d)
        self.assertIsNotNone(got, "该走快路径")
        self.assertEqual(got, want)
        self.assertEqual(gitrefs.head_names(d), sorted(want))

    def test_branches_loose_slash_unicode(self):
        d = self._repo("b_loose")
        for n in ("feature", "bug/fix-1", "发布"):
            _gmust(d, "branch", n)
        self._assert_branches(d)

    def test_branches_packed_and_mixed(self):
        d = self._repo("b_packed")
        for i in range(6):
            _gmust(d, "branch", "p%d" % i)
        _gmust(d, "pack-refs", "--all")
        self._assert_branches(d)
        _gmust(d, "branch", "loose_after_pack")
        self._assert_branches(d)

    def test_branches_many(self):
        d = self._repo("b_many")
        for i in range(50):
            _gmust(d, "branch", "b%03d" % i)
        self._assert_branches(d)

    # -- HEAD 三态 -------------------------------------------------------
    def _assert_head(self, d):
        p = _g(d, "rev-parse", "--verify", "--quiet", "HEAD")
        want_sha = p.stdout.strip() if p.returncode == 0 else None
        q = _g(d, "rev-parse", "--abbrev-ref", "HEAD")
        want_cur = q.stdout.strip() or None     # 不看 rc：未出生/游离都打 "HEAD"
        st = gitrefs.head_state(d)
        self.assertIsNotNone(st, "该走快路径")
        got_sha = st[2]
        got_cur = (st[1] if st[0] == "branch"
                   else ("HEAD" if st[0] in ("detached", "unborn") else None))
        self.assertEqual((got_sha, got_cur), (want_sha, want_cur))

    def test_head_normal(self):
        self._assert_head(self._repo("h_normal"))

    def test_head_detached(self):
        d = self._repo("h_detached")
        _gmust(d, "checkout", "-q", "--detach", "HEAD")
        self._assert_head(d)

    def test_head_unborn(self):
        self._assert_head(self._repo("h_unborn", commit=False))

    # -- stash -----------------------------------------------------------
    def test_stash_matches_git(self):
        d = self._repo("s_two")
        (d / "a.txt").write_text("dirty1\n", encoding="utf-8", newline="\n")
        _gmust(d, "stash", "push", "-q", "-m", "first stash")
        (d / "a.txt").write_text("dirty2\n", encoding="utf-8", newline="\n")
        _gmust(d, "stash", "push", "-q")
        want = [x for x in _gmust(d, "stash", "list").splitlines() if x.strip()]
        ent = gitrefs.stash_entries(d)
        self.assertIsNotNone(ent)
        mine = ["stash@{%d}: %s" % (i, msg)
                for i, (_s, msg) in enumerate(ent[::-1])]
        self.assertEqual(mine, want)

    def test_stash_empty(self):
        d = self._repo("s_none")
        self.assertEqual(gitrefs.stash_entries(d), [])

    # -- 未合并 ----------------------------------------------------------
    def test_unmerged_matches_git(self):
        d = self._repo("u_conflict")
        (d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "base")
        _gmust(d, "checkout", "-q", "-b", "side")
        (d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "side")
        _gmust(d, "checkout", "-q", "main")
        (d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "main")
        _g(d, "merge", "side")               # 故意冲突
        want = sorted(x for x in _gmust(d, "diff", "--name-only",
                                        "--diff-filter=U", "-z").split("\0") if x)
        self.assertEqual(gitrefs.unmerged_paths(d), want)
        self.assertEqual(want, ["c.txt"])
        # 解决之后应为空
        _gmust(d, "checkout", "--theirs", "c.txt")
        _gmust(d, "add", "-A")
        self.assertEqual(gitrefs.unmerged_paths(d), [])

    # -- 回退 ------------------------------------------------------------
    def test_worktree_falls_back(self):
        d = self._repo("wt_base")
        wt = self.tmp / "wt_link"
        _gmust(d, "worktree", "add", "-q", str(wt), "-b", "wtb")
        self.assertTrue((wt / ".git").is_file(), "前置:worktree 的 .git 是文件")
        self.assertIsNone(gitrefs.head_names(wt))
        self.assertIsNone(gitrefs.head_shas(wt))
        self.assertIsNone(gitrefs.head_state(wt))
        self.assertIsNone(gitrefs.stash_entries(wt))
        self.assertIsNone(gitrefs.parse_index(wt))


class TestRefstoreHttp(unittest.TestCase):
    """HTTP 层：四处不再 spawn；有冲突时仍走正确路径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_refsthttp_%d" % (os.getpid() % 100000))
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
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        return d

    def test_branches_zero_spawn(self):
        d = self._repo("hb")
        _gmust(d, "branch", "feature")
        _gmust(d, "branch", "bug/x")
        proc, base = _serve(d)
        try:
            r = _get(base, "/api/branches")
            self.assertEqual(r["http"], 200)
            names = [b["name"] for b in r["json"]["branches"]]
            self.assertEqual(sorted(names), ["bug/x", "feature", "main"])
            cur = [b["name"] for b in r["json"]["branches"] if b["current"]]
            self.assertEqual(cur, ["main"])
            self.assertEqual(r["calls"], "0", "branches 不该再 spawn")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_stashes_zero_spawn_and_order(self):
        d = self._repo("hs")
        (d / "a.txt").write_text("d1\n", encoding="utf-8", newline="\n")
        _gmust(d, "stash", "push", "-q", "-m", "msg one")
        (d / "a.txt").write_text("d2\n", encoding="utf-8", newline="\n")
        _gmust(d, "stash", "push", "-q")
        proc, base = _serve(d)
        try:
            r = _get(base, "/api/history/stashes")
            self.assertEqual(r["http"], 200)
            rows = r["json"]["stashes"]   # 2026-11-02: {ok, stashes:[...]}
            self.assertEqual([x["index"] for x in rows], [0, 1])
            self.assertEqual([x["ref"] for x in rows], ["stash@{0}", "stash@{1}"])
            self.assertIn("msg one", rows[1]["raw"])
            self.assertEqual(r["calls"], "0", "stashes 不该再 spawn")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_conflict_list_zero_spawn_when_clean(self):
        d = self._repo("hc")
        proc, base = _serve(d)
        try:
            r = _get(base, "/api/conflict/list")
            self.assertEqual(r["http"], 200)
            self.assertEqual(r["json"]["count"], 0)
            self.assertFalse(r["json"]["inMerge"])
            self.assertEqual(r["calls"], "0", "没有未合并条目时不该 spawn")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_conflict_list_still_works_with_conflict(self):
        d = self._repo("hc2")
        (d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "base")
        _gmust(d, "checkout", "-q", "-b", "side")
        (d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "side")
        _gmust(d, "checkout", "-q", "main")
        (d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "main")
        _g(d, "merge", "side")
        proc, base = _serve(d)
        try:
            r = _get(base, "/api/conflict/list")
            self.assertEqual(r["http"], 200)
            self.assertEqual(r["json"]["count"], 1)
            self.assertTrue(r["json"]["inMerge"])
            self.assertNotEqual(r["calls"], "0", "有冲突时必须走原来的路径")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_conflict_state_zero_spawn(self):
        d = self._repo("hcs")
        proc, base = _serve(d)
        try:
            r = _get(base, "/api/conflict/state")
            self.assertEqual(r["http"], 200)
            self.assertEqual(r["json"]["head"],
                             _gmust(d, "rev-parse", "HEAD").strip())
            self.assertEqual(r["json"]["currentBranch"], "main")
            self.assertFalse(r["json"]["inMerge"])
            self.assertEqual(r["calls"], "0", "conflict/state 不该再 spawn")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_worktree_still_serves_branches(self):
        """负向对照：.git 是文件时会被回退，功能不许变。"""
        d = self._repo("hwt")
        wt = self.tmp / "hwt_link"
        _gmust(d, "worktree", "add", "-q", str(wt), "-b", "wtb")
        proc, base = _serve(wt)
        try:
            r = _get(base, "/api/branches")
            self.assertEqual(r["http"], 200)
            names = [b["name"] for b in r["json"]["branches"]]
            self.assertEqual(sorted(names), ["main", "wtb"])
            self.assertNotEqual(r["calls"], "0", "worktree 必须回退到 git")
            # 冲突端点同一条回退路径（_gitdir 也要回退，否则读错目录）
            r2 = _get(base, "/api/conflict/state")
            self.assertEqual(r2["http"], 200)
            self.assertFalse(r2["json"]["inMerge"])
            self.assertNotEqual(r2["calls"], "0", "worktree 的 _gitdir 必须回退")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_default_branch_remote_head(self):
        """远端 HEAD 的**目标分支名**（2026-10-27 修：原来返回 `origin`）。"""
        import history_ops
        d = self._repo("hd")
        _gmust(d, "branch", "trunk")
        _gmust(d, "update-ref", "refs/remotes/origin/main",
               _gmust(d, "rev-parse", "HEAD").strip())
        _gmust(d, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.assertEqual(gitrefs.remote_head_branch(d), "main")
        self.assertEqual(history_ops.HistoryOps(d).default_branch(), "main")
        # 远端 HEAD 指向 trunk 时就要报 trunk，不是 main
        _gmust(d, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        _gmust(d, "update-ref", "refs/remotes/origin/trunk",
               _gmust(d, "rev-parse", "HEAD").strip())
        self.assertEqual(history_ops.HistoryOps(d).default_branch(), "trunk")

    def test_default_branch_no_remote_head(self):
        """没有远端 HEAD 时退回本地 main/master 猜测（行为不变）。"""
        import history_ops
        d = self._repo("hnd")
        self.assertIsNone(gitrefs.remote_head_branch(d))
        self.assertEqual(history_ops.HistoryOps(d).default_branch(), "main")

    def test_default_branch_remote_head_detached(self):
        """远端 HEAD 直接指向 sha（不是符号引用）→ 判不出分支名，退回本地猜测。"""
        import history_ops
        d = self._repo("hdd")
        _gmust(d, "update-ref", "refs/remotes/origin/HEAD",
               _gmust(d, "rev-parse", "HEAD").strip())
        self.assertIsNone(gitrefs.remote_head_branch(d))
        self.assertEqual(history_ops.HistoryOps(d).default_branch(), "main")


if __name__ == "__main__":
    import io

    class _Reporter(unittest.TextTestResult):
        """与 test_aigit.py 同形的行格式（mutation_runner 的 MARK_RE 认这个）。"""

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
