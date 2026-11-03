# aigit C 重写方案（v1，只出方案，不写代码）

> 目标：用 C 从零实现与当前 Python 版**接口对等**的 aigit。
> 判据不是「能跑」，而是**现有黑盒套件与差分判据直接跑 C 服务全绿**。

## 0. 先说清楚：C 能带来什么、带不来什么

| 期望 | 现实（都有实测依据） |
|---|---|
| **更快** | 收益很小。aigit 的成本 79% 是 **git 子进程启动税**（实测 36.4ms/次），C 省掉的是 HTTP+JSON 层（约 8ms/请求）与解释器内存。读端点 40–80ms、commit 400–520ms 基本不变 |
| **单文件分发** | ✔ 真正收益：一个 `aigit.exe`，不需要 Python |
| **内存/线程可控** | ✔ 线程池、零 GC、可预测的峰值 |
| **可移植到非 Windows** | 部分：核心可移植，但 junction/句柄那套安全逻辑是 Win32 专属 |
| **更容易审计** | 双刃：C 的内存安全面更大，需要 ASan/静态分析配套 |

**结论**：如果目标是「更快」，不建议重写；如果目标是「单二进制、无解释器依赖、可嵌入」，C 是对的选择。

## 1. 范围（v1 = 与 Python 版对等）

### 1.1 必须实现（对等判据会直接跑）

| 组 | 端点 |
|---|---|
| 读 | `health` `status` `log` `branches` `diff` `file` `history/{tags,reflog,stashes,blame,file}` `conflict/{state,list,inspect}` `remote/{capability,list,ahead-behind}` |
| 写 | `stage` `unstage` `discard` `commit` `checkout` `branch` `merge` `merge-abort` `reset` + 对应 `*/plan` |
| 冲突 | `conflict/{resolve,choose,delete,commit,abort}` |
| 历史 | `history/{amend,undo-last-commit,revert,revert/abort,stash,stash/apply,stash/pop,stash/drop,tag/create,tag/delete,branch/rename,branch/delete,reflog/restore}` |
| 静态 | `/` 与三个面板的 js/html |

### 1.2 v1 不做（明确登记）

- `remote/{fetch,pull,push,ls-remote}`：要接凭据助手/SSH/代理，属于另一条线；v1 保留 `capability` 与只读的 `ahead-behind`
- `/api/gen`（随机测试数据生成器）：测试工具，不是产品面
- 非 Windows 平台

### 1.3 契约冻结

**JSON 字段名、错误文案、HTTP 状态码逐字不变** —— 这是能用现成套件验收的前提。
（已知的有意差异：冲突合并提交会补 `Merged: <分支>` 一行，见 `doc/aigit-conflicts.md:147`。）

## 2. 架构

```
aigit.exe
├── http.c       WinSock2 + 线程池；只支持 Content-Length（不支持 chunked）
├── json.c       自写 writer（UTF-8 转义）+ 严格 parser（拒绝 NaN/重复键以外的一切）
├── git.c        CreateProcessW + 匿名管道；argv 数组（不经 shell）；统一计数钩子
├── guard.c      环境中和：GIT_CONFIG_NOSYSTEM / GIT_CONFIG_GLOBAL→不存在 /
│                -c hooksPath=空目录 / fsmonitor= / gpgsign=false / 语义键透传
├── paths.c      真实路径判定（GetFinalPathNameByHandleW）+ 逐级父链句柄 + 联接检测
├── repo.c       status / 写操作 / 锁（每仓一把 mutex，与 Python 版同构）
├── index.c      .git/index 直读（v2/v3；v4/split/sparse 回退 ls-files）
├── refs.c       loose + packed-refs / HEAD 三态 / reflog / stash reflog
├── catfile.c    常驻 git cat-file --batch（P4）+ 空闲回收
├── objfmt.c     commit/tag 对象解析 + subject 段落折叠 + ISO 日期 + %h 唯一前缀
└── handlers_*.c 逐端点
```

### 2.1 关键设计点

* **锁**：每仓一把 `CRITICAL_SECTION`（对应 `repolock.repo_git_lock`）。history/conflicts 与 repo 共用同一把 —— 与 Python 版最终形态一致。
* **git 调用**：`CreateProcessW` + 两个匿名管道（stdout/stderr）+ 可选 stdin（作用域受限 add 用 `--pathspec-from-file=-`）。
* **安全面（最重）**：路径解析必须用**句柄**判真实路径，不能只做字符串前缀比较；联接/符号链接要逐级父链判 `FILE_ATTRIBUTE_REPARSE_POINT`。F-02 / F-06 的每条用例都要有对应实现。
* **快路径**：索引/refs/reflog 直读在 C 里比 Python 更自然；`cat-file --batch` 常驻 + 60s 空闲回收照搬。
* **并发**：线程池默认 8；每连接一线程；请求级超时（写入超时直接断连，不挂死）。

## 3. 目录与构建

```
aigit/
└── c/
    ├── Makefile            # mingw32-make，产出 c/build/aigit.exe
    ├── CMakeLists.txt      # 可选（同样只依赖 Win32）
    ├── src/*.c
    ├── src/*.h
    └── tests/              # C 单测（自写断言，不引第三方框架）
```

构建命令（本机已验证工具链存在）：

```powershell
mingw32-make -C D:\project\aigit\c -j4          # MinGW-w64 gcc
# 或（VS 2022 已装，走 vcvars64 后）
cmake -S c -B c\build-msvc -G Ninja && ninja -C c\build-msvc
```

发布形态：`-O2 -s -static -static-libgcc` → 单个 exe。

## 4. 验收（方案的核心：直接复用现有判据）

现有 Python 套件全是**黑盒 HTTP**，所以只要 JSON 契约不变，就能直接打 C 服务：

