/* read_histplan.c —— M1 历史计划面：/api/history/stash 与 /api/history/plan/<op>。
 *
 * 对照 Python 版：
 *   /api/history/stash      -> server.py:2173 -> history_ops.stash_show(index, stat)
 *   /api/history/plan/<op>  -> server.py:2207 -> _history_plan(op, q)
 *
 * 全部是**只读预演**：这里一条写命令都不跑（stash show / status / log /
 * rev-parse / branch -r --contains / worktree list / reflog / for-each-ref /
 * rev-list / show -s）。改工作区、索引、HEAD、stash 的一条都不许出现。
 *
 * 口径（照抄 Python，别自作聪明）：
 *   * 所有 git 输出都过一遍「universal newlines」（\r\n 与 \r 都变 \n）——
 *     Python 的 subprocess(text=True) 默认就这么干，diff 文本逐字对齐靠它；
 *   * `_ok()` = (stdout + stderr).strip()，`_out()` = 只取 stdout；
 *   * `_plan()` 的骨架：op / safe / warning / blocked / executable /
 *     requires_confirmation，然后才是各 op 的字段；
 *   * `_history_plan()` 末尾把 `op` 归一到 URL 段，旧值进 `legacy_op`；
 *   * HistoryError 走 400 + {ok:false, error, code, ...extra}；
 *     GitError（参数不合法之类）走 400 + {ok:false, error}（**没有 code**）。
 *
 * 已知差异（都是边角，见交付说明）：
 *   1. 非 ASCII 数字（int("٥")）Python 收，这里 400；
 *   2. 参数里内嵌 NUL 的 ref 名按「正则不匹配」拒绝（Python 同样拒绝，
 *      只是文案可能落在别的检查项上）；message 里的 NUL 会原样进 JSON；
 *   3. Python 的 `\w` 是 Unicode 语义，这里把 >=0x80 的字节都当 word
 *      （所以 emoji 分支名这里放过、Python 拒绝）；
 *   4. 超长整数饱和（Python 任意精度）。
 */
#include "routes.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "git.h"
#include "http.h"
#include "json.h"
#include "params.h"
#include "util.h"

#define FS "\x1f"
#define WBUF 4096

/* ================================================================ 小工具 */

/* Python 的 str.strip()：就地截断尾部空白，返回跳过前导空白后的指针。 */
static char *strip(char *s) {
    return py_strip(s);
}

static int blank(const char *s) {
    for (; *s; s++)
        if (!py_is_ascii_space((unsigned char)*s)) return 0;
    return 1;
}

/* 换行分隔 -> NUL 分隔（就地）。 */
static char *lines_of(buf *b) {
    char *s = buf_cstr(b);
    for (size_t i = 0; i < b->n; i++)
        if (s[i] == '\n') s[i] = 0;
    return s;
}

/* Python subprocess(text=True) 的 universal newlines：\r\n -> \n、单独 \r -> \n。 */
static void crlf_norm(buf *b) {
    if (!b->p) return;
    size_t w = 0;
    for (size_t i = 0; i < b->n; i++) {
        char c = b->p[i];
        if (c == '\r') {
            if (i + 1 < b->n && b->p[i + 1] == '\n') continue;  /* 丢掉 \r，保留 \n */
            c = '\n';
        }
        b->p[w++] = c;
    }
    b->n = w;
    b->p[w] = 0;
}

/* UTF-8 码点数（Python 的 len(str)）。 */
static size_t utf8_count(const char *s) {
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)s; *p; p++)
        if ((*p & 0xc0) != 0x80) n++;
    return n;
}

/* Python 的 s[:nchars]（按码点切，不是按字节）。 */
static char *utf8_prefix(const char *s, size_t nchars) {
    size_t i = 0, cnt = 0;
    while (s[i] && cnt < nchars) {
        unsigned char c = (unsigned char)s[i];
        size_t step = 1;
        if (c >= 0xf0) step = 4;
        else if (c >= 0xe0) step = 3;
        else if (c >= 0xc0) step = 2;
        i += step;
        cnt++;
    }
    if (i > strlen(s)) i = strlen(s);
    return xstrndup(s, i);
}

/* Python int() 的 ASCII 子集：可选空白/符号 + 数字（数字间允许单个下划线）。 */
static int py_int(const char *raw, long long *out) {
    const char *p = raw;
    while (*p && py_is_ascii_space((unsigned char)*p)) p++;
    const char *q = p + strlen(p);
    while (q > p && py_is_ascii_space((unsigned char)q[-1])) q--;
    if (p == q) return 0;
    int neg = 0;
    if (*p == '+' || *p == '-') { neg = (*p == '-'); p++; }
    if (p == q) return 0;
    long long v = 0;
    int prev = 0;
    for (const char *c = p; c < q; c++) {
        if (*c >= '0' && *c <= '9') {
            if (v < 100000000000000000LL) v = v * 10 + (*c - '0');
            prev = 1;
        } else if (*c == '_' && prev && c + 1 < q && c[1] >= '0' && c[1] <= '9') {
            prev = 0;
        } else {
            return 0;
        }
    }
    if (!prev) return 0;
    *out = neg ? -v : v;
    return 1;
}

/* Python repr(str) 的子集：单引号包裹，转义 \ ' 控制字符。 */
static void repr_into(buf *o, const char *s, size_t n) {
    buf_putc(o, '\'');
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '\'': buf_puts(o, "\\'"); break;
        case '\\': buf_puts(o, "\\\\"); break;
        case '\t': buf_puts(o, "\\t"); break;
        case '\n': buf_puts(o, "\\n"); break;
        case '\r': buf_puts(o, "\\r"); break;
        default:
            if (c < 0x20 || c == 0x7f) buf_printf(o, "\\x%02x", c);
            else buf_putc(o, (char)c);
        }
    }
    buf_putc(o, '\'');
}

/* 带长度的 JSON 字符串（Python str 可以含 NUL，message 会走到这里）。 */
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

/* ---------------------------------------------------------------- 字符串表 */

typedef struct {
    char **v;
    size_t n, cap;
} strlist;

static void sl_init(strlist *l) { l->v = NULL; l->n = l->cap = 0; }

static void sl_push(strlist *l, const char *s) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 8;
        l->v = xrealloc(l->v, l->cap * sizeof(char *));
    }
    l->v[l->n++] = xstrdup(s);
}

static void sl_free(strlist *l) {
    for (size_t i = 0; i < l->n; i++) free(l->v[i]);
    free(l->v);
    sl_init(l);
}

static void json_strlist(buf *o, const strlist *l) {
    buf_putc(o, '[');
    for (size_t i = 0; i < l->n; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, l->v[i]);
    }
    buf_putc(o, ']');
}

/* ---------------------------------------------------------------- query */

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* urllib.parse.parse_qs(keep_blank_values=False) 的取值语义：
 *   * `?a` 与 `?a=` 都算「没有这个键」（空值被丢掉，实测 parse_qs 两者都是 {}）；
 *   * 同名多次取第一个**非空**值；
 *   * `+` 是空格，%XX 解码，非法 % 原样保留（与 params.c 同源）。
 * 返回 malloc 的 NUL 结尾串，*outlen 是真实长度（可含 NUL）；没有返回 NULL。 */
static char *qp_get(const char *query, const char *key, size_t *outlen) {
    if (outlen) *outlen = 0;
    if (!query || !key) return NULL;
    size_t klen = strlen(key);
    const char *p = query;
    while (p && *p) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        size_t this_klen = eq ? (size_t)(eq - p) : (size_t)(end - p);
        if (this_klen == klen && !strncmp(p, key, klen)) {
            const char *v = eq ? eq + 1 : end;
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
            if (oi == 0) {           /* keep_blank_values=False：空值等于没给 */
                free(o);
                p = amp ? amp + 1 : NULL;
                continue;
            }
            if (outlen) *outlen = oi;
            return o;
        }
        p = amp ? amp + 1 : NULL;
    }
    return NULL;
}

/* 取原始值（malloc，NUL 结尾；*outlen 为真实长度，可含 NUL）。没有返回 NULL。 */
static char *get_raw(const char *query, const char *key, size_t *outlen) {
    return qp_get(query, key, outlen);
}

/* 取字符串参数：没有返回 NULL（调用方区分「没有」与「空串」）。 */
static char *get_str(const char *query, const char *key, size_t *outlen) {
    size_t n = 0;
    char *v = get_raw(query, key, &n);
    if (v && outlen) *outlen = n;
    return v;
}

