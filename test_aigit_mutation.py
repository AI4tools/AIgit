"""test_aigit_mutation —— aigit 变异体目录(被 mutation_runner 加载)。

每条变异: 在 **--base-ref 指定的基线快照**的文件副本里做一次最小修改,
用配套探针自证它真改变了被测行为(LANDED),否则该条作废。

编辑锚点用 (anchor 唯一子串, expect 子串, new) 三元组 —— **没有行号**:

* anchor 必须在目标文件里**恰好命中 1 行**。0 处 -> GONE,多于 1 处 ->
  AMBIGUOUS(硬失败,绝不取第一个)。
* expect 是「找对行了吗」的二次确认:唯一命中行必须含它。
* 改完再验:该行必须含 new 的字面量。

为什么彻底删掉行号: 行号锚点会**静默过期**。2026-10-02 的 15 条里有 6 条
的行号在基线上已经位移 +1 ~ +55,而引擎只会报 DRIFT —— 更糟的是,只要
``expect`` 子串碰巧还在,改坏的地方和想改的地方就可能不是同一处。
「当前是什么」必须由**内容**决定,不由行号决定。
"""

from mutation_runner import Mutation, Edit

CATALOG = [
    # ---------- 安全护栏(safety) ----------
    Mutation(
        id="S-commit-leak-guard-off",
        target="server.py",
        edits=[Edit("leaks = self.escaping_links()",
                    "leaks = self.escaping_links()",
                    "leaks = []  # MUTATED")],
        probe="commit_leak",
        guard_class="safety",
        why="commit() 的逃逸链接闸整个不查:git add -A 会把仓外内容抄进历史",
    ),
    Mutation(
        id="S-discard-dir-guard-off",
        target="server.py",
        # 跨行锚点。`if inner:` 在 server.py 里命中 2 处 —— 一处是 discard()
        # 真正 raise GitError 的拦截,一处是预览时往 blocked 里塞条目的那份
        # 近乎逐字重复的代码。要改的是**拦截**那处,所以锚点带上后一行
        # `raise GitError(` 消歧(约定:窗口第一行 = 被改行)。缩进由
        # locate_edit 归一化掉,重新缩进不会失效。
        edits=[Edit('if inner:\n'
                    'raise GitError(',
                    "if inner:",
                    "if False:")],
        probe="discard_inner_tracked",
        guard_class="safety",
        why="discard(目录)时不再拒绝含已跟踪文件的目录,整目录被 rmtree",
    ),
    Mutation(
        id="S-discard-pin-plain-off",
        target="server.py",
        # 跨行锚点(2026-10-04 修): 48a0c94 起 server.py 里 'if p.reparse():'
        # 有 2 处 —— 本条的 _assert_pinned_plain, 和 stage_everything_safely 的
        # 另一道。带上后两行上下文消歧, 把目标钉在 _assert_pinned_plain。
        edits=[Edit('if p.reparse():\n'
                    'raise GitError(\n'
                    '"路径上有目录联接/符号链接,已拒绝动手:%s"',
                    "if p.reparse():",
                    "if False:")],
        probe="assert_pinned_plain",
        guard_class="safety",
        why="_assert_pinned_plain 判联接的闸整个关掉:父目录可被偷换成联接",
        # 2026-10-03: 这条曾实测 SURVIVED —— 变异确实改了行为(raised 1->0),
        # 但 4 个老套件 146 条用例**一条都没红**。新加的护栏不变量套件
        # 专测这个守卫。只加给这 2 条 SURVIVED, 不动别的, 便于归因。
        suites=["test_aigit.py", "test_aigit_extra.py", "smoke.py",
                "test_aigit_web.py", "test_aigit_invariants.py"],
    ),
    Mutation(
        id="S-checkout-branchexists-off",
        target="server.py",
        # 2026-10-04 修: render_new() 做的是**整行替换**, 而目标行是多行
        # self._run_full([...]) 的第 2 行; 旧 new 只有片段, 会把整行替成
        # 'check=False).returncode == 0:' -> SyntaxError(MUTANT-UNLOADABLE)。
        # 同时 48a0c94 起该片段在 server.py 有 2 处(checkout 的分支存在性闸 /
        # _index_delta 的 HEAD 存在性), 故锚点用整行 + 下一行消歧。
        edits=[Edit('"refs/heads/" + name], check=False).returncode != 0:\n'
                    'raise GitError("分支不存在,已拒绝 checkout:%s"',
                    "check=False).returncode != 0:",
                    '"refs/heads/" + name], check=False).returncode == 0:')],
        probe="checkout_exists",
        guard_class="safety",
        why="checkout 的「必须是已存在的本地分支」闸:短 sha 会把仓切到游离 HEAD",
    ),
    Mutation(
        id="S-ref-colon-inject",
        target="server.py",
        edits=[Edit(r'_REF_RE = re.compile(r"[\w@^~][\w@^~{}+./-]*")',
                    r'_REF_RE = re.compile(r"[\w@^~][\w@^~{}+./-]*")',
                    r'_REF_RE = re.compile(r"[\w@^~-][\w@^~{}+./:-]*")')],
        probe="ref_whitelist",
        guard_class="safety",
        why="_REF_RE 放行冒号和前导 -:'--format=%H'/'HEAD:x' 重新打进 git show",
    ),
    Mutation(
        id="S-resolve-rootself-off",
        target="server.py",
        edits=[Edit("if p == self.root:",
                    "if p == self.root:",
                    "if False:")],
        probe="resolve_root_self",
        guard_class="safety",
        why="resolve_in_root 不拒根目录自己:discard('.')=删整个仓",
    ),
    Mutation(
        id="S-resolve-gitdir-off",
        target="server.py",
        # 2026-10-04 修: 旧 new 只有片段, 整行替换后 server.py:594 变成
        # '        .lower() == ".git-never":' (丢了 if ... parts[0] 前缀)
        # -> SyntaxError(MUTANT-UNLOADABLE)。补全整行。
        edits=[Edit('if p.relative_to(self.root).parts[0].lower() == ".git":',
                    '.lower() == ".git":',
                    'if p.relative_to(self.root).parts[0].lower() == ".git-never":')],
        probe="resolve_git",
        guard_class="safety",
        why=".git 目录保护失效:discard('.git') 直删版本库",
    ),
    Mutation(
        id="S-api-file-dir-e23-off",
        target="server.py",
        edits=[Edit("if ap.is_dir():",
                    "if ap.is_dir():",
                    "if False:")],
        probe="api_file_dir_guard",
        guard_class="boundary",
        why="E23 回归:/api/file?file=<目录> 返回 tree 清单当 old 字段",
        suites=["test_aigit_extra.py"],
    ),
    Mutation(
        id="S-branch-seed-prefix-off",
        target="server.py",
        edits=[Edit('_gen.BRANCH_PREFIX = "%s/s%s" % (_GEN_BASE_PREFIX, seed)',
                    '_gen.BRANCH_PREFIX = "%s/s%s" % (_GEN_BASE_PREFIX, seed)',
                    "_gen.BRANCH_PREFIX = _GEN_BASE_PREFIX")],
        probe="gen_seed_prefix",
        guard_class="logic",
        why="D8 回归:分支名不带 seed 前缀,同仓连续生成撞分支名",
        suites=["test_aigit.py"],
    ),
    Mutation(
        id="H-validate-refname-slash-off",
        target="history_ops.py",
        edits=[Edit('_need(not name.startswith("/") and not name.startswith("."),',
                    '_need(not name.startswith("/") and not name.startswith("."),',
                    "_need(True,  # MUTATED")],
        probe="hist_ref_whitelist",
        guard_class="safety",
        why="分支名白名单放行前导 / 和 .:'/abs' 被当有效名字",
    ),
    Mutation(
        id="H-require-confirm-off",
        target="history_ops.py",
        edits=[Edit("_need(confirmed is True,",
                    "_need(confirmed is True,",
                    "_need(True,  # MUTATED")],
        probe="hist_require_confirm",
        guard_class="safety",
        why="所有破坏性 do_* 不再强制 confirmed=True,误操作无二次确认",
    ),
    Mutation(
        id="G-assert-prefix-off",
        target="gen.py",
        edits=[Edit("if not rel.startswith(PREFIX):",
                    "if not rel.startswith(PREFIX):",
                    "if False:")],
        probe="gen_prefix",
        guard_class="safety",
        why="ScenarioGen 不再要求落盘路径带 aigit_gen_ 前缀,"
            "演练仓会直接写用户文件",
        # 2026-10-03: 这条曾实测 SURVIVED —— 变异确实改了行为(raised 2->0),
        # 但 2 个老套件 67 条用例**一条都没红**。同上, 只加给这 2 条。
        suites=["selftest.py", "test_aigit.py", "test_aigit_invariants.py"],
    ),
    Mutation(
        id="C-abs-dotdot-off",
        target="conflicts.py",
        edits=[Edit('if ".." in Path(rel).parts:',
                    'if ".." in Path(rel).parts:',
                    "if False:")],
        probe="conf_resolve_dotdot",
        guard_class="safety",
        why="ConflictResolver 路径放行 ..:冲突处理写出仓根",
    ),
    Mutation(
        id="C-abs-gitdir-off",
        target="conflicts.py",
        edits=[Edit('gitdir = (self.git.root / ".git").resolve()',
                    'gitdir = (self.git.root / ".git").resolve()',
                    "gitdir = None  # MUTATED")],
        probe="conf_resolve_git",
        guard_class="safety",
        why="ConflictResolver 不再拦 .git 路径",
    ),
    Mutation(
        id="C-merge-add-scope-off",
        target="conflicts.py",
        # commit_merge 的**第五道**防线: 作用域受限的显式 `add -- <scope>`。
        # 退回修复前的裸 `add -A`, 未跟踪的目录联接内容就会被抄进历史。
        edits=[Edit('self.git.run(["add", "--"] + scope)',
                    'self.git.run(["add", "--"] + scope)',
                    'self.git.run(["add", "-A"])')],
        probe="conf_merge_add_scope",
        guard_class="safety",
        why="commit_merge 第五道退回裸 add -A: 未跟踪的联接内容被抄进历史",
        # ⚠ 必须显式带上 test_f06_guard.py: SUITES_BY_TARGET["conflicts.py"]
        #   只有 test_conflicts.py, 而这条防线正是 T3 在测。
        #   (缺这条时引擎直接 KeyError -> ENGINE-ERROR, 2026-10-06 实测过。)
        # ⚠ 而且 **T3 必须是修过的那一版**(工作区那份, 未提交)。
        #   已提交的旧 T3 断言 `ck(lp and lc, "防线关掉后没有泄漏")` ——
        #   它**期望**泄漏, 所以这条变异在旧 T3 下会 SURVIVED。
        #   要复现杀灭必须带 `--overlay-test test_f06_guard.py`。
        #   2026-10-06 两组实测(各 --workers 1):
        #     不带 overlay : SURVIVED, 有效 1 / 杀 0 / 存 1 = 0.0%
        #     带 overlay   : KILLED,   有效 1 / 杀 1 / 存 0 = 100.0%
        #   杀它的断言原文:
        #     FAIL T3 第四道已拆, 仓外路径却还是进了索引或进了历史 ——
        #     **第五道(作用域受限 add)没有独立挡住**, (leaked_path=True leaked_content=True)
        suites=["test_conflicts.py", "test_f06_guard.py"],
    ),
    # ---------- 响应形状(contract) ----------
    # 这两条针对 find_test_gaps.py 2026-10-06 报出的「零提及」键:
    # 形状契约测试钉住了它们, 但**只有静态断言, 没有变异体**。
    # 补上变异体才算行为化证明「删键真的会被咬住」。
    Mutation(
        id="C-conf-state-key-off",
        target="conflicts.py",
        # merge_state() 的 11 个键里, currentBranch / origHead / mergeMsgPath
        # 原先没有任何套件断言过。把 currentBranch 改名 => 该键从响应里消失。
        edits=[Edit('"currentBranch": current,',
                    '"currentBranch":',
                    '"_mutated_away": current,')],
        probe="conf_state_keys",
        guard_class="logic",
        why="merge_state 丢掉 currentBranch 键(响应形状回归, 前端/调用方读不到并入方分支)",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="S-ab-trackingref-off",
        target="gitops.py",
        # ahead_behind 的「没有远程跟踪 ref」分支: 删掉 trackingRefExists。
        # 这个键是 behind=None 的**消歧义信号** —— 没有它, behind 的语义
        # 只能靠猜(见 gitops.py 那段 docstring 记的坑)。
        edits=[Edit('"trackingRefExists": False, "ahead": n, "behind": None,',
                    '"trackingRefExists": False',
                    '"ahead": n, "behind": None,')],
        probe="ab_trackingref",
        guard_class="logic",
        why="ahead_behind 丢掉 trackingRefExists 键(behind=None 的消歧义信号没了)",
        suites=["test_aigit_net.py"],
    ),
    # ---------- 过期工作区守卫(stale-worktree guards) ----------
    # 这三条是**同一类**危害的三个独立实例: 客户端拿着读到的 sha256 回来动手,
    # 但工作区已经被别人改过。守卫拒绝动手 = 不静默覆盖/删除用户没见过的内容。
    # 关掉它们的危害是**真的丢数据**, 不是「报错信息变差」—— 与下面那条
    # H-stash-clean 不同(那条关掉只是报错变差, git 自己还会拒, 所以没选它)。
    Mutation(
        id="C-del-stale-worktree-off",
        target="conflicts.py",
        # 锚点带上**独有的**那行消息: '工作区已被改动,拒绝删除' 在 conflicts.py
        # 里只有这一处(另两条是 '拒绝覆盖' 和 '无需「取消解决」')。
        # expect 是**改之前**那行的内容(二次确认找对了行), new 是**替换文本**
        # (render_new 自己补缩进)。所以 expect 写原条件, new 写 if False:。
        edits=[Edit('if hashlib.sha256(cur).hexdigest() != expect_sha256:\n'
                    '                raise ConflictError("工作区已被改动,拒绝删除。请重新读取:%s" % rel)',
                    "if hashlib.sha256(cur).hexdigest() != expect_sha256:",
                    "if False:")],
        probe="conf_del_stale",
        guard_class="safety",
        why="delete_file 不再比对 sha256: 客户端状态过期时会**删掉它没见过的内容**",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="C-unresolve-notresolved-off",
        target="conflicts.py",
        # unresolve 只对**已解决**的路径有意义。对还在冲突态的路径调它,
        # 下面那步 `git checkout --merge` 会把用户刚解决好的内容冲掉。
        edits=[Edit('        if self._stages(rel):\n'
                    '            raise ConflictError("该路径还没解决,无需「取消解决」:%s" % rel)',
                    "if self._stages(rel):",
                    "if False:")],
        probe="conf_unresolve_notresolved",
        guard_class="safety",
        why="unresolve 不再拒「还没解决的路径」: 会把用户刚解决好的内容冲掉",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="G-no-main-branch-off",
        target="gen.py",
        # gen 要求仓里有 main 分支当**场景基准点**。关掉它 = 在一个没有
        # 基准分支的仓上生成场景, 产出的分支/文件全是悬空的。
        edits=[Edit('        if not self.git.branch_exists("main"):\n'
                    '            raise GenError("仓里没有 main 分支,无法作为场景基准点")',
                    'if not self.git.branch_exists("main"):',
                    "if False:")],
        probe="gen_no_main",
        guard_class="safety",
        why="gen 不再要求 main 分支: 会在没有基准点的仓上生成场景",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="G-dirty-worktree-off",
        target="gen.py",
        # `_check_clean` 是 gen 最要紧的一道闸: 在别人的工作区上叠随机内容,
        # 等于把别人的活搅成一团, 事后还分不清哪些改动是谁造的。
        # 2026-10-06 实测: 全仓测试与变异体对 `_check_clean` **零提及**。
        edits=[Edit('if dirty:\n            lines = dirty.splitlines()',
                    "if dirty:",
                    "if False:")],
        probe="gen_dirty",
        guard_class="safety",
        why="_check_clean 不再拒脏工作区: 会往用户未提交的改动里叠随机内容",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="G-branch-clash-off",
        target="gen.py",
        # 分支已存在就拒绝, 而不是覆盖。关掉它 = 反复 generate 会互相踩,
        # 半途 git 报错留下一个生成到一半的仓。
        edits=[Edit('if clash:\n            raise GenError(',
                    "if clash:",
                    "if False:")],
        probe="gen_branch_clash",
        guard_class="safety",
        why="gen 不再拒同名分支: 反复生成会互相踩, 留下半成品仓",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="C-guard-unavailable-off",
        target="conflicts.py",
        # 拿不到仓外联接防线时**宁可拒绝**提交。关掉它, ImportError 会裸奔,
        # 调用方按 ConflictError 判「防不住 -> 拒绝」的逻辑就接不住了。
        # L203 带注释、L996 不带, 所以锚点必须带上那行注释消歧。
        edits=[Edit('except Exception as e: # ImportError / 别的线把 server.py 改坏了\nraise ConflictError(',
                    "except Exception as e:",
                    "except ZeroDivisionError as e:")],
        probe="conf_guard_unavailable",
        guard_class="safety",
        why="拿不到仓外联接防线时不再转成 ConflictError: 拒绝信号裸奔成 ImportError",
        suites=["test_conflicts.py"],
    ),
    Mutation(
        id="C-merge-unresolved-off",
        target="conflicts.py",
        # 还有未解决文件时**拒绝**完成合并。放行 = 把半解决的冲突直接提交,
        # 冲突标记被当成正常内容写进历史。
        edits=[Edit('if remaining["count"]:\n            raise ConflictError(',
                    'if remaining["count"]:',
                    "if False:")],
        probe="conf_merge_unresolved",
        guard_class="safety",
        why="commit_merge 不再拒未解决文件: 冲突标记会被当成内容提交",
        suites=["test_conflicts.py", "test_f06_guard.py"],
    ),
    # ---------- plan 预演的响应形状(2026-10-21) ----------
    # find_test_gaps 扩到 plan 端点后报出 8 个零提及键。这三条是其中
    # 危害最实的: 客户端靠它们判断「能不能推 / 推什么 / 是不是已发布」。
    Mutation(
        id="H-amend-published-off",
        target="history_ops.py",
        # published=False 时客户端不知道「这条提交还在本地」, 会以为
        # amend 之后不用提醒用户「已推送的提交会被改写」。
        edits=[Edit('return {"published": bool(remotes), "remotes": remotes,',
                    '"published": bool(remotes)',
                    'return {"remotes": remotes,')],
        probe="amend_published",
        guard_class="logic",
        why="plan_amend 丢掉 published: 客户端看不出这条提交是否已推送",
        suites=["test_history_ops.py"],
    ),
    Mutation(
        id="G-fetch-aheadbehind-off",
        target="gitops.py",
        edits=[Edit('"aheadBehind": ab}',
                    '"aheadBehind": ab',
                    "}")],
        probe="fetch_aheadBehind",
        guard_class="logic",
        why="plan_fetch 丢掉 aheadBehind: 预演里看不出落后/领先多少",
        suites=["test_aigit_net.py"],
    ),
    Mutation(
        id="G-push-revrange-off",
        target="gitops.py",
        edits=[Edit('"revRange": rng, "commits": commits, "count": len(commits),',
                    '"revRange": rng',
                    '"commits": commits, "count": len(commits),')],
        probe="push_revRange",
        guard_class="logic",
        why="plan_push 丢掉 revRange: 预演里看不出要推哪个区间",
        suites=["test_aigit_net.py"],
    ),
    # ---------- 前端(ui) ----------
    Mutation(
        id="U-esc-quote-off",
        target="web/index.html",
        edits=[Edit('.replace(/"/g, "&quot;")',
                    '.replace(/"/g, "&quot;")',
                    "")],
        probe="esc_index",
        guard_class="ui",
        why="index.html 的 esc() 不再转双引号:属性注入 XSS",
        suites=["test_aigit_web.py"],
    ),
    # ---------- 快路径/回退边界(2026-10-22; 2026-10-27 解析器下沉到 gitrefs) ----------
    Mutation(
        id="S-idx-fast-extgate-off",
        target="gitrefs.py",
        edits=[Edit("if sig in _UNSAFE_INDEX_EXT:",
                    "_UNSAFE_INDEX_EXT",
                    "if False:")],
        probe="index_fast_extgate",
        guard_class="safety",
        why="索引直读不再拒绝 split-index/sparse-index: 只读到主索引里的部分条目, "
            "stage/unstage 的过滤会漏掉大量已跟踪路径",
        suites=["test_index_fastpath.py", "test_refstore.py"],
    ),
    Mutation(
        id="S-idx-fast-firstentry-only",
        target="gitrefs.py",
        edits=[Edit("for _ in range(count):",
                    "range(count)",
                    "for _ in range(1):")],
        probe="index_fast_count",
        guard_class="safety",
        why="索引直读只读第一条条目: 已跟踪集合变成「几乎全空」, 已跟踪但工作区"
            "已删的路径会被过滤掉(那次删除就不进提交)",
        suites=["test_index_fastpath.py", "test_refstore.py"],
    ),
    Mutation(
        id="S-branch-ref-loose-off",
        target="server.py",
        edits=[Edit("return looks_like_ref(p.read_text(encoding=\"utf-8\", errors=\"strict\")) or None",
                    "looks_like_ref",
                    "return None")],
        probe="branch_ref_loose",
        guard_class="safety",
        why="loose ref 直读失效: checkout 快路径永远回退(功能不变但多付一次 spawn; "
            "这条钉的是快路径本身还在生效)",
        suites=["test_index_fastpath.py"],
    ),
    Mutation(
        id="S-refs-packed-ignored",
        target="gitrefs.py",
        edits=[Edit("out[parts[1]] = parts[0]",
                    "parts[1]",
                    "pass")],
        probe="refs_packed_names",
        guard_class="logic",
        why="packed-refs 被整段忽略: 所有被 pack 过的分支从 /api/branches 里消失"
            "(显示层错, 不是数据错)",
        suites=["test_refstore.py"],
    ),
    Mutation(
        id="S-refs-headmark-off",
        target="server.py",
        edits=[Edit('cur = state[1] if state[0] == "branch" else None',
                    "state[0]",
                    "cur = None")],
        probe="refs_head_mark",
        guard_class="logic",
        why="当前分支标记永远是 false: 面板上看不出自己在哪条分支上",
        suites=["test_refstore.py"],
    ),
    Mutation(
        id="S-stash-order-off",
        target="history_ops.py",
        edits=[Edit("for i, (_sha, msg) in enumerate(entries[::-1][:n]))",
                    "entries[::-1]",
                    "for i, (_sha, msg) in enumerate(entries[:n]))")],
        probe="stash_order",
        guard_class="logic",
        why="stash 列表按旧→新输出: stash@{0} 指向最老的那条, pop/drop 会打错目标",
        suites=["test_refstore.py", "test_history_ops.py"],
    ),
    Mutation(
        id="C-unmerged-always-empty",
        target="gitrefs.py",
        edits=[Edit("return sorted(got[1])",
                    "sorted(got[1])",
                    "return []")],
        probe="unmerged_list",
        guard_class="safety",
        why="未合并清单永远为空: 真冲突时 /api/conflict/list 报 count=0, "
            "面板显示「没有冲突」而工作区其实卡在冲突里",
        suites=["test_refstore.py", "test_conflicts.py"],
    ),
    # ---------- 写端点输入边界(2026-10-27 巡检) ----------
    Mutation(
        id="S-path-nul-not-rejected",
        target="server.py",
        edits=[Edit('if "\\x00" in rel:', 'in rel', "if False:")],
        probe="path_nul_guard",
        guard_class="safety",
        why="路径里的 NUL 不再被拒: Windows 上 Path 会静默截断, "
            "unstage/discard 会去动**另一个文件**(discard 即数据丢失)",
        suites=["test_write_inputs.py"],
    ),
    Mutation(
        id="C-commit-nul-msg-off",
        target="server.py",
        edits=[Edit('if "\\x00" in message:', 'in message', "if False:")],
        probe="commit_nul_msg",
        guard_class="safety",
        why="commit 的 NUL 消息校验被去掉: 文件先被暂存, 然后 git commit 才失败, "
            "请求 400 但索引已经改了(拒绝带前半截副作用)",
        suites=["test_write_inputs.py"],
    ),
    Mutation(
        id="H-msg-type-not-checked",
        target="history_ops.py",
        edits=[Edit('_need(isinstance(value, str), "%s必须是字符串,收到 %s"',
                    "isinstance(value, str)",
                    '_need(True, "%s必须是字符串,收到 %s"')],
        probe="msg_type_guard",
        guard_class="boundary",
        why="消息类参数不再校验类型: message=['x'] 会被 %s 静默拼成 \"['x']\", "
            "打出一个内容莫名的 stash/amend/tag",
        suites=["test_write_inputs.py"],
    ),
    Mutation(
        id="H-reflog-index-type-off",
        target="history_ops.py",
        edits=[Edit('_need(isinstance(index, int) and not isinstance(index, bool),\n'
                    '              "reflog 索引必须是整数,收到 %r" % (index,), code="bad_index")',
                    "isinstance(index, int)",
                    "_need(not isinstance(index, bool),")],
        probe="reflog_index_type",
        guard_class="boundary",
        why="reflog 索引不再校验整数: index=None 时先抛 TypeError, "
            "响应里泄漏 Python 原文(\"%d format: ... not NoneType\")",
        suites=["test_write_inputs.py"],
    ),
    Mutation(
        id="S-catfile-type-lie",
        target="gitcat.py",
        edits=[Edit('return ("ok", parts[0], parts[1], data)',
                    "parts[1]",
                    'return ("ok", parts[0], "blob", data)')],
        probe="catfile_type",
        guard_class="safety",
        why="cat-file 的返回值里把类型一律报成 blob: 目录(tree)会被当成文件内容, "
            "左右分栏的「旧版」显示的是一棵 tree 清单的字节",
        suites=["test_object_read.py"],
    ),
    # ---------- tags/reflog 的快路径(2026-10-27) ----------
    Mutation(
        id="S-tag-subject-join-off",
        target="gitobjects.py",
        edits=[Edit('return " ".join(out)',
                    '" ".join(out)',
                    'return "".join(out)')],
        probe="tag_subject_rule",
        guard_class="logic",
        why="标签 subject 的行间不再补空格: 多行 tag 消息被拼成一串, "
            "与 git 的 %(contents:subject) 不一致",
        suites=["test_history_fast.py"],
    ),
    Mutation(
        id="S-reflog-order-off",
        target="history_ops.py",
        edits=[Edit("for i, (sha, msg) in enumerate(ents[::-1][:n]):",
                    "ents[::-1][:n]",
                    "for i, (sha, msg) in enumerate(ents[:n]):")],
        probe="reflog_order",
        guard_class="logic",
        why="reflog 按旧→新输出: HEAD@{0} 指向最老那条, 恢复/回滚会选错目标",
        suites=["test_history_fast.py"],
    ),
    Mutation(
        id="S-abbrev-minlen-off",
        target="history_ops.py",
        edits=[Edit('start = mode if isinstance(mode, int) else 7',
                    "else 7",
                    "start = mode if isinstance(mode, int) else 4")],
        probe="abbrev_minlen",
        guard_class="logic",
        why="%h 的最小长度从 7 降到 4: 显示出来的短 sha 比 git 自己给的短, "
            "复制出去可能撞别的对象",
        suites=["test_history_fast.py"],
    ),
    Mutation(
        id="S-tags-sort-off",
        target="history_ops.py",
        edits=[Edit('rows.sort(key=lambda r: -r["_ts"])',
                    '-r["_ts"]',
                    'rows.sort(key=lambda r: r["_ts"])')],
        probe="tags_sort",
        guard_class="logic",
        why="标签按 creatordate **升序**输出: 最新的标签排到最后, 与 --sort=-creatordate 相反",
        suites=["test_history_fast.py"],
    ),
    Mutation(
        id="S-semantic-config-dropped",
        target="server.py",
        edits=[Edit('env = gitguard.apply_semantic(gitguard.safe_env(net_enabled=self._net_enabled()), self.root)',
                    "apply_semantic",
                    'env = gitguard.safe_env(net_enabled=self._net_enabled())')],
        probe="semantic_autocrlf",
        guard_class="safety",
        why="不再透传用户视角的 core.* 语义配置: autocrlf=true 的仓里 CRLF 工作区会被"
            "报成「已修改」(phantom 改动), 且提交会把 CRLF 写进对象库",
        suites=["test_vs_git.py", "test_write_vs_git.py"],
    ),
    # ---------- 契约收口(2026-11-02) ----------
    Mutation(
        id="S-log-limit-clamp-off",
        target="server.py",
        edits=[Edit("limit = max(1, min(limit, 1000))",
                    "max(1, min(limit, 1000))",
                    "limit = limit")],
        probe="log_limit_clamp",
        guard_class="boundary",
        why="log 的 limit 不再夹取: limit=0/-1 会被拼成 `git log -0`/`--1`, 结果是"
            "**静默空**(与同族 blame 的夹取口径不一致)",
        suites=["test_api_contract.py"],
    ),
    Mutation(
        id="S-diff-path-known-off",
        target="server.py",
        edits=[Edit("return self.show_file(rel) is not None",
                    "show_file(rel)",
                    "return True")],
        probe="diff_path_known",
        guard_class="boundary",
        why="path_known 恒为真: 从未存在过的路径也返回 200 + 空 diff, "
            "与「工作区删除」混成同一种响应",
        suites=["test_api_contract.py"],
    ),
    Mutation(
        id="S-tags-shape-key-off",
        target="server.py",
        edits=[Edit('return self._json({"ok": True, "tags": history_ops().list_tags(',
                    '"tags"',
                    'return self._json({"ok": True, "tag_list": history_ops().list_tags(')],
        probe="tags_shape",
        guard_class="boundary",
        why="tags 响应的键名漂移: 前端 asArray 找不到 tags, 标签页变成空列表",
        suites=["test_api_contract.py"],
    ),
    Mutation(
        id="C-commit-rc-ignored",
        target="server.py",
        edits=[Edit('if p.returncode != 0:\n'
                    '                if "nothing to commit" in text or "no changes added to commit" in text:',
                    "p.returncode",
                    "if False:")],
        probe="commit_rc_guard",
        guard_class="safety",
        why="提交不再看 returncode: `git commit` 失败时把 'nothing to commit' 打到"
            " stdout, 于是**没提交却回 200 + ok:true**(命令返回成功、事情没发生)",
        suites=["test_api_contract.py"],
    ),
    Mutation(
        id="S-status-optout-ignored",
        target="server.py",
        edits=[Edit('return self._json({"ok": True, **(extra or {}), "statusOmitted": True}, 200)',
                    "statusOmitted",
                    "pass")],
        probe="status_optout",
        guard_class="logic",
        why="`status:false` 被忽略: 客户端明确说不要刷新后状态, 服务端还是多花一次"
            " spawn 去取(而且响应形状与它要求的相反)",
        suites=["test_api_contract.py"],
    ),
]
