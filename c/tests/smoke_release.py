# -*- coding: utf-8 -*-
"""发布产物冒烟：把 c/dist/aigit-c.zip 解到「中文 + 空格 + 括号」路径下，
按用户视角走一遍（起服务、静态资源、读端点、批量端点、写端点、加固、仓锁、
端口顺延、start.bat）。

和 conformance 的区别：那边比的是「C 与 Python 语义一致」，这边只问
「下载下来的包，用户双击能不能用」。

用法：
    python -X utf8 c/tests/smoke_release.py [zip 路径]

默认 zip 是 c/dist/aigit-c.zip（先跑 c/package.ps1）。退出码非 0 表示有 FAIL。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
ZIP = (sys.argv[1] if len(sys.argv) > 1
       else os.path.join(C_ROOT, "dist", "aigit-c.zip"))
BASE = tempfile.mkdtemp(prefix="aigit_smoke_")
OK, BAD = [], []


def ck(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %-52s %s" % ("PASS" if cond else "FAIL", name, detail))


def get(port, path, headers=None, method="GET", body=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def getj(port, path, **kw):
    st, raw = get(port, path, **kw)
    try:
        return st, json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return st, {"__raw_len__": len(raw)}


def dec_console(b):
    """exe 的控制台/日志输出应该是 UTF-8 字节（util.c::werr）；退回 GBK 兼容旧包。"""
    for enc in ("utf-8", "gbk"):
        try:
            t = b.decode(enc)
        except Exception:
            continue
        if "\ufffd" not in t:
            return t
    return b.decode("utf-8", "replace")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def kill_under(path):
    """只杀可执行文件在本目录下的 aigit（别误杀用户自己的实例）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='aigit.exe'\" | "
          "Where-Object { $_.ExecutablePath -like '%s*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
          % path.replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True)


