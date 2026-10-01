# aigit 测试数据生成器 —— 三类场景的构造方式与「可合并」的保证

文件:

| 文件 | 角色 |
|---|---|
| `gen.py` | 生成器本体。**不依赖 HTTP 层**,只依赖 `subprocess` 跑 git。可单测、可被别的工具复用。 |
| `server.py` | 只做转发 + 返回值整形(见文末「server.py 的改动」)。 |
| `selftest.py` | 9 组自测,全部在 `%TEMP%\aigit-gen-test\` 下的临时仓里跑。 |

跑法:

```bash
python gen.py --root <repo> --kind mergeable --count 3 --seed 42
python gen.py --root <repo> --kind all --count 2 --seed 42
python gen.py --root <repo> --clean            # 只删自己造的东西
python selftest.py                             # 9 组自测
```

---

## 1. 四类场景共用同一套骨架

不管哪一类,每个场景都走这 6 步。**第 3、4 步是旧实现最大的 bug 所在。**

```
1. 造该场景专属的种子文件(文件名带 index,场景之间零重叠)
2. 提交,记下这个提交作为 base(本场景的基准)
3. 两个分支都从 base 这个 sha 拉出来 —— 不是从 main 拉的
4. 两个分支各自从 base 的内容出发改自己的东西
5. 真跑一次 git merge 验证语义
6. 把 main 回滚到 base,交给用户手动试
```

### 为什么第 3 步必须用 sha 而不是 `main`

旧实现写的是 `git checkout -b b_name main`。看起来没问题,但 `main` 在
生成过程中是**会往前走的** —— 每个场景的种子提交都落在 main 上。于是
场景 2 的分支是从「已经包含场景 1 种子」的 main 拉出来的,两个分支的
差异里混着不属于本场景的内容。

用 `git checkout -b <name> <base_sha>` 之后,本场景的两个分支永远只差
base 一个提交,验证结果才有意义。

### 为什么第 4 步必须「各自从 base 重新算」

旧实现用一个**共享的可变 `lines` 列表**:

```python
lines[0] = "EDITED-BY-A ..."      # 就地改
...
self._run(["checkout", "-b", b_name, "main"])   # 回到 main
lines = "\n".join(lines).split("\n")            # ← 还是那个被 A 污染过的列表
lines[-2] = "EDITED-BY-B ..."
```

`checkout` 只切换工作区文件,**不会还原 Python 里的列表**。于是分支 B 的
diff 里混着分支 A 的改动。merge 确实不冲突 —— 但不是因为「改了不同区域」,
而是因为两边把那行改成了**一模一样**的文字。

这是「不能只靠我生成了两个分支就说它可合并」的典型陷阱:结果对了,理由是错的。

现在的做法是每个分支都 `list(seed)` 重新拷一份,`seed` 来自 base 的
原始内容,两个分支互不污染。

---

## 2. 三类场景的确切构造

### `mergeable` —— 同一文件的不同区域

一个文件,行数 56~104。A 改**头部**一块(3~7 行),B 改**尾部**一块(3~7 行)。

可合并不是靠「祈祷」,而是构造出三路合并的经典条件:

> 两个改动块之间留足 **>= 20 行**完全未触碰的内容。

git 的 merge 只在改动**相邻或重叠**时才产生冲突。间隔 20 行以上,两个改动
必然落在两个独立的 hunk 上,不可能撞在一起。

实测间隔(种子 1001):78 / 64 / 52 行。

### `unrelated` —— 完全不同的文件

两个文件 `aigit_gen_unrel_a_N.txt` / `aigit_gen_unrel_b_N.txt`。
A 改写前者,B 改写后者。

三路合并根本不会碰到同一个文件,所以必然干净。这是最强的一种「可合并」,
强到不需要任何行号计算。

### `adddel` —— 新增 + 删除

A **新增** `aigit_gen_adddel_fresh_N.txt`,B **删除** `aigit_gen_adddel_doomed_N.txt`。

关键在于这两个动作落在**不同路径**上。git 真正会冲突的组合是:

| 组合 | 结果 |
|---|---|
| 同路径 新增 / 新增 | 冲突 |
| 同路径 删除 / 修改 | 冲突 |
| 同路径 修改 / 修改(重叠) | 冲突 |
| **不同路径 新增 / 删除** | **干净** ← 本场景 |

### `conflict` —— 对照组

两个分支改**同一文件的同一行**(不取首尾,避免空行干扰)。必然冲突。

**注意验证方式**:只合 A 是看不出冲突的 —— A 相对 main 是快进,压根不做内容
合并。必须「先合 A(快进成功)、再合 B(撞同一行)」。旧的自测只合 A,所以
一直显示「符合预期」,其实什么都没验证到。

---

## 3. 「可合并」是怎么被验证的

**不靠推断,真跑 `git merge`。** 而且有两层:

1. 生成器内部 `_verify()` 跑一遍,结果写进每个场景的 `verified` 字段;
2. `selftest.py` 的 `independent_verify()` **自己再跑一遍**,不读生成器自报的字段。

判定「可合并」的三条硬标准(缺一不可):

```
1. 两次 merge 全部 rc == 0
2. 结束后 git status --porcelain 为空(工作区干净)
3. 结束后 git diff --diff-filter=U 为空(没有未合并路径)
```

判定「必然冲突」:

```
1. merge rc != 0
2. git diff --diff-filter=U 非空
```

验证完立刻回滚到 base(`merge --abort` + `checkout main` + `reset --hard base`),
把干净的分支留给用户手动试。

---

## 4. 确定性

同一个 `(seed, kind, count)` 必然产出逐字节相同的内容。

随机源用 sha256 派生,不用内置 `hash()`:

```python
h = hashlib.sha256(("%s|%s|%d|%s" % (seed, kind, index, salt)).encode()).digest()
return random.Random(int.from_bytes(h[:8], "big"))
```

理由:`hash()` 对 str **每进程加盐**,跨进程不稳定,确定性无从谈起。
每个场景有独立的随机源(由 `index` 区分),所以场景之间互不影响,但整体
仍由 seed 完全决定。

文件落盘时显式 `newline="\n"`。否则 Windows 上默认写 CRLF,同一个 seed 在
不同机器上生成的内容就逐字节不同了,确定性直接失效。

---

## 5. 安全护栏

生成器会写文件、建分支、跑 merge,所以护栏比功能优先。

| 护栏 | 实现 |
|---|---|
| 文件名强制前缀 | 所有落盘/删除路径必须以 `aigit_gen_` 开头,否则 `GenError` 拒绝 |
| 分支名强制前缀 | `aigit_gen/<kind>/<index>/{a,b}`,`--clean` 只删这个前缀下的 |
| 路径越界检查 | `resolve()` 后必须仍在 repo root 内,含 `..` 直接拒绝 |
| 脏工作区拒绝 | 生成前 `git status --porcelain` 非空就拒绝,报错列出前 8 条 |
| 分支已存在就拒绝 | 不覆盖。防止同一仓反复生成时互相踩 |
| 不改仓库配置 | git 身份等用 `-c` 逐条传,不在别人的 `.git/config` 里留副作用 |

### `--clean` 的一个坑

`git rm` 只把文件从**索引**里摘掉,HEAD 里那份还在。于是文件确实没了,
但 `git status` 一片红 —— 显示「N 个已暂存的删除」。**这不叫收拾干净。**

所以 `clean()` 默认把这次删除**提交掉**,让仓回到干净状态。提交内容只有
aigit 自己的文件被删除,不含任何用户改动(生成阶段已要求工作区干净,这里又
只按前缀删)。不想让生成器产生提交就传 `commit=False`(CLI 是 `--no-commit`)。

---

## 6. 随机内容的质量

旧版 `_random_lines` 产出 `line 1 alpha beta` 这种等长占位符。现在
(`_code_block`)升级四处,每一条都有实测:

| 项 | 实测(seed=1001, mergeable#0) |
|---|---|
| 行长跨度 | min=18 max=88,极差 70 |
| 不同长度值 | 38 种 / 80 行 |
| 逐字重复行 | 14 行(涉及 7 个不同行) |
| 缩进层级 | `{0:33, 4:33, 8:8, 12:6}` |

- **行长度有变化** —— 否则 diff 视图全是横向滚动。
- **重复内容有保底** —— 约 8% 的行是前面某行的逐字重复,加上首尾样板行。
  给「在文件里找某一行」这类搜索场景提供素材,不靠随机数碰运气。
- **首尾样板行** —— 每个文件都带两行相同的 license 头注释。真实代码库就是
  这样,多个文件共用同一份头。
- **缩进用对称随机游走** —— 进/出嵌套概率接近(0.11 / 0.14),深度封顶 3。
  早先按行型分支决定加不减(进 35% / 退 10%),结果深度一路涨到顶,80 行里
  48 行缩进 12 空格,看起来像 `def` 嵌在 `if` 里面 —— 那是假代码。

---

## 7. server.py 的改动

只有三处,其余一行未动:

1. **import** —— `from gen import ScenarioGen, GenError`
2. **`Repo.generate()`** —— 转发到 `ScenarioGen(self.root).generate(...)`,
   再把返回值整形回旧前端吃的形状 `branches=[{file, branches, expect}]`。
   新增 `Repo.clean_generated()` 转发 `--clean`。
3. **异常映射** —— `GenError` 走 HTTP 400(护栏拒绝是用法问题,不是服务端故障)。

顺带删掉了 `Repo` 里的 `_gen_two_edits` / `_gen_disjoint` / `_gen_add_delete`
/ `_random_lines` / `_write`。它们只被生成器用,留着就是第二份会分叉的实现。
`server.py` 510 → 369 行。

`Repo.generate` 的返回值**新增**了 `scenarios`(每场景的详细信息)、`seed`、
`count`、`allVerified`、`summary`,旧字段全部保留。

---

## 8. 已知边界

- **要求仓里有 `main` 分支**。没有就报错,不自动创建 —— 在别人的仓里凭空
  建分支是越界行为。
- **要求仓里已有至少一个提交**。空仓(无 HEAD)报错。
- **同一仓反复生成同 seed 会被拒绝**(分支已存在)。先 `--clean`,或换 seed。
  这是有意的:覆盖已有分支可能毁掉用户正在用的东西。
- **`gc_oom` 一类的极端场景不在范围内**。生成器只造文本文件,不造大文件。
