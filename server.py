#!/usr/bin/env python3
"""
aigit —— 类 VSCode「源代码管理」面板的本地 git 图形工具 + 随机仓库测试数据生成器。

设计约束:
  * 零第三方依赖,只用 Python 标准库。理由:这工具要在别人机器上直接跑,
    `pip install` 本身就是失败原因。
  * **不开窗口**。启动后只打印一个 URL,由使用者自己打开浏览器。
    理由:本机同时跑着多条开发线,任何自动弹窗都会打断人干活。

安全边界(这是能执行任意 git 操作的工具,必须收紧):
  * 所有文件路径都 resolve 后校验必须在 repo root 内,否则拒绝。
    `discard` 会真的删掉工作区内容,这里放行一个 `..` 就是数据丢失。
  * 所有 git 参数走列表形式,不经 shell。
  * 只 bind 127.0.0.1。

用法:
    python server.py --root C:\\some\\repo
    python server.py --root C:\\tmp\\aigit-demo --sandbox   # 建一个干净的演练仓
"""

import argparse
import difflib
import json
import os
import random
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 测试数据生成器已抽成独立模块:它不依赖 HTTP 层,可以不起服务直接单测,
# 也可以被别的工具复用。构造方式与「可合并」的保证见 gen-notes.md。
from gen import ScenarioGen, GenError

# ---------------------------------------------------------------- git 封装

class GitError(Exception):
    pass


