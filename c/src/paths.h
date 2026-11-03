/* paths.h —— 请求路径 → 仓内相对路径（**安全关键**，与 Python resolve_in_root 对位）。
 *
 * 拒绝四类（少一类就是一个能丢数据/读仓外的洞）：
 *   1. 绝对路径 / 盘符 / 越界（`..` 走出 root）；
 *   2. 解析结果恰好是 root 自己；
 *   3. root/.git 及以下；
 *   4. 顺着符号链接/目录联接走出去（用句柄取 real path 再比一次）。
 */
#ifndef AIGIT_PATHS_H
#define AIGIT_PATHS_H

#include <stddef.h>

/* 成功返回 malloc 的仓内相对路径（'/' 分隔，UTF-8）；失败返回 NULL 并写 *err
 * （malloc 的可读文案，调用方 free）。 */
char *path_rel_in_root(const char *input, char **err);

#endif
