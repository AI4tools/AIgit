# -*- coding: utf-8 -*-
"""常驻 `git cat-file --batch`：把「读对象」从「起一个进程」变成「写一行、读一段」。

为什么只做**读对象**：一次 spawn 的固定成本是 ~36ms（`git --version` 实测），而
`git show HEAD:path` 这类命令的本体只要 2–3ms。所以只有「命令的唯一工作就是取一个
对象」的场合才值得常驻，`status`/`diff`/`blame` 那种要 git 自己算的不在此列。

边界（都是**回退**，不是「猜」）：
  * 进程起不来 / 中途死掉 / 协议读不懂 → 返回 None，调用方原样走一次性 git；
  * `.git` 不存在或不是仓 → None；
  * 单个对象超过 `MAX_BYTES` → None（不把内存赌在一个巨大 blob 上）。

线程模型：一个仓一个进程、一把锁。`cat-file --batch` 的请求/响应是一问一答，
两个线程交错写就会把对象读串 —— 这比慢危险得多。
"""

import atexit
import os
import subprocess
import threading
import time

import gitguard

MAX_BYTES = 128 * 1024 * 1024
#: 空闲多久就关掉常驻进程（秒）。0 或负数 = 不回收（测试/排查用）。
#: 为什么要回收：进程按仓持有目录/包文件句柄，Windows 上会让「删仓/搬仓」失败
#: （WinError 32）。回收之后下一次请求会重新起一个进程（~40ms），这是有意的取舍。
IDLE_SECONDS = float(os.environ.get("AIGIT_CATFILE_IDLE") or 60)
#: 回收线程的扫描间隔（秒）。测试可以直接调 reap()，不必等线程。
REAP_INTERVAL = 15.0
_SESS = {}
_SESS_MU = threading.Lock()
_REAPER = None
_REAPER_MU = threading.Lock()


class _Session(object):
    def __init__(self, root):
        self.root = str(root)
        argv = gitguard.for_root(self.root, ["cat-file", "--batch"],
                                 net_enabled=False)
        self.proc = subprocess.Popen(
            argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=gitguard.safe_env(net_enabled=False))
        self.lock = threading.Lock()
        self.dead = False
        self.last = time.time()

    def read(self, spec):
        """→ (bytes, type) | None(取不到) ；进程坏了抛 _Dead 由上层重建。"""
        st, sha, typ, data = self.read_ex(spec)
        if st != "ok":
            return None
        return data, typ

    def read_ex(self, spec):
        """→ (state, 解析到的 sha, type, bytes)；state ∈ ok / missing / ambiguous。

        `ambiguous` 单独返回是给「短 sha 到底要多长才唯一」用的：
        `git cat-file --batch` 对歧义前缀会答 ambiguous，那正是 git 自己判定
        `%h` 要不要加长的依据 —— 复用它比自己扫对象库靠谱。
        """
        line = spec.encode("utf-8") + b"\n"
        with self.lock:
            if self.dead:
                raise _Dead()
            self.last = time.time()
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                head = self.proc.stdout.readline()
                if not head:
                    raise _Dead()
                parts = head.decode("utf-8", "replace").strip().split(" ")
                if len(parts) == 2 and parts[1] in ("missing", "ambiguous"):
                    return ("missing" if parts[1] == "missing" else "ambiguous",
                            None, None, None)
                if len(parts) != 3:
                    raise _Dead()
                size = int(parts[2])
                if size < 0 or size > MAX_BYTES:
                    # 先把字节读掉，否则下一条会错位
                    left = size
                    while left > 0:
                        chunk = self.proc.stdout.read(min(65536, left))
                        if not chunk:
                            raise _Dead()
                        left -= len(chunk)
                    self.proc.stdout.read(1)
                    return ("missing", None, None, None)
                data = b""
                left = size
                while left > 0:
                    chunk = self.proc.stdout.read(min(65536, left))
                    if not chunk:
                        raise _Dead()
                    data += chunk
                    left -= len(chunk)
                self.proc.stdout.read(1)          # 结尾换行
                return ("ok", parts[0], parts[1], data)
            except (OSError, ValueError):
                raise _Dead()

    def close(self):
        self.dead = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class _Dead(Exception):
    pass


def _get(root):
    key = os.path.normcase(os.path.abspath(str(root)))
    with _SESS_MU:
        s = _SESS.get(key)
        if s is None or s.dead:
            if s is not None:
                s.close()
            s = _Session(root)
            _SESS[key] = s
    start_reaper()
    return s


def read(root, spec):
    """读一个对象。成功 → (bytes, type)；任何不确定 → None（调用方回退）。"""
    got = read_ex(root, spec)
    if got[0] != "ok":
        return None
    return got[3], got[2]


def read_ex(root, spec):
    """→ (state, sha, type, bytes)；任何不确定 → ("missing", None, None, None)。

    state 三态（ok / missing / ambiguous）是公开契约：`%h` 的唯一性判定要用
    ambiguous，不能和「对象不存在」混为一谈。
    """
    if not isinstance(spec, str) or not spec or "\x00" in spec or "\n" in spec:
        return ("missing", None, None, None)
    if not os.path.isdir(os.path.join(str(root), ".git")):
        return ("missing", None, None, None)
    try:
        s = _get(root)
        return s.read_ex(spec)
    except _Dead:
        # 死一次就重建一次；再失败就回退（不无限重试）
        try:
            with _SESS_MU:
                old = _SESS.pop(os.path.normcase(os.path.abspath(str(root))), None)
            if old is not None:
                old.close()
            return _get(root).read_ex(spec)
        except Exception:
            return ("missing", None, None, None)
    except Exception:
        return ("missing", None, None, None)


def reap(now=None):
    """回收空闲超过 `IDLE_SECONDS` 的会话（一次扫描）。返回关掉的会话数。

    只在**拿到会话锁**时才关：拿不到说明有人正在用它，跳过这一轮 ——
    绝不能把一个正在读响应的进程 terminate 掉。
    """
    if IDLE_SECONDS is None or IDLE_SECONDS <= 0:
        return 0
    now = time.time() if now is None else now
    with _SESS_MU:
        keys = list(_SESS.keys())
    closed = 0
    for key in keys:
        with _SESS_MU:
            s = _SESS.get(key)
        if s is None:
            continue
        if s.lock.acquire(blocking=False):
            try:
                if time.time() - s.last > IDLE_SECONDS:
                    with _SESS_MU:
                        _SESS.pop(key, None)
                    s.close()
                    closed += 1
            finally:
                try:
                    s.lock.release()
                except RuntimeError:
                    pass
    return closed


def _reaper_loop():
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            reap()
        except Exception:
            pass


def start_reaper():
    """惰性启动回收线程（daemon，不影响进程退出）。"""
    global _REAPER
    if IDLE_SECONDS is None or IDLE_SECONDS <= 0:
        return
    with _REAPER_MU:
        if _REAPER is not None and _REAPER.is_alive():
            return
        _REAPER = threading.Thread(target=_reaper_loop, name="aigit-catfile-reaper",
                                   daemon=True)
        _REAPER.start()


def close_for(root):
    """关掉某个仓的会话（删仓/搬仓前调用）。返回是否关掉了一个。"""
    key = os.path.normcase(os.path.abspath(str(root)))
    with _SESS_MU:
        s = _SESS.pop(key, None)
    if s is None:
        return False
    s.close()
    return True


def close_all():
    with _SESS_MU:
        for s in list(_SESS.values()):
            s.close()
        _SESS.clear()


atexit.register(close_all)
