#!/usr/bin/env python3
"""
aigit 性能层 —— 大仓分页 + 大 diff 截断 + status 快路径。零第三方依赖,只用标准库。

## ⚠ 这个文件**不是**基准测试,里面一行计时代码都没有

名字里的 `perf` 指「性能增强层」,不是「performance benchmark」。
本文件只做三件事:分页、diff 截断、把 status 换成单次子进程的 v2 解析。
**它不测量任何东西,没有判据,不可能把任何东西当成「正常」。**

想看带数字的基准在别处:
  * 改造前后的那组数字(102.1ms / 139.7ms)在 `aigit-dist.md` §5,
    而它引用的 6 个脚本已不在仓库里 ⇒ **那组数字不可复现**;
  * 真实规模(1k/10k/50k/100k 文件)上的曲线在
    `doc/aigit-A5-scale-perf.md` §2.2–§2.3,以及 E13 的复测
    `doc/aigit-E13-perf-layer.md` §3。
别再把这个文件当基准的载体派活。

## 状态:**默认不生效**,必须显式打开

`aigit.bat` / `aigit.ps1` / `python server.py` **三条正常启动路径都不会调用
`install()`**,所以下面三项增强一个都没生效。这是刻意的,理由见
`doc/aigit-E13-perf-layer.md` §4。历史上唯一的自动挂载点是 `build_aigit.py`
生成的 zipapp 入口,而仓库里没有任何预构建产物(`*.pyz` 被 .gitignore 忽略)⇒
普通用户启动 aigit 从来没跑过这一层。

**从仓库任何位置都能打开它(不需要先 cd 进 tools\\aigit):**

    python tools\\aigit\\aigit_perf.py --serve --root <仓库> --port 8777

其余参数(`--sandbox` / `--max-diff-lines` / `--page-limit` /
`--no-fast-status`)原样转交 `server.py` / `install()`。
不带 `--serve` 时跑的是纯内存自检,见文末「自检」。

## 为什么是独立文件

`server.py` 归 aigit1 所有,本文件不改它一个字。三处增强以「外挂层」的形式
提供,由 `install()` 在运行时挂到 `server.Repo` / `server.Handler` 上。

## 行为兼容性(**有一处必须说清的例外,别再写成「逐字节一致」**)

挂上之后:

  * `/api/diff` 不带 maxLines → 仍返回完整 diff,不截断;但响应里**多**了
    `truncated` / `totalLines` / `shownLines` / `maxLines` 四个字段。
  * `/api/status` 不带 offset/limit → 返回的**形状**不变(完整的 staged /
    unstaged / conflicted 三个数组,字段名一个没多一个没少),
    `web/index.html` 因此零改动可用。
  * **但 `/api/status` 的内容会变**:`install()` 默认 `fast_status=True`,
    把 `server.Repo.status` 从 v1 解析器换成 v2 解析器。这两者**不可能
    逐字节一致** —— 换解析器就换了输出。这里原来写的是「不带分页参数的请求与
    改造前逐字节一致」,那是错的(见 `doc/aigit-A5-scale-perf.md` §1.5 D-1)。
    实测:没有含空格文件名的仓里两套解析器逐字节相同(形态清单见
    `doc/aigit-E13-perf-layer.md` §3.2);含空格的仓上 v2 会截断路径,已修,
    修法与验证见 `parse_porcelain_v2`。想保留 v1 原实现做对照测量就传
    `fast_status=False`(命令行上是 `--no-fast-status`)。

只有显式传了分页参数,响应里才多出 `page` / `totals`,前端据此切换到分页渲染。
"""

import argparse
import json
import pathlib
import sys
import urllib.parse

__all__ = [
    "flatten_status", "paginate", "paginate_payload", "build_status_payload",
    "truncate_diff", "parse_int", "parse_porcelain_v2", "make_fast_status",
    "install", "self_test", "DEFAULT_MAX_DIFF_LINES", "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
]

# 单文件 diff 超过这么多行就截断。前端显示「已截断,点击加载全部」。
DEFAULT_MAX_DIFF_LINES = 5000
# 不显式传 limit 时的分页条数。None = 不分页(全量,与旧行为一致)。
DEFAULT_PAGE_LIMIT = 200

# 单个响应体上限,防止有人传 limit=1000000 把内存打满。
MAX_PAGE_LIMIT = 5000


def parse_int(raw, default=None, lo=None, hi=None):
    """把 query string 里的值安全地转成 int。非法值退回 default,不抛异常。

    故意不信任外部输入:limit 会被拿去切片,负数会让 `items[-5:]` 静默返回
    错误的页(不报错但结果是错的),超大值会打爆内存。
    """
    if raw is None:
        return default
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
        if raw is None:
            return default
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return hi
    return n


