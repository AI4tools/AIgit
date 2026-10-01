#!/usr/bin/env python3
"""
build_aigit.py —— 把 aigit 打成免安装的单文件可执行分发件。

零第三方依赖:只用标准库(zipapp / zipfile / argparse / re / pathlib)。
理由与 server.py 一致 —— 这工具要在别人机器上直接跑,`pip install` 本身就是失败原因。

产物两种,按本机能力自动选:

  1. aigit.pyz        —— zipapp 打包。**需要目标机器有 Python 3.5+。**
  2. aigit.exe        —— PyInstaller 单文件。仅当本机装了 PyInstaller 时才产出。

版本号唯一来源是 server.py 里的 AIGIT_VERSION 常量,本脚本只读取、不复制。
读不到就直接报错退出,绝不在这里另写一份 —— 两份版本号迟早分叉。

用法:
    python build_aigit.py                    # 打包到 ./dist
    python build_aigit.py --out C:\\somewhere
    python build_aigit.py --exe              # 强制要求 PyInstaller
    python build_aigit.py --check            # 只检查不打包
"""

import argparse
import ast
import os
import re
import shutil
import sys
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 入口模块。server.py 归 aigit1 所有,这里只打包不改写。
ENTRY_MODULES = ["server.py", "aigit_perf.py"]
WEB_DIR = "web"