def main():
    if not os.path.isfile(ZIP):
        print("  缺 %s（先跑 powershell -File c/package.ps1）" % ZIP)
        return 1
    dist = os.path.join(BASE, "下载 的 包", "aigit (beta)")
    os.makedirs(dist, exist_ok=True)

    print("\n=== 1. 解压（中文/空格/括号路径）")
    zipfile.ZipFile(ZIP).extractall(dist)
    exe = os.path.join(dist, "aigit.exe")
    need = ["aigit.exe", "start.bat", "README.txt", "README.md", "LICENSE",
            os.path.join("web", "index.html"),
            os.path.join("web", "virtuallist.js"),
            os.path.join("web", "conflicts-panel.js"),
            os.path.join("web", "history-panel.js")]
    missing = [n for n in need if not os.path.isfile(os.path.join(dist, n))]
    ck("解压后关键文件齐全（%d 项）" % len(need), not missing, str(missing))
    lic = open(os.path.join(dist, "LICENSE"), encoding="utf-8", errors="replace").read()
    ck("LICENSE 是 GPL-2.0",
       "GNU GENERAL PUBLIC LICENSE" in lic and "Version 2" in lic)
    rt = open(os.path.join(dist, "README.txt"), encoding="utf-8", errors="replace").read()
    ck("README.txt 是 UTF-8 中文说明",
       "aigit" in rt and any("\u4e00" <= c <= "\u9fff" for c in rt),
       "%d 字" % len(rt))

    print("\n=== 2. --version")
    r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    ck("--version 输出且 rc=0", r.returncode == 0 and "aigit" in r.stdout,
       r.stdout.strip()[:40])

    print("\n=== 3. 起服务（中文仓名 + 中文/空格文件名）")
    repo = os.path.join(BASE, "我的 仓库 (test)")
    os.makedirs(repo, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@t")
    for rel, text in (("a.txt", "base a\n"), ("中文 文件.txt", "base cn\n"),
                      (os.path.join("sub", "c.txt"), "base c\n")):
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
        open(p, "w", encoding="utf-8", newline="\n").write(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    open(os.path.join(repo, "a.txt"), "a", encoding="utf-8", newline="\n").write("edit a\n")
    open(os.path.join(repo, "中文 文件.txt"), "a", encoding="utf-8", newline="\n").write("edit cn\n")

    port = free_port()
    env = dict(os.environ, AIGIT_NO_BROWSER="1")
    proc = subprocess.Popen([exe, "--root", repo, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=dist, env=env)
    try:
        t0 = time.perf_counter()
        up = False
        for _ in range(150):
            try:
                st, _ = get(port, "/api/health")
                if st == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        ck("冷启动到可服务 < 3s", up and (time.perf_counter() - t0) < 3.0,
           "%.2fs" % (time.perf_counter() - t0))
        if not up:
            return 1

        print("\n=== 4. 首页与静态资源")
        st, raw = get(port, "/")
        html = raw.decode("utf-8", "replace")
        ck("GET / 200 且是真页面", st == 200 and len(raw) > 50000, "%d bytes" % len(raw))
        ck("首页引用了 web 资源", "virtuallist.js" in html or "history-panel" in html)
        # 静态资源挂在根路径（http.c::static_builtin），没有 /web/ 前缀
        for name in ("index.html", "virtuallist.js", "conflicts-panel.js",
                     "history-panel.js", "conflicts-demo.html"):
            st, raw = get(port, "/" + name)
            ck("GET /%s" % name, st == 200 and len(raw) > 100, "%d bytes" % len(raw))

        print("\n=== 5. 读端点（含中文/空格路径）")
        st, js = getj(port, "/api/health")
        ck("/api/health 是仓且 root 正确",
           st == 200 and js.get("isRepo") and js.get("root", "").endswith("我的 仓库 (test)"),
           js.get("root", "")[-24:])
        st, js = getj(port, "/api/status")
        paths = [f.get("path") for f in (js.get("unstaged") or [])]
        ck("/api/status 列出 2 个脏文件",
           st == 200 and sorted(paths) == sorted(["a.txt", "中文 文件.txt"]), str(paths))
        st, js = getj(port, "/api/branches")
        ck("/api/branches 有 main", st == 200 and "main" in json.dumps(js, ensure_ascii=False))
        st, js = getj(port, "/api/log?limit=5")
        ck("/api/log 有提交", st == 200 and b"base" in json.dumps(js, ensure_ascii=False).encode())
        q = urllib.parse.quote("中文 文件.txt")
        st, js = getj(port, "/api/file?file=" + q)
        ck("/api/file 读中文空格名", st == 200 and js.get("new", "").endswith("edit cn\n"),
           str(js.get("path")))
        st, js = getj(port, "/api/diff?file=a.txt")
        ck("/api/diff 有 hunk", st == 200 and "+edit a" in (js.get("diff") or ""))
        st, js = getj(port, "/api/diff?file=" + q)
        ck("/api/diff 中文空格名不被截断", st == 200 and "+edit cn" in (js.get("diff") or ""))

        print("\n=== 6. 批量端点（C 的超集）")
        files = ["a.txt", "中文 文件.txt", "sub/c.txt"]
        st, js = getj(port, "/api/diff", method="POST", body={"files": files})
        got = {f["path"]: f["diff"] for f in (js.get("files") or [])}
        ck("POST /api/diff 200 且 count=3", st == 200 and js.get("count") == 3,
           "count=%s" % js.get("count"))
        same = True
        for n in files:
            _, single = getj(port, "/api/diff?file=" + urllib.parse.quote(n))
            if got.get(n) != (single.get("diff") or ""):
                same = False
                print("       差异: %s" % n)
        ck("批量结果与逐文件逐字节相同", same)

        print("\n=== 7. 写端点")
        st, js = getj(port, "/api/stage", method="POST", body={"files": ["a.txt"]})
        ck("POST /api/stage 200", st == 200 and js.get("ok") is True, str(js.get("error"))[:40])
        st, js = getj(port, "/api/status")
        ck("stage 后 a.txt 进暂存", "a.txt" in [f.get("path") for f in (js.get("staged") or [])])
        st, js = getj(port, "/api/unstage", method="POST", body={"files": ["a.txt"]})
        ck("POST /api/unstage 200", st == 200 and js.get("ok") is True)

        print("\n=== 8. 上架加固")
        st, _ = get(port, "/api/health", headers={"Origin": "http://evil.example"})
        ck("外域 Origin -> 403", st == 403, str(st))
        st, _ = get(port, "/api/health", headers={"Origin": "null"})
        ck("Origin: null -> 403", st == 403, str(st))
        st, _ = get(port, "/api/health", headers={"Origin": "http://127.0.0.1:%d" % port})
        ck("本机同源 Origin -> 200", st == 200, str(st))
        st, js = getj(port, "/api/file?file=../x.txt")
        ck("越界路径 -> 400", st == 400, str(js.get("error"))[:30])
        st, js = getj(port, "/api/file?file=.git/config")
        ck(".git -> 400", st == 400, str(js.get("error"))[:30])
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        resp = b""
        while True:
            b = s.recv(4096)
            if not b:
                break
            resp += b
        s.close()
        head = resp.split(b"\r\n\r\n")[0].lower()
        ck("响应带 X-Content-Type-Options: nosniff",
           b"x-content-type-options: nosniff" in head)
        ck("响应不带 Access-Control-Allow-Origin",
           b"access-control-allow-origin" not in head)

        print("\n=== 9. 仓锁：同一个仓起第二个实例必须被拒")
        r2 = subprocess.run([exe, "--root", repo, "--port", str(free_port())],
                            capture_output=True, timeout=60, cwd=dist, env=env)
        msg = dec_console(r2.stderr or b"")
        ck("同仓第二实例退出（仓锁生效）", r2.returncode != 0, "rc=%s" % r2.returncode)
        if "另一个 aigit 实例" in msg:
            ck("仓锁提示是中文可读的（stderr 重定向也 UTF-8）", True,
               msg.strip().splitlines()[-1][:46])
        else:
            print("  NOTE stderr 重定向后中文不可读：%r" % msg.strip().splitlines()[-1][:60])

        print("\n=== 9b. 端口占用自动顺延（换一个仓）")
        repo2 = os.path.join(BASE, "第二个 仓库")
        os.makedirs(repo2, exist_ok=True)
        git(repo2, "init", "-q", "-b", "main")
        git(repo2, "config", "user.name", "t")
        git(repo2, "config", "user.email", "t@t")
        open(os.path.join(repo2, "x.txt"), "w", encoding="utf-8", newline="\n").write("x\n")
        git(repo2, "add", "-A")
        git(repo2, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
        p2 = subprocess.Popen([exe, "--root", repo2, "--port", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              cwd=dist, env=env)
        moved = False
        for _ in range(80):
            try:
                st, _ = get(port + 1, "/api/health")
                if st == 200:
                    moved = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        ck("被占用时顺延到 port+1", moved, "port=%d" % (port + 1))
        try:
            p2.terminate()
            p2.wait(10)
        except Exception:
            p2.kill()
        st, _ = get(port, "/api/health")
        ck("第二个实例退出后原端口仍可用", st == 200)
    finally:
        try:
            proc.terminate()
            proc.wait(10)
        except Exception:
            proc.kill()

    print("\n=== 10. start.bat（用户双击的那个）")
    bat = os.path.join(dist, "start.bat")
    txt = open(bat, encoding="utf-8", errors="replace").read()
    ck("start.bat 是纯 ASCII（PS 5.1 才稳）", all(ord(c) < 128 for c in txt),
       "%d 字" % len(txt))
    ck("start.bat 会用 --open 且失败 pause", "--open" in txt and "pause" in txt.lower())
    ck("start.bat 设了 chcp 65001（中文诊断不乱码）", "chcp 65001" in txt)
    logp = os.path.join(dist, "aigit.log")
    if os.path.isfile(logp):
        os.remove(logp)
    subprocess.Popen(["cmd", "/c", "start.bat"], cwd=dist,
                     env=dict(os.environ, AIGIT_NO_BROWSER="1"),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    port3 = None
    if os.path.isfile(logp):
        m = re.search(r"127\.0\.0\.1:(\d+)", open(logp, encoding="utf-8", errors="replace").read())
        if m:
            port3 = int(m.group(1))
    ck("start.bat 起了服务并落了日志", port3 is not None, "port=%s" % port3)
    if port3:
        st, raw = get(port3, "/")
        ck("start.bat 起的服务首页 200", st == 200, "%d bytes" % len(raw))
    kill_under(dist)
    gone = False
    for _ in range(40):
        try:
            if os.path.isfile(logp):
                os.remove(logp)
            gone = True
            break
        except PermissionError:
            time.sleep(0.25)
    ck("start.bat 起的进程可被清理（日志文件能删掉）", gone)

    print("\n------------------------------------------------------------")
    print("  通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
    for b in BAD:
        print("   FAIL: %s" % b)
    return 1 if BAD else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
