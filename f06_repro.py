#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-06 独立复现 —— 串行, 无任何线程。

A/B 对照: 同一个仓、同一个目录联接(junction)、同一份仓外哨兵
  A) server.Repo.commit()      -> 期望: 拒绝
  B) conflicts.commit_merge()  -> 期望: 也拒绝; 缺陷态下: 静默提交仓外内容

联接一律 `mklink /J`(junction), 不用符号链接 —— Python 3.11 的
os.path.islink() 对目录联接返回 False, 判定口径与真符号链接不同。

判据(不是「看起来像」, 是逐条可核):
  C1  server.Repo.commit() 在联接可见时抛 GitError      -> A 侧防线有效
  C2  commit_merge() 同样必须拒绝                       -> B 侧修好
  C3  `git ls-files` 里不得出现 escape/ 下的仓外路径
  C4  对象库里不得能 `git cat-file -p HEAD:escape/leak.txt` 读出仓外字节
  C5  仓外哨兵文件本身仍在原处(证明不是「搬走」而是真的复制进了历史)

退出码: 0 = 两条入口都拒绝(缺陷已关); 1 = 泄漏(缺陷仍在)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SECRET = b"OUTSIDE-SECRET-1234567890\n"


def git(repo, *args, binary=False):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=not binary, encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        env={**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
             "GIT_AUTHOR_NAME": "aigit", "GIT_AUTHOR_EMAIL": "a@localhost",
             "GIT_COMMITTER_NAME": "aigit", "GIT_COMMITTER_EMAIL": "a@localhost"},
    )


