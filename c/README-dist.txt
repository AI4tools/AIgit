aigit -- 本地 git 前端（单文件 exe，只监听 127.0.0.1）
版本: 运行 aigit.exe --version 查看

怎么用
  1) 解压到任意目录（不要放在 Program Files，避免权限问题）
  2) 双击 start.bat；浏览器会自动打开 http://127.0.0.1:8777/
  3) 第一次启动会在同目录建 workspace\ 当演示仓库
  4) 想管理自己的仓库: start.bat "D:\path\to\repo" 8899

许可
  * GPL-2.0（与本仓库一致）。完整文本见同目录 LICENSE。
  * 源代码: https://github.com/AI4tools/AIgit

安全
  * 只绑定 127.0.0.1，局域网/外网都连不上
  * 校验 Host / Origin，只接受本机同源请求（防 DNS rebinding 与网页 CSRF）
  * 破坏性操作（discard / reset / 删除）都要 confirmed=true，并先有 plan 预演
  * exe 未做代码签名；可核对发布页给的 SHA-256

数据
  * 不改你的提交历史（除了你显式点 reset/revert）
  * 日志: aigit.log（同目录）
  * 卸载 = 删目录；workspace\ 是你的数据，删前自行备份

命令行
  aigit.exe --version
  aigit.exe --root <repo> [--port <n>] [--web <dir>] [--open] [--sandbox]
