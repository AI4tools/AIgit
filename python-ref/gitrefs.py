# -*- coding: utf-8 -*-
"""直接读 `.git` 里的**引用 / reflog / 索引**，给「只读清单」类端点省掉一次 spawn。

⚠ 这不是 git 的替代品，是一个**可回退的加速器**：任何一处看不懂就返回 None，
调用方原样走原来的 git 命令。理由很实际 —— 这些端点的输出是**显示**，一旦漂移
用户看到的是错的分支、错的 stash 列表；让 git 自己回答永远是对的，快只是附加值。

不接的场合（一律返回 None，不猜）：
  * `.git` 不是目录（worktree / submodule 的 `.git` 是文件）
  * 设置了 `GIT_DIR` / `GIT_COMMON_DIR` / `GIT_INDEX_FILE`
  * `.git/reftable` 存在（git 2.45+ 的另一种 ref 存储，文件布局完全不同）
  * 任何 OSError / UnicodeDecodeError / 结构越界

等价性证据见 `test_index_fastpath.py`（差分：与 `for-each-ref` / `stash list` /
`diff --diff-filter=U` 逐项比对）。
"""

import os
import struct

_UNSAFE_INDEX_EXT = (b"link", b"sdir")


def _is_sha(text):
    t = (text or "").strip()
    return len(t) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in t)


def gitdir(root):
    """仓的 `.git` 目录；不安全/不确定时 None。"""
    if os.environ.get("GIT_DIR") or os.environ.get("GIT_COMMON_DIR"):
        return None
    gd = os.path.join(str(root), ".git")
    if not os.path.isdir(gd):
        return None
    if os.path.isdir(os.path.join(gd, "reftable")):
        return None
    return gd


def _packed(gd):
    """packed-refs → {完整 refname: sha}；读不到是 {}，格式不对是 None。"""
    out = {}
    try:
        with open(os.path.join(gd, "packed-refs"), "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#") or line.startswith("^"):
                    continue          # 注释与 peeled 行
                parts = line.split(" ", 1)
                if len(parts) != 2 or not _is_sha(parts[0]) \
                        or not parts[1].startswith("refs/"):
                    return None
                out[parts[1]] = parts[0]
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        return None
    return out


def _loose(gd, prefix, want_content=True):
    """`<gd>/<prefix>/**` 下的松散 ref → {完整 refname: 内容或 None}。"""
    base = os.path.join(gd, *prefix.split("/"))
    if not os.path.isdir(base):
        return {}
    out = {}
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, gd).replace("\\", "/")
                if not want_content:
                    out[rel] = None
                    continue
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        out[rel] = f.read().strip()
                except (OSError, UnicodeDecodeError):
                    return None
    except OSError:
        return None
    return out


def _all_refs(gd, prefix):
    """packed + loose 合并（loose 覆盖 packed）。"""
    p = _packed(gd)
    if p is None:
        return None
    l = _loose(gd, prefix)
    if l is None:
        return None
    merged = {k: v for k, v in p.items() if k.startswith(prefix + "/")}
    for k, v in l.items():
        merged[k] = v
    return merged


