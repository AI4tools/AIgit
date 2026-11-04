#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unicode 名字语义：C 必须与 Python 同码同文案。

覆盖三处曾各自实现的口径：
  * 分支名 `[\\w][\\w./-]{0,99}` —— `\\w` 是 **Unicode** alnum+`_`
    （旧 C 用 Windows CT_CTYPE1，漏认 85,535 个码点，CJK 分支名会被误拒）；
  * ref 名的「不能含空白或控制字符」—— Python 用 str.isspace()（含 U+00A0、
    U+3000、U+001C-U+001F），不是 ASCII 空白；
  * `\\w` 之外仍是 ASCII-only（`_REF_RE`），两者别混。

判据：状态码 + 响应体（含错误文案）逐字节相等。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(C_ROOT)
PY_ROOT = os.environ.get("AIGIT_PY_ROOT") or os.path.join(REPO, "python-ref")
EXE = os.environ.get("AIGIT_C_EXE") or os.path.join(C_ROOT, "build", "aigit.exe")
BASE = os.path.join(os.environ["TEMP"], "aigit_uniname_%d" % (os.getpid() % 100000))

# 合法：Unicode 字母/数字/`_`/`.`/`/`/`-`
BRANCH_OK = [
    "中文分支",                 # CJK：旧 C 的 CT_CTYPE1 判成非 \w
    "b\u00b2",                  # SUPERSCRIPT TWO：isalnum 真、isalpha 假
    "b\u2160",                  # ROMAN NUMERAL ONE (Nl)
    "ب",                        # 阿拉伯字母
    "x_1.2/y-z",                # ASCII 基线
    "a" * 100,                  # 长度上限
]
# 非法：非 \w 首字符 / 含空白（含 Unicode 空白）/ 超长
BRANCH_BAD = [
    "-b",                       # 首字符不能是 -
    ".b",                       # 首字符不能是 .
    "分支 名",                  # ASCII 空格
    "分支\u00a0名",             # NBSP
    "分支\u3000名",             # IDEOGRAPHIC SPACE
    "分支\u001c名",             # 文件分隔符（Python isspace 真）
    "a" * 101,                  # 超长
    "",                         # 空
]
# /api/file 的 ref：ASCII-only 白名单 + 「不能含空白或控制字符」
# `_check_ref`（gitops）的「不能含空白或控制字符」：走 /api/remote/ahead-behind。
# 这条路径以前 C 只按 ASCII 空白判，Unicode 空白会落到「不合法」那条文案上。
CHECKREF_CASES = [
    "x\u3000",   # IDEOGRAPHIC SPACE
    "x\u00a0",   # NBSP
    "x\u001f",   # 控制字符
    "x\u200b",   # ZERO WIDTH SPACE：不是 isspace，应过空白检查
    "-x",         # 选项注入
    "x:y",        # refspec 分隔符
    "x\u00b2",   # isalnum 真 → 合法
]

REF_CASES = [
    "分支\u3000",               # 空白检查（Python：isspace 真）
    "分支\u00a0",               # NBSP
    "分支\u001f",               # 控制字符
    "中文",                     # 非 ASCII 字母：过空白检查，但 _REF_RE 是 ASCII-only
    "main",
]


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, capture_output=True)


def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def build(d):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    sh("git", "init", "-q", cwd=d)
    sh("git", "config", "user.name", "t", cwd=d)
    sh("git", "config", "user.email", "t@t", cwd=d)
    open(os.path.join(d, "a.txt"), "w").write("a\n")
    sh("git", "add", "-A", cwd=d)
    sh("git", "commit", "-q", "-m", "c1", cwd=d)


def start(argv, repo):
    port = free()
    p = subprocess.Popen(argv + ["--root", repo, "--port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/health"); c.getresponse().read(); c.close()
            return p, port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server slow")


def norm(status, raw):
    """比语义，不比字节：C 的 JSON 是紧凑格式、Python 带空格；root 是各自的临时目录。"""
    try:
        js = json.loads(raw)
    except Exception:
        return (status, raw)
    if isinstance(js, dict):
        js.pop("root", None)
    return (status, json.dumps(js, ensure_ascii=False, sort_keys=True))


def req(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data,
              headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse(); b = r.read(); c.close()
    return norm(r.status, b.decode("utf-8", "replace"))


def run(argv, repo):
    p, port = start(argv, repo)
    try:
        out = []
        for i, name in enumerate(BRANCH_OK):
            out.append(("branch-ok-%d" % i,) + req(port, "POST", "/api/branch",
                                                   {"name": name}))
        for i, name in enumerate(BRANCH_BAD):
            out.append(("branch-bad-%d" % i,) + req(port, "POST", "/api/branch",
                                                    {"name": name}))
        for i, ref in enumerate(REF_CASES):
            q = "/api/file?file=a.txt&ref=" + urllib.parse.quote(ref)
            out.append(("ref-%d" % i,) + req(port, "GET", q))
        for i, name in enumerate(CHECKREF_CASES):
            q = "/api/remote/ahead-behind?branch=" + urllib.parse.quote(name)
            out.append(("checkref-%d" % i,) + req(port, "GET", q))
        return out
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


def main():
    dpy = os.path.join(BASE, "python")
    build(dpy)
    py = run([sys.executable, "-X", "utf8", os.path.join(PY_ROOT, "server.py")], dpy)

    dc = os.path.join(BASE, "c")
    build(dc)
    c = run([EXE], dc)
    fails = 0
    for (n1, s1, b1), (n2, s2, b2) in zip(py, c):
        if s1 != s2 or b1 != b2:
            fails += 1
            print("  FAIL %-14s python=%s %s" % (n1, s1, b1[:110]))
            print("       %-14s c     =%s %s" % ("", s2, b2[:110]))
    total = len(py)
    print("\n  %d 例，失败 %d" % (total, fails))
    # 打印几个关键用例的实际结果，便于人工核对
    for (n1, s1, b1) in py:
        if n1 in ("branch-ok-0", "branch-ok-1", "branch-bad-2", "ref-0", "ref-3"):
            print("    %-14s %s %s" % (n1, s1, b1[:90]))
    shutil.rmtree(BASE, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
