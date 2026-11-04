#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C 版最小变异基线：证明判据真的会红，而不是「怎么改都绿」。

做法：把 c/ 复制到临时目录 -> 先跑一次未变异基线（必须绿）-> 对每个靶改一处源码
-> 重新构建 -> 跑对应分片（必须红）。全部 KILLED 才算通过。

不修改工作区源码；临时目录用完即删。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
C_ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(C_ROOT)
PY_ROOT = os.path.join(REPO, "python-ref")

MUTANTS = [
    ("status-path-split", "src/status.c",
     'size_t plen = (size_t)(f + flen - p);',
     'size_t plen = (size_t)(f + flen - p);\n'
     '            for (size_t _k = 0; _k < plen; _k++) if (p[_k] == \' \') { plen = _k; break; }',
     "conformance_m0.py"),
    ("paths-allow-dotgit", "src/paths.c",
     'if (seg_len == 4 && _wcsnicmp(relw, L".git", 4) == 0) {',
     'if (0 && seg_len == 4 && _wcsnicmp(relw, L".git", 4) == 0) {',
     "conformance_basic.py"),
    ("commit-drop-output", "src/writeutil.c",
     'if (extra_json && *extra_json) {',
     'if (0 && extra_json && *extra_json) {',
     "conformance_write_basic.py"),
]


def sh(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def build(cdir, env):
    # -B：强制全量重编。只删单个 .o 在某些时序下会留下旧对象（实测：
    # 门禁里跑出过一次 ALIVE，但同一变异单独跑 3 次都是 KILLED）。
    r = sh(["mingw32-make", "-B", "-C", cdir, "-j4"], cdir, env)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def run_shard(cdir, test, env):
    r = sh([sys.executable, "-X", "utf8", os.path.join(cdir, "tests", test)], cdir, env)
    return r.returncode, (r.stdout or "")[-800:]


def main():
    env = dict(os.environ)
    # 不要写死本机 MinGW 路径：CI 上是 msys2 的 C:\msys64\mingw64。
    mingw = os.environ.get("AIGIT_MINGW")
    if mingw:
        env["PATH"] = os.path.join(mingw, "bin") + ";" + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    tmp = tempfile.mkdtemp(prefix="aigit_mut_c_")
    cdir = os.path.join(tmp, "c")
    shutil.copytree(C_ROOT, cdir,
                    ignore=shutil.ignore_patterns("build", "dist", "__pycache__"))
    # m0 的参考服务按 dirname(C_ROOT)/python-ref 找，临时目录下也要有一份
    shutil.copytree(PY_ROOT, os.path.join(tmp, "python-ref"),
                    ignore=shutil.ignore_patterns("__pycache__"))

    fails = 0
    try:
        ok, log = build(cdir, env)
        if not ok:
            print("  FAIL 基线构建失败:\n" + log[-800:])
            return 1
        for base_test in ("conformance_basic.py", "conformance_m0.py"):
            rc, out = run_shard(cdir, base_test, env)
            print("  %-22s %s" % ("baseline(" + base_test[12:-3] + ")", "PASS" if rc == 0 else "FAIL"))
            if rc != 0:
                print(out)
                return 1

        for name, rel, old, new, test in MUTANTS:
            path = os.path.join(cdir, rel)
            src = open(path, encoding="utf-8", newline="").read()
            if old not in src:
                print("  %-22s SKIP（源码串没匹配到）" % name)
                fails += 1
                continue
            open(path, "w", encoding="utf-8", newline="").write(src.replace(old, new, 1))
            obj = os.path.join(cdir, "build",
                               os.path.splitext(os.path.basename(rel))[0] + ".o")
            if os.path.exists(obj):
                os.remove(obj)          # 防同一秒内 mtime 相同导致 make 跳过重编
            ok, log = build(cdir, env)
            if not ok:
                print("  %-22s BUILD-FAIL（不算 KILLED）" % name)
                print(log[-400:])
                fails += 1
            else:
                rc, out = run_shard(cdir, test, env)
                killed = rc != 0
                print("  %-22s %s (rc=%s)" % (name, "KILLED" if killed else "ALIVE", rc))
                if not killed:
                    fails += 1
                    print("    tail:", out[-300:].replace("\n", " | "))
            # 还原
            open(path, "w", encoding="utf-8", newline="").write(src)
            obj = os.path.join(cdir, "build",
                               os.path.splitext(os.path.basename(rel))[0] + ".o")
            if os.path.exists(obj):
                os.remove(obj)
            build(cdir, env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n  失败 %d / %d" % (fails, len(MUTANTS)))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
