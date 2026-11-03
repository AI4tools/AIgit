#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑 c/tests 下全部 conformance_*.py 并汇总（M0 + M1 各分片）。

用法：
    python -X utf8 c/tests/run_conformance.py            # 全跑
    python -X utf8 c/tests/run_conformance.py m0 histlist # 只跑指定分片

约定：每个脚本自己起 Python 参考服务 + C 服务、自己造 fixture、自己清理；
退出码 0 才算这一片通过。本脚本只负责聚合，不改任何判据。
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def shards():
    out = []
    for path in sorted(glob.glob(os.path.join(HERE, "conformance_*.py"))):
        name = os.path.basename(path)[len("conformance_"):-len(".py")]
        out.append((name, path))
    return out


def main(argv):
    want = set(argv[1:])
    todo = [(n, p) for n, p in shards() if not want or n in want]
    if not todo:
        print("没有匹配的分片：%s" % " ".join(sorted(want)))
        print("可用：" + " ".join(n for n, _ in shards()))
        return 2

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    results = []
    t0 = time.time()
    for name, path in todo:
        print("\n" + "=" * 68)
        print("分片 %s  (%s)" % (name, path))
        print("=" * 68)
        start = time.time()
        p = subprocess.run([sys.executable, "-X", "utf8", path], env=env)
        dt = time.time() - start
        ok = (p.returncode == 0)
        results.append((name, ok, dt))
        print("-- 分片 %s: %s  %.1fs" % (name, "PASS" if ok else "FAIL", dt))

    print("\n" + "=" * 68)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, dt in results:
        print("  %-12s %-4s %6.1fs" % (name, "PASS" if ok else "FAIL", dt))
    print("  合计 %d/%d 通过  %.1fs" % (passed, total, time.time() - t0))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
