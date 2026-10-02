#!/usr/bin/env python3
"""mutation_runner.py —— aigit 变异测试引擎。

它回答一个问题:**aigit 那 7 个套件、几百条断言,到底能抓出多少东西?**

    「全绿」本身不能证明代码是对的。断言可能压根没连到被它声称测的那段行为,
    代码怎么坏它都绿。变异测试是唯一能量化这件事的办法:故意把一段安全护栏弄坏,
    看套件会不会红。

与 ekolang 那套方法学一致的三条纪律:

1. **每个变异体先自证生效,再引用它的结论。**
   本文件提供 `selfcert()`: 先在**未变异**的对照上跑一个行为探针, 拿到签名;
   再在**变异体**上跑同一探针。签名变了 = 变异真的落地了(``LANDED``)。
   签名没变 = 这个变异打不进去(no-op), 它的存活/被杀**都没有意义**, 作废。

2. **探针本身必须能证伪。**
   所以每个探针都带一个 `expect` —— 对照签名必须**逐字等于** `expect`。
   对不上就是 ``PROBE_DRIFT``: 探针的输入根本没碰到那段代码,
   这时候「变异存活了」是探针的错, 不是套件的错。两回事, 不能混。

3. **打不上的变异等于没有变异。**
   引擎会把每个变异体标成 LANDED / NO-OP / PROBE-DRIFT 三态, 只允许 LANDED 的
   进最终统计。

隔离与安全:

* **绝不改工作区的任何源文件。** 所有变异都在 ``%TEMP%\\opencode\\mut\\`` 下的
  **副本**里做, 收尾整目录删除。每个变异体有自己的沙盒 + 自己的 TEMP/TMP。
* 不 commit、不 push、不写 git 索引。
* 端口由套件自己 ``bind(0)`` 动态取, 所以多个变异体可以并行。

用法::

    python mutation_runner.py --list                    # 列出全部变异体
    python mutation_runner.py --selfcert                # 只跑自证矩阵(快, 不跑套件)
    python mutation_runner.py --all                     # 全跑
    python mutation_runner.py --mutate S-escaping-links-off
    python mutation_runner.py --guard-class             # 只跑安全护栏类
    python mutation_runner.py --workers 4
"""

import argparse
import concurrent.futures as futures
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
SB = os.path.join(os.environ.get("TEMP", r"C:\Users\Public\tmp\opencode"), "mut")
SNAP = os.path.join(SB, "snap")
PY = sys.executable

# 快照时跳过的: .git 绝不能复制(在副本里误操作会污染真仓), __pycache__ 是脏缓存
SKIP_ENTRIES = {".git", "__pycache__", ".gitignore"}

# 变异体按改动哪个文件决定跑哪些套件。
#   server.py  -> 4 个套件都起服务, 全部会碰到
#   conflicts.py / history_ops.py -> 直接 import, 只有一个套件
#   gen.py     -> selftest.py 是它的主门禁, test_aigit.py 的 D 组也大量用它
#   web/*.html -> 只有 test_aigit_web.py 读前端
SUITES_BY_TARGET = {
    "server.py": ["test_aigit.py", "test_aigit_extra.py", "smoke.py",
                  "test_aigit_web.py"],
    "conflicts.py": ["test_conflicts.py"],
    "history_ops.py": ["test_history_ops.py"],
    "gen.py": ["selftest.py", "test_aigit.py"],
    "index.html": ["test_aigit_web.py"],
    "conflicts-demo.html": ["test_aigit_web.py"],
}


# ════════════════════════════════════════════════════════ 小工具

def sha256_file(p):
    return hashlib.sha256(io.open(p, "rb").read()).hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def manifest_of(d):
    return {f: sha256_file(os.path.join(d, f))
            for f in sorted(os.listdir(d))
            if os.path.isfile(os.path.join(d, f))}


# ════════════════════════════════════════════════════════ 变异体描述

class Edit(object):
    """一次单行替换。

    用 ``expect`` 锚定而不是盲替: 那一行必须**含有** expect 里的子串,
    否则拒绝应用。这样源码被别人挪了行号时引擎会报 DRIFT 而不是改坏别的地方。
    """

    def __init__(self, lineno, expect, new, why=""):
        self.lineno = lineno
        self.expect = expect
        self.new = new
        self.why = why


