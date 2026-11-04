/* read_history.c —— /api/history/{file,blame}（M1 读面）
 *
 * 逐项对照 D:\project\aigit\history_ops.py 的 file_history() / blame()，
 * 以及 server.py 里这两条路由的参数口径（_qi / _qopt / _qflag / rel_in_root /
 * reject_dir）。判据是「与 Python 版响应逐项相等」，不是「能跑」。
 *
 * 几条容易走样的地方，这里刻意照抄 Python：
 *   * 参数求值顺序：file -> rel_in_root -> reject_dir -> start -> end -> limit
 *     （Python 是位置参数从左到右求值，所以坏 start 比「文件不存在」先报）；
 *   * file_history 先 _require_repo 再 _require_commits，**再看文件在不在**
 *     —— 文件不存在不是错误，是 200 + commits=[]；
 *   * blame 的「文件不存在」判据实际退化成 `not ap.exists()`
 *     （Python 那句 `cat-file -e` 的 stdout 永远是空串，`.strip() != ""` 恒假），
 *     但 cat-file 那次 spawn **仍然发生**，计数头要跟上；
 *   * 空仓的报错优先级高于 no_such_file / binary_file / blame_failed；
 *   * 截断字段（requested_first/requested_end/first_line/last_line/total_lines/
 *     truncated/next_line/limit）按 Python 的算式原样算。
 */
#include "routes.h"

#include <ctype.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "git.h"
#include "http.h"
#include "params.h"
#include "paths.h"
#include "util.h"

/* history_ops.py 的同名常量 */
#define MAX_HISTORY_ROWS    2000
#define MAX_BLAME_LINES     20000
#define DEFAULT_BLAME_LINES 500

/* history_ops.RS / FS —— 只用 FS（\x1f）。它不在 Python str.splitlines()
 * 的分行集合里，所以按行切不会把字段切开。 */
#define FS_CH '\x1f'

/* ---------------------------------------------------------------- 输出 */

/* 与 json.c::json_str 同口径，但：
 *   * 按长度写（git 输出里可能有 NUL）；
 *   * 非法 UTF-8 换成 U+FFFD —— Python 侧 stdout 是 errors="replace" 解码的。 */
static void jstr_n(buf *o, const char *s, size_t n) {
    buf_putc(o, '"');
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) {
            switch (c) {
            case '"':  buf_puts(o, "\\\""); break;
            case '\\': buf_puts(o, "\\\\"); break;
            case '\n': buf_puts(o, "\\n");  break;
            case '\r': buf_puts(o, "\\r");  break;
            case '\t': buf_puts(o, "\\t");  break;
            case '\b': buf_puts(o, "\\b");  break;
            case '\f': buf_puts(o, "\\f");  break;
            default:
                if (c < 0x20) buf_printf(o, "\\u%04x", (unsigned)c);
                else buf_putc(o, (char)c);
            }
            i++;
            continue;
        }
        int need;
        unsigned cp;
        if ((c & 0xE0) == 0xC0)      { need = 1; cp = c & 0x1Fu; }
        else if ((c & 0xF0) == 0xE0) { need = 2; cp = c & 0x0Fu; }
        else if ((c & 0xF8) == 0xF0) { need = 3; cp = c & 0x07u; }
        else { buf_puts(o, "\xEF\xBF\xBD"); i++; continue; }
        int ok = 1;
        int valid = 0;
        for (int k = 1; k <= need; k++) {
            if (i + (size_t)k >= n) { ok = 0; break; }
            unsigned char cc = (unsigned char)s[i + (size_t)k];
            if ((cc & 0xC0) != 0x80) { ok = 0; break; }
            cp = (cp << 6) | (cc & 0x3Fu);
            valid++;
        }
        int bad_cp = 0;
        if (ok && ((need == 1 && cp < 0x80) || (need == 2 && cp < 0x800) ||
                   (need == 3 && cp < 0x10000) || cp > 0x10FFFF ||
                   (cp >= 0xD800 && cp <= 0xDFFF)))
            bad_cp = 1;
        if (ok && !bad_cp) {
            buf_append(o, s + i, (size_t)need + 1);
            i += (size_t)need + 1;
        } else if (!ok && valid > 0) {
            /* 被截断的合法前缀：CPython 的 errors="replace" 只吐**一个** U+FFFD
             * （实测 b"\xe4\xb8\n" -> "\ufffd\n"），不是每个字节一个。 */
            buf_puts(o, "\xEF\xBF\xBD");
            i += (size_t)valid + 1;
        } else {
            /* 非法首字节 / 非法码点（代理区等）：CPython 逐字节吐 U+FFFD。 */
            buf_puts(o, "\xEF\xBF\xBD");
            i++;
        }
    }
    buf_putc(o, '"');
}

static void jstr(buf *o, const char *s) {
    jstr_n(o, s ? s : "", s ? strlen(s) : 0);
}

/* server.py::_err —— 只有 ok/error（+可选 code）。 */
static int reply_err(SOCKET s, const char *msg, const char *code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    jstr(&o, msg ? msg : "");
    if (code && *code) {
        buf_puts(&o, ",\"code\":");
        jstr(&o, code);
    }
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
    return 1;
}

