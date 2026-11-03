/* read_basic.c —— M1 基础读端点：/api/branches /api/log /api/file /api/diff。
 *
 * 对位关系（Python 版 D:\project\aigit\server.py）：
 *   branches -> Repo.branches()          （gitrefs.head_names/head_state 快路径，0 spawn）
 *   log      -> Repo.log(limit)          （1 spawn: git log）
 *   file     -> /api/file 处理器          （gitcat 常驻 cat-file，0 spawn；缺失回退 git show）
 *   diff     -> /api/diff + Repo.diff()   （1 spawn: git diff；path_known 走索引直读）
 *
 * 为什么这里还要抄一份 refs / index / cat-file：Python 版的这几个端点靠它们把
 * spawn 数压到 0，而 X-aigit-git-calls 是**对等判据的一部分**（tests/conformance_basic.py
 * 逐端点比这个头）。走 git_run 的调用才计数；cat-file 常驻进程自己 CreateProcessW，
 * 与 Python 的 gitcat.Popen 一样**不计数**。
 *
 * 本文件只写 static 小工具，不改动其它模块。
 */
#include "routes.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "catfile.h"
#include "git.h"
#include "http.h"
#include "json.h"
#include "params.h"
#include "paths.h"
#include "util.h"

/* ================================================================ 通用小工具 */

typedef struct { char **v; size_t n, cap; } slist;

static void slist_init(slist *s) { s->v = NULL; s->n = s->cap = 0; }

static void slist_push(slist *s, char *own) {
    if (s->n == s->cap) {
        s->cap = s->cap ? s->cap * 2 : 8;
        s->v = xrealloc(s->v, s->cap * sizeof(char *));
    }
    s->v[s->n++] = own;
}

static int slist_has(const slist *s, const char *x) {
    for (size_t i = 0; i < s->n; i++)
        if (!strcmp(s->v[i], x)) return 1;
    return 0;
}

static void slist_free(slist *s) {
    for (size_t i = 0; i < s->n; i++) free(s->v[i]);
    free(s->v);
    s->v = NULL; s->n = s->cap = 0;
}

static int cmp_str(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

static int str_starts(const char *s, const char *p) {
    size_t n = strlen(p);
    return strncmp(s, p, n) == 0;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* ================================================================ UTF-8 解码
 *
 * Python 侧所有 git 输出都经过 `text=True, encoding="utf-8", errors="replace"`，
 * 也就是**先按 UTF-8 解码（非法序列替换成 U+FFFD）再做 universal newlines**
 * （\r\n -> \n、孤立 \r -> \n）。两条都不能省：
 *   * 二进制文件里的 0xFF 不替换就写出非法 JSON（Python 那边是 \ufffd）；
 *   * diff 输出的 CRLF 行尾会被 Python 的文本模式吃掉（实测见 probe_crlf2.py），
 *     不翻译就会在 "diff" 字段里多出 \r。
 *
 * 替换粒度按 Unicode「最大子部分」规则，与 CPython 实测一致：
 *   E0 80 80 -> 3 个 FFFD；ED A0 80 -> 3；F4 90 80 80 -> 4；C0 AF -> 2；
 *   E4 B8（截断）-> 1；F0 9F 92（截断）-> 1。
 */

static int u8_one(const unsigned char *p, size_t n, size_t *used) {
    unsigned c = p[0];
    if (c < 0x80) { *used = 1; return 1; }
    size_t need;
    unsigned lo = 0x80, hi = 0xBF;
    if (c >= 0xC2 && c <= 0xDF) {
        need = 2;
    } else if (c >= 0xE0 && c <= 0xEF) {
        need = 3;
        if (c == 0xE0) lo = 0xA0;
        else if (c == 0xED) hi = 0x9F;
    } else if (c >= 0xF0 && c <= 0xF4) {
        need = 4;
        if (c == 0xF0) lo = 0x90;
        else if (c == 0xF4) hi = 0x8F;
    } else {
        *used = 1;
        return 0;
    }
    if (n < 2 || p[1] < lo || p[1] > hi) { *used = 1; return 0; }
    size_t i = 2;
    while (i < need) {
        if (n <= i || p[i] < 0x80 || p[i] > 0xBF) { *used = i; return 0; }
        i++;
    }
    *used = need;
    return 1;
}

/* 解一个码点（调用前保证 u8_one 成功）。 */
static unsigned u8_cp(const unsigned char *p, size_t used) {
    if (used == 1) return p[0];
    if (used == 2) return ((unsigned)(p[0] & 0x1F) << 6) | (p[1] & 0x3F);
    if (used == 3)
        return ((unsigned)(p[0] & 0x0F) << 12) | ((unsigned)(p[1] & 0x3F) << 6) | (p[2] & 0x3F);
    return ((unsigned)(p[0] & 0x07) << 18) | ((unsigned)(p[1] & 0x3F) << 12) |
           ((unsigned)(p[2] & 0x3F) << 6) | (p[3] & 0x3F);
}

static int utf8_valid(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) { i++; continue; }
        size_t used = 0;
        if (!u8_one((const unsigned char *)s + i, n - i, &used)) return 0;
        i += used;
    }
    return 1;
}

static void append_decoded(buf *o, const char *data, size_t n, int nl) {
    const unsigned char *p = (const unsigned char *)data;
    size_t i = 0;
    while (i < n) {
        unsigned char c = p[i];
        if (c < 0x80) {
            if (nl && c == '\r') {
                buf_putc(o, '\n');
                i++;
                if (i < n && p[i] == '\n') i++;
            } else {
                buf_putc(o, (char)c);
                i++;
            }
            continue;
        }
        size_t used = 0;
        if (u8_one(p + i, n - i, &used)) {
            buf_append(o, p + i, used);
            i += used;
        } else {
            buf_puts(o, "\xEF\xBF\xBD");
            i += used ? used : 1;
        }
    }
}

/* 取 query 里的一个值（percent-decode）。
 *
 * 与 params.c 的 q_get 的差别只有两点，但两点都必须：
 *   1. **带长度**：`%00` 会解出一个 NUL 字节，而 Python 的 unquote 保留它、
 *      随后 resolve_in_root 明确拒绝（"路径里不能包含 NUL 字符"）。
 *      用 NUL 结尾的缓冲会把 `a.txt%00x` 静默截断成 `a.txt` —— 那是路径走私。
 *   2. 空值 / 无值 -> 没找到（parse_qs(keep_blank_values=False) 的行为）。
 */