# ------------------------------------------------------------------ 状态分页

def flatten_status(st):
    """把三段状态压成一条有序、去重的改动清单。

    顺序与 web/index.html 的 buildRows() 一致:冲突优先单列,再已暂存,最后未暂存。
    去重键沿用前端:path + "|" + staged(0/1) —— 同一路径既已暂存又未暂存时
    是**两条独立改动**,不能合并成一条。
    """
    out = []
    seen = set()
    for bucket in ("conflicted", "staged", "unstaged"):
        for f in (st.get(bucket) or []):
            key = (f.get("path"), 1 if f.get("staged") else 0)
            if key in seen:
                continue
            seen.add(key)
            item = dict(f)
            item["bucket"] = bucket
            out.append(item)
    return out


def paginate(items, offset, limit):
    """切片并给出前端自己数不出来的聚合信息。

    关键点:hasStaged / totals 都基于**全量** items,不是当前页。
    否则「第 2 页里没有已暂存项」会让前端把「已暂存」徽标错误地清掉。
    """
    total = len(items)
    offset = max(0, min(offset, total))
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    page = items[offset:offset + limit]
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "hasMore": offset + len(page) < total,
        "items": page,
    }


def paginate_payload(st, offset=None, limit=None):
    """把一份**已经拿到的**全量 status 包成分页响应。

    与 build_status_payload 分开是因为底座有两条(原 v1 实现 / v2 快路径),
    分页这一层对两者是同一份逻辑,不该各写一遍。

    offset 与 limit 都为 None → 原样返回(兼容既有前端)。
    任一有值 → 额外带 page / totals。
    """
    if offset is None and limit is None:
        return st

    # 在这里钳制,而不是只在 HTTP 层钳:这个函数是模块的公开入口,
    # 被直接调用(脚本、测试、将来的其它面板)时也必须自己站得住。
    # 不过钳的话 "abc" 会一路传到切片那步炸掉,而负数 limit 会让
    # items[:-5] 静默返回错误的页 —— 不报错但结果是错的,更难查。
    if limit is None:
        # 只给了 offset(向下滚动翻页):用默认条数,而不是把剩下的全吐出来。
        limit = DEFAULT_PAGE_LIMIT
    offset = parse_int(offset, 0, lo=0)
    limit = parse_int(limit, DEFAULT_PAGE_LIMIT, lo=1, hi=MAX_PAGE_LIMIT)

    items = flatten_status(st)
    staged_n = len(st.get("staged") or [])
    unstaged_n = len(st.get("unstaged") or [])
    conflicted_n = len(st.get("conflicted") or [])

    return {
        "branch": st.get("branch"),
        "root": st.get("root"),
        # 聚合信息:按全量算,前端直接用,不要自己数当前页。
        "hasStaged": bool(staged_n),
        "totals": {
            "staged": staged_n,
            "unstaged": unstaged_n,
            "conflicted": conflicted_n,
            "changes": len(items),
        },
        "page": paginate(items, offset, limit),
    }


def build_status_payload(repo, offset=None, limit=None):
    """取全量 status 并(按需)分页。给只想一步到位的调用方用。"""
    return paginate_payload(repo.status(), offset, limit)


# ------------------------------------------------------------------ diff 截断

def truncate_diff(text, max_lines):
    """超过 max_lines 行就只留头部,并如实报告被砍掉多少。

    返回 {diff, truncated, totalLines, shownLines}。max_lines <= 0 表示不限
    (前端点「加载全部」时传 0)。

    注意这里省下的是**传输与前端解析/DOM 构建**的成本,不是 git 那次子进程的
    成本 —— 判定是否超长必须先拿到全文。这是刻意的取舍:aigit-dist.md 里的
    基准数字按这个口径给。
    """
    text = text or ""
    if max_lines is None or max_lines <= 0:
        lines = text.split("\n")
        return {"diff": text, "truncated": False,
                "totalLines": len(lines), "shownLines": len(lines)}

    lines = text.split("\n")
    total = len(lines)
    if total <= max_lines:
        return {"diff": text, "truncated": False,
                "totalLines": total, "shownLines": total}

    kept = lines[:max_lines]
    return {
        "diff": "\n".join(kept),
        "truncated": True,
        "totalLines": total,
        "shownLines": max_lines,
    }


# ------------------------------------------------------------------ 状态快路径

