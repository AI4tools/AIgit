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
    char lim[64];
    snprintf(lim, sizeof(lim), "-%lld", n);
    const char *args[] = { "stash", "list", lim, NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    (void)git_run(args, NULL, 0, &out, &er);      /* rc 忽略，同 Python check=False */
    buf_free(&er);

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