typedef struct { char *p; size_t n; } qval;

static int q_val(const char *query, const char *key, qval *out) {
    out->p = NULL;
    out->n = 0;
    if (!query) return 0;
    size_t klen = strlen(key);
    const char *p = query;
    while (p && *p) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        size_t this_klen = eq ? (size_t)(eq - p) : (size_t)(end - p);
        if (this_klen == klen && !strncmp(p, key, klen)) {
            const char *v = eq ? eq + 1 : end;
            buf b;
            buf_init(&b);
            for (const char *q = v; q < end; q++) {
                char c = *q;
                if (c == '+') {
                    c = ' ';
                } else if (c == '%' && q + 2 < end) {
                    int h = hexval(q[1]), l = hexval(q[2]);
                    if (h >= 0 && l >= 0) {
                        c = (char)((h << 4) | l);
                        q += 2;
                    }
                }
                buf_putc(&b, c);
            }
            buf_cstr(&b);
            if (b.n == 0) { buf_free(&b); return 0; }
            out->p = b.p;
            out->n = b.n;
            return 1;
        }
        p = amp ? amp + 1 : NULL;
    }
    return 0;
}

/* 按 Python unquote(errors="replace") 的口径：非法 UTF-8 -> U+FFFD，保留 NUL。 */
static char *u8_replace(const char *data, size_t n) {
    buf b;
    buf_init(&b);
    append_decoded(&b, data, n, 0);
    buf_cstr(&b);
    char *out = xstrndup(b.p ? b.p : "", b.n);
    buf_free(&b);
    return out;
}

/* JSON 字符串（带长度）：内容里可能有 NUL（二进制文件），不能靠 strlen。 */
static void json_str_n(buf *o, const char *s, size_t n) {
    buf_putc(o, '"');
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"':  buf_puts(o, "\\\""); break;
        case '\\': buf_puts(o, "\\\\"); break;
        case '\n': buf_puts(o, "\\n"); break;
        case '\r': buf_puts(o, "\\r"); break;
        case '\t': buf_puts(o, "\\t"); break;
        case '\b': buf_puts(o, "\\b"); break;
        case '\f': buf_puts(o, "\\f"); break;
        default:
            if (c < 0x20) buf_printf(o, "\\u%04x", c);
            else buf_putc(o, (char)c);
        }
    }
    buf_putc(o, '"');
}

/* Python repr() 的常见形态（只用于错误文案里的值回显）。 */
static char *py_repr_n(const char *s, size_t n) {
    int has_sq = memchr(s, '\'', n) != NULL;
    int has_dq = memchr(s, '"', n) != NULL;
    char quote = (has_sq && !has_dq) ? '"' : '\'';
    buf b;
    buf_init(&b);
    buf_putc(&b, quote);
    for (size_t i = 0; i < n; i++) {
        unsigned c = (unsigned char)s[i];
        if (c == (unsigned char)quote || c == '\\') {
            buf_putc(&b, '\\');
            buf_putc(&b, (char)c);
        } else if (c == '\n') buf_puts(&b, "\\n");
        else if (c == '\r') buf_puts(&b, "\\r");
        else if (c == '\t') buf_puts(&b, "\\t");
        else if (c < 0x20 || c == 0x7f) buf_printf(&b, "\\x%02x", c);
        else buf_putc(&b, (char)c);
    }
    buf_putc(&b, quote);
    buf_cstr(&b);
    char *out = xstrdup(b.p);
    buf_free(&b);
    return out;
}

/* Python int() 的近似：允许前后空白与正负号、数字之间的单个下划线，
 * 其余必须是十进制数字。 */
static int parse_int(const char *s, long long *out) {
    while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r' || *s == '\v' || *s == '\f') s++;
    if (!*s) return 0;
    buf clean;
    buf_init(&clean);
    for (size_t i = 0; s[i]; i++) {
        if (s[i] == '_') {
            /* int() 只认「数字 _ 数字」 */
            if (i == 0 || !isdigit((unsigned char)s[i - 1]) || !isdigit((unsigned char)s[i + 1])) {
                buf_free(&clean);
                return 0;
            }
            continue;
        }
        buf_putc(&clean, s[i]);
    }
    buf_cstr(&clean);
    char *end = NULL;
    long long v = strtoll(clean.p, &end, 10);
    int ok = end && end != clean.p;
    if (ok) {
        while (*end == ' ' || *end == '\t' || *end == '\n' || *end == '\r' ||
               *end == '\v' || *end == '\f') end++;
        if (*end) ok = 0;
    }
    buf_free(&clean);
    if (!ok) return 0;
    *out = v;
    return 1;
}

/* ================================================================ 路径判定 */

#define WBUF 4096

enum { PK_NONE = 0, PK_FILE = 1, PK_DIR = 2, PK_LINK = 3 };

/* root + "\" + rel（rel 是 '/' 分隔的 UTF-8） */
static wchar_t *rel_w(const char *rel) {
    const wchar_t *root = git_root();
    wchar_t *w = u82w(rel);
    if (!w) return NULL;
    for (wchar_t *p = w; *p; p++) if (*p == L'/') *p = L'\\';
    size_t n = wcslen(root) + 1 + wcslen(w) + 1;
    wchar_t *out = xmalloc(n * sizeof(wchar_t));
    _snwprintf(out, n, L"%ls\\%ls", root, w);
    free(w);
    return out;
}

/* exists()/is_dir()/is_file() 的合并判定（跟随符号链接，与 pathlib 一致；
 * 悬空链接单独给 PK_LINK，因为 path_known 用 is_symlink() 判存在）。 */
