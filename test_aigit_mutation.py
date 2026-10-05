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
]
