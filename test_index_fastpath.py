# -*- coding: utf-8 -*-
"""索引直读 / ref 直读的「快路径 ∪ 回退」边界（2026-10-22 录）。

背景（doc/aigit-perf.md §12.12）：`stage`/`unstage` 的 `ls-files` 守卫改成直接读
`.git/index`（省一次 ~37ms spawn），`checkout` 的 `show-ref --verify` 改成读
loose ref + `packed-refs`。两者都是「快路径 + 回退」结构，**回退条件本身就是安全
边界**：该回退却没回退，就会拿一份错的已跟踪集合/错的 ref 结论去做判断。

本文件钉三件事，缺一不可：
  1. **该走快路径的形态**：集合与 `git ls-files -z` 逐名相等（否则「永远回退」也能全绿）；
  2. **该回退的形态**：必须返回 None —— v4 前缀压缩、split-index、sparse-index、
     worktree（`.git` 是文件）、`GIT_INDEX_FILE` 被设置、没有索引文件；
  3. **HTTP 层契约不变**：stage 混合路径不 500、checkout 拒绝文案逐字、
     packed-refs 里的分支能切过去、discard 仍然问真 git（反向对照：它**不许**走快路径）。

运行方式：`python -X utf8 -m unittest test_index_fastpath -v`（仓根目录）。
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

_ID = ["-c", "user.name=T", "-c", "user.email=t@t"]
REPO_ROOT = Path(__file__).resolve().parent


def _g(repo, *args):
    return subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace")


def _gmust(repo, *args):
    p = _g(repo, *args)
    if p.returncode != 0:
        raise AssertionError("git %s rc=%d %s"
                             % (" ".join(args[:2]), p.returncode,
                                (p.stderr or p.stdout)[:200]))
    return p.stdout


def _force_rmtree(p):
    def onerror(func, path, exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass
    shutil.rmtree(str(p), onerror=onerror)


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start(base_dir, name):
    """建仓 + 起 server，返回 (proc, base_url, repo)"""
    repo = Path(base_dir) / name
    repo.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (repo / ("a%d.txt" % i)).write_text("v%d\n" % i, encoding="utf-8")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "deep.txt").write_text("d\n", encoding="utf-8")
    _gmust(repo, "init", "-q")
    _gmust(repo, "config", "user.name", "bench")
    _gmust(repo, "config", "user.email", "bench@localhost")
    _gmust(repo, "add", "-A")
    _gmust(repo, *_ID, "commit", "-q", "-m", "init")
    return _serve(repo)


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
            return p, base, repo
        except Exception:
            if p.poll() is not None:
                raise AssertionError("server 死了")
            time.sleep(0.25)
    raise AssertionError("server 起太慢")


def _post(base, path, body):
    data = json.dumps(body).encode("utf-8")
    rq = urllib.request.Request(base + path, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        r = urllib.request.urlopen(rq, timeout=120)
        code, payload, hdr = r.getcode(), r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, payload, hdr = e.code, e.read(), e.headers
    try:
        body_j = json.loads(payload.decode("utf-8"))
    except Exception:
        body_j = {}
    return {"http": code, "json": body_j, "text": payload.decode("utf-8", "replace"),
            "calls": hdr.get("X-aigit-git-calls"), "cmds": hdr.get("X-aigit-git-cmds")}


def _staged(repo):
    return {x for x in _gmust(repo, "diff", "--cached", "--name-only", "-z").split("\0") if x}


class TestIndexFastPath(unittest.TestCase):
    """索引直读：该走的逐名相等，该退的必须退。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_idxfast_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _force_rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _force_rmtree(cls.tmp)

    def _repo(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        for i in range(3):
            (d / ("f%d.txt" % i)).write_text("v%d\n" % i, encoding="utf-8")
        (d / "sub").mkdir(exist_ok=True)
        (d / "sub" / "deep.txt").write_text("d\n", encoding="utf-8")
        (d / "有 空格.txt").write_text("u\n", encoding="utf-8")
        return d

    def _both(self, repo):
        fast = server.Repo(Path(repo))._index_tracked_fast()
        truth = {x for x in _gmust(repo, "ls-files", "-z").split("\0") if x}
        return fast, truth

    def test_v2_exact(self):
        d = self._repo("v2")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        fast, truth = self._both(d)
        self.assertIsNotNone(fast, "v2 索引用该走快路径")
        self.assertEqual(fast, truth)

    def test_v3_skip_worktree_exact(self):
        d = self._repo("v3")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        _gmust(d, "update-index", "--skip-worktree", "f0.txt")
        fast, truth = self._both(d)
        self.assertIsNotNone(fast, "v3 索引(扩展 flags)该走快路径")
        self.assertEqual(fast, truth)

    def test_unborn_head_exact(self):
        d = self.tmp / "unborn"
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q")
        (d / "x.txt").write_text("1\n", encoding="utf-8")
        (d / "y").mkdir()
        (d / "y" / "z.txt").write_text("2\n", encoding="utf-8")
        _gmust(d, "add", "-A")
        fast, truth = self._both(d)
        self.assertIsNotNone(fast)
        self.assertEqual(fast, truth)

    def test_unmerged_exact(self):
        d = self._repo("unmerged")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        _gmust(d, "checkout", "-q", "-b", "side")
        (d / "f0.txt").write_text("side\n", encoding="utf-8")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "s")
        _gmust(d, "checkout", "-q", "master")
        (d / "f0.txt").write_text("main\n", encoding="utf-8")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "m")
        _g(d, "merge", "side")           # 故意让它冲突
        fast, truth = self._both(d)
        self.assertIsNotNone(fast)
        self.assertEqual(fast, truth)

    def test_v4_falls_back(self):
        d = self._repo("v4")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        _gmust(d, "update-index", "--index-version", "4")
        fast, _ = self._both(d)
        self.assertIsNone(fast, "v4 是路径前缀压缩，必须回退")

    def test_split_index_falls_back(self):
        d = self._repo("split")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        _gmust(d, "update-index", "--split-index")
        fast, _ = self._both(d)
        self.assertIsNone(fast, "split-index 的条目不全在主索引里，必须回退")

    def test_sparse_index_falls_back(self):
        d = self._repo("sparse")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        _gmust(d, "sparse-checkout", "init", "--cone", "--no-sparse-index")
        _gmust(d, "sparse-checkout", "set", "--sparse-index", "sub")
        fast, _ = self._both(d)
        self.assertIsNone(fast, "sparse-index 必须回退")

    def test_worktree_falls_back(self):
        d = self._repo("wtmain")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        wt = self.tmp / "wtlinked"
        _gmust(d, "worktree", "add", "-q", str(wt), "-b", "wtbranch")
        self.assertTrue((Path(wt) / ".git").is_file(), "worktree 的 .git 应该是文件")
        fast = server.Repo(Path(wt))._index_tracked_fast()
        self.assertIsNone(fast, "worktree 的 .git 是文件，必须回退")

    def test_git_index_file_env_falls_back(self):
        d = self._repo("idxenv")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        old = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = str(Path(d) / ".git" / "index")
        try:
            fast = server.Repo(Path(d))._index_tracked_fast()
        finally:
            if old is None:
                os.environ.pop("GIT_INDEX_FILE", None)
            else:
                os.environ["GIT_INDEX_FILE"] = old
        self.assertIsNone(fast, "GIT_INDEX_FILE 被设置时不许猜")

    def test_no_index_is_empty_not_fallback(self):
        """索引文件不存在 = 空索引（git 的语义），不是「不认识」。

        2026-10-27 改判据：此前这里断言「必须回退」。但 `git ls-files` 在没有
        索引文件时本来就输出空，所以返回空集合**与 git 一致**；把它判成「必须
        回退」等于要求实现比 git 更保守，白白多付一次 spawn。
        """
        d = self.tmp / "noidx"
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q")
        got = server.Repo(Path(d))._index_tracked_fast()
        self.assertIsNotNone(got, "索引缺失应视为空索引")
        self.assertEqual(got, set())