static int path_kind(const char *rel) {
    wchar_t *p = rel_w(rel);
    if (!p) return PK_NONE;
    DWORD a = GetFileAttributesW(p);
    int k = PK_NONE;
    if (a != INVALID_FILE_ATTRIBUTES) {
        k = (a & FILE_ATTRIBUTE_DIRECTORY) ? PK_DIR : PK_FILE;
    } else {
        HANDLE h = CreateFileW(p, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL, OPEN_EXISTING,
                               FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
        if (h != INVALID_HANDLE_VALUE) { k = PK_LINK; CloseHandle(h); }
    }
    free(p);
    return k;
}

static int read_bytes_w(const wchar_t *path, buf *out) {
    HANDLE h = CreateFileW(path, GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    char tmp[65536];
    DWORD got = 0;
    while (ReadFile(h, tmp, sizeof(tmp), &got, NULL) && got > 0) buf_append(out, tmp, got);
    CloseHandle(h);
    return 1;
}

/* Python 的 rel_in_root 是 `resolve_in_root(rel).relative_to(root).as_posix()`，
 * 也就是 **Path.resolve() 之后的路径**：已存在部分的磁盘真实大小写、联接/符号
 * 链接解析、`\` -> `/`。paths.c 只做了越界校验、返回的是请求里的写法，于是
 * `/api/file?file=A.TXT` 在两边给出不同的 `path`（Python 是 "a.txt"），
 * 而 `HEAD:A.TXT` 在 Windows 上取不到对象（tree 里的名字是 a.txt）。
 * 这里按「最长已存在祖先的真实路径 + 剩余后缀」补一次，与 Python 对齐；
 * 任何一步取不到就原样返回（paths.c 已经做过越界拒绝，不会因此放行仓外路径）。 */

static const wchar_t *strip_ns(const wchar_t *p) {
    if (p && p[0] == L'\\' && p[1] == L'\\' && p[2] == L'?' && p[3] == L'\\') return p + 4;
    return p;
}

static char *normalize_rel(const char *rel) {
    wchar_t *full = rel_w(rel);
    if (!full) return xstrdup(rel);
    wchar_t *cur = _wcsdup(full);
    wchar_t *suffix = _wcsdup(L"");
    wchar_t *resolved = NULL;

    for (int depth = 0; depth < 128 && !resolved; depth++) {
        HANDLE h = CreateFileW(cur, 0,
                               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
        if (h == INVALID_HANDLE_VALUE)
            h = CreateFileW(cur, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED);
            if (need) {
                wchar_t *rp = xmalloc(((size_t)need + 1) * sizeof(wchar_t));
                DWORD got = GetFinalPathNameByHandleW(h, rp, need, FILE_NAME_NORMALIZED);
                if (got) {
                    const wchar_t *p = strip_ns(rp);
                    buf j;
                    buf_init(&j);
                    buf_append(&j, p, wcslen(p) * sizeof(wchar_t));
                    if (suffix[0]) {
                        buf_append(&j, L"\\", sizeof(wchar_t));
                        buf_append(&j, suffix, wcslen(suffix) * sizeof(wchar_t));
                    }
                    wchar_t z = 0;
                    buf_append(&j, &z, sizeof(wchar_t));
                    resolved = (wchar_t *)j.p;
                }
                free(rp);
            }
            CloseHandle(h);
            if (resolved) break;
        }
        wchar_t *slash = wcsrchr(cur, L'\\');
        if (!slash || slash == cur) break;
        size_t tl = wcslen(slash + 1), sl = wcslen(suffix);
        wchar_t *ns = xmalloc((tl + (sl ? sl + 1 : 0) + 1) * sizeof(wchar_t));
        wcscpy(ns, slash + 1);
        if (sl) { wcscat(ns, L"\\"); wcscat(ns, suffix); }
        free(suffix);
        suffix = ns;
        *slash = 0;
    }

    char *out = NULL;
    const wchar_t *root = git_root();
    size_t rl = wcslen(root);
    if (resolved && _wcsnicmp(resolved, root, rl) == 0 && resolved[rl] == L'\\' &&
        resolved[rl + 1]) {
        wchar_t *r = resolved + rl + 1;
        for (wchar_t *p = r; *p; p++) if (*p == L'\\') *p = L'/';
        out = w2u8(r);
    }
    if (!out) out = xstrdup(rel);
    free(resolved);
    free(suffix);
    free(cur);
    free(full);
    return out;
}

/* ================================================================ refs 直读 */

typedef struct { char *name, *val; } refpair;
typedef struct { refpair *v; size_t n, cap; } refmap;

static void refmap_put(refmap *m, const char *name, const char *val) {
    for (size_t i = 0; i < m->n; i++) {
        if (!strcmp(m->v[i].name, name)) {
            free(m->v[i].val);
            m->v[i].val = val ? xstrdup(val) : NULL;
            return;
        }
    }
    if (m->n == m->cap) {
        m->cap = m->cap ? m->cap * 2 : 16;
        m->v = xrealloc(m->v, m->cap * sizeof(refpair));
    }
    m->v[m->n].name = xstrdup(name);
    m->v[m->n].val = val ? xstrdup(val) : NULL;
    m->n++;
}

static const char *refmap_get(const refmap *m, const char *name) {
    for (size_t i = 0; i < m->n; i++)
        if (!strcmp(m->v[i].name, name)) return m->v[i].val;
    return NULL;
}

static void refmap_free(refmap *m) {
    for (size_t i = 0; i < m->n; i++) { free(m->v[i].name); free(m->v[i].val); }
    free(m->v);
    m->v = NULL; m->n = m->cap = 0;
}

static int is_sha_text(const char *t) {
    size_t n = strlen(t);
    if (n != 40 && n != 64) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = t[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
            return 0;
    }
    return 1;
}

/* gitrefs.gitdir()：不确定就返回 0（调用方回退 git）。 */
static int get_gitdir(wchar_t *out, size_t outsz) {
    const char *gd = getenv("GIT_DIR");
    const char *gcd = getenv("GIT_COMMON_DIR");
    if (gd && *gd) return 0;
    if (gcd && *gcd) return 0;
    const wchar_t *root = git_root();
    _snwprintf(out, outsz, L"%ls\\.git", root);
    DWORD a = GetFileAttributesW(out);
    if (a == INVALID_FILE_ATTRIBUTES || !(a & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    wchar_t rt[WBUF];
    _snwprintf(rt, WBUF, L"%ls\\reftable", out);
    DWORD a2 = GetFileAttributesW(rt);
    if (a2 != INVALID_FILE_ATTRIBUTES && (a2 & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    return 1;
}

/* packed-refs -> map；读不到是空，格式不对 -> 0（回退）。 */
static int packed_load(const wchar_t *gd, refmap *m) {
    wchar_t path[WBUF];
    _snwprintf(path, WBUF, L"%ls\\packed-refs", gd);
    DWORD a = GetFileAttributesW(path);
    if (a == INVALID_FILE_ATTRIBUTES) return 1;
    buf raw;
    buf_init(&raw);
    if (!read_bytes_w(path, &raw)) { buf_free(&raw); return 0; }
    if (!utf8_valid(raw.p ? raw.p : "", raw.n)) { buf_free(&raw); return 0; }
    char *p = raw.p ? raw.p : "";
    char *end = p + raw.n;
    while (p < end) {
        char *nl = memchr(p, '\n', (size_t)(end - p));
        char *line_end = nl ? nl : end;
        char *q = line_end;
        while (q > p && (q[-1] == '\r' || q[-1] == '\n')) q--;
        size_t len = (size_t)(q - p);
        if (len && p[0] != '#' && p[0] != '^') {
            char *sp = memchr(p, ' ', len);
            if (!sp) { buf_free(&raw); return 0; }
            char *sha = xstrndup(p, (size_t)(sp - p));
            size_t restlen = len - (size_t)(sp - p) - 1;
            char *name = xstrndup(sp + 1, restlen);
            if (!is_sha_text(sha) || !str_starts(name, "refs/")) {
                free(sha); free(name); buf_free(&raw); return 0;
            }
            refmap_put(m, name, sha);
            free(sha); free(name);
        }
        p = nl ? nl + 1 : end;
    }
    buf_free(&raw);
    return 1;
}

/* 递归走 <gd>/<prefix>，把 rel(相对 gd, '/' 分隔) 写进 map。
 * Python os.walk(followlinks=False) 不跟随联接，这里同样跳过重解析点。 */
static int walk_refs(const wchar_t *dir, const wchar_t *relw, refmap *m, int want_content) {
    size_t dl = wcslen(dir);
    wchar_t *pat = xmalloc((dl + 3) * sizeof(wchar_t));
    _snwprintf(pat, dl + 3, L"%ls\\*", dir);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pat, &fd);
    free(pat);
    if (h == INVALID_HANDLE_VALUE) return 1;

    int ok = 1;
    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
        size_t fl = wcslen(fd.cFileName);
        size_t nl = dl + 1 + fl + 1;
        wchar_t *full = xmalloc(nl * sizeof(wchar_t));
        _snwprintf(full, nl, L"%ls\\%ls", dir, fd.cFileName);

        size_t rl = wcslen(relw);
        size_t nn = rl + 1 + fl + 1;
        wchar_t *nrel = xmalloc(nn * sizeof(wchar_t));
        _snwprintf(nrel, nn, L"%ls/%ls", relw, fd.cFileName);

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
                if (!walk_refs(full, nrel, m, want_content)) ok = 0;
            }
        } else if (!want_content) {
            char *rel = w2u8(nrel);
            if (rel) { refmap_put(m, rel, NULL); free(rel); }
            else ok = 0;
        } else {
            buf raw;
            buf_init(&raw);
            if (!read_bytes_w(full, &raw)) {
                ok = 0;
            } else {
                char *rel = w2u8(nrel);
                if (!rel || !utf8_valid(raw.p ? raw.p : "", raw.n)) {
                    ok = 0;
                } else {
                    /* Python 的 .strip()：去掉两端空白 */
                    char *s = raw.p ? raw.p : "";
                    size_t n = raw.n;
                    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\n' ||
                                 s[n - 1] == '\r' || s[n - 1] == '\v' || s[n - 1] == '\f')) n--;
                    size_t st = 0;
                    while (st < n && (s[st] == ' ' || s[st] == '\t' || s[st] == '\n' ||
                                      s[st] == '\r' || s[st] == '\v' || s[st] == '\f')) st++;
                    char *val = xstrndup(s + st, n - st);
                    refmap_put(m, rel, val);
                    free(val);
                }
                free(rel);
            }
            buf_free(&raw);
        }
        free(full);
        free(nrel);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

static int loose_load(const wchar_t *gd, const char *prefix, refmap *m, int want_content) {
    wchar_t *base = xmalloc((wcslen(gd) + strlen(prefix) + 2) * sizeof(wchar_t));
    wchar_t *pw = u82w(prefix);
    if (!pw) { free(base); return 0; }
    for (wchar_t *p = pw; *p; p++) if (*p == L'/') *p = L'\\';
    _snwprintf(base, wcslen(gd) + strlen(prefix) + 2, L"%ls\\%ls", gd, pw);
    free(pw);
    DWORD a = GetFileAttributesW(base);
    int ok = 1;
    if (a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY)) {
        wchar_t *relw = u82w(prefix);
        if (relw) {
            ok = walk_refs(base, relw, m, want_content);
            free(relw);
        } else {
            ok = 0;
        }
    }
    free(base);
    return ok;
}

/* for-each-ref refs/heads 的顺序 = 按名字排序。 */
static int head_names(slist *out) {
    wchar_t gd[WBUF];
    if (!get_gitdir(gd, WBUF)) return 0;
    refmap m;
    memset(&m, 0, sizeof(m));
    if (!packed_load(gd, &m)) { refmap_free(&m); return 0; }
    /* 只留 refs/heads/ 下的 */
    refmap f;
    memset(&f, 0, sizeof(f));
    for (size_t i = 0; i < m.n; i++)
        if (str_starts(m.v[i].name, "refs/heads/"))
            refmap_put(&f, m.v[i].name, m.v[i].val);
    refmap_free(&m);
    if (!loose_load(gd, "refs/heads", &f, 1)) { refmap_free(&f); return 0; }
    const size_t pn = strlen("refs/heads/");
    for (size_t i = 0; i < f.n; i++) slist_push(out, xstrdup(f.v[i].name + pn));
    refmap_free(&f);
    if (out->n > 1) qsort(out->v, out->n, sizeof(char *), cmp_str);
    return 1;
}

typedef struct {
    int kind;    /* 0 未知, 1 branch, 2 detached, 3 unborn */
    char *name;
} headst;

static int head_state(headst *st) {
    wchar_t gd[WBUF];
    if (!get_gitdir(gd, WBUF)) return 0;
    wchar_t hp[WBUF];
    _snwprintf(hp, WBUF, L"%ls\\HEAD", gd);
    buf raw;
    buf_init(&raw);
    if (!read_bytes_w(hp, &raw) || !utf8_valid(raw.p ? raw.p : "", raw.n)) {
        buf_free(&raw);
        return 0;
    }
    char *s = raw.p ? raw.p : "";
    size_t n = raw.n;
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\n' ||
                 s[n - 1] == '\r' || s[n - 1] == '\v' || s[n - 1] == '\f')) n--;
    size_t b = 0;
    while (b < n && (s[b] == ' ' || s[b] == '\t' || s[b] == '\n' ||
                     s[b] == '\r' || s[b] == '\v' || s[b] == '\f')) b++;
    char *text = xstrndup(s + b, n - b);

    int rc = 1;
    if (str_starts(text, "ref: ")) {
        char *ref = text + 5;
        while (*ref == ' ' || *ref == '\t') ref++;
        char *re = ref + strlen(ref);
        while (re > ref && (re[-1] == ' ' || re[-1] == '\t')) re--;
        *re = 0;
        if (!str_starts(ref, "refs/heads/") || !ref[strlen("refs/heads/")]) {
            rc = 0;
        } else {
            const char *name = ref + strlen("refs/heads/");
            /* resolve(): loose 优先，再 packed */
            char *sha = NULL;
            refmap l;
            memset(&l, 0, sizeof(l));
            if (loose_load(gd, "refs", &l, 1)) {
                const char *v = refmap_get(&l, ref);
                if (v && *v) sha = is_sha_text(v) ? xstrdup(v) : NULL;
            }
            refmap_free(&l);
            if (!sha) {
                refmap p;
                memset(&p, 0, sizeof(p));
                if (packed_load(gd, &p)) {
                    const char *v = refmap_get(&p, ref);
                    if (v) sha = xstrdup(v);
                }
                refmap_free(&p);
            }
            if (!sha) {
                st->kind = 3;
                st->name = xstrdup(name);
            } else if (!is_sha_text(sha)) {
                rc = 0;
            } else {
                st->kind = 1;
                st->name = xstrdup(name);
            }
            free(sha);
        }
    } else if (is_sha_text(text)) {
        st->kind = 2;
    } else {
        rc = 0;
    }
    free(text);
    buf_free(&raw);
    return rc;
}