def parse_porcelain_v2(out):
    """解析 `git status --porcelain=v2 --branch -z`,产出与 Repo.status() 同形的 dict。

    换 v2 只有一个理由还在:**少一次子进程**。

    1) 少一次子进程。v1 那条路径为拿分支名要**另跑一次**
       `git rev-parse --abbrev-ref HEAD`(现在 server.py 先试
       `symbolic-ref --short`,失败才退回 rev-parse)—— 在 1000 文件的仓上
       实测那第二次子进程 44.0ms,而整个 `Repo.status()` 96.0ms,占 46%。
       v2 的 `--branch` 把 `# branch.head` 放在同一个输出里,一次调用拿全,
       实测整条快路径 57.5ms ⇒ 省 38.5ms。
       (1000 文件 / 9 次取中位数,server.py = 566A1967…,本机 git 2.50.1。
       完整复现命令见 `doc/aigit-E13-perf-layer.md` §3.1。
       A5 报告 §1.4 里那个「5000 文件 / 44.8ms / 占 28%」的数字算错了 ——
       44.8/139.7 = 32%,它自己写的 28% 与自己的数字对不上,见 §1.5 D-3。)

    2) ~~v1 + `-z` 下的 rename 解析是错的~~ —— **这一条已经过期了,别再当理由。**
       历史上 server.py 确实在 `-z` 输出里找 `" -> "` 分隔符,那是死代码,
       少消费一个 NUL 字段就会凭空造出幽灵条目。但 server.py 现在有真的
       `Repo.porcelain_z_parse`(`x=e[0]`、`y=e[1]`、`path=e[3:]`,
       `x in ("R","C")` 时多吃掉一个字段),那个 bug **已经被独立修好了**。
       所以 v2 解析器现在**不是修复,是另一种写法** —— 选它只因为少一次子进程。
       写「v2 修 rename」会让人以为 server.py 还是坏的。

    字段下标(见下面 `parts = f.split(" ", N)` 的 N —— **不能用无参 split**):
        1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>         → path @8
        2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><sc> <path>  → path @9,
            且紧随的 NUL 字段是 origPath,必须一起吃掉
        u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path> → path @10
        ? <path>                                              → path @1(用切片,
            不是 split —— 那一行本来就是安全的)

    ⚠⚠ **路径里有空格时必须用 `split(" ", N)` 而不是 `split(" ")`。**
    这是 E13 实测出来的缺陷(见 `doc/aigit-E13-perf-layer.md` §3.3):
    原来的无参 `f.split(" ")` 会把 `1 M. N... 100644 ... <hash> with space.txt`
    切成 `["1","M.","N...","100644","100644","100644","<hash>","with","space.txt"]`,
    `parts[8]` 拿到的是 **`"with"`** 而不是 `"with space.txt"`。
    实测症状:改名 `plain.txt` → `moved with space.txt`,
    v1 解析器报 `moved with space.txt`(正确),v2 报 **`moved`**(磁盘上不存在)。
    后果和历史上那个 rename 幽灵条目一模一样:点它得到空 diff、
    stage/discard 静默无操作、条目永远清不掉。
    `maxsplit` 让「前 N 个字段是定长的」这件事由切分本身保证,
    剩下的全部归 path —— 这才是 porcelain v2 对含空格路径的承诺。

    ⚠ v2 的「未改动」用 `.` 表示,v1 用空格。同一个改动在 v2 下是 `1 M. N...`,
    在 v1 下是 `M `。忘了归一化就会把 `.` 当状态码吐给前端(status="."),
    并把「只改了工作区」的文件误判成「也改了暂存区」。
    """
    def _xy(xy):
        """把 v2 的 XY 归一成 v1 的空格语义。"""
        x = xy[0] if len(xy) > 0 else " "
        y = xy[1] if len(xy) > 1 else " "
        return (" " if x == "." else x, " " if y == "." else y)

    branch, staged, unstaged, conflicted = "", [], [], []
    fields = out.split("\0")
    i, n = 0, len(fields)
    while i < n:
        f = fields[i]
        i += 1
        if not f:
            continue
        head = f[0]
        if head == "#":
            rest = f[2:].strip()
            if rest.startswith("branch.head "):
                b = rest[len("branch.head "):].strip()
                # v2 写 "(detached)",v1 的 rev-parse 写 "HEAD"。保持后者,
                # 前端与既有脚本看到的仍是同一个字符串。
                branch = "HEAD" if b == "(detached)" else b
            continue
        if head == "?":
            path = f[2:] if f[1:2] == " " else f[1:]
            if path:
                unstaged.append({"path": path, "status": "?", "staged": False})
            continue
        if head == "!":
            continue                       # ignored,当前不展示

        # kind 是单字符标签(1 / 2 / u),后面必跟一个空格。partition 只切第一个
        # 空格,所以拿到的 kind 不会被后面的路径内容影响。
        kind, _, _tail = f.partition(" ")
        # ⚠ maxsplit 必须逐 kind 给对:path 之前的前导字段数是**定长**的
        # (1 → 8 个,2 → 9 个,u → 10 个),剩下的整段都是 path,哪怕里面有空格。
        # 无参 split(" ") 会把 path 从第一个空格处劈开,拿到的 path 被截断 ——
        # 症状是磁盘上不存在的幽灵路径,见上面 docstring 的 ⚠⚠。
        if kind == "1":
            parts = f.split(" ", 8)
            if len(parts) <= 8:
                continue
            xy, path = parts[1], parts[8]
        elif kind == "2":
            parts = f.split(" ", 9)
            if len(parts) <= 9:
                continue
            xy, path = parts[1], parts[9]
            i += 1                          # 吃掉紧随的 origPath 字段
        elif kind == "u":
            parts = f.split(" ", 10)
            if len(parts) <= 10:
                continue
            xy, path = parts[1], parts[10]
            conflicted.append({"path": path, "status": "U"})
            x, y = _xy(xy)
            if x not in (" ", "?"):
                staged.append({"path": path, "staged": True, "status": x})
            if y not in (" ", "?"):
                unstaged.append({"path": path, "staged": False, "status": y})
            continue
        else:
            continue                       # 认不出的行丢掉,绝不猜

        if not path:
            continue
        x, y = _xy(xy)
        if x not in (" ", "?"):
            staged.append({"path": path, "staged": True, "status": x})
        if y not in (" ", "?"):
            unstaged.append({"path": path, "staged": False, "status": y})
        if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
            conflicted.append({"path": path, "status": "U"})

    return {"branch": branch, "root": "", "staged": staged,
            "unstaged": unstaged, "conflicted": conflicted,
            "hasStaged": bool(staged)}