def _imports_of(path: Path):
    """取一个 .py 文件顶层 import 的模块名(绝对导入)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module:
                out.append(node.module.split(".")[0])
    return out


def local_deps(entries, _seen=None):
    """列出要一起打包的**本地** .py(递归传递依赖)。

    不写死列表是有原因的:server.py 的本地依赖会随 aigit1..aigit7 的交付增减
    —— 它现在就 import 了 gen。写死的列表今天对、明天就漏,而漏掉的那个模块
    只在打包产物里才炸 ModuleNotFoundError,源码树里跑一切正常,极难查。
    """
    seen = _seen if _seen is not None else []
    for entry in entries:
        for name in _imports_of(HERE / entry):
            if name in seen or name in sys.stdlib_module_names:
                continue
            f = HERE / f"{name}.py"
            if not f.exists():
                continue                      # 标准库或第三方,交给解释器
            seen.append(name)
            local_deps([f], seen)              # 传递依赖
    return seen


def sources():
    """完整打包清单 = 入口 + 本地传递依赖,按确定顺序去重。"""
    out = list(ENTRY_MODULES)
    for m in local_deps(ENTRY_MODULES):
        if m + ".py" not in out:
            out.append(m + ".py")
    return out

# zipapp 的入口。它 import server、挂上性能层、再把 argv 转交出去。
# 之所以不直接 `import server; server.main()`:server.py 用
# `Path(__file__).parent / "web"` 定位前端,而 __file__ 在 zipapp 内指向 zip
# 内部,那里没有可读的文件系统。必须在运行前把 web/ 落到真实目录。
ENTRY = '''# -*- coding: utf-8 -*-
"""aigit.pyz 入口。由 build_aigit.py 生成,不要手改。"""
import hashlib
import pathlib
import sys
import tempfile
import zipfile


def _archive():
    """找出 .pyz 自己的路径。

    zipapp 里 __file__ 指向 zip **内部**(.../aigit.pyz/__main__.py),拿它算不出
    zip 本身在哪,而且 .name 恒等于 "__main__.py",不同产物会撞名。
    __loader__.archive 才是真的 zip 路径;拿不到时退回 argv[0]。
    """
    arc = getattr(globals().get("__loader__"), "archive", None)
    if arc:
        return pathlib.Path(arc)
    return pathlib.Path(sys.argv[0]).resolve()


def _extract_web():
    """把 zip 内的 web/ 解到临时目录,让 server.py 的 WEB 指向真实路径。

    server.py 用 `Path(__file__).parent / "web"` 定位前端,而 zip 内没有可读的
    文件系统,必须先落到真实目录。

    返回值是**含 index.html 的那一层**(即 <缓存目录>/web),不是缓存目录本身 ——
    server.py 之后会做 `(WEB / "index.html")`,少一层就 FileNotFoundError。
    缓存目录名带「路径哈希 + mtime」:换一版 .pyz 会自动解到新目录,不会读到
    上一版的残留页面。
    """
    z = _archive()
    key = hashlib.sha1(str(z).encode("utf-8", "replace")).hexdigest()[:12]
    try:
        key += "-%d" % int(z.stat().st_mtime)
    except OSError:
        pass
    cache = pathlib.Path(tempfile.gettempdir()) / ("aigit-web-" + key)
    if (cache / "web" / "index.html").exists():
        return cache / "web"
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(z)) as zf:
        for n in zf.namelist():
            if not n.startswith("web/") or n.endswith("/"):
                continue
            dest = cache / n
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(n))
    return cache / "web"


import server
_WEB = _extract_web()
server.WEB = _WEB
server.AIGIT_VERSION = __version__

import aigit_perf
aigit_perf.install(server, max_lines=__max_diff_lines__)


# main() 里有一句 `WEB = Path(__file__).parent / "web"`,会把上面设好的
# server.WEB 覆盖掉。zipapp 里 __file__ 是 <...>/aigit.pyz/__main__.py,
# 于是它算出 <...>/aigit.pyz/web —— 那不是目录,首页会 500。
#
# 改 module 的 __class__ 挡赋值是**没用**的:main() 里的 `WEB = ...` 编译成
# STORE_GLOBAL,直接写 module.__dict__,不经过 __setattr__。
#
# 正确的接缝在 main() 的时序里:它在第 391 行改 WEB,第 406 行才构造
# ThreadingHTTPServer,第 415 行才 serve_forever 开始收请求。所以在构造器里
# 把 WEB 改回来,是确定性的 —— 不需要线程,也不需要复制 main() 的参数解析。
_RealHTTPServer = server.ThreadingHTTPServer


def _httpserver(*args, **kwargs):
    srv = _RealHTTPServer(*args, **kwargs)
    server.WEB = _WEB          # 覆盖 main() 刚算出来的那个 zip 内路径
    return srv


server.ThreadingHTTPServer = _httpserver

if __name__ == "__main__":
    server.main()
'''


def read_version(src: Path):
    """从 server.py 里读 AIGIT_VERSION。

    用 ast 而不是正则:正则会把注释里、字符串里的同名片段也匹配上,
    ast 只认真正的模块级赋值。
    """
    if not src.exists():
        raise SystemExit(f"找不到 {src}")
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except SyntaxError as e:
        raise SystemExit(f"{src} 语法错误,无法取版本号: {e}")
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets
                       if isinstance(t, ast.Name)]
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)):
            targets = [node.target.id]
        for name in targets:
            if name == "AIGIT_VERSION" and isinstance(node.value, ast.Constant):
                val = node.value.value
                if isinstance(val, str) and val.strip():
                    return val.strip()
    raise SystemExit(
        f"{src} 里没有 AIGIT_VERSION 常量。\n"
        f"版本号的唯一来源必须是 server.py —— 本脚本不会自己写一份。\n"
        f"请在 server.py 顶部加一行(该文件归 aigit1 所有,本任务不碰):\n"
        f"    AIGIT_VERSION = \"<版本号>\"\n"
        f"注意本脚本里不含任何版本号字面量,包括这条提示 —— 两份版本号迟早分叉。")


def have_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except Exception:
        return False


def build_pyz(out: Path, version: str, max_diff_lines: int) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    stage = out / "_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for s in sources():
        shutil.copy2(HERE / s, stage / s)
    if (HERE / WEB_DIR).is_dir():
        shutil.copytree(HERE / WEB_DIR, stage / WEB_DIR)

    entry = (ENTRY
             .replace("__version__", repr(version))
             .replace("__max_diff_lines__", str(max_diff_lines)))
    (stage / "__main__.py").write_text(entry, encoding="utf-8", newline="\n")

    target = out / "aigit.pyz"
    if target.exists():
        target.unlink()
    zipapp.create_archive(stage, target=target,
                          interpreter="/usr/bin/env python3", compressed=True)
    shutil.rmtree(stage)
    return target


def build_exe(out: Path, version: str) -> Path:
    try:
        import PyInstaller.__main__ as pymain
    except Exception as e:
        raise SystemExit(
            "本机没有 PyInstaller,无法产出 .exe。\\n"
            "aigit-dist.md 记录了安装命令(需要联网,这是唯一允许装依赖的场合:\\n"
            "它只影响构建机,分发出去的 aigit.pyz 本身零依赖)。\\n"
            f"原始错误:{e}")
    out.mkdir(parents=True, exist_ok=True)
    args = ["--onefile", "--console", "--name", "aigit",
            "--add-data", f"{HERE / WEB_DIR}{os.pathsep}web",
            "--distpath", str(out), "--workpath", str(out / "_work"),
            "--specpath", str(out / "_work"),
            str(HERE / "server.py")]
    pymain.run(args)
    return out / "aigit.exe"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "dist"))
    ap.add_argument("--exe", action="store_true",
                    help="强制走 PyInstaller(没有则报错退出)")
    ap.add_argument("--no-exe", action="store_true",
                    help="只出 .pyz,即使装了 PyInstaller 也不出 .exe")
    ap.add_argument("--max-diff-lines", type=int, default=5000,
                    help="单文件 diff 超过这么多行就截断")
    ap.add_argument("--check", action="store_true", help="只检查环境与版本号")
    a = ap.parse_args()

    version = read_version(HERE / "server.py")
    files = sources()
    print(f"版本号(读自 server.py): {version}")
    print(f"本机 Python          : {sys.version.split()[0]}")
    print(f"PyInstaller          : {'有' if have_pyinstaller() else '没有'}")
    print(f"打包清单              : {', '.join(files)}")

    missing = [s for s in files if not (HERE / s).exists()]
    if missing:
        raise SystemExit(f"缺少源文件: {', '.join(missing)}")
    if not (HERE / WEB_DIR / "index.html").exists():
        raise SystemExit(f"缺少 {WEB_DIR}/index.html")

    if a.check:
        print("检查通过,未打包(--check)")
        return

    out = Path(a.out)
    pyz = build_pyz(out, version, a.max_diff_lines)
    print(f"已生成 {pyz}  ({pyz.stat().st_size} 字节)")

    if a.exe:
        exe = build_exe(out, version)
        print(f"已生成 {exe}")
    elif not a.no_exe and have_pyinstaller():
        exe = build_exe(out, version)
        print(f"已生成 {exe}")
    else:
        print("未生成 .exe —— 本机没有 PyInstaller,只出了 .pyz。")
        print("  .pyz 需要目标机器有 Python 3.5+;要真正免安装请在装有")
        print("  PyInstaller 的机器上重跑本脚本(pip install pyinstaller)。")


if __name__ == "__main__":
    main()