/* ================================================================ index 直读 */

static unsigned be32(const unsigned char *p) {
    return ((unsigned)p[0] << 24) | ((unsigned)p[1] << 16) | ((unsigned)p[2] << 8) | p[3];
}
static unsigned be16(const unsigned char *p) {
    return ((unsigned)p[0] << 8) | p[1];
}

/* 1 = 已知（可能为空集），0 = 不认识（调用方回退 git ls-files）。 */
static int index_names(slist *out) {
    const char *gif = getenv("GIT_INDEX_FILE");
    if (gif && *gif) return 0;
    wchar_t gd[WBUF];
    if (!get_gitdir(gd, WBUF)) return 0;
    wchar_t ip[WBUF];
    _snwprintf(ip, WBUF, L"%ls\\index", gd);
    DWORD a = GetFileAttributesW(ip);
    if (a == INVALID_FILE_ATTRIBUTES) return 1;         /* 空索引 */
    buf raw;
    buf_init(&raw);
    if (!read_bytes_w(ip, &raw)) { buf_free(&raw); return 0; }
    const unsigned char *r = (const unsigned char *)(raw.p ? raw.p : "");
    size_t n = raw.n;
    int ok = 1;
    if (n < 12 || memcmp(r, "DIRC", 4)) {
        ok = 0;
    } else {
        unsigned ver = be32(r + 4);
        unsigned count = be32(r + 8);
        if (ver != 2 && ver != 3) {
            ok = 0;
        } else {
            size_t off = 12;
            for (unsigned i = 0; i < count && ok; i++) {
                if (off + 62 > n) { ok = 0; break; }
                unsigned flags = be16(r + off + 60);
                size_t name_len = flags & 0x0FFF;
                size_t p = off + 62;
                if (ver >= 3 && (flags & 0x4000)) p += 2;
                if (p > n) { ok = 0; break; }
                size_t nstart = p, nlen = 0, entry_end = 0;
                if (name_len < 0xFFF) {
                    size_t end = p + name_len;
                    if (end >= n || r[end] != 0) { ok = 0; break; }
                    nlen = name_len;
                    entry_end = end + 1;
                } else {
                    size_t end = p;
                    while (end < n && r[end] != 0) end++;
                    if (end >= n) { ok = 0; break; }
                    nlen = end - p;
                    entry_end = end + 1;
                }
                if (!nlen || !utf8_valid((const char *)r + nstart, nlen)) { ok = 0; break; }
                slist_push(out, xstrndup((const char *)r + nstart, nlen));
                off = off + ((entry_end - off + 7) & ~(size_t)7);
            }
            while (ok && off + 8 <= n) {
                const unsigned char *sig = r + off;
                unsigned size = be32(r + off + 4);
                if (!memcmp(sig, "link", 4) || !memcmp(sig, "sdir", 4)) { ok = 0; break; }
                off += 8 + size;
            }
        }
    }
    buf_free(&raw);
    return ok;
}

