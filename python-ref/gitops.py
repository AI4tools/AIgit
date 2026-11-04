#!/usr/bin/env python3
"""gitops —— aigit 的**可选**远程能力:一个能力闸 + 几个远程 git 操作。

═════════════════════════════════════════════════════════════════════
先说清楚一件事:这个文件里**一行网络代码都没有,而且不应该有**
═════════════════════════════════════════════════════════════════════
传输是 `git` 二进制自己做的。HTTPS 的证书校验、SSH 私钥、凭据助手
(`credential.helper`)、代理、协议白名单(`GIT_ALLOW_PROTOCOL`)——全部在
`git` 里。所以「支持远程」要做的不是「实现远程」,而是:

    开一道闸 → 把一个 argv 交给 git

依据(不是印象,是可以查的):
  * aigit 的硬约束是**零第三方依赖**(见 server.py 的模块 docstring)。
    自己实现 TLS / 凭据助手,既做不到「别人的机器上直接跑」,也没有意义。
  * 唯一可能需要自己判断的「远程能不能连」,对应的是
    `git ls-remote`(只读子命令)。它同样是调 git,不需要 socket。
  * 所以下面 `import` 的全是 `json/os/re/subprocess/threading/pathlib`,
    **一个联网模块都没有**。`test_aigit_net.py` 用 `sys.modules` 实测这一点。

═════════════════════════════════════════════════════════════════════
分级:不是「联网 on/off」一刀切
═════════════════════════════════════════════════════════════════════
aigit 现有的危险操作(amend / revert / reset)至少都留在本地、可以用
reflog 捞回来。**push 不是** —— 推出去就撤不回来(除非远程允许 force,
而那又是另一层危险)。所以:

  off(默认)  任何远程动词都不许跑
  read       ls-remote / fetch / ls 远程分支 —— 只往本地写 ref,可撤销
  write      push / pull —— 会写到仓库之外,必须显式开启 + 显式确认

**write 没有任何默认值。** 它必须同时满足两件事才动得了:
    1. 闸开到 write(显式,默认 off)
    2. 请求带 `confirmed=True`(沿用 history_ops 的 plan/do 两步协议)

漏掉任何一件都是拒绝,而且拒绝发生在**任何 git 子进程被 fork 之前**。

═════════════════════════════════════════════════════════════════════
「关着就是真关着」是怎么保证的
═════════════════════════════════════════════════════════════════════
不是靠「记得检查」,是靠两件事:

1. **白名单,不是黑名单。** 判定分四步(见 `_verb_level`):
     LOCAL_FORMS[动词] 命中 argv[1]      -> local
     REMOTE_FORMS[动词] 命中形态特征       -> 那一级
     LOCAL_VERBS                        -> local
     REMOTE_VERBS                       -> 那一级
     **都不在**                          -> 直接拒绝(哪怕闸开到 write)
   将来有人加一个新动词,必须**故意**改这几张表之一 —— 忘了改会立刻红,
   而不是静悄悄开个洞。

2. **闸只有一个执行点**,就是 `_run()`。文件里没有第二处拼远程 argv 的地方。
   test_aigit_net.py 用 AST 扫全仓每一个 .py 反证这条:产品代码里构造远程
   argv 的地方只有 gitops.py。

3. **write 级动词有第二道锁**:`_run` 自己再查一遍 `confirmed is True`。
   防的是「后来者新写了个 do_xxx 忘了调 `_check_bool`」。

`gitops.py` 不 import 任何 aigit 自己的模块(实测只 import 7 条标准库),
所以它与别的线的改动不耦合;接线路由只在 server.py 里加 `/api/remote/*`。
"""

import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

# 注意:**本模块不 import 任何 aigit 自己的模块**(test_aigit_net.py 的 c3 用
# ast.walk 检查,函数内部的 import 也会被算进去)。这是有意的隔离:gitops 是
# 「唯一碰远程的地方」,它的 import 面越小越好审。
# 配置中和由**调用方注入**(GitOps(..., guard=gitguard)),见下方 __init__。
# 没有注入时走 _fb_*(纯标准库的降级档),它比 gitguard 少一层「枚举」——
# 差别见 _fb_build 的注释。

__all__ = [
    "OFF", "READ", "WRITE", "LEVELS",
    "LOCAL_VERBS", "LOCAL_FORMS", "REMOTE_VERBS", "REMOTE_FORMS",
    "NetError", "NetGate", "GitOps",
]


# ═══════════════════════════════════════════════════════ 分级常量

OFF = "off"
READ = "read"
WRITE = "write"
LEVELS = (OFF, READ, WRITE)


# P1 计数钩子(doc/aigit-perf.md §12.2): 由 server 注入
# (`_gitops.COUNT_HOOK = server._count_hook`), 记每次 spawn 的子命令名。
# 默认 None(独立使用零开销); 本模块按自己的规矩不 import aigit 模块,
# 闸(NetGate)与中和层(guard)照旧 —— 这里只补账不碰闸。
COUNT_HOOK = None


