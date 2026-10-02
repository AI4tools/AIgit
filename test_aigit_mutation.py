"""test_aigit_mutation —— aigit 变异体目录(被 mutation_runner 加载)。

每条变异: 在**已提交基线 f99c10a**的文件副本里做一次最小修改,
用配套探针自证它真改变了被测行为(LANDED),否则该条作废。

编辑锚点用 (lineno, expect子串, new) 三元组: 行号对不上或该行不含
expect 子串就报 DRIFT 不动笔 —— 防止被别的线挪了行还盲改。
"""

from mutation_runner import Mutation, Edit

CATALOG = [
    # ---------- 安全护栏(safety) ----------
    Mutation(
        id="S-commit-leak-guard-off",
        target="server.py",
        edits=[Edit(854, "leaks = self.escaping_links()",
                    "leaks = []  # MUTATED")],
        probe="commit_leak",
        guard_class="safety",
        why="commit() 的逃逸链接闸整个不查:git add -A 会把仓外内容抄进历史",
    ),
    Mutation(
        id="S-discard-dir-guard-off",
        target="server.py",
        edits=[Edit(770, "if inner:", "if False:")],
        probe="discard_inner_tracked",
        guard_class="safety",
        why="discard(目录)时不再拒绝含已跟踪文件的目录,整目录被 rmtree",
    ),
    Mutation(
        id="S-discard-pin-plain-off",
        target="server.py",
        edits=[Edit(683, "if p.reparse():", "if False:")],
        probe="assert_pinned_plain",
        guard_class="safety",
        why="_assert_pinned_plain 判联接的闸整个关掉:父目录可被偷换成联接",
    ),
    Mutation(
        id="S-checkout-branchexists-off",
        target="server.py",
        edits=[Edit(884, "check=False).returncode != 0:",
                    "check=False).returncode == 0:")],
        probe="checkout_exists",
        guard_class="safety",
        why="checkout 的「必须是已存在的本地分支」闸:短 sha 会把仓切到游离 HEAD",
    ),
    Mutation(
        id="S-ref-colon-inject",
        target="server.py",
        edits=[Edit(190, r'_REF_RE = re.compile(r"[\w@^~][\w@^~{}+./-]*")',
                    r'_REF_RE = re.compile(r"[\w@^~-][\w@^~{}+./:-]*")')],
        probe="ref_whitelist",
        guard_class="safety",
        why="_REF_RE 放行冒号和前导 -:'--format=%H'/'HEAD:x' 重新打进 git show",
    ),
    Mutation(
        id="S-resolve-rootself-off",
        target="server.py",
        edits=[Edit(458, "if p == self.root:", "if False:")],
        probe="resolve_root_self",
        guard_class="safety",
        why="resolve_in_root 不拒根目录自己:discard('.')=删整个仓",
    ),
    Mutation(
        id="S-resolve-gitdir-off",
        target="server.py",
        edits=[Edit(460, ".lower() == \".git\":", ".lower() == \".git-never\":")],
        probe="resolve_git",
        guard_class="safety",
        why=".git 目录保护失效:discard('.git') 直删版本库",
    ),
    Mutation(
        id="S-api-file-dir-e23-off",
        target="server.py",
        edits=[Edit(1072, "if ap.is_dir():", "if False:")],
        probe="api_file_dir_guard",
        guard_class="boundary",
        why="E23 回归:/api/file?file=<目录> 返回 tree 清单当 old 字段",
        suites=["test_aigit_extra.py"],
    ),
    Mutation(
        id="S-branch-seed-prefix-off",
        target="server.py",
        edits=[Edit(940, '_gen.BRANCH_PREFIX = "%s/s%s" % (_GEN_BASE_PREFIX, seed)',
                    "_gen.BRANCH_PREFIX = _GEN_BASE_PREFIX")],
        probe="gen_seed_prefix",
        guard_class="logic",
        why="D8 回归:分支名不带 seed 前缀,同仓连续生成撞分支名",
        suites=["test_aigit.py"],
    ),
    Mutation(
        id="H-validate-refname-slash-off",
        target="history_ops.py",
        edits=[Edit(193, '_need(not name.startswith("/") and not name.startswith("."),',
                    "_need(True,  # MUTATED")],
        probe="hist_ref_whitelist",
        guard_class="safety",
        why="分支名白名单放行前导 / 和 .:'/abs' 被当有效名字",
    ),
    Mutation(
        id="H-require-confirm-off",
        target="history_ops.py",
        edits=[Edit(385, "_need(confirmed is True,", "_need(True,  # MUTATED")],
        probe="hist_require_confirm",
        guard_class="safety",
        why="所有破坏性 do_* 不再强制 confirmed=True,误操作无二次确认",
    ),
    Mutation(
        id="G-assert-prefix-off",
        target="gen.py",
        edits=[Edit(296, "if not rel.startswith(PREFIX):", "if False:")],
        probe="gen_prefix",
        guard_class="safety",
        why="ScenarioGen 不再要求落盘路径带 aigit_gen_ 前缀,"
            "演练仓会直接写用户文件",
    ),
    Mutation(
        id="C-abs-dotdot-off",
        target="conflicts.py",
        edits=[Edit(302, 'if ".." in Path(rel).parts:', "if False:")],
        probe="conf_resolve_dotdot",
        guard_class="safety",
        why="ConflictResolver 路径放行 ..:冲突处理写出仓根",
    ),
    Mutation(
        id="C-abs-gitdir-off",
        target="conflicts.py",
        edits=[Edit(311, "gitdir = (self.git.root / \".git\").resolve()",
                    "gitdir = None  # MUTATED")],
        probe="conf_resolve_git",
        guard_class="safety",
        why="ConflictResolver 不再拦 .git 路径",
    ),
    # ---------- 前端(ui) ----------
    Mutation(
        id="U-esc-quote-off",
        target="web/index.html",
        edits=[Edit(466, '.replace(/"/g, "&quot;")', "")],
        probe="esc_index",
        guard_class="ui",
        why="index.html 的 esc() 不再转双引号:属性注入 XSS",
        suites=["test_aigit_web.py"],
    ),
]
