# 发布清单（aigit 1.0.0）

状态：`[x]` 已做并有判据 / `[ ]` 还没做

## 一、已做

- [x] **许可：GPL-2.0**（与 git 相同）。根目录 `LICENSE`，发行包内也带一份。
- [x] **上架安全加固**：只监听 127.0.0.1；`Host`/`Origin` 本机同源闸（挡 DNS
      rebinding 与网页 CSRF）；无 CORS；破坏性操作 `confirmed` + plan 预演；
      `nosniff`；路径/`.git`/越界一律 400。判据 `c/tests/launch_guard_c.py`
- [x] **启动体验**：端口占用自动顺延 20 个；`--open`（`AIGIT_NO_BROWSER=1` 可关）；
      `--version`；`start.bat` 写 `aigit.log`，失败 `pause`
- [x] **打包**：`c/package.ps1` → `c/dist/aigit-c.zip`（exe + web + start.bat +
      README.txt + LICENSE）
- [x] **CI**：`.github/workflows/ci.yml`（构建 / 16 片对拍 / 质量 / 压力 四个 job）
- [x] **CD**：`.github/workflows/release.yml`（`v*` 标签 → 构建打包 + SHA256SUMS +
      发布 GitHub Release）
- [x] **可移植构建**：`build.ps1` 依次找 `AIGIT_MINGW` → 常见目录 → PATH；
      `mutation_c.py` 不再写死本机路径；makefile 的 mkdir/clean 在 cmd 与 bash 下都能跑

## 二、发布操作（30 秒）

```powershell
# 1) 推代码（CI 会自动跑）
git push origin main

# 2) 打标签即发布（CD 自动构建 zip 并挂到 Release）
git tag v1.0.0
git push origin v1.0.0
```

标签推上去后，Release 里会有：

- `aigit-c.zip`（约 0.96MB）
- `SHA256SUMS.txt`

## 三、本机可复现的验收

```powershell
# 完整门禁（约 25 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File c\tests\run-all-c.ps1

# 打包 + 解压冒烟
powershell -NoProfile -ExecutionPolicy Bypass -File c\package.ps1
```

## 四、体积/性能（写下载页可以用）

| 项 | 数值 |
|---|---|
| exe | 1,942,523 字节（静态链接，单文件） |
| zip | 963,246 字节（含 LICENSE） |
| 冷启动到可服务 | < 1s（127.0.0.1 本机） |
| 批量 diff 300 文件 | 142.8ms（逐文件 13.4s） |
| stage / unstage / discard 300 | 229 / 189 / 317ms |
| reset --hard（300 脏文件） | 351ms |
| fork 一个节点 / 回到某节点 | ~100ms / ~144ms |

## 五、你需要知道的限制（自己心里有数即可）

- exe 未签名：SmartScreen 会提示「未知发布者」（按你的决定，不做签名）。
- Windows-only（Win32/Winsock，MinGW 构建）。
- 只监听 127.0.0.1，没有远程访问模式。
- 不做历史改写（除用户显式 `reset` / `revert`）。
## 六、发布记录

### v1.0.0-beta.1（2026-11-15，预发布）

| 项 | 值 |
|---|---|
| 代码 | `6f1f191`（main） |
| CI | run #7 全绿：build / conformance 16/16 / quality / stress，22m11s |
| 发布 | https://github.com/AI4tools/AIgit/releases/tag/v1.0.0-beta.1 |
| 资产 | `aigit-c.zip` 963,246B，`SHA256SUMS.txt` |
| zip SHA256 | `8fb4364c1be96a3a6531cd695a473606f363e515c41e5b7a44cf7a3ff44fa148` |
| 本地门禁 | `run-all-c.ps1` 全绿（c 全部门禁通过） |
| 冒烟 | 包内 exe `--version` → `aigit 1.0.0-beta.1`；`/api/health` 正常；外域 `Origin` → 403 |

首次跑 CI 时暴露的三个只在 CI 环境成立的坑（本地都绿）：

1. 测试脚本把 `EXE` / `PY_ROOT` 写死成本机路径 → 改成相对推导 +
   `AIGIT_C_EXE` / `AIGIT_PY_ROOT` 覆盖。
2. runner 的 TEMP 是 8.3 短名（`C:\Users\RUNNER~1\...`），而路径校验比的是
   真实长路径 → 仓内文件全被判「路径越界」。`main.c` 现在用
   `GetFinalPathNameByHandleW` 把 `--root` 规范成真实长路径/真实大小写。
3. 批量 diff 直接返回 git 的原始 stdout，工作区是 CRLF 时每个 hunk 行尾多一个
   `\r`（单文件 GET 走 universal newlines）→ 拆块前先归一化。

另外 CPython 补丁版本之间自己的行为也会变（`b64decode("abcd=")`：3.11.0 /
3.12.4 接受，3.12.10 拒绝；`dict[:12]`：TypeError vs KeyError 文案）。C 跟
**新版**（3.12.10）口径，判据对「异常文案族」只折叠**已知等价**的几条并打 NOTE。
