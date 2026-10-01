#!/usr/bin/env python3
"""
aigit 性能层 —— 大仓分页 + 大 diff 截断。零第三方依赖,只用标准库。

## 为什么是独立文件

`server.py` 归 aigit1 所有,本文件不改它一个字。两处增强(分页、diff 截断)
以「外挂层」的形式提供,由 `install()` 在运行时挂到 `server.Repo` / `server.Handler`
上。build_aigit.py 生成的 .pyz 会自动调用 `install()`;在源码树里跑
`python server.py` 则需要手动挂一次(见 aigit-dist.md 的「源码树集成」一节)。

## 行为兼容性(重要)

`install()` 之后,**不带 offset/limit 的请求与改造前逐字节一致**:

  * `/api/status` 不带分页参数 → 仍返回完整的 staged / unstaged / conflicted
    三个数组,字段名一个没多一个没少。aigit1 的 web/index.html 因此零改动可用。
  * `/api/diff` 不带 maxLines → 仍返回完整 diff,`truncated` 恒为 false。

只有显式传了分页参数,响应里才多出 `page` / `totals`,前端据此切换到分页渲染。
"""

import json
import urllib.parse

__all__ = [
    "flatten_status", "paginate", "paginate_payload", "build_status_payload",
    "truncate_diff", "parse_int", "parse_porcelain_v2", "make_fast_status",
    "install", "DEFAULT_MAX_DIFF_LINES", "DEFAULT_PAGE_LIMIT", "MAX_PAGE_LIMIT",
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

    换 v2 有两个理由,第二个是修一个真实的 bug。

    1) 少一次子进程。v1 那条路径为拿分支名要**另跑一次**
       `git rev-parse --abbrev-ref HEAD` —— 在 5000 文件的仓上实测 44.8ms,
       占整个 status 的 28%。v2 的 `--branch` 把 `# branch.head` 放在同一个
       输出里,一次调用拿全。

    2) v1 + `-z` 下的 rename 解析是错的。-z 模式把新旧路径写成**两个 NUL
       分隔的字段**(`XY new\\0old\\0`),根本没有 " -> " 分隔符。server.py 里那句
       `if " -> " in path` 在 -z 下永远不成立(死代码),于是旧路径那个字段被
       当成一条独立记录再解析一次:x 取到 'p'、y 取到 'k'、path 被砍掉前两个
       字符。实测 `git mv a.txt b.txt` 会凭空多出两条 path='00/a.txt'、
       status='p'/'k' 的幽灵条目。v2 的 rename 形如
       `2 <XY> ... <X><score> <newPath>\\0<origPath>\\0`,显式区分新旧路径。

    字段下标(整行按空格切分后):
        1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>         → path @8
        2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><sc> <path>  → path @9,
            且紧随的 NUL 字段是 origPath,必须一起吃掉
        u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path> → path @10
        ? <path>                                              → path @1

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

        parts = f.split(" ")
        kind = parts[0]
        if kind == "1" and len(parts) > 8:
            xy, path = parts[1], parts[8]
        elif kind == "2" and len(parts) > 9:
            xy, path = parts[1], parts[9]
            i += 1                          # 吃掉紧随的 origPath 字段
        elif kind == "u" and len(parts) > 10:
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
    (包括它那个 rename 的 bug)——用于对照测量。
    """
    if getattr(server, "_aigit_perf_installed", False):
        return False

    Repo = server.Repo
    orig_status = Repo.status
    orig_do_GET = server.Handler.do_GET
    limits = {"max_lines": max_lines, "page_limit": page_limit,
              "fast_status": bool(fast_status)}

    # 底座:拿全量 status。fast_status 打开时用 v2 单次子进程(顺带修 rename),
    # 否则沿用 server.py 原实现(对照组用)。
    base_fn = make_fast_status() if fast_status else orig_status

    def status(self, offset=None, limit=None):
        """带分页的 status。offset=limit=None → 与原实现返回完全一致。"""
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
            f = q.get("file", [""])[0]
            staged = q.get("staged", ["0"])[0] in ("1", "true", "yes")
            server.REPO.resolve_in_root(f)
            text = server.REPO.diff(f, staged=staged)
            body = truncate_diff(text, cap)
            return handler._json({
                "ok": True, "path": f, "staged": staged,
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


if __name__ == "__main__":
    # 手工自测入口:python aigit_perf.py  打一张能力表。
    print(json.dumps({
        "status": "module ok",
        "truncate": truncate_diff("\n".join(f"l{i}" for i in range(20)), 5),
        "page": paginate([{"path": f"f{i}"} for i in range(25)], 10, 10),
    }, ensure_ascii=False, indent=2))
