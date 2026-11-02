# -*- coding: utf-8 -*-
"""commit / tag 对象的**最小解析**（只取 aigit 展示需要的字段）。

只服务两个只读端点：`/api/history/tags`、`/api/history/reflog`。它们原来的
`for-each-ref` / `reflog` 里带 `%(contents:subject)`、`%(creatordate:short)`、
`%an`、`%ad` —— 这些都要读对象，所以配合常驻 `cat-file --batch` 在进程内算。

**格式规则是实测钉住的**（git 2.50.1）：
  * subject = 消息的**第一段**：取到第一个空行为止，行与行之间用一个空格连接
    （`log --pretty=%s` 与 `%(contents:subject)` 一致：`"L1\nL2\n\nbody"` → `"L1 L2"`）；
  * `%ad`（`--date=iso`）= `YYYY-MM-DD HH:MM:SS +ZZZZ`，时区是**作者/打标签者
    记录在对象里的那个偏移**，不是本机时区；
  * 注解 tag 的 `%(creatordate)` 取 **tagger** 时间；轻量 tag 取 **committer** 时间。

任何一处看不懂（非 UTF-8、缺 author/tagger、带 encoding 头）都返回 None，
由调用方回退到 git —— 这是显示层，宁可慢也不能显示错。
"""

from datetime import datetime, timedelta, timezone


def _split(data):
    """(header 行列表, 消息正文) ；不支持带续行的多值头？支持 —— 续行并入上一行。"""
    text = data.decode("utf-8")            # 非 UTF-8 直接抛，由调用方回退
    head, sep, body = text.partition("\n\n")
    lines = []
    for raw in head.split("\n"):
        if raw.startswith(" ") and lines:
            lines[-1] += "\n" + raw[1:]
            continue
        lines.append(raw)
    return lines, (body if sep else "")


def _ident(line, prefix):
    """"author Name <mail> 1793093032 +0800" → (name, ts, tz)。"""
    if not line.startswith(prefix + " "):
        return None
    rest = line[len(prefix) + 1:]
    lt = rest.find(" <")
    if lt <= 0:
        return None
    name = rest[:lt]
    tail = rest[rest.rfind(">") + 1:].strip().split()
    if len(tail) != 2:
        return None
    try:
        ts = int(tail[0])
    except ValueError:
        return None
    tz = tail[1]
    if len(tz) != 5 or tz[0] not in "+-" or not tz[1:].isdigit():
        return None
    return (name, ts, tz)


def parse_commit(data):
    """→ {"author": (name, ts, tz), "committer": (...), "message": str} | None"""
    try:
        lines, body = _split(data)
    except UnicodeDecodeError:
        return None
    author = committer = None
    for ln in lines:
        if ln.startswith("encoding "):
            return None                    # 消息不是 UTF-8，交回 git
        if author is None and ln.startswith("author "):
            author = _ident(ln, "author")
        elif committer is None and ln.startswith("committer "):
            committer = _ident(ln, "committer")
    if author is None or committer is None:
        return None
    return {"author": author, "committer": committer, "message": body}


def parse_tag(data):
    """→ {"object": sha, "type": str, "tag": str, "tagger": (name, ts, tz),
           "message": str} | None"""
    try:
        lines, body = _split(data)
    except UnicodeDecodeError:
        return None
    obj = typ = name = tagger = None
    for ln in lines:
        if ln.startswith("object "):
            obj = ln[len("object "):].strip()
        elif ln.startswith("type "):
            typ = ln[len("type "):].strip()
        elif ln.startswith("tag "):
            name = ln[len("tag "):].strip()
        elif ln.startswith("tagger "):
            tagger = _ident(ln, "tagger")
    if not obj or not typ or not name or tagger is None:
        return None
    return {"object": obj, "type": typ, "tag": name, "tagger": tagger,
            "message": body}


def subject(message):
    """第一段：到第一个空行为止，行间用一个空格连接。"""
    out = []
    for line in (message or "").split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            break
        out.append(line.strip())
    return " ".join(out)


def _local(ts, tz):
    sign = 1 if tz[0] == "+" else -1
    off = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(minutes=off)))


def iso(ts, tz):
    """`--date=iso` 的形状：YYYY-MM-DD HH:MM:SS +ZZZZ。"""
    d = _local(ts, tz)
    return "%04d-%02d-%02d %02d:%02d:%02d %s" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second, tz)


def short_date(ts, tz):
    """`%(creatordate:short)` 的形状：YYYY-MM-DD。"""
    d = _local(ts, tz)
    return "%04d-%02d-%02d" % (d.year, d.month, d.day)