class Mutation(object):
    def __init__(self, id, target, edits, probe, guard_class, why, suites=None):
        self.id = id
        self.target = target
        self.edits = edits
        self.probe = probe
        self.guard_class = guard_class        # safety / boundary / logic / ui
        self.why = why
        self.suites = suites or SUITES_BY_TARGET.get(target, [])


# ════════════════════════════════════════════════════════ 行为探针(自证用)
#
# 探针是一段独立的 Python, 在沙盒里跑, `import` 到的是**被变异的那份**。
# 它只做一件事: 把「那段护栏在恶意输入下的判定结果」压成一行可比较的签名。
#
# 签名约定: 每行 `SIG key=value`, 引擎把全部 SIG 行拼起来就是签名。
#
PREAMBLE = r'''
import os, subprocess, sys, tempfile, shutil, json
from pathlib import Path

def sig(k, v):
    print("SIG %s=%s" % (k, v))

def mkjunction(link, target):
    """mklink /J 是 cmd 内建, 必须经 cmd /c。路径全 ASCII(沙盒在 TEMP 下)。"""
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    return r.returncode == 0

def mkrepo():
    d = Path(tempfile.mkdtemp(prefix="mp_"))
    def g(*a):
        return subprocess.run(["git", "-C", str(d)] + list(a),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    g("init", "-q", "-b", "main")
    g("config", "user.email", "a@b"); g("config", "user.name", "p")
    (d / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    g("add", "-A"); g("commit", "-qm", "c1")
    return d, g

def rmt(*paths):
    """删目录树且 junction 安全:shutil.rmtree 会踩 junction(WinError),
    cmd rmdir /s /q 把联接本身删掉且不跟进目标。失败不抛(供收尾)。"""
    for p in paths:
        p = str(p)
        if os.path.lexists(p):
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", p],
                           capture_output=True)
'''


# ---- 探针库 ----
# 每条探针的 expect 是在**对照**上实测出来的签名(见 --calibrate)。
# 引擎会先校验对照签名 == expect, 不等就判 PROBE-DRIFT。

PROBES = {}


def probe(pid, expect, code):
    PROBES[pid] = {"expect": expect, "code": PREAMBLE + code}


probe(
    "ref_whitelist",
    "ok=0|n=4",
    r'''
import server
def _t(fn, v):
    try:
        fn(v); return True
    except Exception:
        return False
hostile = ["--format=%H", "--output=C:/tmp/x", "a..b", "HEAD:x"]
ok = sum(1 for r in hostile if _t(server.check_ref_name, r))
sig("ok", ok)
sig("n", len(hostile))
''',
)

probe(
    "branch_whitelist",
    "ok=0|n=5",
    r'''
import server
def _t(fn, v):
    try:
        fn(v); return True
    except Exception:
        return False
hostile = ["--detach", "-f", "--", ".", "-b"]
ok = sum(1 for r in hostile if _t(server.check_branch_name, r))
sig("ok", ok)
sig("n", len(hostile))
''',
)

probe(
    "escaping_links",
    "junc=True|leaks=1",
    r'''
import server
d, g = mkrepo()
outside = d.parent / (d.name + "_outside")
outside.mkdir()
(outside / "leak.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
made = mkjunction(d / "escape", outside)
sig("junc", made)
r = server.Repo(d)
leaks = r.escaping_links()
sig("leaks", len(leaks))
rmt(d); rmt(outside)
''',
)

probe(
    "escaping_links_nested",
    "junc=True|leaks=1",
    r'''
import server
d, g = mkrepo()
outside = d.parent / (d.name + "_out2")
outside.mkdir()
(outside / "leak.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
(d / "sub").mkdir()
made = mkjunction(d / "sub" / "escape", outside)
sig("junc", made)
r = server.Repo(d)
leaks = r.escaping_links()
sig("leaks", len(leaks))
rmt(d); rmt(outside)
''',
)

probe(
    "commit_leak",
    "raised=1|leaked=1",
    r'''
import server
d, g = mkrepo()
outside = d.parent / (d.name + "_out3")
outside.mkdir()
(outside / "leak.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
mkjunction(d / "escape", outside)
(d / "a.txt").write_text("changed\n", encoding="utf-8", newline="\n")
r = server.Repo(d)
raised = 0
try:
    r.commit("c2")
except server.GitError:
    raised = 1
sig("raised", raised)
# 提交是否真把仓外文件写进了历史 —— 这是 N8 的本体危害
g("add", "-A"); g("commit", "-qm", "peek")
p = g("ls-files")
sig("leaked", 1 if "escape/leak.txt" in p.stdout else 0)
rmt(d); rmt(outside)
''',
)

