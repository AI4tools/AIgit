# -*- coding: utf-8 -*-
"""读面差分矩阵（2026-11-02 扩）：大文件 / 二进制 / 暂存改名 / 子模块 / 稀疏检出。

`test_vs_git.py` 覆盖的是「普通仓 + 脏状态」；这里补的是**仓的形态**：
同样的端点在这些形态下会不会给出与 plain git 不同的答案。

每项都跑**两遍**：快路径（文件直读生效）与回退路径（`GIT_DIR` 强制失效），
两遍都必须与 plain git 相等 —— 这样「快路径写错了」和「回退路径写错了」
不会互相掩盖。

运行方式：`python -X utf8 test_matrix_vs_git.py`（仓根目录）。
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

ID = ["-c", "user.name=bench", "-c", "user.email=bench@localhost"]
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


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


class _Server(object):
    def __init__(self, repo, extra_env=None):
        port = _free_port()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "server.py",
             "--root", str(repo), "--port", str(port)],
            cwd=REPO_ROOT, env=env,
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
            r = urllib.request.urlopen(self.base + path, timeout=120)
            return r.getcode(), json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}


class TestMatrixVsGit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.environ["TEMP"], "aigit_matrix_%d" % (os.getpid() % 100000))
        if os.path.isdir(cls.tmp):
            _rmtree(cls.tmp)
        os.makedirs(cls.tmp)
        cls.env = dict(os.environ)
        cfg = os.path.join(cls.tmp, "cfg_empty")
        with open(cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write("")
        cls.env["GIT_CONFIG_GLOBAL"] = cfg

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    # -- fixtures --------------------------------------------------------
    def _base(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        _must(d, "init", "-q", "-b", "main", env=self.env)
        _must(d, "config", "user.name", "bench", env=self.env)
        _must(d, "config", "user.email", "bench@localhost", env=self.env)
        _w(d, "a.txt", "a\n")
        _w(d, "sub/b.txt", "b\n")
        _must(d, "add", "-A", env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "c1", env=self.env)
        return d

    def _large(self):
        d = self._base("large")
        _w(d, "big.txt", "".join("line %05d abcdefghij\n" % i for i in range(60000)))
        _must(d, "add", "-A", env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "big", env=self.env)
        _w(d, "big.txt", "".join("line %05d CHANGED\n" % i for i in range(60000)))
        return d, "big.txt"

    def _binary(self):
        d = self._base("binary")
        _w(d, "bin.dat", "", binary=b"\x00\x01\x02hello\x00")
        _must(d, "add", "-A", env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "bin", env=self.env)
        _w(d, "bin.dat", "", binary=b"\x00\x01\x02WORLD\x00")
        return d, "bin.dat"

    def _rename(self):
        d = self._base("rename")
        _must(d, "mv", "a.txt", "a2.txt", env=self.env)
        return d, "a2.txt"

    def _submodule(self):
        d = self._base("subm")
        inner = os.path.join(self.tmp, "subm_inner")
        os.makedirs(inner, exist_ok=True)
        _must(inner, "init", "-q", "-b", "main", env=self.env)
        _must(inner, "config", "user.name", "bench", env=self.env)
        _must(inner, "config", "user.email", "bench@localhost", env=self.env)
        _w(inner, "m.txt", "m\n")
        _must(inner, "add", "-A", env=self.env)
        _must(inner, *ID, "commit", "-q", "-m", "inner", env=self.env)
        p = _g(d, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
               inner, "mod", env=self.env)
        if p.returncode != 0:
            raise unittest.SkipTest("submodule add 失败: %s" % (p.stderr or p.stdout)[:120])
        _must(d, "add", "-A", env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "add sub", env=self.env)
        _w(os.path.join(d, "mod"), "m.txt", "dirty\n")
        return d, "a.txt"

    def _sparse(self):
        d = self._base("sparse")
        _w(d, "sub2/deep.txt", "d\n")
        _must(d, "add", "-A", env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "sub2", env=self.env)
        _must(d, "sparse-checkout", "init", "--cone", "--no-sparse-index", env=self.env)
        p = _g(d, "sparse-checkout", "set", "sub2", env=self.env)
        if p.returncode != 0:
            raise unittest.SkipTest("sparse set 失败: %s" % (p.stderr or p.stdout)[:120])
        return d, "sub2/deep.txt"

    # -- 检查 ------------------------------------------------------------
    def _status_sets(self, repo, env):
        out = _must(repo, "status", "--porcelain", "-z", env=env)
        staged, unstaged, conflicted = set(), set(), set()
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
            if x == "U" or y == "U" or (x, y) in (("A", "A"), ("D", "D")):
                conflicted.add(rest)
            if x != " ":
                staged.add(rest)
            if y != " ":
                unstaged.add(rest)
        return staged, unstaged, conflicted

    def _check(self, repo, path, mode):
        extra = {"GIT_DIR": os.path.join(repo, ".git")} if mode == "fallback" else {}
        env = dict(self.env, **extra)
        s = _Server(repo, extra)
        try:
            code, st = s.get("/api/status")
            self.assertEqual(code, 200, "status HTTP %s" % code)
            staged, unstaged, conflicted = self._status_sets(repo, env)
            self.assertEqual({d["path"] for d in st["staged"]}, staged, "staged")
            self.assertEqual({d["path"] for d in st["unstaged"]}, unstaged, "unstaged")
            self.assertEqual({d["path"] for d in st["conflicted"]}, conflicted, "conflicted")

            # file：old 与 new
            code, fl = s.get("/api/file?file=" + urllib.request.quote(path))
            self.assertEqual(code, 200, "file HTTP %s" % code)
            p = _g(repo, "show", "HEAD:" + path, env=env)
            want_old = p.stdout if p.returncode == 0 else None
            if want_old == "":
                want_old = None
            if want_old is None:
                # /api/file 的既有规则：HEAD 里没有就退而读暂存区版本(:path)
                q = _g(repo, "show", ":" + path, env=env)
                if q.returncode == 0 and q.stdout != "":
                    want_old = q.stdout
            ap = os.path.join(repo, path)
            want_new = None
            if os.path.exists(ap):
                with open(ap, "r", encoding="utf-8", errors="replace") as f:
                    want_new = f.read()
            self.assertEqual(fl.get("old"), want_old, "file.old")
            self.assertEqual(fl.get("new"), want_new, "file.new")

            # diff：已知路径必须与 git 相等
            code, df = s.get("/api/diff?file=" + urllib.request.quote(path))
            self.assertEqual(code, 200, "diff HTTP %s" % code)
            want = _g(repo, "diff", "--no-color", "--no-ext-diff", "--", path,
                      env=env).stdout
            self.assertEqual(df.get("diff"), want, "diff 内容")
        finally:
            s.close()

    # -- 用例 ------------------------------------------------------------
    def _symlink_entry(self):
        """symlink **以对象形式**进 HEAD（工作区没有它）——覆盖 symlink blob 的读。

        为什么不在写面测：aigit 的 commit 契约是「先全量暂存」
        （`stage_everything_safely`），索引里有、工作区没有的条目会被 `git add`
        当成删除 —— 这与 `git add -A && git commit` 的行为一致，不是缺陷。
        所以 symlink 只在**读面**验证：HEAD 里是 120000 条目时，端点的表现。
        """
        d = self._base("symlink_obj")
        # ⚠ 必须用 **bytes** 喂 stdin：text=True 时 Python 会把 "\n" 写成
        # os.linesep（Windows 上是 CRLF），于是 blob 里凭空多出一个 \r ——
        # 那不是被测程序的问题，是 fixture 自己制造的差异（踩过）。
        p = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=d,
                           input=b"a.txt\n", capture_output=True, env=self.env)
        if p.returncode != 0:
            raise unittest.SkipTest("hash-object 失败")
        sha = p.stdout.decode("ascii").strip()
        _must(d, "update-index", "--add", "--cacheinfo",
              "120000,%s,link.txt" % sha, env=self.env)
        _must(d, *ID, "commit", "-q", "-m", "symlink entry", env=self.env)
        return d, "link.txt"

    def test_symlink_entry(self):
        d, path = self._symlink_entry()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")

    def test_large_file(self):
        d, path = self._large()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")

    def test_binary_file(self):
        d, path = self._binary()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")

    def test_staged_rename(self):
        d, path = self._rename()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")

    def test_submodule(self):
        d, path = self._submodule()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")

    def test_sparse_checkout(self):
        d, path = self._sparse()
        self._check(d, path, "fast")
        self._check(d, path, "fallback")


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