def make_fast_status():
    """造一个替代 Repo.status 的快实现:一次子进程拿到状态 + 分支名。"""
    def fast_status(self):
        out = self._run(["status", "--porcelain=v2", "--branch", "-z",
                         "--untracked-files=all"])
        d = parse_porcelain_v2(out)
        d["root"] = str(self.root)
        return d
    return fast_status


# ------------------------------------------------------------------ 挂载

def install(server, max_lines=DEFAULT_MAX_DIFF_LINES,
            page_limit=DEFAULT_PAGE_LIMIT, fast_status=True):
    """把分页、截断、状态快路径挂到 server 模块上。幂等:重复调用只生效一次。

    server 需要暴露 Repo 与 Handler(server.py 两者都有)。
    fast_status=False 时不换 v2 解析,保留 server.py 原有的 v1 实现
    ——用于对照测量。

    **默认没有任何启动路径会调它。** aigit.bat / aigit.ps1 / `python server.py`
    三条都不调,所以挂上这一步必须是显式的(命令行上是
    `python tools\\aigit\\aigit_perf.py --serve ...`)。理由见
    `doc/aigit-E13-perf-layer.md` §4。

    ⚠ 挂载点是**当前** server.py(实测 = 566A1967…)。这个模块靠 monkeypatch
    与 server.py 耦合,server.py 改路由时这里不会自动跟着改:
      * 本文件早于 server.py 的 `Repo.rel_in_root`,原来的 `/api/diff` 分支
        只调 `resolve_in_root` —— 校验没少(越界照样拒),但回显的 `path` 是
        **请求原串**而不是规范化后的,与不带性能层的服务不一致。
        实测两条路由的 diff 正文**逐字节相同**,差的只有这个字段(E13 §3.4)。
        现在这里跟着用 `rel_in_root`,老 server.py 上自动退回 `resolve_in_root`。
      * 反过来,server.py **新加**的路由逻辑(路径规范化、目录拒绝、ref 白名单)
        只有在性能层不抢那条路由时才会生效。E3/E4 两条线加的东西都是这个形状。
    所以结论是:**性能层是可选增强,不是默认底座**;挂了它就要自己负责跟
    server.py 的路由保持一致。
    """
    if getattr(server, "_aigit_perf_installed", False):
        return False

    Repo = server.Repo
    orig_status = Repo.status
    orig_do_GET = server.Handler.do_GET
    limits = {"max_lines": max_lines, "page_limit": page_limit,
              "fast_status": bool(fast_status)}

    # 底座:拿全量 status。fast_status 打开时用 v2 单次子进程,
    # 否则沿用 server.py 原实现(对照组用)。
    base_fn = make_fast_status() if fast_status else orig_status

    def status(self, offset=None, limit=None):
        """带分页的 status。offset=limit=None → 不分页,返回底座的原始 dict。

        注意措辞:是「不分页」,**不是**「与原实现返回完全一致」——
        fast_status=True 时底座已经是 v2 解析器了,见模块 docstring。
        """
        base = base_fn(self)
        if offset is None and limit is None:
            return base
        return paginate_payload(base, offset, limit)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            q = urllib.parse.parse_qs(parsed.query)
            # limit 不传就是不分页(旧行为);offset 也不传就不进分页分支。
            raw_limit = q.get("limit")
            raw_offset = q.get("offset")
            if raw_limit is not None or raw_offset is not None:
                off = parse_int(raw_offset, 0, lo=0)
                lim = parse_int(raw_limit, limits["page_limit"],
                                lo=1, hi=MAX_PAGE_LIMIT)
                try:
                    return self._json({"ok": True,
                                       **server.REPO.status(off, lim)})
                except server.GitError as e:
                    return self._err(e, 400)
                except Exception as e:
                    return self._err(f"{type(e).__name__}: {e}", 500)
        if parsed.path == "/api/diff":
            q = urllib.parse.parse_qs(parsed.query)
            raw_max = q.get("maxLines")
            if raw_max is not None:
                return _diff_with_cap(self, parsed, q, parse_int(
                    raw_max, limits["max_lines"], lo=0, hi=2_000_000))
        return orig_do_GET(self)

    def _diff_with_cap(handler, parsed, q, cap):
        try:
            if "file" not in q:
                # server.py 的同名路由在缺 file 时报「缺少 file 参数」,
                # 这里原来靠 resolve_in_root("") 兜住,报的是「路径指向仓库根目录
                # 本身」—— 同样是 400,但信息会误导人。补一条一样的。
                raise server.GitError("缺少 file 参数")
            f = q["file"][0]
            staged = q.get("staged", ["0"])[0] in ("1", "true", "yes")
            # 用 rel_in_root(校验 + 规范化)而不是只 resolve_in_root:
            # 与 server.py 的 do_GET 走同一个函数,回显的 `path` 才是同一个字符串。
            # 老 server.py 上没有 rel_in_root 就退回 resolve_in_root(只校验)。
            norm = getattr(server.REPO, "rel_in_root", None)
            rel = norm(f) if norm is not None else f
            if norm is None:
                server.REPO.resolve_in_root(f)
            text = server.REPO.diff(rel, staged=staged)
            body = truncate_diff(text, cap)
            return handler._json({
                "ok": True, "path": rel, "staged": staged,
                "truncated": body["truncated"],
                "totalLines": body["totalLines"],
                "shownLines": body["shownLines"],
                "maxLines": cap,
                "diff": body["diff"],
            })
        except server.GitError as e:
            return handler._err(e, 400)
        except Exception as e:
            return handler._err(f"{type(e).__name__}: {e}", 500)

    Repo.status = status
    server.Handler.do_GET = do_GET
    server._aigit_perf_installed = True
    server.AIGIT_PERF_LIMITS = limits
    return True