/* 取整数参数。返回 0=成功（含缺省），1=不是整数（*bad 已填 repr 文本）。 */
/* 大整数回显：优先用 query 原文本的规范十进制（Python 的 int 无上限）。 */
static char *index_echo_q(const char *query, long long v) {
    size_t n = 0;
    char *raw = get_raw(query, "index", &n);
    char *c = raw ? py_int_canon_n(raw, n) : NULL;
    free(raw);
    if (c) return c;
    char t[32];
    snprintf(t, sizeof(t), "%lld", v);
    return xstrdup(t);
}

static int get_int(const char *query, const char *key, long long dflt, long long *out,
                   buf *bad) {
    size_t n = 0;
    char *v = get_raw(query, key, &n);
    if (!v) { *out = dflt; return 0; }
    if (n == 0) { free(v); *out = dflt; return 0; }   /* _qi: v == "" -> 缺省 */
    char *s = xmalloc(n + 1);
    memcpy(s, v, n);
    s[n] = 0;
    int ok = (strlen(s) == n) && py_int(s, out);
    if (!ok) repr_into(bad, v, n);
    free(s);
    free(v);
    return ok ? 0 : 1;
}

/* _qflag：缺省返回 dflt；"1"/"true"/"yes"/"on" 为真。 */
static int get_flag(const char *query, const char *key, int dflt) {
    size_t n = 0;
    char *v = get_raw(query, key, &n);
    if (!v) return dflt;
    int r = (!strcmp(v, "1") || !strcmp(v, "true") ||
             !strcmp(v, "yes") || !strcmp(v, "on"));
    free(v);
    return r;
}

/* ---------------------------------------------------------------- git 调用 */

static int run_out(const char *const args[], buf *out) {
    buf er;
    buf_init(&er);
    int rc = git_run(args, NULL, 0, out, &er);
    buf_free(&er);
    crlf_norm(out);
    return rc;
}

/* `_ok()`：返回 rc，*text 收 (stdout + stderr).strip()。 */
static int run_ok(const char *const args[], buf *text) {
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    crlf_norm(&out);
    crlf_norm(&er);
    buf_append(text, buf_cstr(&out), out.n);
    buf_append(text, buf_cstr(&er), er.n);
    char *p = buf_cstr(text);
    char *s = strip(p);
    if (s != p) memmove(p, s, strlen(s) + 1);
    text->n = strlen(p);
    buf_free(&out);
    buf_free(&er);
    return rc;
}

/* `_out()`：只取 stdout，rc 忽略。 */
static void out_only(const char *const args[], buf *out) {
    (void)run_out(args, out);
}

/* ================================================================ 错误响应 */

/* HistoryError -> 400 + {"ok":false,"error":msg,"code":code,...extra}
 * GitError     -> 400 + {"ok":false,"error":msg}（code 传 NULL） */
static void send_hist(SOCKET s, const char *msg, const char *code, const char *extra) {
    buf b;
    buf_init(&b);
    buf_puts(&b, "{\"ok\":false,\"error\":");
    json_str(&b, msg);
    if (code && *code) {
        buf_puts(&b, ",\"code\":");
        json_str(&b, code);
    }
    if (extra && *extra) buf_puts(&b, extra);
    buf_putc(&b, '}');
    http_send_json(s, 400, buf_cstr(&b), b.n);
    buf_free(&b);
}

static void send_giterr(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
}

/* "参数 X 必须是整数,收到 %r" */
static void send_bad_int(SOCKET s, const char *key, const buf *repr) {
    buf m;
    buf_init(&m);
    buf_printf(&m, "参数 %s 必须是整数,收到 %s", key, buf_cstr((buf *)repr));
    send_giterr(s, buf_cstr(&m));
    buf_free(&m);
}

/* ================================================================ 仓库状态 */

static int is_repo(void) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t p[WBUF];
    _snwprintf(p, WBUF, L"%ls\\.git", root);
    return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES;
}

