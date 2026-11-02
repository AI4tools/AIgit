# -*- coding: utf-8 -*-
"""写操作 vs 真 git 的差分判据（2026-10-30 录）。

读面差分（`test_vs_git.py`）证明「看到的」一致；这份证明**「写出来的」也一致**：

    同一份 fixture 复制两份 → 一份走 aigit API、一份走 plain git → 比较最终仓库状态。

比较项不是「都返回 200」，而是：commit sha / tree sha / `ls-files -s`（索引全列）/
每个工作区文件的 sha256 / `status --porcelain` / 分支表。

身份与时间用 `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` 固定，所以**同一次提交的 sha
必须逐字相同** —— 这一条比「树一样」严格得多（消息、作者、时间任一不同就不成立）。

单独一条 **CRLF 提交**：工作区 CRLF + `core.autocrlf=true` 时通过 aigit 提交，
写进对象库的 blob **必须是 LF**。这是 §12.16 那个 phantom 改动缺陷更严重的后果：
读错了只是显示问题，写错了是**历史污染**。

运行方式：`python -X utf8 test_write_vs_git.py`（仓根目录）。
"""

import hashlib
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

ID = ["-c", "user.name=bench", "-c", "user.email=bench@localhost"]
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FIXED = {
    "GIT_AUTHOR_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_COMMITTER_DATE": "2021-03-04 05:06:07 +0800",
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@localhost",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@localhost",
}


def _g(repo, *a, env=None):
    return subprocess.run(["git"] + list(a), cwd=str(repo), capture_output=True,
                          encoding="utf-8", errors="replace", env=env)


def _must(repo, *a, env=None):
    p = _g(repo, *a, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s rc=%d %s" % (" ".join(a[:2]), p.returncode,
                                                  (p.stderr or p.stdout)[:200]))
    return p.stdout


def _w(repo, name, text, binary=None):
    p = os.path.join(str(repo), name)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    if binary is not None:
        open(p, "wb").write(binary)
    else:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


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


def _post(repo, env, path, body):
    port = _free_port()
    e = dict(env)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("PYTHONUTF8", "1")
    p = subprocess.Popen([sys.executable, "-X", "utf8", "server.py",
                          "--root", str(repo), "--port", str(port)],
                         cwd=REPO_ROOT, env=e,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1).read()
                break
            except Exception:
                if p.poll() is not None:
                    raise AssertionError("server 死了")
                time.sleep(0.25)
        else:
            raise AssertionError("server 起太慢")
        data = json.dumps(body).encode("utf-8")
        rq = urllib.request.Request(base + path, data=data,
                                    headers={"Content-Type": "application/json"},
                                    method="POST")
        try:
            r = urllib.request.urlopen(rq, timeout=120)
            return r.getcode(), json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e2:
            try:
                return e2.code, json.loads(e2.read().decode("utf-8") or "{}")
            except Exception:
                return e2.code, {}
    finally:
        p.terminate()
        p.wait(10)


def _state(repo, env):
    out = {}
    p = _g(repo, "rev-parse", "--verify", "--quiet", "HEAD", env=env)
    out["head"] = p.stdout.strip() if p.returncode == 0 else None
    p = _g(repo, "rev-parse", "HEAD^{tree}", env=env)
    out["tree"] = p.stdout.strip() if p.returncode == 0 else None
    out["index"] = sorted(x for x in _must(repo, "ls-files", "-s", env=env).splitlines() if x)
    out["status"] = sorted(x for x in _must(repo, "status", "--porcelain", env=env)
                           .replace("\r\n", "\n").split("\n") if x)
    out["branches"] = sorted(_must(repo, "for-each-ref", "--format=%(refname:short)|%(objectname)",
                                   "refs/heads", env=env).splitlines())
    files = {}
    for dirpath, dirnames, filenames in os.walk(str(repo)):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, str(repo)).replace("\\", "/")
            with open(full, "rb") as f:
                files[rel] = hashlib.sha256(f.read()).hexdigest()
    out["files"] = files
    return out