/* HistoryError(code="blame_empty", total_lines=...) 的 to_dict() 形状 */
static int reply_blame_empty(SOCKET s, const char *msg, long long total) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    jstr(&o, msg);
    buf_puts(&o, ",\"code\":\"blame_empty\",\"total_lines\":");
    buf_printf(&o, "%lld", total);
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
    return 1;
}

/* ---------------------------------------------------------------- 参数 */

/* 路径参数用共享的 q_get_n（带长度，能看见内嵌 NUL）。
 * 曾在这里放过一份同实现的 static 副本 —— 现在 params.c 已补上，
 * 留副本会和外部声明冲突，所以删掉。 */

/* Python int(str) 的可接受子集：两端空白、可选 +/-、数字间单个 '_'。
 * 用 __int128 承接，Python 的 int 是任意精度 —— 10^30 这种 start/end 是能传到
 * `git blame -L` 的（git 自己会夹），饱和到 long long 就会变成负数并报错。 */
static void fmt_i128(char *out, size_t n, __int128 v) {
    char tmp[64];
    int i = 0;
    int neg = v < 0;
    unsigned __int128 u = neg ? (unsigned __int128)(-v) : (unsigned __int128)v;
    if (!u) tmp[i++] = '0';
    while (u) { tmp[i++] = (char)('0' + (int)(u % 10)); u /= 10; }
    size_t k = 0;
    if (neg && k + 1 < n) out[k++] = '-';
    while (i > 0 && k + 1 < n) out[k++] = tmp[--i];
    out[k] = 0;
}

/* Python repr(str) 的常见形态（用于「参数 X 必须是整数,收到 %r」）。 */
static void hist_repr_str(buf *o, const char *s, size_t n) {
    py_repr_str(o, s, n);
}


static int bad_int_err(SOCKET s, const char *key, const char *val, size_t vlen) {
    buf m;
    buf_init(&m);
    buf_printf(&m, "参数 %s 必须是整数,收到 ", key);
    hist_repr_str(&m, val, vlen);
    int r = reply_err(s, buf_cstr(&m), NULL);
    buf_free(&m);
    return r;
}

/* server.py::_qi —— 缺省或空串取默认；非法 -> 400（返回 -1，已发响应）。 */
static int qi_py(SOCKET s, const char *query, const char *key, long long dflt,
                 long long *out) {
    char *v = NULL;
    size_t vlen = 0;
    if (!q_get_n(query, key, &v, &vlen) || vlen == 0) {
        free(v);
        *out = dflt;
        return 0;
    }
    int ok = !memchr(v, 0, vlen) && py_int_parse(v, out);
    if (!ok) { bad_int_err(s, key, v, vlen); free(v); return -1; }
    free(v);
    return 0;
}

/* server.py::_qopt —— 没给/空串 -> 无；非法 -> 400（返回 -1，已发响应）。 */
static int qopt_py(SOCKET s, const char *query, const char *key,
                   int *given, __int128 *out, char **canon) {
    char *v = NULL;
    size_t vlen = 0;
    if (canon) *canon = NULL;
    if (!q_get_n(query, key, &v, &vlen) || vlen == 0) {
        free(v);
        *given = 0;
        return 0;
    }
    int ok = !memchr(v, 0, vlen) && py_int128_parse(v, out);
    if (!ok) { bad_int_err(s, key, v, vlen); free(v); return -1; }
    *given = 1;
    /* 原文本的规范十进制：C 只算到 2^126，回显必须给精确值。 */
    if (canon) *canon = py_int_canon_n(v, vlen);
    free(v);
    return 0;
}

/* server.py::_qflag —— 只认 1/true/yes/on；**缺省**才取默认。 */
static int qflag_py(const char *query, const char *key, int dflt) {
    char *v = NULL;
    size_t vlen = 0;
    /* ⚠ urllib.parse.parse_qs 默认 keep_blank_values=False：`?follow=` 等价于
     * 没给这个参数（实测：Python 侧回 follow=true）。空串必须当缺省。 */
    if (!q_get_n(query, key, &v, &vlen) || vlen == 0) { free(v); return dflt; }
    static const char *yes[] = { "1", "true", "yes", "on", NULL };
    int r = 0;
    for (int i = 0; yes[i]; i++)
        if (vlen == strlen(yes[i]) && !memcmp(v, yes[i], vlen)) { r = 1; break; }
    free(v);
    return r;
}

/* 取 file 参数：缺 / 空 -> 「缺少 file 参数」；含 NUL -> Python 的路径走私文案。
 * 返回 1 表示已发响应（调用方直接 return）。成功返回 0 并写 *out（malloc）。 */
static int take_file(SOCKET s, const char *query, char **out) {
    char *v = NULL;
    size_t vlen = 0;
    if (!q_get_n(query, "file", &v, &vlen) || vlen == 0) {
        free(v);
        reply_err(s, "缺少 file 参数", NULL);
        return 1;
    }
    if (memchr(v, 0, vlen)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "路径里不能包含 NUL 字符,已拒绝:");
        hist_repr_str(&m, v, vlen);
        int r = reply_err(s, buf_cstr(&m), NULL);
        buf_free(&m);
        free(v);
        return r;
    }
    *out = v;
    return 0;
}

/* ---------------------------------------------------------------- 路径 */

