#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C 版 HTTP 层健壮性：畸形请求不能把服务打死。

判据：
  * 200 条畸形请求（坏请求行、负/超大 Content-Length、chunked、超长头、
    NUL、截断 body）逐条发出，进程不能退出；
  * 之后 `/api/health` 仍能 200。

不要求畸形请求返回某个特定码 —— 只要求「不崩、之后还能服务」。
"""
import http.client
import os
import random
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(os.path.dirname(HERE), "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_fuzzh_%d" % (os.getpid() % 100000))

REQS = [
    b"GARBAGE\r\n\r\n",
    b"GET\r\n\r\n",
    b"GET /api/health HTTP/9.9\r\n\r\n",
    b"GET /api/health HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
    b"GET /api/health HTTP/1.1\r\nContent-Length: 99999999999999999999\r\n\r\n",
    b"POST /api/commit HTTP/1.1\r\nContent-Length: 5\r\n\r\n{bad}",
    b"POST /api/commit HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
    b"POST /api/nope HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    b"GET /" + b"A" * 70000 + b" HTTP/1.1\r\n\r\n",
    b"GET /api/file?file=" + b"%00" * 100 + b" HTTP/1.1\r\n\r\n",
    b"GET /api/health HTTP/1.1\r\n" + b"X: y\r\n" * 2000 + b"\r\n",
    b"\x00\x01\x02\x03\r\n\r\n",
]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    subprocess.run(["git", "-C", BASE, "init", "-q"], capture_output=True)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([EXE, "--root", BASE, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/api/health")
                c.getresponse().read()
                c.close()
                break
            except Exception:
                time.sleep(0.1)

        bad = 0
        for i in range(n):
            payload = random.choice(REQS)
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=3)
                c.sendall(payload)
                c.settimeout(2)
                try:
                    c.recv(200)
                except Exception:
                    pass
                c.close()
            except Exception:
                bad += 1
            if p.poll() is not None:
                print("  FAIL 服务在第 %d 条请求后退出（%r）" % (i, payload[:60]))
                return 1

        ok = False
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/api/health")
            r = c.getresponse()
            ok = r.status == 200
            r.read()
            c.close()
        except Exception:
            pass
        print("  %d 条畸形请求：连接异常 %d，服务存活 True，health 正常 %s"
              % (n, bad, ok))
        fails = (0 if ok else 1)
        print("\n  失败 %d" % fails)
        return 0 if fails == 0 else 1
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
