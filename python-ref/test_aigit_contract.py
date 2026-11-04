#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""响应形状契约测试(2026-10-06 新增)。

## 为什么要有这个文件

`f155916`「消除并发判据 C3 的四处真缺陷」把 `/api/commit` 的响应从

    self._json({'ok': True, 'output': out, **REPO.status()})     # f155916^

改成

    out, st, err = REPO.commit_and_status(msg or "")
    self._ok_with_status(st, err)          # out 拿到手就没再被读过

`output` 键就此从响应里消失(AST 实测: `out` 在 server.py:1907 被赋值,
Load 出现在 0 处; `_ok_with_status` 只返回 `{'ok': True, **st}`)。
而 `web/index.html:2095` 仍然读 `d.output`, 所以**每次提交前端都显示
"(git 无输出)"**。

那一版没有任何套件发现: 全仓只有 `smoke.py:513` 断言过 `/api/commit`
的 `output`, `test_aigit.py` / `test_aigit_extra.py` / `test_aigit_web.py` /
`test_aigit_concurrency.py` / `test_aigit_gate.py` 全是 **0 处**
(`test_aigit_net.py` 那 3 处是 `/api/pull`, 另一个端点)。
本文件补的就是这个洞 —— **把「响应形状」也纳入测试范围**。

2026-10-06 晚些时候, `find_test_gaps.py`(只认 AST, 面来自真跑一遍 server)
报出 **4 个零提及键** —— 没有任何套件断言过它们: `currentBranch` / `origHead` /
`mergeMsgPath`(都在 `/api/conflict/state` 的 11 个键里)与 `trackingRefExists`
(`/api/remote/ahead-behind`)。本文件因此从 **6 个端点扩到 8 个**。

## 三条自律(2026-10-06 那轮踩出来的, 不是套话)

1. **判据不许与被检查对象同源。** 期望键集是**写死的字面量**, 依据是
   `git show f155916^:server.py` 的历史 handler 加上 smoke.py 实测响应体,
   **不是**从现行 `server.py` 推出来的。若改成从源码自动推导, 这个测试
   就变成了「用被检查者的说明书去检查被检查者」, 永远绿。
2. **0 项检查恒真。** 参与核对的契约条数必须 `>= MIN_CONTRACTS`,
   否则直接抛 —— 静默跳过一条契约比判错更险。
3. **阳性对照自己先验一次。** `--selftest` 那组在跑真实端点**之前**就要
   证明两件事: 检查器**会咬人**(缺键必须被抓到)且**不乱咬**(键齐必须放过)。

## 用法

    python test_aigit_contract.py --selftest    # 只跑自证, 秒出
    python test_aigit_contract.py               # 自证 + 真实端点
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# --------------------------------------------------------------------------
# 契约(写死的字面量 —— 见文件头第 1 条自律)
# --------------------------------------------------------------------------

# `server.py` 里 `{'ok': True, **REPO.status()}`; status() 的形状由
# smoke.py 实测响应体确认: ok/branch/root/staged/unstaged/conflicted/hasStaged
STATUS_KEYS = frozenset(
    ("ok", "branch", "root", "staged", "unstaged", "conflicted", "hasStaged")
)

# `output` 是 f155916 之前就在的键, 由 web/index.html:2095 实测消费。
COMMIT_KEYS = STATUS_KEYS | frozenset(("output",))

# 2026-10-06: `find_test_gaps.py` 报出 4 个**零提及**键(没有任何套件断言过),
# 全在这两个响应里。依据:
#   - conflict/state: doc/aigit-conflicts.md:352-353 列出的形状(11 键);
#     conflicts.py 的 merge_state() 是**一个**无条件 dict 字面量 —— 11 键恒在。
#   - ahead-behind: gitops.py 的两个 return 分支取**交集**(7 键) ——
#     ⚠ `note` 只在 `trackingRefExists=False` 时出现(条件键), 刻意不进必需集。
CONFLICT_STATE_KEYS = frozenset((
    "ok", "inMerge", "mergeHead", "origHead", "head", "currentBranch",
    "branches", "defaultMessage", "mergeMsgPath", "remaining", "files",
))
AHEAD_BEHIND_KEYS = frozenset((
    "ok", "remote", "branch", "basis", "trackingRefExists", "ahead", "behind",
))