class Repo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lock = threading.Lock()

    # -- 底层 ------------------------------------------------------------
    def _run(self, args, check=True, stdin=None):
        with self._lock:
            p = subprocess.run(
                ["git", "-C", str(self.root)] + args,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", input=stdin,
                env={**os.environ, "GIT_PAGER": "cat",
                     "GIT_TERMINAL_PROMPT": "0"},
                timeout=120,
            )
        if check and p.returncode != 0:
            raise GitError((p.stderr or p.stdout or "git 失败").strip())
        return p.stdout

    def exists(self):
        return (self.root / ".git").exists()

    def init(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._run(["init", "-b", "main"])
        self._run(["config", "user.email", "aigit@localhost"])
        self._run(["config", "user.name", "aigit"])
        self._run(["config", "commit.gpgsign", "false"])
        # 避免 CRLF 干扰 diff 展示
        self._run(["config", "core.autocrlf", "false"])

    # -- 路径校验(关键安全点) --------------------------------------------
    def resolve_in_root(self, rel):
        """把仓库相对路径解析成绝对路径,并拒绝任何越界的输入。"""
        if rel is None:
            raise GitError("路径为空")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise GitError(f"路径越界,已拒绝:{rel}")
        return p

    # -- 查询 ------------------------------------------------------------
    def status(self):
        """返回 VSCode 风格的三段状态。"""
        out = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        entries = [e for e in out.split("\0") if e]
        staged, unstaged, conflicted = [], [], []
        for e in entries:
            if len(e) < 3:
                continue
            x, y, path = e[0], e[1], e[3:]
            # rename 格式为 "R  old -> new",统一取新路径
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if x == "?" and y == "?":
                unstaged.append({"path": path, "status": "?",
                                 "staged": False})
                continue
            item = {"path": path, "staged": True}
            if x not in (" ", "?"):
                staged.append(dict(item, status=x))
            if y not in (" ", "?"):
                unstaged.append(dict(item, status=y, staged=False))
            if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
                conflicted.append({"path": path, "status": "U"})

        branch = self._run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        staged_paths = [i["path"] for i in staged]
        return {
            "branch": branch,
            "root": str(self.root),
            "staged": staged,
            "unstaged": unstaged,
            "conflicted": conflicted,
            "hasStaged": bool(staged_paths),
        }

    def porcelain_z_parse(self, out):
        """helper: --porcelain -z 的结果已被 status() 内联处理"""
        return out

    def diff(self, path, staged=False):
        args = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        args += ["--", path]
        return self._run(args, check=False)

    def show_file(self, path, ref="HEAD"):
        p = self._run(["show", f"{ref}:{path}"], check=False)
        return None if p == "" else p

    def log(self, limit=30):
        fmt = "%H\x1f%an\x1f%ad\x1f%s\x1e"
        out = self._run(
            ["log", f"-{limit}", f"--pretty=format:{fmt}", "--date=short"],
            check=False)
        rows = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            parts = rec.split("\x1f")
            if len(parts) == 4:
                rows.append({"sha": parts[0][:8], "author": parts[1],
                             "date": parts[2], "subject": parts[3]})
        return rows

    def branches(self):
        out = self._run(
            ["for-each-ref", "--format=%(refname:short)\x1f%(HEAD)",
             "refs/heads"], check=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name, head = (line.split("\x1f") + [""])[:2]
            rows.append({"name": name, "current": head.strip() == "*"})
        return sorted(rows, key=lambda r: r["name"])

    # -- 变更操作 --------------------------------------------------------
    def stage(self, files):
        if not files:
            return
        for f in files:
            self.resolve_in_root(f)
        self._run(["add", "--"] + files)

    def unstage(self, files):
        if not files:
            return
        for f in files:
            self.resolve_in_root(f)
        self._run(["reset", "HEAD", "--"] + files)

    def discard(self, files):
        """丢弃工作区改动。这是破坏性操作 —— 先校验,再确认,最后才动手。"""
        if not files:
            return
        paths = [str(self.resolve_in_root(f)) for f in files]
        tracked, untracked = [], []
        ls = self._run(["ls-files", "-z"], check=False).split("\0")
        tracked_set = {x for x in ls if x}
        for f, ap in zip(files, paths):
            (tracked if f in tracked_set else untracked).append((f, ap))
        if tracked:
            self._run(["checkout", "--"] + [f for f, _ in tracked])
        for f, ap in untracked:
            ap = Path(ap)
            if ap.is_dir():
                import shutil
                shutil.rmtree(ap)
            elif ap.exists():
                ap.unlink()

    def commit(self, message):
        if not message.strip():
            raise GitError("提交信息不能为空")
        self._run(["add", "-A"])
        out = self._run(["commit", "-m", message], check=False)
        if not out.strip():
            raise GitError("没有可提交的改动(或提交被 hook 拒绝)")
        return out.strip()

    def checkout(self, name):
        self._run(["checkout", name])

    def new_branch(self, name):
        if not re.fullmatch(r"[\w./-]{1,100}", name or ""):
            raise GitError("分支名只允许字母数字和 _ . / -")
        self._run(["checkout", "-b", name])

    def merge(self, name):
        p = self._run(["merge", "--no-edit", name], check=False)
        return {"ok": p.returncode == 0,
                "output": (p.stdout + p.stderr).strip(),
                "conflicted": self._run(
                    ["diff", "--name-only", "--diff-filter=U"],
                    check=False).strip()}

    def abort_merge(self):
        self._run(["merge", "--abort"], check=False)

    def hard_reset(self):
        self._run(["reset", "--hard", "HEAD"], check=False)
        self._run(["clean", "-fd"], check=False)

    # -- 测试数据生成 ----------------------------------------------------
    def generate(self, kind, count=3, seed=None):
        """生成随机仓库状态。kind: mergeable / unrelated / adddel / conflict / all

        实现已搬到 gen.py(不依赖 HTTP 层)。这里只做转发 + 返回值整形:
        旧前端吃的是 ``branches=[{file, branches, expect}]`` 这个形状,
        映射一份回去免得改前端;``scenarios`` 里是更详细的每场景信息。
        """
        out = ScenarioGen(self.root).generate(kind, count, seed)
        out["branches"] = [
            {"file": ", ".join(sc["files"]),
             "branches": list(sc["branches"]),
             "expect": sc["expect"],
             "id": sc["id"],
             "verified": sc["verified"]}
            for sc in out["scenarios"]
        ]
        out["note"] = out["summary"]
        return out

    def clean_generated(self):
        """收拾掉生成器自己造的东西(只删 aigit_gen/ 分支与 aigit_gen_ 文件)。"""
        return ScenarioGen(self.root).clean()


# ---------------------------------------------------------------- HTTP

REPO: Repo = None
WEB: Path = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[aigit] " + (fmt % a) + "\n")

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _q(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._q()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json({"ok": True, **REPO.status()})
            if path == "/api/log":
                return self._json({"ok": True,
                                   "log": REPO.log(int(q.get("limit", ["30"])[0]))})
            if path == "/api/branches":
                return self._json({"ok": True, "branches": REPO.branches()})
            if path == "/api/diff":
                f = q.get("file", [""])[0]
                staged = q.get("staged", ["0"])[0] in ("1", "true", "yes")
                REPO.resolve_in_root(f)
                text = REPO.diff(f, staged=staged)
                before = REPO.show_file("HEAD", "HEAD") if False else None
                return self._json({"ok": True, "path": f, "staged": staged,
                                   "diff": text})
            if path == "/api/file":
                # 读文件当前内容,用于左右分栏视图
                f = q.get("file", [""])[0]
                ap = REPO.resolve_in_root(f)
                cur = ap.read_text(encoding="utf-8", errors="replace") \
                    if ap.exists() and ap.is_file() else None
                ref = q.get("ref", ["HEAD"])[0]
                old = REPO.show_file(f, ref)
                if old is None and ref == "HEAD":
                    old = REPO._run(["show", f":{f}"], check=False) or None
                return self._json({"ok": True, "path": f,
                                   "old": old, "new": cur})
            if path == "/api/health":
                return self._json({"ok": True, "root": str(REPO.root),
                                   "isRepo": REPO.exists()})
            return self._err("没有这个接口", 404)
        except GitError as e:
            return self._err(e, 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if path == "/api/stage":
                REPO.stage(b.get("files") or [])
            elif path == "/api/unstage":
                REPO.unstage(b.get("files") or [])
            elif path == "/api/discard":
                REPO.discard(b.get("files") or [])
            elif path == "/api/commit":
                out = REPO.commit(b.get("message", ""))
                return self._json({"ok": True, "output": out, **REPO.status()})
            elif path == "/api/checkout":
                REPO.checkout(b.get("name", ""))
            elif path == "/api/branch":
                REPO.new_branch(b.get("name", ""))
            elif path == "/api/merge":
                return self._json({"ok": True, **REPO.merge(b.get("name", ""))})
            elif path == "/api/merge-abort":
                REPO.abort_merge()
            elif path == "/api/reset":
                REPO.hard_reset()
            elif path == "/api/gen":
                return self._json({"ok": True,
                                   **REPO.generate(b.get("kind", "all"),
                                                   int(b.get("count", 3)),
                                                   b.get("seed"))})
            else:
                return self._err("没有这个接口", 404)
            return self._json({"ok": True, **REPO.status()})
        except GitError as e:
            return self._err(e, 400)
        except GenError as e:
            # 生成器的护栏拒绝(工作区不干净、分支已存在等)是**用法问题**,
            # 不是服务端故障,所以走 400。报错文案自带「怎么办」,直接透传显示。
            return self._err(e, 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)


def main():
    global REPO, WEB
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="仓库路径")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--sandbox", action="store_true",
                    help="建一个干净的演练仓(清空已有内容)")
    a = ap.parse_args()

    WEB = Path(__file__).parent / "web"
    root = Path(a.root)

    if a.sandbox and root.exists():
        import shutil
        shutil.rmtree(root)
    REPO = Repo(root)
    if not REPO.exists():
        REPO.init()
        (root / "README.md").write_text(
            "# aigit sandbox\n\n这是一个演练仓,可以随便删。\n",
            encoding="utf-8", newline="\n")
        REPO._run(["add", "-A"])
        REPO._run(["commit", "-m", "init: sandbox"])

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("=" * 56)
    print("  aigit 已启动(**没有**自动打开浏览器)")
    print(f"  仓库   {root}")
    print(f"  打开   http://127.0.0.1:{a.port}/")
    print("  停止   Ctrl+C")
    print("=" * 56)
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\naigit 已停止")


if __name__ == "__main__":
    main()
