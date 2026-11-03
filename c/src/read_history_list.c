/* read_history_list.c —— M1 历史清单读：history/{tags,reflog,stashes}。
 *
 * 三个端点全部交给 git 自己的格式化输出，逐行解析 —— 与 Python 版
 * history_ops.list_tags / list_reflog / list_stashes 的**回退路径**同源。
 * Python 那边的「快路径」（直接读 refs/reflog 文件 + 常驻 cat-file）只省 spawn，
 * 输出口径与这里逐字相同（实测：4 个标签含中文名/多行注解、4 条 reflog、2 个
 * stash，两路输出逐项相等；见 tests/conformance_histlist.py）。
 *
 * 口径（照抄 Python，别自作聪明）：
 *   tags    : for-each-ref --count=<n> --sort=-creatordate
 *             subject   = %(contents:subject)  第一段、行间一个空格
 *             date      = %(creatordate:short) 记录时区下的 YYYY-MM-DD
 *             annotated = (objecttype == "tag")
 *   reflog  : reflog -<n> --date=iso
 *             short 直接用 git 的 %h（core.abbrev + 最短唯一前缀，与 Python
 *             的 _short_sha 同源），date 是 `YYYY-MM-DD HH:MM:SS +ZZZZ`，
 *             selector 自己拼 HEAD@{下标}（下标即列表序号）。
 *   stashes : stash list -<n>
 *             逐行照 Python 的 re.match(r"stash@\{(\d+)\}:\s*(.*)") 解析：
 *             name 是第一个冒号前那段、subject 是冒号后那段（空则退回整条 desc），
 *             raw 是整行 strip 之后的样子。
 *
 * 错误口径：
 *   * limit 非整数 → 400 + {"ok":false,"error":"参数 limit 必须是整数,收到 'abc'"}
 *     （无 code，与 Python GitError 走 _err(e, 400) 一致）；
 *   * limit 夹取：tags 1..500（缺省 100）、reflog 1..500（缺省 30）、
 *     stashes 1..200（缺省 50）；`limit=` 空值等于没给（Python _qi 同义）；
 *   * 空仓 / 无标签 / 无 stash / reflog 为空 → 200 + 空数组，**不是错误**；
 *   * 只有 tags 的 for-each-ref 失败会报错（Python 那里是 check=True），
 *     reflog 与 stashes 的 rc 一律忽略（Python check=False）。
 */
#include "routes.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "git.h"
#include "http.h"
#include "json.h"
#include "params.h"
#include "util.h"

/* 记录分隔符，与 Python 的 FS = "\x1f" 同值 */
#define FS "\x1f"

/* ---------------------------------------------------------------- 小工具 */