1. 给 `tools/run-all.ps1` 加 `-Server <exe>`（或环境变量 `AIGIT_SERVER`）；
2. 20 个套件 + 并发 15 组 + 性能预算 28 项，全部指向 C 服务；
3. 差分脚本（`cmp_vs_git.py` / `cmp_write_vs_git.py`）同样只换被测服务。

**v1 的退出判据（硬）**：

| 面 | 判据 |
|---|---|
| 读差分 | 275 项 0 失败 |
| 写差分 | 12 场景 0 失败，且固定身份/日期后 **commit sha 逐字相同** |
| 输入边界 | 119 例 0 失败（不 500、不泄漏、被拒无副作用） |
| 冲突/历史 | `test_conflicts` 20/20、`test_history_ops` 37/37 对等 |
| 安全面 | F-06 9 条对等；F-02 路径/联接用例对等 |
| 并发 | 15 组 0 失败（含注入自检） |
| 性能 | 28 项预算内（预期与 Python 版同量级） |
| 内存 | 10k 请求 soak 后无泄漏（Working Set 稳定）、无句柄泄漏 |

### 4.1 C 版需要新建的两件工具

* **变异测试**：Python 引擎只认 `.py` 靶。C 方案用**编译期开关**：关键守卫写成
  `#ifndef MUTANT_<id>`，构建脚本生成变体并跑套件；50 条靶沿用现有 id 与预期。
* **内存/句柄检查**：MinGW 下用 AppVerifier 或 Dr. Memory；VS 构建用
  `/fsanitize=address`（clang-cl 可用）或 `_CrtSetDbgFlag` 泄漏检测。

## 5. 里程碑与退出判据

> 人日估算基于「Python 版约 10.8k 行产品代码 + 20k 行测试」，是**估算不是承诺**。

| 阶段 | 内容 | 退出判据 | 估算 |
|---|---|---|---|
| M0 | HTTP + JSON + git_run + guard + health/status + 计数 | 2 个套件绿 | 3–5 人日 |
| M1 | 读面 + 索引/refs/reflog 直读 + cat-file 常驻 | **275 项差分全绿** | 8–12 人日 |
| M2 | 写面 + 作用域/哨兵/输入守卫 | 12 场景 + 119 例全绿 | 8–12 人日 |
| M3 | 冲突 + 历史写操作 | conflicts/history_ops 对等 | 6–10 人日 |
| M4 | 安全面（句柄级路径/联接）+ F-06 用例 | f06 对等 + 对抗用例 | 5–8 人日 |
| M5 | 并发/性能/打包/文档 | 15 组 + 28 项预算 | 4–6 人日 |

**建议先做 M0+M1**：读面差分是现成的 275 项，能在两周内给出「C 版可信不可信」的硬答案；
写面和安全面再投入。

### 5.1 实际进度（2026-11-03，[实测]）

| 阶段 | 状态 | 实测依据 |
|---|---|---|
| M0 | **完成** | `conformance_m0.py` 9/9；`c/build/aigit.exe` 静态单文件 |
| M1（读面） | **完成** | Python `do_GET` 的 21 组路由全部接上；`cmp_vs_git_c.py` **275/275**；8 片一致性 **2,442 项全过** |
| M1.5（直读快路径） | **完成（读面）** | branches/file/diff/conflict/tags/reflog/stashes 均 0 spawn；tags 63.4→1.0ms、reflog 57.5→2.1ms、stashes 57.6→5.4ms（详见 `doc/aigit-perf.md` §12.23） |
| M2（写面） | 未开始 | 12 场景 / 119 例判据尚未对 C 跑 |
| M3（冲突/历史写） | 未开始 | 读面已覆盖；写侧 `conflict/{resolve,choose,delete,commit,abort}` 未接 |
| M4（安全面） | 部分 | `paths.c` 已有越界/`.git`/真实路径复查；符号链接受限（本机 WinError 1314），未做句柄级全量对拍 |
| M5（并发/打包） | 部分 | 并发冒烟 12×25=300 请求 0 不一致；性能预算 28 项尚未挂到 C；打包仅静态 exe |

读面已完成的边界（与 Python 版仍不同的点）登记在 `c/README.md`「已知差异」，
主要包括：非 ASCII 数字、超 2^126 饱和、Unicode `strip`/`\w` 近似、非法 UTF-8
输出透传、以及无符号链接权限导致的未验证项。

## 6. 风险与取舍（不粉饰）

1. **安全面是最大风险**：Python 版有约 2k 行守卫（路径解析、联接、作用域、索引哨兵）。
   C 里漏一处就是**数据丢失或仓外读取**。M4 不能省，且必须用例驱动移植。
2. **解析细节要重做一遍**：porcelain v2、`ls-files -t`、reflog、tag 格式化、`%h` 唯一前缀
   —— 每一条都已有差分判据，但都要在 C 里重新踩一遍。
3. **性能不会明显变好**（见 §0）。如果验收时有人期待「C 版更快」，会失望。
4. **双实现维护成本**：C 版对等后，建议 Python 版转为**参考实现/测试预言机**保留，
   不再加功能；否则两边的契约会漂。
5. **变异测试要重做**：现有 50 靶是源码文本变异 + Python 套件，C 需要编译期开关方案。

## 7. 与现有仓库的关系

* 代码放 `c/`，**不动** Python 版；分支 `c-rewrite` 开发，`main` 继续是 Python 版
  （远端 `main` 现在是 Python 版 10 个提交）。
* `tools/run-all.ps1` 加 `-Server` 参数即可双跑：`-Server c/build/aigit.exe`。
* 文档：本方案 + C 版自己的 `doc/`（不覆盖 Python 版的性能正本）。
