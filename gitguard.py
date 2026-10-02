#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gitguard —— 中和仓库自带 git 配置指向的程序(aigit 专用)。

背景
====
aigit 把「目录/仓库」交给不受信的来源(下载、解压、拷贝、U 盘、共享盘),
而 git 会**执行 `.git/config` 里写着的程序**。E 线实测确认日常 argv 就会触发:

  探针做法(往标记文件写一行,只测触发不测破坏)见 doc/aigit-gitguard.md。

本模块只做一件事:给一条 git argv 加上「即使仓库配置恶意也执行不到东西」的覆盖。

设计原则
========
1. **白名单,不是黑名单。** `filter.*` / `diff.*` / `merge.*` 的**子段名是任意的**,
   git 的配置里**没有通配符**。实测(见下)任何 `filter.*.clean=` 形式的通配都无效,
   所以这三族只能靠**枚举 + 逐个覆盖**。枚举读不全 = 漏,这一点在 report() 里如实返回。

2. **环境变量优先于一切。** `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`
   能注入**枚举根本看不到的** driver 名。实测:枚举只覆盖了 `filter.san` 而环境注入
   `filter.zzz` 时,`add -A` **照样命中标记**;删掉这些环境变量后才不命中。
   所以 `safe_env()` 删它们是**必需**的,不是保险。

3. **`-c` 只挡同名键。** 实测同一条键上 `-c` 压过 `GIT_CONFIG_COUNT`,
   但第 2 条说明这不是理由去留环境变量。

4. **不加 `--no-...` 之外的花招去猜。** 每条覆盖都注明「实测过 / 没实测过」。