static int is_ws(int c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

/* Python 的 str.strip()：就地截断尾部空白，返回跳过前导空白后的指针。 */
static char *strip(char *s) {
    while (*s && is_ws((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && is_ws((unsigned char)s[n - 1])) s[--n] = 0;
    return s;
}

/* 全空白（Python 的 `not line.strip()`）。 */
static int blank(const char *s) {
    for (; *s; s++)
        if (!is_ws((unsigned char)*s)) return 0;
    return 1;
}

/* 按 FS 切一行，最多取 max 段，不足补 ""（等价于
 * `(line.split(FS) + [""]*max)[:max]`）。就地写 NUL。 */
static void split_fs(char *line, char **out, size_t max) {
    size_t n = 0;
    char *p = line;
    while (n < max) {
        out[n++] = p;
        if (n == max) break;
        char *q = strchr(p, 0x1f);
        if (!q) break;
        *q = 0;
        p = q + 1;
    }
    while (n < max) out[n++] = (char *)"";
}

/* git 的输出是换行分隔；就地改成 NUL 分隔，好让逐行扫描用 strlen 定位。
 * （Python 那边是 splitlines()；这里只认 \n，行尾 \r 留给调用方吃。） */
static char *lines_of(buf *b) {
    char *s = buf_cstr(b);
    for (size_t i = 0; i < b->n; i++)
        if (s[i] == '\n') s[i] = 0;
    return s;
}

/* ---------------------------------------------------------------- limit */

/* 解析 limit。返回 0=成功（*out 已夹取），-1=不是整数（*raw 是原值）。
 * 语义对齐 Python 的 _qi + _clamp：
 *   缺参数 / 空串 -> dflt；int() 失败 -> 400；然后 max(lo, min(hi, v))。 */
static int limit_of(const char *query, long long dflt, long long lo, long long hi,
                    long long *out, char *raw, size_t rawsz) {
    char tmp[512];
    int got = q_get(query, "limit", tmp, sizeof(tmp));
    if (got == 0) { *out = dflt; return 0; }
    if (got < 0) {                       /* 超长：不可能是个合法整数 */
        snprintf(raw, rawsz, "(超长)");
        return -1;
    }
    const char *p = tmp;
    while (*p && is_ws((unsigned char)*p)) p++;
    const char *q = p + strlen(p);
    while (q > p && is_ws((unsigned char)q[-1])) q--;
    if (q == p) { *out = dflt; return 0; }        /* "" / "   " -> 缺省 */

    /* Python int() 的 ASCII 子集：可选正负号 + 数字（数字间允许单个下划线）。 */
    const char *d = p;
    int neg = 0;
    if (*d == '+' || *d == '-') { neg = (*d == '-'); d++; }
    if (d == q) { snprintf(raw, rawsz, "%s", tmp); return -1; }
    long long v = 0;
    int prev_digit = 0;
    for (const char *c = d; c < q; c++) {
        if (*c >= '0' && *c <= '9') {
            if (v < 100000000000000000LL) v = v * 10 + (*c - '0');
            prev_digit = 1;
        } else if (*c == '_' && prev_digit && c + 1 < q &&
                   c[1] >= '0' && c[1] <= '9') {
            prev_digit = 0;
        } else {
            snprintf(raw, rawsz, "%s", tmp);
            return -1;
        }
    }
    if (!prev_digit) { snprintf(raw, rawsz, "%s", tmp); return -1; }
    if (neg) v = -v;
    if (v < lo) v = lo;
    if (v > hi) v = hi;
    *out = v;
    return 0;
}

static void send_bad_limit(SOCKET s, const char *raw) {
    buf m;
    buf_init(&m);
    buf_puts(&m, "参数 limit 必须是整数,收到 '");
    buf_puts(&m, raw);
    buf_puts(&m, "'");
    http_send_err(s, 400, buf_cstr(&m), NULL);
    buf_free(&m);
}

/* ================================================================ 快路径
 *
 * 与 Python 版 history_ops._tags_fast / _reflog_fast + gitrefs 对位：
 * 直接读 `.git` 下的 refs / reflog / config，对象内容走常驻 cat-file（不计数）。
 * 任何一处「不确定」就整条返回 0，调用方回退到 git 命令 —— 与 Python 的
 * 「fast is None → 走原命令」同形，所以回退路径的计数/输出仍然逐项相等。
 */

#define WBUF 4096

#include <wchar.h>
#include <windows.h>

#include "catfile.h"

typedef struct { char *name; char *sha; } tag_ref;
typedef struct { char *sha; char *msg; } log_ent;

static int dotgit_usable(void) {
    if (getenv("GIT_DIR") || getenv("GIT_COMMON_DIR")) return 0;
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t p[WBUF];
    _snwprintf(p, WBUF, L"%ls\\.git", root);
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

static char *read_file_bytes(const wchar_t *path, size_t *len) {
    FILE *f = _wfopen(path, L"rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0 || sz > (long)(64 * 1024 * 1024)) { fclose(f); return NULL; }
    rewind(f);
    char *b = xmalloc((size_t)sz + 1);
    size_t got = fread(b, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) { free(b); return NULL; }
    b[sz] = 0;
    if (len) *len = (size_t)sz;
    return b;
}

static int is_sha40(const char *s) {
    if (!s || strlen(s) != 40) return 0;
    for (int i = 0; i < 40; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    return 1;
}


/* Python `data.decode("utf-8")` 的等价：任何非法序列都算失败（回退 git）。 */
static int utf8_ok(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) { i++; continue; }
        int need;
        unsigned cp;
        if (c >= 0xc2 && c <= 0xdf) { need = 1; cp = c & 0x1f; }
        else if (c >= 0xe0 && c <= 0xef) { need = 2; cp = c & 0x0f; }
        else if (c >= 0xf0 && c <= 0xf4) { need = 3; cp = c & 0x07; }
        else return 0;
        if (i + (size_t)need >= n) return 0;
        for (int k = 1; k <= need; k++) {
            unsigned char cc = (unsigned char)s[i + (size_t)k];
            if ((cc & 0xc0) != 0x80) return 0;
            cp = (cp << 6) | (cc & 0x3f);
        }
        /* 过长编码 / 代理区 / 越界 */
        if ((need == 2 && cp < 0x800) || (need == 3 && cp < 0x10000) ||
            (cp >= 0xd800 && cp <= 0xdfff) || cp > 0x10ffff) return 0;
        i += (size_t)need + 1;
    }
    return 1;
}

static void civil_from_days(long long z, int *yy, int *mm, int *dd) {
    z += 719468;
    long long era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned)(z - era * 146097);
    unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    long long y = (long long)yoe + era * 400;
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    unsigned mp = (5 * doy + 2) / 153;
    unsigned d = doy - (153 * mp + 2) / 5 + 1;
    unsigned m = mp + (mp < 10 ? 3 : -9);
    *yy = (int)(y + (m <= 2));
    *mm = (int)m;
    *dd = (int)d;
}

/* `gitobjects.iso` / `short_date`：记录时区下的本地时间（不碰进程 TZ）。 */
static void fmt_date(long long ts, const char *tz, int shortfmt, char *out, size_t osz) {
    int off = 0;
    if (tz && strlen(tz) >= 5) {
        int sign = (tz[0] == '-') ? -1 : 1;
        off = ((tz[1] - '0') * 10 + (tz[2] - '0')) * 60 +
              ((tz[3] - '0') * 10 + (tz[4] - '0'));
        off *= sign;
    }
    long long secs = ts + (long long)off * 60;
    long long days = secs / 86400;
    long long rem = secs % 86400;
    if (rem < 0) { rem += 86400; days--; }
    int y, m, d;
    civil_from_days(days, &y, &m, &d);
    if (shortfmt)
        snprintf(out, osz, "%04d-%02d-%02d", y, m, d);
    else
        snprintf(out, osz, "%04d-%02d-%02d %02d:%02d:%02d %s",
                 y, m, d, (int)(rem / 3600), (int)((rem % 3600) / 60),
                 (int)(rem % 60), tz ? tz : "+0000");
}

/* "author Name <mail> 1793093032 +0800" → name/ts/tz。 */
static int parse_ident(const char *line, const char *prefix, char **name,
                       long long *ts, char tz[8]) {
    size_t pl = strlen(prefix);
    if (strncmp(line, prefix, pl) || line[pl] != ' ') return 0;
    const char *rest = line + pl + 1;
    const char *lt = strstr(rest, " <");
    if (!lt || lt == rest) return 0;
    const char *gt = strrchr(rest, '>');
    if (!gt || gt < lt) return 0;
    const char *p = gt + 1;
    while (*p == ' ') p++;
    char *endp = NULL;
    long long t = strtoll(p, &endp, 10);
    if (endp == p) return 0;
    while (*endp == ' ') endp++;
    if (!(endp[0] == '+' || endp[0] == '-') || strlen(endp) < 5) return 0;
    for (int i = 1; i < 5; i++)
        if (endp[i] < '0' || endp[i] > '9') return 0;
    memcpy(tz, endp, 5);
    tz[5] = 0;
    *name = xmalloc((size_t)(lt - rest) + 1);
    memcpy(*name, rest, (size_t)(lt - rest));
    (*name)[lt - rest] = 0;
    *ts = t;
    return 1;
}

/* 头/正文切分 + 续行合并（与 gitobjects._split 同口径）。返回正文起点。 */
static const char *obj_body(const char *data, size_t n, size_t *body_off) {
    const char *sep = NULL;
    for (size_t i = 0; i + 1 < n; i++)
        if (data[i] == '\n' && data[i + 1] == '\n') { sep = data + i; break; }
    if (sep) *body_off = (size_t)(sep - data) + 2;
    else *body_off = n;
    return sep ? sep + 2 : data + n;
}

/* 解析 commit → author/committer + message；不支持 encoding 头（回退 git）。 */
static int parse_commit_obj(const char *data, size_t n, char **author,
                            long long *ats, char atz[8], char **committer,
                            long long *cts, char ctz[8], char **msg) {
    if (!utf8_ok(data, n)) return 0;
    size_t boff = 0;
    obj_body(data, n, &boff);
    size_t hend = boff >= 2 ? boff - 2 : n;
    const char *p = data;
    const char *end = data + hend;
    int have_a = 0, have_c = 0;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        size_t ll = nl ? (size_t)(nl - p) : (size_t)(end - p);
        char *line = xmalloc(ll + 1);
        memcpy(line, p, ll);
        line[ll] = 0;
        if (!strncmp(line, "encoding ", 9)) { free(line); return 0; }
        if (ll && line[0] == ' ') { free(line); p = nl ? nl + 1 : end; continue; }
        if (!have_a && !strncmp(line, "author ", 7))
            have_a = parse_ident(line, "author", author, ats, atz);
        else if (!have_c && !strncmp(line, "committer ", 10))
            have_c = parse_ident(line, "committer", committer, cts, ctz);
        free(line);
        p = nl ? nl + 1 : end;
    }
    if (!have_a || !have_c) return 0;
    *msg = xmalloc(n - boff + 1);
    memcpy(*msg, data + boff, n - boff);
    (*msg)[n - boff] = 0;
    return 1;
}

/* 解析 annotation tag → tagger + message（object/type/tag 仅做存在性检查）。 */
static int parse_tag_obj(const char *data, size_t n, char **tagger,
                         long long *ts, char tz[8], char **msg) {
    if (!utf8_ok(data, n)) return 0;
    size_t boff = 0;
    obj_body(data, n, &boff);
    size_t hend = boff >= 2 ? boff - 2 : n;
    const char *p = data;
    const char *end = data + hend;
    int have_obj = 0, have_type = 0, have_name = 0, have_tagger = 0;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        size_t ll = nl ? (size_t)(nl - p) : (size_t)(end - p);
        char *line = xmalloc(ll + 1);
        memcpy(line, p, ll);
        line[ll] = 0;
        if (ll && line[0] == ' ') { free(line); p = nl ? nl + 1 : end; continue; }
        if (!strncmp(line, "object ", 7) && line[7]) have_obj = 1;
        else if (!strncmp(line, "type ", 5) && line[5]) have_type = 1;
        else if (!strncmp(line, "tag ", 4) && line[4]) have_name = 1;
        else if (!have_tagger && !strncmp(line, "tagger ", 7))
            have_tagger = parse_ident(line, "tagger", tagger, ts, tz);
        free(line);
        p = nl ? nl + 1 : end;
    }
    if (!have_obj || !have_type || !have_name || !have_tagger) return 0;
    *msg = xmalloc(n - boff + 1);
    memcpy(*msg, data + boff, n - boff);
    (*msg)[n - boff] = 0;
    return 1;
}

/* gitobjects.subject：到第一个空行为止，行间一个空格，行两端空白去掉。 */
static char *subject_of(const char *msg) {
    buf o;
    buf_init(&o);
    const char *p = msg ? msg : "";
    int first = 1;
    while (*p) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        while (len && (p[len - 1] == '\r' || p[len - 1] == ' ' || p[len - 1] == '\t'))
            len--;
        size_t s = 0;
        while (s < len && (p[s] == ' ' || p[s] == '\t' || p[s] == '\r')) s++;
        if (s == len) break;
        if (!first) buf_putc(&o, ' ');
        buf_append(&o, p + s, len - s);
        first = 0;
        if (!nl) break;
        p = nl + 1;
    }
    char *r = xstrdup(buf_cstr(&o));
    buf_free(&o);
    return r;
}