static wchar_t *abs_of(const char *rel) {
    const wchar_t *root = git_root();
    wchar_t *w = u82w(rel);
    if (!root || !w) { free(w); return NULL; }
    size_t n = wcslen(root) + wcslen(w) + 2;
    wchar_t *out = xmalloc(n * sizeof(wchar_t));
    _snwprintf(out, n, L"%ls\\%ls", root, w);
    for (wchar_t *p = out; *p; p++) if (*p == L'/') *p = L'\\';
    free(w);
    return out;
}

static DWORD attrs_of(const char *rel) {
    wchar_t *p = abs_of(rel);
    if (!p) return INVALID_FILE_ATTRIBUTES;
    DWORD a = GetFileAttributesW(p);
    free(p);
    return a;
}

static int rel_is_dir(const char *rel) {
    DWORD a = attrs_of(rel);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) != 0;
}

static int rel_exists(const char *rel) {
    return attrs_of(rel) != INVALID_FILE_ATTRIBUTES;
}

/* Path.is_file()：存在且不是目录 */
static int rel_is_file(const char *rel) {
    DWORD a = attrs_of(rel);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

static char *cat2(const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    char *o = xmalloc(la + lb + 1);
    memcpy(o, a, la);
    memcpy(o + la, b, lb);
    o[la + lb] = 0;
    return o;
}

/* ------------------------------------------------------- 路径规范化（大小写/尾斜杠）
 *
 * Python 侧拿到的是 `(root / rel).resolve()` 之后的路径再 relative_to(root)，
 * 所以 `RENAMED.TXT` / `renamed.txt/` 都会被**规约成磁盘上的真实写法**。
 * paths.c 只做越界检查、不返回规约后的相对路径，于是 C 版会把原样交给 git：
 * 实测 `git log -- RENAMED.TXT` 在 core.ignorecase=true 的仓里**匹配不到**
 * （tree 的 pathspec 匹配是大小写敏感的），count=0 而 Python 是 4。
 * 这里补一次 realpath 规约：已存在的那一段换成真实大小写，不存在的尾部原样保留
 * —— 与 Path.resolve() 在 Windows 上的行为一致。
 */

/* 最长已存在祖先的真实路径 + 原样拼回剩余后缀 */
static char *canon_rel(const char *rel) {
    return path_canon_rel(rel);
}

/* ---------------------------------------------------------------- 杂项 */

static int has_nonspace(const char *s) {
    for (; *s; s++) if (!isspace((unsigned char)*s)) return 1;
    return 0;
}

/* Python 的 subprocess(text=True) 默认开 universal newlines：stdout 里的
 * "\r\n" 与单独的 "\r" 都被翻成 "\n"。这条**不是**细节：git blame 输出的是
 * 文件内容，CRLF 文件在 Python 侧 content 不带 \r，C 版原样透传就多一个 \r
 * （实测 core.autocrlf=false + CRLF 文件）。所有要解析的 stdout 都要过一遍。 */
static void universal_nl(buf *b) {
    if (!b->p || !b->n) return;
    size_t r = 0, w = 0;
    while (r < b->n) {
        char c = b->p[r];
        if (c == '\r') {
            b->p[w++] = '\n';
            r++;
            if (r < b->n && b->p[r] == '\n') r++;
        } else {
            b->p[w++] = c;
            r++;
        }
    }
    b->n = w;
    b->p[w] = 0;
}

static int all_digits(const char *s) {
    if (!*s) return 0;
    for (; *s; s++) if (*s < '0' || *s > '9') return 0;
    return 1;
}

static long long clamp_ll(long long v, long long lo, long long hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static int repo_exists(void) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t p[32768];
    _snwprintf(p, 32768, L"%ls\\.git", root);
    return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES;
}

/* history_ops._has_commits() —— 一次 rev-parse，计入 spawn 计数 */
static int has_commits(void) {
    const char *args[] = { "rev-parse", "--verify", "--quiet", "HEAD", NULL };
    buf o;
    buf_init(&o);
    int rc = git_run(args, NULL, 0, &o, NULL);
    buf_free(&o);
    return rc == 0;
}

/* 文件总行数：工作区优先，读不到就从 HEAD 的 blob 数（Python 同款）。 */
static long long worktree_lines(const char *rel) {
    wchar_t *p = abs_of(rel);
    if (!p) return 0;
    FILE *f = _wfopen(p, L"rb");
    free(p);
    if (!f) return 0;
    long long n = 0;
    int last = 0, c;
    while ((c = fgetc(f)) != EOF) {
        if (c == '\n') n++;
        last = c;
    }
    fclose(f);
    if (last && last != '\n') n++;
    return n;
}

static char *read_head_bytes(const char *rel, size_t max, size_t *len) {
    wchar_t *p = abs_of(rel);
    if (!p) { *len = 0; return xmalloc(1); }
    FILE *f = _wfopen(p, L"rb");
    free(p);
    if (!f) { *len = 0; return xmalloc(1); }
    char *d = xmalloc(max ? max : 1);
    size_t got = fread(d, 1, max, f);
    fclose(f);
    *len = got;
    return d;
}

/* _file_total_lines 的非工作区分支：cat-file -s + cat-file blob */
static long long blob_lines(const char *rel) {
    char *headrel = cat2("HEAD:", rel);
    long long total = 0;
    {
        const char *args[] = { "cat-file", "-s", headrel, NULL };
        buf o;
        buf_init(&o);
        int rc = git_run(args, NULL, 0, &o, NULL);
        char *s = py_strip_dup(buf_cstr(&o));
        if (rc != 0 || !all_digits(s)) {
            free(s);
            buf_free(&o);
            free(headrel);
            return 0;
        }
        long long size = 0;
        (void)py_int_parse(s, &size);
        free(s);
        buf_free(&o);

        const char *args2[] = { "cat-file", "blob", headrel, NULL };
        buf b;
        buf_init(&b);
        int rc2 = git_run(args2, NULL, 0, &b, NULL);
        if (rc2 != 0) { buf_free(&b); free(headrel); return 0; }
        universal_nl(&b);
        long long nl = 0;
        for (size_t i = 0; i < b.n; i++) if (b.p[i] == '\n') nl++;
        total = nl ? nl : (size ? 1 : 0);
        buf_free(&b);
    }
    free(headrel);
    return total;
}

/* history_ops._iso_from_epoch —— epoch + tz -> "%Y-%m-%d %H:%M:%S%z" */
static void civil_from_days(long long z, int *y, unsigned *m, unsigned *d) {
    z += 719468;
    long long era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned)(z - era * 146097);
    unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    long long yy = (long long)yoe + era * 400;
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    unsigned mp = (5 * doy + 2) / 153;
    unsigned dd = doy - (153 * mp + 2) / 5 + 1;
    unsigned mm = mp < 10 ? mp + 3 : mp - 9;
    *y = (int)(yy + (mm <= 2));
    *m = mm;
    *d = dd;
}