static int has_commits(void) {
    const char *args[] = { "rev-parse", "--verify", "--quiet", "HEAD", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    buf_free(&out);
    buf_free(&er);
    return rc == 0;
}

/* _require_repo：失败已发响应，返回 0。 */
static int require_repo(SOCKET s) {
    if (is_repo()) return 1;
    buf m;
    buf_init(&m);
    char *root = w2u8(git_root());
    buf_printf(&m, "不是一个 git 仓库(或路径不存在):%s", root ? root : "");
    send_hist(s, buf_cstr(&m), "not_a_repo", NULL);
    free(root);
    buf_free(&m);
    return 0;
}

/* _require_commits：失败已发响应，返回 0。 */
static int require_commits(SOCKET s, const char *what) {
    if (has_commits()) return 1;
    buf m;
    buf_init(&m);
    buf_printf(&m, "仓库还没有任何提交,%s无从谈起", what);
    send_hist(s, buf_cstr(&m), "no_commits", NULL);
    buf_free(&m);
    return 0;
}

static char *head_sha(void) {
    const char *args[] = { "rev-parse", "HEAD", NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *p = strip(buf_cstr(&out));
    char *r = xstrdup(p);
    buf_free(&out);
    return r;
}

/* symbolic-ref --quiet --short HEAD；detached 时返回 NULL。 */
static char *head_branch(void) {
    const char *args[] = { "symbolic-ref", "--quiet", "--short", "HEAD", NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *p = strip(buf_cstr(&out));
    char *r = *p ? xstrdup(p) : NULL;
    buf_free(&out);
    return r;
}

static char *subject_of(const char *rev) {
    const char *args[] = { "show", "-s", "--format=%s", rev, NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *p = strip(buf_cstr(&out));
    char *r = xstrdup(p);
    buf_free(&out);
    return r;
}

static strlist parents_of(const char *rev) {
    strlist l;
    sl_init(&l);
    const char *args[] = { "show", "-s", "--format=%P", rev, NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        /* Python: s.split() —— 任意空白切分 */
        char *p = line;
        while (*p) {
            while (*p && py_is_ascii_space((unsigned char)*p)) p++;
            if (!*p) break;
            char *q = p;
            while (*q && !py_is_ascii_space((unsigned char)*q)) q++;
            char save = *q;
            *q = 0;
            sl_push(&l, p);
            *q = save;
            p = q;
        }
    }
    buf_free(&out);
    return l;
}

/* ================================================================ dirty */

typedef struct {
    strlist staged, unstaged, untracked, conflicted;
} dirty_t;

static void dirty_compute(dirty_t *d) {
    sl_init(&d->staged);
    sl_init(&d->unstaged);
    sl_init(&d->untracked);
    sl_init(&d->conflicted);

    const char *args[] = { "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    crlf_norm(&out);

    char *s = out.p ? out.p : (char *)"";
    size_t n = out.n, i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && s[j]) j++;
        size_t len = j - i;
        if (len >= 3) {
            char x = s[i], y = s[i + 1];
            char *path = xstrndup(s + i + 3, len - 3);
            /* Python: if " -> " in path: path = path.split(" -> ")[-1] */
            char *last = path;
            char *p2 = path;
            while ((p2 = strstr(p2, " -> ")) != NULL) { last = p2 + 4; p2 += 4; }
            if (last != path) memmove(path, last, strlen(last) + 1);
            if (x == '?' && y == '?') {
                sl_push(&d->untracked, path);
                free(path);
                i = j + 1;
                continue;
            }
            if (x == 'U' || y == 'U' || (x == 'A' && y == 'A') ||
                (x == 'D' && y == 'D'))
                sl_push(&d->conflicted, path);
            if (x != ' ' && x != '?') sl_push(&d->staged, path);
            if (y != ' ' && y != '?') sl_push(&d->unstaged, path);
            free(path);
        }
        i = j + 1;
    }
    buf_free(&out);
}

static void dirty_free(dirty_t *d) {
    sl_free(&d->staged);
    sl_free(&d->unstaged);
    sl_free(&d->untracked);
    sl_free(&d->conflicted);
}

static void dirty_json(buf *o, const dirty_t *d) {
    buf_puts(o, "{\"staged\":");
    json_strlist(o, &d->staged);
    buf_puts(o, ",\"unstaged\":");
    json_strlist(o, &d->unstaged);
    buf_puts(o, ",\"untracked\":");
    json_strlist(o, &d->untracked);
    buf_puts(o, ",\"conflicted\":");
    json_strlist(o, &d->conflicted);
    buf_putc(o, '}');
}

/* ================================================================ published */

typedef struct {
    int published;
    strlist remotes;
} pub_t;

static void published_compute(const char *rev, pub_t *p) {
    sl_init(&p->remotes);
    const char *args[] = { "branch", "-r", "--contains", rev, NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        char *t = strip(line);
        if (*t) {
            char *sp = strchr(t, ' ');
            if (sp) *sp = 0;
            sl_push(&p->remotes, t);
        }
    }
    p->published = p->remotes.n > 0;
    buf_free(&out);
}

static void published_free(pub_t *p) { sl_free(&p->remotes); }

static void published_json(buf *o, const pub_t *p) {
    buf_printf(o, "{\"published\":%s,\"remotes\":", p->published ? "true" : "false");
    json_strlist(o, &p->remotes);
    buf_puts(o, ",\"note\":");
    json_str(o, "只检查本地已有的远端跟踪分支,没有联网查询。查不到不等于没推送过。");
    buf_putc(o, '}');
}

/* ================================================================ 分支 */

typedef struct {
    char *name;
    char *sha;
    int current;
} branch_row;

static int branch_cmp(const void *a, const void *b) {
    const branch_row *x = (const branch_row *)a;
    const branch_row *y = (const branch_row *)b;
    return strcmp(x->name, y->name);
}

static branch_row *list_branches(size_t *out_n) {
    const char *args[] = { "for-each-ref",
                           "--format=%(refname:short)" FS "%(HEAD)" FS "%(objectname)",
                           "refs/heads", NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);

    size_t cap = 8, n = 0;
    branch_row *rows = xmalloc(cap * sizeof(branch_row));
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (blank(line)) continue;
        char *f[3] = { line, (char *)"", (char *)"" };
        size_t fi = 0;
        char *p = line;
        while (fi < 2) {
            char *q = strchr(p, 0x1f);
            if (!q) break;
            *q = 0;
            f[++fi] = q + 1;
            p = q + 1;
        }
        if (n == cap) {
            cap *= 2;
            rows = xrealloc(rows, cap * sizeof(branch_row));
        }
        rows[n].name = xstrdup(f[0]);
        char *h = strip(f[1]);
        rows[n].current = !strcmp(h, "*");
        rows[n].sha = xstrdup(f[2]);
        n++;
    }
    buf_free(&out);
    qsort(rows, n, sizeof(branch_row), branch_cmp);
    *out_n = n;
    return rows;
}

static void branches_free(branch_row *r, size_t n) {
    for (size_t i = 0; i < n; i++) { free(r[i].name); free(r[i].sha); }
    free(r);
}

static char *branch_sha(const char *name) {
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, "refs/heads/");
    buf_puts(&spec, name);
    const char *args[] = { "rev-parse", "--verify", "--quiet", buf_cstr(&spec), NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *p = strip(buf_cstr(&out));
    char *r = *p ? xstrdup(p) : NULL;
    buf_free(&out);
    buf_free(&spec);
    return r;
}

static int branch_exists(const char *name) {
    char *s = branch_sha(name);
    int r = s != NULL;
    free(s);
    return r;
}

static int is_current_branch(const char *name) {
    char *cur = head_branch();
    if (cur) {
        int r = !strcmp(cur, name);
        free(cur);
        return r;
    }
    if (!has_commits()) return 0;
    char *bs = branch_sha(name);
    char *hs = head_sha();
    int r = bs && hs && !strcmp(bs, hs);
    free(bs);
    free(hs);
    return r;
}

/* default_branch：refs/remotes 下各 HEAD 的 symref -> main -> master -> NULL。 */
static char *default_branch(void) {
    const char *args[] = { "for-each-ref", "--format=%(symref)",
                           "refs/remotes/*/HEAD", NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        char *t = strip(line);
        if (!strncmp(t, "refs/remotes/", 13)) {
            char *slash = strrchr(t, '/');
            char *r = xstrdup(slash ? slash + 1 : t);
            buf_free(&out);
            return r;
        }
    }
    buf_free(&out);

    size_t n = 0;
    branch_row *br = list_branches(&n);
    char *found = NULL;
    for (size_t i = 0; i < n && !found; i++)
        if (!strcmp(br[i].name, "main")) found = xstrdup("main");
    for (size_t i = 0; i < n && !found; i++)
        if (!strcmp(br[i].name, "master")) found = xstrdup("master");
    branches_free(br, n);
    return found;
}

/* _unmerged_commits：返回未合并提交数，*arr 收 JSON 数组文本，*listed 收条数。 */
static long long unmerged_commits(const char *name, const char *dflt, buf *arr,
                                  long long *listed_out) {
    char *target = NULL;
    if (dflt && branch_exists(dflt)) target = xstrdup(dflt);
    else target = xstrdup("HEAD");

    buf range;
    buf_init(&range);
    buf_puts(&range, target);
    buf_puts(&range, "..");
    buf_puts(&range, name);

    long long nr = 0;
    {
        const char *args[] = { "rev-list", "--count", buf_cstr(&range), NULL };
        buf out;
        buf_init(&out);
        out_only(args, &out);
        char *t = strip(buf_cstr(&out));
        if (*t) {
            long long v = 0;
            if (py_int(t, &v)) nr = v;
        }
        buf_free(&out);
    }

    buf_puts(arr, "[");
    long long listed = 0;
    {
        const char *args[] = { "log", "-20", "--date=short",
                               "--pretty=format:%H" FS "%an" FS "%ad" FS "%s",
                               buf_cstr(&range), NULL };
        buf out;
        buf_init(&out);
        out_only(args, &out);
        char *s = lines_of(&out);
        size_t pos = 0, len = out.n;
        int first = 1;
        while (pos < len) {
            char *line = s + pos;
            size_t ll = strlen(line);
            pos += ll + 1;
            if (ll && line[ll - 1] == '\r') line[--ll] = 0;
            if (blank(line)) continue;
            char *f[4] = { line, (char *)"", (char *)"", (char *)"" };
            size_t fi = 0;
            char *p = line;
            int extra = 0;
            while (fi < 3) {
                char *q = strchr(p, 0x1f);
                if (!q) break;
                *q = 0;
                f[++fi] = q + 1;
                p = q + 1;
            }
            if (fi == 3 && strchr(f[3], 0x1f)) extra = 1;   /* len(parts) > 4 */
            if (fi != 3 || extra) continue;                 /* Python: len(parts) == 4 */
            if (!first) buf_putc(arr, ',');
            first = 0;
            listed++;
            buf_puts(arr, "{\"sha\":");
            json_str(arr, f[0]);
            buf_puts(arr, ",\"short\":");
            char *sh8 = xstrndup(f[0], strlen(f[0]) < 8 ? strlen(f[0]) : 8);
            json_str(arr, sh8);
            free(sh8);
            buf_puts(arr, ",\"author\":");
            json_str(arr, f[1]);
            buf_puts(arr, ",\"date\":");
            json_str(arr, f[2]);
            buf_puts(arr, ",\"subject\":");
            json_str(arr, f[3]);
            buf_putc(arr, '}');
        }
        buf_free(&out);
    }
    buf_puts(arr, "]");
    buf_free(&range);
    free(target);
    *listed_out = listed;
    return nr;
}

/* ================================================================ reflog */

typedef struct {
    char *sha;
    char *shortsha;
    char *author;
    char *date;
    char *action;
} reflog_row;

/* list_reflog(limit) —— 走 git 的 reflog 格式化输出（与 Python 回退路径同源）。 */
static reflog_row *list_reflog(long long lim, size_t *out_n) {
    char larg[64];
    snprintf(larg, sizeof(larg), "-%lld", lim);
    const char *args[] = { "reflog", larg, "--date=iso",
                           "--pretty=format:%H" FS "%h" FS "%an" FS "%ad" FS "%gs",
                           NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    buf_free(&er);
    *out_n = 0;
    if (rc != 0) {              /* 空仓 -> 128 -> 空列表 */
        buf_free(&out);
        return NULL;
    }
    crlf_norm(&out);

    size_t cap = 8, n = 0;
    reflog_row *rows = xmalloc(cap * sizeof(reflog_row));
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (blank(line)) continue;
        char *f[5] = { line, (char *)"", (char *)"", (char *)"", (char *)"" };
        size_t fi = 0;
        char *p = line;
        while (fi < 4) {
            char *q = strchr(p, 0x1f);
            if (!q) break;
            *q = 0;
            f[++fi] = q + 1;
            p = q + 1;
        }
        if (n == cap) {
            cap *= 2;
            rows = xrealloc(rows, cap * sizeof(reflog_row));
        }
        rows[n].sha = xstrdup(f[0]);
        rows[n].shortsha = xstrdup(f[1]);
        rows[n].author = xstrdup(f[2]);
        rows[n].date = xstrdup(f[3]);
        rows[n].action = xstrdup(f[4]);
        n++;
    }
    buf_free(&out);
    *out_n = n;
    return rows;
}

static void reflog_free(reflog_row *r, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(r[i].sha);
        free(r[i].shortsha);
        free(r[i].author);
        free(r[i].date);
        free(r[i].action);
    }
    free(r);
}

/* ================================================================ stash */

typedef struct {
    char *ref;
    char *name;
    char *subject;
} stash_row;

/* ^stash@\{(\d+)\}:\s*(.*) —— 匹配返回 desc 起点并写 *idx。 */
static char *match_stash(const char *line, long long *idx) {
    if (strncmp(line, "stash@{", 7)) return NULL;
    const char *p = line + 7;
    if (*p < '0' || *p > '9') return NULL;
    long long v = 0;
    while (*p >= '0' && *p <= '9') {
        if (v < 100000000000LL) v = v * 10 + (*p - '0');
        p++;
    }
    if (p[0] != '}' || p[1] != ':') return NULL;
    p += 2;
    while (*p && py_is_ascii_space((unsigned char)*p)) p++;
    *idx = v;
    return (char *)p;
}

/* list_stashes(limit) —— 逐行照 Python 的解析（含多行 message 会被拆成多行这条）。 */
static stash_row *list_stashes(long long lim, size_t *out_n) {
    char larg[64];
    snprintf(larg, sizeof(larg), "-%lld", lim);
    const char *args[] = { "stash", "list", larg, NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);            /* rc 忽略（Python check=False） */

    size_t cap = 8, n = 0;
    stash_row *rows = xmalloc(cap * sizeof(stash_row));
    char *s = lines_of(&out);
    size_t pos = 0, len = out.n;
    while (pos < len) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (blank(line)) continue;

        char *work = xstrdup(line);
        long long idx = (long long)n;
        char *desc = match_stash(work, &idx);
        char *dfull;
        char *dpart;
        if (desc) {
            dfull = xstrdup(desc);
            dpart = xstrdup(desc);
        } else {
            char *t = strip(work);
            dfull = xstrdup(t);
            dpart = xstrdup(t);
        }
        char *colon = strchr(dpart, ':');
        char *sub_src = NULL;
        if (colon) { *colon = 0; sub_src = colon + 1; }
        char *name = xstrdup(strip(dpart));
        char *sub = xstrdup(sub_src ? strip(sub_src) : "");
        if (!*sub) {
            free(sub);
            sub = xstrdup(strip(dfull));
        }
        if (n == cap) {
            cap *= 2;
            rows = xrealloc(rows, cap * sizeof(stash_row));
        }
        char ref[64];
        snprintf(ref, sizeof(ref), "stash@{%lld}", idx);
        rows[n].ref = xstrdup(ref);
        rows[n].name = name;
        rows[n].subject = sub;
        n++;
        free(dpart);
        free(dfull);
        free(work);
    }
    buf_free(&out);
    *out_n = n;
    return rows;
}