/* core.abbrev：-1=不确定(回退)，0=auto，1=整数，2=full。 */
static int core_abbrev_mode(long long *val) {
    *val = 0;
    if (!dotgit_usable()) return -1;
    wchar_t p[WBUF];
    _snwprintf(p, WBUF, L"%ls\\.git\\config", git_root());
    size_t n = 0;
    char *b = read_file_bytes(p, &n);
    if (!b) return 0;                      /* FileNotFound → auto */
    if (!utf8_ok(b, n)) { free(b); return -1; }
    int mode = 0;
    long long ival = 0;
    int section_core = 0, has_include = 0;
    const char *line = b;
    while (line && *line) {
        const char *nl = strchr(line, '\n');
        size_t ll = nl ? (size_t)(nl - line) : strlen(line);
        char *s = xmalloc(ll + 1);
        memcpy(s, line, ll);
        s[ll] = 0;
        char *t = strip(s);
        if (*t && *t != '#' && *t != ';') {
            if (*t == '[') {
                char *rb = strchr(t, ']');
                if (!rb) { free(s); free(b); return -1; }
                *rb = 0;
                char *inner = t + 1;
                char *q = strchr(inner, '"');
                if (q) *q = 0;
                char *is = strip(inner);
                for (char *c = is; *c; c++) *c = (char)tolower((unsigned char)*c);
                if (!strncmp(is, "include", 7)) has_include = 1;
                section_core = !strcmp(is, "core");
            } else if (section_core) {
                char *eq = strchr(t, '=');
                if (eq) {
                    *eq = 0;
                    char *k = strip(t);
                    for (char *c = k; *c; c++) *c = (char)tolower((unsigned char)*c);
                    if (!strcmp(k, "abbrev")) {
                        char *v = strip(eq + 1);
                        long long iv = 0;
                        int digits = *v != 0;
                        for (char *c = v; *c; c++)
                            if (*c < '0' || *c > '9') { digits = 0; break; }
                        if (digits && *v) {
                            for (char *c = v; *c; c++) iv = iv * 10 + (*c - '0');
                            mode = (iv >= 4) ? 1 : -1;
                            ival = iv;
                        } else if (!strcmp(v, "auto") || !*v) {
                            mode = 0;
                        } else if (!strcmp(v, "no") || !strcmp(v, "full")) {
                            mode = 2;
                        } else {
                            mode = -1;
                        }
                    }
                }
            }
        }
        free(s);
        line = nl ? nl + 1 : NULL;
    }
    free(b);
    if (has_include) return -1;
    *val = ival;
    return mode;
}

