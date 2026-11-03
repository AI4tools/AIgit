/* indexfile.h —— `.git/index` 直读（只服务两个明确需求）：
 *   1. 写端点的 tracked 集合（Python `_tracked_for_filter` 的快路径）；
 *   2. 冲突清单的「有没有未合并条目」判定（有 → 才 spawn `ls-files -u`）。
 *
 * 不认识就返回 0，调用方回退 git 命令 —— 与 Python `parse_index() -> None` 同义。
 */
#ifndef AIGIT_INDEXFILE_H
#define AIGIT_INDEXFILE_H

#include <stddef.h>

typedef struct {
    char **paths;      /* 全部条目（未合并路径会出现多次，与 index 同） */
    size_t n;
    int has_unmerged;  /* 存在 stage != 0 的条目 */
} idx_names;

/* 1 = 解析成功（*out 已初始化，可能为空）；0 = 不认识（worktree/reftable/
 * split-index/v4/GIT_INDEX_FILE 等）。 */
int  idx_read(idx_names *out);
void idx_free(idx_names *out);
int  idx_has(const idx_names *ix, const char *path);

#endif
