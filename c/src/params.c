#include "params.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* 在 query 里找 key，把值 percent-decode 成**带长度**的字节串（可含 NUL）。
 * 返回 1=找到（*out 为 malloc，调用方 free），0=没有，-1=内存不足。 */
int q_get_n(const char *query, const char *key, char **out, size_t *outlen) {
    if (out) *out = NULL;
    if (outlen) *outlen = 0;
    if (!query || !key) return 0;
    size_t klen = strlen(key);
    const char *p = query;
    while (p && *p) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        size_t this_klen = eq ? (size_t)(eq - p) : (size_t)(end - p);
        if (this_klen == klen && !strncmp(p, key, klen)) {
            const char *v = eq ? eq + 1 : end;
            /* 解码后绝不会比原文更长 */
            char *o = xmalloc((size_t)(end - v) + 1);
            size_t oi = 0;
            for (const char *q = v; q < end; q++) {
                char c = *q;
                if (c == '+') c = ' ';
                else if (c == '%' && q + 2 < end) {
                    int h = hexval(q[1]), l = hexval(q[2]);
                    if (h >= 0 && l >= 0) { c = (char)((h << 4) | l); q += 2; }
                }
                o[oi++] = c;
            }
            o[oi] = 0;
            if (out) *out = o; else free(o);
            if (outlen) *outlen = oi;
            return 1;
        }
        p = amp ? amp + 1 : NULL;
    }
    return 0;
}

/* 与 Python 版的对齐点：
 *   * 缺参数 → 0；参数非整数 → -1（调用方回 400），**不静默取默认**；
 *   * 解码结果含 NUL → -2（不能截断成另一个名字，那是路径走私）；
 *   * `+` 在 query 里是空格（urllib 同语义）；%XX 非法 → 原样保留。 */
int q_get(const char *query, const char *key, char *out, size_t outsz) {
    char *v = NULL;
    size_t n = 0;
    int got = q_get_n(query, key, &v, &n);
    if (got != 1) return got;
    if (memchr(v, '\0', n)) { free(v); return -2; }   /* 内嵌 NUL：拒绝，不截断 */
    if (n + 1 > outsz) { free(v); return -1; }
    memcpy(out, v, n + 1);
    free(v);
    return 1;
}

int q_int(const char *query, const char *key, long long dflt, long long *out) {
    char tmp[64];
    int got = q_get(query, key, tmp, sizeof(tmp));
    if (got == 0) { *out = dflt; return 0; }
    if (got < 0 || !tmp[0]) return -1;
    char *endp = NULL;
    long long v = strtoll(tmp, &endp, 10);
    if (!endp || *endp != 0) return -1;
    *out = v;
    return 1;
}

int q_bool(const char *query, const char *key, int dflt) {
    char tmp[16];
    int got = q_get(query, key, tmp, sizeof(tmp));
    if (got != 1) return dflt;
    return (!strcmp(tmp, "1") || !strcmp(tmp, "true") || !strcmp(tmp, "yes")) ? 1 : 0;
}
