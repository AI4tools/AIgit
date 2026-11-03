# aigit C 版

Python 参考实现在仓库根目录（`server.py` 等），这里是**同契约**的 C 重写。
目标不是「再写一个 git 前端」，而是：单文件静态 exe、无运行时依赖、JSON 逐字段与
Python 版相等，从而直接复用同一套黑盒判据。

## 构建

```powershell
$env:PATH='C:\Environment\cpp\mingw64\bin;'+$env:PATH
mingw32-make -C c -j4          # 产出 c/build/aigit.exe（静态链接，单文件）
```

要求 MinGW-W64（UCRT）与 cmake 无关；makefile 只用了 `gcc` 与 `windres` 级别的工具。

## 运行

```powershell
c\build\aigit.exe --root D:\path\to\repo --port 8765
```

`--root` 必须是仓库工作区根目录。服务只读 + 少量写端点（M2 范围内），默认监听
`127.0.0.1`。

## 当前覆盖（2026-11-03，M1 读面 + 计划面完成）

Python 版 `do_GET` 里的 21 组路由已经**全部**接上：

| 组 | 端点 |
|---|---|
| 基础 | `/api/health`、`/api/status`、`/api/branches`、`/api/log`、`/api/file`、`/api/diff` |
| 历史 | `/api/history/file`、`/api/history/blame`、`/api/history/tags`、`/api/history/reflog`、`/api/history/stashes`、`/api/history/stash`、`/api/history/plan/<op>` |
| 冲突/远程 | `/api/conflict/state`、`/api/conflict/list`、`/api/conflict/inspect`、`/api/remote/capability`、`/api/remote/list`、`/api/remote/heads`、`/api/remote/ahead-behind`、`/api/remote/plan/<op>` |
| 预演 | `/api/reset/plan`、`/api/discard/plan` |

写面（M2/M3）已经接上 27 个 POST 路由里的 **全部 27 个**：

| 组 | 端点 |
|---|---|
| 基础写 | `/api/stage`、`/api/unstage`、`/api/discard`、`/api/commit` |
| 分支/合并 | `/api/checkout`、`/api/branch`、`/api/merge`、`/api/merge-abort`、`/api/reset` |
| 冲突 | `/api/conflict/resolve`、`/api/conflict/choose`、`/api/conflict/delete`、`/api/conflict/commit`、`/api/conflict/abort` |
| 历史写 | `/api/history/amend`、`/api/history/undo-last-commit`、`/api/history/reflog/restore` |
| 引用写 | `/api/history/revert`、`/api/history/revert/abort`、`/api/history/branch/rename`、`/api/history/branch/delete`、`/api/history/tag/create`、`/api/history/tag/delete` |
| stash 写 | `/api/history/stash`、`/api/history/stash/apply`、`/api/history/stash/pop`、`/api/history/stash/drop` |

v1 明确不做：`/api/gen`、`/api/remote/{fetch,pull,push}`。对这些路径返回 404，与
Python 版「没有这个接口」一致。远程能力闸（NetGate）默认关闭，`/api/remote/heads`
在关闭时与 Python 一样回 400 `net_disabled`（连 `X-aigit-git-calls` 也一并对齐）。

## 判据

```powershell
python -X utf8 c\tests\run_conformance.py          # 14 片
python -X utf8 c\tests\concurrency_c.py 12 25     # 并发冒烟，300 请求
```

每片都是「同一仓、同一时刻，起 Python 参考服务与 C 服务，逐端点比较
HTTP 码 + JSON 深比较」（读面分片还比 `X-aigit-git-calls`/`cmds`）。分片：