/* `%h` 的宽度：auto 从 7 起逐级试探「前缀在 cat-file 里是否唯一」（与 git 同源）。 */
static char *abbrev_sha(const char *sha, int mode, long long val) {
    if (mode == 2) return xstrdup(sha);
    int start = (mode == 1) ? (int)val : 7;
    for (int len = start; len <= (int)strlen(sha); len++) {
        char spec[64];
        memcpy(spec, sha, (size_t)len);
        spec[len] = 0;
        char got[64] = {0}, type[32] = {0};
        int st = -2;
        buf tmp;
        buf_init(&tmp);
        cat_read_ex(spec, got, sizeof(got), &tmp, type, sizeof(type), &st);
        buf_free(&tmp);
        if (st == -1) continue;                  /* ambiguous：加长再试 */
        if (st == 1 && !strcmp(got, sha)) return xstrdup(spec);
        return NULL;
    }
    return NULL;
}

/* ---------------------------------------------------------------- reflog 文件 */

static log_ent *parse_log_file(const wchar_t *path, size_t *count) {
    *count = 0;
    size_t n = 0;
    char *b = read_file_bytes(path, &n);
    if (!b) return NULL;
    log_ent *arr = NULL;
    size_t cnt = 0, cap = 0;
    const char *p = b;
    while (p && *p) {
        const char *nl = strchr(p, '\n');
        size_t ll = nl ? (size_t)(nl - p) : strlen(p);
        if (ll && p[ll - 1] == '\r') ll--;
        if (ll == 0 || blank(p)) { p = nl ? nl + 1 : NULL; continue; }
        const char *tab = memchr(p, '\t', ll);
        if (!tab) { goto fail; }
        /* 前两个 token：old new */
        const char *sp1 = memchr(p, ' ', (size_t)(tab - p));
        if (!sp1) goto fail;
        const char *sp2 = memchr(sp1 + 1, ' ', (size_t)(tab - sp1 - 1));
        if (!sp2) goto fail;
        size_t shalen = (size_t)(sp2 - (sp1 + 1));
        if (shalen != 40) goto fail;
        char sha[41];
        memcpy(sha, sp1 + 1, 40);
        sha[40] = 0;
        if (!is_sha40(sha)) goto fail;
        if (cnt == cap) {
            cap = cap ? cap * 2 : 16;
            arr = xrealloc(arr, cap * sizeof(*arr));
        }
        arr[cnt].sha = xstrdup(sha);
        arr[cnt].msg = xmalloc((size_t)((p + ll) - (tab + 1)) + 1);
        memcpy(arr[cnt].msg, tab + 1, (size_t)((p + ll) - (tab + 1)));
        arr[cnt].msg[(p + ll) - (tab + 1)] = 0;
        cnt++;
        p = nl ? nl + 1 : NULL;
    }
    free(b);
    *count = cnt;
    return arr;
fail:
    for (size_t i = 0; i < cnt; i++) { free(arr[i].sha); free(arr[i].msg); }
    free(arr);
    free(b);
    return NULL;
}

