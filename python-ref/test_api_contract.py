# -*- coding: utf-8 -*-
"""四条对外契约收口（2026-11-02）。

doc/aigit-perf.md §12.9 曾登记四条「不一致但没改」的口径问题。这份套件把它们钉住：

  1. `log?limit=0/-1` 曾**静默空**，而同族的 `blame` 是**夹取** —— 统一为夹到
     `[1, 1000]`（0/-1 返回 1 条，不再返回空数组）；
  2. `diff?file=<从未存在>` 曾 200 + 空 diff（与「删除态」不可区分）—— 现在 400；
     **阴性对照**：工作区删掉但已跟踪的文件必须仍然 200 且给出删除差异；
  3. `history/reflog|stashes|tags` 曾返回**裸数组**，其余端点返回 `{ok:...}` ——
     统一成 `{ok:true, reflog|stashes|tags:[...]}`（前端 asArray 两种都认）；
  4. `PUT/DELETE` 曾由基类回 **501**（「不认识这个方法」）—— 本服务明明实现了
     GET/POST，正确语义是 **405** + `Allow: GET, POST`。

运行方式：`python -X utf8 test_api_contract.py`（仓根目录）。
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
        self.repo = str(repo)
        port = _free_port()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "server.py",
             "--root", self.repo, "--port", str(port)],
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

    def req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        rq = urllib.request.Request(self.base + path, data=data,
                                    headers=headers, method=method)
        try:
            r = urllib.request.urlopen(rq, timeout=60)
            code, payload, hdr = r.getcode(), r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, payload, hdr = e.code, e.read(), e.headers
        try:
            js = json.loads(payload.decode("utf-8"))
        except Exception:
            js = None
        return code, js, hdr


class TestApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.environ["TEMP"], "aigit_contract_%d" % (os.getpid() % 100000))
        if os.path.isdir(cls.tmp):
            _rmtree(cls.tmp)
        os.makedirs(cls.tmp)
        d = os.path.join(cls.tmp, "repo")
        os.makedirs(d)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        for i in range(3):
            with open(os.path.join(d, "f%d.txt" % i), "w", encoding="utf-8", newline="\n") as f:
                f.write("v%d\n" % i)
            _gmust(d, "add", "-A")
            _gmust(d, *ID, "commit", "-q", "-m", "c%d" % (i + 1))
        with open(os.path.join(d, "tracked.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("t\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c4")
        _gmust(d, "tag", "-a", "v1", "-m", "rel")
        with open(os.path.join(d, "tracked.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("d1\n")
        _gmust(d, "stash", "push", "-q", "-m", "s1")
        cls.repo = d

    @classmethod
    def tearDownClass(cls):
        _rmtree(cls.tmp)

    # -- 1. log limit 夹取 -------------------------------------------------
    def test_log_limit_is_clamped(self):
        s = _Server(self.repo)
        try:
            for q, want in (("0", 1), ("-1", 1), ("-99", 1), ("2", 2), ("999999", 4)):
                code, js, _ = s.req("GET", "/api/log?limit=" + q)
                self.assertEqual(code, 200, q)
                self.assertEqual(len(js["log"]), want,
                                 "limit=%s 应返回 %d 条（夹到 [1,1000]）" % (q, want))
            code, js, _ = s.req("GET", "/api/log?limit=abc")
            self.assertEqual(code, 400, "非整数 limit 仍然 400")
        finally:
            s.close()

    # -- 2. diff：从未存在 -> 400；删除态 -> 200 -------------------------
    def test_diff_unknown_file_is_400(self):
        s = _Server(self.repo)
        try:
            code, js, _ = s.req("GET", "/api/diff?file=never_existed.txt")
            self.assertEqual(code, 400)
            self.assertIs(js.get("ok"), False)
            self.assertIn("文件不存在", js.get("error") or "")
        finally:
            s.close()

    def test_diff_deleted_tracked_file_still_200(self):
        """阴性对照：已跟踪但工作区删掉的文件，必须仍然给出删除差异。"""
        d = os.path.join(self.tmp, "deleted")
        os.makedirs(d)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        with open(os.path.join(d, "gone.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("g\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        os.remove(os.path.join(d, "gone.txt"))
        s = _Server(d)
        try:
            code, js, _ = s.req("GET", "/api/diff?file=gone.txt")
            self.assertEqual(code, 200, "删除态必须 200: %r" % (js,))
            self.assertIn("deleted file", js.get("diff") or "")
        finally:
            s.close()

    def test_commit_with_nothing_to_commit_is_400(self):
        """提交没有可提交内容时，必须 400 + ok:false，且 HEAD 不动。

        2026-11-02 修：`git commit` 失败时把 "nothing to commit" 打到 **stdout**，
        旧代码只判断「stdout 是否为空」，于是提交没发生却回 200 + ok:true。
        """
        d = os.path.join(self.tmp, "nocommit")
        os.makedirs(d)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("a\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        head0 = _gmust(d, "rev-parse", "HEAD").strip()
        s = _Server(d)
        try:
            code, js, _ = s.req("POST", "/api/commit", {"message": "nothing"})
            self.assertEqual(code, 400, "无可提交内容应 400: %r" % (js,))
            self.assertIs(js.get("ok"), False)
            self.assertIn("没有可提交的改动", js.get("error") or "")
        finally:
            s.close()
        self.assertEqual(_gmust(d, "rev-parse", "HEAD").strip(), head0,
                         "被拒的提交不许动 HEAD")

    def test_write_status_opt_out(self):
        """写操作可以显式省略 status（`status: false`）：少一次 spawn。

        UI 的每个写操作后面都紧跟 `refresh()`（它会再取 /api/status），所以响应里
        那份 status 是重复计算。默认仍然带（契约不变），只有 opt-out 才省。
        判据两半：① 省略时响应里没有 status 键、但有 `statusOmitted`；
        ② **操作本身仍然生效**（不是「不取 status 就顺带不干活」）。
        """
        d = os.path.join(self.tmp, "optout")
        os.makedirs(d)
        _gmust(d, "init", "-q", "-b", "main")
        _gmust(d, "config", "user.name", "bench")
        _gmust(d, "config", "user.email", "bench@localhost")
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("a\n")
        _gmust(d, "add", "-A")
        _gmust(d, *ID, "commit", "-q", "-m", "c1")
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("a2\n")
        s = _Server(d)
        try:
            code, js, _ = s.req("POST", "/api/stage", {"files": ["a.txt"], "status": False})
            self.assertEqual(code, 200, js)
            self.assertIs(js.get("ok"), True)
            self.assertNotIn("staged", js, "opt-out 时不应带 status 键")
            self.assertIs(js.get("statusOmitted"), True)
            self.assertIn("a.txt",
                          [x for x in _gmust(d, "diff", "--cached", "--name-only").split()],
                          "省略 status 不影响操作本身")
            # 默认（不传 status）仍然要带 —— 契约没变
            code, js, _ = s.req("POST", "/api/stage", {"files": ["a.txt"]})
            self.assertEqual(code, 200)
            self.assertIn("staged", js)
            self.assertNotIn("statusOmitted", js)
            # commit 的 opt-out 也要真的提交
            code, js, _ = s.req("POST", "/api/commit",
                                {"message": "opt-out commit", "status": False})
            self.assertEqual(code, 200, js)
            self.assertNotIn("staged", js)
            self.assertIn("opt-out commit", _gmust(d, "log", "-1", "--pretty=%s"))
        finally:
            s.close()

    # -- 3. 形状统一 -------------------------------------------------------
    def test_history_endpoints_are_objects(self):
        s = _Server(self.repo)
        try:
            for path, key in (("/api/history/reflog", "reflog"),
                              ("/api/history/stashes", "stashes"),
                              ("/api/history/tags", "tags")):
                code, js, _ = s.req("GET", path)
                self.assertEqual(code, 200, path)
                self.assertIsInstance(js, dict, "%s 应是对象" % path)
                self.assertIs(js.get("ok"), True, path)
                self.assertIsInstance(js.get(key), list, "%s 应有 %s 数组" % (path, key))
        finally:
            s.close()

    # -- 4. 不支持的方法 -> 405 + Allow -----------------------------------
    def test_unsupported_methods_are_405(self):
        s = _Server(self.repo)
        try:
            for m in ("PUT", "DELETE", "PATCH"):
                code, js, hdr = s.req(m, "/api/status")
                self.assertEqual(code, 405, "%s 应 405（不是 501）" % m)
                self.assertIsInstance(js, dict, m)
                self.assertIs(js.get("ok"), False, m)
                allow = (hdr.get("Allow") or "")
                self.assertIn("GET", allow, m)
                self.assertIn("POST", allow, m)
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