static void stashes_free(stash_row *r, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(r[i].ref);
        free(r[i].name);
        free(r[i].subject);
    }
    free(r);
}

/* ================================================================ plan 骨架 */

static void plan_begin(buf *o, const char *op, int safe, const char *warning,
                       const char *blocked) {
    buf_puts(o, "{\"op\":");
    json_str(o, op);
    buf_printf(o, ",\"safe\":%s", safe ? "true" : "false");
    buf_puts(o, ",\"warning\":");
    if (warning) json_str(o, warning);
    else buf_puts(o, "null");
    buf_puts(o, ",\"blocked\":");
    if (blocked) json_str(o, blocked);
    else buf_puts(o, "null");
    buf_printf(o, ",\"executable\":%s,\"requires_confirmation\":true",
               blocked ? "false" : "true");
}

/* " ".join(warnings) */
static void warn_add(buf *w, const char *s) {
    if (w->n) buf_putc(w, ' ');
    buf_puts(w, s);
}

/* ================================================================ 参数校验 */

/* REF_NAME_RE = [\w./-]{1,100}（\w 这里按 ASCII + 所有 >=0x80 字节近似）。 */
static int ref_chars_ok(const char *name, size_t len) {
    size_t cnt = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)name[i];
        if (c >= 0x80) {                 /* 非 ASCII 一律算一个码点（近似 \w 的 Unicode 语义） */
            if ((c & 0xc0) != 0x80) cnt++;
            continue;
        }
        if (isalnum(c) || c == '_' || c == '.' || c == '/' || c == '-') { cnt++; continue; }
        return 0;
    }
    return cnt >= 1 && cnt <= 100;
}

/* validate_ref_name：通过返回 malloc 的（strip 后）名字，失败发响应返回 NULL。 */
static char *validate_ref_name(SOCKET s, const char *raw, size_t rawlen,
                               const char *what) {
    buf m;
    buf_init(&m);

    if (!raw) {
        buf_printf(&m, "%s必须是字符串", what);
        send_hist(s, buf_cstr(&m), "bad_name", NULL);
        buf_free(&m);
        return NULL;
    }
    char *name = xmalloc(rawlen + 1);
    memcpy(name, raw, rawlen);
    name[rawlen] = 0;
    size_t nlen = rawlen;
    int has_nul = memchr(name, 0, nlen) != NULL;

    char *t = strip(name);
    if (t != name) {
        memmove(name, t, strlen(t) + 1);
        nlen = strlen(name);
        has_nul = memchr(name, 0, nlen) != NULL;
    } else {
        nlen = strlen(name);
    }

    const char *err = NULL;
    if (!*name) err = "不能为空";
    else if (!has_nul && utf8_count(name) > 100) err = "超过 100 字符";
    else if (has_nul || !ref_chars_ok(name, nlen))
        err = "只允许字母数字和 _ . / -(正则 [\\w./-]{1,100})";
    else if (name[0] == '-') err = "不能以 - 开头:git 会把它当成选项读";
    else if (name[0] == '/' || name[0] == '.') err = "不能以 / 或 . 开头";
    else if (name[nlen - 1] == '/' || name[nlen - 1] == '.') err = "不能以 / 或 . 结尾";
    else if (strstr(name, "..")) err = "不能包含 ..";
    else if (strstr(name, "//")) err = "不能包含 //";
    else if (strstr(name, "@{")) err = "不能包含 @{:这是 reflog 语法";
    else if (!strcmp(name, "@")) err = "不能是 @";
    else if (nlen >= 5 && !strcmp(name + nlen - 5, ".lock")) err = "不能以 .lock 结尾";

    if (!err) {
        for (size_t i = 0; i < nlen; i++) {
            unsigned char c = (unsigned char)name[i];
            if (c < 32 || c == 127) { err = "不能包含控制字符"; break; }
        }
    }
    if (err) {
        buf_printf(&m, "%s%s", what, err);
        send_hist(s, buf_cstr(&m), "bad_name", NULL);
        buf_free(&m);
        free(name);
        return NULL;
    }
    buf_free(&m);
    return name;
}

/* _check_sha：校验 rev 是不是能解析的 commit-ish；失败发响应返回 NULL。 */
static char *check_sha(SOCKET s, const char *raw, size_t rawlen, const char *what) {
    buf m;
    buf_init(&m);
    if (!raw) {
        buf_printf(&m, "%s不能为空", what);
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        return NULL;
    }
    char *rev = xmalloc(rawlen + 1);
    memcpy(rev, raw, rawlen);
    rev[rawlen] = 0;
    int has_nul = memchr(rev, 0, rawlen) != NULL;
    char *t = strip(rev);
    if (t != rev) { memmove(rev, t, strlen(t) + 1); rawlen = strlen(rev); }
    else rawlen = strlen(rev);
    has_nul = memchr(rev, 0, rawlen) != NULL;

    if (!*rev) {
        buf_printf(&m, "%s不能为空", what);
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        free(rev);
        return NULL;
    }
    if (rev[0] == '-') {
        buf_printf(&m, "%s不能以 - 开头", what);
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        free(rev);
        return NULL;
    }
    int chars_ok = !has_nul && rawlen <= 64;
    if (chars_ok) {
        for (size_t i = 0; i < rawlen; i++) {
            char c = rev[i];
            if (!(isalnum((unsigned char)c) || c == '_' || c == '.' || c == '/' ||
                  c == '^' || c == '~' || c == '@' || c == '{' || c == '}' ||
                  c == '-')) {
                chars_ok = 0;
                break;
            }
        }
    }
    if (!chars_ok) {
        buf_printf(&m, "%s含有非法字符:%s", what, rev);
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        free(rev);
        return NULL;
    }

    buf spec;
    buf_init(&spec);
    buf_puts(&spec, rev);
    buf_puts(&spec, "^{commit}");
    const char *args[] = { "rev-parse", "--verify", "--quiet", buf_cstr(&spec), NULL };
    buf out;
    buf_init(&out);
    out_only(args, &out);
    char *p = strip(buf_cstr(&out));
    char *real = *p ? xstrdup(p) : NULL;
    buf_free(&out);
    buf_free(&spec);
    if (!real) {
        buf_printf(&m, "解析不了这个%s:%s", what, rev);
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        free(rev);
        return NULL;
    }
    buf_free(&m);
    free(rev);
    return real;
}