# ------------------------------------------------------------------ 自检

# 一条真实的 `git status --porcelain=v2 -z` 记录。三个 kind 各一条,
# 路径**故意带空格** —— 那正是 E13 修掉的截断缺陷的触发条件
# (doc/aigit-E13-perf-layer.md §3.3)。
_V2_MODIFY = "1 M. N... 100644 100644 100644 aaaaaaa bbbbbbb with space.txt"
_V2_RENAME = ("2 R. N... 100644 100644 100644 aaaaaaa bbbbbbb R100 "
              "moved with space.txt\0plain.txt\0")
_V2_UNMERGED = ("u UU N... 100644 100644 100644 100644 aaaaaaa bbbbbbb "
                "ccccccc both added.txt")
_V2_BRANCH = "# branch.oid 1234567\0# branch.head main\0"


def self_test():
    """纯内存自检。**不建仓、不跑 git、不写任何文件、不联网。**

    返回失败原因列表(空 = 全过)。命令行上有失败就退 1,
    所以它能直接进 CI —— 这一点是 E13 加的:原来那个 `__main__` 只打印一张
    能力表,**永远退 0**,也就是说它抓不住任何回归。

    真值全部来自 git porcelain v2 的格式定义,不是「跑一遍把输出存下来」。
    E13 修 parse_porcelain_v2 之后,唯一的验收方式是拿这里这三条
    「含空格的记录」过一遍 —— 那正是当初漏掉的那一类。
    """
    bad = []

    def eq(label, got, want):
        if got != want:
            bad.append("%s: 期望 %r,实际 %r" % (label, want, got))

    # --- v2 解析:含空格的路径必须原样保留(E13 的核心回归点) ---
    # 字段之间一律 NUL 分隔(这正是 -z 的约定),少一个分隔符就会把两条记录
    # 粘成一条 —— 上面那条 fixture 写错时被本自检当场抓住,算它有牙。
    d = parse_porcelain_v2(_V2_BRANCH + _V2_MODIFY + "\0" + _V2_RENAME)
    eq("branch", d["branch"], "main")
    # `1 M.` 是 x 改了、y 没改 ⇒ 进 staged;`2 R.` 同理。两条都在 staged 里。
    eq("staged.paths", [x["path"] for x in d["staged"]],
       ["with space.txt", "moved with space.txt"])
    eq("staged.status", [x["status"] for x in d["staged"]], ["M", "R"])
    eq("unstaged.empty", d["unstaged"], [])
    # 改名必须恰好一条记录,且**原路径不能变成第二条** ——
    # 少消费那个 NUL 字段就会多出幽灵条目(历史上 F-05 的形态)。
    eq("rename.path", [x["path"] for x in d["staged"] if x["status"] == "R"],
       ["moved with space.txt"])
    eq("rename.origPath_not_a_record",
       [x["path"] for x in d["staged"] + d["unstaged"]
        if x["path"] == "plain.txt"], [])
    # 总数也钉住:两条记录(一条 M + 一条 R)就是全部。origPath 那个字段如果
    # 没被吃掉,解析器会把它当成又一条记录 —— 它的头两个字符被当状态码切走,
    # path 只剩 e[3:]("plain.txt" → "in.txt"),于是 staged 变三条。
    eq("staged.count", len(d["staged"]), 2)
    eq("unstaged.count", len(d["unstaged"]), 0)

    # 同一个 kind,路径里**没有**空格 —— 证明 maxsplit 没有把别的列挪位
    d2 = parse_porcelain_v2("1 MM N... 100644 100644 100644 a b plain.txt")
    eq("plain.path", [x["path"] for x in d2["staged"]], ["plain.txt"])
    eq("plain.x", [x["status"] for x in d2["staged"]], ["M"])
    eq("plain.y", [x["status"] for x in d2["unstaged"]], ["M"])
    eq("plain.hasStaged", d2["hasStaged"], True)

    # unmerged 记录同样带空格
    d3 = parse_porcelain_v2(_V2_UNMERGED)
    eq("unmerged.conflicted", [x["path"] for x in d3["conflicted"]],
       ["both added.txt"])
    eq("unmerged.staged", [x["path"] for x in d3["staged"]],
       ["both added.txt"])

    # 未跟踪行:那一支用的是切片不是 split,本来就对,钉住它别被改坏
    d4 = parse_porcelain_v2("? untracked with space.txt\0")
    eq("untracked.path", [x["path"] for x in d4["unstaged"]],
       ["untracked with space.txt"])
    eq("untracked.status", [x["status"] for x in d4["unstaged"]], ["?"])

    # v2 的 '.' 必须归一成 v1 的空格语义,否则会把「只改工作区」说成「也改了暂存区」
    d5 = parse_porcelain_v2("1 .M N... 100644 100644 100644 a b wt_only.txt")
    eq("dot.staged_empty", d5["staged"], [])
    eq("dot.unstaged", [x["path"] for x in d5["unstaged"]], ["wt_only.txt"])

    # detached: v2 写 "(detached)",对外要报 "HEAD"
    eq("detached",
       parse_porcelain_v2("# branch.head (detached)\0")["branch"], "HEAD")

    # 认不出的行丢掉而不是猜
    eq("garbage", parse_porcelain_v2("Z junk here\n")["staged"], [])

    # 改名时紧跟的 origPath 字段**必须被吃掉**。少了 `i += 1`,它会被当成
    # 又一条记录:x=e[0]='p'、y=e[1]='l'、path=e[3:]="in.txt" ⇒ staged 多一条
    # status="p" 的幽灵。这就是历史上 F-05 的形态。上面 staged.count 这条
    # 就是钉它的(变异测试实测:去掉 `i += 1` 只有它红)。
    _ghost = parse_porcelain_v2("2 R. N... 100644 100644 100644 a b R100 "
                                "new.txt\0old.txt\0")
    eq("rename.origPath_eaten.count", len(_ghost["staged"]), 1)
    eq("rename.origPath_eaten.no_ghost_path",
       [x["path"] for x in _ghost["staged"]], ["new.txt"])
    eq("rename.origPath_eaten.status",
       [x["status"] for x in _ghost["staged"]], ["R"])

    # --- parse_int:负数会让 items[-5:] 静默返回错误的页,必须挡住 ---
    eq("int.garbage", parse_int("abc", 7, lo=1, hi=9), 7)
    eq("int.negative", parse_int("-5", 7, lo=1, hi=9), 7)
    eq("int.too_big", parse_int("9999", 7, lo=1, hi=9), 9)
    eq("int.ok", parse_int("3", 7, lo=1, hi=9), 3)
    eq("int.zero_below_lo", parse_int("0", 7, lo=1, hi=9), 7)
    eq("int.whitespace", parse_int("  3  ", 7, lo=1, hi=9), 3)
    eq("int.negative_offset", parse_int("-5", 0, lo=0), 0)
    eq("int.plus_sign", parse_int("+3", 7, lo=1, hi=9), 3)

    # --- flatten_status:顺序、去重键、冲突优先 ---
    # 顺序必须与 web/index.html 的 buildRows() 一致:冲突 → 已暂存 → 未暂存。
    # 去重键是 path + staged 标志:同一路径既已暂存又未暂存是**两条独立改动**,
    # 合并成一条就等于吞掉一半的改动。key 只用 path 的话这一条就红。
    st2 = {"staged": [{"path": "a", "staged": True, "status": "M"},
                     {"path": "dup", "staged": True, "status": "M"}],
          "unstaged": [{"path": "dup", "staged": False, "status": "M"},
                       {"path": "z", "staged": False, "status": "?"}],
          "conflicted": [{"path": "c", "status": "U"}]}
    eq("flatten.order",
       [(i["path"], i["bucket"]) for i in flatten_status(st2)],
       [("c", "conflicted"), ("a", "staged"), ("dup", "staged"),
        ("dup", "unstaged"), ("z", "unstaged")])
    eq("flatten.same_path_two_buckets_kept", len(flatten_status(st2)), 5)
    # 真的重复的条目(同一 bucket 内同 path 同 staged)要去重
    st3 = {"staged": [{"path": "a", "staged": True, "status": "M"},
                     {"path": "a", "staged": True, "status": "M"}],
          "unstaged": [], "conflicted": []}
    eq("flatten.dedup_true_dupes", len(flatten_status(st3)), 1)
    # 缺 bucket 键 / None 不能炸
    eq("flatten.missing_keys", flatten_status({}), [])

    # --- paginate:hasStaged / totals 必须按**全量**算,不是当前页 ---
    st = {"branch": "main", "root": "/r",
          "staged": [{"path": "s%d" % i, "staged": True, "status": "M"}
                     for i in range(3)],
          "unstaged": [{"path": "u%d" % i, "staged": False, "status": "?"}
                       for i in range(4)],
          "conflicted": [], "hasStaged": True}
    # 列表要**够长**才有意义:只有 10 项时,钳不钳到 5000 结果一样,
    # 这条断言就抓不住「有人把 hi 去掉」—— 必须真的造出 MAX_PAGE_LIMIT+50 项。
    _long = [{"path": "p%d" % i} for i in range(MAX_PAGE_LIMIT + 50)]
    eq("page.clamped_to_max", paginate(_long, 0, 10 ** 9)["limit"],
       MAX_PAGE_LIMIT)
    eq("page.max_item_count", len(paginate(_long, 0, 10 ** 9)["items"]),
       MAX_PAGE_LIMIT)
    # 少于上限时不该被截短
    eq("page.under_max_untouched",
       len(paginate(_long[:7], 0, 10 ** 9)["items"]), 7)
    # offset 超界钳到末尾:不报错、不越界、返回空页
    eq("page.offset_clamped", paginate(list(range(5)), 99, 2)["offset"], 5)
    pg = paginate_payload(st, 3, 2)
    eq("page.total", pg["page"]["total"], 7)
    eq("page.hasMore", pg["page"]["hasMore"], True)
    eq("page.items", len(pg["page"]["items"]), 2)
    eq("page.totals", pg["totals"], {"staged": 3, "unstaged": 4,
                                     "conflicted": 0, "changes": 7})
    # 第 2 页里没有已暂存项,但 hasStaged 必须仍为 true(否则前端清掉徽标)
    eq("page.hasStaged_from_full_set", paginate_payload(st, 5, 2)["hasStaged"],
       True)
    # 不传分页参数 → 原样返回,不多一个字段
    eq("page.passthrough", paginate_payload(st), st)
    # 负数 / 垃圾 limit **必须**退回默认条数,而不是被切片:
    # `items[offset:offset + (-5)]` 会返回错误的页,而且不报错。
    # 这条必须从 paginate_payload 外面打 —— 只测 parse_int 抓不到
    # 「有人把 lo 改成 0」这种改法(变异测试实测它能活)。
    for label, raw in (("neg", "-5"), ("zero", "0"), ("junk", "abc")):
        q = paginate_payload(st, 0, raw)
        eq("page.bad_limit.%s clamped" % label, q["page"]["limit"],
           DEFAULT_PAGE_LIMIT)
        eq("page.bad_limit.%s page_complete" % label,
           [i["path"] for i in q["page"]["items"]],
           [i["path"] for i in flatten_status(st)][:DEFAULT_PAGE_LIMIT])
    # 只给 offset 不给 limit → 用默认条数,不是把剩下的全吐出来
    eq("page.offset_only", paginate_payload(st, 5)["page"]["limit"],
       DEFAULT_PAGE_LIMIT)
    # offset 超界要钳到末尾而不是报错
    eq("page.offset_past_end", paginate_payload(st, 999)["page"]["items"], [])
    # limit 超上限要钳住,别让有人打爆内存
    eq("page.limit_clamped", paginate_payload(st, 0, 10 ** 9)["page"]["limit"],
       MAX_PAGE_LIMIT)

    # --- truncate_diff:必须如实报告砍掉多少 ---
    t = truncate_diff("\n".join("l%d" % i for i in range(20)), 5)
    eq("trunc.truncated", t["truncated"], True)
    eq("trunc.total", t["totalLines"], 20)
    eq("trunc.shown", t["shownLines"], 5)
    eq("trunc.kept", t["diff"].count("\n"), 4)
    t0 = truncate_diff("a\nb\nc", 0)
    eq("trunc.unlimited", (t0["truncated"], t0["shownLines"]), (False, 3))
    tbig = truncate_diff("a\nb", 99)
    eq("trunc.under_cap", (tbig["truncated"], tbig["shownLines"]), (False, 2))

    return bad