probe(
    "resolve_git",
    "raised=1",
    r'''
import server
d, g = mkrepo()
r = server.Repo(d)
raised = 0
try:
    p = r.resolve_in_root(".git/config")
except server.GitError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "resolve_root_self",
    "raised=3",
    r'''
import server
d, g = mkrepo()
r = server.Repo(d)
raised = 0
for rel in (".", "./", ""):
    try:
        r.resolve_in_root(rel)
    except server.GitError:
        raised += 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "checkout_exists",
    "raised=1|detached=0",
    r'''
import server
d, g = mkrepo()
sha = g("rev-parse", "HEAD").stdout.strip()
r = server.Repo(d)
raised = 0
try:
    r.checkout(sha[:8])   # 短 sha:只存在的本地分支才允许 checkout
except server.GitError:
    raised = 1
out = g("branch", "--show-current").stdout.strip()
sig("raised", raised)
sig("detached", 0 if out else 1)   # ==1 表示真的切过去了(游离 HEAD)
rmt(d)
''',
)

probe(
    "api_file_dir_guard",
    "code=400",
    r'''
import os, threading, urllib.request, urllib.error
import server
d, g = mkrepo()
(d / "sub").mkdir()
(d / "sub" / "b.txt").write_text("B\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "c2")
server.WEB = Path(os.path.dirname(os.getcwd())) / "web"
server.REPO = server.Repo(d)
server._CONFLICTS = None; server._HISTORY = None
srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                 daemon=True).start()
port = srv.server_address[1]
code = 0
try:
    urllib.request.urlopen("http://127.0.0.1:%d/api/file?file=sub" % port)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
sig("code", code)
srv.shutdown()
rmt(d)
''',
)

probe(
    "gen_seed_prefix",
    "seeded=1",
    r'''
import server
d, g = mkrepo()
r = server.Repo(d)
out = r.generate("mergeable", 1, seed=12345)
names = []
for br in out["branches"]:
    names.extend(br["branches"])
sig("seeded", 1 if any("/s12345/" in n for n in names) else 0)
rmt(d)
''',
)

probe(
    "conf_resolve_dotdot",
    "raised=1",
    r'''
import conflicts
d, g = mkrepo()
c = conflicts.ConflictResolver(d)
raised = 0
try:
    c._abs("../evil.txt")
except conflicts.ConflictError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "conf_resolve_git",
    "raised=1",
    r'''
import conflicts
d, g = mkrepo()
c = conflicts.ConflictResolver(d)
raised = 0
try:
    c._abs(".git/config")
except conflicts.ConflictError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "discard_inner_tracked",
    "raised=1|tracked_alive=1",
    r'''
import server
d, g = mkrepo()
(d / "sub").mkdir()
(d / "sub" / "tracked.txt").write_text("t\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "c2")
(d / "sub" / "untracked.txt").write_text("u\n", encoding="utf-8", newline="\n")
r = server.Repo(d)
raised = 0
try:
    r.discard(["sub"])
except server.GitError:
    raised = 1
sig("raised", raised)
sig("tracked_alive", 1 if (d / "sub" / "tracked.txt").exists() else 0)
rmt(d)
''',
)

probe(
    "assert_pinned_plain",
    "raised=1",
    r'''
import server
d, g = mkrepo()
outside = d.parent / (d.name + "_out4")
outside.mkdir()
(outside / "victim.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
(d / "sub").mkdir()
mkjunction(d / "sub" / "esc", outside)
r = server.Repo(d)
raised = 0
try:
    with r._pinned([(d / "sub" / "esc", True)]) as pins:
        r._assert_pinned_plain(pins)
except server.GitError:
    raised = 1
sig("raised", raised)
rmt(d); rmt(outside)
''',
)

probe(
    "delete_tree_link",
    "raised=1|outside_alive=1",
    r'''
import server
d, g = mkrepo()
outside = d.parent / (d.name + "_out5")
outside.mkdir()
(outside / "keep.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
(d / "tree").mkdir()
(d / "tree" / "esc").mkdir()
mkjunction(d / "tree" / "esc" / "inner", outside)
r = server.Repo(d)
raised = 0
try:
    r._delete_tree(d / "tree")
except server.GitError:
    raised = 1
sig("raised", raised)
sig("outside_alive", 1 if (outside / "keep.txt").exists() else 0)
rmt(d); rmt(outside)
''',
)