static void free_log(log_ent *e, size_t n) {
    for (size_t i = 0; i < n; i++) { free(e[i].sha); free(e[i].msg); }
    free(e);
}

static int fast_reflog(long long n, buf *o) {
    if (!dotgit_usable()) return 0;
    wchar_t p[WBUF];
    _snwprintf(p, WBUF, L"%ls\\.git\\logs\\HEAD", git_root());
    if (GetFileAttributesW(p) == INVALID_FILE_ATTRIBUTES) {
        buf_puts(o, "{\"ok\":true,\"reflog\":[]}");
        return 1;
    }
    size_t cnt = 0;
    log_ent *e = parse_log_file(p, &cnt);
    if (!e) return 0;
    long long av = 0;
    int am = core_abbrev_mode(&av);
    if (am < 0) { free_log(e, cnt); return 0; }

    buf rows;
    buf_init(&rows);
    buf_puts(&rows, "{\"ok\":true,\"reflog\":[");
    int first = 1;
    long long row = 0;
    for (long long i = (long long)cnt - 1; i >= 0 && row < n; i--) {
        char got[64] = {0}, type[32] = {0};
        int st = -2;
        buf data;
        buf_init(&data);
        cat_read_ex(e[i].sha, got, sizeof(got), &data, type, sizeof(type), &st);
        int ok = st == 1 && !strcmp(type, "commit");
        char *aname = NULL, *cname = NULL, *msg = NULL;
        long long ats = 0, cts = 0;
        char atz[8] = {0}, ctz[8] = {0};
        if (ok) ok = parse_commit_obj(buf_cstr(&data), data.n, &aname, &ats, atz,
                                      &cname, &cts, ctz, &msg);
        buf_free(&data);
        char date[64] = {0};
        char *shortsha = NULL;
        if (ok) {
            fmt_date(ats, atz, 0, date, sizeof(date));
            shortsha = abbrev_sha(e[i].sha, am, av);
            if (!shortsha) ok = 0;
        }
        if (!ok) {
            free(aname); free(cname); free(msg); free(shortsha);
            buf_free(&rows);
            free_log(e, cnt);
            return 0;                       /* 任何不确定 → 整条回退 git */
        }
        if (!first) buf_putc(&rows, ',');
        first = 0;
        buf_printf(&rows, "{\"index\":%lld,\"sha\":", row);
        json_str(&rows, e[i].sha);
        buf_puts(&rows, ",\"short\":");
        json_str(&rows, shortsha);
        buf_printf(&rows, ",\"selector\":\"HEAD@{%lld}\",\"author\":", row);
        json_str(&rows, aname);
        buf_puts(&rows, ",\"date\":");
        json_str(&rows, date);
        buf_puts(&rows, ",\"action\":");
        json_str(&rows, e[i].msg);
        buf_putc(&rows, '}');
        row++;
        free(aname); free(cname); free(msg); free(shortsha);
    }
    buf_puts(&rows, "]}");
    buf_puts(o, buf_cstr(&rows));
    buf_free(&rows);
    free_log(e, cnt);
    return 1;
}

/* ---------------------------------------------------------------- tags 快路径 */

static int ref_push(tag_ref **arr, size_t *cnt, size_t *cap, const char *name,
                    const char *sha) {
    if (*cnt == *cap) {
        *cap = *cap ? *cap * 2 : 16;
        *arr = xrealloc(*arr, *cap * sizeof(**arr));
    }
    (*arr)[*cnt].name = xstrdup(name);
    (*arr)[*cnt].sha = xstrdup(sha);
    (*cnt)++;
    return 1;
}

static int scan_tag_dir(const wchar_t *dir, const char *prefix, tag_ref **arr,
                        size_t *cnt, size_t *cap) {
    wchar_t pat[WBUF];
    _snwprintf(pat, WBUF, L"%ls\\*", dir);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pat, &fd);
    if (h == INVALID_HANDLE_VALUE) return 1;
    int ok = 1;
    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
        wchar_t child[WBUF];
        _snwprintf(child, WBUF, L"%ls\\%ls", dir, fd.cFileName);
        char *nm = w2u8(fd.cFileName);
        if (!nm) { ok = 0; break; }
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            buf np;
            buf_init(&np);
            buf_puts(&np, prefix);
            buf_puts(&np, nm);
            buf_putc(&np, '/');
            ok = scan_tag_dir(child, buf_cstr(&np), arr, cnt, cap);
            buf_free(&np);
        } else {
            size_t n = 0;
            char *b = read_file_bytes(child, &n);
            if (!b) { ok = 0; free(nm); break; }
            char *v = strip(b);
            if (!is_sha40(v)) ok = 0;
            else {
                buf full;
                buf_init(&full);
                buf_puts(&full, prefix);
                buf_puts(&full, nm);
                ref_push(arr, cnt, cap, buf_cstr(&full), v);
                buf_free(&full);
            }
            free(b);
        }
        free(nm);
        if (!ok) break;
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