/* ================================================================ stash show */

static void handle_stash_show(const char *query, SOCKET s) {
    long long index = 0;
    buf bad;
    buf_init(&bad);
    if (get_int(query, "index", 0, &index, &bad)) {
        send_bad_int(s, "index", &bad);
        buf_free(&bad);
        return;
    }
    buf_free(&bad);
    int stat = get_flag(query, "stat", 0);

    if (index < 0) {
        send_hist(s, "stash 索引不能为负", "bad_index", NULL);
        return;
    }
    size_t nrow = 0;
    stash_row *rows = list_stashes(200, &nrow);
    if (index >= (long long)nrow) {
        buf m;
        buf_init(&m);
        char *echo = index_echo_q(query, index);
        buf_printf(&m, "没有第 %s 个 stash(共 %zu 个)", echo, nrow);
        free(echo);
        send_hist(s, buf_cstr(&m), "no_such_stash", NULL);
        buf_free(&m);
        stashes_free(rows, nrow);
        return;
    }
    char *ref = xstrdup(rows[index].ref);
    char *name = xstrdup(rows[index].name);
    stashes_free(rows, nrow);

    const char *args[] = { "stash", "show", "--no-color",
                           stat ? "--stat" : "-p", ref, NULL };
    buf text;
    buf_init(&text);
    int rc = run_ok(args, &text);

    buf o;
    buf_init(&o);
    buf_printf(&o, "{\"ok\":%s,\"index\":%lld,\"ref\":", rc == 0 ? "true" : "false", index);
    json_str(&o, ref);
    buf_puts(&o, ",\"name\":");
    json_str(&o, name);
    buf_puts(&o, ",\"text\":");
    json_str(&o, buf_cstr(&text));
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&text);
    free(ref);
    free(name);
}

/* ================================================================ plan: stash */

static void plan_stash_q(const char *query, SOCKET s) {
    if (!require_repo(s)) return;
    int inc_unt = get_flag(query, "include_untracked", 0);

    dirty_t d;
    dirty_compute(&d);
    size_t changes = d.staged.n + d.unstaged.n + d.conflicted.n +
                     (inc_unt ? d.untracked.n : 0);
    if (changes == 0) {
        buf extra;
        buf_init(&extra);
        buf_puts(&extra, ",\"dirty\":");
        dirty_json(&extra, &d);
        send_hist(s,
                  "工作区是干净的,没有东西可以暂存。git stash 在这种情况下会直接"
                  "报错(而不是塞一个空条目进 stash 列表),所以这里提前拦下。",
                  "nothing_to_stash", buf_cstr(&extra));
        buf_free(&extra);
        dirty_free(&d);
        return;
    }

    buf w;
    buf_init(&w);
    if (d.conflicted.n) {
        buf t;
        buf_init(&t);
        buf_printf(&t,
                   "仓库正处于冲突未解决状态(%zu 个路径)。stash 会把冲突一起收走,"
                   "之后 git stash pop 可能产生新的冲突。冲突未解决时 stash "
                   "在 git 自身的语义里就是不完整的。", d.conflicted.n);
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }
    if (inc_unt && d.untracked.n == 0) {
        warn_add(&w, "没有未跟踪文件,include_untracked 不起作用。");
    }

    size_t nstash = 0;
    stash_row *rows = list_stashes(50, &nstash);
    stashes_free(rows, nstash);

    buf o;
    buf_init(&o);
    plan_begin(&o, "stash", 1, w.n ? buf_cstr(&w) : NULL, NULL);
    buf_puts(&o, ",\"dirty\":");
    dirty_json(&o, &d);
    buf_printf(&o, ",\"will_stash\":%zu,\"untracked_kept\":%zu,\"current_stash_count\":%zu}",
               changes, inc_unt ? (size_t)0 : d.untracked.n, nstash);
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    dirty_free(&d);
}

/* ================================================================ plan: stash-drop */

static void plan_stash_drop_q(const char *query, SOCKET s) {
    long long index = 0;
    buf bad;
    buf_init(&bad);
    if (get_int(query, "index", 0, &index, &bad)) {
        send_bad_int(s, "index", &bad);
        buf_free(&bad);
        return;
    }
    buf_free(&bad);

    if (!require_repo(s)) return;
    if (index < 0) {
        send_hist(s, "stash 索引不能为负", "bad_index", NULL);
        return;
    }
    size_t nrow = 0;
    stash_row *rows = list_stashes(200, &nrow);
    if (index >= (long long)nrow) {
        buf m;
        buf_init(&m);
        char *echo = index_echo_q(query, index);
        buf_printf(&m, "没有第 %s 个 stash(共 %zu 个)", echo, nrow);
        free(echo);
        send_hist(s, buf_cstr(&m), "no_such_stash", NULL);
        buf_free(&m);
        stashes_free(rows, nrow);
        return;
    }
    char *ref = xstrdup(rows[index].ref);
    char *name = xstrdup(rows[index].name);
    char *subj = xstrdup(rows[index].subject);
    stashes_free(rows, nrow);

    size_t nstash = 0;
    stash_row *all = list_stashes(50, &nstash);
    stashes_free(all, nstash);

    buf w;
    buf_init(&w);
    buf_printf(&w,
               "丢弃 stash 列表里的第 %lld 个(%s)。丢掉之后要靠 reflog 找回,"
               "而 reflog 会随 gc 被清掉,不要指望它一定在。", index, name);

    buf o;
    buf_init(&o);
    plan_begin(&o, "stash-drop", 0, buf_cstr(&w), NULL);
    buf_printf(&o, ",\"stash\":{\"index\":%lld,\"ref\":", index);
    json_str(&o, ref);
    buf_puts(&o, ",\"name\":");
    json_str(&o, name);
    buf_puts(&o, ",\"subject\":");
    json_str(&o, subj);
    buf_printf(&o, "},\"stash_count\":%zu}", nstash);
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    free(ref);
    free(name);
    free(subj);
}

/* ================================================================ plan: amend */

static void plan_amend_q(const char *query, SOCKET s) {
    size_t msglen = 0;
    char *msg = get_str(query, "message", &msglen);
    if (!require_repo(s)) { free(msg); return; }
    if (!require_commits(s, "amend")) { free(msg); return; }

    dirty_t d;
    dirty_compute(&d);
    char *old_sha = head_sha();

    buf w;
    buf_init(&w);
    if (d.unstaged.n) {
        buf t;
        buf_init(&t);
        buf_printf(&t,
                   "工作区有 %zu 个**未暂存**改动。amend 只提交**已暂存**的内容"
                   "(不会加 -a/--all),所以它们不会被卷进这次提交,会原样留在工作区。"
                   "如果你本意是把它们一起提交,先 stage 再 amend。", d.unstaged.n);
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }
    if (!d.staged.n) {
        warn_add(&w, "没有已暂存的改动 —— 这次 amend 只能改提交信息,"
                     "提交内容不会变。");
    }
    if (d.untracked.n) {
        buf t;
        buf_init(&t);
        buf_printf(&t, "有 %zu 个未跟踪文件,它们不属于任何一次提交,amend "
                   "不会碰它们。", d.untracked.n);
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }

    pub_t pub;
    published_compute(old_sha, &pub);
    {
        buf t;
        buf_init(&t);
        if (pub.published) {
            buf dest;
            buf_init(&dest);
            for (size_t i = 0; i < pub.remotes.n; i++) {
                if (i) buf_puts(&dest, ", ");
                buf_puts(&dest, pub.remotes.v[i]);
            }
            buf_printf(&t,
                       "HEAD 已经在 %zu 个远端跟踪分支上(%s)。改写它会让已推送的历史失效,"
                       "协作者必须重新拉取或 reset --hard,否则会出乱子。%s",
                       pub.remotes.n, buf_cstr(&dest),
                       "只检查本地已有的远端跟踪分支,没有联网查询。查不到不等于没推送过。");
            buf_free(&dest);
        } else {
            buf_puts(&t,
                     "本地看不到它被推送过(只查了本地远端跟踪分支,"
                     "没有联网)。如果你已经推过,请先确认远端。");
        }
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }

    char *short8 = xstrndup(old_sha, strlen(old_sha) < 8 ? strlen(old_sha) : 8);
    char *subj = subject_of(old_sha);

    buf o;
    buf_init(&o);
    plan_begin(&o, "amend", 0, buf_cstr(&w), NULL);
    buf_puts(&o, ",\"old_sha\":");
    json_str(&o, old_sha);
    buf_puts(&o, ",\"old_short\":");
    json_str(&o, short8);
    buf_puts(&o, ",\"old_subject\":");
    json_str(&o, subj);
    buf_puts(&o, ",\"new_message\":");
    if (msg) json_str_n(&o, msg, msglen);
    else buf_puts(&o, "null");
    buf_printf(&o, ",\"message_changes\":%s", msg ? "true" : "false");
    buf_puts(&o, ",\"dirty\":");
    dirty_json(&o, &d);
    buf_puts(&o, ",\"published\":");
    published_json(&o, &pub);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    dirty_free(&d);
    published_free(&pub);
    free(short8);
    free(subj);
    free(old_sha);
    free(msg);
}