实测结论(全部本地仓 + 私有 TEMP,无联网;git 2.50.1.windows.1)
============================================================
探针=一个只往标记文件追加一行的 .bat,被配置指向它。

  机制可用性            filter.X.clean + .gitattributes `filter=X` → 裸 `add -A` 命中  ✔
  filter.X              `-c filter.X.clean=`(空值) → 不命中, rc=0, 内容原样入 index   ✔
                       `-c filter.X.required=false` 单独用 → **照样命中且内容被改坏**  ✘
                       ⇒ 必须覆盖 clean/smudge/process 三个键, 不能只动 required
  filter.X(名字带空格)  枚举能取到 `filter.b b c.clean`,`-c filter.b b c.clean=` 语法被接受
  filter.* 通配         `-c 'filter.*.clean='` / `'filter..clean='` **全部无效**(只是造了个
                       字面名叫 `*` 的段)→ 只能枚举                                    ✘
  diff.X.textconv       裸 `diff` / `diff HEAD` / `show` / `log -p` 全部命中            ✔
                       `--no-textconv` → 四个都不命中;  `--no-ext-diff` → **照样命中**  ✔
  diff.X.command        裸 `diff` / `diff HEAD` 命中;  `--no-ext-diff` → 不命中;
                       `--no-textconv` → **照样命中**                                    ✔
                       ⇒ **两个 flag 必须同时给**,只给一个等于没给
                       `-c diff.X.textconv=`(空值) → diff 直接 rc=128 报错,
                         走不通,只能靠 flag                                             ✘
  merge.X.driver        裸 `merge --no-edit` 命中                                     ✔
                       `-c merge.X.driver=`(空值) → 不命中, rc=1, 且 `git diff
                         --diff-filter=U` 仍列出该文件(= 仍然是「有冲突」,aigit 的
                         冲突 UI 要的就是这个)                                        ✔
                         (空值会让 git 报 `cannot spawn :`,属 fail-closed)
  core.hooksPath        裸 `commit -m` 命中 pre-commit **和** post-commit;
                       `--no-verify` 只挡 pre-commit, **post-commit 照样跑** → flag 不够;
                       `-c core.hooksPath=`(空串) 与 `-c core.hooksPath=<空目录>` 都不命中
  commit.gpgsign        仓里 gpgsign=true 时 `commit -m` 会 spawn `gpg.program` 指定的
                       程序(探针记录到 argv `--status-fd=2 -bsau <key>`)              ✔
                       `-c commit.gpgsign=false` → 不再 spawn, 普通提交 rc=0 不受影响
  core.fsmonitor        裸 `status` 与裸 `add -A` **都**命中;
                       `-c core.fsmonitor=` → 两条都不命中                              ✔
  GIT_CONFIG_COUNT      能注入枚举看不到的 driver(见「原则 2」)                          ✔
  ~/.gitconfig          `HOME` 指过去时 `add -A` 命中 → 用户级配置同样是面            ✔
  `git config --list`   仓里把 alias.config / core.pager / core.fsmonitor 全指到探针,
                       跑 `git config --local --list` **不命中** → 该只读命令本身安全。
                       但本模块**运行期仍然不 spawn 任何 git** 来枚举(见 _read_config)
  core.sshCommand       `git ls-remote git@host:repo` 命中;本地路径 remote 不命中      ✔
                       `-c core.sshCommand=`(空值) → 不命中但 `unable to fork` 报错,
                         所以**联网闸关着时**才清,闸开着保留用户自己的                ✔
  credential.helper     `-c credential.helper=` **能压住**(stderr 从 `git: 'credential-...`
                       ' 变成 `could not read Username ... prompts disabled`)         ✔
                       ← 这一条 E 线没测出来(它在 DNS 就失败了),这里是本地实测
  alias.X               实测 `alias.status` / `alias.log` **劫持不了** builtin 动词
                       (git 2.50 内置动词优先)。仍然覆盖,属纵深防御,不是实测出来的面
  core.editor /         `commit -m` / 无 tty 的 `commit` / `rebase -i` 都不触发
  sequence.editor       (被 -m / --no-edit 挡住)。仍然覆盖,属纵深防御

已知残余风险(不在本模块能力范围内,report() 会报出来)
====================================================
* **枚举读不全 = 漏。** `_read_config` 是自己写的容错解析器。若配置文件用了
  include/includeIf 之外的机制,或解析失败,漏掉的 driver 名不会被覆盖。
  `report()["parse_errors"]` 非空即表示这一仓的覆盖面是打折的。
* **仓库级 `.git/config` 读不掉。** `GIT_CONFIG_NOSYSTEM` 只挡系统级,
  `GIT_CONFIG_GLOBAL`/`HOME` 只挡用户级。仓级那一半只能逐键覆盖,这就是枚举存在的原因。
* **TOCTOU**:枚举结果按 (mtime, size) 缓存。两次 git 调用之间配置被换掉会漏。
  威胁模型里配置是随目录**静态**到手,所以接受,但不是零。
* **`core.attributesFile` 只挡全局 attributes。** 树内的 `.gitattributes` 是
  **已提交文件**,clone/解压都会带,它写 `filter=X` / `merge=X` / `diff=X` 照样生效。
  也就是说:光有 `.gitattributes` **不够**,还必须有 `.git/config` 那一半 —— 门槛在 config。
* **`include.path` 里若有环 / 深度超限**,只按上限截断,可能漏。
"""

import os
import re
import sys
import tempfile
import threading

__all__ = ["safe_env", "safe_argv", "neutralize", "report", "sandbox_hooks_dir"]


# ═══════════════════════════════════════════════════════════ 沙箱目录

_SANDBOX = None
_SANDBOX_LOCK = threading.Lock()


def sandbox_hooks_dir():
    """一个**空的**临时目录,给 `core.hooksPath` 用。

    为什么不直接用 `core.hooksPath=`(空串):两者实测都不命中(见文件头 F1),
    但空串会让 git 去解析一个「空路径」,某些版本会因此报 hook 找不到(实测 rc=1)。
    给一个真实存在且为空的目录,两个风险都没有。

    只在**第一次**调用时建;模块级缓存,不会每次 git 调用都 mkdir。
    """
    global _SANDBOX
    with _SANDBOX_LOCK:
        if _SANDBOX is None:
            root = os.path.join(tempfile.gettempdir(), "aigit-gitguard")
            hooks = os.path.join(root, "hooks")
            os.makedirs(hooks, exist_ok=True)
            _SANDBOX = root
        return os.path.join(_SANDBOX, "hooks")


# ═══════════════════════════════════════════════════════════ 环境

# 必须从 os.environ 里删掉的。判据见文件头「原则 2」:实测环境注入能带进一个
# 枚举永远看不到的 driver 名,`-c` 挡不住。
_ENV_STRIP_PREFIXES = (
    "GIT_CONFIG_COUNT",     # 计数
    "GIT_CONFIG_KEY_",      # GIT_CONFIG_KEY_0 / KEY_1 / ...
    "GIT_CONFIG_VALUE_",    # GIT_CONFIG_VALUE_0 / ...
    "GIT_CONFIG_PARAMETERS",  # 老式同族注入
)

# aigit 原本就设的,行为必须保留 —— 这里只做「集中一处」,不改语义。
_ENV_KEEP = {
    "GIT_PAGER": "cat",              # 5 处原有的 capture_output 之外的第二道
    "GIT_TERMINAL_PROMPT": "0",      # 免得没有 tty 时挂死
    "GIT_EDITOR": "true",            # history_ops/conflicts 原有,server 原来**没有**(缺口)
    "GIT_SEQUENCE_EDITOR": "true",   # 同上
    "GIT_MERGE_AUTOEDIT": "no",      # conflicts 原有
    "GIT_TERGE_AUTOEDIT": "no",      # conflicts 原有
    "GIT_CONFIG_NOSYSTEM": "1",      # 系统级配置整个读不到
}


def safe_env(base=None, net_enabled=False):
    """给一条 git 调用用的环境。`base` 缺省用 `os.environ`。

    删的(实测必需):`GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*`
    —— 它们能注入枚举看不到的 driver。

    加的:见 `_ENV_KEEP`(GIT_PAGER=cat 等,**保持 aigit 原行为**),
    外加 `GIT_CONFIG_NOSYSTEM=1`。

    `net_enabled=True` 表示联网能力闸是开着的 —— 这时不碰用户级配置,
    用户的 credential.helper / core.sshCommand 继续生效(闸本来就管这个);
    `False` 时把 `GIT_CONFIG_GLOBAL` 指到一个不存在的路径,顺手把用户级
    `filter.*` / `merge.*` 那一半也关掉(实测 HOME 指空目录后 `add -A` 不命中)。
    """
    env = dict(os.environ if base is None else base)
    for k in [k for k in env if k.startswith(_ENV_STRIP_PREFIXES)]:
        env.pop(k, None)
    env.update(_ENV_KEEP)
    if not net_enabled:
        # 联网闸关着:用户级配置整个不可达。GIT_CONFIG_GLOBAL 是 git>=2.32 的
        # 精确重定向,比改 HOME 副作用小(不动 ~/.ssh、~/.git-credentials)。
        # 老 git 不认这个变量时会忽略它 —— 那就靠 enumerate() 兜住,见 report()。
        env["GIT_CONFIG_GLOBAL"] = os.path.join(
            sandbox_hooks_dir(), "__aigit_no_user_config__")
    return env


# ═══════════════════════════════════════════════════════════ 配置解析(不 spawn git)

_SEC_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$')
# 续行:值以 \ 结尾
_CONT_RE = re.compile(r'\\(.*)$')
# 兜底:哪怕整份文件解析失败,也能把危险段的段名抠出来
_SEC_LOOSE_RE = re.compile(r'\[\s*([A-Za-z0-9.-]+)\s*"((?:[^"\\]|\\.)*)"\s*\]')

_INCLUDE_DEPTH = 8


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            out.append(v[i + 1]); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def _norm(s):
    """把 `filter "aaa"` 里的 `aaa` 规范成 git 打印时的样子(小写段名、原样子段)。"""
    return s.replace("\\", "")


def _parse_config_text(text, errs, where):
    """返回 (键集合, include 路径列表)。键形如 `filter.aaa.clean` / `include.path`。"""
    keys, includes = set(), []
    section, sub = "", ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line.lstrip().startswith(";"):
            continue
        m = _SEC_RE.match(line)
        if m:
            # 段名**必须**小写化:git 的段名大小写不敏感,写出来可能是
            # `[includeIf "gitdir:..."]` / `[Filter "x"]`。不折叠的话
            # `includeIf...path` 匹配不到下面 startswith("includeif.") 那条,
            # 于是 includeIf 引进来的配置**整个看不见**(静默漏)。
            # 子段名是**大小写敏感**的,原样保留。
            section, sub = _norm(m.group(1)).lower(), _norm(m.group(2) or "")
            continue
        m = _KEY_RE.match(line)
        if m and section:
            key, val = m.group(1).lower(), m.group(2) or ""
            # 续行
            while _CONT_RE.search(val) and i < len(lines):
                val = _CONT_RE.sub(r"\1", val) + lines[i].lstrip()
                i += 1
            val = _unquote(val)
            full = "%s.%s.%s" % (section, sub, key) if sub else "%s.%s" % (section, key)
            keys.add(full)
            if full == "include.path" or (full.startswith("includeif.")
                                         and full.endswith(".path")):
                if val:
                    includes.append(val)
            continue
        # 认不出的行:不致命,但记账。report() 会把非空的 parse_errors 报出来,
        # 提醒「这一仓的枚举覆盖面是打折的」。
        errs.append("%s: %s" % (where, line.strip()[:80]))
    return keys, includes


def _git_dir(root):
    d = os.path.join(str(root), ".git")
    if os.path.isfile(d):  # worktree / submodule: .git 是个文件,里面是 gitdir: 路径
        try:
            txt = open(d, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            return d
        m = re.search(r"gitdir:\s*(.+)", txt)
        if m:
            p = m.group(1).strip()
            return p if os.path.isabs(p) else os.path.normpath(os.path.join(str(root), p))
    return d


def _home(base):
    """从**将要交给 git 的那个 env** 里解 HOME,不是从父进程 os.environ。

    这一条是实测踩出来的:父进程的 ~ 和子进程 git 看到的 ~ 可以不是同一个家目录
    (探针里就是拿 USERPROFILE/HOME 指到别处)。用父进程的 ~ 去枚举用户级配置,
    枚举的是**错误的文件** —— 覆盖会漏,而且是静默漏。
    Windows 上 expanduser 看的是 USERPROFILE(其次 HOMEDRIVE+HOMEPATH),不是 HOME。
    """
    src = os.environ if base is None else base
    for k in ("HOME", "USERPROFILE"):
        v = src.get(k)
        if v:
            return v
    drive, path = src.get("HOMEDRIVE"), src.get("HOMEPATH")
    if drive and path:
        return drive + path
    return os.path.expanduser("~")


def _config_files(root, net_enabled, base=None):
    """要读哪几份配置。顺序不影响结果(只取并集)。"""
    gd = _git_dir(root)
    files = []
    # 仓级:读不掉,只能逐键覆盖 —— 这就是枚举存在的原因
    files.append(os.path.join(gd, "config"))
    files.append(os.path.join(gd, "config.worktree"))
    if not net_enabled:
        # 联网闸关着:用户级整个不可达(GIT_CONFIG_GLOBAL 指到不存在),
        # 仍然读一遍是**纵深防御**:老 git 不认 GIT_CONFIG_GLOBAL 时靠这个兜住。
        files.append(os.path.join(
            sandbox_hooks_dir(), "__aigit_no_user_config__"))
    else:
        home = _home(base)
        files.append(os.path.join(home, ".gitconfig"))
        xdg = (os.environ if base is None else base).get("XDG_CONFIG_HOME")
        xdg = xdg or os.path.join(home, ".config")
        files.append(os.path.join(xdg, "git", "config"))
    return files


def _read_config(path, keys, errs, seen, depth=0):
    if path in seen or depth > _INCLUDE_DEPTH:
        return
    seen.add(path)
    rp = os.path.realpath(path)
    try:
        if not os.path.isfile(rp):
            return
        with open(rp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return
    k, inc = _parse_config_text(text, errs, os.path.basename(rp))
    keys |= k
    base = os.path.dirname(rp)
    for rel in inc:
        # include.path 相对**包含它的那个文件**解析
        p = rel if os.path.isabs(rel) else os.path.join(base, rel)
        _read_config(p, keys, errs, seen, depth + 1)


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _stamp(paths):
    st = []
    for p in paths:
        try:
            s = os.stat(p)
            st.append((p, s.st_mtime_ns, s.st_size))
        except OSError:
            st.append((p, None, None))
    return tuple(st)


def _collect(root, net_enabled, base=None):
    """枚举所有 driver 名。带缓存,按 (mtime, size) 失效。"""
    files = _config_files(root, net_enabled, base)
    key = (str(root), bool(net_enabled), _home(base), _stamp(files))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    keys, errs, seen = set(), [], set()
    for p in files:
        _read_config(p, keys, errs, seen)
    # 整份文件解析失败时的兜底:直接从原文抠段名
    if errs:
        for p in files:
            try:
                if os.path.isfile(p):
                    t = open(p, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for sec, sub in _SEC_LOOSE_RE.findall(t):
                if _norm(sec).lower() in ("filter", "diff", "merge", "alias"):
                    keys.add("%s.%s.__loose__" % (_norm(sec).lower(), _norm(sub)))
    val = (keys, errs)
    with _CACHE_LOCK:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = val
    return val


def _split_families(keys):
    """把键集合拆成 {族: {子段名}}。只取**有可执行键**的子段。"""
    out = {"filter": set(), "diff": set(), "merge": set(), "alias": set()}
    # 哪些键算「这个 driver 会执行程序」
    danger = {
        "filter": ("clean", "smudge", "process"),
        "diff": ("textconv", "command"),
        "merge": ("driver",),
        "alias": (),          # alias.<n> 本身整个值就是命令
    }
    for k in keys:
        parts = k.split(".")
        fam = parts[0]
        if fam not in out:
            continue
        if fam == "alias":
            if len(parts) >= 2 and parts[1]:
                out["alias"].add(".".join(parts[1:]))
            continue
        if len(parts) < 3:
            continue
        sub = ".".join(parts[1:-1])
        last = parts[-1].lower()
        if last == "__loose__":
            out[fam].add(sub)          # 兜底扫出来的段:整段盖掉
            continue
        if last in danger[fam] and sub:
            out[fam].add(sub)
    return out


# ═══════════════════════════════════════════════════════════ 覆盖

# 固定标量项。这些是**必须**的(实测触发),与仓库里有没有对应段无关 ——
# 「仓里本来干净」不等于「仓里没被换过」,所以无条件加。
def _scalar_overrides():
    return [
        # F1: hooksPath 命中 pre-commit + post-commit;--no-verify 挡不住 post-commit
        ("core.hooksPath", sandbox_hooks_dir()),
        # C2/G: status 和 add -A 都命中
        ("core.fsmonitor", ""),
        # F2: gpgsign=true 时 gpg.program 会被 spawn
        ("commit.gpgsign", "false"),
        ("gpg.program", ""),
        ("tag.gpgSign", "false"),
        # 未实测触发(G3/C7: 被 -m / --no-edit 挡住),纵深防御
        ("core.editor", ""),
        ("sequence.editor", ""),
        # 原本靠 GIT_PAGER=cat;这里再加一道。注意 core.pager 空值会让 git 报错
        ("core.pager", "cat"),
        # 树内 .gitattributes 读不掉,但全局 attributes 读得掉
        ("core.attributesFile", os.path.join(
            sandbox_hooks_dir(), "__aigit_no_attributes__")),
    ]


def _driver_overrides(fams):
    out = []
    for name in sorted(fams["filter"]):
        # 三个执行键全盖,再加 required=false 兜住「空值导致报错」的情况。
        # 只改 required 是**无效**的(实测照样命中且内容被改坏)。
        for k in ("clean", "smudge", "process"):
            out.append(("filter.%s.%s" % (name, k), ""))
        out.append(("filter.%s.required" % name, "false"))
    for name in sorted(fams["diff"]):
        # 只是双保险:真正压住的是 --no-ext-diff/--no-textconv(见 safe_argv)。
        # 单靠 -c 置空会让 diff rc=128,不能只靠这个。
        for k in ("textconv", "command"):
            out.append(("diff.%s.%s" % (name, k), ""))
        out.append(("diff.%s.cachetextconv" % name, "false"))
    for name in sorted(fams["merge"]):
        # 实测:不命中, rc=1, 且 `diff --diff-filter=U` 仍列出该文件
        # (对 aigit 的冲突 UI 而言这正是要的「有冲突」状态)
        out.append(("merge.%s.driver" % name, ""))
    for name in sorted(fams["alias"]):
        out.append(("alias.%s" % name, ""))
    return out


def neutralize(root, net_enabled=False, base=None):
    """给一条 git 命令追加的 `-c` 参数(返回 `["-c","k=v", ...]`)。

    顺序无关紧要(git 自己按后写的覆盖先写的),但固定标量在前、逐名覆盖在后,
    方便人读。

    `base` 是**将要交给 git 的那个 env**,只用来定位用户级配置文件(见 `_home`)。
    调用点应当先 `safe_env()` 拿到 env,再把同一个 env 传进来。
    """
    keys, _ = _collect(root, net_enabled, base)
    fams = _split_families(keys)
    pairs = _scalar_overrides()
    if not net_enabled:
        # 联网闸关着:这几个是**能力**,闸已经管住了,这里只做「别让仓库自己偷偷打开」。
        #   credential.helper  实测 `-c` 置空能压住(stderr 从 `git: 'credential-...'`
        #                    变成 `could not read Username ... prompts disabled`)
        #   core.sshCommand    实测能命中(`ls-remote git@host:repo` 会 spawn),但
        #                    **本地路径 remote 不命中**,所以离线也测得到触发。
        #   uploadpack.packObjectsHook / core.gitProxy
        #                    ⚠️ **没测出来**:本地路径 remote 上两者都 MISS(见
        #                    doc 的「没中和」栏)。仍然清掉,属纵深防御,
        #                    **不声称已中和**。离线无法验证(要联网)。
        pairs.append(("credential.helper", ""))
        pairs.append(("core.sshCommand", ""))
        pairs.append(("uploadpack.packObjectsHook", ""))
        pairs.append(("core.gitProxy", ""))
    pairs += _driver_overrides(fams)
    out = []
    for k, v in pairs:
        out += ["-c", "%s=%s" % (k, v)]
    return out


# ═══════════════════════════════════════════════════════════ argv

# 会被 diff/show/log 用来跑外部程序的两个 flag。**两个都必须给**:
#   diff.X.textconv  --no-ext-diff 压不住(实测命中)
#   diff.X.command   --no-textconv 压不住(实测命中)
_NO_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")
_DIFF_VERBS = ("diff", "show", "log", "whatchanged", "format-patch")


def safe_argv(argv, root=None, net_enabled=False, base=None):
    """只做第 1 步:在**子命令后面**插 `--no-ext-diff --no-textconv`。

    只对会渲染 diff 的动词插,插在子命令后面 —— 放在前面 git 报
    `unknown option`(实测 rc=129)。

    **不**在这里拼 `-c`:`-c` 是 git 全局选项,必须排在子命令**前面**,混进
    `safe_argv` 会让 `git add -A -c ...` 直接 rc=129(踩过一次)。
    需要完整 argv 的调用点用 `for_root()` / `build()`。
    """
    argv = [str(a) for a in argv]
    if not argv:
        return list(_NO_DIFF_FLAGS)
    if argv[0] in _DIFF_VERBS:
        return [argv[0]] + list(_NO_DIFF_FLAGS) + argv[1:]
    return list(argv)


def for_root(root, argv, net_enabled=False, base=None):
    """推荐入口:`argv` 是子命令列表(如 `["diff","HEAD"]`),`root` 是仓库路径。

    返回完整的 `["git", "-C", <root>, ...覆盖..., <argv...>]`,可直接喂 subprocess。
    """
    return build("git", root, argv, net_enabled, base)


def build(git_bin, root, argv, net_enabled=False, base=None, extra_pre=()):
    """**所有**调用点都该用这个。`git_bin` 允许调用方换掉 `git` 本身。

    最终形状:
        [git_bin, -C, root, *extra_pre, -c k=v ..., *子命令, --no-ext-diff, --no-textconv, ...]

    两个位置都是被实测逼出来的,别动:
      * `-c` 必须在子命令**前** —— 放后面 `git add -A -c x=y` 是 rc=129。
      * `--no-ext-diff/--no-textconv` 必须在子命令**后** —— 放前面也是 rc=129,
        而且**两个必须同时给**:只给 `--no-ext-diff` 时 `diff.<x>.textconv`
        实测照样执行(反过来也一样)。
    """
    return ([str(git_bin), "-C", str(root)]
            + list(extra_pre)
            + neutralize(root, net_enabled, base)
            + safe_argv(argv))


# ═══════════════════════════════════════════════════════════ 自检

def report(root, net_enabled=False, base=None):
    """这一仓的覆盖情况。**如实**返回,包括解析失败。"""
    keys, errs = _collect(root, net_enabled, base)
    fams = _split_families(keys)
    return {
        "config_keys": len(keys),
        "filter_names": sorted(fams["filter"]),
        "diff_names": sorted(fams["diff"]),
        "merge_names": sorted(fams["merge"]),
        "alias_names": sorted(fams["alias"]),
        "parse_errors": errs[:20],
        "parse_error_count": len(errs),
        "caveat": "枚举读不全 = 漏;仓级 .git/config 读不掉,只能逐键覆盖",
    }


if __name__ == "__main__":
    # 用法: python gitguard.py <repo>   打印这一仓会加什么覆盖
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    r = sys.argv[1]
    print("== neutralize ==")
    for i in range(0, len(neutralize(r)), 2):
        print("   -c %s" % neutralize(r)[i + 1])
    print("== report ==")
    for k, v in report(r).items():
        print("   %-20s %s" % (k, v))