/* ================================================================ git 输出解码 */

static void run_text(const char *const args[], buf *out_text, int *rc) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int r = git_run(args, NULL, 0, &o, &e);
    if (rc) *rc = r;
    buf_init(out_text);
    append_decoded(out_text, o.p ? o.p : "", o.n, 1);
    buf_cstr(out_text);
    buf_free(&o);
    buf_free(&e);
}

/* ================================================================ /api/file 支撑 */

static const char *REF_HINT =
    "ref 不合法:只允许 git rev-spec 的常见写法"
    "(字母数字与 _ . / @ ~ ^ { } + -),首字符不能是 -,不能含 : 与空白";

static int is_word_ascii(unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}

/* Python 的 `\w` 对 str 是 Unicode 词字符（字母/数字/下划线）。
 * 完整 Unicode 分类表太重，这里用**区间白名单**近似：宁可少收（把 Python 接受的
 * 生僻字母判成不合法），也不要多收（让 `。` 这种标点混进 ref 去问 git）。 */
static int u8_is_word(unsigned cp) {
    if (cp < 0x80) return is_word_ascii((unsigned char)cp);
    if (cp >= 0x00C0 && cp <= 0x024F && cp != 0x00D7 && cp != 0x00F7) return 1; /* Latin 补充/扩展 */
    if (cp >= 0x0370 && cp <= 0x03FF) return 1;      /* Greek */
    if (cp >= 0x0400 && cp <= 0x04FF) return 1;      /* Cyrillic */
    if (cp >= 0x0531 && cp <= 0x058F) return 1;      /* Armenian */
    if (cp >= 0x05D0 && cp <= 0x05EA) return 1;      /* Hebrew 字母 */
    if (cp >= 0x0620 && cp <= 0x06FF) return 1;      /* Arabic */
    if (cp >= 0x0900 && cp <= 0x0DFF) return 1;      /* 天城文等 Indic */
    if (cp >= 0x3040 && cp <= 0x30FF) return 1;      /* 平假名/片假名 */
    if (cp >= 0x3400 && cp <= 0x4DBF) return 1;      /* CJK 扩展 A */
    if (cp >= 0x4E00 && cp <= 0x9FFF) return 1;      /* CJK */
    if (cp >= 0xAC00 && cp <= 0xD7AF) return 1;      /* 谚文音节 */
    if (cp >= 0xF900 && cp <= 0xFAFF) return 1;      /* CJK 兼容 */
    if (cp >= 0xFF10 && cp <= 0xFF19) return 1;      /* 全角数字 */
    if (cp >= 0xFF21 && cp <= 0xFF3A) return 1;      /* 全角大写 */
    if (cp >= 0xFF41 && cp <= 0xFF5A) return 1;      /* 全角小写 */
    if (cp >= 0x20000 && cp <= 0x2FA1F) return 1;    /* CJK 扩展 B+ */
    return 0;
}

