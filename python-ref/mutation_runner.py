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

两条关于**基线与锚点**的纪律(2026-10-03 加, 补的是同一个病的两个实例):

4. **基线不许写死。** ``--base-ref`` 默认 ``HEAD``,语义就是 ``git archive <ref>``。
   曾经这里是个模块常量 ``BASE_REF = "f99c10a"``,于是代码提交了、装置照跑、
   报得出一个漂亮百分比,而它测的是两天前的代码 —— 没有任何告警。
5. **锚点不许是行号。** 每个 :class:`Edit` 用**唯一上下文子串**定位;
   0 处命中报 ``GONE``,多于 1 处报 ``AMBIGUOUS``。``AMBIGUOUS`` 是硬失败:
   取第一个命中就是又一种静默错位。

**VOID ≠ SURVIVED。** 锚点没了的变异体是「没打上去」,不是「打上去没被杀掉」。
两者混进同一个分母,杀灭率就成了伪造的结论。汇总里两者分开计数。

隔离与安全:

* **绝不改工作区的任何源文件。** 所有变异都在 ``%TEMP%\\opencode\\mut\\`` 下的
  **副本**里做, 收尾整目录删除。每个变异体有自己的沙盒 + 自己的 TEMP/TMP。
* 不 commit、不 push、不写 git 索引。

一处已知的**不对称**,读结果时必须知道: 快照(被测代码)来自 ``--base-ref``,
但**变异体目录和引擎本身是从工作区加载的**(``sys.path.insert(0, HERE)``)。
所以改 catalog 不会改快照,改快照也不会改 catalog。

用法::

    python mutation_runner.py --list                    # 列出全部变异体
    python mutation_runner.py --snapshots-only          # 只建快照并打 sha256
    python mutation_runner.py --verify-probes           # 探针可证伪性总检(快)
    python mutation_runner.py --all                     # 全跑
    python mutation_runner.py --all --base-ref 1a1f1b1  # 钉死基线,可复现
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
    "gitops.py": ["test_aigit_net.py"],
    "history_ops.py": ["test_history_ops.py"],
    "gen.py": ["selftest.py", "test_aigit.py"],
    "index.html": ["test_aigit_web.py"],
    # 2026-10-27: 引用/reflog/索引直读的下沉模块(快路径/回退的两套差分都在这儿)
    "gitrefs.py": ["test_index_fastpath.py", "test_refstore.py"],
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
    """一次单行替换,用**唯一的上下文子串**定位。行号已从数据结构里彻底删除,
    不是「保留但不当判据」—— 留着就还会有人拿它当唯一判据。

    定位契约(apply_mutation 逐条执行,任何一条不满足都是**硬失败**):

    * ``anchor`` 在目标文件里必须**恰好命中 1 处**。0 处 -> ``GONE``,
      多于 1 处 -> ``AMBIGUOUS``。**绝不**取第一个 —— 取第一个就是又一种
      静默错位,比行号锚点更难查。
    * ``anchor`` 可以**跨行**(含 ``\\n``)。跨行时按窗口匹配,并约定
      **窗口的第一行**就是被改的那一行,其余各行是用来消歧的**后文**上下文。
      这不是可选装饰:server.py 里 ``discard()`` 的拦截和 ``discard`` 预览的
      blocked 清单是两份近乎逐字重复的代码,单行锚点必然二义
      (实测 ``if inner:`` 命中 2 处,行号锚点则会默默改错一处)。
    * 匹配前每行先做**空白归一化**(连续空白折叠成单空格、去首尾),所以锚点
      不依赖缩进 —— 重新缩进代码不该让锚点失效。
    * ``expect`` 必须在那唯一命中行里(改**之前**)。它是「找对行了吗」的
      二次确认,证明 anchor 不是碰巧撞上另一段。
    * 改**之后**再验一次:该行必须含 ``new`` 的字面量(按同一套缩进归一化)。

    搜索有**明确上界**:``window`` 为 0 时上界就是文件行数,否则为 window 行。
    上界内找不到就是 ``GONE``。**没有**放宽循环、没有重试、没有越界的
    「再找找看」—— 那种循环实测能跑到 step=3978 仍不退出。
    """

    def __init__(self, anchor, expect, new, why="", window=0):
        self.anchor = anchor
        self.expect = expect
        self.new = new
        self.why = why
        self.window = window

    def __repr__(self):
        return "Edit(anchor=%r)" % (self.anchor,)


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
    # mklink 是 cmd 内建, 成功/失败信息是**本地化文本**(本机 CP936/GBK)。
    # text=True 若用 utf-8 严格解码, GBK 字节会在 subprocess 的 _readerthread
    # 里抛 UnicodeDecodeError —— 那条 traceback 落在本探针的 stderr 上,
    # 被 classify_probe_stderr 归成 UNCLASSIFIED -> PROBE-BROKEN -> ENGINE-ERROR,
    # 于是一条**签名完全有效**的变异被误判为「引擎坏了」, 整轮不可归档
    # (2026-10-04 实测 S-commit-leak-guard-off, EXITCODE=5)。
    # 这是**测量工具**的 bug, 不是被测程序/变异体的错: 对照(未变异)探针也会触发。
    # 显式 errors="replace" 让本地化字节降级为 U+FFFD, 不再炸读线程。
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
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
    # 用**仓内** .. (sub/../a.txt), 不用 ../evil.txt:
    # 后者会被 _abs 的第 3 道守卫(resolve()+relative_to(root))冗余兜底,
    # 于是变异体与对照签名都 raised=1 —— 探针无法隔离被变异的那道
    # ('..' in Path(rel).parts), 会把一次真落地记成假 PROBE-NOOP。
    # 仓内 .. 只有第 2 道守卫会拦; 关掉它才观测得到 raised 1->0。
    c._abs("sub/../a.txt")
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
    "conf_merge_add_scope",
    "conflict=1|committed=1|leaked=0",
    r'''
import conflicts
d, g = mkrepo()
# 造一个**真冲突**。conflict 进签名当非空守卫: 造不出冲突时下面两条
# 观测全是空真, 必须让它自己暴露。
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
p = g("merge", "--no-edit", "side")
sig("conflict", 1 if p.returncode != 0 else 0)
# ⚠ 联接必须在仓**里**。挂仓外的话 escaping_links() 根本看不见,
#   这条探针会因「没有泄漏」而空跑(与 test_f06_guard.py 第一版同一个坑)。
outside = d.parent / (d.name + "_out5")
outside.mkdir()
(outside / "leak.txt").write_text("SECRET\n", encoding="utf-8", newline="\n")
mkjunction(d / "escape", outside)
# 拆掉**第四道**(escaping_links), 让**第五道**(作用域受限 add)单独暴露 ——
# 这正是 test_f06_guard.py T3 的场景: 第四道在位时它会先拒绝, 观测不到第五道。
prev = conflicts.install_escaping_guard(lambda root: [])
try:
    c = conflicts.ConflictResolver(d)
    c.resolve("c.txt", "resolved\n")
    try:
        c.commit_merge("m")
        sig("committed", 1)
    except Exception:
        sig("committed", 0)
    sig("leaked", 1 if "escape/leak.txt" in g("ls-files").stdout else 0)
finally:
    conflicts.install_escaping_guard(prev)
rmt(d); rmt(outside)
''',
)

probe(
    "conf_state_keys",
    "conflict=1|has_currentBranch=1|has_origHead=1|has_mergeMsgPath=1",
    r'''
import conflicts
d, g = mkrepo()
# 造一个**真冲突**(单文件同行冲突, 与 test_conflicts.py 的 identify 用例同形态)。
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
p = g("merge", "--no-edit", "side")
# 非空守卫: 没造出冲突的话下面三条全是空真, 必须让它自己暴露。
sig("conflict", 1 if p.returncode != 0 else 0)
st = conflicts.ConflictResolver(d).merge_state()
for k in ("currentBranch", "origHead", "mergeMsgPath"):
    sig("has_" + k, 1 if k in st else 0)
rmt(d)
''',
)

probe(
    "ab_trackingref",
    "has_trackingRefExists=1|behind_none=1",
    r'''
import gitops
d, g = mkrepo()
# mkrepo **没有配 remote** => ahead_behind 走「没有远程跟踪 ref」那条分支,
# 也就是 trackingRefExists=False / behind=None 的那条。
go = gitops.GitOps(d, gitops.NetGate(gitops.OFF))
ab = go.ahead_behind()
sig("has_trackingRefExists", 1 if "trackingRefExists" in ab else 0)
# 非空守卫: behind 必须**真的是 None**(不是键缺失) —— 否则上面那条也可能是空真。
sig("behind_none", 1 if ("behind" in ab and ab["behind"] is None) else 0)
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
    p = subprocess.run([node, f], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
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
    p = subprocess.run([node, f], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for l in p.stdout.splitlines():
        if l.strip():
            print("SIG " + l.strip())
    if p.returncode != 0 or not p.stdout.strip():
        sig("node_err", (p.stderr.strip().splitlines() or ["empty"])[-1][:60])
    shutil.rmtree(t, ignore_errors=True)
''',
)

# ---- 过期工作区守卫(stale-worktree guards) + 联网闸 ----
# 造冲突的前置在两条探针里**各写一遍**(不用拼接): 拼接会让 raw/非 raw 的
# 边界出错 —— 实测踩过一次: r'''''' 是空串, 后面那段变成**非** raw,
# 于是 "tampered\n" 的 \n 成了真换行, 探针直接 SyntaxError。
# conflict 进签名当**非空守卫** —— 造不出冲突时下面几条观测全是空真。

