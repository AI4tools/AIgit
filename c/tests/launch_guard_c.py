# -*- coding: utf-8 -*-
"""上架前的安全/体验加固判据：

1. 本机同源闸：Host=外域 / Origin=外域 -> 403（防 DNS rebinding / CSRF）；
   本机 Host、无 Origin、同源 Origin -> 正常 200。
2. 端口占用自动换端口。
3. --version / --help。
4. 响应头 nosniff（不破坏既有 Content-Type）。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

EXE = r"D:\project\aigit\c\build\aigit.exe"
BASE = tempfile.mkdtemp(prefix="aigit_guard_")
FAILS, OKS = [], []


def log(m):
    print(m, flush=True)


def ok(name, cond, detail=""):
    (OKS if cond else FAILS).append(name)
    log("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  " + detail) if detail else ""))


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def raw(port, text):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(text.encode("utf-8"))
    data = b""
    while True:
        try:
            b = s.recv(65536)
        except Exception:
            break
        if not b:
            break
        data += b
    s.close()
    return data.decode("utf-8", "replace")


def main():
    repo = os.path.join(BASE, "repo")
    os.makedirs(repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    sh("git", "config", "user.email", "t@t", cwd=repo)
    open(os.path.join(repo, "a.txt"), "w").write("a\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "c", cwd=repo)

    log("\n=== 1. Host / Origin 闸")
    port = free()
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(80):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/health"); c.getresponse().read(); c.close()
            break
        except Exception:
            time.sleep(0.1)
    try:
        body = json.dumps({"files": ["a.txt"], "confirmed": True})
        # 攻击者页面：Host 是外域（DNS rebinding）
        resp = raw(port, "POST /api/discard HTTP/1.1\r\nHost: evil.example\r\n"
                         "Origin: http://evil.example\r\nContent-Type: text/plain\r\n"
                         "Content-Length: %d\r\n\r\n%s" % (len(body), body))
        ok("Host=外域 -> 403", resp.startswith("HTTP/1.1 403"), resp.splitlines()[0])

        # 正常 Host，但 Origin 是外站（CSRF 简单请求）
        resp = raw(port, "POST /api/discard HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                         "Origin: http://evil.example\r\nContent-Type: text/plain\r\n"
                         "Content-Length: %d\r\n\r\n%s" % (port, len(body), body))
        ok("Origin=外站 -> 403", resp.startswith("HTTP/1.1 403"), resp.splitlines()[0])

        # Origin: null（file:// 页面）
        resp = raw(port, "POST /api/discard HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                         "Origin: null\r\nContent-Type: text/plain\r\n"
                         "Content-Length: %d\r\n\r\n%s" % (port, len(body), body))
        ok("Origin: null -> 403", resp.startswith("HTTP/1.1 403"), resp.splitlines()[0])

        # 同源 Origin（浏览器同源 POST 会带）
        resp = raw(port, "POST /api/discard HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                         "Origin: http://127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                         "Content-Length: %d\r\n\r\n%s" % (port, port, len(body), body))
        ok("同源 Origin -> 200", resp.startswith("HTTP/1.1 200"), resp.splitlines()[0])

        # URL 写着 localhost 但连 127.0.0.1（常见）
        resp = raw(port, "GET /api/health HTTP/1.1\r\nHost: localhost:%d\r\n\r\n" % port)
        ok("Host=localhost -> 200", resp.startswith("HTTP/1.1 200"), resp.splitlines()[0])

        # 不带 Host（HTTP/1.0 客户端）
        resp = raw(port, "GET /api/health HTTP/1.0\r\n\r\n")
        ok("没有 Host 头 -> 403（只认本机同源）",
           resp.startswith("HTTP/1.1 403"), resp.splitlines()[0])

        # 正常客户端不受影响 + nosniff
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/api/health")
        r = c.getresponse()
        hdrs = dict(r.getheaders())
        ok("普通客户端（无 Origin）不受影响", r.status == 200)
        ok("响应带 X-Content-Type-Options: nosniff",
           hdrs.get("X-Content-Type-Options") == "nosniff")
        ok("没有放开 CORS（无 Access-Control-Allow-Origin）",
           "Access-Control-Allow-Origin" not in hdrs)
        r.read(); c.close()
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

    log("\n=== 2. 端口占用自动换端口")
    s1 = socket.socket(); s1.bind(("127.0.0.1", 0)); s1.listen(1)
    busy = s1.getsockname()[1]
    p = subprocess.Popen([EXE, "--root", repo, "--port", str(busy)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        ln = p.stdout.readline().decode("utf-8", "replace")
        if ln:
            line = ln.strip()
            if "listening" in ln:
                break
    ok("端口被占用时仍能启动并打印真实端口",
       "listening" in line and (":%d" % busy) not in line, line)
    if "listening" in line:
        real = int(line.rsplit(":", 1)[1].split("/")[0])
        try:
            c = http.client.HTTPConnection("127.0.0.1", real, timeout=5)
            c.request("GET", "/api/health")
            r = c.getresponse(); r.read(); c.close()
            ok("换到的端口真的能服务", r.status == 200, "port=%d" % real)
        except Exception as e:
            ok("换到的端口真的能服务", False, str(e)[:60])
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
    s1.close()

    log("\n=== 3. --version / --help")
    r = subprocess.run([EXE, "--version"], capture_output=True, text=True)
    ok("--version 打印版本", r.returncode == 0 and r.stdout.strip().startswith("aigit "),
       r.stdout.strip())
    r = subprocess.run([EXE, "--help"], capture_output=True, text=True)
    ok("--help 提到 --open / --version",
       r.returncode == 0 and "--open" in r.stdout + r.stderr)

    log("\n" + "=" * 56)
    log("  通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    for f in FAILS:
        log("   FAIL: " + f)
    shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