class TestBranchRefFastPath(unittest.TestCase):
    """ref 直读：看见=True，看不见=None（永不 False，防 reftable 错杀）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_refast_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _force_rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _force_rmtree(cls.tmp)

    def _repo(self, name):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        _gmust(d, "init", "-q")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        (d / "a.txt").write_text("a\n", encoding="utf-8")
        _gmust(d, "add", "-A")
        _gmust(d, *_ID, "commit", "-q", "-m", "c")
        return d

    def test_loose_ref_true(self):
        d = self._repo("loose")
        _gmust(d, "branch", "feature")
        self.assertTrue(server.Repo(Path(d))._local_branch_exists("feature"))

    def test_packed_ref_true(self):
        d = self._repo("packed")
        _gmust(d, "branch", "feature")
        _gmust(d, "pack-refs", "--all")
        self.assertFalse((Path(d) / ".git" / "refs" / "heads" / "feature").exists(),
                         "前置条件: 这条分支不该再有 loose ref")
        self.assertTrue(server.Repo(Path(d))._local_branch_exists("feature"))

    def test_missing_branch_none(self):
        d = self._repo("missing")
        self.assertIsNone(server.Repo(Path(d))._local_branch_exists("nope"))

    def test_corrupt_ref_none(self):
        d = self._repo("corrupt")
        p = Path(d) / ".git" / "refs" / "heads" / "broken"
        p.write_text("not-a-sha\n", encoding="utf-8")
        self.assertIsNone(server.Repo(Path(d))._local_branch_exists("broken"),
                          "内容不像 ref 时必须回退，让 show-ref 判")

    def test_worktree_gitfile_none(self):
        d = self._repo("wtbase")
        wt = self.tmp / "wt2"
        _gmust(d, "worktree", "add", "-q", str(wt), "-b", "wt2branch")
        self.assertIsNone(server.Repo(Path(wt))._local_branch_exists("wt2branch"),
                          "worktree 的 refs 在 commondir，必须回退")


class TestHttpContracts(unittest.TestCase):
    """HTTP 层：快路径不许改契约。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.environ["TEMP"]) / ("aigit_lightct_%d" % (os.getpid() % 100000))
        if cls.tmp.exists():
            _force_rmtree(cls.tmp)
        cls.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        _force_rmtree(cls.tmp)

    def test_stage_mixed_paths(self):
        """存在 + 不存在 + 已删除但跟踪: 只暂存该暂存的, 不 500。"""
        proc, base, repo = _start(self.tmp, "mix")
        try:
            (repo / "a0.txt").write_text("changed\n", encoding="utf-8")
            os.remove(str(repo / "a1.txt"))
            (repo / "newf.txt").write_text("n\n", encoding="utf-8")
            r = _post(base, "/api/stage",
                      {"files": ["a0.txt", "a1.txt", "newf.txt", "does_not_exist.txt"]})
            self.assertEqual(r["http"], 200, r["text"][:200])
            self.assertEqual(_staged(repo), {"a0.txt", "a1.txt", "newf.txt"})
            self.assertEqual(r["calls"], "2")
            self.assertEqual(r["cmds"], "add,status")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_checkout_missing_branch_signature(self):
        proc, base, repo = _start(self.tmp, "co_missing")
        try:
            r = _post(base, "/api/checkout", {"name": "nope-branch"})
            self.assertEqual(r["http"], 400)
            self.assertIn("分支不存在,已拒绝 checkout:nope-branch", r["text"])
            self.assertIn("show-ref", (r["cmds"] or ""),
                          "看不见的分支必须回退到权威的 show-ref")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_checkout_packed_branch(self):
        proc, base, repo = _start(self.tmp, "co_packed")
        try:
            _gmust(repo, "branch", "feature")
            _gmust(repo, "pack-refs", "--all")
            r = _post(base, "/api/checkout", {"name": "feature"})
            self.assertEqual(r["http"], 200, r["text"][:200])
            self.assertEqual(_gmust(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                             "feature")
            self.assertEqual(r["cmds"], "checkout,status")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_v4_index_stage_uses_fallback(self):
        proc, base, repo = _start(self.tmp, "v4http")
        try:
            _gmust(repo, "update-index", "--index-version", "4")
            (repo / "a0.txt").write_text("changed\n", encoding="utf-8")
            (repo / "brand_new.txt").write_text("x\n", encoding="utf-8")
            r = _post(base, "/api/stage", {"files": ["a0.txt", "brand_new.txt"]})
            self.assertEqual(r["http"], 200, r["text"][:200])
            self.assertEqual(_staged(repo), {"a0.txt", "brand_new.txt"})
            self.assertEqual(r["calls"], "3")
            self.assertEqual(r["cmds"], "ls-files,add,status")
        finally:
            proc.terminate()
            proc.wait(10)

    def test_discard_still_asks_git(self):
        """反向对照: discard 判错=删掉已跟踪文件, 所以它必须走真 ls-files。"""
        proc, base, repo = _start(self.tmp, "discard")
        try:
            (repo / "a0.txt").write_text("dirty\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
            r = _post(base, "/api/discard",
                      {"files": ["a0.txt", "untracked.txt"], "confirmed": True})
            self.assertEqual(r["http"], 200, r["text"][:200])
            self.assertEqual((repo / "a0.txt").read_text(encoding="utf-8"), "v0\n")
            self.assertFalse((repo / "untracked.txt").exists())
            self.assertEqual(r["calls"], "3")
            self.assertEqual(r["cmds"], "ls-files,checkout,status")
        finally:
            proc.terminate()
            proc.wait(10)


if __name__ == "__main__":
    import io

    class _Reporter(unittest.TextTestResult):
        """按 test_aigit.py 的行格式打印（mutation_runner 的 MARK_RE 认这个形状）。"""

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
