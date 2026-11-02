#include "util.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

void *xmalloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) { fputs("aigit: out of memory\n", stderr); abort(); }
    return p;
}

void *xrealloc(void *p, size_t n) {
    void *q = realloc(p, n ? n : 1);
    if (!q) { fputs("aigit: out of memory\n", stderr); abort(); }
    return q;
}

char *xstrdup(const char *s) {
    size_t n = s ? strlen(s) : 0;
    char *d = xmalloc(n + 1);
    if (n) memcpy(d, s, n);
    d[n] = 0;
    return d;
}

char *xstrndup(const char *s, size_t n) {
    char *d = xmalloc(n + 1);
    if (n) memcpy(d, s, n);
    d[n] = 0;
    return d;
}

void buf_init(buf *b) { b->p = NULL; b->n = 0; b->cap = 0; }

void buf_free(buf *b) { free(b->p); b->p = NULL; b->n = b->cap = 0; }

void buf_reset(buf *b) { b->n = 0; if (b->p) b->p[0] = 0; }

void buf_reserve(buf *b, size_t extra) {
    if (b->n + extra + 1 <= b->cap) return;
    size_t cap = b->cap ? b->cap : 128;
    while (cap < b->n + extra + 1) cap *= 2;
    b->p = xrealloc(b->p, cap);
    b->cap = cap;
}

void buf_append(buf *b, const void *data, size_t n) {
    if (!n) return;
    buf_reserve(b, n);
    memcpy(b->p + b->n, data, n);
    b->n += n;
    b->p[b->n] = 0;
}

void buf_puts(buf *b, const char *s) { if (s) buf_append(b, s, strlen(s)); }

void buf_putc(buf *b, char c) { buf_append(b, &c, 1); }

void buf_printf(buf *b, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    va_list ap2;
    va_copy(ap2, ap);
    int need = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (need < 0) { va_end(ap2); return; }
    buf_reserve(b, (size_t)need);
    vsnprintf(b->p + b->n, (size_t)need + 1, fmt, ap2);
    va_end(ap2);
    b->n += (size_t)need;
}

char *buf_cstr(buf *b) {
    buf_reserve(b, 0);
    b->p[b->n] = 0;
    return b->p;
}

char *w2u8(const wchar_t *w) {
    if (!w) return NULL;
    int need = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (need <= 0) return NULL;
    char *out = xmalloc((size_t)need);
    if (!WideCharToMultiByte(CP_UTF8, 0, w, -1, out, need, NULL, NULL)) {
        free(out);
        return NULL;
    }
    return out;
}

wchar_t *u82w(const char *s) {
    if (!s) return NULL;
    int need = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    if (need <= 0) return NULL;
    wchar_t *out = xmalloc((size_t)need * sizeof(wchar_t));
    if (!MultiByteToWideChar(CP_UTF8, 0, s, -1, out, need)) {
        free(out);
        return NULL;
    }
    return out;
}

char **split_inplace(char *s, char sep, size_t *n) {
    size_t cap = 8, cnt = 0;
    char **out = xmalloc(cap * sizeof(char *));
    char *p = s;
    while (1) {
        if (cnt == cap) { cap *= 2; out = xrealloc(out, cap * sizeof(char *)); }
        out[cnt++] = p;
        char *q = strchr(p, sep);
        if (!q) break;
        *q = 0;
        p = q + 1;
    }
    *n = cnt;
    return out;
}
