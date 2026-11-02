/* util.h —— 动态缓冲、UTF-8/Wide 转换、字符串小工具。
 *
 * 只依赖 C11 + Win32。所有会失败的分配一律 abort（本地工具，宁可崩也不静默半截）。
 */
#ifndef AIGIT_UTIL_H
#define AIGIT_UTIL_H

#include <stddef.h>
#include <wchar.h>

typedef struct {
    char  *p;
    size_t n;
    size_t cap;
} buf;

void  buf_init(buf *b);
void  buf_free(buf *b);
void  buf_reset(buf *b);
void  buf_reserve(buf *b, size_t extra);
void  buf_append(buf *b, const void *data, size_t n);
void  buf_puts(buf *b, const char *s);
void  buf_putc(buf *b, char c);
void  buf_printf(buf *b, const char *fmt, ...);
/* 保证结尾有 NUL，并返回 c_str（不移动数据） */
char *buf_cstr(buf *b);

void *xmalloc(size_t n);
void *xrealloc(void *p, size_t n);
char *xstrdup(const char *s);
char *xstrndup(const char *s, size_t n);

char   *w2u8(const wchar_t *w);     /* 失败返回 NULL */
wchar_t *u82w(const char *s);       /* 失败返回 NULL */

/* 以 sep 切分（就地写 NUL），返回切片数组；*n 为数量。调用方 free 返回的数组。 */
char **split_inplace(char *s, char sep, size_t *n);

#endif