static void iso_from_epoch(const char *epoch, const char *tz, buf *o) {
    long long e = 0;
    if (!epoch || !py_int_parse(epoch, &e)) return;      /* Python: 解析不了 -> "" */
    int sign = 1, hh = 0, mm = 0;
    if (tz && (tz[0] == '+' || tz[0] == '-') &&
        isdigit((unsigned char)tz[1]) && isdigit((unsigned char)tz[2]) &&
        isdigit((unsigned char)tz[3]) && isdigit((unsigned char)tz[4])) {
        sign = (tz[0] == '-') ? -1 : 1;
        hh = (tz[1] - '0') * 10 + (tz[2] - '0');
        mm = (tz[3] - '0') * 10 + (tz[4] - '0');
    }
    long long off = (long long)sign * (hh * 3600LL + mm * 60LL);
    long long t = e + off;
    long long days = t / 86400;
    long long secs = t % 86400;
    if (secs < 0) { secs += 86400; days -= 1; }
    int y;
    unsigned mo, d;
    civil_from_days(days, &y, &mo, &d);
    buf_printf(o, "%04d-%02u-%02u %02u:%02u:%02u%c%02d%02d",
               y, mo, d, (unsigned)(secs / 3600), (unsigned)((secs % 3600) / 60),
               (unsigned)(secs % 60), sign > 0 ? '+' : '-', hh, mm);
}

/* ---------------------------------------------------------------- 文件历史 */

typedef struct {
    char *status, *score, *oldp, *newp, *path;
    int   rename;
} fent;

typedef struct {
    fent  *v;
    size_t n;
} fents;

typedef struct {
    char  *sha, *shortsha, *author, *email, *date, *subject;
    char **parents;
    size_t nparents;
    fents  files;
} hrow;

typedef struct {
    hrow  *v;
    size_t n;
} hrows;

static char **ws_split(char *s, size_t *n) {
    char **out = NULL;
    size_t cnt = 0;
    char *p = s;
    while (*p) {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;
        char *start = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if (*p) { *p = 0; p++; }
        out = xrealloc(out, (cnt + 1) * sizeof(char *));
        out[cnt++] = start;
    }
    *n = cnt;
    return out;
}

/* re.match(r"^[AMDRTCU]\d*\t", line) */
static int is_name_status(const char *s) {
    if (!s[0] || !strchr("AMDRTCU", s[0])) return 0;
    const char *p = s + 1;
    while (*p >= '0' && *p <= '9') p++;
    return *p == '\t';
}

/* 按 Python 的 line.splitlines() 口径切行（会吃掉行尾 \r），就地写 NUL。 */
static void parse_file_history(char *out, hrows *rows) {
    char *p = out;
    for (;;) {
        char *nl = strchr(p, '\n');
        char *line = p;
        if (nl) { *nl = 0; p = nl + 1; } else { p = NULL; }
        size_t len = strlen(line);
        while (len && line[len - 1] == '\r') line[--len] = 0;

        if (has_nonspace(line)) {
            if (strchr(line, FS_CH)) {
                size_t qn = 0;
                char **q = split_inplace(line, FS_CH, &qn);
                if (qn >= 6) {
                    hrow r;
                    memset(&r, 0, sizeof r);
                    r.sha = q[0];
                    r.shortsha = q[1];
                    r.author = q[2];
                    r.email = q[3];
                    r.date = q[4];
                    r.subject = q[5];
                    if (qn > 6) r.parents = ws_split(q[6], &r.nparents);
                    rows->v = xrealloc(rows->v, (rows->n + 1) * sizeof(hrow));
                    rows->v[rows->n++] = r;
                }
                free(q);
            } else if (is_name_status(line) && rows->n) {
                hrow *r = &rows->v[rows->n - 1];
                size_t pn = 0;
                char **parts = split_inplace(line, '\t', &pn);
                size_t k = 0;
                while (parts[0][k] && !isdigit((unsigned char)parts[0][k])) k++;
                char *status = xstrndup(parts[0], k);
                fent fe;
                memset(&fe, 0, sizeof fe);
                fe.status = status;
                if (status[0] == 'R' && pn >= 3) {
                    fe.score = parts[0] + k;
                    fe.oldp = parts[1];
                    fe.newp = parts[2];
                    fe.rename = 1;
                } else {
                    fe.path = parts[pn - 1];
                    fe.rename = 0;
                }
                r->files.v = xrealloc(r->files.v, (r->files.n + 1) * sizeof(fent));
                r->files.v[r->files.n++] = fe;
                free(parts);
            }
        }
        if (!p) break;
    }
}