static int cmp_name(const void *a, const void *b) {
    return strcmp(((const tag_ref *)a)->name, ((const tag_ref *)b)->name);
}

typedef struct {
    char *name, *sha, *type, *subject, *date;
    long long ts;
    int annotated;
} tag_row;

static int cmp_row(const void *a, const void *b) {
    const tag_row *x = a, *y = b;
    if (x->ts != y->ts) return x->ts < y->ts ? 1 : -1;   /* ts 降序 */
    return strcmp(x->name, y->name);
}

static int fast_tags(long long n, buf *o) {
    if (!dotgit_usable()) return 0;
    wchar_t gd[WBUF];
    _snwprintf(gd, WBUF, L"%ls\\.git", git_root());
    tag_ref *arr = NULL;
    size_t cnt = 0, cap = 0;

    /* packed-refs 先入（loose 后入，同名时 loose 覆盖） */
    wchar_t packed[WBUF];
    _snwprintf(packed, WBUF, L"%ls\\packed-refs", gd);
    size_t pn = 0;
    char *pb = read_file_bytes(packed, &pn);
    if (pb) {
        char *line = pb;
        while (line && *line) {
            char *nl = strchr(line, '\n');
            if (nl) *nl = 0;
            if (*line && *line != '#' && *line != '^') {
                char *sp = strchr(line, ' ');
                if (sp) {
                    *sp = 0;
                    if (is_sha40(line) && !strncmp(sp + 1, "refs/tags/", 10) &&
                        sp[11])
                        ref_push(&arr, &cnt, &cap, sp + 1 + 10, line);
                }
            }
            line = nl ? nl + 1 : NULL;
        }
        free(pb);
    }

    wchar_t tagsdir[WBUF];
    _snwprintf(tagsdir, WBUF, L"%ls\\refs\\tags", gd);
    if (GetFileAttributesW(tagsdir) != INVALID_FILE_ATTRIBUTES) {
        if (!scan_tag_dir(tagsdir, "", &arr, &cnt, &cap)) goto fail;
    }
    if (cnt > 500) goto fail;

    /* vanilla sort by name，和 Python 的 sorted(refs.items()) 同序 */
    qsort(arr, cnt, sizeof(*arr), cmp_name);

    tag_row *rows = xmalloc((cnt ? cnt : 1) * sizeof(*rows));
    memset(rows, 0, (cnt ? cnt : 1) * sizeof(*rows));
    size_t rn = 0;
    for (size_t i = 0; i < cnt; i++) {
        int dup = 0;
        for (size_t j = i + 1; j < cnt; j++)
            if (!strcmp(arr[i].name, arr[j].name)) { dup = 1; break; }
        if (dup) continue;                    /* 后面有 loose 版本，用后者 */

        char got[64] = {0}, type[32] = {0};
        int st = -2;
        buf data;
        buf_init(&data);
        cat_read_ex(arr[i].sha, got, sizeof(got), &data, type, sizeof(type), &st);
        long long ts = 0, cts = 0;
        char tz[8] = {0}, ctz[8] = {0};
        char *name = NULL, *msg = NULL, *cname = NULL;
        int ok = st == 1;
        if (ok && !strcmp(type, "tag"))
            ok = parse_tag_obj(buf_cstr(&data), data.n, &name, &ts, tz, &msg);
        else if (ok && !strcmp(type, "commit"))
            ok = parse_commit_obj(buf_cstr(&data), data.n, &cname, &cts, ctz,
                                  &name, &ts, tz, &msg);
        else
            ok = 0;
        buf_free(&data);
        if (!ok) {
            free(name); free(msg); free(cname);
            for (size_t k = 0; k < rn; k++) {
                free(rows[k].name); free(rows[k].sha); free(rows[k].type);
                free(rows[k].subject); free(rows[k].date);
            }
            free(rows);
            goto fail;
        }
        char d[32];
        fmt_date(ts, tz, 1, d, sizeof(d));
        rows[rn].name = xstrdup(arr[i].name);
        rows[rn].sha = xstrdup(arr[i].sha);
        rows[rn].type = xstrdup(type);
        rows[rn].subject = subject_of(msg);
        rows[rn].date = xstrdup(d);
        rows[rn].ts = ts;
        rows[rn].annotated = !strcmp(type, "tag");
        rn++;
        free(name); free(msg); free(cname);
    }
    qsort(rows, rn, sizeof(*rows), cmp_row);
    if (rn > (size_t)n) rn = (size_t)n;

    buf_puts(o, "{\"ok\":true,\"tags\":[");
    for (size_t i = 0; i < rn; i++) {
        if (i) buf_putc(o, ',');
        buf_puts(o, "{\"name\":");
        json_str(o, rows[i].name);
        buf_puts(o, ",\"sha\":");
        json_str(o, rows[i].sha);
        buf_puts(o, ",\"type\":");
        json_str(o, rows[i].type);
        buf_puts(o, ",\"subject\":");
        json_str(o, rows[i].subject);
        buf_puts(o, ",\"date\":");
        json_str(o, rows[i].date);
        buf_printf(o, ",\"annotated\":%s}", rows[i].annotated ? "true" : "false");
    }
    buf_puts(o, "]}");
    for (size_t i = 0; i < rn; i++) {
        free(rows[i].name); free(rows[i].sha); free(rows[i].type);
        free(rows[i].subject); free(rows[i].date);
    }
    free(rows);
    for (size_t i = 0; i < cnt; i++) { free(arr[i].name); free(arr[i].sha); }
    free(arr);
    return 1;
fail:
    for (size_t i = 0; i < cnt; i++) { free(arr[i].name); free(arr[i].sha); }
    free(arr);
    return 0;
}

