/* git.h —— 唯一拼 git argv / 造环境的模块（与 Python 版 gitguard 对位）。
 *
 * 三层：
 *   1. 环境隔离：屏蔽用户/系统配置里**能执行程序**的那一半
 *      （hooksPath / filter / gpg / ssh / pager / editor / fsmonitor …），
 *      做法与 Python 版一致：GIT_CONFIG_NOSYSTEM=1 + GIT_CONFIG_GLOBAL→不存在
 *      + 一串 `-c` 覆盖。
 *   2. 语义透传：只把「影响结果、不能执行程序」的 core.* 透传给 git
 *      （autocrlf / eol / symlinks / ignorecase / quotepath / excludesfile …）。
 *   3. 调用与计数：CreateProcessW + 管道；每个请求的 spawn 次数与子命令名
 *      进 X-aigit-git-calls / X-aigit-git-cmds。
 */
#ifndef AIGIT_GIT_H
#define AIGIT_GIT_H

#include <wchar.h>
#include "util.h"

/* 初始化（argv 解析后调一次）：建 guard 目录、读语义配置。 */
void git_init(const wchar_t *root);

const wchar_t *git_root(void);

/* 运行一条 git。args 是**逻辑参数**（不含 git / -C / guard 的 -c）。
 * stdin_data 可为 NULL。out/err 必须已 init（可为 NULL 表示丢弃）。
 * 返回进程退出码；spawn 失败返回 -1。 */
int git_run(const char *const args[], const char *stdin_data, size_t stdin_len,
            buf *out, buf *errp);

/* 每请求清零；读计数与子命令串（逗号分隔，顺序同调用顺序）。 */
void  git_count_reset(void);
/* 只记一笔调用、不 spawn（能力闸在 fork 之前就记账的路径用）。 */
void  git_count_note(const char *cmd);
int   git_calls(void);
const char *git_cmds(void);

#endif