static void free_file_history(hrows *rows) {
    for (size_t i = 0; i < rows->n; i++) {
        free(rows->v[i].parents);
        for (size_t k = 0; k < rows->v[i].files.n; k++)
            free(rows->v[i].files.v[k].status);
        free(rows->v[i].files.v);
    }
    free(rows->v);
    rows->v = NULL;
    rows->n = 0;
}

static int file_history_json(SOCKET s, const char *rel, long long limit, int follow) {
    if (!repo_exists()) {
        char *root = w2u8(git_root());
        buf m;
        buf_init(&m);
        buf_printf(&m, "不是一个 git 仓库(或路径不存在):%s", root ? root : "");
        free(root);
        int r = reply_err(s, buf_cstr(&m), "not_a_repo");
        buf_free(&m);
        return r;
    }
    if (!has_commits())
        return reply_err(s, "仓库还没有任何提交,看文件历史无从谈起", "no_commits");

    long long n = clamp_ll(limit, 1, MAX_HISTORY_ROWS);
    char narg[32];
    snprintf(narg, sizeof narg, "-%lld", n);

    buf out;
    buf_init(&out);
    {
        const char *args[12];
        int ai = 0;
        args[ai++] = "log";
        args[ai++] = narg;
        args[ai++] = "--date=iso";
        args[ai++] = "--pretty=format:%H\x1f%h\x1f%an\x1f%ae\x1f%ad\x1f%s\x1f%P";
        if (follow) args[ai++] = "--follow";
        args[ai++] = "-M";
        args[ai++] = "--name-status";
        args[ai++] = "--";
        args[ai++] = rel;
        args[ai] = NULL;
        git_run(args, NULL, 0, &out, NULL);
    }

    universal_nl(&out);

    hrows rows;
    memset(&rows, 0, sizeof rows);
    parse_file_history(buf_cstr(&out), &rows);

    int truncated = 0;
    if ((long long)rows.n >= n) {
        const char *args[] = { "rev-list", "--count", "--max-count=100000",
                               "HEAD", "--", rel, NULL };
        buf ro;
        buf_init(&ro);
        git_run(args, NULL, 0, &ro, NULL);
        universal_nl(&ro);
        char *more = py_strip_dup(buf_cstr(&ro));
        if (all_digits(more)) {
            long long mv = 0;
            (void)py_int_parse(more, &mv);
            if (mv > (long long)rows.n) truncated = 1;
        }
        free(more);
        buf_free(&ro);
    }

    int exists = rel_exists(rel);

    int tracked = 0;
    {
        const char *args[] = { "ls-files", "--", rel, NULL };
        buf to;
        buf_init(&to);
        git_run(args, NULL, 0, &to, NULL);
        universal_nl(&to);
        char *t = py_strip_dup(buf_cstr(&to));
        tracked = (*t != 0);
        free(t);
        buf_free(&to);
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    jstr(&o, rel);
    buf_puts(&o, ",\"follow\":");
    buf_puts(&o, follow ? "true" : "false");
    buf_printf(&o, ",\"count\":%zu", rows.n);
    buf_puts(&o, ",\"truncated\":");
    buf_puts(&o, truncated ? "true" : "false");
    buf_puts(&o, ",\"commits\":[");
    for (size_t i = 0; i < rows.n; i++) {
        hrow *r = &rows.v[i];
        if (i) buf_putc(&o, ',');
        buf_puts(&o, "{\"sha\":");
        jstr(&o, r->sha);
        buf_puts(&o, ",\"short\":");
        jstr(&o, r->shortsha);
        buf_puts(&o, ",\"author\":");
        jstr(&o, r->author);
        buf_puts(&o, ",\"email\":");
        jstr(&o, r->email);
        buf_puts(&o, ",\"date\":");
        jstr(&o, r->date);
        buf_puts(&o, ",\"subject\":");
        jstr(&o, r->subject);
        buf_puts(&o, ",\"parents\":[");
        for (size_t k = 0; k < r->nparents; k++) {
            if (k) buf_putc(&o, ',');
            jstr(&o, r->parents[k]);
        }
        buf_puts(&o, "],\"files\":[");
        for (size_t k = 0; k < r->files.n; k++) {
            fent *fe = &r->files.v[k];
            if (k) buf_putc(&o, ',');
            if (fe->rename) {
                buf_puts(&o, "{\"status\":");
                jstr(&o, fe->status);
                buf_puts(&o, ",\"score\":");
                jstr(&o, fe->score);
                buf_puts(&o, ",\"old\":");
                jstr(&o, fe->oldp);
                buf_puts(&o, ",\"new\":");
                jstr(&o, fe->newp);
                buf_puts(&o, ",\"rename\":true}");
            } else {
                buf_puts(&o, "{\"status\":");
                jstr(&o, fe->status);
                buf_puts(&o, ",\"path\":");
                jstr(&o, fe->path);
                buf_puts(&o, ",\"rename\":false}");
            }
        }
        buf_puts(&o, "]}");
    }
    buf_puts(&o, "],\"exists_in_worktree\":");
    buf_puts(&o, exists ? "true" : "false");
    buf_puts(&o, ",\"tracked\":");
    buf_puts(&o, tracked ? "true" : "false");
    buf_putc(&o, '}');

    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free_file_history(&rows);
    buf_free(&out);
    return 1;
}

static int handle_file(SOCKET s, const char *query) {
    char *fbuf = NULL;
    if (take_file(s, query, &fbuf)) return 1;

    char *perr = NULL;
    char *rel0 = path_rel_in_root(fbuf, &perr);
    free(fbuf);
    if (!rel0) {
        int r = reply_err(s, perr ? perr : "路径不合法,已拒绝", NULL);
        free(perr);
        return r;
    }
    char *rel = canon_rel(rel0);
    free(rel0);
    if (rel_is_dir(rel)) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "不是文件(是目录),已拒绝:%s", rel);
        int r = reply_err(s, buf_cstr(&m), NULL);
        buf_free(&m);
        free(rel);
        return r;
    }

    long long limit = 50;
    if (qi_py(s, query, "limit", 50, &limit) < 0) { free(rel); return 1; }
    int follow = qflag_py(query, "follow", 1);

    int r = file_history_json(s, rel, limit, follow);
    free(rel);
    return r;
}