def head_state(root):
    """HEAD 的三态：("branch", 分支名, sha) / ("detached", None, sha) /
    ("unborn", 分支名, None)。不确定返回 None。

    对应 `git rev-parse --verify --quiet HEAD` + `rev-parse --abbrev-ref HEAD`
    两条命令的**可观察结果**（见 test_index_fastpath.py 的三态差分）。
    """
    gd = gitdir(root)
    if gd is None:
        return None
    try:
        with open(os.path.join(gd, "HEAD"), "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if raw.startswith("ref: "):
        ref = raw[5:].strip()
        if not ref.startswith("refs/heads/") or not ref[len("refs/heads/"):]:
            return None
        name = ref[len("refs/heads/"):]
        sha = resolve(gd, ref)
        if sha is None:
            return ("unborn", name, None)
        if not _is_sha(sha):
            return None
        return ("branch", name, sha)
    if _is_sha(raw):
        return ("detached", None, raw)
    return None


def resolve(gd, refname):
    """完整 refname → sha；不存在返回 None；内容不像 ref 返回 None。"""
    l = _loose(gd, "refs")
    if l is None:
        return None
    if refname in l and l[refname]:
        return l[refname].strip() if _is_sha(l[refname]) else None
    p = _packed(gd)
    if p is None:
        return None
    return p.get(refname)


def head_names(root):
    """本地分支短名列表（for-each-ref refs/heads 的顺序）。"""
    gd = gitdir(root)
    if gd is None:
        return None
    refs = _all_refs(gd, "refs/heads")
    if refs is None:
        return None
    return sorted(k[len("refs/heads/"):] for k in refs)


def head_shas(root):
    """{短名: sha}；任一条内容不是 sha 就整体返回 None（交给 git）。"""
    gd = gitdir(root)
    if gd is None:
        return None
    refs = _all_refs(gd, "refs/heads")
    if refs is None:
        return None
    out = {}
    for k, v in refs.items():
        if v is None:                 # _all_refs 只在 want_content=False 时为 None
            return None
        if not _is_sha(v):
            return None
        out[k[len("refs/heads/"):]] = v
    return out


def remote_head_branch(root):
    """远端 HEAD 真正指向的**分支名**（如 "main"）；无法判定返回 None。

    `refs/remotes/origin/HEAD` 的内容是 `ref: refs/remotes/origin/main`，
    所以分支名是去掉 `refs/remotes/<remote>/` 前缀后的那一段。

    为什么不用 `%(refname:short)`：实测它给的是 **`origin`**（远端 HEAD 的特殊
    短名规则），不是 `origin/main` —— 旧实现据此返回 `"origin"`，被调用点的
    `_branch_exists("origin")` 挡掉，等于「远端默认分支」这一支从来没生效过。
    2026-10-27 修（doc/aigit-perf.md §12.13.6）。

    远端 HEAD 若是一条**直接指向 sha** 的引用（没有符号引用），无法反推分支名 →
    返回 None，由调用方退回 main/master 猜测。
    """
    gd = gitdir(root)
    if gd is None:
        return None
    refs = _all_refs(gd, "refs/remotes")
    if refs is None:
        return None
    hits = sorted(k for k in refs if k.startswith("refs/remotes/")
                  and k.endswith("/HEAD") and k.count("/") == 3)
    if not hits:
        return None
    head_ref = hits[0]                       # refs/remotes/<remote>/HEAD
    remote_prefix = head_ref[:-len("/HEAD")] + "/"     # refs/remotes/<remote>/
    content = refs.get(head_ref)
    if not content:
        l = _loose(gd, "refs")
        content = (l or {}).get(head_ref)
    if not content or not content.startswith("ref: "):
        return None
    target = content[5:].strip()
    if not target.startswith(remote_prefix):
        return None
    branch = target[len(remote_prefix):]
    return branch or None


def remote_default(root):
    """`refs/remotes/*/HEAD` 的 **`%(refname:short)` 短名**（如 `origin`），
    或 ""（没有）/ None（不认识）。**只作兼容回退用**，不要用它判断默认分支 ——
    见 `remote_head_branch()` 里记的那条既有缺陷。
    """
    gd = gitdir(root)
    if gd is None:
        return None
    refs = _all_refs(gd, "refs/remotes")
    if refs is None:
        return None
    hits = sorted(k for k in refs if k.startswith("refs/remotes/")
                  and k.endswith("/HEAD") and k.count("/") == 3)
    if not hits:
        return ""
    return hits[0][len("refs/remotes/"):-len("/HEAD")]


def tag_refs(root):
    """标签短名 → ref 指向的对象 sha（注解 tag 是 tag 对象）。"""
    gd = gitdir(root)
    if gd is None:
        return None
    refs = _all_refs(gd, "refs/tags")
    if refs is None:
        return None
    out = {}
    for k, v in refs.items():
        if not _is_sha(v):
            return None
        out[k[len("refs/tags/"):]] = v
    return out


def head_reflog(root):
    """`.git/logs/HEAD` → [(new_sha, message)]，**旧 → 新**；读不到/不认识 → None。

    `git reflog` 的每一行就是这条记录：`<old> <new> <ident> <ts> <tz>\\t<msg>`，
    其中 `<ident>` 里带空格，所以只取前两个 token 与 tab 之后的整段。
    """
    gd = gitdir(root)
    if gd is None:
        return None
    path = os.path.join(gd, "logs", "HEAD")
    if not os.path.exists(path):
        return []                    # 没有 reflog（空仓）→ 与 git 的 [] 同义
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for line in lines:
        if not line.strip():
            continue
        head, sep, msg = line.partition("\t")
        if not sep:
            return None
        parts = head.split(" ")
        if len(parts) < 2 or not _is_sha(parts[1]):
            return None
        out.append((parts[1], msg))
    return out


def core_abbrev(root):
    """`core.abbrev` 的三种取值：整数 / "full" / None(=auto)；不确定 → False。

    只读仓级 `.git/config`：aigit 的 git 调用被 gitguard 掐掉了用户级配置，
    所以「集合上等价」。文件里出现 `[include]` 就不猜（返回 False → 回退 git）。
    """
    gd = gitdir(root)
    if gd is None:
        return False
    try:
        with open(os.path.join(gd, "config"), "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return False
    section, val, has_include = None, None, False
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("["):
            end = s.find("]")
            if end < 0:
                return False
            inner = s[1:end].split('"')[0].strip().lower()
            if inner.startswith("include"):
                has_include = True
            section = inner
            continue
        if section == "core" and "=" in s:
            k, _, v = s.partition("=")
            if k.strip().lower() == "abbrev":
                val = v.strip()
    if has_include:
        return False
    if val is None:
        return None
    low = val.lower()
    if low in ("auto", ""):
        return None
    if low in ("no", "full"):
        return "full"
    if low.isdigit() and int(low) >= 4:
        return int(low)
    return False


def stash_entries(root):
    """stash 的 reflog → [(sha, message)]，**旧 → 新**。无 stash 是 []。"""
    gd = gitdir(root)
    if gd is None:
        return None
    log = os.path.join(gd, "logs", "refs", "stash")
    if not os.path.exists(log):
        # 没有 reflog 时：连 ref 也没有 ⇒ 确实没有 stash（返回 []）；
        # ref 还在却没有 reflog ⇒ 结构不认识，交给 git。
        return [] if resolve(gd, "refs/stash") is None else None
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for line in lines:
        if not line.strip():
            continue
        head, sep, msg = line.partition("\t")
        if not sep:
            return None
        parts = head.split(" ")
        if len(parts) < 2 or not _is_sha(parts[1]):
            return None
        out.append((parts[1], msg))
    return out


def parse_index(root):
    """读 `.git/index` → (全部路径集合, 未合并路径集合)；不认识返回 None。

    未合并 = 该路径有 stage≠0 的条目（`git diff --diff-filter=U` / `ls-files -u`
    的判定口径）。版本只接 2/3；v4 前缀压缩、split-index(`link`)、
    sparse-index(`sdir`) 一律回退。
    """
    gd = gitdir(root)
    if gd is None or os.environ.get("GIT_INDEX_FILE"):
        return None
    try:
        with open(os.path.join(gd, "index"), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        # 索引不存在 = 空索引（git 的语义）。这不是「不认识」，不能白白回退。
        return set(), set()
    except OSError:
        return None
    if len(raw) < 12 or raw[:4] != b"DIRC":
        return None
    ver, count = struct.unpack(">II", raw[4:12])
    if ver not in (2, 3):
        return None
    off, names, unmerged = 12, set(), set()
    try:
        for _ in range(count):
            if off + 62 > len(raw):
                return None
            flags = struct.unpack(">H", raw[off + 60:off + 62])[0]
            stage = (flags >> 12) & 0x3
            name_len = flags & 0x0FFF
            p = off + 62
            if ver >= 3 and (flags & 0x4000):
                p += 2                   # 扩展 flags
            if name_len < 0xFFF:
                end = p + name_len
                if end >= len(raw) or raw[end] != 0:
                    return None
                name, entry_end = raw[p:end], end + 1
            else:
                end = raw.find(b"\0", p)
                if end < 0:
                    return None
                name, entry_end = raw[p:end], end + 1
            if not name:
                return None              # split-index 的「删除条目」就是空路径
            text = name.decode("utf-8")
            names.add(text)
            if stage:
                unmerged.add(text)
            off = off + ((entry_end - off + 7) & ~7)   # v2/v3 按 8 字节对齐
        while off + 8 <= len(raw):
            sig = raw[off:off + 4]
            size = struct.unpack(">I", raw[off + 4:off + 8])[0]
            if sig in _UNSAFE_INDEX_EXT:
                return None
            off += 8 + size
    except Exception:
        return None
    return names, unmerged


def unmerged_paths(root):
    """索引里未合并的路径（排序）；不认识返回 None。"""
    got = parse_index(root)
    if got is None:
        return None
    return sorted(got[1])