# (标签, 方法, 路径, 请求体, 必需键)
CONTRACTS = (
    ("health",   "GET",  "/api/health",   None, frozenset(("ok", "root", "isRepo"))),
    ("status",   "GET",  "/api/status",   None, STATUS_KEYS),
    ("stage",    "POST", "/api/stage",    {"files": ["probe.txt"]}, STATUS_KEYS),
    ("commit",   "POST", "/api/commit",   {"message": "contract: probe"}, COMMIT_KEYS),
    ("checkout", "POST", "/api/checkout", {"name": "__main__"}, STATUS_KEYS),
    ("branch",   "POST", "/api/branch",   {"name": "contract/tmp"}, STATUS_KEYS),
    ("cstate",   "GET",  "/api/conflict/state",    None, CONFLICT_STATE_KEYS),
    ("abehind",  "GET",  "/api/remote/ahead-behind", None, AHEAD_BEHIND_KEYS),
)

# 非空守卫: 少一条就是静默跳过, 比判错更险
MIN_CONTRACTS = 8

# --------------------------------------------------------------------------
# 检查器(纯函数, 不碰网络 —— 这样阳性对照才能单独跑)
# --------------------------------------------------------------------------


def check_contract(label, obj, required):
    """返回问题列表。空列表 = 合契约。"""
    if not isinstance(obj, dict):
        return ["%s: 响应不是 JSON 对象, 是 %s" % (label, type(obj).__name__)]
    return [
        "%s: 缺键 %r" % (label, k)
        for k in sorted(required)
        if k not in obj
    ]


class Report(object):
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        print("  [%s] %-42s %s" % ("PASS" if ok else "FAIL", name, detail))
        return bool(ok)

    def note(self, text):
        print("  ....   %s" % text)

    def count(self):
        return (sum(1 for _, ok, _ in self.rows if ok),
                sum(1 for _, ok, _ in self.rows if not ok))


# --------------------------------------------------------------------------
# 自证组: 阳性对照 + 阴性对照 + 非空守卫
# --------------------------------------------------------------------------


def selftest(rep):
    """先证明尺子会响, 再拿它的结论去判别人。"""
    # 阳性对照: 故意缺键, 检查器**必须**抓到
    full = {k: None for k in STATUS_KEYS}
    missing = dict(full)
    del missing["branch"]
    del missing["hasStaged"]
    pos = check_contract("阳性对照", missing, STATUS_KEYS)
    rep.check(
        "自证 阳性对照(缺 2 个键必须被抓)", len(pos) == 2,
        "抓到 %d 条: %s" % (len(pos), "; ".join(pos)),
    )

    # 阳性对照的镜像: 非对象输入也必须被抓(否则「不是 dict」会被当成过)
    pos2 = check_contract("阳性对照2", ["不是", "对象"], STATUS_KEYS)
    rep.check(
        "自证 阳性对照2(响应非对象必须被抓)", len(pos2) == 1,
        "抓到 %d 条: %s" % (len(pos2), pos2[0] if pos2 else ""),
    )

    # 阴性对照: 键齐必须**一条都不报**(否则检查器乱咬, 结论不可信)
    neg = check_contract("阴性对照", dict(full), STATUS_KEYS)
    rep.check(
        "自证 阴性对照(键齐必须放过)", neg == [],
        "抓到 %d 条%s" % (len(neg), (": " + "; ".join(neg)) if neg else ""),
    )

    # commit 契约自身的阴性对照: 多一个 output 才算合格
    neg2 = check_contract("阴性对照2", dict(full), COMMIT_KEYS)
    rep.check(
        "自证 阴性对照2(少 output 必须被抓)", len(neg2) == 1 and "output" in neg2[0],
        "抓到 %d 条: %s" % (len(neg2), "; ".join(neg2)),
    )

    # 新增的两组形状各自也要**单独**证明会响 ——
    # 「共用同一个 check_contract」不等于「共用了同一份验证」。
    for label, keys in (("conflict/state", CONFLICT_STATE_KEYS),
                        ("ahead-behind", AHEAD_BEHIND_KEYS)):
        rep.check("自证 %s 键集非空(防 0 项恒真)" % label, len(keys) > 0,
                  "%d 个键" % len(keys))
        drop = sorted(keys)[0]
        broken = {k: None for k in keys}
        del broken[drop]
        got = check_contract("自证缺键", broken, keys)
        rep.check("自证 %s 缺 %r 必须被抓" % (label, drop),
                  len(got) == 1 and drop in got[0],
                  "抓到 %d 条: %s" % (len(got), "; ".join(got)))

    # 非空守卫: 契约表本身不许是空的(0 项检查恒真)
    rep.check(
        "自证 非空守卫(契约条数 >= %d)" % MIN_CONTRACTS,
        len(CONTRACTS) >= MIN_CONTRACTS,
        "实际 %d 条" % len(CONTRACTS),
    )
    if len(CONTRACTS) < MIN_CONTRACTS:
        raise AssertionError(
            "契约表只剩 %d 条(<%d) —— 静默跳过比判错更险, 直接抛"
            % (len(CONTRACTS), MIN_CONTRACTS)
        )


