/* catfile.h —— 常驻 `git cat-file --batch`（与 Python gitcat.py 对位）。
 *
 * 一个进程、一问一答、**不计数**（X-aigit-git-calls 不增加）。进程起不来或
 * 中途坏掉一律返回 0，调用方自行回退到 `git show`（那次会计数）。
 *
 * 只读对象；环境继承父进程，不受 guard 影响。
 */
#ifndef AIGIT_CATFILE_H
#define AIGIT_CATFILE_H

#include <stddef.h>
#include "util.h"

/* 成功（且对象存在）返回 1，*type 收对象类型（"commit"/"tag"/"blob"/"tree"）。
 * spec 可以是 sha、短 sha、HEAD:path 等 cat-file 认识的形式。
 * missing / ambiguous / 进程坏 一律返回 0。 */
int cat_read(const char *spec, buf *data, char *type, size_t typesz);

/* 带状态：status 1=ok、0=missing、-1=ambiguous、-2=进程坏/不可用。
 * ok 时 got 收 cat-file 回显的完整 sha（可为 NULL）。 */
int cat_read_ex(const char *spec, char *got, size_t gotsz, buf *data,
                char *type, size_t typesz, int *status);

#endif