/* ================================================================ plan: undo */

/* _reflog_hint(sha, mode) */
static char *reflog_hint(const char *sha, const char *mode) {
    const char *flag;
    if (!strcmp(mode, "soft") || !strcmp(mode, "mixed") ||
        !strcmp(mode, "hard") || !strcmp(mode, "merge")) {
        buf f;
        buf_init(&f);
        buf_puts(&f, "--");
        buf_puts(&f, mode);
        flag = buf_cstr(&f);
        /* flag 指向 f，稍后要用；先算完再 free —— 见下面 */
        const char *args[] = { "reflog", "--format=%H", NULL };
        buf out;
        buf_init(&out);
        out_only(args, &out);
        char *s = lines_of(&out);
        size_t pos = 0, len = out.n;
        long long i = 0;
        char *res = NULL;
        while (pos < len) {
            char *line = s + pos;
            size_t ll = strlen(line);
            pos += ll + 1;
            if (ll && line[ll - 1] == '\r') line[--ll] = 0;
            if (!strcmp(strip(line), sha)) {
                buf r;
                buf_init(&r);
                buf_printf(&r, "git reset %s HEAD@{%lld}", flag, i);
                res = xstrdup(buf_cstr(&r));
                buf_free(&r);
                break;
            }
            i++;
        }
        buf_free(&out);
        buf_free(&f);
        if (res) return res;
        return xstrdup("git reflog");
    }
    return xstrdup("git reflog");
}

static void plan_undo_q(const char *query, SOCKET s) {
    int drop = get_flag(query, "drop_changes", 0);
    if (!require_repo(s)) return;
    if (!require_commits(s, "撤销上一次提交")) return;

    char *sha = head_sha();
    dirty_t d;
    dirty_compute(&d);
    pub_t pub;
    published_compute(sha, &pub);

    buf w;
    buf_init(&w);
    if (drop) {
        warn_add(&w, "**丢弃**这个提交的改动:reset --hard 之后工作区与该提交"
                     "完全一致,提交里的内容就没了。");
    } else {
        warn_add(&w, "保留改动:所有改动回到暂存区(index),工作区文件不动。");
    }
    if (d.unstaged.n || d.untracked.n) {
        buf extra;
        buf_init(&extra);
        if (d.unstaged.n) {
            buf_printf(&extra, "%zu 个未暂存改动", d.unstaged.n);
        }
        if (d.untracked.n) {
            if (extra.n) buf_puts(&extra, "、");
            buf_printf(&extra, "%zu 个未跟踪文件", d.untracked.n);
        }
        buf t;
        buf_init(&t);
        if (drop) {
            buf_printf(&t,
                       "丢弃模式下 reset --hard 会**连同工作区里尚未"
                       "提交的改动一起抹掉**(%s)。", buf_cstr(&extra));
        } else {
            buf_printf(&t, "软回退不影响工作区(%s)。", buf_cstr(&extra));
        }
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
        buf_free(&extra);
    }
    if (d.conflicted.n) {
        warn_add(&w, "仓库处于冲突未解决状态,先解决冲突或 merge --abort。");
    }
    if (pub.published) {
        buf dest;
        buf_init(&dest);
        for (size_t i = 0; i < pub.remotes.n; i++) {
            if (i) buf_puts(&dest, ", ");
            buf_puts(&dest, pub.remotes.v[i]);
        }
        buf t;
        buf_init(&t);
        buf_printf(&t, "这次提交已经在 %s 上,撤销会让它消失,远端要另外处理。",
                   buf_cstr(&dest));
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
        buf_free(&dest);
    }
    strlist parents = parents_of(sha);
    long long nparents = (long long)parents.n;
    if (nparents > 1) {
        buf t;
        buf_init(&t);
        buf_printf(&t,
                   "这是一个 merge 提交(有 %lld 个父提交),撤销它要指定"
                   "保留哪一侧(mainline),见 plan_revert。", nparents);
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }

    char *short8 = xstrndup(sha, strlen(sha) < 8 ? strlen(sha) : 8);
    char *subj = subject_of(sha);
    char *hint = reflog_hint(sha, drop ? "hard" : "soft");

    buf o;
    buf_init(&o);
    plan_begin(&o, "undo-last-commit", 0, buf_cstr(&w), NULL);
    buf_puts(&o, ",\"target_sha\":");
    json_str(&o, sha);
    buf_puts(&o, ",\"target_short\":");
    json_str(&o, short8);
    buf_puts(&o, ",\"target_subject\":");
    json_str(&o, subj);
    buf_printf(&o, ",\"mode\":\"%s\",\"parents\":%lld", drop ? "hard" : "soft",
               nparents);
    buf_puts(&o, ",\"dirty\":");
    dirty_json(&o, &d);
    buf_puts(&o, ",\"published\":");
    published_json(&o, &pub);
    buf_puts(&o, ",\"recoverable_via\":");
    json_str(&o, hint);
    if (drop) {
        buf_puts(&o, ",\"legacy_op\":\"undo-dropped\"");
    }
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    dirty_free(&d);
    published_free(&pub);
    sl_free(&parents);
    free(short8);
    free(subj);
    free(hint);
    free(sha);
}

/* ================================================================ plan: reflog-restore */

static void plan_reflog_restore_q(const char *query, SOCKET s) {
    long long index = 0;
    buf bad;
    buf_init(&bad);
    if (get_int(query, "index", 0, &index, &bad)) {
        send_bad_int(s, "index", &bad);
        buf_free(&bad);
        return;
    }
    buf_free(&bad);

    size_t modelen = 0;
    char *mode = get_str(query, "mode", &modelen);
    if (!mode) mode = xstrdup("soft");

    if (!require_repo(s)) { free(mode); return; }

    size_t nrow = 0;
    reflog_row *rows = list_reflog(500, &nrow);
    if (nrow == 0) {
        send_hist(s,
                  "reflog 是空的(仓库还没有提交,或 reflog 被 gc 清掉),"
                  "没有可恢复的历史。", "empty_reflog", NULL);
        reflog_free(rows, nrow);
        free(mode);
        return;
    }
    if (index < 0 || index >= (long long)nrow) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "reflog 里没有第 %lld 条(共 %zu 条)", index, nrow);
        send_hist(s, buf_cstr(&m), "bad_index", NULL);
        buf_free(&m);
        reflog_free(rows, nrow);
        free(mode);
        return;
    }
    if (strcmp(mode, "soft") && strcmp(mode, "mixed") &&
        strcmp(mode, "hard") && strcmp(mode, "merge")) {
        send_hist(s, "mode 只能是 soft / mixed / hard / merge", "bad_mode", NULL);
        reflog_free(rows, nrow);
        free(mode);
        return;
    }

    reflog_row *row = &rows[index];
    int hard = !strcmp(mode, "hard");
    char *sha = xstrdup(row->sha);
    char *sh = xstrdup(row->shortsha);
    char *sel = xstrdup("HEAD@{0}");
    {
        buf t;
        buf_init(&t);
        buf_printf(&t, "HEAD@{%lld}", index);
        free(sel);
        sel = xstrdup(buf_cstr(&t));
        buf_free(&t);
    }
    char *act = xstrdup(row->action);
    reflog_free(rows, nrow);

    buf w;
    buf_init(&w);
    buf_printf(&w,
               "把 HEAD 移到 %s(%s)。%s本次操作本身也会进 reflog,所以还能"
               "退回来,但别把 reflog 当保险箱 —— gc 之后就没了。",
               sh, act,
               hard ? "mode=hard 会同时丢弃工作区未提交的改动。" : "");

    char *head_now = head_sha();

    buf o;
    buf_init(&o);
    plan_begin(&o, "reflog-restore", hard ? 0 : 1, buf_cstr(&w), NULL);
    buf_printf(&o, ",\"target\":{\"index\":%lld,\"sha\":", index);
    json_str(&o, sha);
    buf_puts(&o, ",\"short\":");
    json_str(&o, sh);
    buf_puts(&o, ",\"selector\":");
    json_str(&o, sel);
    buf_puts(&o, ",\"action\":");
    json_str(&o, act);
    buf_puts(&o, "},\"mode\":");
    json_str(&o, mode);
    buf_puts(&o, ",\"head_now\":");
    json_str(&o, head_now);
    buf_puts(&o, ",\"legacy_op\":\"restore-from-reflog\"");
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    free(sha);
    free(sh);
    free(sel);
    free(act);
    free(head_now);
    free(mode);
}