probe(
    "conf_del_stale",
    "conflict=1|raised=1",
    r'''
import hashlib, conflicts
d, g = mkrepo()
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
p = g("merge", "--no-edit", "side")
sig("conflict", 1 if p.returncode != 0 else 0)
c = conflicts.ConflictResolver(d)
# 先记下当前内容的 sha256, 再把工作区改掉 —— 模拟「客户端读到的状态已过期」。
old = hashlib.sha256((d / "c.txt").read_bytes()).hexdigest()
(d / "c.txt").write_text("tampered\n", encoding="utf-8", newline="\n")
raised = 0
try:
    c.delete_file("c.txt", old)
except conflicts.ConflictError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

# ---- gen 的场景基准点守卫 ----
probe(
    "gen_no_main",
    "raised=1",
    r'''
import gen
d, g = mkrepo()
# mkrepo 建的是 -b main, 这里把 main 改名, 让「场景基准点」消失。
g("branch", "-m", "main", "other")
sg = gen.ScenarioGen(d)
raised = 0
try:
    sg.generate("conflict", 1, seed=1)
except gen.GenError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

# ---- gen 的脏工作区 / 分支冲突守卫 + 仓外联接防线不可用 ----
probe(
    "gen_dirty",
    "raised=1",
    r'''
import gen
d, g = mkrepo()
# mkrepo 之后是干净的。加一个**未跟踪**文件 -> porcelain 有输出 -> 脏。
(d / "dirty.txt").write_text("x\n", encoding="utf-8", newline="\n")
sg = gen.ScenarioGen(d)
raised = 0
try:
    sg.generate("conflict", 1, seed=1)
except gen.GenError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "gen_branch_clash",
    "clean=1|clash_refused=1",
    r'''
import gen
d, g = mkrepo()
sg = gen.ScenarioGen(d)
sg.generate("conflict", 1, seed=7)
# generate 会留下未提交的产物 -> 先提交, 让 _check_clean 不再抢先拦。
g("add", "-A"); g("commit", "-qm", "after gen")
dirty = g("status", "--porcelain").stdout.strip()
# 非空守卫: 仓不干净的话, 下面拦下来的会是 _check_clean 而不是 clash 闸。
sig("clean", 1 if not dirty else 0)
msg = ""
try:
    sg.generate("conflict", 1, seed=7)   # 同 seed -> 同名分支 -> clash
except gen.GenError as e:
    msg = str(e)
# 判据是「**哪条**闸拒的」: 关掉 clash 闸后 git 自己也会报错, 只 sig
# 「有没有抛」分不出来, 必须 sig 消息内容。
sig("clash_refused", 1 if "以下分支已存在" in msg else 0)
rmt(d)
''',
)

probe(
    "conf_guard_unavailable",
    "conflict=1|import_error=0",
    r'''
import sys, conflicts
# sys.modules 里放 None 会让 `import server` 抛 ImportError
# —— 正是「仓外联接防线不可用」那条分支要处理的情况。
saved = sys.modules.get("server")
sys.modules["server"] = None
conflict = 0
importerr = 0
try:
    conflicts._default_escaping_guard(".")
except conflicts.ConflictError:
    conflict = 1
except ImportError:
    importerr = 1
finally:
    if saved is None:
        sys.modules.pop("server", None)
    else:
        sys.modules["server"] = saved
# 判据的形状是「**归类**」: 防不住时必须报成 ConflictError, 不能裸奔成 ImportError。
sig("conflict", conflict)
sig("import_error", importerr)
''',
)

# ---- 路径越界 / 暂存区核对 / 未解决合并 ----
probe(
    "conf_merge_unresolved",
    "conflict=1|unresolved_refused=1",
    r'''
import conflicts
d, g = mkrepo()
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
p = g("merge", "--no-edit", "side")
sig("conflict", 1 if p.returncode != 0 else 0)
c = conflicts.ConflictResolver(d)
msg = ""
try:
    c.commit_merge()
except conflicts.ConflictError as e:
    msg = str(e)
# 判据是「**哪条**闸拒的」: 关掉这条后 git 自己也会拒(索引里还有 U 条目),
# 只 sig「有没有抛」分不出来。
sig("unresolved_refused", 1 if "没解决" in msg else 0)
rmt(d)
''',
)

# ---- plan 预演的响应形状 ----
probe(
    "amend_published",
    "has_published=1|inner_is_bool=1",
    r'''
import history_ops
d, g = mkrepo()
h = history_ops.HistoryOps(d)
res = h.plan_amend()
# plan_amend 把 published_info() 的整个 dict 嵌进 published:
#   {"published": {"published": bool, "remotes": [...], "note": "..."}}
pub = res.get("published")
sig("has_published", 1 if isinstance(pub, dict) and "published" in pub else 0)
sig("inner_is_bool", 1 if isinstance(pub, dict)
    and isinstance(pub.get("published"), bool) else 0)
''',
)

probe(
    "fetch_aheadBehind",
    "has_aheadBehind=1",
    r'''
import gitops
d, g = mkrepo()
o = gitops.GitOps(d)
res = o.plan_fetch("origin")
sig("has_aheadBehind", 1 if "aheadBehind" in res else 0)
''',
)

probe(
    "push_revRange",
    "has_revRange=1",
    r'''
import gitops
d, g = mkrepo()
o = gitops.GitOps(d)
res = o.plan_push("origin")
sig("has_revRange", 1 if "revRange" in res else 0)
''',
)

probe(
    "conf_unresolve_notresolved",
    "conflict=1|raised=1",
    r'''
import conflicts
d, g = mkrepo()
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
p = g("merge", "--no-edit", "side")
sig("conflict", 1 if p.returncode != 0 else 0)
c = conflicts.ConflictResolver(d)
# c.txt 还在冲突态(没解决)。这时调 unresolve 应当被拒 —— 它只对已解决路径有意义。
raised = 0
try:
    c.unresolve("c.txt")
except conflicts.ConflictError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

# ---------- 快路径/回退边界(2026-10-22) ----------
probe(
    "index_fast_extgate",
    "fast_none=1",
    r'''
import server
d, g = mkrepo()
(d / "sub").mkdir(exist_ok=True)
(d / "sub" / "deep.txt").write_text("d\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "sub")
# 用 sparse-index 做探针: 它的 sdir 扩展会把目录条目写进索引(名字带 '/'),
# 关掉扩展闸之后读到的是目录名而不是文件集合 —— 签名必须变。
# 刻意**不用** split-index: 它同时被「空路径」那道守卫兜住, 是等价变异(实测
# 关掉扩展闸签名照样不变, 引擎会把那条记成 PROBE-NOOP)。
g("sparse-checkout", "init", "--cone", "--no-sparse-index")
g("sparse-checkout", "set", "--sparse-index", "sub")
v = server.Repo(d)._index_tracked_fast()
sig("fast_none", 1 if v is None else 0)
rmt(d)
''',
)

probe(
    "index_fast_count",
    "equal=1|n=4",
    r'''
import server
d, g = mkrepo()
for i in range(3):
    (d / ("f%d.txt" % i)).write_text("v%d\n" % i, encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "c2")
fast = server.Repo(d)._index_tracked_fast()
truth = {x for x in g("ls-files", "-z").stdout.split("\0") if x}
sig("equal", 1 if fast == truth else 0)
sig("n", len(truth))
rmt(d)
''',
)

probe(
    "branch_ref_loose",
    "loose=1",
    r'''
import server
d, g = mkrepo()
g("branch", "feature")
sig("loose", 1 if server.Repo(d)._local_branch_exists("feature") else 0)
rmt(d)
''',
)

# ---------- 引用/reflog/索引直读(2026-10-27) ----------
probe(
    "refs_packed_names",
    "n=4",
    r'''
import gitrefs
d, g = mkrepo()
for i in range(3):
    g("branch", "p%d" % i)
g("pack-refs", "--all")
names = gitrefs.head_names(d)
sig("n", -1 if names is None else len(names))
rmt(d)
''',
)

probe(
    "refs_head_mark",
    "cur=1",
    r'''
import server
d, g = mkrepo()
g("branch", "feature")
rows = server.Repo(d).branches()
sig("cur", sum(1 for r in rows if r["current"]))
rmt(d)
''',
)

probe(
    "stash_order",
    "newest_first=1",
    r'''
import history_ops
d, g = mkrepo()
(d / "a.txt").write_text("d1\n", encoding="utf-8", newline="\n")
g("stash", "push", "-q", "-m", "OLD")
(d / "a.txt").write_text("d2\n", encoding="utf-8", newline="\n")
g("stash", "push", "-q", "-m", "NEW")
rows = history_ops.HistoryOps(d).list_stashes()
sig("newest_first", 1 if rows and "NEW" in rows[0]["raw"] else 0)
rmt(d)
''',
)

probe(
    "unmerged_list",
    "n=1",
    r'''
import gitrefs
d, g = mkrepo()
(d / "c.txt").write_text("base\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "base")
g("checkout", "-q", "-b", "side")
(d / "c.txt").write_text("side\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "side")
g("checkout", "-q", "main")
(d / "c.txt").write_text("main\n", encoding="utf-8", newline="\n")
g("commit", "-qam", "main")
g("merge", "--no-edit", "side")
u = gitrefs.unmerged_paths(d)
sig("n", -1 if u is None else len(u))
rmt(d)
''',
)

# ---------- 写端点输入边界(2026-10-27) ----------
probe(
    "path_nul_guard",
    "raised=1",
    r'''
import server
d, g = mkrepo()
raised = 0
try:
    server.Repo(d).resolve_in_root("a.txt\0.png")
except server.GitError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "commit_nul_msg",
    "raised=1|staged=0",
    r'''
import server
d, g = mkrepo()
(d / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
raised = 0
try:
    server.Repo(d).commit("a\0b")
except server.GitError:
    raised = 1
except Exception:
    # 守卫被拿掉时 NUL 会漏到 subprocess, 抛的是 Python 原生 ValueError ——
    # 单独标成 2, 否则探针自己崩掉, 变异会被误记成 ENGINE-ERROR。
    raised = 2
staged = g("diff", "--cached", "--name-only").stdout.strip()
sig("raised", raised)
sig("staged", 0 if not staged else 1)
rmt(d)
''',
)

probe(
    "msg_type_guard",
    "raised=1",
    r'''
import history_ops
d, g = mkrepo()
(d / "a.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
raised = 0
try:
    history_ops.HistoryOps(d).do_stash(message=["x"])
except history_ops.HistoryError:
    raised = 1
sig("raised", raised)
rmt(d)
''',
)

probe(
    "reflog_index_type",
    "clean=1",
    r'''
import history_ops
d, g = mkrepo()
clean = 0
try:
    history_ops.HistoryOps(d).do_restore_from_reflog(index=None, confirmed=True)
except history_ops.HistoryError:
    clean = 1
except Exception:
    clean = 0
sig("clean", clean)
rmt(d)
''',
)

probe(
    "catfile_type",
    "type=2",
    r'''
import gitcat
d, g = mkrepo()
(d / "sub").mkdir()
(d / "sub" / "in.txt").write_text("i\n", encoding="utf-8", newline="\n")
g("add", "-A"); g("commit", "-qm", "sub")
got = gitcat.read(d, "HEAD:sub")
sig("type", 0 if got is None else (1 if got[1] == "blob" else 2))
rmt(d)
''',
)

# ---------- tags/reflog 快路径(2026-10-27) ----------
probe(
    "tag_subject_rule",
    "subject=M1 M2",
    r'''
import history_ops
d, g = mkrepo()
g("tag", "-a", "m", "-m", "M1\nM2\n\nrest")
rows = history_ops.HistoryOps(d).list_tags(10)
sig("subject", rows[0]["subject"] if rows else "EMPTY")
rmt(d)
''',
)

probe(
    "reflog_order",
    "newest_is_head=1",
    r'''
import history_ops
d, g = mkrepo()
for i in range(2):
    g("commit", "--allow-empty", "-qm", "c%d" % i)
head = g("rev-parse", "HEAD").stdout.strip()
rows = history_ops.HistoryOps(d).list_reflog(10)
sig("newest_is_head", 1 if rows and rows[0]["sha"] == head else 0)
rmt(d)
''',
)

probe(
    "abbrev_minlen",
    "len=7",
    r'''
import history_ops
d, g = mkrepo()
g("commit", "--allow-empty", "-qm", "c2")
rows = history_ops.HistoryOps(d).list_reflog(5)
sig("len", len(rows[0]["short"]) if rows else -1)
rmt(d)
''',
)

probe(
    "tags_sort",
    "first=new",
    r'''
import os, subprocess
import history_ops
d, g = mkrepo()
def tag(name, date):
    env = dict(os.environ, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", str(d), "tag", "-a", name, "-m", name],
                   env=env, capture_output=True)
tag("old", "2020-01-02 03:04:05 +0800")
tag("new", "2021-01-02 03:04:05 +0800")
rows = history_ops.HistoryOps(d).list_tags(10)
sig("first", rows[0]["name"] if rows else "EMPTY")
rmt(d)
''',
)

probe(
    "semantic_autocrlf",
    "crlf=1|unstaged=0",
    r'''
import os, subprocess
import server
d, g = mkrepo()
cfg = d.parent / "gc_autocrlf"
cfg.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8", newline="\n")
os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
(d / "a.txt").unlink()
subprocess.run(["git", "-C", str(d), "checkout", "-f", "--", "a.txt"],
               env=dict(os.environ), capture_output=True)
crlf = 1 if b"\r\n" in (d / "a.txt").read_bytes() else 0
st = server.Repo(d).status()
sig("crlf", crlf)
sig("unstaged", len(st["unstaged"]))
rmt(d)
''',
)

# ---------- 契约收口(2026-11-02) ----------
probe(
    "log_limit_clamp",
    "n0=1|nneg=1",
    r'''
import server
d, g = mkrepo()
g("commit", "--allow-empty", "-qm", "c2")
r = server.Repo(d)
sig("n0", len(r.log(0)))
sig("nneg", len(r.log(-1)))
rmt(d)
''',
)

probe(
    "diff_path_known",
    "nope=0|real=1",
    r'''
import server
d, g = mkrepo()
r = server.Repo(d)
sig("nope", 1 if r.path_known("never_existed.txt") else 0)
sig("real", 1 if r.path_known("a.txt") else 0)
rmt(d)
''',
)

probe(
    "tags_shape",
    "shape=1",    r'''
import json, os, subprocess, sys, time, urllib.request
d, g = mkrepo()
g("tag", "-a", "v1", "-m", "rel")
srv_dir = os.environ.get("PYTHONPATH") or os.getcwd()
port = 8900 + (os.getpid() % 400)
p = subprocess.Popen([sys.executable, "-X", "utf8",
                      os.path.join(srv_dir, "server.py"),
                      "--root", str(d), "--port", str(port)],
                     env=dict(os.environ),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(80):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1).read()
            break
        except Exception:
            if p.poll() is not None:
                break
            time.sleep(0.25)
    body = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/api/history/tags" % port, timeout=30).read().decode("utf-8"))
    ok = isinstance(body, dict) and isinstance(body.get("tags"), list)
    sig("shape", 1 if ok else 0)
finally:
    p.terminate()
    try:
        p.wait(10)
    except Exception:
        p.kill()
rmt(d)
''',
)

probe(
    "commit_rc_guard",
    "raised=1|head_same=1",
    r'''
import server
d, g = mkrepo()
head0 = g("rev-parse", "HEAD").stdout.strip()
raised = 0
try:
    server.Repo(d).commit("nothing to commit here")
except server.GitError:
    raised = 1
head1 = g("rev-parse", "HEAD").stdout.strip()
sig("raised", raised)
sig("head_same", 1 if head0 == head1 else 0)
rmt(d)
''',
)

probe(
    "status_optout",
    "omitted=1|has_staged=0",
    r'''
import json, os, subprocess, sys, time, urllib.request
d, g = mkrepo()
(d / "a.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
srv_dir = os.environ.get("PYTHONPATH") or os.getcwd()
port = 8900 + (os.getpid() % 400)
p = subprocess.Popen([sys.executable, "-X", "utf8",
                      os.path.join(srv_dir, "server.py"),
                      "--root", str(d), "--port", str(port)],
                     env=dict(os.environ),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(80):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1).read()
            break
        except Exception:
            if p.poll() is not None:
                break
            time.sleep(0.25)
    data = json.dumps({"files": ["a.txt"], "status": False}).encode()
    rq = urllib.request.Request("http://127.0.0.1:%d/api/stage" % port, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    body = json.loads(urllib.request.urlopen(rq, timeout=30).read().decode("utf-8"))
    sig("omitted", 1 if body.get("statusOmitted") is True else 0)
    sig("has_staged", 1 if "staged" in body else 0)
finally:
    p.terminate()
    try:
        p.wait(10)
    except Exception:
        p.kill()
rmt(d)
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
#   smoke.py            : "[PASS] NN  name -> detail" / "[FAIL] NN  name"(失败行**没有** ->)
#
# ── 2026-10-03:7 个解析器里 4 个结构上不可能记录到失败,已全部重写 ──────────
#
# 旧写法把「状态词」和「用例名」都塞进捕获组,然后靠 group(1) 去查状态表:
#     (re.compile(r"^not ok \d+ - (.+)$"), {"not ok": "fail"})
#                                            ^^^^^^^^^^^^ 只有 1 组, 是**名字**
#     st = table.get(m.group(1))   ->  table.get("guard-path: ...")  ->  None
# 于是「匹配成功」被当成「不是用例」丢掉。实测(MUT_RUN 36 份真实日志):
#   test_conflicts.py   regexHit=14  keyHit=0    <- 查表键取错, 一个用例都记不到
#   selftest.py         regexHit=1   keyHit=0    <- 同上
#   test_history_ops.py regexHit=0   keyHit=0    <- 正则 (\\S+) 吃不下含空格的名字
#   smoke.py            失败行没有 '-> ',而正则强制要求它 -> 失败行永不匹配
# 4 个套件对 verdict 零贡献。**方向全部朝「看不见失败」倒。**
#
# 新写法两条铁律:
#   1. 状态词用**具名组** (?P<st>...),名字用 (?P<nm>...),不许靠数括号。
#   2. 每个解析器必须有 fixture(见 PARSER_FIXTURES + --selftest-parsers),
#      拿一份**已知计数**的日志喂它,数错了就红。没有 fixture 不算修好。

# 实测(MUT_RUN 36 份真实日志)真实行是**右对齐到固定宽度**的标记 + 缩进 2~4 空格:
#     '    ok   L-hardlink-outside-content hardlink'
#     '   skip  L-symlink-file-not-buildable symlink'
#     '   FAIL  F-dir-as-file-arg dir-as-file-arg'
# 所以缩进用 \s* (至少 2), 标记本身固定 6 宽, 之后恰好一个空格再接用例名。
MARK_RE = re.compile(
    r"^\s{2,}(?P<st>  ok  | FAIL | skip |ERROR )\s(?P<nm>\S+)\s+(?P<rest>.*?)\s*$")


def _case(rx, table, name_group="nm"):
    """一条用例规则。状态一律取具名组 ``st``,不许用位置组。"""
    return {"rx": rx, "table": table, "name_group": name_group}


def _tap_parser():
    """TAP 风格的套件(test_conflicts.py / test_aigit_invariants.py 都用它)。

    抽出来是为了两处共用同一份正则, 而不是抄两遍 —— 抄两遍的话,
    改一处忘了另一处, 就会出现「两个格式一样的套件只有一个能解析」。
    """
    return {
        "cases": [_case(re.compile(r"^(?P<st>not ok|ok) \d+ - (?P<nm>.+)$"),
                        {"ok": "pass", "not ok": "fail"})],
        # TAP 有**两种** summary 行(两种都真实出现过):
        #   全绿:  "# passed 14 of 14 test(s), skipped 0"
        #   有红:  "# failed 1 among 14 test(s) (skipped 0)"
        # 只认前一种的话, **每一次有失败的 run 都会变成 NO-SUMMARY**,
        # 交叉校验被跳过 —— 又是一个「只在失败时才瞎」的洞。
        "summary": re.compile(
            r"# (?:passed (?P<pass>\d+) of (?P<total>\d+)"
            r"|failed (?P<fail2>\d+) among (?P<total2>\d+))"
            r" test\(s\)[, ]*(?:\(skipped (?P<skip>\d+)\)|skipped (?P<skip2>\d+))"),
        "fail_from": ("__t_p__", "__f2__"),
    }


SUITE_PARSERS = {
    "test_aigit.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        # 具名组:fail/error 分开,交叉校验时两个都要算进「红」
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    "test_aigit_extra.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-27 新增: 引用 / reflog / 索引直读的「快路径 ∪ 回退」判据。
    "test_refstore.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-27 新增: 写端点输入边界(NUL 路径 / 消息类型 / 索引类型)。
    "test_write_inputs.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-27 新增: 常驻 cat-file 的等价性与回退。
    "test_object_read.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-27 新增: tags/reflog 快路径(格式化语义与 git 逐字段对齐)。
    "test_history_fast.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-28 新增: aigit vs 真 git 的差分（含 autocrlf 透传与阴性对照）。
    "test_vs_git.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-30 新增: 写操作 vs 真 git（同一次提交的 sha 必须逐字相同）。
    "test_write_vs_git.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-11-02 新增: 四条对外契约收口(log 夹取 / diff 不存在 / 形状 / 405)。
    "test_api_contract.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    "test_conflicts.py": _tap_parser(),
    # 2026-10-22 新增: 索引直读 / ref 直读的「快路径 ∪ 回退」边界套件。
    # 它自跑式输出与 test_aigit.py 同形(2 空格 + mark + 空格 + 名字 / 汇总行),
    # 所以共用同一份 MARK_RE 与 summary 正则。
    "test_index_fastpath.py": {
        "cases": [_case(MARK_RE, {"  ok  ": "pass", " FAIL ": "fail",
                                  " skip ": "skip", "ERROR ": "error"})],
        "summary": re.compile(r"汇总: 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)\s+"
                              r"ERROR (?P<error>\d+)"),
    },
    # 2026-10-03 新增: 专测「守卫被关掉时必须有人喊」的护栏不变量。
    # 格式与 test_conflicts.py 同为 TAP, 共用同一份正则(见 _tap_parser)。
    "test_aigit_invariants.py": _tap_parser(),
    "test_history_ops.py": {
        # 实测真实行(36 份日志):
        #   'not ok A-every destructive do_* refuses without confirmed=True - every ...'
        # 没有独立的 sid 字段 —— 整串就是 'not ok ' + <整条描述>, 中间的 ' - ' 只是
        # 描述里恰好有分隔符。旧正则 (?P<sid>\S+) - 要求 sid 后面紧跟 ' - ',
        # 而真实 sid 'A-every' 后面跟的是描述词, 于是永不匹配。
        #
        # 关键: 这个套件**只给失败用例打印 'not ok' 行**, 通过用例一个字符都不打,
        # 只在进度里点 '.' (实测 '...' / '....' 每行 N 个点)。
        # 所以 pass 数必须由**点数**算, 否则 parsed_total 永远是失败数,
        # 与 summary 的 total(37) 对不上 -> 被误判成 VOID/UNPARSEABLE。
        # 那道闸本来是好的, 是它**抓到了我这个解析器的缺陷** —— 这正是它该做的。
        "cases": [_case(re.compile(r"^\s*(?P<st>not ok|ok)\s+(?P<nm>.+?)"
                                   r"(?P<dots>\.*)"
                                   r"(?:\s+\(异常\))?\s*$"),
                        {"ok": "pass", "not ok": "fail"})],
        "dots_are_pass": True,
        "summary": re.compile(r"共 (?P<total>\d+) 条,过 (?P<pass>\d+),"
                              r"失败 (?P<fail>\d+),异常 (?P<error>\d+)"),
    },
    # 2026-10-06 新增: S-ab-trackingref-off 第一次跑就 KeyError 崩在这条缺失的
    # 解析器上(ENGINE-ERROR, 与 test_f06_guard.py 那次同一类)。
    "test_aigit_net.py": {
        # summary 与 test_history_ops.py **同形**(共 N 条,过 P,失败 F,异常 E),
        # 但点**不是独占一行** —— 实测 56 个点散在 12 行, 每行后面都跟着
        # 该用例打的说明文字。所以用 dots_prefix_are_pass(数行首连续的点)。
        # 失败行: '\nnot ok <id> - <name>', id 里含 '-'、名字含中文/空格。
        # 引擎跑套件**不带 -v**(见 run_suite), 所以通过用例只有点、没有 ok 行。
        "cases": [_case(re.compile(r"^\s*(?P<st>not ok)\s+(?P<nm>.+?)\s*$"),
                        {"not ok": "fail"})],
        "dots_prefix_are_pass": True,
        "summary": re.compile(r"共 (?P<total>\d+) 条,过 (?P<pass>\d+),"
                              r"失败 (?P<fail>\d+),异常 (?P<error>\d+)"),
    },
    "selftest.py": {
        # 名字含空格且后面还跟中文说明,所以名字不能是 (\S+)\s*$。
        # 这里取第一个空格后的全部当名字(名字本身在本套件里唯一,够用)。
        "cases": [_case(re.compile(r"^\s*\[(?P<st>通过|失败)\]\s+(?P<nm>.+?)\s*$"),
                        {"通过": "pass", "失败": "fail"})],
        # 注意: 这个 summary **没有失败数字段**,只有 "N / M 组通过"。
        # 所以 selftest.py 无法参与 summary<->cases 交叉校验(见 cross_check),
        # 它的 parse_ok 只按 cases 自洽性判。这一条限制必须写在报告里。
        "summary": re.compile(r"(?P<pass>\d+) / (?P<total>\d+) 组通过"),
        "no_fail_field": True,
    },
    "test_aigit_web.py": {
        # 2026-10-04 修: 旧 nm 取 \S+ 只拿到层号(L1..L4), 同一层里多个用例
        # (如 L3 的 F-04 有两个失败子断言)会被 dedup 合并成一个 -> cases 的
        # 失败数(1)对不上 summary 声称的(2) -> 假 VOID/SUMMARY-CASE-MISMATCH
        # (整轮 EXITCODE=3)。实测同一份日志旧口径 PASS 41->4, FAIL 2->1;
        # 该套件的用例身份 = 状态后**整段描述**, 故 nm 取到行尾。
        "cases": [_case(re.compile(r"^\[(?P<st>PASS|FAIL|WARN|SKIP)\s*\]\s+"
                                   r"(?P<nm>.+?)\s*$"),
                        {"PASS": "pass", "FAIL": "fail", "WARN": "warn",
                         "SKIP": "skip"})],
        "summary": re.compile(r"汇总:(?P<pass>\d+) PASS / (?P<fail>\d+) FAIL / "
                              r"(?P<warn>\d+) WARN / (?P<skip>\d+) SKIP"),
    },
    "smoke.py": {
        # '-> detail' 是**可选**的: 实测 PASS 行有 "-> detail", FAIL 行没有。
        # 旧正则把 \s*->\s* 写成必需, 于是**每一条失败行都被排除**。
        "cases": [_case(re.compile(r"^\[(?P<st>PASS|FAIL|INFO)\]\s*\d+\s+"
                                   r"(?P<nm>.+?)(?:\s*->.*)?$"),
                        {"PASS": "pass", "FAIL": "fail"})],
        "summary": re.compile(r"通过\s+(?P<pass>\d+)\s+失败\s+(?P<fail>\d+)\s+"
                              r"跳过\s+(?P<skip>\d+)"),
    },
    # 2026-10-06 新增: 这条防线(commit_merge 第五道)由 T3 在测, 而
    # SUITES_BY_TARGET["conflicts.py"] 原来只有 test_conflicts.py ——
    # 缺这条解析器时引擎直接 KeyError 崩成 ENGINE-ERROR(实测
    # C-merge-add-scope-off 第一次跑就是这个)。
    # ⚠ 本套件行首**没有**缩进(与 MARK_RE 不同): 'ok   T1   (1.6s)'。
    # ⚠ 'base' 行是**基线测量**, 不进 PASS/FAIL 汇总 —— 必须排除,
    #   否则 parsed_total 会比 summary 多, 判成假 MISMATCH。
    "test_f06_guard.py": {
        "cases": [_case(re.compile(r"^(?P<st>ok|FAIL|skip)\s+(?P<nm>\S+)"),
                        {"ok": "pass", "FAIL": "fail", "skip": "skip"})],
        "summary": re.compile(r"# 共 (?P<total>\d+) 条\s+PASS (?P<pass>\d+)\s+"
                              r"FAIL (?P<fail>\d+)\s+SKIP (?P<skip>\d+)"),
    },
}


def summary_failcount(spec, m):
    """从 summary 匹配里取「声称的失败数」。取不到返回 None。

    * 直接有 fail 字段            -> fail + error(与 cases 的 red 同一定义)
    * TAP 风格(passed/total)      -> total - pass
    * 声明了 no_fail_field(selftest) -> None,并由调用方记成「无法对账」

    **error 必须一起算**: 引擎判红用的是 red = fail + error, 而 test_aigit.py
    与 test_history_ops.py 的 summary 同时有 FAIL 和 ERROR 两列。只拿 fail 去对账,
    一个「1 个 FAIL + 1 个 ERROR」的套件会算出 claimed=1 / parsed=2 -> 假的 MISMATCH。
    那是**朝安全方向误报**, 会把真结论判成作废, 同样不可接受 —— 两个方向都得防。
    """
    if spec.get("no_fail_field"):
        return None
    # test_conflicts.py: 两种 summary 形态各给一组, 哪组有值用哪组。
    #   "# passed P of T"  -> 失败 = T - P
    #   "# failed F among T" -> 失败 = F
    if "__f2__" in (spec.get("fail_from") or ()):
        gd = m.groupdict()
        try:
            if gd.get("fail2") is not None:
                return int(gd["fail2"])
            if gd.get("total") is not None and gd.get("pass") is not None:
                return int(gd["total"]) - int(gd["pass"])
        except (TypeError, ValueError):
            return None
        return None
    ff = spec.get("fail_from")
    if ff:
        hi, lo = ff
        try:
            return int(m.group(hi)) - int(m.group(lo))
        except (IndexError, ValueError):
            return None
    gd = m.groupdict()
    try:
        n = int(gd.get("fail", 0))
        if "error" in gd:
            n += int(gd["error"])
        return n
    except (TypeError, ValueError):
        return None


def parse_suite(suite, out):
    """把一个套件的 stdout 解析成 {状态: [用例名]} + 汇总 + **自洽性判定**。

    返回 (cases, info)。info 里带:
      summary        : summary 正则解出的数字组(没有就是 None)
      claimed_fail   : summary 声称的失败数(None = 无法对账)
      parsed_fail    : cases 里实际数出来的 fail+error
      claimed_total  : summary 声称的用例总数(有 total 字段才有)
      parsed_total   : cases 里数出来的用例总数
      summary_match  : summary 正则有没有匹配上
      crosscheck     : "OK" / "MISMATCH" / "NO-SUMMARY" / "NO-FAIL-FIELD"
      parse_ok       : **完整性**判定,不是「抓到过就算」

    parse_ok 的新定义(C): 用 summary 的总数当分母,cases 解析数不足就是 False。
    旧定义只要抓到过一条就 True —— 44 个用例只抓到 2 条也算「完整」,
    于是「我抓到了 2 条」被当成「我全抓到了」。
    """
    spec = SUITE_PARSERS[suite]
    cases = {}
    dot_n = 0
    for line in out.splitlines():
        done = False
        for rule in spec["cases"]:
            m = rule["rx"].match(line)
            if not m:
                continue
            # 状态一律取具名组 st。取不到就是规则写错了,不是「这条不是用例」。
            gd = m.groupdict()
            if "st" not in gd:
                raise RuntimeError("解析器 %s 的规则缺少具名组 (?P<st>...): %r"
                                   % (suite, rule["rx"].pattern))
            st = rule["table"].get(gd["st"])
            if st is None:
                # 状态词不认识 -> 真的不是用例行,跳过(INFO 之类)
                continue
            ng = rule["name_group"]
            name = gd.get(ng) if ng in gd else m.group(len(m.groups()))
            if name is None:
                raise RuntimeError("解析器 %s 缺具名组 (?P<%s>...): %r"
                                   % (suite, ng, rule["rx"].pattern))
            cases.setdefault(st, [])
            if name not in cases[st]:
                cases[st].append(name)
            done = True
            break
        if done:
            continue
        # test_history_ops.py / test_aigit_net.py: 通过用例只在进度里点 '.',
        # 不打用例行。点数 = 通过数。这是它「声称 N 条却只解析出几条」的正解。
        #
        # ⚠ 名字必须**跨行全局唯一**。旧写法用行内下标 `dot-pass-%d`: 两行各有
        #   5 个点时会生成两套 dot-pass-0..4, 去重后只剩 5 个 —— 实测
        #   test_aigit_net.py 的 56 个点散在 **12 行**上, 旧写法只会数出 13。
        #   (test_history_ops.py 恰好把 37 个点挤在**一行**, 所以一直没暴露。)
        ndots = 0
        if spec.get("dots_are_pass"):
            s = line.strip()
            if s and set(s) == {"."}:
                ndots = len(s)
        elif spec.get("dots_prefix_are_pass"):
            # test_aigit_net.py: 点后面**紧跟着**该用例自己打的说明文字
            # (如 '.        逐字段比对通过: ...'), 所以「整行只有点」永不成立,
            # 必须数**行首连续的点**。
            ndots = len(line) - len(line.lstrip("."))
        for _ in range(ndots):
            cases.setdefault("pass", []).append("dot-pass-%d" % dot_n)
            dot_n += 1

    for st in ("pass", "fail", "error", "skip", "warn"):
        cases.setdefault(st, [])

    m = spec["summary"].search(out)
    # 可选组没匹配上时 m.groups() 里会出现 None, 直接 .isdigit() 会炸 ——
    # 这条是被 fixture 逼出来的: 两种 summary 形态的组名不同, 未命中的那半是 None。
    summary = ([int(x) for x in m.groups() if x and x.isdigit()]
               if m else None)
    claimed_fail = summary_failcount(spec, m) if m else None
    parsed_fail = len(cases["fail"]) + len(cases["error"])
    parsed_total = sum(len(cases[k]) for k in
                       ("pass", "fail", "error", "skip", "warn"))
    claimed_total = None
    if m:
        gd = m.groupdict() or {}
        for key in ("total", "total2"):     # 两种 summary 形态的组名不同
            if gd.get(key) is not None:
                try:
                    claimed_total = int(gd[key])
                except ValueError:
                    claimed_total = None
                break

    if not m:
        crosscheck = "NO-SUMMARY"
    elif claimed_fail is None:
        crosscheck = "NO-FAIL-FIELD"
    elif claimed_fail != parsed_fail:
        crosscheck = "MISMATCH"
    else:
        crosscheck = "OK"

    # 完整性:总数对不上就是没抓全。claimed_total 缺失的套件(selftest /
    # test_aigit_web / smoke 没有 total 字段)退回「至少抓到一条」。
    if claimed_total is not None:
        parse_ok = (parsed_total == claimed_total)
    else:
        parse_ok = parsed_total > 0

    info = {"summary": summary, "claimed_fail": claimed_fail,
            "parsed_fail": parsed_fail, "claimed_total": claimed_total,
            "parsed_total": parsed_total, "summary_match": bool(m),
            "crosscheck": crosscheck, "parse_ok": parse_ok,
            "red_names": cases["fail"] + cases["error"]}
    return cases, info


def cross_check(suite, out, info):
    """summary 声称有失败、cases 却一个红的都没解析出来 => 硬错误。

    这是「朝安全方向静默失败」最典型的形态: 解析器坏了 100% 的格式,
    判据失效, 而失效被记成 VOID —— 语义中立, 观感安全, 最不容易被质疑。
    所以这里必须**响**: 返回一行人类可读的原因, 由调用方升级成
    VOID/SUMMARY-CASE-MISMATCH 并以非 0 退出码结束。
    """
    if info["crosscheck"] != "MISMATCH":
        return None
    lines = [l for l in out.splitlines() if l.strip()]
    summ = [l for l in lines
            if SUITE_PARSERS[suite]["summary"].search(l)]
    return ("summary 声称失败 %d 条, cases 解析出 %d 条 —— 解析器把失败丢了\n"
            "              summary 原文: %s\n"
            "              cases 里的红   : %r"
            % (info["claimed_fail"], info["parsed_fail"],
               (summ[0].strip()[:120] if summ else "<没找到 summary 行>"),
               (info.get("red_names") or [])[:8]))


# ════════════════════════════════════════════════════════ 快照 / 应用

# 基线默认 **HEAD**,不是某个写死的 sha。
#
# 为什么要改成 HEAD(而不是「保留 f99c10a 但可覆盖」):
#   写死基线和写死行号是同一个病 —— 把「当前是什么」编码成常量,于是代码一
#   提交,锚点就悄悄过期,而装置仍然跑得动、仍然报得出一个漂亮的百分比。
#   2026-10-02 那个 77.8% 就是这么来的:它测的是 HEAD~1,一个不含当天任何
#   工作的快照,而装置一声没响。默认值跟着 HEAD 走,过期就不再可能。
#
#   真正的可复现性靠 `--base-ref <sha>` 显式钉死 + 结果 JSON 里记下解析后的
#   完整 sha(见 resolve_base_ref / --snapshots-only 的输出),不靠默认值钉死。
DEFAULT_BASE_REF = "HEAD"


def resolve_base_ref(ref):
    """把 ref 解析成完整 40 位 sha。解析不出来直接抛,不许静默退回默认值。"""
    p = subprocess.run(["git", "-C", HERE, "rev-parse", "--verify", "%s^{commit}" % ref],
                       capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError("--base-ref %r 不是一个可解析的提交: %s"
                           % (ref, (p.stderr or "").strip()))
    return p.stdout.strip()


def build_snapshot(src=None, base_ref=None):
    """把**某个已提交基线**导出成一份只读基准。工作区永远不被改。

    base_ref 语义就是 `git archive <ref>`:可以是 HEAD / HEAD~1 / 任意 sha。
    默认 DEFAULT_BASE_REF="HEAD",即「测当前提交」。

    为什么用 git archive 而不是工作区目录:直接拷工作区测的是移动靶,
    任何结论不可复现。但注意 **比快照 == 比 commit blob,不等于 == 比活动工作区**
    —— 工作区随时可能有别的线在改,拿工作区当判据是时序运气。

    返回 (manifest, base_ref, base_sha)。"""
    base_ref = base_ref or DEFAULT_BASE_REF
    base_sha = resolve_base_ref(base_ref)
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
        # 走到这里说明 SNAP **已经被删掉了**却建不回来(通常是别的进程正占着它,
        # 比如另一次 build_snapshot 或一次套件运行)。所以这不是「本次没建好」,
        # 是「上一份好快照也已经没了」—— 两者的后果完全不同,必须说清楚。
        raise RuntimeError(
            "快照目录建不出来: %s\n"
            "  注意:SNAP 已经被删除,现在**没有**任何可用快照(不是「沿用上一份」)。\n"
            "  常见原因:另一个 build_snapshot / 套件运行正占着这个目录。\n"
            "  这类命令不要并发跑 —— 它们会互相删对方的快照。" % SNAP)
    import zipfile, subprocess as _sp
    archive = os.path.join(SB, "base.zip")
    # `-c core.autocrlf=false`:让快照**逐字节等于 commit blob**。
    #
    # 为什么必须显式压掉:本机 core.autocrlf=true,`git archive` 会照它把 LF 转成
    # CRLF 再导出(server.py 89916 -> 91642 字节,每个换行 +1 字节)。后果有两个,
    # 都实测过:
    #   1. 快照 sha256 永远 != `git cat-file blob` 的 sha256,于是
    #      「快照 == 某个 commit」这条判据在本机**永远不成立**,也没法用来校验
    #      archive/extract 有没有坏 —— 判据失效就等于没有判据。
    #   2. 快照的字节取决于**这台机器的 git 配置**,换台机器基线就变了。
    #      变异测试的基线必须是可复现的,不能读环境变量。
    # 附带的收益: 快照全是 LF,改单行时不会踩 CRLF/LF 混排。
    #
    # ── 2026-10-03: 旧快照那个 sha 的来源已端到端复现,不是未知数 ──────────
    # 旧代码(BASE_REF="f99c10a", 不带 -c 覆盖)建出的快照里 server.py 的
    # sha256 = 5fb8b6212329ca00…。它**不是**「来源不明的异常值」:
    #   实测 autocrlf_evidence.py 的 (A) 段 —— 不带 -c 真跑一次
    #       git archive f99c10a -> server.py 70073 B  sha 5fb8b6212329ca00
    #   实测 (B) 段 —— 枚举仓库全部 3 个 commit 的 raw/crlf 两种形态:
    #       1a1f1b1  raw=cc937213…  crlf=369d2d6b…
    #       f99c10a  raw=0e1a0740…  crlf=5fb8b621…   <== 命中
    #       98bd29e  raw=1baa1c70…  crlf=82892454…
    # 即 5fb8b621 == f99c10a 的 CRLF 形态。它就是上面这个 bug 的第一个物证。
    #
    # 顺带纠正一个无效比较: 「旧快照 5fb8b621 vs 工作区 CC937213」不可比
    # (前者 CRLF、后者 LF)。但**快照过期**这个结论仍然成立, 靠的是另外三条:
    # 旧快照缺 gitops.py、缺 test_aigit_gate.py、index.html 1482 vs 2185 行。
    with open(archive, "wb") as f:
        _sp.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                 "-C", HERE, "archive", "--format=zip", base_ref],
                stdout=f, check=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(SNAP)
    os.remove(archive)
    # .pyc/缓存可能和基线源码不一致,扫掉
    for root, dirs, files in os.walk(SNAP):
        for d in list(dirs):
            if d == "__pycache__":
                rmt_helper(os.path.join(root, d))
    man = manifest_of(SNAP)
    # 自证:快照必须逐字节等于那个 commit 的 blob。不等就是 archive/extract 坏了,
    # 或者 blob 真变了 —— 两种都不许静默继续。
    probe = "server.py"
    if probe in man:
        p = subprocess.run(["git", "-C", HERE, "cat-file", "blob",
                            "%s:%s" % (base_sha, probe)], capture_output=True)
        want = hashlib.sha256(p.stdout).hexdigest()
        if p.returncode != 0 or man[probe] != want:
            raise RuntimeError(
                "快照不等于 commit blob:%s 快照=%s blob(%s)=%s。"
                "基线不可信,拒绝继续。"
                % (probe, man.get(probe), base_sha[:12], want))
    return man, base_ref, base_sha


def rmt_helper(p):
    subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)


def _stamp():
    """本地时间 ISO-8601 到毫秒。给 run 的起止各打一次,用来交叉验证用时。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + \
        ".%03d" % (int(time.time() * 1000) % 1000)


# ══════════════════════════════ 测试覆盖层(overlay): 只搬**测试文件**,不搬被测代码
#
# 2026-10-03 实测出来的结构事实(不是推测):
#   clone_sandbox() 只从 SNAP 拷;  SNAP = git archive <base-ref>;
#   run_suite() 跑的是 `python <suite>` 且 cwd=sandbox。
#   => 套件实际执行的是**提交里那份**测试文件,工作区改了也没用。
# 而本轮任务就是「新增测试」—— 新测试不可能在 HEAD=1a1f1b1 的提交里
# (HEAD 不许动、不许 commit)。所以必须有覆盖层,否则「加了测试」是空话。
#
# 边界(必须硬):
#   * 只允许搬**测试文件**;任何被变异体当靶子的文件一律拒绝(那是代码)。
#     否则就把「被测代码」换成了工作区版本 —— C6 线正在改 server.py,
#     那样测的根本不是 1a1f1b1,整轮数全部作废。
#   * 每个覆盖文件都要把 工作区 sha256 + 字节数 记进结果 JSON,并在开头
#     大声打出来。不能让人以为跑的是基线自带的那份测试。
#
# 这**不改记账口径**: 分母/分子/verdict 判定一行未动。它只改「哪些测试参与测量」。
OVERLAY_FILES = []
# 被变异体当靶子的文件全集(硬边界)。模块级 —— evaluate() 跑在
# ThreadPoolExecutor 的线程里, 拿不到 main() 的局部变量。
OVERLAY_FORBIDDEN = set()


def apply_overlay(box, targets):
    """把 OVERLAY_FILES 里的文件从工作区搬进沙盒, 覆盖快照那一份。返回清单。"""
    rec = []
    for rel in OVERLAY_FILES:
        if rel.replace("\\", "/") in targets:
            raise RuntimeError(
                "拒绝覆盖 %s: 它是**被测代码**(某个变异体的 target)。\n"
                "  覆盖被测代码 = 把基线偷换成工作区版本, 那样测的不是 %s。"
                % (rel, "base-ref"))
        src = os.path.join(HERE, *rel.split("/"))
        dst = os.path.join(box, *rel.split("/"))
        if not os.path.isfile(src):
            raise RuntimeError("覆盖层文件不存在: %s" % src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        rec.append({"file": rel, "sha256": sha256_file(dst),
                    "bytes": os.path.getsize(dst)})
    return rec


def clone_sandbox(dst):
    for _ in range(8):
        if os.path.isdir(dst):
            rmt_helper(dst)
            time.sleep(0.4)
            continue
        shutil.copytree(SNAP, dst, ignore=shutil.ignore_patterns("__pycache__"))
        return dst
    raise RuntimeError("沙盒建不出来: %s" % dst)


def _norm(line):
    """空白归一化:连续空白折叠成单空格并去首尾。锚点因此与缩进无关。"""
    return re.sub(r"[ \t]+", " ", line).strip()


def locate_edit(lines, e, target):
    """在一份**有上界**的行表里找 anchor 唯一命中的那一行。

    跨行 anchor 按 span=行数 做窗口匹配,取**窗口第一行**为被改行(其余是
    用来消歧的**后文**上下文)。
    返回 (idx, state, detail);state ∈ EXACT / GONE / AMBIGUOUS,idx 为 0-based。

    * GONE      : 上界内 0 处命中,或唯一命中行不含 expect(我们想改的东西没了)
    * AMBIGUOUS : 上界内 >1 处命中 —— 硬失败,不取第一个

    一次线性扫描找全部出现(允许重叠),没有循环放宽、没有重试。
    """
    hi = len(lines) if not e.window else min(len(lines), e.window)
    parts = [_norm(p) for p in e.anchor.split("\n")]
    span = len(parts)
    if span > hi:
        return None, "GONE", ("锚点跨 %d 行,超过上界 %d 行" % (span, hi))
    needle = "\n".join(parts)
    hay = "\n".join(_norm(l) for l in lines[:hi])
    hits, start = [], 0
    while True:
        k = hay.find(needle, start)
        if k < 0:
            break
        idx = hay.count("\n", 0, k)          # 归一化不改行数,偏移可直接换算
        if idx + span - 1 < hi:
            hits.append(idx)                 # 约定:窗口第一行 = 被改行
        start = k + 1                        # +1 而不是 +len:同一行内多处也要算
    # 去重但保序:同一目标行被命中多次只算一次
    uniq = sorted(set(hits))
    if not uniq:
        return None, "GONE", ("锚点 %r 在 %s 的前 %d 行内 0 处命中"
                              % (e.anchor, target, hi))
    if len(uniq) > 1:
        return None, "AMBIGUOUS", ("锚点 %r 在 %s 命中 %d 行 %s,拒绝取第一个"
                                   % (e.anchor, target, len(uniq),
                                      [h + 1 for h in uniq[:6]]))
    i = uniq[0]
    if e.expect and e.expect not in lines[i]:
        return None, "GONE", ("唯一命中行 %d 含锚点但不含 expect %r\n"
                              "              实际那行: %r"
                              % (i + 1, e.expect, lines[i].strip()[:110]))
    return i, "EXACT", "行 %d" % (i + 1)


def render_new(cur, new):
    """按当前行的缩进渲染替换文本(空 new = 清空该行内容,保留缩进)。

    保留原行尾(\\r / \\r\\n),否则改一行会在 LF 文件里凭空插一个 CRLF,
    制造出混排文件 —— 那种文件 python 还能解析,但之后任何按字节的比对、
    任何别的工具读它,结论都不可复现。"""
    indent = cur[:len(cur) - len(cur.lstrip())]
    body = new if new.startswith(" ") else indent + new
    tail = "\r" if cur.endswith("\r") else ""      # split("\\n") 后 \\r 留在行尾
    return body + tail


def apply_mutation(mut, sandbox):
    """把变异落到沙盒副本上。返回 (state, detail)。

    state ∈ LANDED / VOID / NOOP
    * VOID  : 锚点 GONE 或 AMBIGUOUS —— **不是**存活,是这一条根本没打上去。
              绝不进 KILLED/SURVIVED 的分母(main 里 invalid 已排除)。
    * NOOP  : 打上去了但新旧文本相同(变异本身写错了)。
    """
    path = os.path.join(sandbox, *mut.target.split("/"))
    if not os.path.isfile(path):
        return "VOID", "目标文件不在快照里:%s" % mut.target
    before = sha256_file(path)
    lines = io.open(path, encoding="utf-8", newline="").read().split("\n")
    resolved = []
    applied = 0
    for e in mut.edits:
        idx, state, detail = locate_edit(lines, e, mut.target)
        resolved.append({"anchor": e.anchor, "expect": e.expect, "new": e.new,
                         "locate": state, "detail": detail})
        if state != "EXACT":
            return "VOID", "[%s] %s" % (state, detail)
        cur = lines[idx]
        want = render_new(cur, e.new)
        if cur == want:
            applied += 1
            resolved[-1]["locate"] = "NOOP"
            continue
        lines[idx] = want
        # 改完之后应该出现的字面量 —— 必须真的在那一行里,否则说明 new 写错了
        # (典型: 引号/转义写错,渲染出来的行并不是我们以为的那行)
        if e.new.strip() and e.new not in lines[idx]:
            return "VOID", ("改完校验失败:%d 行不含 new %r,实际 %r"
                            % (idx + 1, e.new, lines[idx].strip()[:110]))
        applied += 1
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    after = sha256_file(path)
    if after == before:
        return "NOOP", "应用后 sha256 没变"
    return "LANDED", ("%d 处编辑, sha %s -> %s"
                      % (applied, before[:12], after[:12]))


# ═══════════════════════════════════════════ 探针 stderr 的**分级**归类
#
# ── 2026-10-03: 旧判据「stderr 非空 => VOID/PROBE-ERROR」已撤回,原形是错的 ──
#
# 它把 4 条 KILLED 变成 VOID, 其中至少 2 条是误判:
#   stderr = "SyntaxError: invalid syntax"                  -> 变异体加载不了 (VOID 对, 类名要专)
#   stderr = "IndexError: tuple index out of range"         -> 是**变异体**崩了, 不是探针的 bug
#   stderr = '[aigit] "GET /api/file?file=sub HTTP/1.1" 200 -' -> **服务器访问日志, 根本不是错误**
#
# 那个 HTTP 访问日志最要命: 一条本来被杀掉的变异体, 因为程序往 stderr 打了行日志
# 就被判成「测不出来」。**VOID 正是那个「看起来最安全」的方向** —— 和解析器
# 那个病一模一样, 只是又上了一层。而 VOID 会把条目移出分母, 分母变小,
# 百分比天然变大 —— 于是这条虚高的路径还顺带抬高了击杀率。
#
# 病根是**判据的形状错了**: 「非空」是一个会被别的动作改变的量。程序正常说话
# 就是非空的; 变异体崩了就是非空的。正确的形状是**归类**:
#
#   1. 被变异的文件在 import/compile 期就失败
#      -> MUTANT-UNLOADABLE  这是关于**变异体**的事实, 类名独立, 不并进 PROBE-ERROR
#   2. traceback 指向**探针/引擎自己的代码**
#      -> ENGINE-ERROR       测量工具坏了, 必须响(非 0 退出 + 打印完整 traceback
#                            + 该条不进任何统计)。探针坏了就修探针。
#   3. 被测程序自己崩了 / 自己说话(访问日志、进度、告警)
#      -> 不是错误            行为确实变了 => LANDED, 交给套件判 KILLED/SURVIVED
#   4. 非空但归不进以上任何一类
#      -> ENGINE-ERROR(未归类) 默认响, 不默认放过
#
# **「只在 import/compile 期判 unloadable, 不对运行期 stderr 一刀切」** ——
# 第 3 类是这条的具体化。
#
# 另一个必须记的教训: 旧 run_probe 只保留 stderr 的**最后一行**
# (`(p.stderr.strip().splitlines() or [""])[-1]`), 于是 traceback 里
# 「哪个文件哪一行」被丢光, 只剩 `IndexError: tuple index out of range`。
# 归类需要 traceback, 所以现在保留**全量**。

# import/compile 期失败的特征异常(模块级)
_LOADFAIL_EXC = ("SyntaxError", "IndentationError", "ImportError",
                 "ModuleNotFoundError", "TabError")
# 运行期崩溃的特征异常(程序自己抛的, 不是加载失败)
_RUNTIME_EXC = ("IndexError", "KeyError", "AttributeError", "TypeError",
                "ValueError", "ZeroDivisionError", "StopIteration",
                "UnicodeDecodeError", "RecursionError", "AssertionError",
                "NotADirectoryError", "FileNotFoundError", "OSError")


def classify_probe_stderr(err, probe_path, target_name):
    """把探针 stderr 归成上面 4 类之一。返回 (类别, 证据行)。

    类别: MUTANT-UNLOADABLE | ENGINE-ERROR | MUTANT-CRASH | PROGRAM-OUTPUT
          | UNCLASSIFIED | NONE

    probe_path : 探针脚本自己的路径(p.py)—— traceback 指向它就是引擎的错
    target_name: 被变异的文件名(server.py 等)—— traceback 指向它就是变异体的事
    """
    if not err or not err.strip():
        return "NONE", ""
    lines = [l for l in err.strip().splitlines() if l.strip()]

    # Python 的 traceback 是**外层在前、内层在后**。哪一层抛异常, 看的是
    # **最内层那一帧**, 不是「有没有出现过 p.py」。
    #   探针 p.py 的帧永远是最外层那一帧(它调用进去的), 所以拿「出现过 p.py」
    #   当判据会把**每一条** traceback 都判成 ENGINE-ERROR —— 这就是上一版
    #   4/8 fixture 全红的成因。是被 fixture 逼出来的, 不是猜的。
    frames = [l for l in lines if l.strip().startswith("File ")]
    innermost = frames[-1] if frames else ""
    probe_base = probe_path.replace("\\", "/").lower().split("/")[-1] if probe_path else "p.py"
    probe_here = probe_base in innermost.lower() or "mutation_runner" in innermost.lower()
    mut_here = bool(target_name) and target_name in innermost
    if probe_here:
        return "ENGINE-ERROR", innermost.strip()

    exc_lines = [l for l in lines if l and l[0] not in " \t" and
                 ("Error" in l or "Exception" in l)]
    exc = exc_lines[0].strip() if exc_lines else ""

    for name in _LOADFAIL_EXC:
        if exc.startswith(name) or ("%s:" % name) in exc:
            # 加载失败 -> 关于变异体的事实。无论最内层落在哪, 加载期失败都
            # 说明这个变异体模块根本 import 不了 -> VOID/MUTANT-UNLOADABLE。
            return "MUTANT-UNLOADABLE", exc

    for name in _RUNTIME_EXC:
        if exc.startswith(name) or ("%s:" % name) in exc:
            # 运行期崩溃。最内层在被变异文件里 => 变异体崩了, 行为变了 => MUTANT-CRASH;
            # 最内层在别处 => 归不出来, 默认响。
            return ("MUTANT-CRASH", exc) if mut_here else ("UNCLASSIFIED", exc)

    # 有 traceback 但认不出异常类型
    if frames:
        return "UNCLASSIFIED", (innermost or exc or lines[-1]).strip()

    # 没有任何 traceback 结构 -> 多半是程序自己的输出(访问日志/进度/告警)
    return "PROGRAM-OUTPUT", lines[-1].strip()[:160]


def run_probe(sandbox, probe_id, timeout=240):
    """在沙盒里跑行为探针, 返回 (签名, **全量** stderr)。签名是全部 SIG 行。

    stderr 保留全量而不是最后一行: 归类必须看 traceback 指向哪个文件,
    只留最后一行会把「变异体的锅」和「探针的锅」混成同一条看不懂的错误。
    """
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
    return sig, (p.stderr or "").strip()


def selfcert(mut, sandbox):
    """两段式自证。

    1. 变异体签名必须**不等于** expect —— 相等说明这一段变异没改变被探针测的
       那个行为(no-op)。
    2. 更严的一道闸放在 --probe-only 里: 对照(未变异)签名必须**逐字等于**
       expect。对不上就是 PROBE_DRIFT —— 探针的输入压根没碰到那段代码,
       这时候「变异存活了」是探针的错, 不是套件的错。两回事, 不能混。

    **stderr 走分级归类, 不再一刀切**(见 classify_probe_stderr 的完整说明):
        MUTANT-UNLOADABLE -> VOID/MUTANT-UNLOADABLE  (类名独立: 这是变异体的事实)
        ENGINE-ERROR      -> ENGINE-ERROR           (探针/引擎坏了, 必须响)
        MUTANT-CRASH      -> LANDED                 (变异体自己崩了 = 行为变了)
        PROGRAM-OUTPUT    -> LANDED                 (程序在说话, 不是错误)
        UNCLASSIFIED      -> ENGINE-ERROR           (默认响, 不默认放过)

    旧逻辑是「stderr 非空 => PROBE-ERROR」, 那条把 HTTP 访问日志也当成错误,
    于是一条本来被杀掉的变异体被记成「测不出来」—— 而 VOID 会把它移出分母,
    顺带抬高击杀率。详见 classify_probe_stderr 顶部那段。
    """
    pr = PROBES[mut.probe]
    msig, merr = run_probe(sandbox, mut.probe)
    rec = {"control": pr["expect"], "mutant": msig,
           "err": (merr.strip().splitlines() or [""])[-1] if merr else ""}
    if not merr.strip():
        if msig == pr["expect"]:
            rec["state"] = "NOOP"
            return rec
        rec["state"] = "LANDED"
        return rec

    probe_py = "_probe_%s/p.py" % mut.probe
    target = mut.target.rsplit("/", 1)[-1]
    cls, evidence = classify_probe_stderr(merr, probe_py, target)
    rec["stderr_class"] = cls
    rec["stderr_evidence"] = evidence
    rec["stderr_full"] = merr[-4000:]      # 完整 traceback 落 JSON, 供事后复核
    rec["state"] = {"MUTANT-UNLOADABLE": "UNLOADABLE",
                    "ENGINE-ERROR": "PROBE-BROKEN",
                    "UNCLASSIFIED": "PROBE-BROKEN",
                    # 变异体自己崩了 / 程序在说话 => 行为确实变了, 交给套件判
                    "MUTANT-CRASH": "LANDED",
                    "PROGRAM-OUTPUT": "LANDED"}.get(cls, "PROBE-BROKEN")
    if rec["state"] == "LANDED" and msig == pr["expect"]:
        # 崩了但签名仍等于 expect(如崩在无关路径上)—— 判不了, 走 NOOP
        rec["state"] = "NOOP"
    return rec


# 每个解析器都必须有一份 fixture: 一段**人工构造、计数已知**的输出,
# 断言 parse_suite 数出来的计数与已知值逐项相等。
#
# 为什么必须这样: 这个项目被伪造绿坑过很多次, 而这一次伪造绿藏在
# 「测量工具自己从没被测过」里。「我改了正则」不是证据;
# 「我拿一份已知输出的日志喂它, 它数对了」才是。
#
# 每份 fixture 都**故意同时含通过行和失败行** —— 只放通过行的 fixture
# 抓不到「查表键取错」这类缺陷(keyHit=0 而用例全是 pass, 计数仍可能对得上)。
PARSER_FIXTURES = [
    # ---- test_aigit.py / test_aigit_extra.py 共用 MARK_RE ----
    {"suite": "test_aigit.py", "why": "MARK_RE: 2 pass + 1 fail + 1 skip + 1 error",
     "out": ("    ok   A-1            first case\n"
             "   FAIL  A-2            second case\n"
             "   skip  A-3            third case\n"
             "  ERROR  A-4            fourth case\n"
             "    ok   A-5            fifth case\n"
             "汇总: 共 5 条  PASS 2  FAIL 1  SKIP 1  ERROR 1\n"),
     "want": {"pass": 2, "fail": 1, "error": 1, "skip": 1, "warn": 0,
              "parsed_total": 5, "claimed_total": 5, "claimed_fail": 2,
              "crosscheck": "OK", "parse_ok": True}},
    {"suite": "test_aigit_extra.py", "why": "同 MARK_RE, 但只 1 条通过",
     "out": ("    ok   E-1            only case\n"
             "汇总: 共 1 条  PASS 1  FAIL 0  SKIP 0  ERROR 0\n"),
     "want": {"pass": 1, "fail": 0, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 1, "claimed_total": 1, "claimed_fail": 0,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- test_conflicts.py: TAP, 名字含空格, 且必须抓到 not ok ----
    {"suite": "test_conflicts.py",
     "why": "TAP: 名字含空格 + not ok 行(旧解析器这两条都抓不到)",
     "out": ("ok 1 - identify: basic conflict detection\n"
             "ok 2 - threeway: ours/theirs/base rendering\n"
             "not ok 3 - guard-path: 路径越界被拒绝(各种花样)\n"
             "ok 4 - read-nomerge: reading without merge\n"
             "# passed 3 of 4 test(s), skipped 0\n"),
     "want": {"pass": 3, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 4, "claimed_total": 4, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # 新套件与 test_conflicts.py 同为 TAP, 必须**同样**有 fixture ——
    # 「共用同一份正则」不等于「共用同一份验证」。
    {"suite": "test_aigit_invariants.py",
     "why": "TAP: 护栏不变量, 4 条 1 红(新套件, 防止只测了老套件)",
     "out": ("1..4\n"
             "ok 1 - pin-plain-junction-parent-rejected\n"
             "ok 2 - pin-plain-plain-path-allowed\n"
             "not ok 3 - gen-prefix-unprefixed-rejected\n"
             "ok 4 - gen-prefix-prefixed-allowed\n"
             "# failed 1 among 4 test(s) (skipped 0)\n"),
     "want": {"pass": 3, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 4, "claimed_total": 4, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # 第二种 summary 行: 有失败时套件改印 "# failed N among M"。
    # 旧正则只认 "# passed ...", 于是**每一次有失败的 run 都 NO-SUMMARY**,
    # 交叉校验被跳过 —— 又一个「只在失败时才瞎」的洞。必须有 fixture 钉住。
    {"suite": "test_conflicts.py",
     "why": "第二种 summary: '# failed N among M'(旧解析器在这里瞎)",
     "out": ("ok 1 - identify: basic conflict detection\n"
             "not ok 2 - guard-path: 路径越界被拒绝(各种花样)\n"
             "ok 3 - read-nomerge: reading without merge\n"
             "# failed 1 among 3 test(s) (skipped 0)\n"),
     "want": {"pass": 2, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 3, "claimed_total": 3, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- test_history_ops.py: 名字含空格 + 通过用例只点 '.' ----
    {"suite": "test_history_ops.py",
     "why": "名字含空格/';'/'-'; 通过用例**只打点**不打印 -> 点数必须算成 pass",
     "out": (".........\n"
             "not ok A-every destructive do_* refuses without confirmed=True - every destructive do_* refuses without confirmed=True\n"
             "not ok B-stash apply keeps the entry; drop is two-step - stash apply keeps the entry; drop is two-step\n"
             "共 2 条,过 0,失败 2,异常 0\n"),
     "want": {"pass": 9, "fail": 2, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 11, "claimed_total": 2, "claimed_fail": 2,
              "crosscheck": "OK", "parse_ok": False}},
    # ⚠ 上面那份 fixture 把 9 个点挤在**一行** —— 正好躲开了「点散在多行时
    #   名字碰撞」那个洞。这份**故意拆成两行**, 旧写法会数成 5 个 pass
    #   (dot-pass-0..2 与 dot-pass-0..4 去重), parsed_total=6 != 9 -> MISMATCH。
    {"suite": "test_history_ops.py",
     "why": "点在**多行**上(旧写法行内下标会碰撞 -> 少算 pass)",
     "out": ("...\n"
             ".....\n"
             "not ok A-x - 某用例\n"
             "共 9 条,过 8,失败 1,异常 0\n"),
     "want": {"pass": 8, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 9, "claimed_total": 9, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- test_aigit_net.py: 点是**行首连续的点**, 后面紧跟用例自己打的文字 ----
    {"suite": "test_aigit_net.py",
     "why": "点散在 12 行且每行后面有文字 -> 必须数行首连续的点, 且名字跨行唯一",
     "out": ("测试目录: X\n"
             ".\n"
             "...\n"
             ".....        import 语句共 8 条\n"
             "not ok B1-foo - 某用例\n"
             "        某失败详情\n"
             "...\n"
             ".....        GitOps 公开方法 11 个\n"
             "共 18 条,过 17,失败 1,异常 0,用时 1.0s\n"),
     "want": {"pass": 17, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 18, "claimed_total": 18, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- selftest.py: [通过] / [失败], 名字含空格 ----
    # summary "N / M 组通过" **有** total 字段(=M), 但**没有**失败字段,
    # 所以 claimed_total 可对账、claimed_fail 不可对账(NO-FAIL-FIELD)。
    {"suite": "selftest.py",
     "why": "[通过]/[失败] 行, 名字含空格; summary 有 total 无 fail 字段",
     "out": ("  [通过] mergeable 共 3 场景 + 不留残留\n"
             "  [失败] conflict 共 1 场景\n"
             "  [通过] server.py 可导入\n"
             "  2 / 3 组通过\n"),
     "want": {"pass": 2, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 3, "claimed_total": 3, "claimed_fail": None,
              "crosscheck": "NO-FAIL-FIELD", "parse_ok": True}},
    # ---- test_aigit_web.py ----
    {"suite": "test_aigit_web.py", "why": "PASS/FAIL/WARN/SKIP 四种状态",
     "out": ("[PASS] esc   quote escaping        ok\n"
             "[FAIL] xss   attribute injection   bad\n"
             "[WARN] perf  large diff             slow\n"
             "[SKIP] net   live server           skipped\n"
             "汇总:1 PASS / 1 FAIL / 1 WARN / 1 SKIP\n"),
     "want": {"pass": 1, "fail": 1, "error": 0, "skip": 1, "warn": 1,
              "parsed_total": 4, "claimed_total": None, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- smoke.py: 关键 —— FAIL 行**没有** '-> detail' ----
    {"suite": "smoke.py",
     "why": "PASS 行带 '-> detail', FAIL 行不带 —— 旧正则强制要求箭头, "
            "于是每一条失败行都被排除",
     "out": ("[PASS] 01  server 起来并应答 health                -> 200 {\"ok\": true}\n"
             "[FAIL] 02  server 拒绝非法分支名\n"
             "[INFO] 03  建临时仓\n"
             "  通过 1    失败 1    跳过 0\n"),
     "want": {"pass": 1, "fail": 1, "error": 0, "skip": 0, "warn": 0,
              "parsed_total": 2, "claimed_total": None, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
    # ---- test_f06_guard.py: 行首**无缩进** + 'base' 行必须被排除 ----
    {"suite": "test_f06_guard.py",
     "why": "行首无缩进(与 MARK_RE 相反); 'base' 是基线测量不进汇总, "
            "fixture 里故意放一条 —— 若被算进去 parsed_total 会变 6 而 summary 说 5",
     "out": ("ok   T1                               (1.6s)\n"
             "ok   T2                               (1.1s)\n"
             "FAIL T3                               T1 的 PASS 不可信\n"
             "ok   T4                               (11.0s)\n"
             "skip T5                               net\n"
             "base L-1                              (1.2s)\n"
             "# 共 5 条  PASS 3  FAIL 1  SKIP 1  用时 15.0s\n"),
     "want": {"pass": 3, "fail": 1, "error": 0, "skip": 1, "warn": 0,
              "parsed_total": 5, "claimed_total": 5, "claimed_fail": 1,
              "crosscheck": "OK", "parse_ok": True}},
]


def selftest_parsers(verbose=True):
    """解析器反向自测: 拿已知计数的 fixture 喂 parse_suite, 数错了就红。

    返回失败清单(空 = 全过)。这是「测量工具自己被测过」的证据。
    """
    bad = []
    covered = set()
    for fx in PARSER_FIXTURES:
        suite, want = fx["suite"], fx["want"]
        covered.add(suite)
        cases, info = parse_suite(suite, fx["out"])
        got = {"pass": len(cases["pass"]), "fail": len(cases["fail"]),
               "error": len(cases["error"]), "skip": len(cases["skip"]),
               "warn": len(cases["warn"]), "parsed_total": info["parsed_total"],
               "claimed_total": info["claimed_total"],
               "claimed_fail": info["claimed_fail"],
               "crosscheck": info["crosscheck"], "parse_ok": info["parse_ok"]}
        diffs = ["%s: want %r got %r" % (k, want[k], got[k])
                 for k in want if want[k] != got[k]]
        if diffs:
            bad.append((suite, diffs))
            if verbose:
                print("  %-22s FAIL  %s" % (suite, fx["why"]))
                for d in diffs:
                    print("      %s" % d)
        elif verbose:
            print("  %-22s ok    %s" % (suite, fx["why"]))
    missing = set(SUITE_PARSERS) - covered
    if missing:
        bad.append(("MISSING-FIXTURE", ["没有 fixture 的套件: %s"
                                        % sorted(missing)]))
        if verbose:
            print("  !! 没有 fixture 的套件: %s" % sorted(missing))
    if verbose:
        print("\n解析器自测: %d/%d 份 fixture 通过; 覆盖 %d/%d 个套件"
              % (len(PARSER_FIXTURES) - len([b for b in bad
                                             if b[0] != "MISSING-FIXTURE"]),
                 len(PARSER_FIXTURES), len(covered), len(SUITE_PARSERS)))
    return bad


PROBE_STDERR_FIXTURES = [
    # ── 类 1: 被变异文件在 import/compile 期失败 -> MUTANT-UNLOADABLE ──
    {"why": "语法错误(2026-10-03 实测 S-checkout-branchexists-off)",
     "want": "MUTANT-UNLOADABLE",
     "err": ("Traceback (most recent call last):\n"
             "  File \"C:\\...\\S-checkout-branchexists-off\\_probe_checkout_exists\\p.py\", line 4, in <module>\n"
             "  File \"C:\\...\\S-checkout-branchexists-off\\server.py\", line 1045\n"
             "    if not args.confirmed and act in ('del',):\n"
             "                             ^^^^^\n"
             "SyntaxError: closing parenthesis ')' does not match opening parenthesis '['")},
    {"why": "导入期就炸(语义类 1)",
     "want": "MUTANT-UNLOADABLE",
     "err": ("Traceback (most recent call last):\n"
             "  File \"p.py\", line 4, in <module>\n"
             "  File \"C:\\...\\server.py\", line 555, in resolve_in_root\n"
             "ImportError: cannot import name 'GitError' from 'server'")},
    # ── 类 2: 最内层落在探针自己的代码 -> ENGINE-ERROR ──
    {"why": "探针自己解包炸了(测量工具的锅, 不许记到变异体头上)",
     "want": "ENGINE-ERROR",
     "err": ("Traceback (most recent call last):\n"
             "  File \"C:\\...\\mut\\S-x\\_probe_ref_whitelist\\p.py\", line 21, in <module>\n"
             "    a, b = r.stdout.split()\n"
             "ValueError: not enough values to unpack (expected 2, got 1)")},
    {"why": "类2: 最内层在 p.py 且异常类型不认识 —— 仍是探针的锅",
     "want": "ENGINE-ERROR",
     "err": ("Traceback (most recent call last):\n"
             "  File \"C:\\...\\S-y\\_probe_p\\p.py\", line 9, in <module>\n"
             "    boom()\n"
             "SomeBrandNewError: something nobody anticipated")},
    # ── 类 3a: 被测程序自己崩了(traceback 在被变异文件里) -> MUTANT-CRASH ──
    #      这是 2026-10-03 S-resolve-rootself-off 的真实 stderr:
    #      守卫被改成 if False 之后, 落到下一行 p.relative_to(root).parts[0],
    #      而 Path(".").parts == () -> IndexError。**这是变异体在工作**,
    #      不是探针坏了, 所以归 MUTANT-CRASH => LANDED, 交给套件判。
    {"why": "IndexError 在被变异文件里(守卫被删导致 parts[0] 越界)",
     "want": "MUTANT-CRASH",
     "err": ("Traceback (most recent call last):\n"
             "  File \"C:\\...\\S-resolve-rootself-off\\_probe_resolve_root_self\\p.py\", line 41, in <module>\n"
             "    r.resolve_in_root(rel)\n"
             "  File \"C:\\...\\S-resolve-rootself-off\\server.py\", line 555, in resolve_in_root\n"
             "    if p.relative_to(self.root).parts[0].lower() == \".git\":\n"
             "IndexError: tuple index out of range")},
    # ── 类 3b: 程序在说话(访问日志) -> PROGRAM-OUTPUT ──
    #      这条是最要命的旧误判: 一条本来被杀掉的变异体, 因为 server 往 stderr
    #      打了行访问日志就被判「测不出来」。stderr 非空, 但它根本不是错误。
    {"why": "HTTP 访问日志, 根本不是错误(2026-10-03 S-api-file-dir-e23-off)",
     "want": "PROGRAM-OUTPUT",
     "err": '[aigit] "GET /api/file?file=sub HTTP/1.1" 200 -'},
    {"why": "普通告警/进度也在 stderr",
     "want": "PROGRAM-OUTPUT",
     "err": "warning: LF will be replaced by CRLF in a.txt on checkout."},
    # ── 类 4: 非空但归不进去 -> UNCLASSIFIED(默认响) ──
    #      最内层落在被变异文件里、但异常类型谁都不认识。判据不许「不认识就放过」。
    {"why": "类4: 最内层在 server.py 但异常类型不认识(默认响)",
     "want": "UNCLASSIFIED",
     "err": ("Traceback (most recent call last):\n"
             "  File \"C:\\...\\S-y\\_probe_p\\p.py\", line 9, in <module>\n"
             "    r.something()\n"
             "  File \"C:\\...\\S-y\\server.py\", line 777, in something\n"
             "    raise Weird\n"
             "Weird: nobody anticipated this")},
    # ── 空 -> NONE ──
    {"why": "空 stderr", "want": "NONE", "err": ""},
]


def selftest_probe_stderr(verbose=True):
    """stderr 分级判据的反向自测。

    「D 条要求写错了」这件事本身就是**判据没被测过**造成的。上一轮我写
    「stderr 非空 => VOID」, 写了就信了, 没拿真实 stderr 喂过 ——
    结果把 4 条 KILLED 变成 VOID。所以这次分级判据必须自带 fixture。
    """
    bad = []
    for fx in PROBE_STDERR_FIXTURES:
        got, _ev = classify_probe_stderr(fx["err"], "p.py", "server.py")
        if got != fx["want"]:
            bad.append((fx["why"], fx["want"], got))
            if verbose:
                print("  FAIL  %-52s want=%-20s got=%s"
                      % (fx["why"], fx["want"], got))
        elif verbose:
            print("  ok    %-52s -> %s" % (fx["why"], got))
    if verbose:
        print("\n探针 stderr 分级自测: %d/%d 通过"
              % (len(PROBE_STDERR_FIXTURES) - len(bad), len(PROBE_STDERR_FIXTURES)))
    return bad


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
    cases, info = parse_suite(suite, out)
    red = cases["fail"] + cases["error"]
    detail = []
    if info["claimed_total"] is not None:
        detail.append("抓到 %d 条 / 声称 %d 条" % (info["parsed_total"],
                                                  info["claimed_total"]))
    else:
        detail.append("抓到 %d 条 / 声称 ? 条" % info["parsed_total"])
    return {"suite": suite, "rc": rc, "sec": round(el, 1),
            "pass": len(cases["pass"]), "fail": len(cases["fail"]),
            "error": len(cases["error"]), "skip": len(cases["skip"]),
            "warn": len(cases["warn"]), "red": red, "summary": info["summary"],
            "parse_ok": info["parse_ok"], "crosscheck": info["crosscheck"],
            "claimed_fail": info["claimed_fail"],
            "claimed_total": info["claimed_total"],
            "parsed_total": info["parsed_total"],
            "parse_detail": " / ".join(detail),
            "crosscheck_detail": cross_check(suite, out, info)}


# ════════════════════════════════════════════════════════ 评估一个变异体

def evaluate(mut, logdir=None):
    box = os.path.join(SB, mut.id)
    rec = {"id": mut.id, "target": mut.target, "guard": mut.guard_class,
           "why": mut.why, "suites": mut.suites, "probe": mut.probe,
           "edits": [{"anchor": e.anchor, "expect": e.expect, "new": e.new,
                      "window": e.window} for e in mut.edits]}
    tgt = os.path.join(box, *mut.target.split("/"))

    def _final_sha():
        # 目标文件不存在时返回 None 而不是让 sha256_file 抛
        # (旧代码在这里会抛 FileNotFoundError -> 被吞成 ENGINE-ERROR,
        #  把「文件不在快照里」谎报成引擎故障)
        return sha256_file(tgt) if os.path.isfile(tgt) else None

    try:
        clone_sandbox(box)
        ov = apply_overlay(box, OVERLAY_FORBIDDEN) if OVERLAY_FILES else []
        if ov:
            rec["overlay"] = ov
        state, detail = apply_mutation(mut, box)
        rec["apply"] = state
        rec["apply_detail"] = detail
        if state != "LANDED":
            # VOID 细分:锚点 GONE / AMBIGUOUS 各自单列,绝不塌成 SURVIVED
            sub = "GONE"
            if detail.startswith("[AMBIGUOUS]"):
                sub = "AMBIGUOUS"
            elif detail.startswith("[GONE]"):
                sub = "GONE"
            elif state == "NOOP":
                sub = "NOOP"
            rec["verdict"] = "VOID/" + sub
            rec["final_sha"] = _final_sha()
            return rec
        rec["final_sha"] = _final_sha()
        cert = selfcert(mut, box)
        rec["cert"] = cert
        if cert["state"] == "UNLOADABLE":
            # 类名独立: 这是关于**变异体**的事实(它加载不了), 不是「探针测不了」。
            # 混在一个类里就永远答不出「有几种原因导致测不出来」。
            rec["verdict"] = "VOID/MUTANT-UNLOADABLE"
            rec["apply_detail"] = ("变异体在 import/compile 期就失败, 套件无法采集: %s"
                                   % (cert.get("stderr_evidence") or "")[:120])
            return rec
        if cert["state"] == "PROBE-BROKEN":
            # 测量工具坏了。这是**引擎**的错, 不许记到变异体头上,
            # 也不许进任何统计。必须响 —— main() 见到它就非 0 退出。
            rec["verdict"] = "ENGINE-ERROR"
            rec["apply_detail"] = ("探针/引擎错误(stderr 归类=%s), 测量无效: %s"
                                   % (cert.get("stderr_class"),
                                      cert.get("stderr_evidence") or "")[:120])
            rec["stderr_full"] = cert.get("stderr_full")
            return rec
        if cert["state"] != "LANDED":
            # 打上去了,但变异没改变被探针测的那个行为 -> 这一条测不出东西
            rec["verdict"] = "VOID/PROBE-NOOP"
            return rec
        # 没有套件可跑 -> 绝不能报 SURVIVED。
        # 旧代码这里 suites=[] -> killed=[] -> "SURVIVED",于是「一条套件都没跑」
        # 和「跑了但没杀掉」长得一模一样。这正是「命令返回成功而事情没发生」。
        if not mut.suites:
            rec["verdict"] = "VOID/NO-SUITES"
            rec["apply_detail"] = ("没有任何套件会跑(检查 SUITES_BY_TARGET 是否"
                                   "还认得 target=%r)" % mut.target)
            return rec
        rec["suites_run"] = []
        for suite in mut.suites:
            rec["suites_run"].append(run_suite(box, suite, logdir))
        killed = [r for r in rec["suites_run"] if r["red"]]
        rec["killed_by"] = sorted({n for r in killed for n in r["red"]})
        rec["killed_suites"] = [r["suite"] for r in killed]
        # 全部套件都解析不出任何用例 -> 我们什么都没学到,不能报 SURVIVED。
        # 典型触发: 套件文件不在快照里(rc=2)、或 import 就崩了。
        # 只要**有一个**套件解析正常且没红,那才是真的 SURVIVED。
        readable = [r for r in rec["suites_run"] if r["parse_ok"]]
        rec["parse_ok_count"] = len(readable)
        # summary 声称有失败、cases 却解析出 0 条红 => 解析器把失败丢了(B)。
        # 这是**硬错误**,不能记成 SURVIVED(那是「没测到」冒充「测了没杀掉」)。
        mismatch = [r for r in rec["suites_run"]
                    if r["crosscheck"] == "MISMATCH"]
        if mismatch:
            rec["verdict"] = "VOID/SUMMARY-CASE-MISMATCH"
            rec["apply_detail"] = "; ".join(
                (r["crosscheck_detail"] or "MISMATCH") for r in mismatch)[:400]
            return rec
        if not readable:
            rec["verdict"] = "VOID/UNPARSEABLE"
            rec["apply_detail"] = (
                "%d 个套件全部没抓全(抓到的用例数对不上 summary 声称的总数),"
                "没拿到任何结论; rc=%s; %s"
                % (len(rec["suites_run"]),
                   [r["rc"] for r in rec["suites_run"]],
                   "; ".join("%s %s" % (r["suite"], r["parse_detail"])
                             for r in rec["suites_run"])[:240]))
            return rec
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
    ap.add_argument("--base-ref", default=DEFAULT_BASE_REF,
                    help="快照基线,语义就是 `git archive <ref>`;"
                         "默认 %(default)s(=当前提交)。要可复现就显式给 sha,"
                         "不要靠默认值")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=None, help="结果 JSON 落盘位置")
    ap.add_argument("--keep", action="store_true", help="保留沙盒(调试)")
    ap.add_argument("--selftest-parsers", action="store_true",
                    help="只跑解析器反向自测(拿已知计数的 fixture 喂 parse_suite)")
    ap.add_argument("--out-jsonl", default=None,
                    help="增量落盘的 JSONL 路径(默认与 --out 同名 .jsonl)。"
                         "每条判完即 append + fsync, 崩溃只损失当前这一条。")
    ap.add_argument("--log-dir", default=None,
                    help="原始日志目录(默认 <TEMP>\\mut\\logs)。"
                         "换目录是为了**不覆盖**上一轮的证据日志 —— 那批日志是"
                         "唯一能复核『谁被误判』的证据, 覆盖掉就不可再裁定。")
    ap.add_argument("--overlay-test", action="append", default=None,
                    metavar="FILE.py",
                    help="把这个**测试文件**从工作区覆盖进每个沙盒(可重复)。"
                         "因为套件跑的是快照里的测试文件, 而新测试不可能在"
                         "HEAD 的提交里。被变异体当靶子的文件会被拒绝。")
    args = ap.parse_args(argv)

    if args.selftest_parsers:
        print("=== 解析器反向自测(每个套件一份计数已知的 fixture)===")
        bad = selftest_parsers()
        print("\n=== 探针 stderr 分级自测(判据的形状是「归类」, 不是「非空」)===")
        bad2 = selftest_probe_stderr()
        return 1 if (bad or bad2) else 0

    man, base_ref, base_sha = build_snapshot(base_ref=args.base_ref)
    print("base-ref: %s -> %s   (snapshot files: %d, byte-identical to commit blob)"
          % (base_ref, base_sha, len(man)))
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

    logdir = args.log_dir or os.path.join(SB, "logs")
    os.makedirs(logdir, exist_ok=True)

    # 覆盖层装配(在选了目录之后 —— 需要 catalog 的 target 列表做硬边界)
    OVERLAY_FILES[:] = [f.replace("\\", "/") for f in (args.overlay_test or [])]
    all_targets = set(m.target.replace("\\", "/") for m in muts)
    OVERLAY_FORBIDDEN.clear()
    OVERLAY_FORBIDDEN.update(all_targets)
    if OVERLAY_FILES:
        print("测试覆盖层(只搬测试文件, 被测代码一律不搬):")
        for f in OVERLAY_FILES:
            p = os.path.join(HERE, *f.split("/"))
            flag = "!! 这是被测代码, 会拒绝" if f in all_targets else "ok"
            print("  %-34s %s  sha256=%s  %d B"
                  % (f, flag, (sha256_file(p)[:16] if os.path.isfile(p) else "缺失"),
                     (os.path.getsize(p) if os.path.isfile(p) else -1)))
        print("  被测代码仍来自 %s 的快照, 未被触碰。" % base_sha[:12])
        print("")

    # ── 跑前查磁盘: 磁盘将满时**拒跑**, 不许开跑 ──────────────────────────
    # 2026-10-03 实测教训: C 盘被 pagefile.sys + 四条会话 + 累积垃圾
    # 顶到 0.01 GB, shell 被杀 -> 进程随之死 -> 11/15 的计算成果**全丢**。
    # 67 分钟的量说没就没, 所以开跑前必须把实测空间打出来。
    #
    # ── 但单点采样会误杀, 判据的形状必须是「趋势」而不是「瞬时值」 ──────
    # 实测(run-start 时 6.03 GB, 08:29 掉到 2.49 GB, 12:00 又回到 5.31 GB):
    # 两次暴跌期间**没有任何对应的文件删除**, 而 C:\Users\Public\tmp 全量
    # 只有 180 MB、mutbase 一直 16 MB 没长过 —— 所以不是泄漏, 是
    # **pagefile.sys 随内存压力涨缩**(本机 AutomaticManagedPagefile=True,
    # 分页文件当前 6714 MB / 峰值用量 1778 MB, 一次伸缩就是 GB 级)。
    #
    # 后果: 单点采样在 pagefile 的一次抖动上会**误拒**一次 67 分钟的 run。
    # 所以取 3 次采样, **全部**低于下限才拒跑(滞后), 并把三个值都印出来,
    # 让人能一眼分清「抖动」和「趋势」。
    def _free_gb():
        try:
            return shutil.disk_usage(SB).free / (1024.0 ** 3)
        except Exception as e:                    # pragma: no cover
            print("!! 读不到磁盘可用空间: %s" % e)
            return -1.0

    MIN_FREE_GB = 2.0
    samples = []
    for i in range(3):
        g = _free_gb()
        samples.append(g)
        print("磁盘采样 %d/3: %.2f GB (下限 %.1f GB)" % (i + 1, g, MIN_FREE_GB))
        if i < 2:
            time.sleep(2)
    lo, hi = min(samples), max(samples)
    print("磁盘可用: min=%.2f GB  max=%.2f GB  跨度=%.2f GB  (跨度大 = 在抖动, "
          "不是趋势)" % (lo, hi, hi - lo))
    real = [g for g in samples if g >= 0]
    if real and all(g < MIN_FREE_GB for g in real):
        print("\n*** 拒跑: 连续 %d 次采样都低于下限 %.1f GB (实测 %s)。 ***"
              % (len(real), MIN_FREE_GB, ", ".join("%.2f" % g for g in real)))
        print("上一轮就是这么死的(0.01 GB -> shell 被杀 -> 11/15 结果全丢)。")
        print("先清磁盘再跑。本次**没有**开始任何测量, 不是「跑了但没结果」。")
        return 4
    if real and lo < MIN_FREE_GB:
        print("  (有采样低于下限但并非全部 -> 判定为 pagefile 抖动, 放行。")
        print("   若这确实是满盘, 请看 min/max 跨度: 跨度小=趋势, 跨度大=抖动。)")

    print("快照基线 %s (%s)  server.py=%s  变异体 %d 个  workers=%d"
          % (base_ref, base_sha[:12], man.get("server.py", "?")[:16],
             len(sel), args.workers))
    print("沙盒根: %s\n" % SB)

    # F: 两个**独立**的时间戳 + elapsed。一个数可以被抄错/被复制,
    #    两个独立的数对不上立刻暴露(2026-10-02 与 10-03 两次不同基线的 run
    #    报出**完全相同**的 4029s, 就是只有单个数字时无法自查的典型)。
    print("run-start: %s" % _stamp())
    t0 = time.time()
    results = []

    # ── 抗中断落盘: 每条判完立刻 append 进 JSONL ──────────────────────────
    # 旧代码最后一次性 json.dumps 写文件。进程一死(磁盘满/shell 被杀),
    # **已经算完的那几条一起没了** —— 2026-10-03 实测 11/15 全丢就是这个。
    # JSONL 追加是**可判定的增量落盘**: 写进去的就是拿到的, 崩溃只损失当前这一条。
    out_jsonl = args.out_jsonl or (os.path.splitext(
        args.out or os.path.join(SB, "mutation_results.json"))[0] + ".jsonl")
    if os.path.exists(out_jsonl):
        os.remove(out_jsonl)          # 本次 run 的账, 不与旧 run 混在一起
    jf = io.open(out_jsonl, "a", encoding="utf-8", newline="\n")
    n_persisted = [0]

    def persist(r):
        """一条结果 -> 一行 JSON。无返回值失败即抛, 宁可让 run 死也不能静默丢。"""
        rec = dict(r)
        rec["_run_base_ref"] = base_ref
        rec["_run_base_sha"] = base_sha
        rec["_run_stamp"] = _stamp()
        rec["_run_seq"] = n_persisted[0] + 1
        jf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        jf.flush()
        os.fsync(jf.fileno())
        n_persisted[0] += 1

    print("增量落盘: %s  (每条判完即 append, 崩溃只损失当前这条)" % out_jsonl)

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
            persist(r)
            results.append(r)
            print("[%2d/%2d] %-28s %-16s %s"
                  % (done, len(sel), r["id"], r["verdict"],
                     (r.get("apply_detail") or r.get("error") or "")[:66]))
    jf.close()

    order = {m.id: i for i, m in enumerate(muts)}
    results.sort(key=lambda r: order.get(r["id"], 999))
    valid = [r for r in results if r["verdict"] in ("KILLED", "SURVIVED")]
    killed = [r for r in valid if r["verdict"] == "KILLED"]
    inval = [r for r in results if r["verdict"] not in ("KILLED", "SURVIVED")]

    print("\n" + "=" * 78)
    print("run-end  : %s   elapsed=%.1fs" % (_stamp(), time.time() - t0))
    vgone = len([r for r in inval if r["verdict"] == "VOID/GONE"])
    vamb = len([r for r in inval if r["verdict"] == "VOID/AMBIGUOUS"])
    vrest = [r for r in inval
             if r["verdict"] not in ("VOID/GONE", "VOID/AMBIGUOUS")]
    vother = len(vrest)
    print("汇总: 有效变异 %d  杀掉 %d  存活 %d  用时 %.0fs"
          % (len(valid), len(killed), len(valid) - len(killed),
             time.time() - t0))
    print("      作废(VOID,不算存活也不算存活率分母) %d" % len(inval))
    # 按类**逐条点名**。塌成一个「其它 N」就答不出「有几种原因导致测不出来」。
    byclass = {}
    for r in inval:
        byclass.setdefault(r["verdict"], []).append(r["id"])
    for cls in sorted(byclass):
        print("        %-28s %d  %s" % (cls, len(byclass[cls]),
                                       ", ".join(sorted(byclass[cls]))))
    if not valid:
        print("杀灭率 = 无法计算(有效变异 0 条)。'没有有效变异'和'杀灭率 0%%'"
              "是两件事,不要混。")
    else:
        # 两个口径都印。只印排除 VOID 的那个是**朝好看方向倒**:
        # 作废的条目不进分母, 分母变小, 百分比天然变大。
        print("杀灭率(剔作废) = %.1f%% (%d/%d)   [分母 = 真正打上去并测出结论的]"
              % (100.0 * len(killed) / len(valid), len(killed), len(valid)))
        # 分母用 len(sel) 而不是 len(muts): 跑子集时「含作废」必须算这次选的,
        # 否则只跑 1 条会印出 6.7%(1/15), 那个 15 是没跑过的目录全长 —— 又一个
        # 「数字看着像真的但量纲不对」的例子。--all 时 len(sel)==len(muts)==15。
        print("杀灭率(含作废) = %.1f%% (%d/%d)   [分母 = 本次选中的全部变异体]"
              % (100.0 * len(killed) / len(sel), len(killed), len(sel)))
        if inval:
            # 方向提示: 作废越多, 「剔作废」那个口径就越好看。两个都印, 不选。
            print("  ↑ 方向提示: 作废 %d 条会**抬高**「剔作废」口径(分母变小);"
                  "两口径之差 %.1f 个百分点就是作废条目造成的全部虚高。"
                  % (len(inval), 100.0 * len(killed) / len(valid)
                     - 100.0 * len(killed) / len(sel)))
    if inval:
        print("\n作废的变异体(不算进分母, 它们本来就没打上去):")
        for r in inval:
            print("  %-28s %-28s %s"
                  % (r["id"], r["verdict"],
                     (r.get("apply_detail") or r.get("error")
                      or r.get("cert", {}).get("err", ""))[:66]))

    out = args.out or os.path.join(SB, "mutation_results.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"base_ref": base_ref, "base_sha": base_sha, "snapshot": man,
         "results": results,
         "summary": {"valid": len(valid), "killed": len(killed),
                     "survived": len(valid) - len(killed), "invalid": len(inval),
                     "void_gone": len([r for r in inval
                                       if r["verdict"] == "VOID/GONE"]),
                     "void_ambiguous": len([r for r in inval
                                            if r["verdict"] == "VOID/AMBIGUOUS"]),
                     "sec": round(time.time() - t0, 1)}},
        indent=1, ensure_ascii=False))
    print("\n结果已落盘: %s  (+ 增量账 %s, %d 条)"
          % (out, out_jsonl, n_persisted[0]))
    # G: **原始日志一律保留**。这些 .txt 是唯一能推翻引擎结论的证据 ——
    #    2026-10-03 那 3 条真实失败就是靠它们从日志里重算出来的;
    #    若收尾删掉, 解析器缺陷就永远说不清「到底是套件没红还是没人读它的输出」。
    #    沙盒(变异体副本)照旧删除, 体积大且可重建; 日志小, 留。
    if not args.keep:
        for m in sel:
            shutil.rmtree(os.path.join(SB, m.id), ignore_errors=True)
    nlogs = (len([f for f in os.listdir(logdir) if f.endswith(".txt")])
             if os.path.isdir(logdir) else 0)
    print("原始日志已保留: %s  (%d 个文件) —— 重算/复核的唯一证据,不要删"
          % (logdir, nlogs))

    # ── 硬失败出口, 按严重程度 ────────────────────────────────────────
    # ENGINE-ERROR 最重: 测量工具坏了。这不是变异体的性质, 是**本次 run 无效**。
    # 必须响: 打印完整 traceback + 非 0 退出 + 该条不进任何统计(上面已排除)。
    eng = [r for r in results if r["verdict"] == "ENGINE-ERROR"]
    if eng:
        print("\n" + "#" * 78)
        print("### ENGINE-ERROR %d 条 —— 探针/引擎坏了, 本次 run 不可用于归档 ###"
              % len(eng))
        for r in eng:
            print("\n--- %s (%s) ---" % (r["id"], r.get("probe", "?")))
            if r.get("error"):
                print("exception: %s" % r["error"])
            print((r.get("stderr_full") or r.get("apply_detail") or "<无>")[:2000])
        print("#" * 78)
        return 5

    # B 的硬失败出口: 解析器把失败丢了 => 退出码非 0。
    # 静默丢弃是这里最坏的行为 —— 丢掉的正是「有失败」这个信号。
    if any(r["verdict"] == "VOID/SUMMARY-CASE-MISMATCH" for r in results):
        print("\n*** 至少一条套件出现 summary/用例不一致(解析器丢了失败)。"
              "本次结果不可用于归档。 ***")
        return 3

    # 计数必须自洽: KILLED + SURVIVED + VOID/ENG == 目录总数。
    # 不自洽就是有条目被静默漏掉, 那种汇总表看着像真的。
    if len(results) != len(sel):
        print("\n*** 计数不自洽: 判了 %d 条, 目录里 %d 条。 ***"
              % (len(results), len(sel)))
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())