/* 与 Python _REF_RE.fullmatch 对齐。 */
static int ref_ok(const char *ref) {
    if (!ref || !*ref) return 0;
    if (strstr(ref, "..")) return 0;
    const unsigned char *p = (const unsigned char *)ref;
    size_t i = 0;
    int first = 1;
    size_t total = strlen(ref);
    while (i < total) {
        unsigned char c = p[i];
        if (c >= 0x80) {
            size_t used = 0;
            if (!u8_one(p + i, total - i, &used)) return 0;
            if (!u8_is_word(u8_cp(p + i, used))) return 0;
            i += used;
            first = 0;
            continue;
        }
        int good;
        if (first) good = is_word_ascii(c) || c == '@' || c == '^' || c == '~';
        else good = is_word_ascii(c) || strchr("@^~{}+./-", c) != NULL;
        if (!good) return 0;
        i++;
        first = 0;
    }
    return 1;
}

/* Python show_file()：cat-file 命中 blob -> 原样（不做换行翻译）；
 * 否则 `git show <ref>:<rel>`（文本模式：替换 + 换行翻译）。空内容 -> None。
 * 返回 1 = 有内容（写进 out，可能含 NUL，长度看 out->n），0 = None。 */
static int show_file(const char *rel, const char *ref, buf *out) {
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, ref);
    buf_putc(&spec, ':');
    buf_puts(&spec, rel);
    buf_cstr(&spec);

    buf data;
    buf_init(&data);
    char type[32];
    int got = 0;
    if (cat_read(spec.p, &data, type, sizeof(type)) && !strcmp(type, "blob")) {
        append_decoded(out, data.p ? data.p : "", data.n, 0);
        buf_cstr(out);
        got = out->n > 0;
    } else {
        const char *args[] = { "show", spec.p, NULL };
        buf t;
        run_text(args, &t, NULL);
        if (t.n) {
            buf_append(out, t.p, t.n);
            buf_cstr(out);
            got = 1;
        }
        buf_free(&t);
    }
    buf_free(&data);
    buf_free(&spec);
    return got;
}

static int staged_file(const char *rel, buf *out) {
    buf spec;
    buf_init(&spec);
    buf_putc(&spec, ':');
    buf_puts(&spec, rel);
    buf_cstr(&spec);

    buf data;
    buf_init(&data);
    char type[32];
    int got = 0;
    if (cat_read(spec.p, &data, type, sizeof(type)) && !strcmp(type, "blob")) {
        append_decoded(out, data.p ? data.p : "", data.n, 0);
        buf_cstr(out);
        got = out->n > 0;
    } else {
        const char *args[] = { "show", spec.p, NULL };
        buf t;
        run_text(args, &t, NULL);
        if (t.n) {
            buf_append(out, t.p, t.n);
            buf_cstr(out);
            got = 1;
        }
        buf_free(&t);
    }
    buf_free(&data);
    buf_free(&spec);
    return got;
}

/* path_known()：磁盘 / 索引 / HEAD 任一处有就算存在。 */
static int path_known(const char *rel) {
    if (path_kind(rel) != PK_NONE) return 1;

    slist tracked;
    slist_init(&tracked);
    int known = 0;
    if (index_names(&tracked)) {
        known = slist_has(&tracked, rel);
    } else {
        const char *args[] = { "ls-files", "-z", NULL };
        buf t;
        run_text(args, &t, NULL);
        char *p = t.p, *end = t.p + t.n;
        while (p && p < end) {
            char *z = memchr(p, 0, (size_t)(end - p));
            if (!z) break;
            if (!strcmp(p, rel)) { known = 1; break; }
            p = z + 1;
        }
        buf_free(&t);
    }
    slist_free(&tracked);
    if (known) return 1;

    buf old;
    buf_init(&old);
    int has_old = show_file(rel, "HEAD", &old);
    buf_free(&old);
    return has_old;
}

/* ================================================================ 端点实现 */

static void send_ok_json(SOCKET s, buf *o) {
    buf_cstr(o);
    http_send_json(s, 200, o->p, o->n);
}