class NetError(Exception):
    """本层所有可预期失败的唯一出口。带 code,让调用方靠 code 分支而不是 match 中文。"""

    def __init__(self, message, code="net_error", **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra

    def to_dict(self):
        d = {"ok": False, "error": self.message, "code": self.code}
        d.update(self.extra)
        return d


def _need(cond, message, code="net_error", **extra):
    if not cond:
        raise NetError(message, code, **extra)


# ═══════════════════════════════════════════════════════ 动词表(闸的依据)
#
# 这两张表是整个闸的全部知识。**它们是白名单**:不在 LOCAL_VERBS 里的动词,
# 一律当成「可能联网」处理。
#
# LOCAL_VERBS 收得很紧 —— 只收本模块与 aigit 其余部分**真的用到**的、
# 且能一句话说清「它只读/只写本地」的动词。多收一个可能联网的动词就是一个洞,
# 少收一个只是多写一行。
LOCAL_VERBS = frozenset({
    "add", "am", "apply", "blame", "branch", "cat-file", "check-ignore",
    "check-ref-format", "cherry-pick", "clean", "commit", "commit-tree",
    "config", "count-objects", "describe", "diff", "diff-index", "diff-tree",
    "for-each-ref", "fsck", "grep", "hash-object", "init", "log", "ls-files",
    "ls-tree", "merge", "merge-base", "merge-file", "merge-tree", "mv",
    "name-rev", "notes", "rev-list", "rev-parse", "rm", "reset", "restore",
    "revert", "shortlog", "show", "show-ref", "stash", "status", "symbolic-ref",
    "tag", "update-index", "update-ref", "var", "verify-commit", "whatchanged",
    "write-tree", "version", "help", "reflog",
})

# 远程动词 → 需要哪一级。**不在 LOCAL_VERBS / LOCAL_FORMS 里的动词,
# 一律进这张表查闸; 两张表都没有的动词 -> 直接拒绝, 闸开到 write 也不放行。**
#
# 分级的判据不是「危不危险」这种形容词,而是两个能查证的问题:
#     「它写不写到仓库之外?」「出事了能不能靠 reflog 捞回来?」
#   * ls-remote 纯读, 连本地都不写。read。
#   * fetch 只写本地 ref(remote-tracking + FETCH_HEAD), `git reflog` 能看到,
#     要撤销就 `git reset --hard origin/<branch>`。所以是 read。
#   * push 写远程。write。**不可撤销** —— 传输层没有「撤回」这个概念。
#   * pull = fetch + merge, 会改工作区。归 write。它跟 push 同级不是因为
#     「一样危险」, 而是因为两者都必须显式确认后才动手。
#
# 这张表里有些动词本模块根本不调用(clone / daemon / http-backend ...)。
# 它们在表里是**故意**的: 将来有人扩 gitops.py, 这些动词默认就是被拦住的,
# 必须先想清楚级别、再从表里放出来, 而不是「忘了加检查」。
#
# 刻意**不**收进来的: filter-branch / maintenance / bundle —— 它们的全部
# 形态都是纯本地, 不该出现在这张表里凭空要求确认。gitops 不调用它们,
# 所以它们也不在 LOCAL_VERBS 里(=> 落到 _verb_level 的「不认识 -> 拒绝」)。
REMOTE_VERBS = {
    "ls-remote": READ,
    "fetch": READ,
    "fetch-pack": READ,
    "remote-ext": READ,
    "remote-fd": READ,
    "push": WRITE,
    "pull": WRITE,
    "clone": WRITE,
    "send-pack": WRITE,
    "upload-pack": WRITE,
    "receive-pack": WRITE,
    "daemon": WRITE,
    "http-backend": WRITE,
    # submodule 的 update/sync/add 都会 fetch。本模块不调用 submodule 任何
    # 形态, 所以连 status 那种纯本地用法也一并拦掉 —— 保守的代价是多写一行。
    "submodule": WRITE,
    "credential": WRITE,      # 凭据助手会跟外面打交道
    "credential-cache": WRITE,
    "credential-store": WRITE,
    "request-pull": WRITE,
}

# ── 按**形态**判定的动词 ─────────────────────────────────────────────
#
# 上面的表按动词名判定, 解决不了「同一个动词, 带不带某个子命令完全是两回事」。
# 下面两张表补这个洞:
#
#   LOCAL_FORMS[verb]  = 该动词里**确定纯本地**的形态(看 argv[1] 的字面值)
#   REMOTE_FORMS[verb] = {形态特征: 需要的级别}
#
# 两者都没命中的形态 -> 拒绝。这依然是白名单, 不是黑名单。
LOCAL_FORMS = {
    # `git remote` 读/写的是**本地 config**(.git/config), 不联网。
    # 真正会联系远程的只有 show / update 两个子命令(见 REMOTE_FORMS)。
    "remote": {"", "-v", "--verbose", "add", "remove", "rm", "rename",
               "set-url", "get-url", "set-head", "set-branches",
               # prune 只删本地 stale 跟踪 ref, 不联系远程(git 文档明说
               # "does not require the remote to be up-to-date")
               "prune", "-h", "--help"},
}

REMOTE_FORMS = {
    # `remote show <name>` 不带 -n 时会联系远程; `remote update` == fetch 全部
    "remote": {"show": READ, "update": READ},
    # `git archive --remote=<url> HEAD` 是「从远程取对象」的偷渡口,
    # 所以只有带 --remote / --exec 的形态才算远程
    "archive": {"--remote": READ, "--exec": READ},
}


# ═══════════════════════════════════════════════════════ 能力闸

_ENV_VAR = "AIGIT_NET"

# 环境变量里可写的别名 —— 故意只有这几个, 且都映射到同一组语义。
_ENV_ALIAS = {
    "0": OFF, "off": OFF, "none": OFF, "no": OFF, "false": OFF, "": OFF,
    "1": READ, "read": READ, "on": READ, "yes": READ, "true": READ, "ro": READ,
    "2": WRITE, "write": WRITE, "rw": WRITE, "push": WRITE,
}


class NetGate(object):
    """能力闸。默认 **off**,而且「读不懂」也按 off 处理(fail closed)。

    为什么不 fail loud:这个闸把守的是**不可撤销**的操作。一个拼错的
    `AIGIT_NET=raed` 如果被当成 write,那是把用户没打算开的东西打开了;
    如果被当成 off,后果只是「我以为开了远程但它不工作」—— 用户立刻会发现。
    两者不对称,所以选 fail closed。同时把「为什么降级了」记进 `describe()`,
    不让它变成静默的意外。

    优先级: 显式构造参数 > 环境变量 AIGIT_NET > **off**
    """

    def __init__(self, level=None, source="explicit", allow_env=True):
        self.note = None
        self.source = "default"
        resolved = None

        if level is None and allow_env:
            raw = os.environ.get(_ENV_VAR)
            if raw is not None:
                self.source = "env:%s" % _ENV_VAR
                resolved = _ENV_ALIAS.get(raw.strip().lower())
                if resolved is None:
                    # fail closed, 但记下原因
                    resolved = OFF
                    self.note = ("%s=%r 读不懂(只认 %s),已按 off 处理。"
                                 % (_ENV_VAR, raw,
                                    "/".join(sorted(set(_ENV_ALIAS.values())))))
        if level is not None:
            if not isinstance(level, str) or level.strip().lower() not in LEVELS:
                # 显式传错 = 编程错误, 这里不静默
                raise NetError("未知的能力级别 %r,只允许 %s"
                               % (level, "/".join(LEVELS)), code="bad_level")
            resolved = level.strip().lower()
            self.source = source or "explicit"
        if resolved is None:
            resolved = OFF
            self.source = "default"

        self.level = resolved

    # -- 查询 ----------------------------------------------------------
    def allows(self, need):
        return LEVELS.index(self.level) >= LEVELS.index(need)

    def require(self, need, op=None):
        """闸的唯一执行点。**在 fork 任何子进程之前**调用。"""
        if not isinstance(need, str) or need not in LEVELS:
            raise NetError("内部错误:能力级别 %r 不合法" % (need,),
                           code="bad_level")
        if self.allows(need):
            return
        raise NetError(
            "远程能力「%s」当前是 **%s**,拒绝执行%s。"
            "aigit 默认不联网;要开就显式设 %s=%s(本进程默认 %s)。"
            % (need, self.level,
               ("(%s)" % op) if op else "",
               _ENV_VAR, need if need != OFF else OFF, self.source),
            code="net_disabled", required=need, current=self.level,
            op=op)

    def describe(self):
        read_ops = sorted(o for o, n in REMOTE_VERBS.items() if n == READ)
        write_ops = sorted(o for o, n in REMOTE_VERBS.items() if n == WRITE)
        for verb, forms in REMOTE_FORMS.items():
            for form, lvl in forms.items():
                (read_ops if lvl == READ else write_ops).append(
                    "%s %s" % (verb, form))
        d = {"level": self.level, "source": self.source,
             "env_var": _ENV_VAR,
             "levels": list(LEVELS),
             "allows_read": self.allows(READ),
             "allows_write": self.allows(WRITE),
             "read_ops": sorted(read_ops),
             "write_ops": sorted(write_ops),
             "local_verbs": len(LOCAL_VERBS)}
        if self.note:
            # 降级过就必须留痕, 否则「我明明设了 write 却没生效」会变成
            # 一个查不出原因的灵异现象。
            d["note"] = self.note
        return d


# ═══════════════════════════════════════════════════════ 参数校验
#
# argv 是**列表**形式传给 subprocess 的(没有 shell), 所以参数里不会出现
# 「空格分词」那一类注入。但**选项注入**还在:`git push --receive-pack=<cmd>
# <remote>` 会让 git 去执行 <cmd>。所以名字类参数一律过白名单正则。

_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _check_remote_name(name):
    _need(isinstance(name, str) and _REMOTE_NAME_RE.fullmatch(name),
          "远程名不合法:%r(只允许字母数字与 . _ -, 字母数字开头, 最长 100)"
          % (name,), code="bad_remote")
    return name


_REF_RE = re.compile(r"(?:refs/(?:heads|tags)/)?[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _check_ref(ref, what="ref"):
    """分支 / ref 名。拒绝 `-` 开头(选项注入)与 `:`(refspec 分隔符)。

    `:` 要拒是因为调用方要删远程分支时**不给**用 `:branch` 这种 refspec ——
    见 `do_push(delete=True)`:删除是一个显式开关,不是一个要解析的字符串。
    """
    _need(isinstance(ref, str), "%s 必须是字符串,收到 %s"
          % (what, type(ref).__name__), code="bad_ref")
    _need(not ref.startswith("-"),
          "%s 不能以 - 开头(git 会把它当选项读):%r" % (what, ref),
          code="bad_ref")
    _need(":" not in ref,
          "%s 不能含 ':'(那是 refspec 的分隔符, 会让调用方越权指定 ref):%r"
          % (what, ref), code="bad_ref")
    _need(not any(ch.isspace() or ord(ch) < 0x20 for ch in ref),
          "%s 不能含空白或控制字符:%r" % (what, ref), code="bad_ref")
    _need(_REF_RE.fullmatch(ref),
          "%s 不合法:%r" % (what, ref), code="bad_ref")
    return ref


def _check_bool(confirmed, op):
    """`confirmed` 只认**真的** True —— 与 history_ops._require_confirm 同一把尺子。

    用 `bool(x)` 收字段的话, `"confirmed": "false"` 在 Python 里是真的,
    于是「前端把 false 序列化成字符串」这个极常见的 bug 就变成一次误推,
    而误推撤不回来。
    """
    _need(confirmed is True,
          "「%s」会写到仓库之外,不可撤销。必须先调 plan_push 看清将推送哪些提交,"
          "再显式传 confirmed=True。这里没有一步到位的入口。" % op,
          code="needs_confirmation", op=op)


# ═══════════════════════════════════════════════════════ GitOps

class _FallbackGuard(object):
    """**降级档**:没有注入 gitguard 时的最小中和(纯标准库)。

    ⚠️ 它比 gitguard 少一样关键东西:**枚举**。
    gitguard 会读 `.git/config` 把 `filter.*` / `diff.*` / `merge.*` 的
    **子段名**逐个抠出来覆盖;git 的配置里**没有通配符**(实测 `-c 'filter.*.clean='`
    只是造了个字面名叫 `*` 的段,完全无效),所以枚举是那三族唯一的办法。

    降级档只能覆盖「键名固定」的那几条。`filter.<n>.clean` / `diff.<n>.command` /
    `merge.<n>.driver` 这类**仍然会执行**(除非 diff/show/log 那三个动词拿到 flag)。

    所以降级档是「别把事情做得更糟」,**不是等价替代**。
    生产路径一律由 server.py 注入 gitguard。
    """

    _DIFF_VERBS = ("diff", "show", "log", "whatchanged", "format-patch")
    _FLAGS = ("--no-ext-diff", "--no-textconv")

    @staticmethod
    def _sandbox():
        d = os.path.join(tempfile.gettempdir(), "aigit-gitguard")
        os.makedirs(os.path.join(d, "hooks"), exist_ok=True)
        return d

    @classmethod
    def _pairs(cls, net):
        d = cls._sandbox()
        p = [("core.hooksPath", os.path.join(d, "hooks")),
             ("core.fsmonitor", ""),
             ("commit.gpgsign", "false"),
             ("gpg.program", ""),
             ("tag.gpgSign", "false"),
             ("core.editor", ""),
             ("sequence.editor", ""),
             ("core.pager", "cat"),
             ("core.attributesFile", os.path.join(d, "__no_attributes__"))]
        if not net:
            p += [("credential.helper", ""), ("core.sshCommand", ""),
                  ("uploadpack.packObjectsHook", ""), ("core.gitProxy", "")]
        return p

    @classmethod
    def build(cls, git_bin, root, argv, net_enabled=False, base=None, extra_pre=()):
        argv = [str(a) for a in argv]
        if argv and argv[0] in cls._DIFF_VERBS:
            argv = [argv[0]] + list(cls._FLAGS) + argv[1:]
        out = [str(git_bin), "-C", str(root)] + list(extra_pre)
        for k, v in cls._pairs(net_enabled):
            out += ["-c", "%s=%s" % (k, v)]
        return out + argv

    @staticmethod
    def safe_env(base=None, net_enabled=False):
        env = dict(os.environ if base is None else base)
        for k in [k for k in env
                  if k.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_",
                                   "GIT_CONFIG_VALUE_", "GIT_CONFIG_PARAMETERS"))]:
            env.pop(k, None)
        env.update({"GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true",
                    "GIT_MERGE_AUTOEDIT": "no", "GIT_TERGE_AUTOEDIT": "no",
                    "GIT_CONFIG_NOSYSTEM": "1"})
        if not net_enabled:
            env["GIT_CONFIG_GLOBAL"] = os.path.join(
                tempfile.gettempdir(), "aigit-gitguard", "__no_user_config__")
        return env