/* ================================================================ plan: revert */

static void plan_revert_q(const char *query, SOCKET s) {
    size_t shalen = 0;
    char *sha = get_str(query, "sha", &shalen);

    long long mainline = 0;
    int has_mainline = 0;
    {
        buf bad;
        buf_init(&bad);
        size_t mlen = 0;
        char *mv = get_raw(query, "mainline", &mlen);
        if (mv) {
            if (mlen == 0) {
                has_mainline = 0;
            } else {
                char *tmp = xmalloc(mlen + 1);
                memcpy(tmp, mv, mlen);
                tmp[mlen] = 0;
                int ok = (strlen(tmp) == mlen) && py_int(tmp, &mainline);
                free(tmp);
                if (!ok) {
                    repr_into(&bad, mv, mlen);
                    send_bad_int(s, "mainline", &bad);
                    buf_free(&bad);
                    free(mv);
                    free(sha);
                    return;
                }
                has_mainline = 1;
            }
            free(mv);
        }
        buf_free(&bad);
    }

    if (!require_repo(s)) { free(sha); return; }
    char *real = check_sha(s, sha, shalen, "提交");
    if (!real) { free(sha); return; }

    strlist parents = parents_of(real);
    long long nparents = (long long)parents.n;
    buf blocked;
    buf_init(&blocked);
    buf w;
    buf_init(&w);

    if (nparents > 1) {
        if (!has_mainline) {
            buf cand;
            buf_init(&cand);
            for (size_t i = 0; i < parents.n; i++) {
                char *ps = subject_of(parents.v[i]);
                char *p40 = utf8_prefix(ps, 40);
                if (i) buf_puts(&cand, ", ");
                buf_printf(&cand, "%zu = %s", i + 1, p40);
                free(ps);
                free(p40);
            }
            char *sh8 = xstrndup(real, 8);
            buf_printf(&blocked,
                       "%s 是一个 merge 提交,有 %lld 个父提交。revert 一个 merge 必须"
                       "用 -m 指定保留哪一侧(mainline),否则结果不确定:不同的 mainline "
                       "会得到完全相反的逆向改动。候选:%s", sh8, nparents,
                       buf_cstr(&cand));
            free(sh8);
            buf_free(&cand);
        } else {
            if (mainline < 1 || mainline > nparents) {
                buf m;
                buf_init(&m);
                buf_printf(&m, "mainline 必须是 1..%lld,收到 %lld", nparents, mainline);
                send_hist(s, buf_cstr(&m), "bad_mainline", NULL);
                buf_free(&m);
                goto done;
            }
            char *ps = subject_of(parents.v[mainline - 1]);
            char *p40 = utf8_prefix(ps, 40);
            buf t;
            buf_init(&t);
            buf_printf(&t,
                       "mainline=%lld,保留 %s 这一侧。revert 会把"
                       "「相对这一侧的差异」反向应用到当前分支上。", mainline, p40);
            warn_add(&w, buf_cstr(&t));
            buf_free(&t);
            free(ps);
            free(p40);
        }
    } else if (has_mainline) {
        char *sh8 = xstrndup(real, 8);
        buf_printf(&blocked, "%s 只有一个父提交,不是 merge 提交,不能指定 mainline。",
                   sh8);
        free(sh8);
    }

    {
        dirty_t d;
        dirty_compute(&d);
        if (d.conflicted.n) {
            buf_reset(&blocked);
            buf_puts(&blocked, "仓库处于冲突未解决状态,先解决冲突或 git revert --abort。");
        }
        dirty_free(&d);
    }

    {
        buf o;
        buf_init(&o);
        const char *warn = w.n ? buf_cstr(&w)
            : "revert 会新建一个反向提交,不改写任何历史 —— 撤销错了再 revert 一次"
              "就行。";
        plan_begin(&o, "revert", blocked.n ? 0 : 1, warn,
                   blocked.n ? buf_cstr(&blocked) : NULL);
        buf_puts(&o, ",\"sha\":");
        json_str(&o, real);
        buf_puts(&o, ",\"short\":");
        char *sh8 = xstrndup(real, 8);
        json_str(&o, sh8);
        free(sh8);
        buf_puts(&o, ",\"subject\":");
        char *subj = subject_of(real);
        json_str(&o, subj);
        free(subj);
        buf_puts(&o, ",\"parents\":");
        json_strlist(&o, &parents);
        buf_printf(&o, ",\"is_merge\":%s,\"mainline\":",
                   nparents > 1 ? "true" : "false");
        if (has_mainline) buf_printf(&o, "%lld", mainline);
        else buf_puts(&o, "null");
        buf_putc(&o, '}');
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
    }

done:
    buf_free(&w);
    buf_free(&blocked);
    sl_free(&parents);
    free(real);
    free(sha);
}

/* ================================================================ plan: branch-rename */

static void plan_branch_rename_q(const char *query, SOCKET s) {
    size_t oldlen = 0, newlen = 0;
    char *oldraw = get_str(query, "old", &oldlen);
    char *newraw = get_str(query, "new", &newlen);

    if (!require_repo(s)) { free(oldraw); free(newraw); return; }
    char *oldn = validate_ref_name(s, oldraw, oldlen, "旧分支名");
    if (!oldn) { free(oldraw); free(newraw); return; }
    char *newn = validate_ref_name(s, newraw, newlen, "新分支名");
    if (!newn) { free(oldraw); free(newraw); free(oldn); return; }

    if (!branch_exists(oldn)) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "没有这个分支:%s", oldn);
        send_hist(s, buf_cstr(&m), "no_such_branch", NULL);
        buf_free(&m);
        free(oldraw); free(newraw); free(oldn); free(newn);
        return;
    }

    buf blocked;
    buf_init(&blocked);
    if (!strcmp(oldn, newn)) {
        buf_puts(&blocked, "新旧分支名相同,什么都不用做。");
    } else if (branch_exists(newn)) {
        buf_printf(&blocked, "已经有同名分支 %s。改名不会覆盖它 —— 那是另一个人的工作。",
                   newn);
    }

    buf w;
    buf_init(&w);
    if (is_current_branch(oldn)) {
        buf t;
        buf_init(&t);
        buf_printf(&t, "%s 是当前分支,改名会同时更新 HEAD 和上游配置。", oldn);
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }
    {
        const char *args[] = { "worktree", "list", "--porcelain", NULL };
        buf out;
        buf_init(&out);
        out_only(args, &out);
        crlf_norm(&out);
        buf needle;
        buf_init(&needle);
        buf_puts(&needle, "branch refs/heads/");
        buf_puts(&needle, oldn);
        if (strstr(buf_cstr(&out), buf_cstr(&needle))) {
            buf t;
            buf_init(&t);
            buf_printf(&t,
                       "%s 还被别的 worktree 检出着,git 会拒绝改名;"
                       "先在那个 worktree 里切走。", oldn);
            warn_add(&w, buf_cstr(&t));
            buf_free(&t);
        }
        buf_free(&needle);
        buf_free(&out);
    }
    if (w.n == 0) {
        warn_add(&w, "改名不改历史、不删提交,做完随时能改回去。");
    }

    char *sha = branch_sha(oldn);

    buf o;
    buf_init(&o);
    plan_begin(&o, "branch-rename", 0, buf_cstr(&w),
               blocked.n ? buf_cstr(&blocked) : NULL);
    buf_puts(&o, ",\"old\":");
    json_str(&o, oldn);
    buf_puts(&o, ",\"new\":");
    json_str(&o, newn);
    buf_puts(&o, ",\"old_sha\":");
    if (sha) json_str(&o, sha);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"legacy_op\":\"rename-branch\"}");
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    buf_free(&blocked);
    free(sha);
    free(oldraw);
    free(newraw);
    free(oldn);
    free(newn);
}