static int do_branches(SOCKET s) {
    slist names;
    slist_init(&names);
    int have_names = head_names(&names);
    headst st;
    memset(&st, 0, sizeof(st));
    int have_state = head_state(&st);

    buf o;
    buf_init(&o);
    if (have_names && have_state) {
        const char *cur = (st.kind == 1) ? st.name : NULL;
        buf_puts(&o, "{\"ok\":true,\"branches\":[");
        for (size_t i = 0; i < names.n; i++) {
            if (i) buf_putc(&o, ',');
            buf_puts(&o, "{\"name\":");
            json_str(&o, names.v[i]);
            buf_puts(&o, ",\"current\":");
            buf_puts(&o, (cur && !strcmp(names.v[i], cur)) ? "true" : "false");
            buf_putc(&o, '}');
        }
        buf_puts(&o, "]}");
    } else {
        /* 回退：git for-each-ref（1 次 spawn） */
        const char *args[] = { "for-each-ref",
                               "--format=%(refname:short)\x1f%(HEAD)",
                               "refs/heads", NULL };
        buf t;
        run_text(args, &t, NULL);
        slist rows_names;
        slist rows_cur;
        slist_init(&rows_names);
        slist_init(&rows_cur);
        char *p = t.p, *end = t.p + t.n;
        while (p && p < end) {
            char *nl = memchr(p, '\n', (size_t)(end - p));
            char *line_end = nl ? nl : end;
            size_t len = (size_t)(line_end - p);
            /* Python: if not line.strip(): continue */
            size_t a = 0, b = len;
            while (a < b && (p[a] == ' ' || p[a] == '\t' || p[a] == '\r')) a++;
            while (b > a && (p[b - 1] == ' ' || p[b - 1] == '\t' || p[b - 1] == '\r')) b--;
            if (b > a) {
                char *line = xstrndup(p + a, b - a);
                char *sep = strchr(line, '\x1f');
                char *name = sep ? xstrndup(line, (size_t)(sep - line)) : xstrdup(line);
                char *head = sep ? xstrdup(sep + 1) : xstrdup("");
                /* Python: head.strip() == "*" */
                char *hp = head;
                while (*hp == ' ' || *hp == '\t' || *hp == '\r' || *hp == '\n') hp++;
                size_t hl = strlen(hp);
                while (hl && (hp[hl - 1] == ' ' || hp[hl - 1] == '\t' ||
                              hp[hl - 1] == '\r' || hp[hl - 1] == '\n')) hp[--hl] = 0;
                slist_push(&rows_names, name);
                slist_push(&rows_cur, xstrdup((hl == 1 && hp[0] == '*') ? "1" : "0"));
                free(head);
                free(line);
            }
            p = nl ? nl + 1 : end;
        }
        buf_free(&t);
        /* 按 name 排序（rows_cur 跟着换） */
        for (size_t i = 1; i < rows_names.n; i++) {
            for (size_t j = i; j > 0 && strcmp(rows_names.v[j - 1], rows_names.v[j]) > 0; j--) {
                char *tn = rows_names.v[j]; rows_names.v[j] = rows_names.v[j - 1]; rows_names.v[j - 1] = tn;
                char *tc = rows_cur.v[j]; rows_cur.v[j] = rows_cur.v[j - 1]; rows_cur.v[j - 1] = tc;
            }
        }
        buf_puts(&o, "{\"ok\":true,\"branches\":[");
        for (size_t i = 0; i < rows_names.n; i++) {
            if (i) buf_putc(&o, ',');
            buf_puts(&o, "{\"name\":");
            json_str(&o, rows_names.v[i]);
            buf_puts(&o, ",\"current\":");
            buf_puts(&o, strcmp(rows_cur.v[i], "1") ? "false" : "true");
            buf_putc(&o, '}');
        }
        buf_puts(&o, "]}");
        slist_free(&rows_names);
        slist_free(&rows_cur);
    }
    send_ok_json(s, &o);
    buf_free(&o);
    free(st.name);
    slist_free(&names);
    return 1;
}

static int do_log(SOCKET s, const char *query) {
    long long limit = 30;
    qval lv;
    if (q_val(query, "limit", &lv)) {
        long long v = 0;
        char *sval = u8_replace(lv.p, lv.n);
        if (!parse_int(sval, &v)) {
            char *rp = py_repr_n(sval, strlen(sval));
            buf msg;
            buf_init(&msg);
            buf_printf(&msg, "参数 limit 必须是整数,收到 %s", rp);
            http_send_err(s, 400, buf_cstr(&msg), NULL);
            buf_free(&msg);
            free(rp);
            free(sval);
            free(lv.p);
            return 1;
        }
        limit = v;
        free(sval);
        free(lv.p);
    }
    if (limit < 1) limit = 1;
    if (limit > 1000) limit = 1000;

    char lim[32];
    snprintf(lim, sizeof(lim), "-%lld", limit);
    const char *args[] = { "log", lim, "--pretty=format:%H\x1f%an\x1f%ad\x1f%s\x1e",
                           "--date=short", NULL };
    buf t;
    run_text(args, &t, NULL);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"log\":[");
    int first = 1;
    char *p = t.p, *end = t.p + t.n;
    while (p && p < end) {
        char *rs = memchr(p, '\x1e', (size_t)(end - p));
        char *rec_end = rs ? rs : end;
        if (rs) *rs = 0;
        /* Python: rec = rec.strip("\n") */
        char *a = p, *b = rec_end;
        while (a < b && *a == '\n') a++;
        while (b > a && b[-1] == '\n') b--;
        /* if not rec.strip(): continue  (strip 掉所有空白) */
        char *c = a, *d = b;
        while (c < d && (*c == ' ' || *c == '\t' || *c == '\r' || *c == '\n')) c++;
        while (d > c && (d[-1] == ' ' || d[-1] == '\t' || d[-1] == '\r' || d[-1] == '\n')) d--;
        if (d > c) {
            /* split('\x1f')，必须恰好 4 段 */
            char *parts[8];
            size_t np = 0;
            char *q = a;
            int too_many = 0;
            while (q <= b) {
                char *s1 = memchr(q, '\x1f', (size_t)(b - q));
                if (!s1) {
                    if (np < 8) parts[np++] = q;
                    break;
                }
                if (np < 8) parts[np++] = q;
                else too_many = 1;
                *s1 = 0;
                q = s1 + 1;
            }
            if (!too_many && np == 4) {
                size_t l0 = strlen(parts[0]);
                if (l0 > 8) l0 = 8;
                if (!first) buf_putc(&o, ',');
                first = 0;
                buf_puts(&o, "{\"sha\":");
                json_str_n(&o, parts[0], l0);
                buf_puts(&o, ",\"author\":");
                json_str(&o, parts[1]);
                buf_puts(&o, ",\"date\":");
                json_str(&o, parts[2]);
                buf_puts(&o, ",\"subject\":");
                json_str(&o, parts[3]);
                buf_putc(&o, '}');
            }
        }
        p = rs ? rs + 1 : end;
    }
    buf_puts(&o, "]}");
    send_ok_json(s, &o);
    buf_free(&o);
    buf_free(&t);
    return 1;
}

/* 请求的路径是不是「仓库根目录自己」（Python 有专用文案）。 */
static int is_root_itself(const char *input) {
    wchar_t *w = u82w(input);
    if (!w) return 0;
    const wchar_t *root = git_root();
    size_t n = wcslen(root) + 1 + wcslen(w) + 1;
    wchar_t *full = xmalloc(n * sizeof(wchar_t));
    _snwprintf(full, n, L"%ls\\%ls", root, w);
    wchar_t canon[WBUF];
    DWORD got = GetFullPathNameW(full, WBUF, canon, NULL);
    int yes = 0;
    if (got && got < WBUF) {
        size_t cl = wcslen(canon);
        while (cl > 0 && (canon[cl - 1] == L'\\' || canon[cl - 1] == L'/')) canon[--cl] = 0;
        yes = (_wcsicmp(canon, root) == 0);
    }
    free(full);
    free(w);
    return yes;
}