class GitOps(object):
    """对一个本地仓库的远程操作。构造**不**跑任何 git。"""

    def __init__(self, root, gate=None, git_bin="git", timeout=120, guard=None):
        self.root = Path(root).resolve()
        self.gate = gate if gate is not None else NetGate()
        self.git_bin = git_bin
        self.timeout = timeout
        self._lock = threading.Lock()
        # `guard` 是一个和 gitguard 同形状的模块(有 safe_env / build)。
        # **由调用方注入**(server.py 传 gitguard),因为本模块不允许 import
        # aigit 自己的模块(见文件头)。为 None 时用下面的 _fb_* 降级档。
        self.guard = guard if guard is not None else _FallbackGuard

    # -- 底层 ----------------------------------------------------------
    def _verb_level(self, args):
        """这个 argv 要哪一级能力?返回 (level, reason); level=None 表示拒绝。

        判定顺序(先具体后笼统):
          1. LOCAL_FORMS 命中 argv[1]      -> local
          2. REMOTE_FORMS 命中形态特征       -> 那一级
          3. LOCAL_VERBS                   -> local
          4. REMOTE_VERBS                  -> 那一级
          5. 都不在                         -> 拒绝(**即使闸开到 write**)

        第 5 条是整个闸的兜底: 将来有人写了个新动词忘了加检查, 这里拦住。
        """
        op = args[0] if args else ""
        rest = args[1] if len(args) > 1 else ""
        if op in LOCAL_FORMS and rest in LOCAL_FORMS[op]:
            return "local", "git %s %r 是纯本地形态" % (op, rest)
        if op in REMOTE_FORMS:
            # ⚠️ 这里必须同时认 `--remote x` 和 `--remote=x` 两种写法。
            #    第一版只比字面量, 于是 `--remote=origin` 整条溜过去了 ——
            #    而 `--remote=<name>` 恰恰是 git 文档里写的那种写法。
            #    实测: 闸关着时 `git archive --remote=origin HEAD` 被 fork
            #    了出去, 远程侧的 git upload-archive 真的跑了。
            #    一个能力闸漏在"选项带不带等号"上, 等于没闸。
            for key, lvl in REMOTE_FORMS[op].items():
                for a in args[1:]:
                    if a == key or a.startswith(key + "="):
                        return lvl, "git %s %s(=) 会联网" % (op, key)
            return "local", "git %s 不带联网形态(%r)" % (op, rest)
        if op in LOCAL_VERBS:
            return "local", "本地动词"
        if op in REMOTE_VERBS:
            return REMOTE_VERBS[op], "远程动词表"
        return None, ("不在本地动词白名单、也不在远程动词表里 —— "
                      "拒绝。将来要支持它,必须**故意**加进其中一张表。")

    def _run(self, args, check=True, stdin=None, timeout=None, confirmed=False):
        """**唯一**拼 git argv 并执行的地方。闸就在这里, fork 之前。

        这是「关着就是真关着」的机械保证: 整个模块里没有第二处 subprocess,
        所以「有没有远程动作」等价于「有没有走到这里」。

        `confirmed` 在这里是**第二道**,不是唯一一道: 即使将来有人新写了个
        `do_xxx()` 忘了调 `_check_bool`, 只要它跑的是 WRITE 级动词而没把
        confirmed=True 传下来, 这里照样拒。防的就是这种「后来者漏了检查」。

        COUNT_HOOK(可选): server 注入的 P1 计数回调(doc/aigit-perf.md §12.2),
        只记子命令名; 未注入(独立使用)时零开销。闸与中和层照旧不动。
        """
        if COUNT_HOOK is not None:
            COUNT_HOOK(args)
        level, reason = self._verb_level(args)
        if level is None:
            raise NetError("拒绝执行 git %r:%s" % (args[0] if args else "", reason),
                           code="unknown_git_verb",
                           verb=args[0] if args else "")
        if level != "local":
            self.gate.require(level, op=args[0])       # 第一道: 能力闸
            if level == WRITE:
                _check_bool(confirmed, args[0])        # 第二道: 显式确认
        # 第三道: 配置中和。**闸开着**才保留用户自己的 credential.helper /
        # core.sshCommand(否则用户的 https 凭证就用不了了);闸关着 / 本地动词
        # 一律清掉,顺带把用户级配置整个不可达。
        # 注意这一道和上面的闸**不是一回事**:闸管「能不能联网」,
        # 中和管「仓库自己写的配置能不能执行程序」。remote 动词走完闸以后
        # 照样会被中和 —— 闸放行的是**这次动作**,不是仓库的配置。
        net = level != "local"
        env = self.guard.safe_env(net_enabled=net)
        with self._lock:
            p = subprocess.run(
                self.guard.build(self.git_bin, self.root, args,
                                 net_enabled=net, base=env),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", input=stdin,
                env=env,
                timeout=timeout or self.timeout)
        if check and p.returncode != 0:
            raise NetError((p.stderr or p.stdout or "git 失败").strip(),
                           code="git_failed", verb=args[0],
                           returncode=p.returncode)
        return p

    def _out(self, *args, **kw):
        return self._run(list(args), **kw).stdout

    def _require_repo(self):
        _need((self.root / ".git").exists(), "不是一个 git 仓:%s" % self.root,
              code="not_a_repo")
        return True

    # ═══════════════════════════════════════════════ 只读层(不需要确认)

    # -- 本地就能回答的 -------------------------------------------------
    def list_remotes(self):
        """列本地配置里的远程。**纯本地**(`git remote -v` 只读 .git/config),
        所以闸在 off 时也能看 —— 这是有意的: 用户要开远程能力, 得先知道
        自己有没有远程可开。

        注意 `git remote -v` 的输出形状: **一个远程占两行**, 分别是
        `name<TAB>url (fetch)` 和 `name<TAB>url (push)`。所以不能一行一条
        直接塞进结果里 —— 那会把 "(fetch)" 当成 URL(踩过)。
        """
        self._require_repo()
        order, byname = [], {}
        for line in self._out("remote", "-v").splitlines():
            bits = line.split()
            if len(bits) < 2:
                continue
            name, url = bits[0], bits[1]
            tag = bits[2] if len(bits) > 2 else ""
            row = byname.get(name)
            if row is None:
                row = byname[name] = {"name": name, "fetchUrl": None,
                                      "pushUrl": None}
                order.append(name)
            row["pushUrl" if tag == "(push)" else "fetchUrl"] = url
        return {"ok": True, "op": "remotes",
                "remotes": [byname[n] for n in order],
                "gate": self.gate.describe()}

    def remote_url(self, name="origin"):
        _check_remote_name(name)
        p = self._run(["remote", "get-url", name], check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def default_branch(self, remote="origin"):
        """远程的默认分支。**用的是 `git ls-remote --symref`,所以要 read 级。**

        注意这里**不**重复调 gate.require —— 闸的唯一执行点是 `_run`。
        复制一份检查只会带来「改了一处忘了另一处」的机会。
        """
        self._require_repo()
        _check_remote_name(remote)
        p = self._run(["ls-remote", "--symref", remote, "HEAD"], check=False)
        if p.returncode != 0:
            raise NetError((p.stderr or p.stdout or "ls-remote 失败").strip(),
                           code="unreachable", remote=remote)
        m = re.search(r"ref:\s+refs/heads/(\S+)\s+HEAD", p.stdout)
        if m:
            return m.group(1)
        for line in p.stdout.splitlines():
            if line.endswith("\tHEAD"):
                return line.split("\t")[0]
        return None

    def ls_remote(self, remote="origin"):
        self._require_repo()
        _check_remote_name(remote)
        p = self._run(["ls-remote", "--heads", remote], check=False)
        _need(p.returncode == 0,
              (p.stderr or p.stdout or "ls-remote 失败").strip(),
              code="unreachable", remote=remote)
        heads = []
        for line in p.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads.append({"sha": sha, "ref": ref,
                              "branch": ref[len("refs/heads/"):]})
        return {"ok": True, "remote": remote, "heads": heads,
                "count": len(heads)}

    def ahead_behind(self, remote="origin", branch=None):
        """本地分支相对**本地远程跟踪 ref**的领先/落后。**纯本地**(`git rev-list`)。

        ⚠️ 基准是 `origin/<branch>` 这个**本地** ref, 它可能是陈旧的 ——
        别人推的东西在你 fetch 之前根本不在这里。所以:

            ahead  = 本地有、跟踪 ref 没有的提交数 = 「还没推出去的」
            behind = 跟踪 ref 有、本地没有的提交数 = 「跟踪 ref 已经动过了,
                     该 fetch/pull 了」

        它**回答不了**「远程现在到底有没有我缺的东西」—— 那个问题要联网
        (fetch 或 ls-remote)。返回的 `basis` 字段就是提醒调用方别把这
        三个词当「远程的真实现状」。实测踩过: 陈旧的跟踪 ref 会让
        behind=0 看起来像"我啥也不缺", 其实只是还没 fetch。

        所以它在 read 级之下也能跑 —— 要不要联网与答案有没有用是两件事。
        """
        self._require_repo()
        _check_remote_name(remote)
        branch = _check_ref(branch or self._head_branch() or "HEAD", "分支")
        up = "%s/%s" % (remote, branch)
        basis = ("本地远程跟踪 ref %s(可能陈旧; 要知道远程真实现状请先 fetch)"
                 % up)
        has = self._run(["rev-parse", "--verify", "--quiet", up],
                        check=False).returncode == 0
        if not has:
            n = len(self._out("rev-list", branch).split())
            return {"ok": True, "remote": remote, "branch": branch,
                    "basis": "本地没有 %s 这个远程跟踪 ref" % up,
                    "trackingRefExists": False, "ahead": n, "behind": None,
                    "note": "本地没有 %s 这个远程跟踪 ref,上面所有提交都算「待推」" % up}
        ab = self._run(["rev-list", "--left-right", "--count",
                        "%s...%s" % (up, branch)], check=False).stdout.split()
        behind, ahead = (int(x) for x in ab[:2])
        return {"ok": True, "remote": remote, "branch": branch,
                "basis": basis,
                "trackingRefExists": True, "ahead": ahead, "behind": behind}

    def _head_branch(self):
        return (self._run(["rev-parse", "--abbrev-ref", "HEAD"],
                          check=False).stdout.strip() or None)

    # ═══════════════════════════════════════════════ 读层(要 read)

    def plan_fetch(self, remote="origin", branch=None):
        self._require_repo()
        _check_remote_name(remote)
        ab = self.ahead_behind(remote, branch) if self._has_tracking(
            remote, branch) else None
        return {"op": "fetch", "safe": True,
                "warning": None, "blocked": None, "executable": True,
                "requires_confirmation": False,
                "remote": remote, "branch": branch,
                "what": "只会往本地写 %s/%s 这个远程跟踪 ref 与 FETCH_HEAD。"
                        "不碰工作区, 不推任何东西, 撤销 = "
                        "git reset --hard %s/%s" % (remote, branch or "?", remote,
                                                     branch or "?"),
                "aheadBehind": ab}

    def _has_tracking(self, remote, branch):
        b = branch or self._head_branch() or "HEAD"
        return self._run(["rev-parse", "--verify", "--quiet",
                          "%s/%s" % (remote, b)], check=False).returncode == 0

    def do_fetch(self, remote="origin", branch=None, refspec=None):
        self._require_repo()
        _check_remote_name(remote)
        if refspec is not None:
            _check_ref(refspec, "refspec")
        # remote / refspec / branch 都过了白名单正则, 不会有 `-` 开头被当选项读
        args = ["fetch", remote]
        if refspec:
            args.append(refspec)
        elif branch:
            args.append(branch)
        p = self._run(args, check=False)
        _need(p.returncode == 0, (p.stderr or p.stdout).strip(),
              code="fetch_failed", remote=remote)
        return {"ok": True, "op": "fetch", "remote": remote,
                "output": (p.stdout + p.stderr).strip(),
                "head": self._head_branch()}

    # ═══════════════════════════════════════════════ 写层(要 write + confirmed)

    def _unpushed(self, remote, branch):
        """将推送哪些提交。**纯本地**(`git rev-list` / `git log`)。"""
        up = "%s/%s" % (remote, branch)
        if self._run(["rev-parse", "--verify", "--quiet", up],
                     check=False).returncode != 0:
            rng = branch                       # 没有上游 ref = 全部都是新的
        else:
            rng = "%s..%s" % (up, branch)
        rows = []
        out = self._out("log", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s",
                        "-n", "200", rng)
        for line in out.splitlines():
            f = line.split("\x1f")
            if len(f) == 5:
                rows.append({"sha": f[0], "short": f[1], "author": f[2],
                             "date": f[3], "subject": f[4]})
        return rows, rng

    def plan_push(self, remote="origin", branch=None, force=False,
                  delete=False):
        """预演。**不发任何东西**,而且**不联网** —— 只读本地 ref 与 log。

        所以 plan 本身不需要任何级别: 闸在 off 时也照样能预览「会推哪几个提交」。
        这跟 history_ops 的 plan_* 一致(预演永远不要求确认), 而且更实用 ——
        用户想看清了再决定要不要开能力, 而不是「开了才能看」。
        """
        self._require_repo()
        _check_remote_name(remote)
        _need(isinstance(force, bool), "force 必须是布尔, 收到 %s"
              % type(force).__name__, code="bad_force")
        _need(isinstance(delete, bool), "delete 必须是布尔, 收到 %s"
              % type(delete).__name__, code="bad_force")
        if delete:
            branch = _check_ref(branch or self._head_branch() or "HEAD", "分支")
            return {"op": "push-delete", "safe": False,
                    "warning": "会**删除远程分支** %s/%s。删掉的东西要别人重新"
                               "推才能回来。" % (remote, branch),
                    "blocked": None,
                    "executable": self.gate.allows(WRITE),
                    "requires_confirmation": True,
                    "remote": remote, "branch": branch,
                    "commits": [], "count": 0,
                    "gate": self.gate.describe()}
        branch = _check_ref(branch or self._head_branch() or "HEAD", "分支")
        commits, rng = self._unpushed(remote, branch)
        warn = None
        if force:
            warn = ("**force push**: 远程上那条分支指针会被**无条件搬走**。"
                    "别人基于旧指针做的改动就此丢失, reflog 在**远程**上, "
                    "你这边救不回来。")
        elif commits:
            warn = ("将把这 %d 个提交推到 %s/%s。推出去撤不回来 —— 传输层"
                    "没有「撤回」这个概念。" % (len(commits), remote, branch))
        return {"op": "push", "safe": False, "warning": warn,
                "blocked": None,
                # 闸没到 write 时 executable=False —— 前端据此把按钮灰掉
                "executable": self.gate.allows(WRITE),
                "requires_confirmation": True,
                "remote": remote, "branch": branch, "force": bool(force),
                "revRange": rng, "commits": commits, "count": len(commits),
                "gate": self.gate.describe()}

    def do_push(self, remote="origin", branch=None, force=False,
                delete=False, confirmed=False):
        _check_remote_name(remote)
        _need(isinstance(force, bool) and isinstance(delete, bool),
              "force / delete 必须是布尔", code="bad_force")
        # 先要闸, 后要确认。顺序有讲究: 闸关着时不该白算一遍 unpushed,
        # 而且报出来的错应该是「能力没开」而不是「你没确认」。
        self.gate.require(WRITE, op="push")
        _check_bool(confirmed, "push")
        self._require_repo()
        if delete:
            branch = _check_ref(branch or self._head_branch() or "HEAD", "分支")
            p = self._run(["push", remote, "--delete", branch],
                          check=False, confirmed=True)
        else:
            branch = _check_ref(branch or self._head_branch() or "HEAD", "分支")
            args = ["push", remote, branch]
            if force:
                args.insert(1, "--force")
            p = self._run(args, check=False, confirmed=True)
        _need(p.returncode == 0, (p.stderr or p.stdout).strip(),
              code="push_failed", remote=remote, branch=branch)
        return {"ok": True, "op": "push", "remote": remote, "branch": branch,
                "forced": bool(force), "deleted": bool(delete),
                "output": (p.stdout + p.stderr).strip()}

    def do_pull(self, remote="origin", branch=None, confirmed=False):
        """pull = fetch + merge。会改工作区, 归 write 级, 同样要显式确认。"""
        _check_remote_name(remote)
        self.gate.require(WRITE, op="pull")
        _check_bool(confirmed, "pull")
        self._require_repo()
        args = ["pull", remote]
        if branch:
            args.append(_check_ref(branch, "分支"))
        p = self._run(args, check=False, confirmed=True)
        _need(p.returncode == 0, (p.stderr or p.stdout).strip(),
              code="pull_failed", remote=remote)
        return {"ok": True, "op": "pull", "remote": remote,
                "output": (p.stdout + p.stderr).strip(),
                "head": self._head_branch()}

    # -- 能力自述 ------------------------------------------------------
    def capability(self):
        return self.gate.describe()


# ═══════════════════════════════════════════════════════ 自检入口
# 「这个文件是不是真的没碰网络」—— 用全新解释器实测 sys.modules,
# 而不是读 import 列表自证(import 列表也可能是骗人的, 比如被条件包一层)。
if __name__ == "__main__":
    import sys as _sys
    # 第一档:**能开连接**的模块。这个列表必须**一个都不在** sys.modules 里。
    _CAN_CONNECT = ("socket", "ssl", "http", "http.client", "urllib.request",
                    "ftplib", "smtplib", "telnetlib", "asyncio", "requests",
                    "pycurl", "xmlrpc", "webbrowser", "imaplib", "poplib",
                    "nntplib", "socketserver", "ssl")
    # 第二档:**只会解析 URL 字符串**的模块。它出现是可以接受的, 但必须说明
    # 为什么。实测结论: `urllib.parse` 是被 **pathlib** 引进来的(stdlib 自己
    # 拿它实现 Path.as_uri()), 不是本文件 import 的; 而且它**没有 urlopen**,
    # 目录里全是 quote/urlsplit/parse_qs 这类纯字符串函数, 开不了连接。
    _URL_STRINGS_ONLY = ("urllib.parse",)
    print("gitops 自检")
    print("  默认级别          = %s  (source=%s)"
          % (NetGate().level, NetGate().source))
    print("  read / write      = %s / %s"
          % (NetGate(READ).level, NetGate(WRITE).level))
    connect = [m for m in _CAN_CONNECT if m in _sys.modules]
    urlonly = [m for m in _URL_STRINGS_ONLY if m in _sys.modules]
    print("  能开连接的模块      = %s   <- 必须为空" % (connect or "none"))
    print("  仅解析 URL 字符串的  = %s   <- 允许(pathlib 传递依赖, 无连接能力)"
          % (urlonly or "none"))
    print("  LOCAL_VERBS=%d 项 / REMOTE_VERBS=%d 项 / REMOTE_FORMS=%d 项"
          % (len(LOCAL_VERBS), len(REMOTE_VERBS), len(REMOTE_FORMS)))
    print()
    print(json.dumps(NetGate().describe(), ensure_ascii=False, indent=2))
    _sys.exit(1 if connect else 0)