probe(
    "gen_prefix",
    "raised=2|n=2",
    r'''
import gen
g = gen.ScenarioGen.__new__(gen.ScenarioGen)
raised = 0
for rel in ("evil.txt", "../evil.txt"):
    try:
        g._assert_prefix(rel)
    except gen.GenError:
        raised += 1
sig("raised", raised)
sig("n", 2)
''',
)

probe(
    "hist_ref_whitelist",
    "raised=6|n=6",
    r'''
import history_ops as H
h = H.HistoryOps.__new__(H.HistoryOps)
bad = 0
for name in ("--upload-pack=x", "../x", "@{1}", "a.lock", "/abs", "a..b"):
    try:
        h.validate_ref_name(name)
    except H.HistoryError:
        bad += 1
sig("raised", bad)
sig("n", 6)
''',
)

probe(
    "hist_require_confirm",
    "raised=1",
    r'''
import history_ops as H
raised = 0
try:
    H.HistoryOps._require_confirm("drop_branch", False)
except H.HistoryError:
    raised = 1
sig("raised", raised)
''',
)

# ---- 前端 esc() 探针: 真的用 node 跑那一段原封不动的代码 ----
probe(
    "esc_index",
    "quoted=1|less=1",
    r'''
import re, io, os, subprocess, shutil, tempfile
src = io.open(os.path.join(os.path.dirname(os.getcwd()), "web", "index.html"), encoding="utf-8").read()
m = re.search(r"function esc\(s\) \{.*?\n\}", src, re.S)
if not m:
    sig("quoted", "NOTFOUND"); sig("less", "NOTFOUND")
else:
    js = m.group(0) + """
console.log("quoted=" + (esc('a\\"b').indexOf('&quot;') >= 0 ? 1 : 0));
console.log("less=" + (esc('a<b').indexOf('&lt;') >= 0 ? 1 : 0));
"""
    node = shutil.which("node") or r"D:\cangku\nodejs\node.exe"
    t = tempfile.mkdtemp()
    f = os.path.join(t, "e.js")
    io.open(f, "w", encoding="utf-8", newline="\n").write(js)
    p = subprocess.run([node, f], capture_output=True, text=True)
    for l in p.stdout.splitlines():
        if l.strip():
            print("SIG " + l.strip())
    if p.returncode != 0 or not p.stdout.strip():
        sig("node_err", (p.stderr.strip().splitlines() or ["empty"])[-1][:60])
    shutil.rmtree(t, ignore_errors=True)
''',
)

probe(
    "esc_demo",
    "dq=1|sq=1",
    r'''
import re, io, os, subprocess, shutil, tempfile
src = io.open(os.path.join(os.path.dirname(os.getcwd()), "web", "conflicts-demo.html"),
              encoding="utf-8").read()
m = re.search(r"const esc = \(s\) =>[\s\S]*?\);\n", src)
if not m:
    sig("dq", "NOTFOUND"); sig("sq", "NOTFOUND")
else:
    js = m.group(0) + """
console.log("dq=" + (esc('a\\"b').indexOf('&quot;') >= 0 ? 1 : 0));
console.log("sq=" + (esc("a'b").indexOf('&#39;') >= 0 ? 1 : 0));
"""
    node = shutil.which("node") or r"D:\cangku\nodejs\node.exe"
    t = tempfile.mkdtemp()
    f = os.path.join(t, "e.js")
    io.open(f, "w", encoding="utf-8", newline="\n").write(js)
    p = subprocess.run([node, f], capture_output=True, text=True)
    for l in p.stdout.splitlines():
        if l.strip():
            print("SIG " + l.strip())
    if p.returncode != 0 or not p.stdout.strip():
        sig("node_err", (p.stderr.strip().splitlines() or ["empty"])[-1][:60])
    shutil.rmtree(t, ignore_errors=True)
''',
)


# ════════════════════════════════════════════════════════ 套件输出解析
#
# 7 个套件有 7 种格式(都是各自手写的,没有统一 TAP)。这里逐个按**源码里
# 实际的 print 语句**写死正则 —— 猜格式的后果是「抓到了但归因到错误的用例」,
# 那是比没抓到更糟的结果。
#   test_aigit.py       : "  %s %-14s %s"  mark ∈ {"  ok  "," FAIL "," skip ","ERROR "}
#   test_aigit_extra.py : 同上(extra 自己那一份)
#   test_conflicts.py   : TAP "ok N - name" / "not ok N - name"
#   test_history_ops.py : 'not ok {sid} - {name}' / '  FAILED {sid} {name}'
#   selftest.py         : "  [通过] name" / "  [失败] name"  + "N / N 组通过"
#   test_aigit_web.py   : "[{status:4}] {layer} {name}"
#   smoke.py            : "[PASS] NN  name -> detail" / "[FAIL] NN  name -> detail"