class TestWriteVsGit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.environ["TEMP"], "aigit_wvs_%d" % (os.getpid() % 100000))
        if os.path.isdir(cls.tmp):
            _rmtree(cls.tmp)
        os.makedirs(cls.tmp)
        cls.plain_env = dict(os.environ, **FIXED)
        # 空 global 配置：让两边看到的配置完全一致（不受本机 system 配置影响）
        cls.empty_cfg = os.path.join(cls.tmp, "cfg_empty")
        with open(cls.empty_cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write("")
        cls.plain_env["GIT_CONFIG_GLOBAL"] = cls.empty_cfg
        # autocrlf=true 的 global 配置（CRLF 场景用）
        cls.crlf_cfg = os.path.join(cls.tmp, "cfg_crlf")
        with open(cls.crlf_cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write("[core]\n\tautocrlf = true\n")
        cls.crlf_env = dict(cls.plain_env, GIT_CONFIG_GLOBAL=cls.crlf_cfg)

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    # -- 工具 ------------------------------------------------------------
    def _pair(self, name, env, crlf=False):
        out = []
        for tag in ("a", "b"):
            r = os.path.join(self.tmp, "%s_%s" % (name, tag))
            os.makedirs(r)
            _must(r, "init", "-q", "-b", "main", env=env)
            _must(r, "config", "user.name", "bench", env=env)
            _must(r, "config", "user.email", "bench@localhost", env=env)
            _w(r, "a.txt", "a\n")
            _w(r, "sub/b.txt", "b\n")
            _w(r, "del.txt", "d\n")
            _must(r, "add", "-A", env=env)
            _must(r, *ID, "commit", "-q", "-m", "c1", env=env)
            if crlf:
                for rel in ("a.txt", "sub/b.txt", "del.txt"):
                    os.remove(os.path.join(r, rel))
                _must(r, "checkout", "-f", "--", ".", env=env)
            out.append(r)
        return out

    def _assert_same(self, a, b, env, label):
        sa, sb = _state(a, env), _state(b, env)
        for k in sorted(set(sa) | set(sb)):
            self.assertEqual(sa.get(k), sb.get(k), "%s: %s 不一致" % (label, k))

    # -- 场景 ------------------------------------------------------------
    def test_stage(self):
        a, b = self._pair("stage", self.plain_env)
        for r in (a, b):
            _w(r, "a.txt", "a2\n")
            _w(r, "new.txt", "n\n")
        code, j = _post(a, self.plain_env, "/api/stage", {"files": ["a.txt", "new.txt"]})
        self.assertEqual(code, 200, j)
        _must(b, "add", "--", "a.txt", "new.txt", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "stage")

    def test_unstage(self):
        a, b = self._pair("unstage", self.plain_env)
        for r in (a, b):
            _w(r, "a.txt", "a2\n")
            _w(r, "sub/b.txt", "b2\n")
            _g(r, "add", "-A", env=self.plain_env)
        code, j = _post(a, self.plain_env, "/api/unstage", {"files": ["a.txt"]})
        self.assertEqual(code, 200, j)
        _must(b, "reset", "-q", "HEAD", "--", "a.txt", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "unstage")

    def test_commit(self):
        a, b = self._pair("commit", self.plain_env)
        for r in (a, b):
            _w(r, "a.txt", "a2\n")
            _w(r, "new.txt", "n\n")
            os.remove(os.path.join(r, "del.txt"))
        code, j = _post(a, self.plain_env, "/api/commit", {"message": "msg one"})
        self.assertEqual(code, 200, j)
        _must(b, "add", "-A", env=self.plain_env)
        _must(b, *ID, "commit", "-q", "-m", "msg one", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "commit")
        # 最强的一条：固定身份/时间后，commit sha 必须逐字相同
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         _must(b, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         "同一次提交的 sha 不一致")

    def test_discard(self):
        a, b = self._pair("discard", self.plain_env)
        for r in (a, b):
            _w(r, "a.txt", "a2\n")
            _w(r, "untracked.txt", "u\n")
        code, j = _post(a, self.plain_env, "/api/discard",
                        {"files": ["a.txt", "untracked.txt"], "confirmed": True})
        self.assertEqual(code, 200, j)
        _must(b, "checkout", "--", "a.txt", env=self.plain_env)
        os.remove(os.path.join(b, "untracked.txt"))
        self._assert_same(a, b, self.plain_env, "discard")

    def test_checkout(self):
        a, b = self._pair("checkout", self.plain_env)
        for r in (a, b):
            _must(r, "checkout", "-q", "-b", "feature", env=self.plain_env)
            _w(r, "f.txt", "feature\n")
            _must(r, "add", "-A", env=self.plain_env)
            _must(r, *ID, "commit", "-q", "-m", "f1", env=self.plain_env)
            _must(r, "checkout", "-q", "main", env=self.plain_env)
        code, j = _post(a, self.plain_env, "/api/checkout", {"name": "feature"})
        self.assertEqual(code, 200, j)
        _must(b, "checkout", "-q", "feature", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "checkout")

    def test_commit_crlf_stores_lf(self):
        """CRLF 工作区 + autocrlf=true：对象库里的 blob 必须是 LF。"""
        env = self.crlf_env
        a, b = self._pair("crlf", env, crlf=True)
        for r in (a, b):
            with open(os.path.join(r, "a.txt"), "wb") as f:
                f.write("a2\r\nsecond\r\n".encode("utf-8"))
        for r in (a, b):
            self.assertIn(b"\r\n", open(os.path.join(r, "a.txt"), "rb").read(),
                          "前置：工作区应是 CRLF")
        code, j = _post(a, env, "/api/commit", {"message": "crlf commit"})
        self.assertEqual(code, 200, j)
        _must(b, "add", "-A", env=env)
        _must(b, *ID, "commit", "-q", "-m", "crlf commit", env=env)
        blob_a = _must(a, "cat-file", "-p", "HEAD:a.txt", env=env)
        self.assertNotIn("\r", blob_a, "对象库里的 blob 含 CR —— CRLF 被写进了历史")
        self._assert_same(a, b, env, "commit_crlf")
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=env).strip(),
                         _must(b, "rev-parse", "HEAD", env=env).strip(),
                         "CRLF 场景下同一次提交的 sha 不一致")


    def test_commit_unicode_paths(self):
        """中文 + 空格路径的提交：两边树必须相同（quotePath / pathspec 编码都在这条上）。"""
        a, b = self._pair("uni", self.plain_env)
        for r in (a, b):
            _w(r, "中文 目录/文 件.txt", "u\n")
            _w(r, "with space.txt", "s\n")
            _w(r, "a.txt", "a2\n")
        code, j = _post(a, self.plain_env, "/api/commit", {"message": "unicode paths"})
        self.assertEqual(code, 200, j)
        _must(b, "add", "-A", env=self.plain_env)
        _must(b, *ID, "commit", "-q", "-m", "unicode paths", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "commit_unicode")
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         _must(b, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         "中文/空格路径下同一次提交的 sha 不一致")

    def test_branch_create(self):
        a, b = self._pair("branch", self.plain_env)
        code, j = _post(a, self.plain_env, "/api/branch", {"name": "feat/x"})
        self.assertEqual(code, 200, j)
        _must(b, "checkout", "-q", "-b", "feat/x", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "branch_create")


    def test_merge_ff(self):
        a, b = self._pair("mergeff", self.plain_env)
        for r in (a, b):
            _must(r, "checkout", "-q", "-b", "feature", env=self.plain_env)
            _w(r, "f.txt", "feature\n")
            _must(r, "add", "-A", env=self.plain_env)
            _must(r, *ID, "commit", "-q", "-m", "f1", env=self.plain_env)
            _must(r, "checkout", "-q", "main", env=self.plain_env)
        code, j = _post(a, self.plain_env, "/api/merge", {"name": "feature"})
        self.assertEqual(code, 200, j)
        _must(b, "merge", "--no-edit", "feature", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "merge_ff")
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         _must(b, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         "快进合并后的 sha 不一致")

    def _conflict_pair(self, name, env):
        out = []
        for tag in ("a", "b"):
            r = os.path.join(self.tmp, "%s_%s" % (name, tag))
            os.makedirs(r)
            _must(r, "init", "-q", "-b", "main", env=env)
            _must(r, "config", "user.name", "bench", env=env)
            _must(r, "config", "user.email", "bench@localhost", env=env)
            _w(r, "c.txt", "base\n")
            _must(r, "add", "-A", env=env)
            _must(r, *ID, "commit", "-q", "-m", "base", env=env)
            _must(r, "checkout", "-q", "-b", "side", env=env)
            _w(r, "c.txt", "side\n")
            _must(r, "add", "-A", env=env)
            _must(r, *ID, "commit", "-q", "-m", "side", env=env)
            _must(r, "checkout", "-q", "main", env=env)
            _w(r, "c.txt", "main\n")
            _must(r, "add", "-A", env=env)
            _must(r, *ID, "commit", "-q", "-m", "main", env=env)
            _g(r, "merge", "side", env=env)
            out.append(r)
        return out

    def test_conflict_resolve_and_commit(self):
        """冲突解决 + 提交：树/父提交必须与 git 相同。

        ⚠ 消息**故意不同**：aigit 的文档化契约是「自带消息时自动补
        `Merged: <分支>` 行」（doc/aigit-conflicts.md:147）。所以这里**不比 sha**，
        而是显式钉住那条 trailer —— 它要是丢了，这条会红。
        """
        env = self.plain_env
        a, b = self._conflict_pair("conflict", env)
        code, j = _post(a, env, "/api/conflict/resolve",
                        {"file": "c.txt", "content": "resolved\n"})
        self.assertEqual(code, 200, j)
        code, j = _post(a, env, "/api/conflict/commit", {"message": "merge msg"})
        self.assertEqual(code, 200, j)
        _w(b, "c.txt", "resolved\n")
        _must(b, "add", "--", "c.txt", env=env)
        _must(b, *ID, "commit", "-q", "-m", "merge msg", env=env)

        ca = _must(a, "cat-file", "-p", "HEAD", env=env)
        cb = _must(b, "cat-file", "-p", "HEAD", env=env)
        self.assertEqual(ca.split("parent")[0].strip(), cb.split("parent")[0].strip(),
                         "合并提交的 tree 不一致")
        self.assertEqual([x for x in ca.splitlines() if x.startswith("parent ")],
                         [x for x in cb.splitlines() if x.startswith("parent ")],
                         "父提交不一致")
        msg_a = ca.split("\n\n", 1)[-1]
        msg_b = cb.split("\n\n", 1)[-1]
        self.assertEqual(msg_a, msg_b + "\nMerged: side\n",
                         "Merged trailer 契约变了（文档见 doc/aigit-conflicts.md）")

    def test_commit_large_file(self):
        a, b = self._pair("large", self.plain_env)
        big = "".join("line %05d abcdefghij\n" % i for i in range(60000))
        for r in (a, b):
            _w(r, "big.txt", big)
        code, j = _post(a, self.plain_env, "/api/commit", {"message": "big commit"})
        self.assertEqual(code, 200, j)
        _must(b, "add", "-A", env=self.plain_env)
        _must(b, *ID, "commit", "-q", "-m", "big commit", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "commit_large")
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         _must(b, "rev-parse", "HEAD", env=self.plain_env).strip())

    def test_commit_binary_file(self):
        a, b = self._pair("binary", self.plain_env)
        for r in (a, b):
            with open(os.path.join(r, "bin.dat"), "wb") as f:
                f.write(b"\x00\x01\x02hello\x00")
        code, j = _post(a, self.plain_env, "/api/commit", {"message": "binary commit"})
        self.assertEqual(code, 200, j)
        _must(b, "add", "-A", env=self.plain_env)
        _must(b, *ID, "commit", "-q", "-m", "binary commit", env=self.plain_env)
        self._assert_same(a, b, self.plain_env, "commit_binary")
        self.assertEqual(_must(a, "rev-parse", "HEAD", env=self.plain_env).strip(),
                         _must(b, "rev-parse", "HEAD", env=self.plain_env).strip())


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
