# repolock —— 仓库级互斥锁的**唯一**实现(中立模块)。
#
# 为什么单独一个文件: 锁工厂原先长在 conflicts.py 里, 于是只有 conflicts 这条
# 通路按 realpath 共享, server.Repo 与 history_ops.HistoryOps 各自
# `threading.Lock()` 每实例新建 —— 同一个仓上出现 3 把互不相干的锁。
# 搬进中立模块后, 三条通路 import 的是**同一个函数、同一个字典**。
#
# ⚠ 本模块**只准 import os / threading**。两条硬约束:
#   1. aigit 零第三方依赖(test_aigit_net.py D 组实测「没有联网模块」);
#   2. gitops.py 被 test_aigit_net.py c3 禁止 import 任何 aigit 模块 ——
#      而 c3 的 `local` 来自 test_aigit_net.py:774 `_iter_py()`, 也就是
#      **本目录下每一个 .py**。所以只要这个文件躺在 aigit 目录里, 它对
#      gitops 就**不中立**, gitops 那一行只能单开一轮。
#      本模块不 import 任何 aigit 模块 ⇒ 零新 import 环
#      (既有的环: server.py:74 顶层 import conflicts, 而 conflicts.py:208
#       的 import server 必须在函数内 —— 本文件不参与, 不受影响)。
#
# 名字为什么从 `_GIT_LOCKS` 改成 `_REPO_LOCKS`: 这个锁保护的是**整个仓库**的
# 互斥, 不只是 git —— 历史上有过「它只管 git 所以叫 GIT」的说法, 那已经不成立。
# `repo_git_lock` 这个函数名保留不变: 它是对外接口, `conflicts.__all__` 里
# 有它, 且 test_f06_guard.py:403 靠 `conflicts.repo_git_lock` 猴补它。
import os
import threading

# ------------------------------------------------------------------ 仓库级互斥
#
# aigit 有三条会跑 git 的通路: server.Repo、history_ops.HistoryOps、以及
# conflicts。三者在同一个仓库上跑 git ⇒ 同一个 .git/index 会被同时写。
# 实测(见 litematica-preview/doc/aigit-concurrency-audit.md F-01):
# `stage` × `conflict/resolve` 每遍 20–23/72 的请求以
# `Unable to create '.git/index.lock'` 失败。
#
# 这里按 realpath 记忆化: **同一仓库的所有入口拿同一把锁, 不同仓库仍然并行**。
# 用模块级字典而不是实例属性 —— 实例属性会让每个入口各拿一把, 而
# server.py 的 `conflict_resolver()` / `history_ops()` 是懒单例、测试又会另建
# 实例, 用实例属性的话同一个仓上会出现 N 把互不相干的锁, 等于没加。
#
# ⚠ 必须是 **RLock** 不能是 Lock, 而且这条不是「防御性兼容」, 是**既有正确性
#   要求**, 每天都在走。实测(litematica-preview/doc/aigit-lock-graph-20261003.md
#   §1, locknest.py 四段真实负载):
#       server.Repo._lock      取锁 175 次 / 嵌套(层>1) **97** 次 / 最大层数 2
#       conflicts.Git._lock    取锁 275 次 / 嵌套 **103** 次 / 最大层数 2
#       history_ops._lock      取锁 671 次 / 嵌套 **0** 次  / 最大层数 1
#   嵌套的两条来源都是「操作层持锁 → 段内回调命令层, 命令层再取同一把」:
#       server.py:1312 commit() 持 _lock → server.py:1321 self._run(…)
#                 → server.py:507 _run 转发 → server.py:483 _run_full() 再取 _lock
#       conflicts.py:1198 commit_merge() 持 _lock → 段内 self.git.run(…)
#                 → conflicts.py:241 再取同一把
#   换成普通 Lock, 这 97 / 103 次里每一次都会自死锁。
#   ⚠ 但 server.py 的静态扫描器(test_f06_guard.py T6「嵌套取锁边 0 条」与
#     test_aigit_concurrency.py C9 `nested_edges: []`) **看不见**这两条 ——
#     它们只认「持锁方法体里直接写 self.别的持锁方法」, 认不出经 :507 那个
#     薄转发器中转的。所以那两条静态结论是**假阴性**, 不能拿来论证「无嵌套」。
#
# ⚠ 动态可重入性实测: 同线程 acquire 两次, join(2.0) 后仍在跑 = 不可重入。
#   (不可重入的锁会让那个子线程永久卡住, 所以那个线程**不 join 到底**。)
#   实测 repo_git_lock 给的是 RLock = **可重入**, 与上面「必须是 RLock」吻合。

_REPO_LOCKS = {}
_REPO_LOCKS_MU = threading.Lock()


def repo_git_lock(root):
    """返回该仓库唯一的互斥锁。同一 root(按 realpath 归一)永远拿到同一把。

    ⚠ **锁图无环(2026-10-03 按本文件的现状逐句重推, 不是继承 conflicts.py:156
       的旧论证)**:

      1. `_REPO_LOCKS_MU` 在**整个 aigit 仓里只有一个持有点**, 就是本函数体
         里的 `with _REPO_LOCKS_MU:`(即下面第 4 行 `lk = _REPO_LOCKS.get(key)`
         那一段的入口)。实测口径: `grep -n "_REPO_LOCKS_MU" *.py` 的全部命中里,
         除了本文件这 3 处(定义 1 / with 1 / 无), 没有任何一个 `with` 或
         `.acquire()` 消费它 —— T6 的锁点清单(`base_test_f06_guard.out.txt:13`)
         把 `_GIT_LOCKS_MU` 列成 `decl@150` + `hold@160` **两条**, 也就是
         「声明 + 恰好一个持有点」, 与此一致。
      2. 本函数体内**没有任何 git 调用、没有任何 aigit 模块调用**。它只做
         `os.path.normcase` / `os.path.realpath` / 字典 get / `threading.RLock()`。
         —— 这一点是第 3 条的前提, 也是把工厂挪进独立文件之后**必须重新确认**的
         那一条(旧论证成立是因为它长在 conflicts.py 里那段注释旁边)。
      3. 因为持有 MU 的代码路径上不可能去取任何仓库锁, 所以
         「MU → 仓库锁」这条嵌套边**不存在**, 仓库锁与 MU 之间无环。
      4. 反向的「仓库锁 → MU」这条边**存在且无害**: 它是
         `Repo._lock` / `Git._lock` / `HistoryOps._lock` 的持有者调用
         `repo_git_lock(...)` 去取锁时产生的, 而 RLock 允许同线程重入, 不会死锁。

      动态反证(不是静态推断): 猴补 `repo_git_lock`, 在**调用它的那一刻**检查
      `_REPO_LOCKS_MU` 是否正被本线程持有, 跑在 test_conflicts.py 的 14 条真实
      用例上(locksites2.py §2)。实测 **调用 16 次 / 持有中 0 次** ——
      该论证在真实负载下未被推翻。注意: 16 是**样本数**, 不是「覆盖全部路径」的
      证明; 要推翻只需一次在 MU 内取仓库锁的调用, 而第 2 条说明那种调用在代码里
      不存在。
    """
    key = os.path.normcase(os.path.realpath(str(root)))
    with _REPO_LOCKS_MU:
        lk = _REPO_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _REPO_LOCKS[key] = lk
        return lk


__all__ = ["repo_git_lock"]
