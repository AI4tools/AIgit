/* guardcfg.h —— 与 Python `gitguard._collect/_driver_overrides` 对位：
 * 读仓级/用户级 config（含 include），把 filter/diff/merge/alias 的
 * **可执行子段名**收集出来，生成一串 `key=value`（value 一律置空/false）。
 *
 * 为什么必须枚举而不是固定几条：`filter.<name>.clean` 的名字由仓库自己写，
 * 不枚举就完全不知道要覆盖哪个键 —— 这正是「仓库能偷偷执行程序」的那个洞。
 */
#ifndef AIGIT_GUARDCFG_H
#define AIGIT_GUARDCFG_H

#include <stddef.h>
#include <wchar.h>

/* 返回 malloc 的 UTF-8 "key=value" 字符串数组（不含 "-c"）；*n 是条数。
 * 读不到任何配置就返回 NULL / 0。调用方用 guard_free_pairs 释放。 */
char **guard_driver_pairs(const wchar_t *root, size_t *n);

/* 配置指纹（mtime+size）；0 = 读不到。用于运行中检测配置被改。 */
unsigned long long guard_config_stamp(const wchar_t *root);
void   guard_free_pairs(char **pairs, size_t n);

#endif