/* ---------------------------------------------------------------- stash 快路径 */

static int fast_stash_lines(buf *out, long long n) {
    if (!dotgit_usable()) return 0;
    wchar_t p[WBUF];
    _snwprintf(p, WBUF, L"%ls\\.git\\logs\\refs\\stash", git_root());
    if (GetFileAttributesW(p) == INVALID_FILE_ATTRIBUTES) {
        /* 没有 reflog：ref 也没有 ⇒ 确实没 stash；ref 还在 ⇒ 回退 git */
        wchar_t r[WBUF];
        _snwprintf(r, WBUF, L"%ls\\.git\\refs\\stash", git_root());
        if (GetFileAttributesW(r) != INVALID_FILE_ATTRIBUTES) return 0;
        wchar_t packed[WBUF];
        _snwprintf(packed, WBUF, L"%ls\\.git\\packed-refs", git_root());
        size_t pn = 0;
        char *pb = read_file_bytes(packed, &pn);
        if (pb) {
            int found = strstr(pb, " refs/stash") != NULL;
            free(pb);
            if (found) return 0;
        }
        return 1;                             /* 空数组 */
    }
    size_t cnt = 0;
    log_ent *e = parse_log_file(p, &cnt);
    if (!e) return 0;
    long long row = 0;
    for (long long i = (long long)cnt - 1; i >= 0 && row < n; i--, row++) {
        buf_printf(out, "stash@{%lld}: %s\n", row, e[i].msg);
    }
    free_log(e, cnt);
    return 1;
}

/* ---------------------------------------------------------------- tags */

#define TAG_FMT "%(refname:short)" FS "%(objectname)" FS "%(objecttype)" FS \
                "%(contents:subject)" FS "%(creatordate:short)"

