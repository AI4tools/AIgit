/* status.h —— `git status --porcelain=v2 --branch -z` 的解析与 JSON 渲染。
 * 解析口径与 server.py::parse_porcelain_v2 逐条对齐（含 v2 的 '.'→空格 归一）。
 */
#ifndef AIGIT_STATUS_H
#define AIGIT_STATUS_H

#include <stddef.h>
#include "util.h"

typedef struct {
    char *path;
    char  code;      /* 状态字符：M/A/D/R/C/U/? */
    int   staged;    /* 1=暂存侧, 0=未暂存侧 */
} sitem;

typedef struct {
    char  *branch;   /* 分支名；游离 HEAD 时为 "HEAD" */
    sitem *staged;   size_t ns;
    sitem *unstaged; size_t nu;
    sitem *conf;     size_t nc;
} status_t;

/* 跑 git status 并解析。成功 0；失败 -1 且 *err 为可读错误。 */
int  status_compute(status_t *st, char **err);
void status_free(status_t *st);
/* 渲染成 {ok,branch,root,staged,unstaged,conflicted,hasStaged} */
void status_json(buf *o, const status_t *st, const char *root_u8);

#endif
