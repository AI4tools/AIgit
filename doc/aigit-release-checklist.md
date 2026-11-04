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
| exe | 1,992,105 字节（静态链接，单文件） |
| zip | 962,560 字节（含 LICENSE） |
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