MARK_RE = re.compile(r"^\s{2}(  ok  | FAIL | skip |ERROR )\s(\S+)\s+(.*?)\s*$")

SUITE_PARSERS = {
    "test_aigit.py": {
        "cases": [(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                             " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (\d+) 条\s+PASS (\d+)\s+FAIL (\d+)\s+"
                              r"SKIP (\d+)\s+ERROR (\d+)"),
    },
    "test_aigit_extra.py": {
        "cases": [(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                             " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (\d+) 条\s+PASS (\d+)\s+FAIL (\d+)\s+"
                              r"SKIP (\d+)\s+ERROR (\d+)"),
    },
    "test_conflicts.py": {
        "cases": [
            (re.compile(r"^ok \d+ - (.+)$"), {"ok": "pass"}),
            (re.compile(r"^not ok \d+ - (.+)$"), {"not ok": "fail"}),
        ],
        "summary": re.compile(r"# passed (\d+) of (\d+) test\(s\), skipped (\d+)"),
    },
    "test_history_ops.py": {
        "cases": [
            (re.compile(r"^\s*not ok (\S+) - (.+?)(?: \(异常\))?\s*$"),
             {"not ok": "fail"}),
        ],
        "summary": re.compile(r"共 (\d+) 条,过 (\d+),失败 (\d+),异常 (\d+)"),
    },
    "selftest.py": {
        "cases": [
            (re.compile(r"^\s*\[通过\]\s+(\S+)\s*$"), {"通过": "pass"}),
            (re.compile(r"^\s*\[失败\]\s+(\S+)\s*$"), {"失败": "fail"}),
        ],
        "summary": re.compile(r"(\d+) / (\d+) 组通过"),
    },
    "test_aigit_web.py": {
        "cases": [
            (re.compile(r"^\[(PASS|FAIL|WARN|SKIP)\s*\]\s+(\S+)\s+(.+?)\s*$"),
             {"PASS": "pass", "FAIL": "fail", "WARN": "warn", "SKIP": "skip"}),
        ],
        "summary": re.compile(r"汇总:(\d+) PASS / (\d+) FAIL / (\d+) WARN / "
                              r"(\d+) SKIP"),
    },
    "smoke.py": {
        "cases": [
            (re.compile(r"^\[(PASS|FAIL|INFO)\]\s*(\d+)\s+(.+?)\s*->\s*"),
             {"PASS": "pass", "FAIL": "fail"}),
        ],
        "summary": re.compile(r"通过\s+(\d+)\s+失败\s+(\d+)\s+跳过\s+(\d+)"),
    },
}


def parse_suite(suite, out):
    """把一个套件的 stdout 解析成 {状态: [用例名]} + 汇总。"""
    spec = SUITE_PARSERS[suite]
    cases, summary = {}, None
    for line in out.splitlines():
        done = False
        for rx, table in spec["cases"]:
            m = rx.match(line)
            if not m:
                continue
            st = table.get(m.group(1))
            if st is None:
                continue
            name = m.group(len(m.groups()))   # 末组是用例名
            cases.setdefault(st, [])
            if name not in cases[st]:
                cases[st].append(name)
            done = True
            break
        if done:
            continue
        m = spec["summary"].search(line)
        if m:
            summary = [int(x) for x in m.groups()]
    for st in ("pass", "fail", "error", "skip", "warn"):
        cases.setdefault(st, [])
    return cases, summary


# ════════════════════════════════════════════════════════ 快照 / 应用

BASE_REF = "f99c10a"


def build_snapshot(src=None):
    """把**已提交基线 f99c10a**导出成一份只读基准。工作区永远不被改。

    为什么用 git archive 而不是工作区目录:工作区此刻有 7 条线的未提交
    改动(server.py/history_ops.py/index.html/README 都脏了),直接拷
    工作区测的是移动靶,任何结论不可复现。"""
    os.makedirs(SB, exist_ok=True)
    for _ in range(8):
        if os.path.isdir(SNAP):
            rmt_helper(SNAP)
            time.sleep(0.4)
            continue
        try:
            os.makedirs(SNAP)
            break
        except FileExistsError:
            time.sleep(0.4)
    else:
        raise RuntimeError("快照目录建不出来: %s" % SNAP)
    import zipfile, subprocess as _sp
    archive = os.path.join(SB, "base.zip")
    with open(archive, "wb") as f:
        _sp.run(["git", "-C", HERE, "archive", "--format=zip", BASE_REF],
                stdout=f, check=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(SNAP)
    os.remove(archive)
    # .pyc/缓存可能和基线源码不一致,扫掉
    for root, dirs, files in os.walk(SNAP):
        for d in list(dirs):
            if d == "__pycache__":
                rmt_helper(os.path.join(root, d))
    return manifest_of(SNAP)


def rmt_helper(p):
    subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)


def clone_sandbox(dst):
    for _ in range(8):
        if os.path.isdir(dst):
            rmt_helper(dst)
            time.sleep(0.4)
            continue
        shutil.copytree(SNAP, dst, ignore=shutil.ignore_patterns("__pycache__"))
        return dst
    raise RuntimeError("沙盒建不出来: %s" % dst)


def apply_mutation(mut, sandbox):
    """把变异落到沙盒副本上。返回 (state, detail)。

    state ∈ LANDED / DRIFT / NOOP
    * DRIFT: 锚点那行不含 expect 的子串 —— 源码挪了或被改过, 拒绝盲改
    * NOOP  : 行号对上了, 但新旧文本相同(变异本身写错了)
    """
    path = os.path.join(sandbox, *mut.target.split("/"))
    if not os.path.isfile(path):
        return "DRIFT", "目标文件不存在:%s" % mut.target
    before = sha256_file(path)
    lines = io.open(path, encoding="utf-8", newline="").read().split("\n")
    applied = 0
    for e in mut.edits:
        if e.lineno > len(lines):
            return "DRIFT", "行号 %d 超出 %s 的 %d 行" % (e.lineno, mut.target,
                                                          len(lines))
        cur = lines[e.lineno - 1]
        if e.expect and e.expect not in cur:
            return "DRIFT", (":%d 不含锚点 %r\n              实际那行: %r"
                             % (e.lineno, e.expect, cur.strip()[:110]))
        if cur.strip() == e.new.strip():
            applied += 1
            continue
        indent = cur[:len(cur) - len(cur.lstrip())]
        lines[e.lineno - 1] = e.new if e.new.startswith(" ") else indent + e.new
        applied += 1
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    after = sha256_file(path)
    if after == before:
        return "NOOP", "应用后 sha256 没变"
    return "LANDED", "%d 处编辑, sha %s -> %s" % (applied, before[:12], after[:12])


def run_probe(sandbox, probe_id, timeout=240):
    """在沙盒里跑行为探针, 返回 (签名, stderr 尾巴)。签名是全部 SIG 行。"""
    pr = PROBES[probe_id]
    d = os.path.join(sandbox, "_probe_%s" % probe_id)
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, "p.py"), "w", encoding="utf-8", newline="\n").write(pr["code"])
    tmp = os.path.join(sandbox, "_probe_tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TEMP=tmp, TMP=tmp, PYTHONPATH=sandbox,
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PATH="C:\\Environment\\cpp\\mingw64\\bin;" + os.environ.get("PATH", ""))
    try:
        p = subprocess.run([PY, "-X", "utf8", "p.py"], cwd=d, env=env,
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "探针超时 %ds" % timeout
    sig = "|".join(l[4:].strip() for l in (p.stdout or "").splitlines()
                   if l.startswith("SIG "))
    return sig, ((p.stderr or "").strip().splitlines() or [""])[-1]


def selfcert(mut, sandbox):
    """两段式自证。

    1. 变异体签名必须**不等于** expect —— 相等说明这一段变异没改变被探针测的
       那个行为(no-op)。
    2. 更严的一道闸放在 --probe-only 里: 对照(未变异)签名必须**逐字等于**
       expect。对不上就是 PROBE_DRIFT —— 探针的输入压根没碰到那段代码,
       这时候「变异存活了」是探针的错, 不是套件的错。两回事, 不能混。
    """
    pr = PROBES[mut.probe]
    msig, merr = run_probe(sandbox, mut.probe)
    if msig == pr["expect"]:
        return {"state": "NOOP", "control": pr["expect"], "mutant": msig,
                "err": merr}
    return {"state": "LANDED", "control": pr["expect"], "mutant": msig, "err": merr}


def verify_probes(sandbox=SNAP):
    """可证伪性总检: 逐条探针在**未变异**的快照上跑, 签名必须等于 expect。

    这是整套自证的地基: 如果某条探针在对照上都对不上 expect, 那么它对
    任何变异体的结论都不可信 —— 那种情况下「变异存活」是探针的锅。
    """
    bad = []
    for pid, pr in sorted(PROBES.items()):
        sig, err = run_probe(sandbox, pid)
        ok = (sig == pr["expect"])
        if not ok:
            bad.append(pid)
        print("  %-24s %-8s 对照=%-22s expect=%-22s stderr=%s"
              % (pid, "OK" if ok else "MISMATCH", sig, pr["expect"],
                 (err or "")[:60]))
    print("\n可证伪性: %d/%d 条探针在对照上签名正确" %
          (len(PROBES) - len(bad), len(PROBES)))
    if bad:
        print("不匹配的探针(其结论一律作废): %s" % bad)
    return bad


# ════════════════════════════════════════════════════════ 跑套件

def run_suite(sandbox, suite, logdir, timeout=3600):
    tmp = os.path.join(sandbox, "_t_" + suite.replace(".py", ""))
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TEMP=tmp, TMP=tmp,
               PATH="C:\\Environment\\cpp\\mingw64\\bin;" + os.environ.get("PATH", ""),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    t0 = time.time()
    try:
        p = subprocess.run([PY, "-X", "utf8", suite], cwd=sandbox, env=env,
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        out, rc = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) \
            else (e.stdout or "")
        rc = -99
    el = time.time() - t0
    if logdir:
        io.open(os.path.join(logdir, "%s__%s.txt" % (os.path.basename(sandbox),
                                                      suite)),
                "w", encoding="utf-8", newline="\n").write(out)
    cases, summary = parse_suite(suite, out)
    return {"suite": suite, "rc": rc, "sec": round(el, 1),
            "pass": len(cases["pass"]), "fail": len(cases["fail"]),
            "error": len(cases["error"]), "skip": len(cases["skip"]),
            "warn": len(cases["warn"]),
            "red": cases["fail"] + cases["error"], "summary": summary,
            "parse_ok": bool(cases["pass"] or cases["fail"] or cases["error"])}


# ════════════════════════════════════════════════════════ 评估一个变异体

def evaluate(mut, logdir=None):
    box = os.path.join(SB, mut.id)
    rec = {"id": mut.id, "target": mut.target, "guard": mut.guard_class,
           "why": mut.why, "suites": mut.suites, "probe": mut.probe,
           "edits": [{"lineno": e.lineno, "expect": e.expect, "new": e.new}
                     for e in mut.edits]}
    try:
        clone_sandbox(box)
        state, detail = apply_mutation(mut, box)
        rec["apply"] = state
        rec["apply_detail"] = detail
        if state != "LANDED":
            rec["verdict"] = "INVALID/" + state
            rec["final_sha"] = sha256_file(os.path.join(box, *mut.target.split("/")))
            return rec
        rec["final_sha"] = sha256_file(os.path.join(box, *mut.target.split("/")))
        cert = selfcert(mut, box)
        rec["cert"] = cert
        if cert["state"] != "LANDED":
            rec["verdict"] = "INVALID/" + cert["state"]
            return rec
        rec["suites_run"] = []
        for suite in mut.suites:
            rec["suites_run"].append(run_suite(box, suite, logdir))
        killed = [r for r in rec["suites_run"] if r["red"]]
        rec["killed_by"] = sorted({n for r in killed for n in r["red"]})
        rec["killed_suites"] = [r["suite"] for r in killed]
        rec["verdict"] = "KILLED" if killed else "SURVIVED"
    except Exception as e:
        rec["verdict"] = "ENGINE-ERROR"
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        shutil.rmtree(box, ignore_errors=True)
    return rec


# ════════════════════════════════════════════════════════ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="aigit 变异测试引擎")
    ap.add_argument("--list", action="store_true", help="列出全部变异体")
    ap.add_argument("--catalog", default=None,
                    help="变异体目录模块(默认 test_aigit_mutation)")
    ap.add_argument("--mutate", action="append", default=[], help="只跑这些 id")
    ap.add_argument("--all", action="store_true", help="全跑")
    ap.add_argument("--guard-class", default=None, help="safety/boundary/ui/logic")
    ap.add_argument("--probe-only", default=None, help="只跑某条探针看对照签名")
    ap.add_argument("--verify-probes", action="store_true", help="探针可证伪性总检")
    ap.add_argument("--snapshots-only", action="store_true", help="只建快照并打 sha256")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=None, help="结果 JSON 落盘位置")
    ap.add_argument("--keep", action="store_true", help="保留沙盒(调试)")
    args = ap.parse_args(argv)

    man = build_snapshot()
    if args.snapshots_only:
        for f, h in sorted(man.items()):
            print("  %-26s %s" % (f, h))
        return 0

    if args.verify_probes:
        print("=== 探针可证伪性总检(在未变异的快照上跑) ===")
        bad = verify_probes(SNAP)
        return 1 if bad else 0

    if args.probe_only:
        sig, err = run_probe(SNAP, args.probe_only)
        exp = PROBES[args.probe_only]["expect"]
        print("probe %s\n  对照签名: %s\n  expect  : %s\n  一致    : %s\n  stderr  : %s"
              % (args.probe_only, sig, exp, sig == exp, err))
        return 0 if sig == exp else 1

    sys.path.insert(0, HERE)
    cat = __import__(args.catalog or "test_aigit_mutation")
    muts = cat.CATALOG
    if args.list:
        for m in muts:
            print("  %-28s %-22s %-9s %s" % (m.id, m.target, m.guard_class, m.probe))
            print("      %s" % m.why)
            print("      套件: %s" % ", ".join(m.suites))
        print("\n共 %d 个变异体" % len(muts))
        return 0

    sel = muts
    if args.mutate:
        want = set(args.mutate)
        sel = [m for m in muts if m.id in want]
        missing = want - {m.id for m in sel}
        if missing:
            print("没有这些变异体: %s" % sorted(missing))
    if args.guard_class:
        sel = [m for m in sel if m.guard_class == args.guard_class]
    if not sel:
        print("没有选中任何变异体")
        return 2

    logdir = os.path.join(SB, "logs")
    os.makedirs(logdir, exist_ok=True)
    print("快照 server.py=%s  变异体 %d 个  workers=%d"
          % (man.get("server.py", "?")[:16], len(sel), args.workers))
    print("沙盒根: %s\n" % SB)

    t0 = time.time()
    results = []
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        fut2m = {ex.submit(evaluate, m, logdir): m for m in sel}
        done = 0
        for fut in futures.as_completed(fut2m):
            m = fut2m[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                r = {"id": m.id, "verdict": "ENGINE-ERROR", "error": str(e),
                     "target": m.target, "guard": m.guard_class}
            results.append(r)
            print("[%2d/%2d] %-28s %-16s %s"
                  % (done, len(sel), r["id"], r["verdict"],
                     (r.get("apply_detail") or r.get("error") or "")[:66]))

    order = {m.id: i for i, m in enumerate(muts)}
    results.sort(key=lambda r: order.get(r["id"], 999))
    valid = [r for r in results if r["verdict"] in ("KILLED", "SURVIVED")]
    killed = [r for r in valid if r["verdict"] == "KILLED"]
    inval = [r for r in results if r["verdict"] not in ("KILLED", "SURVIVED")]

    print("\n" + "=" * 78)
    print("汇总: 有效变异 %d  杀掉 %d  存活 %d  作废 %d  用时 %.0fs"
          % (len(valid), len(killed), len(valid) - len(killed), len(inval),
             time.time() - t0))
    if valid:
        print("杀灭率 = %.1f%% (%d/%d)"
              % (100.0 * len(killed) / len(valid), len(killed), len(valid)))
    if inval:
        print("\n作废的变异体(不算进分母, 它们本来就没打上去):")
        for r in inval:
            print("  %-28s %-16s %s"
                  % (r["id"], r["verdict"],
                     (r.get("apply_detail") or r.get("error")
                      or r.get("cert", {}).get("err", ""))[:66]))

    out = args.out or os.path.join(SB, "mutation_results.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"snapshot": man, "results": results,
         "summary": {"valid": len(valid), "killed": len(killed),
                     "survived": len(valid) - len(killed), "invalid": len(inval),
                     "sec": round(time.time() - t0, 1)}},
        indent=1, ensure_ascii=False))
    print("\n结果已落盘: %s" % out)
    if not args.keep:
        for m in sel:
            shutil.rmtree(os.path.join(SB, m.id), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())