def sh(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def mklink_junction(link, target):
    """造**目录联接**。返回 (ok, message)。删要用 rmdir。"""
    p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def build_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # 造一个真冲突: main 改一边, side 改另一边
    git(root, "checkout", "-q", "-b", "side")
    (root / "f.txt").write_text("side\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "side")
    git(root, "checkout", "-q", "main")
    (root / "f.txt").write_text("main\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "main")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="f06-", dir=os.environ["TEMP"]))
    repo = tmp / "repo"
    outside = tmp / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "leak.txt").write_bytes(SECRET)

    print("tmp       = %s" % tmp)
    print("repo      = %s" % repo)
    print("outside   = %s" % outside)
    print("sentinel  = %r  sha256=%s" % (
        SECRET, __import__("hashlib").sha256(SECRET).hexdigest()[:16]))
    build_repo(repo)

    p = git(repo, "merge", "side", check=False) if False else git(repo, "merge", "--no-edit", "side")
    print("\nmerge rc=%d (预期非 0 = 有冲突)" % p.returncode)

    # 造联接: <repo>/escape  ->  <tmp>/outside
    link = repo / "escape"
    ok, msg = mklink_junction(link, outside)
    st = os.lstat(str(link)) if link.exists() else None
    print("mklink /J -> %s  islink=%s isdir=%s attr&0x400=%s"
          % (ok, os.path.islink(str(link)), os.path.isdir(str(link)),
             bool(getattr(st, "st_file_attributes", 0) & 0x400)))
    if not ok:
        print("mklink 失败: %s" % msg.strip())
        return 2

    # ---- A) server.Repo.commit() -------------------------------------
    import server
    import conflicts

    r = server.Repo(repo)
    leaks = r.escaping_links()
    print("\n[A] server.Repo.escaping_links() -> %d 个" % len(leaks))
    for lp, tgt in leaks:
        print("      %s -> %s" % (lp, tgt))

    a_refused, a_msg = False, ""
    try:
        r.commit("A: via server.Repo.commit")
        a_msg = "(没有抛异常 —— 提交成功了)"
    except server.GitError as e:
        a_refused = True
        a_msg = str(e).splitlines()[0]
    print("[A] server.Repo.commit(): %s -> %s"
          % ("拒绝" if a_refused else "!! 提交成功", a_msg))
    print("    A 之后 HEAD=%s" % git(repo, "rev-parse", "--short", "HEAD").stdout.strip())

    # A 在 escaping_links() 就拒了, **没有跑到 add -A** —— 所以索引/合并态原封不动,
    # B 从完全相同的仓状态出发。这正是 A/B 对照的前提。
    # ⚠ 这里**不能**用 `git reset` 复位: reset 会清掉 .git/MERGE_HEAD, 合并态就没了。
    lsf = git(repo, "ls-files").stdout.split()
    print("    A 之后 ls-files 里的 escape/* : %s"
          % [x for x in lsf if x.startswith("escape")])
    print("    A 之后 MERGE_HEAD 仍在 = %s"
          % (repo / ".git" / "MERGE_HEAD").exists())
    print("    A 之后 HEAD=%s"
          % git(repo, "rev-parse", "--short", "HEAD").stdout.strip())

    # ---- B) conflicts.commit_merge() ---------------------------------
    print("\n未合并:", git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split())
    cr = conflicts.ConflictResolver(repo)
    r1 = cr.resolve("f.txt", "resolved-by-conflicts\n")
    print("resolve -> ok=%s" % r1.get("ok"))

    b_refused, b_msg = False, ""
    b_head = ""
    try:
        r2 = cr.commit_merge("B: via conflicts.commit_merge")
        b_msg = "成功 -> %s" % r2.get("shortCommit")
        b_head = r2.get("commit") or ""
    except conflicts.ConflictError as e:
        b_refused = True
        b_msg = str(e).splitlines()[0]
    print("[B] conflicts.commit_merge(): %s" % b_msg)

    # ---- 判据 ------------------------------------------------------
    print("\n" + "=" * 72)
    lsf = git(repo, "ls-files").stdout.split()
    esc_paths = [x for x in lsf if x.startswith("escape")]
    print("ls-files 里的 escape/*        : %s" % esc_paths)

    show = git(repo, "show", "HEAD:escape/leak.txt", binary=True)
    got = show.stdout
    print("git show HEAD:escape/leak.txt rc=%d 内容=%r"
          % (show.returncode, got if show.returncode == 0 else None))

    cat = git(repo, "cat-file", "-p", "HEAD:escape/leak.txt", binary=True)
    print("git cat-file -p HEAD:escape/leak.txt rc=%d 内容=%r"
          % (cat.returncode, cat.stdout if cat.returncode == 0 else None))

    log = git(repo, "log", "-1", "--stat", "--oneline")
    print("\ngit log -1 --stat --oneline:\n%s" % log.stdout.rstrip())

    fsck = git(repo, "fsck", "--no-progress")
    print("git fsck rc=%d out=%r" % (fsck.returncode, (fsck.stdout or "")[:200]))

    sentinel_still = (outside / "leak.txt").exists()
    sentinel_bytes = (outside / "leak.txt").read_bytes() if sentinel_still else b""
    print("\n仓外哨兵仍存在=%s 内容=%r" % (sentinel_still, sentinel_bytes))

    leaked_path = bool(esc_paths)
    leaked_content = (show.returncode == 0 and SECRET in got)
    print("\nVERDICT leaked_path=%s leaked_content=%s" % (leaked_path, leaked_content))

    c1 = a_refused
    c2 = b_refused
    c3 = not leaked_path
    c4 = not leaked_content
    c5 = sentinel_still and sentinel_bytes == SECRET
    print("C1 server 侧防线有效 (A 拒绝)      : %s" % c1)
    print("C2 commit_merge 也拒绝 (缺陷已关)  : %s" % c2)
    print("C3 ls-files 无仓外路径              : %s" % c3)
    print("C4 cat-file 读不出仓外字节          : %s" % c4)
    print("C5 仓外哨兵原样保留                 : %s" % c5)

    # 收尾: 先删联接(rmdir), 再删目录 —— 顺序反了会跟进目标目录
    print("\n收尾 rmdir 联接 -> rc=%s" % sh("cmd", "/c", "rmdir", str(link)).returncode)
    print("收尾 shutil.rmtree  -> %s" % end_cleanup(tmp))

    ok_all = c1 and c2 and c3 and c4 and c5
    print("\nRESULT: %s" % ("PASS (F-06 已关)" if ok_all else "FAIL (F-06 仍可泄漏)"))
    return 0 if ok_all else 1


def end_cleanup(tmp):
    import contextlib
    try:
        for root, dirs, files in os.walk(tmp, topdown=False):
            for d in dirs:
                p = Path(root) / d
                if os.path.islink(str(p)) or _is_junction(p):
                    try:
                        os.rmdir(str(p))
                        continue
                    except OSError:
                        pass
            shutil.rmtree(tmp, ignore_errors=True)
        return shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        traceback.print_exc()
        return False


def _is_junction(p):
    try:
        return bool(getattr(os.lstat(str(p)), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())