/* ---------------------------------------------------------------- blame */

typedef struct {
    long long line;
    char *sha, *author, *email, *summary, *filename, *previous, *content;
    size_t content_len;
    buf   time;
} brec;

typedef struct {
    brec  *v;
    size_t n;
} brecs;

static const char *meta_get(char **keys, char **vals, size_t n, const char *k) {
    for (size_t i = 0; i < n; i++)
        if (keys[i] && !strcmp(keys[i], k)) return vals[i];
    return NULL;
}

static void free_blame(brecs *recs, char **keys, char **vals, size_t nmeta,
                       char **lines, size_t *lens) {
    for (size_t i = 0; i < recs->n; i++) buf_free(&recs->v[i].time);
    free(recs->v);
    free(keys);
    free(vals);
    free(lines);
    free(lens);
    recs->v = NULL;
    recs->n = 0;
}

static int blame_json(SOCKET s, const char *rel, int have_start, __int128 start,
                      int have_end, __int128 end, long long limit_in,
                      const char *start_canon, const char *end_canon) {
    if (!repo_exists()) {
        char *root = w2u8(git_root());
        buf m;
        buf_init(&m);
        buf_printf(&m, "不是一个 git 仓库(或路径不存在):%s", root ? root : "");
        free(root);
        int r = reply_err(s, buf_cstr(&m), "not_a_repo");
        buf_free(&m);
        return r;
    }

    /* 文件不存在 —— Python 那句 `cat-file -e` 的判据恒假，但 spawn 照跑。 */
    if (!rel_exists(rel)) {
        char *headrel = cat2("HEAD:", rel);
        const char *args[] = { "cat-file", "-e", headrel, NULL };
        buf o;
        buf_init(&o);
        git_run(args, NULL, 0, &o, NULL);
        buf_free(&o);
        free(headrel);
        if (!has_commits())
            return reply_err(s, "仓库还没有任何提交,blame无从谈起", "no_commits");
        buf m;
        buf_init(&m);
        buf_printf(&m, "文件不存在,也没有历史:%s", rel);
        int r = reply_err(s, buf_cstr(&m), "no_such_file");
        buf_free(&m);
        return r;
    }

    /* 二进制检测：工作区前 8000 字节（非普通文件才退回 blob）。 */
    {
        char *data = NULL;
        size_t dlen = 0;
        if (rel_is_file(rel)) {
            data = read_head_bytes(rel, 8000, &dlen);
        } else {
            char *headrel = cat2("HEAD:", rel);
            const char *args[] = { "cat-file", "blob", headrel, NULL };
            buf bo;
            buf_init(&bo);
            git_run(args, NULL, 0, &bo, NULL);
            universal_nl(&bo);
            dlen = bo.n > 8000 ? 8000 : bo.n;
            data = xmalloc(dlen ? dlen : 1);
            if (dlen) memcpy(data, bo.p, dlen);
            buf_free(&bo);
            free(headrel);
        }
        int binary = (dlen && memchr(data, 0, dlen) != NULL);
        free(data);
        if (binary) {
            if (!has_commits())
                return reply_err(s, "仓库还没有任何提交,blame无从谈起", "no_commits");
            return reply_err(s, "这是二进制文件,blame 没有意义。", "binary_file");
        }
    }

    long long limit = clamp_ll(limit_in, 1, MAX_BLAME_LINES);
    long long total = rel_is_file(rel) ? worktree_lines(rel) : blob_lines(rel);

    __int128 first = 1;
    if (have_start) first = start > 1 ? start : 1;
    __int128 last = total
        ? (((__int128)total < first + limit - 1) ? (__int128)total
                                                 : first + limit - 1)
        : first + limit - 1;
    if (have_end) {
        if (end < first) {
            char s_end[64], s_first[64];
            fmt_i128(s_end, sizeof s_end, end);
            fmt_i128(s_first, sizeof s_first, first);
            buf m;
            buf_init(&m);
            /* 负的超大 end 也要按用户原文回显（Python 的 int 无上限）。 */
            const char *e_txt = end_canon ? end_canon : s_end;
            buf_printf(&m, "end_line(%s) 不能小于 start_line(%s)", e_txt, s_first);
            int r = reply_err(s, buf_cstr(&m), "bad_line");
            buf_free(&m);
            return r;
        }
        if (end < last) last = end;
    }

    buf out;
    buf_init(&out);
    int rc;
    {
        char s_first[64], s_last[64], lrange[160];
        fmt_i128(s_first, sizeof s_first, first);
        fmt_i128(s_last, sizeof s_last, last);
        snprintf(lrange, sizeof lrange, "%s,%s", s_first, s_last);
        const char *args[8];
        int ai = 0;
        args[ai++] = "blame";
        args[ai++] = "--line-porcelain";
        if (total || have_end) {
            args[ai++] = "-L";
            args[ai++] = lrange;
        }
        args[ai++] = "--";
        args[ai++] = rel;
        args[ai] = NULL;
        buf errb;
        buf_init(&errb);
        rc = git_run(args, NULL, 0, &out, &errb);
        if (rc != 0) {
            char *msg = py_strip_dup(errb.n ? buf_cstr(&errb) : buf_cstr(&out));
            buf_free(&errb);
            buf_free(&out);
            if (!has_commits()) {
                free(msg);
                return reply_err(s, "仓库还没有任何提交,blame无从谈起", "no_commits");
            }
            buf m;
            buf_init(&m);
            buf_puts(&m, "git blame 失败:");
            buf_puts(&m, msg);
            free(msg);
            int r = reply_err(s, buf_cstr(&m), "blame_failed");
            buf_free(&m);
            return r;
        }
        buf_free(&errb);
    }

    universal_nl(&out);

    /* 按 "\n" 切（Python 用的是 split("\n")，**不吃** \r）。
     * 用 memchr + 长度而不是 strchr：内容行里可能有 NUL（二进制检测只看前
     * 8000 字节，之后的 NUL 不会触发 binary_file），Python 的字符串能带 NUL，
     * C 的 C 字符串不能 —— 实测 content 会被截断 9005 -> 9000 字节。 */
    char **lines = NULL;
    size_t *lens = NULL;
    size_t nlines = 0;
    {
        char *p = out.p;
        size_t rem = out.n;
        for (;;) {
            char *nl = memchr(p, '\n', rem);
            size_t len = nl ? (size_t)(nl - p) : rem;
            lines = xrealloc(lines, (nlines + 1) * sizeof(char *));
            lens = xrealloc(lens, (nlines + 1) * sizeof(size_t));
            lines[nlines] = p;
            lens[nlines] = len;
            nlines++;
            if (!nl) break;
            *nl = 0;
            rem -= len + 1;
            p = nl + 1;
        }
    }

    brecs recs;
    memset(&recs, 0, sizeof recs);
    char **keys = NULL, **vals = NULL;
    size_t nmeta = 0;

    size_t i = 0;
    while (i < nlines) {
        char *hdr = lines[i];
        if (!hdr[0] || hdr[0] == '\t') { i++; continue; }
        size_t pn = 0;
        char **parts = split_inplace(hdr, ' ', &pn);
        if (pn < 3) { free(parts); i++; continue; }
        char *sha = parts[0];
        char *final = parts[1];

        for (size_t k = 0; k < nmeta; k++) { keys[k] = NULL; vals[k] = NULL; }
        nmeta = 0;
        size_t j = i + 1;
        while (j < nlines && lines[j][0] != '\t') {
            char *sp = strchr(lines[j], ' ');
            char *k, *v;
            if (sp) { *sp = 0; k = lines[j]; v = sp + 1; }
            else { k = lines[j]; v = lines[j] + strlen(lines[j]); }
            int dup = 0;
            for (size_t m = 0; m < nmeta; m++)
                if (keys[m] && !strcmp(keys[m], k)) { dup = 1; break; }
            if (!dup) {
                keys = xrealloc(keys, (nmeta + 1) * sizeof(char *));
                vals = xrealloc(vals, (nmeta + 1) * sizeof(char *));
                keys[nmeta] = k;
                vals[nmeta] = v;
                nmeta++;
            }
            j++;
        }
        char *content = (j < nlines && lens[j] > 0 && lines[j][0] == '\t')
                        ? lines[j] + 1 : "";
        size_t content_len = (j < nlines && lens[j] > 0 && lines[j][0] == '\t')
                             ? lens[j] - 1 : 0;

        long long lno = 0;
        (void)py_int_parse(final, &lno);

        brec r;
        memset(&r, 0, sizeof r);
        buf_init(&r.time);
        r.line = lno;
        r.sha = sha;
        const char *a = meta_get(keys, vals, nmeta, "author");
        const char *ae = meta_get(keys, vals, nmeta, "author-mail");
        const char *at = meta_get(keys, vals, nmeta, "author-time");
        const char *az = meta_get(keys, vals, nmeta, "author-tz");
        const char *su = meta_get(keys, vals, nmeta, "summary");
        const char *fn = meta_get(keys, vals, nmeta, "filename");
        const char *pv = meta_get(keys, vals, nmeta, "previous");
        r.author = (char *)(a ? a : "");
        r.email = (char *)(ae ? ae : "");
        r.summary = (char *)(su ? su : "");
        r.filename = (char *)(fn ? fn : rel);
        r.previous = (char *)(pv ? pv : "");
        r.content = content;
        r.content_len = content_len;
        iso_from_epoch(at ? at : "0", az, &r.time);

        recs.v = xrealloc(recs.v, (recs.n + 1) * sizeof(brec));
        recs.v[recs.n++] = r;
        free(parts);
        i = j + 1;
    }

    if (!recs.n) {
        free_blame(&recs, keys, vals, nmeta, lines, lens);
        buf_free(&out);
        return reply_blame_empty(
            s, "blame 没有返回任何行(文件可能是空的,或范围选错了)。", total);
    }

    long long covered = recs.v[recs.n - 1].line;
    int truncated = (total && covered < total) ? 1 : 0;

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    jstr(&o, rel);
    buf_printf(&o, ",\"count\":%zu", recs.n);
    {
        char s_first[64], s_last[64];
        fmt_i128(s_first, sizeof s_first, first);
        fmt_i128(s_last, sizeof s_last, last);
        /* 用户给的大整数按原样回显（Python 的 int 没有上限）：
         * 只在「确实取了用户值」时才用规范文本，否则用夹取后的内部值。 */
        const char *f = (start_canon && first > 1) ? start_canon : s_first;
        const char *l = (end_canon && end > 0 && last == end) ? end_canon : s_last;
        buf_printf(&o, ",\"requested_first\":%s", f);
        buf_printf(&o, ",\"requested_end\":%s", l);
    }
    buf_printf(&o, ",\"first_line\":%lld", recs.v[0].line);
    buf_printf(&o, ",\"last_line\":%lld", covered);
    buf_printf(&o, ",\"total_lines\":%lld", total);
    buf_puts(&o, ",\"truncated\":");
    buf_puts(&o, truncated ? "true" : "false");
    if (truncated) buf_printf(&o, ",\"next_line\":%lld", covered + 1);
    else buf_puts(&o, ",\"next_line\":null");
    buf_printf(&o, ",\"limit\":%lld", limit);
    buf_puts(&o, ",\"lines\":[");
    for (size_t k = 0; k < recs.n; k++) {
        brec *r = &recs.v[k];
        if (k) buf_putc(&o, ',');
        buf_printf(&o, "{\"line\":%lld,\"sha\":", r->line);
        jstr(&o, r->sha);
        buf_puts(&o, ",\"short\":");
        jstr_n(&o, r->sha, strlen(r->sha) > 8 ? 8 : strlen(r->sha));
        buf_puts(&o, ",\"author\":");
        jstr(&o, r->author);
        buf_puts(&o, ",\"author_email\":");
        jstr(&o, r->email);
        buf_puts(&o, ",\"time\":");
        jstr_n(&o, r->time.p ? r->time.p : "", r->time.n);
        buf_puts(&o, ",\"summary\":");
        jstr(&o, r->summary);
        buf_puts(&o, ",\"filename\":");
        jstr(&o, r->filename);
        buf_puts(&o, ",\"previous\":");
        jstr(&o, r->previous);
        buf_puts(&o, ",\"content\":");
        jstr_n(&o, r->content, r->content_len);
        buf_putc(&o, '}');
    }
    buf_puts(&o, "]}");

    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free_blame(&recs, keys, vals, nmeta, lines, lens);
    buf_free(&out);
    return 1;
}