/* ================================================================ plan: branch-delete */

static void plan_branch_delete_q(const char *query, SOCKET s) {
    size_t namelen = 0;
    char *nameraw = get_str(query, "name", &namelen);
    int force = get_flag(query, "force", 0);

    if (!require_repo(s)) { free(nameraw); return; }
    char *name = validate_ref_name(s, nameraw, namelen, "分支名");
    if (!name) { free(nameraw); return; }

    if (!branch_exists(name)) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "没有这个分支:%s", name);
        send_hist(s, buf_cstr(&m), "no_such_branch", NULL);
        buf_free(&m);
        free(nameraw);
        free(name);
        return;
    }

    size_t nbranch = 0;
    branch_row *branches = list_branches(&nbranch);
    int current = is_current_branch(name);
    char *dflt = default_branch();

    buf blocked;
    buf_init(&blocked);
    buf w;
    buf_init(&w);

    if (nbranch <= 1) {
        buf_puts(&blocked,
                 "这是仓库里唯一的分支,删掉之后 HEAD 就没有落脚点了。"
                 "git 本身也会拒绝。");
    } else if (current) {
        const char *elsewhere = NULL;
        for (size_t i = 0; i < nbranch; i++) {
            if (strcmp(branches[i].name, name)) { elsewhere = branches[i].name; break; }
        }
        buf hint;
        buf_init(&hint);
        if (elsewhere) { buf_puts(&hint, "git checkout "); buf_puts(&hint, elsewhere); }
        else buf_puts(&hint, "git checkout <其他分支>");
        buf_printf(&blocked,
                   "不能删除当前所在的分支 %s。先切到别的分支(%s),再删它。",
                   name, buf_cstr(&hint));
        buf_free(&hint);
    } else {
        if (dflt && !strcmp(name, dflt)) {
            buf_printf(&blocked,
                       "不能删除 default 分支 %s。它是别人 clone 之后的落脚点,"
                       "也是判别「这个提交算不算已合并」的基准。想删它,先改"
                       "名字(plan_branch_rename)再删。", name);
        }
    }

    buf commits;
    buf_init(&commits);
    long long listed = 0;
    long long nr = unmerged_commits(name, dflt, &commits, &listed);
    if (nr) {
        buf t;
        buf_init(&t);
        buf_printf(&t, "该分支有 %lld 个未合并提交(相对 %s)", nr,
                   dflt ? dflt : "HEAD");
        if (force) buf_puts(&t, ",force=True 会直接丢掉,不可恢复");
        else buf_puts(&t, ",不加 force 会被拒绝");
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
        if (listed < nr) {
            buf t2;
            buf_init(&t2);
            buf_printf(&t2, "(只列出前 %lld 条)", listed);
            warn_add(&w, buf_cstr(&t2));
            buf_free(&t2);
        }
    } else {
        buf t;
        buf_init(&t);
        buf_printf(&t, "该分支的提交都已并入 %s,删除是安全的。",
                   dflt ? dflt : "HEAD");
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
    }

    int safe;
    if (blocked.n) {
        buf t;
        buf_init(&t);
        buf_puts(&t, "不过先说结论:这一步现在**不能做** —— ");
        buf_puts(&t, buf_cstr(&blocked));
        warn_add(&w, buf_cstr(&t));
        buf_free(&t);
        safe = 0;
    } else {
        safe = (nr == 0);
    }

    char *sha = branch_sha(name);

    buf o;
    buf_init(&o);
    plan_begin(&o, "branch-delete", safe, buf_cstr(&w),
               blocked.n ? buf_cstr(&blocked) : NULL);
    buf_puts(&o, ",\"name\":");
    json_str(&o, name);
    buf_printf(&o, ",\"force\":%s,\"unmerged_count\":%lld,\"commits\":%s",
               force ? "true" : "false", nr, buf_cstr(&commits));
    buf_printf(&o, ",\"current\":%s,\"is_default\":%s",
               current ? "true" : "false",
               (dflt && !strcmp(dflt, name)) ? "true" : "false");
    buf_printf(&o, ",\"branch_count\":%zu,\"sha\":", nbranch);
    if (sha) json_str(&o, sha);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"legacy_op\":\"delete-branch\"}");
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    buf_free(&blocked);
    buf_free(&commits);
    branches_free(branches, nbranch);
    free(dflt);
    free(sha);
    free(nameraw);
    free(name);
}

/* ================================================================ plan: tag-delete */

static void plan_tag_delete_q(const char *query, SOCKET s) {
    size_t namelen = 0;
    char *nameraw = get_str(query, "name", &namelen);

    if (!require_repo(s)) { free(nameraw); return; }
    char *name = validate_ref_name(s, nameraw, namelen, "标签名");
    if (!name) { free(nameraw); return; }

    {
        buf spec;
        buf_init(&spec);
        buf_puts(&spec, "refs/tags/");
        buf_puts(&spec, name);
        const char *args[] = { "for-each-ref", "--count=1", buf_cstr(&spec), NULL };
        buf out;
        buf_init(&out);
        out_only(args, &out);
        char *t = strip(buf_cstr(&out));
        int exists = *t != 0;
        buf_free(&out);
        buf_free(&spec);
        if (!exists) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "没有这个标签:%s", name);
            send_hist(s, buf_cstr(&m), "no_such_tag", NULL);
            buf_free(&m);
            free(nameraw);
            free(name);
            return;
        }
    }

    buf sha;
    buf_init(&sha);
    {
        const char *args[] = { "rev-list", "-1", name, NULL };
        out_only(args, &sha);
    }
    char *shastr = strip(buf_cstr(&sha));

    buf w;
    buf_init(&w);
    {
        char *sh8;
        if (*shastr) sh8 = xstrndup(shastr, strlen(shastr) < 8 ? strlen(shastr) : 8);
        else sh8 = xstrdup("?");
        buf_printf(&w,
                   "删掉标签 %s(指向 %s)。标签通常不进 reflog,所以这一删基本"
                   "不可恢复;提交本身还在,只是这个标记没了。", name, sh8);
        free(sh8);
    }

    buf o;
    buf_init(&o);
    plan_begin(&o, "tag-delete", 0, buf_cstr(&w), NULL);
    buf_puts(&o, ",\"name\":");
    json_str(&o, name);
    buf_puts(&o, ",\"sha\":");
    json_str(&o, shastr);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&w);
    buf_free(&sha);
    free(nameraw);
    free(name);
}

/* ================================================================ 分派 */

static const char *PLAN_OPS[] = {
    "stash", "stash-drop", "amend", "undo-last-commit", "reflog-restore",
    "revert", "branch-rename", "branch-delete", "tag-delete"
};

static void handle_plan(const char *path, const char *query, SOCKET s) {
    const char *op = strrchr(path, '/');
    op = op ? op + 1 : path;

    if (!strcmp(op, "stash")) { plan_stash_q(query, s); return; }
    if (!strcmp(op, "stash-drop")) { plan_stash_drop_q(query, s); return; }
    if (!strcmp(op, "amend")) { plan_amend_q(query, s); return; }
    if (!strcmp(op, "undo-last-commit")) { plan_undo_q(query, s); return; }
    if (!strcmp(op, "reflog-restore")) { plan_reflog_restore_q(query, s); return; }
    if (!strcmp(op, "revert")) { plan_revert_q(query, s); return; }
    if (!strcmp(op, "branch-rename")) { plan_branch_rename_q(query, s); return; }
    if (!strcmp(op, "branch-delete")) { plan_branch_delete_q(query, s); return; }
    if (!strcmp(op, "tag-delete")) { plan_tag_delete_q(query, s); return; }

    {
        buf m;
        buf_init(&m);
        buf_printf(&m, "没有这个预演:%s(合法值:", op);
        for (size_t i = 0; i < sizeof(PLAN_OPS) / sizeof(PLAN_OPS[0]); i++) {
            if (i) buf_putc(&m, '/');
            buf_puts(&m, PLAN_OPS[i]);
        }
        buf_putc(&m, ')');
        send_giterr(s, buf_cstr(&m));
        buf_free(&m);
    }
}

int read_histplan_get(const char *path, const char *query, SOCKET s) {
    if (!path) return 0;
    if (!strcmp(path, "/api/history/stash")) {
        handle_stash_show(query, s);
        return 1;
    }
    if (!strncmp(path, "/api/history/plan/", 18)) {
        handle_plan(path, query, s);
        return 1;
    }
    return 0;
}