/* Python _classify() 里与「读」有关的两档；其余落到 history_error。 */
static const char *classify(const char *msg) {
    char low[512];
    size_t i = 0;
    for (; msg && msg[i] && i + 1 < sizeof(low); i++) {
        char c = msg[i];
        low[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    low[i] = 0;
    if (strstr(low, "not a git repository") || strstr(low, "does not have any commits"))
        return "no_commits";
    return "history_error";
}

static void handle_tags(const char *query, SOCKET s) {
    long long n;
    char raw[512];
    if (limit_of(query, 100, 1, 500, &n, raw, sizeof(raw)) < 0) {
        send_bad_limit(s, raw);
        return;
    }
    {
        buf fast;
        buf_init(&fast);
        if (fast_tags(n, &fast)) {
            http_send_json(s, 200, buf_cstr(&fast), fast.n);
            buf_free(&fast);
            return;
        }
        buf_free(&fast);
    }
    char count[64];
    snprintf(count, sizeof(count), "--count=%lld", n);
    const char *args[] = { "for-each-ref", count, "--sort=-creatordate",
                           "--format=" TAG_FMT, "refs/tags", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    if (rc != 0) {
        /* Python: msg = (stderr or stdout or "git 失败").strip() */
        buf m;
        buf_init(&m);
        if (er.n) buf_puts(&m, buf_cstr(&er));
        else if (out.n) buf_puts(&m, buf_cstr(&out));
        char *msg = strip(buf_cstr(&m));
        http_send_err(s, 400, *msg ? msg : "git 失败", classify(msg));
        buf_free(&m);
        buf_free(&out);
        buf_free(&er);
        return;
    }
    buf_free(&er);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"tags\":[");
    int first = 1;
    char *s0 = lines_of(&out);
    size_t len = out.n;
    size_t pos = 0;
    while (pos < len) {
        char *line = s0 + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;   /* splitlines 认 \r\n */
        if (blank(line)) continue;
        char *f[5];
        split_fs(line, f, 5);
        if (!first) buf_putc(&o, ',');
        first = 0;
        buf_puts(&o, "{\"name\":");
        json_str(&o, f[0]);
        buf_puts(&o, ",\"sha\":");
        json_str(&o, f[1]);
        buf_puts(&o, ",\"type\":");
        json_str(&o, f[2]);
        buf_puts(&o, ",\"subject\":");
        json_str(&o, f[3]);
        buf_puts(&o, ",\"date\":");
        json_str(&o, f[4]);
        buf_printf(&o, ",\"annotated\":%s}", strcmp(f[2], "tag") ? "false" : "true");
    }
    buf_puts(&o, "]}");
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    buf_free(&out);
}

/* ---------------------------------------------------------------- reflog */

#define REFLOG_FMT "%H" FS "%h" FS "%an" FS "%ad" FS "%gs"

static void handle_reflog(const char *query, SOCKET s) {
    long long n;
    char raw[512];
    if (limit_of(query, 30, 1, 500, &n, raw, sizeof(raw)) < 0) {
        send_bad_limit(s, raw);
        return;
    }
    {
        buf fast;
        buf_init(&fast);
        if (fast_reflog(n, &fast)) {
            http_send_json(s, 200, buf_cstr(&fast), fast.n);
            buf_free(&fast);
            return;
        }
        buf_free(&fast);
    }
    char lim[64];
    snprintf(lim, sizeof(lim), "-%lld", n);
    const char *args[] = { "reflog", lim, "--date=iso",
                           "--pretty=format:" REFLOG_FMT, NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    buf_free(&er);
    /* rc != 0（空仓就是 128）一律空数组 —— 与 Python 逐字相同。 */
    if (rc != 0) {
        buf_free(&out);
        http_send_json(s, 200, "{\"ok\":true,\"reflog\":[]}",
                       strlen("{\"ok\":true,\"reflog\":[]}"));
        return;
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"reflog\":[");
    int first = 1;
    size_t row = 0;
    char *s0 = lines_of(&out);
    size_t len = out.n;
    size_t pos = 0;
    while (pos < len) {
        char *line = s0 + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (blank(line)) continue;
        char *f[5];
        split_fs(line, f, 5);
        if (!first) buf_putc(&o, ',');
        first = 0;
        buf_printf(&o, "{\"index\":%zu,\"sha\":", row);
        json_str(&o, f[0]);
        buf_puts(&o, ",\"short\":");
        json_str(&o, f[1]);
        buf_printf(&o, ",\"selector\":\"HEAD@{%zu}\",\"author\":", row);
        json_str(&o, f[2]);
        buf_puts(&o, ",\"date\":");
        json_str(&o, f[3]);
        buf_puts(&o, ",\"action\":");
        json_str(&o, f[4]);
        buf_putc(&o, '}');
        row++;
    }
    buf_puts(&o, "]}");
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    buf_free(&out);
}

/* ---------------------------------------------------------------- stashes */

/* 试匹配 ^stash@\{(\d+)\}:\s*(.*)，匹配则写 *idx 并返回 desc 起点，
 * 不匹配返回 NULL（调用方按 Python 那样把整行当 desc）。 */
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
    while (*p && is_ws((unsigned char)*p)) p++;
    *idx = v;
    return (char *)p;
}

static void emit_stash(buf *o, const char *line, size_t rowidx, int *first) {
    char *work = xstrdup(line);
    long long idx = (long long)rowidx;
    char *desc = match_stash(work, &idx);
    if (!desc) desc = strip(work);

    char *dfull = xstrdup(desc);          /* 未切分，给 subject 兜底 */
    char *dpart = xstrdup(desc);          /* 切分用 */
    char *colon = strchr(dpart, ':');
    char *sub_src = NULL;
    if (colon) { *colon = 0; sub_src = colon + 1; }
    char *name = xstrdup(strip(dpart));
    char *sub = xstrdup(sub_src ? strip(sub_src) : "");
    if (!*sub) {                          /* subject.strip() or desc.strip() */
        free(sub);
        sub = xstrdup(strip(dfull));
    }
    char *raw = xstrdup(line);
    strip(raw);

    char ref[64];
    snprintf(ref, sizeof(ref), "stash@{%lld}", idx);
    if (!*first) buf_putc(o, ',');
    *first = 0;
    buf_printf(o, "{\"index\":%lld,\"ref\":", idx);
    json_str(o, ref);
    buf_puts(o, ",\"name\":");
    json_str(o, name);
    buf_puts(o, ",\"subject\":");
    json_str(o, sub);
    buf_puts(o, ",\"raw\":");
    json_str(o, raw);
    buf_putc(o, '}');

    free(raw);
    free(sub);
    free(name);
    free(dpart);
    free(dfull);
    free(work);
}

static void handle_stashes(const char *query, SOCKET s) {
    long long n;
    char raw[512];
    if (limit_of(query, 50, 1, 200, &n, raw, sizeof(raw)) < 0) {
        send_bad_limit(s, raw);
        return;
    }
    buf out;
    buf_init(&out);
    if (!fast_stash_lines(&out, n)) {
        char lim[64];
        snprintf(lim, sizeof(lim), "-%lld", n);
        const char *args[] = { "stash", "list", lim, NULL };
        buf er;
        buf_init(&er);
        (void)git_run(args, NULL, 0, &out, &er);  /* rc 忽略，同 Python check=False */
        buf_free(&er);
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"stashes\":[");
    int first = 1;
    size_t row = 0;
    char *s0 = lines_of(&out);
    size_t len = out.n;
    size_t pos = 0;
    while (pos < len) {
        char *line = s0 + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (blank(line)) continue;
        emit_stash(&o, line, row, &first);
        row++;
    }
    buf_puts(&o, "]}");
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    buf_free(&out);
}

/* ---------------------------------------------------------------- 分派 */

int read_history_list_get(const char *path, const char *query, SOCKET s) {
    if (!path) return 0;
    if (!strcmp(path, "/api/history/tags"))    { handle_tags(query, s);    return 1; }
    if (!strcmp(path, "/api/history/reflog"))  { handle_reflog(query, s);  return 1; }
    if (!strcmp(path, "/api/history/stashes")) { handle_stashes(query, s); return 1; }
    return 0;
}