# --------------------------------------------------------------------------
# 演练仓 + 服务端
# --------------------------------------------------------------------------


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_sandbox():
    """只建一个**空目录**当 root。

    ⚠ 不能自己 `git init` 再让 server 带 `--sandbox` 起: `--sandbox` 会
    `shutil.rmtree(root)`(server.py:2156), 而 git 对象文件在 Windows 上是
    只读的, 删到 `.git\\objects\\**` 就 PermissionError 退出(rc=1)。
    实测过: 空目录 -> rc=0; 已有提交的仓 -> rc=1 + PermissionError。
    建仓这件事交给 `--sandbox` 自己干。
    """
    d = tempfile.mkdtemp(prefix="aigit-contract-")
    root = os.path.join(d, "root")
    os.makedirs(root)
    return d, root


def rmtree(path):
    def onerr(func, p, _e):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
        except OSError:
            pass
        try:
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerr)


def call(port, method, path, body=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, {"__raw__": raw.decode("utf-8", "replace")}


def wait_health(port, proc, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False, "server 提前退出 rc=%s" % proc.returncode
        try:
            st, obj = call(port, "GET", "/api/health", timeout=3)
            if st == 200:
                return True, obj
        except OSError:
            pass
        time.sleep(0.15)
    return False, "等 health 超时 %ds" % timeout


# --------------------------------------------------------------------------
# 真实端点
# --------------------------------------------------------------------------


def live(rep):
    sandbox, repo = make_sandbox()
    port = free_port()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    errlog = open(os.path.join(sandbox, "server.err"), "wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", SERVER, "--root", repo,
         "--port", str(port), "--sandbox"],
        stdout=subprocess.DEVNULL, stderr=errlog, env=env,
    )
    try:
        ok, info = wait_health(port, proc)
        if not ok:
            errlog.flush()
            try:
                tail = open(os.path.join(sandbox, "server.err"),
                            encoding="utf-8", errors="replace").read()[-600:]
            except OSError:
                tail = "(读不到 stderr)"
            rep.check("起服务", False, "%s\n%s" % (info, tail))
            return
        rep.note("演练仓 %s  端口 %d  (server --sandbox 自建, 不碰任何真实仓库)"
                 % (repo, port))
        rep.check("起服务并应答 health", True, "")

        # probe.txt 必须在 server 建完仓**之后**写 —— --sandbox 会先清空 root
        with open(os.path.join(repo, "probe.txt"), "w", encoding="utf-8") as f:
            f.write("line 1\n")

        # 分支名不猜(git init 默认可能是 master 或 main): 从 status 响应里读
        st0, obj0 = call(port, "GET", "/api/status")
        branch = (obj0 or {}).get("branch") or ""
        if not branch:
            rep.note("!! 读不到当前分支名, checkout/branch 两条会跳过(非空守卫会抛)")

        contracts = tuple(
            (lb, m, pth,
             (dict(b, name=branch) if pth == "/api/checkout" else b),
             req)
            for (lb, m, pth, b, req) in CONTRACTS
        )

        for label, method, path, body, required in contracts:
            st, obj = call(port, method, path, body)
            probs = check_contract("%s(%s %s)" % (label, method, path),
                                   obj, required)
            extra = ""
            if isinstance(obj, dict):
                if "output" in obj:
                    extra = "output=%r" % (obj.get("output"),)
                else:
                    extra = "键=%s" % sorted(obj)
            rep.check(
                "%-8s 响应形状" % label,
                st == 200 and not probs,
                ("HTTP %d " % st) + (extra or "") +
                ("" if not probs else "  <<< " + "; ".join(probs)),
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        errlog.close()
        rmtree(sandbox)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only = "--selftest" in argv
    print("=" * 74)
    print("  响应形状契约测试")
    print("=" * 74)
    rep = Report()
    print("\n  ---- 自证组(跑真实端点之前先证明检查器会响)----")
    selftest(rep)
    if only:
        p, f = rep.count()
        print("\n  自证 %d 条: PASS %d  FAIL %d\n" % (p + f, p, f))
        return 1 if f else 0
    print("\n  ---- 真实端点组(演练仓, 不碰任何真实仓库)----")
    live(rep)
    p, f = rep.count()
    print("\n  共 %d 条  PASS %d  FAIL %d\n" % (p + f, p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