/* 从 query 取 file -> 归一化的仓内相对路径；失败时**已经发完 400**，返回 NULL。 */
static char *file_param_rel(const char *query, SOCKET s) {
    qval fv;
    if (!q_val(query, "file", &fv)) {
        http_send_err(s, 400, "缺少 file 参数", NULL);
        return NULL;
    }
    if (memchr(fv.p, 0, fv.n)) {
        char *rp = py_repr_n(fv.p, fv.n);
        buf msg;
        buf_init(&msg);
        buf_printf(&msg, "路径里不能包含 NUL 字符,已拒绝:%s", rp);
        http_send_err(s, 400, buf_cstr(&msg), NULL);
        buf_free(&msg);
        free(rp);
        free(fv.p);
        return NULL;
    }
    char *f = u8_replace(fv.p, fv.n);
    free(fv.p);
    char *err = NULL;
    char *rel = path_rel_in_root(f, &err);
    if (!rel) {
        if (is_root_itself(f)) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "路径指向仓库根目录本身,已拒绝:%s", f);
            http_send_err(s, 400, buf_cstr(&m), NULL);
            buf_free(&m);
        } else {
            http_send_err(s, 400, err ? err : "路径不合法", NULL);
        }
        free(err);
        free(f);
        return NULL;
    }
    free(err);
    free(f);
    char *nrel = normalize_rel(rel);
    free(rel);
    return nrel;
}

static int do_file(SOCKET s, const char *query) {
    char *rel = file_param_rel(query, s);
    if (!rel) return 1;
    if (path_kind(rel) == PK_DIR) {
        buf msg;
        buf_init(&msg);
        buf_printf(&msg, "不是文件(是目录),已拒绝:%s", rel);
        http_send_err(s, 400, buf_cstr(&msg), NULL);
        buf_free(&msg);
        free(rel);
        return 1;
    }

    /* 工作区内容：read_text(encoding="utf-8", errors="replace")，带换行翻译 */
    buf cur;
    buf_init(&cur);
    int has_cur = 0;
    if (path_kind(rel) == PK_FILE) {
        wchar_t *wp = rel_w(rel);
        buf raw;
        buf_init(&raw);
        if (wp && read_bytes_w(wp, &raw)) {
            append_decoded(&cur, raw.p ? raw.p : "", raw.n, 1);
            buf_cstr(&cur);
            has_cur = 1;
        }
        buf_free(&raw);
        free(wp);
    }

    char *ref = NULL;
    const char *use_ref = "HEAD";
    qval rv;
    if (q_val(query, "ref", &rv)) {
        if (memchr(rv.p, 0, rv.n)) {
            http_send_err(s, 400, REF_HINT, NULL);
            buf_free(&cur);
            free(rv.p);
            free(rel);
            return 1;
        }
        ref = u8_replace(rv.p, rv.n);
        free(rv.p);
        use_ref = ref;
    }
    if (!ref_ok(use_ref)) {
        http_send_err(s, 400, REF_HINT, NULL);
        buf_free(&cur);
        free(ref);
        free(rel);
        return 1;
    }

    buf old;
    buf_init(&old);
    int has_old = show_file(rel, use_ref, &old);
    if (!has_old && !strcmp(use_ref, "HEAD")) has_old = staged_file(rel, &old);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    json_str(&o, rel);
    buf_puts(&o, ",\"old\":");
    if (has_old) json_str_n(&o, old.p ? old.p : "", old.n);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"new\":");
    if (has_cur) json_str_n(&o, cur.p ? cur.p : "", cur.n);
    else buf_puts(&o, "null");
    buf_putc(&o, '}');
    send_ok_json(s, &o);
    buf_free(&o);
    buf_free(&old);
    buf_free(&cur);
    free(ref);
    free(rel);
    return 1;
}

static int do_diff(SOCKET s, const char *query) {
    char *rel = file_param_rel(query, s);
    if (!rel) return 1;

    int staged = 0;
    qval sv;
    if (q_val(query, "staged", &sv)) {
        staged = (sv.n == 1 && sv.p[0] == '1') ||
                 (sv.n == 4 && !memcmp(sv.p, "true", 4)) ||
                 (sv.n == 3 && !memcmp(sv.p, "yes", 3));
        free(sv.p);
    }

    if (path_kind(rel) == PK_DIR) {
        buf msg;
        buf_init(&msg);
        buf_printf(&msg, "不是文件(是目录),已拒绝:%s", rel);
        http_send_err(s, 400, buf_cstr(&msg), NULL);
        buf_free(&msg);
        free(rel);
        return 1;
    }
    if (!path_known(rel)) {
        buf msg;
        buf_init(&msg);
        buf_printf(&msg, "文件不存在,已拒绝:%s", rel);
        http_send_err(s, 400, buf_cstr(&msg), NULL);
        buf_free(&msg);
        free(rel);
        return 1;
    }

    const char *args[8];
    size_t ai = 0;
    args[ai++] = "--literal-pathspecs";
    args[ai++] = "diff";
    args[ai++] = "--no-color";
    args[ai++] = "--no-ext-diff";
    if (staged) args[ai++] = "--cached";
    args[ai++] = "--";
    args[ai++] = rel;
    args[ai] = NULL;

    buf t;
    run_text(args, &t, NULL);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    json_str(&o, rel);
    buf_puts(&o, ",\"staged\":");
    buf_puts(&o, staged ? "true" : "false");
    buf_puts(&o, ",\"diff\":");
    json_str_n(&o, t.p ? t.p : "", t.n);
    buf_putc(&o, '}');
    send_ok_json(s, &o);
    buf_free(&o);
    buf_free(&t);
    free(rel);
    return 1;
}

/* ================================================================ 分派 */

int read_basic_get(const char *path, const char *query, SOCKET s) {
    if (!strcmp(path, "/api/branches")) return do_branches(s);
    if (!strcmp(path, "/api/log")) return do_log(s, query);
    if (!strcmp(path, "/api/file")) return do_file(s, query);
    if (!strcmp(path, "/api/diff")) return do_diff(s, query);
    return 0;
}