- `conformance_m0.py` —— health/status/404/405/CRLF（9 项）
- `conformance_basic.py` —— branches/log/file/diff（98 项）
- `conformance_histfile.py` —— history/file、history/blame（96 项）
- `conformance_histlist.py` —— history/tags、reflog、stashes（35 项）
- `conformance_conflict.py` —— conflict/*、remote/{capability,list,ahead-behind}（107 项）
- `conformance_plan.py` —— reset/plan、discard/plan（92 项）
- `conformance_histplan.py` —— history/stash、history/plan/<op>（185 项）
- `conformance_remoteplan.py` —— remote/heads、remote/plan/<op>（1,820 项，含 5 档能力闸）
- `conformance_write_basic.py` —— stage/unstage/discard/commit（122 项，含与真 git 的对照）
- `conformance_write_more.py` —— checkout/branch/merge/merge-abort/reset（93 个 POST 步骤 + 28 个最终状态）
- `conformance_write_conflict.py` —— conflict/{resolve,choose,delete,commit,abort}（221 个 POST 步骤 + 44 个最终状态）
- `conformance_write_history.py` —— amend/undo-last-commit/reflog restore（93 次 POST + 32 个最终状态）
- `conformance_write_refs.py` —— revert/revert-abort/branch rename|delete/tag create|delete（154 步 + 466 条 fuzz）
- `conformance_write_stash.py` —— stash/stash apply|pop|drop（103 次 POST + 32 个最终状态）

写面退出判据是 12 个「与真 git 同操作」的场景（`cmp_write_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_write_vs_git_c.py
# 失败 0 / 12 场景（stage/unstage/commit/discard/checkout/commit_crlf/merge_ff/
# conflict_resolve/commit_large/commit_binary/commit_submodule/commit_mode_bit）
```

读面退出判据是 275 项「与 git 直接比对」的差分（`cmp_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_vs_git_c.py
# 合计 275 项检查，失败 0（symlink 场景因本机无符号链接权限 SKIP）
```

## 与 Python 版仍存在的已知差异

只列会影响结果的；都是边角：

1. `limit` / `index` / `start` / `end` 的**非 ASCII 数字**（如 `limit=٥`）：Python 的
   `int()` 收，C 回 400。
2. 超过 2^126 的 start/end / index 在 C 里饱和（Python 任意精度）。
3. 工作区文件读失败（被独占/无权限）时，Python 抛 500，C 按「不许 500」返回空内容。
4. 请求头/参数上限：C 的 query 值超过 64 KiB 会回 400（Python 无此上限）；
   请求体在响应前会先读走（避免 RST）。
5. Unicode 细节：C 的 `strip`/`\w` 只近似 Python 的 Unicode 语义（NBSP、全角空格、
   emoji 等边角会分叉）；git 输出里的非法 UTF-8，Python 转 U+FFFD，C 原样透传。
6. 符号链接语义未验证：本机建链接需要开发者模式/管理员权限（WinError 1314）。
7. `net_enabled` 时 C 的 guard 仍塞 `credential.helper=""`/`core.sshCommand=""`，
   接真 https/ssh 远程会拿不到用户凭据（本地路径 remote 无差异）。
8. 写面 `stage/unstage` 的 spawn 数可能比 Python 多 1（Python 直读 `.git/index`，
   C 走 `git ls-files`）；响应与副作用相同。
9. Python 在文件系统异常（unlink/rmdir/listdir 失败）或 body 非法 UTF-8 时可能抛
   500；C 按「不许 500」一律回 400。未构造稳定 fixture。
10. commit 失败文案 Python 按**字符**截 400，C 按**字节**截（非 ASCII git 输出时可见）。
11. **写面无仓锁**：Python 有 per-repo RLock，C 的 HTTP 是多线程；并发写不保证串行
    （单请求语义已对上，并发写属未测范围）。
12. 仓内「符号链接→目录」C 一律跳过（Python 当文件链接收下）；目录联接已验证一致，
    真 symlink 因本机权限（WinError 1314）未验证。
13. `--root` 指向**非仓库目录**时，Python 启动会自动 `git init` 并造一个 sandbox 提交；
    C 不做这一步（`/api/health` 报 `isRepo:false`，写端点返回 not_a_repo）。指向真实
    仓库时无差异。
14. 历史写（amend/revert/branch/tag/stash）的 spawn 数可能比 Python 多：Python 有
    refs/reflog 文件快路径，C 走 git 命令；响应与副作用已逐项对齐。

## 目录

```
c/src/
  main.c       参数解析 / 启动
  http.c       WinSock2 HTTP + 路由分派
  json.c       JSON 渲染与转义
  util.c       buf / UTF-8 转换 / 字符串工具
  git.c        git 调用 + 环境隔离 + 语义配置透传 + 调用计数
  params.c     query 解析（percent-decode，NUL 可见）
  paths.c      请求路径 → 仓内相对路径（安全关键）
  status.c     porcelain v2 解析与 JSON
  catfile.c    常驻 git cat-file --batch（不计数，带状态查询）
  bodyjson.c   请求体 JSON 解析（Python json.loads 口径）
  writeutil.c  写端点共用：body 取值 / 统一成功响应（含 status opt-out、E3 兜底）
  read_basic.c       branches/log/file/diff
  read_history.c     history/file、history/blame
  read_history_list.c history/tags、reflog、stashes（直读 .git）
  read_conflict.c    conflict/*、remote/{capability,list,ahead-behind}
  read_plan.c        reset/plan、discard/plan
  read_histplan.c    history/stash、history/plan/<op>
  read_remoteplan.c  remote/heads、remote/plan/<op>
  write_basic.c      stage/unstage/discard/commit
  write_more.c       checkout/branch/merge/merge-abort/reset
  write_conflict.c   conflict/{resolve,choose,delete,commit,abort}
  write_history.c    amend/undo-last-commit/reflog restore
  write_refs.c       revert/revert-abort/branch rename|delete/tag create|delete
  write_stash.c      stash/stash apply|pop|drop
  routes.h     各模块的分派约定
```