# ------------------------------------------------------------------ 命令行

def _serve(argv):
    """opt-in 启动:挂上性能层,然后走 server.main()。

    `sys.argv` 被改写成只剩 server.py 认识的参数 —— 它自己用 argparse 读
    `sys.argv[1:]`,不 strip 就多出本文件的参数。
    """
    here = pathlib.Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import server as _server

    ap = argparse.ArgumentParser(prog="aigit_perf.py --serve", add_help=False)
    ap.add_argument("--max-diff-lines", type=int,
                    default=DEFAULT_MAX_DIFF_LINES)
    ap.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT)
    ap.add_argument("--no-fast-status", dest="fast_status", action="store_false")
    ap.set_defaults(fast_status=True)
    known, rest = ap.parse_known_args(argv)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("用法: python aigit_perf.py --serve --root <仓库> [--port N] "
              "[--sandbox]\n"
              "      python aigit_perf.py")
        return 0

    ok = install(_server, max_lines=known.max_diff_lines,
                 page_limit=known.page_limit, fast_status=known.fast_status)
    print("=" * 56)
    print("  性能层已挂上: 分页 + diff 截断 + status 快路径"
          f"({'v2 单次子进程' if known.fast_status else 'v1 原实现(对照组)'})")
    print("  剩下的参数原样转交 server.py: %s" % (" ".join(rest) or "(无)"))
    print("  停用:不带 --serve 即可 —— aigit.bat / aigit.ps1 从不挂这一层。")
    print("=" * 56)
    print("  install() 返回 %r(True=本次真正挂上,False=此前已挂过)" % ok)
    sys.stdout.flush()

    sys.argv = [sys.argv[0]] + rest
    _server.main()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--serve" in argv:
        return _serve([a for a in argv if a != "--serve"])
    bad = self_test()
    if bad:
        for b in bad:
            print("[FAIL] " + b)
        print("  自检 %d 条失败" % len(bad))
        return 1
    # 能力表:仍然打出来,它是给人看的;但**自检不过时不会走到这里**。
    print(json.dumps({
        "status": "module ok, self_test 0 fail",
        "truncate": truncate_diff("\n".join("l%d" % i for i in range(20)), 5),
        "page": paginate([{"path": "f%d" % i} for i in range(25)], 10, 10),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