static int handle_blame(SOCKET s, const char *query) {
    char *fbuf = NULL;
    if (take_file(s, query, &fbuf)) return 1;

    char *perr = NULL;
    char *rel0 = path_rel_in_root(fbuf, &perr);
    free(fbuf);
    if (!rel0) {
        int r = reply_err(s, perr ? perr : "路径不合法,已拒绝", NULL);
        free(perr);
        return r;
    }
    char *rel = canon_rel(rel0);
    free(rel0);
    if (rel_is_dir(rel)) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "不是文件(是目录),已拒绝:%s", rel);
        int r = reply_err(s, buf_cstr(&m), NULL);
        buf_free(&m);
        free(rel);
        return r;
    }

    /* 参数求值顺序与 Python 的位置参数一致：start -> end -> limit */
    __int128 start = 0, end = 0;
    long long limit = DEFAULT_BLAME_LINES;
    int have_start = 0, have_end = 0;
    char *start_canon = NULL, *end_canon = NULL;
    if (qopt_py(s, query, "start", &have_start, &start, &start_canon) < 0) {
        free(rel);
        return 1;
    }
    if (qopt_py(s, query, "end", &have_end, &end, &end_canon) < 0) {
        free(start_canon);
        free(rel);
        return 1;
    }
    if (qi_py(s, query, "limit", DEFAULT_BLAME_LINES, &limit) < 0) {
        free(rel);
        return 1;
    }

    int r = blame_json(s, rel, have_start, start, have_end, end, limit,
                       start_canon, end_canon);
    free(start_canon);
    free(end_canon);
    free(rel);
    return r;
}

/* ---------------------------------------------------------------- 入口 */

int read_history_get(const char *path, const char *query, SOCKET s) {
    if (!path) return 0;
    if (!strcmp(path, "/api/history/file"))  return handle_file(s, query);
    if (!strcmp(path, "/api/history/blame")) return handle_blame(s, query);
    return 0;
}
