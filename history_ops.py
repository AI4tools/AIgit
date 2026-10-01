#!/usr/bin/env python3
"""aigit 历史与危险操作层 —— 对齐 VSCode 源代码管理面板里 aigit 缺的那一批。

范围(本文件负责,一个不多):
    stash 存取 / amend / 撤销上次提交(保留 or 丢弃)/ revert / 分支改名 / 分支删除 /
    打标签 / 文件历史 / blame / 从 reflog 恢复。

设计立场(与 server.py 一致,不是新规矩):
    * 零第三方依赖,只用标准库。理由同 server.py:这工具要在别人机器上直接跑。
    * 所有 git 参数走**列表**形式,不经 shell。
    * 路径越界一律拒绝。`resolve_path()` 与 server.py 的 `resolve_in_root()` 同规格。
    * 名字(分支/标签)走白名单正则,再叠一层 git 自身的保留规则。

**本文件最重要的一条:破坏性操作一律两步走。**

    plan_xxx()  ->  只读,回答"这一步会动到什么、有多危险",不碰仓库
    do_xxx(..., confirmed=True)  ->  真执行

`do_*` 里 `confirmed` 是**必填**的,不给就抛异常。这不是装饰:两步之间仓库可能
已经变了(用户切了分支、别人推了新提交),所以 `do_*` 会把 `plan_*` 用过的每一道
前置检查**重新跑一遍**,而不是相信前端手里那份已经过期的结论。

一步到位的入口**不存在**。`audit_two_step()` 会把这件事变成可执行的断言,
test_history_ops.py 里对它做了检查:每个登记在册的破坏性操作都必须同时有
plan 和 do,且不允许出现裸的动词函数名。

破坏性操作清单见 DESTRUCTIVE_OPS(新增操作必须一并登记,否则审计会报)。

用法(集成方 server.py 侧只需 new 出实例,路由自己接):
    from history_ops import HistoryOps, HistoryError
    ops = HistoryOps(repo_root)
    try:
        info = ops.plan_branch_delete("feature")
        if info["safe"] or user_confirmed:
            ops.do_branch_delete("feature", force=True, confirmed=True)
    except HistoryError as e:
        return e.to_dict()
"""

import os
import re
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 常量

#: 分支/标签名白名单。与 server.py 的 new_branch() 同严格度。
#: 注意这个正则**允许**前导 "-",所以下面 _validate_ref_name() 额外挡掉它 ——
#: `git branch -foo` 会被 git 当成选项读,这是实打实的参数注入。
REF_NAME_RE = re.compile(r"[\w./-]{1,100}")

#: file_history / blame 的返回条数上限。前端一次渲染不了十万行,与其让浏览器
#: 慢慢卡死,不如在参数层就截断,并在返回值里说清"还有多少、从哪继续"。
MAX_HISTORY_ROWS = 2000
MAX_BLAME_LINES = 20000
DEFAULT_BLAME_LINES = 500

#: plan_* 生成的"将要被改动的提交"最多列这么多条(计数仍然是准的)
MAX_LISTED_COMMITS = 20

#: 破坏性操作登记表:op 名 -> (plan 函数名, do 函数名)。
#: 这张表里的 do_* **必须**调 HistoryOps._require_confirm(),audit_two_step()
#: 会用 inspect 检查源码里到底有没有这个调用 —— 光把函数登记进来不算数。
DESTRUCTIVE_OPS = {
    "amend": ("plan_amend", "do_amend"),
    "undo-last-commit": ("plan_undo_last_commit", "do_undo_last_commit"),
    "undo-dropped": ("plan_undo_last_commit", "do_undo_last_commit"),
    "delete-branch": ("plan_branch_delete", "do_branch_delete"),
    "rename-branch": ("plan_branch_rename", "do_branch_rename"),
    "stash-drop": ("plan_stash_drop", "do_stash_drop"),
    "tag-delete": ("plan_tag_delete", "do_tag_delete"),
    "restore-from-reflog": ("plan_restore_from_reflog", "do_restore_from_reflog"),
}

#: 同样是「plan_* + do_*」两步,但**不需要**确认闸的操作。
#:
#: 为什么要显式列出来:两步走的形状不代表危险。不登记的话,要么被误当成破坏性
#: 操作(每次弹确认框,用户就学会闭眼点确认了),要么被 audit 当成漏登记。
#: 写成白名单,等于逼着每一个"我这条不危险"变成一次显式判断。
SAFE_STEPS = {
    # stash 是可恢复的:丢了下次 stash list 还在,顶多浪费一次 pop
    "stash": ("plan_stash", "do_stash"),
    "stash-apply": ("plan_stash", "do_stash_apply"),
    "stash-pop": ("plan_stash", "do_stash_pop"),
    # revert 只新增一个反向提交,不改历史 —— 撤销错了再 revert 一次
    "revert": ("plan_revert", "do_revert"),
    "revert-abort": ("plan_revert", "do_revert_abort"),
    # 打标签是纯新增引用。plan 栏为 None:安全操作不需要预演。
    "tag-create": (None, "do_tag_create"),
}

#: 这些**动词本身**不允许作为导出函数名出现。
#: 存在的意义:让「顺手加个 amend() 图方便」在审计里直接红。
FORBIDDEN_STANDALONE = (
    "amend", "undo_last_commit", "delete_branch", "rename_branch",
    "stash_drop", "tag_delete", "restore_from_reflog",
    "revert", "reset", "stash", "pop", "drop", "delete", "rename",
    "tag", "restore", "recover", "force", "rollback",
)

#: record 分隔符。用 ASCII 控制符,避开路径与提交信息里可能出现的任何字符。
RS = "\x1e"
FS = "\x1f"


class HistoryError(Exception):
    """本层所有可预期失败的唯一出口。

    带 code 是为了让前端能分支处理,而不是去 match 中文文案。
    """

    def __init__(self, message, code="history_error", **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra

    def to_dict(self):
        d = {"ok": False, "error": self.message, "code": self.code}
        d.update(self.extra)
        return d


def _need(cond, message, code="history_error", **extra):
    if not cond:
        raise HistoryError(message, code, **extra)


class HistoryOps:
    """对一个本地仓库的历史操作。构造后不写仓库,第一次写发生在某个 do_* 里。"""

    def __init__(self, root, git_bin="git", timeout=120):
        self.root = Path(root).resolve()
        self.git_bin = git_bin
        self.timeout = timeout
        self._lock = threading.Lock()

    # ═══════════════════════════════════════════════════ 底层

    def _run(self, args, check=True, stdin=None, timeout=None):
        """跑 git,参数列表形式。check=False 时返回 CompletedProcess(为了读退出码)。"""
        with self._lock:
            p = subprocess.run(
                [self.git_bin, "-C", str(self.root)] + [str(a) for a in args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", input=stdin,
                env={**os.environ, "GIT_PAGER": "cat",
                     "GIT_TERMINAL_PROMPT": "0",
                     # 关掉交互式凭证/编辑器,免得 do_* 在没有 tty 的环境里挂死
                     "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true",
                     "GIT_MERGE_AUTOEDIT": "no"},
                timeout=timeout or self.timeout,
            )
        if check and p.returncode != 0:
            msg = (p.stderr or p.stdout or "git 失败").strip()
            raise HistoryError(msg, code=_classify(msg))
        return p

    def _out(self, args, check=True, **kw):
        return self._run(args, check=check, **kw).stdout

    def _ok(self, args):
        """跑一条只关心成败的命令,返回 (returncode, 输出)。"""
        p = self._run(args, check=False)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    # ═══════════════════════════════════════════════════ 校验

    def _require_repo(self):
        _need((self.root / ".git").exists(),
              "不是一个 git 仓库(或路径不存在):%s" % self.root,
              code="not_a_repo")

    def validate_ref_name(self, name, what="名称"):
        """分支/标签名白名单。返回校验后的名字。

        挡的是三类东西:
          1. shell/option 注入 —— 前导 "-"、控制字符、空串。
          2. 路径穿越 —— ".."、"../x"、前导 "/"、"//"。
          3. git 自身的保留形式 —— "@"、"@{...}"(reflog 语法)、结尾的 ".lock"。
        """
        _need(isinstance(name, str), "%s必须是字符串" % what, code="bad_name")
        name = name.strip()
        _need(name, "%s不能为空" % what, code="bad_name")
        _need(len(name) <= 100, "%s超过 100 字符" % what, code="bad_name")
        _need(REF_NAME_RE.fullmatch(name) is not None,
              "%s只允许字母数字和 _ . / -(正则 [\\w./-]{1,100})" % what,
              code="bad_name")
        _need(not name.startswith("-"),
              "%s不能以 - 开头:git 会把它当成选项读" % what, code="bad_name")
        _need(not name.startswith("/") and not name.startswith("."),
              "%s不能以 / 或 . 开头" % what, code="bad_name")
        _need(not name.endswith("/") and not name.endswith("."),
              "%s不能以 / 或 . 结尾" % what, code="bad_name")
        _need(".." not in name, "%s不能包含 .." % what, code="bad_name")
        _need("//" not in name, "%s不能包含 //" % what, code="bad_name")
        _need("@{" not in name, "%s不能包含 @{:这是 reflog 语法" % what,
              code="bad_name")
        _need(name != "@", "%s不能是 @" % what, code="bad_name")
        _need(not name.endswith(".lock"), "%s不能以 .lock 结尾" % what,
              code="bad_name")
        _need(not any(ord(c) < 32 or ord(c) == 127 for c in name),
              "%s不能包含控制字符" % what, code="bad_name")
        return name

    def resolve_path(self, rel):
        """仓库相对路径 -> 绝对路径,越界拒绝。与 server.py 同规格。"""
        _need(isinstance(rel, str) and rel.strip(), "路径为空", code="bad_path")
        rel = rel.strip().replace("\\", "/")
        _need(not rel.startswith("/"), "路径不能是绝对路径:%s" % rel,
              code="bad_path")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise HistoryError("路径越界,已拒绝:%s" % rel, code="path_escape")
        return p

    def _check_sha(self, rev, what="提交"):
        """校验 rev 是不是一个能解析的 commit-ish。通过返回规范化 sha。"""
        _need(isinstance(rev, str) and rev.strip(), "%s不能为空" % what,
              code="bad_rev")
        rev = rev.strip()
        _need(not rev.startswith("-"), "%s不能以 - 开头" % what, code="bad_rev")
        _need(re.fullmatch(r"[A-Za-z0-9_./^~@{}\-]{1,64}", rev) is not None,
              "%s含有非法字符:%s" % (what, rev), code="bad_rev")
        p = self._run(["rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                      check=False)
        _need(p.returncode == 0 and p.stdout.strip(),
              "解析不了这个%s:%s" % (what, rev), code="bad_rev")
        return p.stdout.strip()

    # ═══════════════════════════════════════════════════ 仓库状态

    def _has_commits(self):
        return self._run(["rev-parse", "--verify", "--quiet", "HEAD"],
                         check=False).returncode == 0

    def _require_commits(self, what="这个操作"):
        _need(self._has_commits(), "仓库还没有任何提交,%s无从谈起" % what,
              code="no_commits")

    def head_sha(self):
        self._require_commits("读 HEAD")
        return self._out(["rev-parse", "HEAD"]).strip()

    def head_branch(self):
        """当前分支名;detached HEAD 时返回 None。"""
        p = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        return p.stdout.strip() or None

    def subject_of(self, rev):
        return self._out(["show", "-s", "--format=%s", rev], check=False).strip()

    def parents_of(self, rev):
        s = self._out(["show", "-s", "--format=%P", rev], check=False).strip()
        return [x for x in s.split() if x]

    def count_commits(self):
        return int(self._out(["rev-list", "--count", "HEAD"],
                             check=False).strip() or 0)

    def dirty(self):
        """工作区三段状态,和 server.py 的 status() 同一套 --porcelain -z 解析。"""
        out = self._out(["status", "--porcelain=v1", "-z",
                         "--untracked-files=all"], check=False)
        staged, unstaged, untracked, conflicted = [], [], [], []
        for e in out.split("\0"):
            if not e or len(e) < 3:
                continue
            x, y, path = e[0], e[1], e[3:]
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if x == "?" and y == "?":
                untracked.append(path)
                continue
            if x == "U" or y == "U" or (x == "A" and y == "A") \
                    or (x == "D" and y == "D"):
                conflicted.append(path)
            if x not in (" ", "?"):
                staged.append(path)
            if y not in (" ", "?"):
                unstaged.append(path)
        return {"staged": staged, "unstaged": unstaged,
                "untracked": untracked, "conflicted": conflicted}

    def is_clean(self):
        d = self.dirty()
        return not (d["staged"] or d["unstaged"] or d["untracked"]
                    or d["conflicted"])

    def published_info(self, rev="HEAD"):
        """HEAD(或给定 rev)是否已经在某个远端跟踪分支上。

        只看**本地已有的** remote-tracking ref,不做任何网络访问 —— 所以答案是
        保守的:查不到不等于没推送,plan_* 的文案必须写清这个局限。
        """
        self._require_commits("检查推送状态")
        p = self._run(["branch", "-r", "--contains", rev], check=False)
        remotes = [ln.strip().split(" ")[0] for ln in p.stdout.splitlines()
                   if ln.strip()]
        return {"published": bool(remotes), "remotes": remotes,
                "note": "只检查本地已有的远端跟踪分支,没有联网查询。"
                        "查不到不等于没推送过。"}

    def default_branch(self):
        """判断哪个分支是 default。顺序:远端 HEAD -> main -> master -> None。"""
        p = self._run(["for-each-ref", "--format=%(refname:short)",
                       "refs/remotes/*/HEAD"], check=False)
        lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
        if lines:
            # "origin/main" -> "main"
            return lines[0].split("/", 1)[1] if "/" in lines[0] else lines[0]
        names = {b["name"] for b in self.list_branches()}
        for cand in ("main", "master"):
            if cand in names:
                return cand
        return None

    def list_branches(self):
        out = self._out(["for-each-ref",
                         "--format=%(refname:short)" + FS + "%(HEAD)" + FS + "%(objectname)",
                         "refs/heads"], check=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = (line.split(FS) + ["", ""])[:3]
            rows.append({"name": parts[0], "current": parts[1].strip() == "*",
                         "sha": parts[2].strip()})
        return sorted(rows, key=lambda r: r["name"])

    def _branch_sha(self, name):
        p = self._run(["rev-parse", "--verify", "--quiet",
                       "refs/heads/" + name], check=False)
        return p.stdout.strip() or None

    def _branch_exists(self, name):
        return self._branch_sha(name) is not None

    def _is_current_branch(self, name):
        """当前分支就是它?detached HEAD 时改比 sha。"""
        cur = self.head_branch()
        if cur is not None:
            return cur == name
        return self._has_commits() and self._branch_sha(name) == self.head_sha()

    def _unmerged_commits(self, name):
        """name 上有、基准分支上没有的提交。已并入 default 分支的不算未合并
        —— 否则"这个分支早就并进 main 了"的提示会永远挂着,久而久之没人看警告。

        返回的每条都带 short:前端要在警告里点出具体是哪几个提交,
        光给一串 40 位 sha 没人会去核对。"""
        target = "HEAD"
        dflt = self.default_branch()
        if dflt and self._branch_exists(dflt):
            target = dflt
        n = int(self._out(["rev-list", "--count", target + ".." + name],
                          check=False).strip() or 0)
        rows = []
        out = self._out(["log", "-%d" % MAX_LISTED_COMMITS, "--date=short",
                         "--pretty=format:" + FS.join(["%H", "%an", "%ad", "%s"]),
                         target + ".." + name], check=False)
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split(FS)
            if len(parts) == 4:
                rows.append({"sha": parts[0], "short": parts[0][:8],
                             "author": parts[1],
                             "date": parts[2], "subject": parts[3]})
        return n, rows

    # ═══════════════════════════════════════════════════ plan/do 骨架

    @staticmethod
    def _require_confirm(op, confirmed):
        """破坏性操作的两步闸。

        这是"没有一步到位路径"这条要求**唯一**的执行点:do_* 全部先过这里。
        调用方想跳过,只能去改这个函数 —— 而那会立刻被 test_history_ops.py
        的 D 组测试抓住。
        """
        _need(confirmed is True,
              "「%s」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 "
              "confirmed=True。这里没有一步到位的入口。" % op,
              code="needs_confirmation", op=op)

    @staticmethod
    def _plan(op, safe, warning=None, blocked=None, **data):
        d = {"op": op, "safe": bool(safe), "warning": warning,
             "blocked": blocked,
             # 前端只看 safe 决定"要不要弹确认框"是错的:被 blocked 的操作
             # 压根不该有确认框,直接灰掉。blocked 就是给它用的。
             "executable": blocked is None,
             "requires_confirmation": True}
        d.update(data)
        return d

    # ═══════════════════════════════════════════════════ stash

    def plan_stash(self, message=None, include_untracked=False):
        """暂存当前改动(不提交)。可恢复 —— 不进 DESTRUCTIVE_OPS。"""
        self._require_repo()
        d = self.dirty()
        changes = d["staged"] + d["unstaged"] + d["conflicted"] \
            + (d["untracked"] if include_untracked else [])
        if not changes:
            raise HistoryError(
                "工作区是干净的,没有东西可以暂存。git stash 在这种情况下会直接"
                "报错(而不是塞一个空条目进 stash 列表),所以这里提前拦下。",
                code="nothing_to_stash", dirty=d)
        note = None
        if d["conflicted"]:
            note = ("仓库正处于冲突未解决状态(%d 个路径)。stash 会把冲突一起收走,"
                    "之后 git stash pop 可能产生新的冲突。冲突未解决时 stash "
                    "在 git 自身的语义里就是不完整的。" % len(d["conflicted"]))
        if include_untracked and not d["untracked"]:
            note = (note + " " if note else "") + \
                "没有未跟踪文件,include_untracked 不起作用。"
        return self._plan("stash", safe=True, warning=note, dirty=d,
                          will_stash=len(changes),
                          untracked_kept=len(d["untracked"]) - (
                              len(d["untracked"]) if include_untracked else 0),
                          current_stash_count=len(self.list_stashes()))

    def do_stash(self, message=None, include_untracked=False, keep_index=False):
        """真的暂存。git stash 是可恢复的,所以不设确认闸。"""
        info = self.plan_stash(message, include_untracked)
        args = ["stash", "push"]
        if include_untracked:
            args.append("--include-untracked")
        if keep_index:
            args.append("--keep-index")
        if message:
            args += ["-m", message]
        rc, out = self._ok(args)
        _need(rc == 0, "git stash push 失败:" + out, code="stash_failed")
        return {"ok": True, "stashed": info["will_stash"],
                "now_clean": self.is_clean(), "output": out,
                "stash_count": len(self.list_stashes())}

    def list_stashes(self, limit=50):
        """stash 列表。索引就是 do_stash_pop/apply/drop 接受的那个 index。"""
        out = self._out(["stash", "list", "-%d" % _clamp(limit, 1, 200)], check=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # "stash@{0}: On main: 4b8f1a2 chore: x"
            m = re.match(r"stash@\{(\d+)\}:\s*(.*)", line)
            idx = int(m.group(1)) if m else len(rows)
            desc = m.group(2) if m else line.strip()
            sha, _, subject = desc.partition(":")
            rows.append({
                "index": idx,
                "ref": "stash@{%d}" % idx,
                "name": sha.strip(),
                "subject": subject.strip() or desc.strip(),
                "raw": line.strip(),
            })
        return rows

    def _stash_ref(self, index):
        """把用户给的 index 变成 stash@{n}。索引必须是能解析的整数。"""
        _need(isinstance(index, int) and not isinstance(index, bool),
              "stash 索引必须是整数", code="bad_index")
        _need(index >= 0, "stash 索引不能为负", code="bad_index")
        rows = self.list_stashes(limit=200)
        _need(index < len(rows),
              "没有第 %d 个 stash(共 %d 个)" % (index, len(rows)),
              code="no_such_stash")
        return rows[index]["ref"], rows[index]

    def stash_show(self, index=0, stat=False):
        ref, row = self._stash_ref(index)
        args = ["stash", "show", "--no-color"]
        if stat:
            args.append("--stat")
        else:
            args.append("-p")
        args.append(ref)
        rc, out = self._ok(args)
        return {"ok": rc == 0, "index": index, "ref": ref,
                "name": row["name"], "text": out}

    def do_stash_apply(self, index=0):
        """apply:只贴回来,不删 stash。贴坏了 stash 还在,这是可恢复操作。"""
        ref, row = self._stash_ref(index)
        rc, out = self._ok(["stash", "apply", "--index", ref])
        conflicted = self._conflicted_paths()
        _need(rc == 0 and not conflicted,
              "stash 贴回失败(仓库已保留原样,这个 stash 没被删):\n%s%s"
              % (out, ("\n冲突路径:" + ", ".join(conflicted)) if conflicted else ""),
              code="stash_conflict", conflicted=conflicted, output=out)
        return {"ok": True, "applied": row["ref"], "output": out,
                "stash_count": len(self.list_stashes())}

    def do_stash_pop(self, index=0):
        """pop:贴回并删除。

        这里**没有**直接调 `git stash pop`。git 自己的 pop 在冲突时也不删 stash,
        但它把"贴回"和"删除"揉在一个命令里,失败时返回码与输出容易漏看。改成
        apply 成功才 drop:冲突时 stash 一定还在,而且返回结构里明确带
        conflicted 列表 —— 静默失败是最糟的一种失败。
        """
        ref, row = self._stash_ref(index)
        applied = self.do_stash_apply(index)
        rc, out = self._ok(["stash", "drop", ref])
        _need(rc == 0, "stash 已贴回但删除失败(内容已在工作区,stash 条目还在):"
              + out, code="stash_drop_failed", output=out)
        return {"ok": True, "popped": row["ref"], "applied": applied,
                "dropped": True, "stash_count": len(self.list_stashes())}

    def plan_stash_drop(self, index=0):
        ref, row = self._stash_ref(index)
        return self._plan(
            "stash-drop", safe=False,
            warning="丢弃 stash 列表里的第 %d 个(%s)。丢掉之后要靠 reflog 找回,"
                    "而 reflog 会随 gc 被清掉,不要指望它一定在。"
                    % (index, row["name"]),
            stash={"index": index, "ref": ref, "name": row["name"],
                   "subject": row["subject"]},
            stash_count=len(self.list_stashes()))

    def do_stash_drop(self, index=0, confirmed=False):
        self._require_confirm("stash-drop", confirmed)
        info = self.plan_stash_drop(index)
        rc, out = self._ok(["stash", "drop", info["stash"]["ref"]])
        _need(rc == 0, "git stash drop 失败:" + out, code="stash_drop_failed")
        return {"ok": True, "dropped": info["stash"], "output": out,
                "stash_count": len(self.list_stashes())}

    def _conflicted_paths(self):
        return [p for p in self._out(
            ["diff", "--name-only", "--diff-filter=U"], check=False
        ).splitlines() if p.strip()]

    # ═══════════════════════════════════════════════════ amend

    def plan_amend(self, message=None):
        """修改上一次提交。改写历史 —— 三条不可逆里的一条。"""
        self._require_repo()
        self._require_commits("amend")
        d = self.dirty()
        old_sha = self.head_sha()
        warnings = []

        if d["unstaged"]:
            warnings.append(
                "工作区有 %d 个**未暂存**改动。amend 只提交**已暂存**的内容"
                "(不会加 -a/--all),所以它们不会被卷进这次提交,会原样留在工作区。"
                "如果你本意是把它们一起提交,先 stage 再 amend。"
                % len(d["unstaged"]))
        if not d["staged"]:
            warnings.append("没有已暂存的改动 —— 这次 amend 只能改提交信息,"
                            "提交内容不会变。")
        if d["untracked"]:
            warnings.append("有 %d 个未跟踪文件,它们不属于任何一次提交,amend "
                            "不会碰它们。" % len(d["untracked"]))

        pub = self.published_info(old_sha)
        if pub["published"]:
            warnings.append(
                "HEAD 已经在 %d 个远端跟踪分支上(%s)。改写它会让已推送的历史失效,"
                "协作者必须重新拉取或 reset --hard,否则会出乱子。%s"
                % (len(pub["remotes"]), ", ".join(pub["remotes"]), pub["note"]))
        else:
            warnings.append("本地看不到它被推送过(只查了本地远端跟踪分支,"
                            "没有联网)。如果你已经推过,请先确认远端。")

        return self._plan(
            "amend", safe=False,
            warning=" ".join(warnings),
            old_sha=old_sha, old_short=old_sha[:8],
            old_subject=self.subject_of(old_sha),
            new_message=message,
            message_changes=(message is not None),
            dirty=d, published=pub)

    def do_amend(self, message=None, confirmed=False):
        """真的 amend。

        刻意**不传** -a / --all:amend 的语义是"用当前 index 重写上次提交",
        把工作区未暂存的改动顺手带进去是最常见的事故,也正是 plan_amend
        在 warning 里逐条说明的事。要提交它们,调用方自己 stage 之后再进来。
        """
        self._require_confirm("amend", confirmed)
        info = self.plan_amend(message)
        args = ["commit", "--amend", "--no-verify", "--allow-empty-message"]
        if message is not None:
            args += ["-m", message]
        rc, out = self._ok(args)
        _need(rc == 0, "git commit --amend 失败:" + out, code="amend_failed")
        new_sha = self.head_sha()
        return {"ok": True, "old_sha": info["old_sha"], "new_sha": new_sha,
                "rewritten": info["old_sha"] != new_sha,
                "subject": self.subject_of(new_sha),
                "unstaged_kept": self.dirty()["unstaged"], "output": out}

    # ═══════════════════════════════════════════════════ 撤销上一次提交

    def plan_undo_last_commit(self, drop_changes=False):
        """撤销上一次提交。drop_changes=False 保留改动,--soft 软回退;
        True 直接丢弃,不可恢复 —— 三条不可逆里的一条。"""
        self._require_repo()
        self._require_commits("撤销上一次提交")
        sha = self.head_sha()
        warnings = []
        d = self.dirty()
        pub = self.published_info(sha)

        if drop_changes:
            warnings.append("**丢弃**这个提交的改动:reset --hard 之后工作区与该提交"
                            "完全一致,提交里的内容就没了。")
        else:
            warnings.append("保留改动:所有改动回到暂存区(index),工作区文件不动。")
        if d["unstaged"] or d["untracked"]:
            extra = []
            if d["unstaged"]:
                extra.append("%d 个未暂存改动" % len(d["unstaged"]))
            if d["untracked"]:
                extra.append("%d 个未跟踪文件" % len(d["untracked"]))
            if drop_changes:
                warnings.append("丢弃模式下 reset --hard 会**连同工作区里尚未"
                                "提交的改动一起抹掉**(%s)。" % "、".join(extra))
            else:
                warnings.append("软回退不影响工作区(%s)。" % "、".join(extra))
        if d["conflicted"]:
            warnings.append("仓库处于冲突未解决状态,先解决冲突或 merge --abort。")
        if pub["published"]:
            warnings.append("这次提交已经在 %s 上,撤销会让它消失,远端要另外处理。"
                            % ", ".join(pub["remotes"]))
        nparents = len(self.parents_of(sha))
        if nparents > 1:
            warnings.append("这是一个 merge 提交(有 %d 个父提交),撤销它要指定"
                            "保留哪一侧(mainline),见 plan_revert。" % nparents)

        return self._plan(
            "undo-dropped" if drop_changes else "undo-last-commit",
            safe=False, warning=" ".join(warnings),
            target_sha=sha, target_short=sha[:8],
            target_subject=self.subject_of(sha),
            mode="hard" if drop_changes else "soft",
            parents=nparents, dirty=d, published=pub,
            recoverable_via=self._reflog_hint(
                sha, "hard" if drop_changes else "soft"))

    def do_undo_last_commit(self, drop_changes=False, confirmed=False):
        self._require_confirm("undo-dropped" if drop_changes
                              else "undo-last-commit", confirmed)
        info = self.plan_undo_last_commit(drop_changes)
        args = ["reset", "--hard" if drop_changes else "--soft", "HEAD~1"]
        rc, out = self._ok(args)
        _need(rc == 0, "git reset 失败:" + out, code="reset_failed")
        return {"ok": True, "undone": info["target_sha"],
                "undone_short": info["target_short"],
                "mode": info["mode"], "head_now": self.head_sha(),
                "recoverable_via": info["recoverable_via"], "output": out}

    # ═══════════════════════════════════════════════════ reflog 恢复

    def _reflog_hint(self, sha, mode="soft"):
        """在 reflog 里找这条提交,给一句可直接粘贴的恢复命令。

        mode 跟着这次撤销的实际模式走:soft 撤销配 `reset --soft`,否则用户
        照抄这条命令会顺手把自己的工作区也清掉 —— 恢复动作不该比原来的
        撤销更狠。
        """
        flag = "--" + mode if mode in ("soft", "mixed", "hard", "merge") \
            else "--soft"
        p = self._run(["reflog", "--format=%H"], check=False)
        for i, line in enumerate(p.stdout.splitlines()):
            if line.strip() == sha:
                return "git reset %s %s" % (flag, "HEAD@{%d}" % i)
        return "git reflog"

    def list_reflog(self, limit=30):
        """reflog 列表。reflog 为空(仓库无提交)时返回空 list,不是错误。"""
        if not self._has_commits():
            return []
        # selector **自己拼**而不用 %gd:配了 --date 之后 %gd 会吐出日期
        # (实测 "HEAD@{2026-10-01 00:21:27 +0800}"),那不是能粘回命令行的东西。
        # 列表下标即 reflog 序号,所以 HEAD@{index} 一定对得上。
        out = self._out(["reflog", "-%d" % _clamp(limit, 1, 500),
                         "--date=iso", "--pretty=format:" + FS.join(
                             ["%H", "%h", "%an", "%ad", "%gs"])],
                        check=False)
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            p = (line.split(FS) + [""] * 5)[:5]
            i = len(rows)
            rows.append({"index": i, "sha": p[0], "short": p[1],
                         "selector": "HEAD@{%d}" % i,
                         "author": p[2], "date": p[3], "action": p[4]})
        return rows

    def plan_restore_from_reflog(self, index=0, mode="soft"):
        """从 reflog 恢复到某次历史状态。reflog 空则拒绝。"""
        self._require_repo()
        rows = self.list_reflog(limit=500)
        _need(rows, "reflog 是空的(仓库还没有提交,或 reflog 被 gc 清掉),"
                    "没有可恢复的历史。", code="empty_reflog")
        _need(isinstance(index, int) and 0 <= index < len(rows),
              "reflog 里没有第 %d 条(共 %d 条)" % (index, len(rows)),
              code="bad_index")
        _need(mode in ("soft", "mixed", "hard", "merge"),
              "mode 只能是 soft / mixed / hard / merge", code="bad_mode")
        row = rows[index]
        return self._plan(
            "restore-from-reflog", safe=(mode != "hard"),
            warning="把 HEAD 移到 %s(%s)。%s本次操作本身也会进 reflog,所以还能"
                    "退回来,但别把 reflog 当保险箱 —— gc 之后就没了。"
                    % (row["short"], row["action"],
                       "mode=hard 会同时丢弃工作区未提交的改动。"
                       if mode == "hard" else ""),
            target={"index": index, "sha": row["sha"], "short": row["short"],
                    "selector": row["selector"], "action": row["action"]},
            mode=mode, head_now=self.head_sha())

    def do_restore_from_reflog(self, index=0, mode="soft", confirmed=False):
        self._require_confirm("restore-from-reflog", confirmed)
        info = self.plan_restore_from_reflog(index, mode)
        rc, out = self._ok(["reset", "--" + info["mode"], info["target"]["sha"]])
        _need(rc == 0, "git reset --%s 失败:%s" % (info["mode"], out),
              code="reset_failed")
        return {"ok": True, "restored": info["target"], "mode": info["mode"],
                "head_now": self.head_sha(), "output": out}

    # ═══════════════════════════════════════════════════ revert

    def plan_revert(self, sha, mainline=None):
        """生成反向提交。安全操作(不改历史),但 merge 提交必须指定 mainline。"""
        self._require_repo()
        real = self._check_sha(sha, "提交")
        parents = self.parents_of(real)
        blocker = None
        warnings = []

        if len(parents) > 1:
            if mainline is None:
                blocker = (
                    "%s 是一个 merge 提交,有 %d 个父提交。revert 一个 merge 必须"
                    "用 -m 指定保留哪一侧(mainline),否则结果不确定:不同的 mainline "
                    "会得到完全相反的逆向改动。候选:%s"
                    % (real[:8], len(parents),
                       ", ".join("%d = %s" % (i + 1, self.subject_of(p)[:40])
                                 for i, p in enumerate(parents))))
            else:
                _need(isinstance(mainline, int) and not isinstance(mainline, bool),
                      "mainline 必须是整数(1..%d)" % len(parents), code="bad_mainline")
                _need(1 <= mainline <= len(parents),
                      "mainline 必须是 1..%d,收到 %r" % (len(parents), mainline),
                      code="bad_mainline")
                warnings.append("mainline=%d,保留 %s 这一侧。revert 会把"
                                "「相对这一侧的差异」反向应用到当前分支上。"
                                % (mainline, self.subject_of(parents[mainline - 1])[:40]))
        elif mainline is not None:
            blocker = ("%s 只有一个父提交,不是 merge 提交,不能指定 mainline。"
                       % real[:8])

        if self.dirty()["conflicted"]:
            blocker = "仓库处于冲突未解决状态,先解决冲突或 git revert --abort。"

        return self._plan(
            "revert", safe=(blocker is None),
            warning=" ".join(warnings) or
            ("revert 会新建一个反向提交,不改写任何历史 —— 撤销错了再 revert 一次"
             "就行。"),
            blocked=blocker, sha=real, short=real[:8],
            subject=self.subject_of(real), parents=parents,
            is_merge=len(parents) > 1, mainline=mainline)

    def do_revert(self, sha, mainline=None, no_edit=True):
        """真的 revert。冲突时明确报冲突路径,并留下 --abort 的路。"""
        info = self.plan_revert(sha, mainline)
        _need(info["executable"], info["blocked"], code="revert_refused")
        args = ["revert"]
        if no_edit:
            args.append("--no-edit")
        if mainline is not None:
            args += ["-m", str(mainline)]
        args.append(info["sha"])
        rc, out = self._ok(args)
        if rc != 0:
            conflicted = self._conflicted_paths()
            raise HistoryError(
                "revert 失败:%s%s\n仓库可能停在 revert 中途,"
                "用 do_revert_abort() 收场,或者手工解冲突后 git revert --continue。"
                % (out, ("\n冲突路径:" + ", ".join(conflicted)) if conflicted else ""),
                code="revert_conflict" if conflicted else "revert_failed",
                conflicted=conflicted, output=out)
        return {"ok": True, "reverted": info["short"], "mainline": mainline,
                "new_head": self.head_sha(), "output": out}

    def _revert_in_progress(self):
        """仓库是否停在 revert 中途。

        两条路都要查,因为 git 两种模式下留的痕迹不一样:
          * 单个提交的 revert 停在中途 -> 写 REVERT_HEAD,**没有** sequencer 目录。
          * 多个提交的 revert(我们不提供多选,但 git 内部仍可能走到)-> 走 sequencer。

        写法上必须用裸的 "REVERT_HEAD" 而不是 ".git/REVERT_HEAD":
        后者是**路径**而非 ref,rev-parse --verify 会拿它当 rev 解析,恒定失败
        (git version 2.x 实测 rc=1)。踩过一次,留个注释。
        """
        head = self._run(["rev-parse", "--verify", "--quiet", "REVERT_HEAD"],
                        check=False)
        if head.returncode == 0:
            return True
        seq = self._run(["rev-parse", "--git-path", "sequencer"], check=False)
        if seq.returncode != 0:
            return False
        rel = seq.stdout.strip()
        # --git-path 给的是相对 .git 的路径(实测 ".git/sequencer"),两种拼法都试
        return (os.path.isdir(os.path.join(str(self.root), rel))
                or os.path.isdir(os.path.join(str(self.root), ".git",
                                              rel.replace(".git/", "", 1))))

    def do_revert_abort(self):
        active = self._revert_in_progress()
        _need(active, "现在没有进行中的 revert,没有东西可中止。",
              code="no_revert_in_progress")
        rc, out = self._ok(["revert", "--abort"])
        _need(rc == 0, "git revert --abort 失败:" + out, code="revert_abort_failed")
        return {"ok": True, "aborted": True, "output": out}

    # ═══════════════════════════════════════════════════ 分支改名

    def plan_branch_rename(self, old, new):
        self._require_repo()
        old = self.validate_ref_name(old, "旧分支名")
        new = self.validate_ref_name(new, "新分支名")
        _need(self._branch_exists(old), "没有这个分支:%s" % old, code="no_such_branch")
        blocked = None
        if old == new:
            blocked = "新旧分支名相同,什么都不用做。"
        elif self._branch_exists(new):
            blocked = ("已经有同名分支 %s。改名不会覆盖它 —— 那是另一个人的工作。"
                       % new)
        warnings = []
        if self._is_current_branch(old):
            warnings.append("%s 是当前分支,改名会同时更新 HEAD 和上游配置。" % old)
        wt = self._out(["worktree", "list", "--porcelain"], check=False)
        if ("branch refs/heads/" + old) in wt:
            warnings.append("%s 还被别的 worktree 检出着,git 会拒绝改名;"
                            "先在那个 worktree 里切走。" % old)
        if len(warnings) == 0:
            warnings.append("改名不改历史、不删提交,做完随时能改回去。")
        return self._plan("rename-branch", safe=False, warning=" ".join(warnings),
                          blocked=blocked, old=old, new=new,
                          old_sha=self._branch_sha(old))

    def do_branch_rename(self, old, new, confirmed=False):
        self._require_confirm("rename-branch", confirmed)
        info = self.plan_branch_rename(old, new)
        _need(info["executable"], info["blocked"], code="rename_refused")
        rc, out = self._ok(["branch", "-m", info["old"], info["new"]])
        _need(rc == 0, "git branch -m 失败:" + out, code="rename_failed")
        return {"ok": True, "old": info["old"], "new": info["new"],
                "sha": self._branch_sha(info["new"]), "output": out}

    # ═══════════════════════════════════════════════════ 分支删除

    def plan_branch_delete(self, name, force=False):
        """删除分支。force=True 强删未合并分支 = 三条不可逆里的一条。"""
        self._require_repo()
        name = self.validate_ref_name(name, "分支名")
        _need(self._branch_exists(name), "没有这个分支:%s" % name,
              code="no_such_branch")

        branches = self.list_branches()
        blocked = None
        warnings = []

        # 顺序有讲究:单分支仓里"当前分支"和"唯一分支"同时成立,而"唯一分支"
        # 信息量更大(它解释了为什么切走也没用),所以先判它。
        if len(branches) <= 1:
            blocked = ("这是仓库里唯一的分支,删掉之后 HEAD 就没有落脚点了。"
                       "git 本身也会拒绝。")
        elif self._is_current_branch(name):
            # 别在这里写死一个分支名:"先切到 main"在 main 恰好就是被删的那个
            # 时候是废话。挑一个**确实存在且不是它**的分支来举例。
            elsewhere = next((b["name"] for b in branches
                              if b["name"] != name), None)
            blocked = ("不能删除当前所在的分支 %s。先切到别的分支(%s),再删它。"
                       % (name, "git checkout " + elsewhere if elsewhere
                          else "git checkout <其他分支>"))
        else:
            dflt = self.default_branch()
            if dflt and name == dflt:
                blocked = ("不能删除 default 分支 %s。它是别人 clone 之后的落脚点,"
                           "也是判别「这个提交算不算已合并」的基准。想删它,先改"
                           "名字(plan_branch_rename)再删。" % name)

        nr, commits = self._unmerged_commits(name)
        if nr:
            base = self.default_branch() or "HEAD"
            msg = "该分支有 %d 个未合并提交(相对 %s)" % (nr, base)
            if force:
                msg += ",force=True 会直接丢掉,不可恢复"
            else:
                msg += ",不加 force 会被拒绝"
            warnings.append(msg)
            if len(commits) < nr:
                warnings.append("(只列出前 %d 条)" % len(commits))
        else:
            warnings.append("该分支的提交都已并入 %s,删除是安全的。"
                            % (self.default_branch() or "HEAD"))

        if blocked:
            warnings.append("不过先说结论:这一步现在**不能做** —— " + blocked)
            safe = False
        else:
            safe = (nr == 0)

        return self._plan("delete-branch", safe=safe, warning=" ".join(warnings),
                          blocked=blocked, name=name, force=bool(force),
                          unmerged_count=nr, commits=commits,
                          current=self._is_current_branch(name),
                          is_default=(self.default_branch() == name),
                          branch_count=len(branches),
                          sha=self._branch_sha(name))

    def do_branch_delete(self, name, force=False, confirmed=False):
        self._require_confirm("delete-branch", confirmed)
        info = self.plan_branch_delete(name, force)
        _need(info["executable"], info["blocked"], code="delete_refused")
        if info["unmerged_count"] and not force:
            raise HistoryError(
                "「%s」有 %d 个未合并提交,不加 force 拒绝删除。\n%s\n"
                "确认要丢就再调一次,带 force=True(和 confirmed=True)。"
                % (name, info["unmerged_count"], info["warning"]),
                code="unmerged_branch", unmerged_count=info["unmerged_count"],
                commits=info["commits"], hint="再调一次,带 force=True")
        args = ["branch", "-D" if force else "-d", name]
        rc, out = self._ok(args)
        _need(rc == 0, "git branch %s 失败:%s"
              % ("-D" if force else "-d", out), code="delete_failed")
        return {"ok": True, "deleted": name, "forced": bool(force),
                "unmerged_count": info["unmerged_count"], "output": out,
                "remaining": [b["name"] for b in self.list_branches()]}

    # ═══════════════════════════════════════════════════ 标签

    def list_tags(self, limit=100):
        # for-each-ref 只认 --count=<n>,不认 git log 那种 -<n> 简写。这里用
        # check=True:查询函数唯一的失败原因就是 git 自己出错,那必须冒出来,
        # 不能被 check=False 吞成"一个标签都没有" —— 那是静默失败。
        out = self._out(["for-each-ref", "--count=%d" % _clamp(limit, 1, 500),
                         "--sort=-creatordate",
                         "--format=" + FS.join(["%(refname:short)", "%(objectname)",
                                               "%(objecttype)", "%(contents:subject)",
                                               "%(creatordate:short)"]),
                         "refs/tags"])
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            p = (line.split(FS) + [""] * 5)[:5]
            rows.append({"name": p[0], "sha": p[1], "type": p[2],
                         "subject": p[3], "date": p[4],
                         "annotated": p[2] == "tag"})
        return rows

    def do_tag_create(self, name, sha=None, message=None, force=False):
        """打标签。新增引用,不碰历史 —— 安全操作,无确认闸。"""
        self._require_repo()
        self._require_commits("打标签")
        name = self.validate_ref_name(name, "标签名")
        target = self._check_sha(sha or "HEAD", "提交")
        if self._out(["for-each-ref", "--count=1", "refs/tags/" + name],
                     check=False).strip():
            _need(force, "标签 %s 已存在。删了重建,或者传 force=True 覆盖。"
                  % name, code="tag_exists")
        args = ["tag"]
        if force:
            args.append("-f")
        if message:
            args += ["-a", "-m", message]
        args += [name, target]
        rc, out = self._ok(args)
        _need(rc == 0, "git tag 失败:" + out, code="tag_failed")
        return {"ok": True, "name": name, "sha": target, "annotated": bool(message),
                "output": out}

    def plan_tag_delete(self, name):
        self._require_repo()
        name = self.validate_ref_name(name, "标签名")
        _need(self._out(["for-each-ref", "--count=1", "refs/tags/" + name],
                        check=False).strip(), "没有这个标签:%s" % name,
              code="no_such_tag")
        sha = self._out(["rev-list", "-1", name], check=False).strip()
        return self._plan(
            "tag-delete", safe=False,
            warning="删掉标签 %s(指向 %s)。标签通常不进 reflog,所以这一删基本"
                    "不可恢复;提交本身还在,只是这个标记没了。"
                    % (name, sha[:8] if sha else "?"),
            name=name, sha=sha)

    def do_tag_delete(self, name, confirmed=False):
        self._require_confirm("tag-delete", confirmed)
        info = self.plan_tag_delete(name)
        rc, out = self._ok(["tag", "-d", info["name"]])
        _need(rc == 0, "git tag -d 失败:" + out, code="tag_delete_failed")
        return {"ok": True, "deleted": info["name"], "sha": info["sha"],
                "output": out}

    # ═══════════════════════════════════════════════════ 文件历史

    def file_history(self, path, limit=50, follow=True):
        """单文件历史。follow=True 跟重命名(VSCode 的 File History 语义)。"""
        self._require_repo()
        self._require_commits("看文件历史")
        ap = self.resolve_path(path)
        rel = ap.relative_to(self.root).as_posix()
        rows = []
        args = ["log", "-%d" % _clamp(limit, 1, MAX_HISTORY_ROWS), "--date=iso",
                "--pretty=format:" + FS.join(
                    ["%H", "%h", "%an", "%ae", "%ad", "%s", "%P"])]
        if follow:
            args.append("--follow")
        args += ["-M", "--name-status", "--", rel]
        p = self._run(args, check=False)
        # --name-status 的行紧跟在 commit 行后面,顺序不固定,所以按行型分类,
        # 不靠位置猜:含 FS 的是 commit 头,匹配 ^[AMDRTC]\d*\t 的是 name-status。
        for line in p.stdout.splitlines():
            if not line.strip():
                continue
            if FS in line:
                q = line.split(FS)
                if len(q) >= 6:
                    rows.append({"sha": q[0], "short": q[1], "author": q[2],
                                 "email": q[3], "date": q[4], "subject": q[5],
                                 "parents": [x for x in q[6].split() if x] if len(q) > 6 else [],
                                 "files": []})
            elif re.match(r"^[AMDRTCU]\d*\t", line) and rows:
                parts = line.split("\t")
                st = parts[0].rstrip("0123456789")
                if st.startswith("R") and len(parts) >= 3:
                    rows[-1]["files"].append({"status": "R", "score": parts[0][1:],
                                              "old": parts[1], "new": parts[2],
                                              "rename": True})
                else:
                    rows[-1]["files"].append({"status": st, "path": parts[-1],
                                              "rename": False})
        truncated = False
        if len(rows) >= _clamp(limit, 1, MAX_HISTORY_ROWS):
            more = self._out(["rev-list", "--count", "--max-count=100000",
                              "HEAD", "--", rel], check=False).strip()
            truncated = more.isdigit() and int(more) > len(rows)
        return {"ok": True, "path": rel, "follow": bool(follow),
                "count": len(rows), "truncated": truncated, "commits": rows,
                "exists_in_worktree": ap.exists(),
                "tracked": bool(self._out(["ls-files", "--", rel],
                                          check=False).strip())}

    # ═══════════════════════════════════════════════════ blame

    def _file_total_lines(self, ap, rel):
        """取文件的总行数。优先读工作区,读不到就从 HEAD 的 blob 数。"""
        if ap.exists() and ap.is_file():
            n = 0
            with open(ap, "rb") as f:
                for _ in f:
                    n += 1
            return n
        p = self._run(["cat-file", "-s", "HEAD:" + rel], check=False)
        if p.returncode != 0 or not p.stdout.strip().isdigit():
            return 0
        size = int(p.stdout.strip())
        blob = self._run(["cat-file", "blob", "HEAD:" + rel], check=False)
        if blob.returncode != 0:
            return 0
        return blob.stdout.count("\n") or (1 if size else 0)

    def blame(self, path, start_line=None, end_line=None, limit=DEFAULT_BLAME_LINES):
        """逐行 blame。返回行号 + 提交 + 作者 + 时间。

        截断在这里做,而且是在**问 git 之前**就决定好范围:先用 -L 把范围卡住,
        再在参数层把 limit 夹到 [1, MAX_BLAME_LINES]。十万行的文件不该让 git
        把整份 --line-porcelain 吐出来再由我们扔掉。
        """
        self._require_repo()
        self._require_commits("blame")
        ap = self.resolve_path(path)
        rel = ap.relative_to(self.root).as_posix()
        _need(ap.exists() or self._out(["cat-file", "-e", "HEAD:" + rel],
                                       check=False).strip() != "",
              "文件不存在,也没有历史:%s" % rel, code="no_such_file")
        # 二进制文件 blame 没意义,而且 porcelain 解析会被 NUL 搅乱。
        data = b""
        if ap.exists() and ap.is_file():
            with open(ap, "rb") as f:
                data = f.read(8000)
        else:
            b = self._run(["cat-file", "blob", "HEAD:" + rel], check=False)
            data = b.stdout.encode("utf-8", "replace")[:8000]
        _need(b"\0" not in data, "这是二进制文件,blame 没有意义。",
              code="binary_file")

        limit = _clamp(limit, 1, MAX_BLAME_LINES)
        total = self._file_total_lines(ap, rel)
        first = 1
        if start_line is not None:
            _need(isinstance(start_line, int) and not isinstance(start_line, bool),
                  "start_line 必须是整数", code="bad_line")
            first = max(1, start_line)
        last = min(total, first + limit - 1) if total else first + limit - 1
        if end_line is not None:
            _need(isinstance(end_line, int) and not isinstance(end_line, bool),
                  "end_line 必须是整数", code="bad_line")
            _need(end_line >= first, "end_line(%d) 不能小于 start_line(%d)"
                  % (end_line, first), code="bad_line")
            last = min(last, end_line)

        args = ["blame", "--line-porcelain"]
        if total or end_line is not None:
            args += ["-L", "%d,%d" % (first, last)]
        args += ["--", rel]
        p = self._run(args, check=False)
        _need(p.returncode == 0, "git blame 失败:" + (p.stderr or p.stdout).strip(),
              code="blame_failed")
        lines = p.stdout.split("\n")
        recs, i = [], 0
        while i < len(lines):
            hdr = lines[i]
            if not hdr or hdr.startswith("\t"):
                i += 1
                continue
            parts = hdr.split(" ")
            if len(parts) < 3:
                i += 1
                continue
            sha, final = parts[0], parts[1]
            meta, j = {}, i + 1
            while j < len(lines) and not lines[j].startswith("\t"):
                if " " in lines[j]:
                    k, v = lines[j].split(" ", 1)
                else:
                    k, v = lines[j], ""
                meta.setdefault(k, v)
                j += 1
            content = lines[j][1:] if j < len(lines) and \
                lines[j].startswith("\t") else ""
            recs.append({
                "line": int(final),
                "sha": sha,
                "short": sha[:8],
                "author": meta.get("author", ""),
                "author_email": meta.get("author-mail", ""),
                "time": _iso_from_epoch(meta.get("author-time", "0"),
                                        meta.get("author-tz", "+0000")),
                "summary": meta.get("summary", ""),
                "filename": meta.get("filename", rel),
                "previous": meta.get("previous", ""),
                "content": content,
            })
            i = j + 1

        _need(recs, "blame 没有返回任何行(文件可能是空的,或范围选错了)。",
              code="blame_empty", total_lines=total)
        covered = recs[-1]["line"]
        truncated = bool(total and covered < total)
        # requested_first / requested_end 一起回传:光给 first_line/last_line 的话,
        # 前端分不清"用户要的就是这一段"和"这一段是被 limit 截出来的"。
        return {"ok": True, "path": rel, "count": len(recs),
                "requested_first": first, "requested_end": last,
                "first_line": recs[0]["line"], "last_line": covered,
                "total_lines": total, "truncated": truncated,
                "next_line": (covered + 1) if truncated else None,
                "limit": limit, "lines": recs}

    # ═══════════════════════════════════════════════════ 自检


def audit_two_step():
    """自检:破坏性操作是否都提供了 plan/do 两步,确认闸是否真的装上了,
    以及有没有一步到位的入口。

    检查项(全部是可执行的断言,不是注释):
      1. DESTRUCTIVE_OPS 里每个 op 的 plan_* 与 do_* 都存在。
      2. **每个破坏性 do_* 的源码里真的调了 _require_confirm()**。
         登记进表不等于装了闸,这里读源码来确认。
      3. 反过来,SAFE_STEPS 里的 do_* 不该装闸(装了就是过度保守,弹框疲劳)。
      4. 每个 do_* 都被 DESTRUCTIVE_OPS 或 SAFE_STEPS 覆盖 —— 漏登记就会在这里
         被当成"没人审过它的危险等级"报出来。
      5. 没有裸动词的导出函数(一步到位的入口)。

    返回问题列表,空列表表示通过。集成方可以在启动时调一次,把这个当断言用。
    """
    import inspect

    problems = []
    dest = {d for _, d in DESTRUCTIVE_OPS.values()}
    safe = {d for _, d in SAFE_STEPS.values()}

    for op, (plan_name, do_name) in DESTRUCTIVE_OPS.items():
        if not callable(getattr(HistoryOps, plan_name, None)):
            problems.append("破坏性操作 %s 缺少 %s()(两步走的第一步)"
                            % (op, plan_name))
        fn = getattr(HistoryOps, do_name, None)
        if not callable(fn):
            problems.append("破坏性操作 %s 缺少 %s()(两步走的第二步)" % (op, do_name))
            continue
        if "_require_confirm(" not in inspect.getsource(fn):
            problems.append(
                "%s() 登记为破坏性操作,但源码里没有调 _require_confirm() —— "
                "这样就能一步到位。确认闸必须装在函数体里,不能靠调用方自觉。"
                % do_name)

    for op, (plan_name, do_name) in SAFE_STEPS.items():
        fn = getattr(HistoryOps, do_name, None)
        if not callable(fn):
            problems.append("安全操作 %s 缺少 %s()" % (op, do_name))
            continue
        if "_require_confirm(" in inspect.getsource(fn):
            problems.append(
                "%s() 登记为安全操作,却装了确认闸。请先判断它到底危不危险,再决定"
                "挪到 DESTRUCTIVE_OPS 还是把闸拆掉。" % do_name)
        if plan_name and not callable(getattr(HistoryOps, plan_name, None)):
            problems.append("安全操作 %s 声明了 %s(),但它不存在" % (op, plan_name))

    for n in [x for x in dir(HistoryOps) if not x.startswith("_")]:
        if n.startswith("do_") and n not in dest and n not in safe:
            problems.append(
                "do_%s 既不在 DESTRUCTIVE_OPS 也不在 SAFE_STEPS 里。它的危险等级"
                "没人审过 —— 登记进其中一张表,别让它悄悄滑过去。" % n[3:])
        if n.startswith("plan_") and n[5:] + "" and (
                n not in [p for p, _ in DESTRUCTIVE_OPS.values() if p]
                and n not in [p for p, _ in SAFE_STEPS.values() if p]):
            problems.append("plan_%s 没有对应的登记表条目。" % n[5:])
        if not n.startswith(("plan_", "do_")) and n in FORBIDDEN_STANDALONE:
            problems.append(
                "HistoryOps.%s 是一步到位的破坏性入口。破坏性操作只能以 "
                "plan_*(只读) + do_*(confirmed=True) 成对出现。" % n)
    return problems


def _clamp(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise HistoryError("必须是整数:%r" % (v,), code="bad_number")
    return max(lo, min(hi, v))


def _iso_from_epoch(epoch, tz):
    """git blame 的 author-time 是 epoch 秒 + author-tz 偏移,转成可读时间。"""
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return ""
    m = re.match(r"([+-])(\d{2})(\d{2})", tz or "")
    off = timedelta(hours=0)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        off = sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return datetime.fromtimestamp(e, timezone.utc).astimezone(
        timezone(off)).strftime("%Y-%m-%d %H:%M:%S%z")


def _classify(msg):
    """把 git 的报错粗分类,让前端能分支处理,而不是 match 中文文案。"""
    m = msg.lower()
    if "conflict" in m or "could not apply" in m:
        return "conflict"
    if "not a git repository" in m or "does not have any commits" in m:
        return "no_commits"
    if "unknown revision" in m or "unknown revision or path" in m \
            or "bad revision" in m:
        return "bad_rev"
    if "used by worktree" in m or "cannot delete" in m \
            or "checked out at" in m:
        return "refused"
    if "nothing to commit" in m or "no local changes" in m \
            or "working directory clean" in m:
        return "nothing_to_do"
    return "history_error"


if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="history_ops 自检")
    ap.add_argument("--audit", action="store_true", help="只跑两步走自检")
    a = ap.parse_args()
    probs = audit_two_step()
    if probs:
        for p in probs:
            print("FAIL " + p)
        raise SystemExit(1)
    print("ok  破坏性操作 %d 项,全部 plan/do 配对且装了确认闸;安全两步操作 %d 项;"
          "无一步到位入口" % (len(DESTRUCTIVE_OPS), len(SAFE_STEPS)))
    if a.audit:
        raise SystemExit(0)
    print(_json.dumps({k: v for k, v in sorted(DESTRUCTIVE_OPS.items())},
                      ensure_ascii=False, indent=2))
