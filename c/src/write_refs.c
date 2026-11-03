/* write_refs.c —— M3 引用/历史写第二组：revert / revert-abort / 分支改名 /
 * 分支删除 / 打标签 / 删标签。
 *
 * 对位 Python（server.py::do_POST + history_ops.py）：
 *   POST /api/history/revert         -> do_revert(sha, mainline, no_edit=True)
 *   POST /api/history/revert/abort   -> do_revert_abort()
 *   POST /api/history/branch/rename  -> do_branch_rename(old, new, confirmed)
 *   POST /api/history/branch/delete  -> do_branch_delete(name, force, confirmed)
 *   POST /api/history/tag/create     -> do_tag_create(name, sha, message, force)
 *   POST /api/history/tag/delete     -> do_tag_delete(name, confirmed)
 *
 * 六个端点都是 `return self._json(dict)` —— **不经过** _ok_with_status，所以响应
 * 里没有 status / root 字段，成功就是模块自己的 dict，失败是 HistoryError 的
 * {"ok":false,"error","code",**extra}（400）。
 *
 * 逐项对齐的要点（都是读 Python 源码确认过的）：
 *   * 参数求值顺序：`do_xxx(b.get(..), _bb(b, "confirmed"))` 里 _bb 先求值，
 *     所以「confirmed 类型不对」的 400 **早于**任何名字/sha 校验。顺序照抄。
 *   * `validate_ref_name`：先 strip（Unicode 空白），再空/长度（**按码点**）/
 *     `[\w./-]{1,100}` fullmatch/前导 -、/ . /尾随 / . /..、//、@{、@、.lock、
 *     控制字符，逐条同序同文案。
 *   * `_check_sha`：str+非空 -> strip -> 不以 - 开头 -> `[A-Za-z0-9_./^~@{}\-]{1,64}`
 *     -> `rev-parse --verify --quiet <rev>^{commit}`。
 *   * `_ok()` 的 output 是 **(stdout+stderr).strip()**，且 Python 用 text=True
 *     （universal newlines），所以 \r\n / \r 要先折成 \n 再 strip。
 *   * 分支删除：plan 用**strip 后**的名字，真正执行 `git branch -d/-D` 和响应里的
 *     `deleted` 用**原始**名字（Python 就是这样，带空格的名字会在 git 那步失败）。
 *   * 分支删除的 `unmerged_count` 基准是 default_branch()（远端 HEAD -> main ->
 *     master -> None），`default_branch() or "HEAD"`。
 *   * 标签：`sha or "HEAD"`（falsy 一律退化），`message` 走 _need_msg（只收 str/None，
 *     不许 NUL），空串 = 轻量标签（annotated=false）。
 *   * revert 成功响应里的 `mainline` 是**请求体里的原值**（None 或合法 int）。
 */
#include "routes.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "bodyjson.h"
#include "git.h"
#include "http.h"
#include "json.h"
#include "util.h"
#include "writeutil.h"

/* ---------------------------------------------------------------- 文案 */

#define MSG_NOT_REPO     "不是一个 git 仓库(或路径不存在):"
#define MSG_REVERT_IDLE  "现在没有进行中的 revert,没有东西可中止。"
#define MSG_REVERT_DIRTY "仓库处于冲突未解决状态,先解决冲突或 git revert --abort。"
#define MSG_RENAME_SAME  "新旧分支名相同,什么都不用做。"
#define MSG_ONLY_BRANCH \
    "这是仓库里唯一的分支,删掉之后 HEAD 就没有落脚点了。git 本身也会拒绝。"

/* ---------------------------------------------------------------- 响应 */

static void send_400(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
}

/* json_str 的「只写转义后的内容、不带引号」版本，且按长度写（错误文案里可能
 * 回显带 NUL 的 sha —— Python 那边 str 能装 NUL，json.dumps 会写成 \u0000）。 */
static void json_esc(buf *o, const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned c = (unsigned char)s[i];
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
}

/* HistoryError -> 400：{"ok":false,"error":msg,"code":code,<extra>}。 */
static void send_hist(SOCKET s, const char *msg, const char *code,
                      const char *extra_json) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    if (code && *code) {
        buf_puts(&o, ",\"code\":");
        json_str(&o, code);
    }
    if (extra_json && *extra_json) {
        buf_putc(&o, ',');
        buf_puts(&o, extra_json);
    }
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
}

/* 三段拼接的错误文案（中段按长度写，可能含 NUL）。 */
static void send_hist3(SOCKET s, const char *a, const char *b, size_t bn,
                       const char *c, const char *code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":\"");
    json_esc(&o, a, strlen(a));
    if (b) json_esc(&o, b, bn);
    if (c) json_esc(&o, c, strlen(c));
    buf_putc(&o, '"');
    if (code && *code) {
        buf_puts(&o, ",\"code\":");
        json_str(&o, code);
    }
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
}

/* ---------------------------------------------------------------- 文本口径 */

/* Python `str.strip()` 的 ASCII 空白集。 */
static int py_space(unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

/* `text=True` 的 universal newlines：\r\n 与孤立 \r 都折成 \n。 */
static char *py_text(const char *p, size_t n) {
    char *o = xmalloc(n + 1);
    size_t k = 0;
    for (size_t i = 0; i < n; i++) {
        if (p[i] == '\r') {
            if (i + 1 < n && p[i + 1] == '\n') i++;
            o[k++] = '\n';
        } else {
            o[k++] = p[i];
        }
    }
    o[k] = 0;
    return o;
}

/* 就地 strip（前后都去），返回同一个指针。 */
static char *py_strip(char *s) {
    size_t n = strlen(s);
    size_t a = 0;
    while (a < n && py_space((unsigned char)s[a])) a++;
    if (a) memmove(s, s + a, n - a + 1);
    n -= a;
    while (n > 0 && py_space((unsigned char)s[n - 1])) s[--n] = 0;
    return s;
}

/* UTF-8 解一个码点。非法序列返回 0（Python 侧是 U+FFFD，同样不是 \w）。 */
static int next_cp(const unsigned char *s, size_t n, size_t *i, unsigned *cp) {
    unsigned char c = s[*i];
    if (c < 0x80) { *cp = c; (*i)++; return 1; }
    int need;
    unsigned v;
    if (c >= 0xc2 && c <= 0xdf) { need = 1; v = c & 0x1fu; }
    else if (c >= 0xe0 && c <= 0xef) { need = 2; v = c & 0x0fu; }
    else if (c >= 0xf0 && c <= 0xf4) { need = 3; v = c & 0x07u; }
    else { (*i)++; return 0; }
    if (*i + (size_t)need >= n) { (*i)++; return 0; }
    for (int k = 1; k <= need; k++) {
        unsigned char cc = s[*i + (size_t)k];
        if ((cc & 0xc0) != 0x80) { (*i)++; return 0; }
        v = (v << 6) | (cc & 0x3fu);
    }
    if ((need == 2 && v < 0x800) || (need == 3 && v < 0x10000) ||
        (v >= 0xd800 && v <= 0xdfff) || v > 0x10ffff) { (*i)++; return 0; }
    *i += (size_t)need + 1;
    *cp = v;
    return 1;
}

/* Python 的 `\w`（str 模式，Unicode）：字母/数字/下划线。 */
static int word_cp(unsigned cp) {
    if (cp < 0x80)
        return (cp >= '0' && cp <= '9') || (cp >= 'a' && cp <= 'z') ||
               (cp >= 'A' && cp <= 'Z') || cp == '_';
    wchar_t w[2];
    int n = 0;
    if (cp <= 0xffff) {
        w[n++] = (wchar_t)cp;
    } else {
        unsigned v = cp - 0x10000;
        w[n++] = (wchar_t)(0xd800 + (v >> 10));
        w[n++] = (wchar_t)(0xdc00 + (v & 0x3ff));
    }
    WORD t[2] = {0, 0};
    if (!GetStringTypeW(CT_CTYPE1, w, n, t)) return 0;
    for (int i = 0; i < n; i++)
        if (t[i] & (C1_ALPHA | C1_DIGIT)) return 1;
    return 0;
}

/* Python `str.strip()` 的空白判定，含非 ASCII 空白（U+00A0 / U+3000 …）。 */
static int py_space_cp(unsigned cp) {
    if (cp < 0x80) return py_space((unsigned char)cp);
    return cp == 0x85 || cp == 0xa0 || cp == 0x1680 ||
           (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 ||
           cp == 0x202f || cp == 0x205f || cp == 0x3000;
}

/* `str.strip()`：按码点去前后空白。返回 malloc 的串，*outn 是长度（可能含 NUL）。 */
static char *strip_utf8(const char *s, size_t n, size_t *outn) {
    size_t i = 0, start = 0, end = 0;
    int seen = 0;
    while (i < n) {
        size_t j = i;
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &j, &cp)) { j = i + 1; cp = 0xfffd; }
        if (!py_space_cp(cp)) {
            if (!seen) { start = i; seen = 1; }
            end = j;
        }
        i = j;
    }
    if (!seen) { *outn = 0; return xstrdup(""); }
    *outn = end - start;
    return xstrndup(s + start, end - start);
}

/* Python `s[:maxcp]`（按码点截断）。 */
static char *cut_cp(const char *s, size_t n, size_t maxcp) {
    size_t i = 0, count = 0, end = 0;
    while (i < n && count < maxcp) {
        size_t j = i;
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &j, &cp)) j = i + 1;
        i = j;
        count++;
        end = j;
    }
    return xstrndup(s, end);
}

/* 码点计数（`len(name)` 用的是字符数，不是字节数）。 */
static size_t cp_count(const char *s, size_t n) {
    size_t i = 0, c = 0;
    while (i < n) {
        size_t j = i;
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &j, &cp)) j = i + 1;
        i = j;
        c++;
    }
    return c;
}

/* Python repr 的常用子集（错误文案里的 `%r` 用）。float 单独处理：w_repr 的
 * `%g` 会把 5.0 打成 "5"，而 Python repr 是 "5.0"。 */
static void repr_value(const jnode *v, buf *o) {
    if (!v) { buf_puts(o, "None"); return; }
    if (bjtype(v) == BJ_FLOAT) {
        double d = jfloat(v);
        long long ll = (long long)d;
        if ((double)ll == d && d > -1e16 && d < 1e16) buf_printf(o, "%lld.0", ll);
        else buf_printf(o, "%g", d);
        return;
    }
    w_repr(v, o);
}

/* server.py::_bb —— 只认真 bool，其余 400。**不用 writeutil 的 wbody_bool**：
 * 那条对 float 用 w_repr 的 `%g`，`"confirmed": 1.0` 会回 "收到 1"，
 * 而 Python 是 "收到 1.0"。这里按 repr_value 的 float 口径重做一遍。 */
static int ref_bool(SOCKET s, const jnode *b, const char *key, int dflt, int *out) {
    const jnode *v = jget(b, key);
    if (!v) { *out = dflt; return 1; }
    if (jis_bool(v)) { *out = jbool(v); return 1; }
    buf m;
    buf_init(&m);
    buf_puts(&m, key);
    buf_puts(&m, " 必须是 true 或 false,收到 ");
    repr_value(v, &m);
    send_400(s, buf_cstr(&m));
    buf_free(&m);
    return 0;
}

/* ---------------------------------------------------------------- git 封装 */

/* Python `_out(args, check=False)`：返回 stdout（universal newlines，不 strip）。 */
static char *run_out(const char *const args[], int *rc_out) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);
    if (rc_out) *rc_out = rc;
    char *so = py_text(o.p ? o.p : "", o.n);
    buf_free(&o);
    buf_free(&e);
    return so;
}

/* Python `_ok(args)`：返回 rc，*out 为 (stdout+stderr).strip()。 */
static int run_ok(const char *const args[], char **out) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);
    char *so = py_text(o.p ? o.p : "", o.n);
    char *se = py_text(e.p ? e.p : "", e.n);
    buf cat;
    buf_init(&cat);
    buf_puts(&cat, so);
    buf_puts(&cat, se);
    py_strip(buf_cstr(&cat));
    *out = xstrdup(buf_cstr(&cat));
    buf_free(&cat);
    buf_free(&o);
    buf_free(&e);
    free(so);
    free(se);
    return rc;
}

/* `rev-parse --verify --quiet <spec>` 的 stdout.strip()，失败 NULL。 */
static char *rev_parse(const char *spec) {
    const char *args[] = { "rev-parse", "--verify", "--quiet", spec, NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    if (rc != 0) { free(o); return NULL; }
    py_strip(o);
    if (!*o) { free(o); return NULL; }
    return o;
}

/* `refs/heads/<name>` 的 sha；不存在 NULL。 */
static char *branch_sha(const char *name) {
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, "refs/heads/");
    buf_puts(&spec, name);
    char *r = rev_parse(buf_cstr(&spec));
    buf_free(&spec);
    return r;
}

static int branch_exists(const char *name) {
    char *s = branch_sha(name);
    free(s);
    return s != NULL;
}

static int has_commits(void) {
    char *s = rev_parse("HEAD");
    free(s);
    return s != NULL;
}

static char *head_sha(void) {
    const char *args[] = { "rev-parse", "HEAD", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    py_strip(o);
    if (rc != 0 || !*o) { free(o); return NULL; }
    return o;
}

/* 当前分支名；detached HEAD 时 NULL。 */
static char *head_branch(void) {
    const char *args[] = { "symbolic-ref", "--quiet", "--short", "HEAD", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    py_strip(o);
    if (!*o) { free(o); return NULL; }
    return o;
}

static int is_current_branch(const char *name) {
    char *cur = head_branch();
    if (cur) {
        int same = !strcmp(cur, name);
        free(cur);
        return same;
    }
    if (!has_commits()) return 0;
    char *bs = branch_sha(name);
    char *hs = head_sha();
    int same = bs && hs && !strcmp(bs, hs);
    free(bs);
    free(hs);
    return same;
}

static int name_cmp(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

/* list_branches：名字列表（**已排序**，与 Python 的 sorted 一致）。
 * Python 有一条读 refs 文件的快路径，但结论与 for-each-ref 相同，这里只跑后者。 */
static char **list_branches(size_t *np) {
    const char *args[] = { "for-each-ref", "--format=%(refname:short)",
                           "refs/heads", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    char **v = NULL;
    size_t n = 0;
    size_t pos = 0, len = strlen(o);
    while (pos <= len) {
        size_t end = pos;
        while (end < len && o[end] != '\n') end++;
        if (end > pos) {
            v = xrealloc(v, (n + 1) * sizeof(*v));
            v[n++] = xstrndup(o + pos, end - pos);
        }
        if (end >= len) break;
        pos = end + 1;
    }
    free(o);
    if (n > 1) qsort(v, n, sizeof(*v), name_cmp);
    *np = n;
    return v;
}

static void free_strv(char **v, size_t n) {
    for (size_t i = 0; i < n; i++) free(v[i]);
    free(v);
}

/* default_branch：远端 HEAD -> main -> master -> None。 */
static char *default_branch(void) {
    const char *args[] = { "for-each-ref", "--format=%(symref)",
                           "refs/remotes/*/HEAD", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    char *found = NULL;
    size_t pos = 0, len = strlen(o);
    while (pos <= len && !found) {
        size_t end = pos;
        while (end < len && o[end] != '\n') end++;
        char *line = xstrndup(o + pos, end - pos);
        py_strip(line);
        if (!strncmp(line, "refs/remotes/", 13)) {
            char *slash = strrchr(line, '/');
            if (slash && slash[1]) found = xstrdup(slash + 1);
        }
        free(line);
        if (end >= len) break;
        pos = end + 1;
    }
    free(o);
    if (found) return found;

    size_t nb = 0;
    char **bs = list_branches(&nb);
    for (int pass = 0; pass < 2 && !found; pass++) {
        const char *cand = pass == 0 ? "main" : "master";
        for (size_t i = 0; i < nb; i++)
            if (!strcmp(bs[i], cand)) { found = xstrdup(cand); break; }
    }
    free_strv(bs, nb);
    return found;
}

/* subject_of(rev) 的 stdout.strip()。 */
static char *subject_of(const char *rev) {
    const char *args[] = { "show", "-s", "--format=%s", rev, NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    py_strip(o);
    return o;
}

/* parents_of(rev)：`show -s --format=%P` 按空白切。 */
static char **parents_of(const char *rev, size_t *np) {
    const char *args[] = { "show", "-s", "--format=%P", rev, NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    char **v = NULL;
    size_t n = 0, i = 0, len = strlen(o);
    while (i < len) {
        while (i < len && py_space((unsigned char)o[i])) i++;
        size_t a = i;
        while (i < len && !py_space((unsigned char)o[i])) i++;
        if (i > a) {
            v = xrealloc(v, (n + 1) * sizeof(*v));
            v[n++] = xstrndup(o + a, i - a);
        }
    }
    free(o);
    *np = n;
    return v;
}

typedef struct {
    char *sha;
    char *author;
    char *date;
    char *subject;
} urow;

/* `_unmerged_commits(name)`：target..name 的计数 + 前 20 条。 */
static long unmerged_commits(const char *name, urow **rows_out, size_t *nrow_out) {
    char *target = NULL;
    char *dflt = default_branch();
    if (dflt && branch_exists(dflt)) target = dflt;
    else { free(dflt); target = xstrdup("HEAD"); }

    buf range;
    buf_init(&range);
    buf_puts(&range, target);
    buf_puts(&range, "..");
    buf_puts(&range, name);

    const char *cargs[] = { "rev-list", "--count", buf_cstr(&range), NULL };
    int rc = 0;
    char *cnt = run_out(cargs, &rc);
    py_strip(cnt);
    long nr = strtol(cnt, NULL, 10);
    free(cnt);

    buf fmt;
    buf_init(&fmt);
    buf_puts(&fmt, "--pretty=format:%H\x1f%an\x1f%ad\x1f%s");
    const char *larg[] = { "log", "-20", "--date=short", buf_cstr(&fmt),
                           buf_cstr(&range), NULL };
    char *o = run_out(larg, &rc);

    urow *rows = NULL;
    size_t n = 0, pos = 0, len = strlen(o);
    while (pos <= len) {
        size_t end = pos;
        while (end < len && o[end] != '\n') end++;
        if (end > pos) {
            char *line = xstrndup(o + pos, end - pos);
            /* line.split("\x1f")，必须正好 4 段 */
            char *parts[4];
            int np = 0;
            char *p = line;
            while (np < 4) {
                char *s = strchr(p, '\x1f');
                parts[np++] = p;
                if (!s) break;
                *s = 0;
                p = s + 1;
            }
            if (np == 4 && !strchr(parts[3], '\x1f')) {
                rows = xrealloc(rows, (n + 1) * sizeof(*rows));
                rows[n].sha = xstrdup(parts[0]);
                rows[n].author = xstrdup(parts[1]);
                rows[n].date = xstrdup(parts[2]);
                rows[n].subject = xstrdup(parts[3]);
                n++;
            }
            free(line);
        }
        if (end >= len) break;
        pos = end + 1;
    }
    free(o);
    buf_free(&fmt);
    buf_free(&range);
    free(target);

    *rows_out = rows;
    *nrow_out = n;
    return nr;
}

static void free_urows(urow *rows, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(rows[i].sha);
        free(rows[i].author);
        free(rows[i].date);
        free(rows[i].subject);
    }
    free(rows);
}

/* `_conflicted_paths()`：`diff --name-only --diff-filter=U` 的非空行。 */
static char **conflicted_paths(size_t *np) {
    const char *args[] = { "diff", "--name-only", "--diff-filter=U", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    char **v = NULL;
    size_t n = 0, pos = 0, len = strlen(o);
    while (pos <= len) {
        size_t end = pos;
        while (end < len && o[end] != '\n') end++;
        size_t a = pos, b = end;
        while (a < b && py_space((unsigned char)o[a])) a++;
        while (b > a && py_space((unsigned char)o[b - 1])) b--;
        if (b > a) {
            v = xrealloc(v, (n + 1) * sizeof(*v));
            v[n++] = xstrndup(o + pos, end - pos);   /* Python 保留原始行 */
        }
        if (end >= len) break;
        pos = end + 1;
    }
    free(o);
    *np = n;
    return v;
}

/* dirty()["conflicted"]：porcelain=v1 -z 里的 U/AA/DD。 */
static int has_conflicts(void) {
    const char *args[] = { "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    int found = 0;
    size_t pos = 0, len = strlen(o);
    while (pos < len && !found) {
        size_t flen = 0;
        while (pos + flen < len && o[pos + flen]) flen++;
        if (flen >= 3) {
            char x = o[pos], y = o[pos + 1];
            if (x == 'U' || y == 'U' || (x == 'A' && y == 'A') ||
                (x == 'D' && y == 'D')) found = 1;
        }
        pos += flen + 1;
    }
    free(o);
    return found;
}

/* ---------------------------------------------------------------- 路径小工具 */

static wchar_t *wjoin(const wchar_t *a, const wchar_t *b) {
    size_t n = wcslen(a) + wcslen(b) + 2;
    wchar_t *o = xmalloc(n * sizeof(wchar_t));
    _snwprintf(o, n, L"%ls\\%ls", a, b);
    return o;
}

static int is_dir_w(const wchar_t *path) {
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

/* os.path.isdir(os.path.join(root, rel))，rel 用 '/' 分隔。 */
static int is_dir_rel(const char *rel) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t *relw = u82w(rel);
    if (!relw) return 0;
    for (wchar_t *p = relw; *p; p++) if (*p == L'/') *p = L'\\';
    wchar_t *full = wjoin(root, relw);
    int r = is_dir_w(full);
    free(full);
    free(relw);
    return r;
}

/* `s.replace(needle, "", 1)` */
static char *replace_first(const char *s, const char *needle) {
    const char *p = strstr(s, needle);
    if (!p) return xstrdup(s);
    buf b;
    buf_init(&b);
    buf_append(&b, s, (size_t)(p - s));
    buf_puts(&b, p + strlen(needle));
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

/* ---------------------------------------------------------------- 校验 */

/* `_require_repo()`。成功 1；失败已回 400。 */
static int require_repo(SOCKET s) {
    const wchar_t *root = git_root();
    if (root) {
        wchar_t *gd = wjoin(root, L".git");
        DWORD a = GetFileAttributesW(gd);
        free(gd);
        if (a != INVALID_FILE_ATTRIBUTES) return 1;
    }
    char *r = root ? w2u8(root) : NULL;
    buf m;
    buf_init(&m);
    buf_puts(&m, MSG_NOT_REPO);
    buf_puts(&m, r ? r : "");
    send_hist(s, buf_cstr(&m), "not_a_repo", NULL);
    buf_free(&m);
    free(r);
    return 0;
}

/* `_require_commits(what)`。 */
static int require_commits(SOCKET s, const char *what) {
    if (has_commits()) return 1;
    buf m;
    buf_init(&m);
    buf_puts(&m, "仓库还没有任何提交,");
    buf_puts(&m, what);
    buf_puts(&m, "无从谈起");
    send_hist(s, buf_cstr(&m), "no_commits", NULL);
    buf_free(&m);
    return 0;
}

/* `validate_ref_name(name, what)`。成功返回 malloc 的**strip 后**名字。 */
static char *validate_ref_name(SOCKET s, const jnode *v, const char *what) {
    if (!v || !jis_str(v)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, what);
        buf_puts(&m, "必须是字符串");
        send_hist(s, buf_cstr(&m), "bad_name", NULL);
        buf_free(&m);
        return NULL;
    }
    size_t n = 0;
    char *name = strip_utf8(jstr(v), jstrlen(v), &n);
    buf m;
    buf_init(&m);

    if (n == 0) {
        buf_puts(&m, what);
        buf_puts(&m, "不能为空");
        goto bad;
    }
    if (cp_count(name, n) > 100) {
        buf_puts(&m, what);
        buf_puts(&m, "超过 100 字符");
        goto bad;
    }
    /* `[\w./-]{1,100}` 的 fullmatch */
    {
        size_t i = 0, count = 0;
        while (i < n) {
            unsigned cp = 0;
            size_t j = i;
            if (!next_cp((const unsigned char *)name, n, &j, &cp)) goto regex_bad;
            if (!(word_cp(cp) || cp == '.' || cp == '/' || cp == '-'))
                goto regex_bad;
            if (++count > 100) goto regex_bad;
            i = j;
        }
        goto regex_ok;
    regex_bad:
        buf_puts(&m, what);
        buf_puts(&m, "只允许字母数字和 _ . / -(正则 [\\w./-]{1,100})");
        goto bad;
    }
regex_ok:
    if (name[0] == '-') {
        buf_puts(&m, what);
        buf_puts(&m, "不能以 - 开头:git 会把它当成选项读");
        goto bad;
    }
    if (name[0] == '/' || name[0] == '.') {
        buf_puts(&m, what);
        buf_puts(&m, "不能以 / 或 . 开头");
        goto bad;
    }
    if (name[n - 1] == '/' || name[n - 1] == '.') {
        buf_puts(&m, what);
        buf_puts(&m, "不能以 / 或 . 结尾");
        goto bad;
    }
    for (size_t i = 0; i + 1 < n; i++) {
        if (name[i] == '.' && name[i + 1] == '.') {
            buf_puts(&m, what);
            buf_puts(&m, "不能包含 ..");
            goto bad;
        }
    }
    for (size_t i = 0; i + 1 < n; i++) {
        if (name[i] == '/' && name[i + 1] == '/') {
            buf_puts(&m, what);
            buf_puts(&m, "不能包含 //");
            goto bad;
        }
    }
    for (size_t i = 0; i + 1 < n; i++) {
        if (name[i] == '@' && name[i + 1] == '{') {
            buf_puts(&m, what);
            buf_puts(&m, "不能包含 @{:这是 reflog 语法");
            goto bad;
        }
    }
    if (n == 1 && name[0] == '@') {
        buf_puts(&m, what);
        buf_puts(&m, "不能是 @");
        goto bad;
    }
    if (n >= 5 && !memcmp(name + n - 5, ".lock", 5)) {
        buf_puts(&m, what);
        buf_puts(&m, "不能以 .lock 结尾");
        goto bad;
    }
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)name[i];
        if (c < 32 || c == 127) {
            buf_puts(&m, what);
            buf_puts(&m, "不能包含控制字符");
            goto bad;
        }
    }
    buf_free(&m);
    return name;

bad:
    send_hist(s, buf_cstr(&m), "bad_name", NULL);
    buf_free(&m);
    free(name);
    return NULL;
}

/* `_check_sha(rev, what)`。成功返回 malloc 的规范化 sha。 */
static char *check_sha_str(SOCKET s, const char *src, size_t srclen,
                           const char *what) {
    size_t n = 0;
    char *rev = strip_utf8(src, srclen, &n);
    buf m;
    buf_init(&m);

    if (n == 0) {
        buf_puts(&m, what);
        buf_puts(&m, "不能为空");
        goto bad;
    }
    if (rev[0] == '-') {
        buf_puts(&m, what);
        buf_puts(&m, "不能以 - 开头");
        goto bad;
    }
    /* `[A-Za-z0-9_./^~@{}\-]{1,64}` */
    {
        int ok = (n >= 1 && n <= 64);
        for (size_t i = 0; ok && i < n; i++) {
            unsigned char c = (unsigned char)rev[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') ||
                  (c >= 'A' && c <= 'Z') || c == '_' || c == '.' || c == '/' ||
                  c == '^' || c == '~' || c == '@' || c == '{' || c == '}' ||
                  c == '-'))
                ok = 0;
        }
        if (!ok) {
            buf pre;
            buf_init(&pre);
            buf_puts(&pre, what);
            buf_puts(&pre, "含有非法字符:");
            send_hist3(s, buf_cstr(&pre), rev, n, "", "bad_rev");
            buf_free(&pre);
            buf_free(&m);
            free(rev);
            return NULL;
        }
    }
    {
        buf spec;
        buf_init(&spec);
        buf_append(&spec, rev, n);
        buf_puts(&spec, "^{commit}");
        char *sha = rev_parse(buf_cstr(&spec));
        buf_free(&spec);
        if (!sha) {
            buf pre;
            buf_init(&pre);
            buf_puts(&pre, "解析不了这个");
            buf_puts(&pre, what);
            buf_puts(&pre, ":");
            send_hist3(s, buf_cstr(&pre), rev, n, "", "bad_rev");
            buf_free(&pre);
            buf_free(&m);
            free(rev);
            return NULL;
        }
        buf_free(&m);
        free(rev);
        return sha;
    }

bad:
    send_hist(s, buf_cstr(&m), "bad_rev", NULL);
    buf_free(&m);
    free(rev);
    return NULL;
}

/* 请求体里的 rev（`_check_sha(b.get(..))` 那一档）。 */
static char *check_sha(SOCKET s, const jnode *v, const char *what) {
    if (!v || !jis_str(v)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, what);
        buf_puts(&m, "不能为空");
        send_hist(s, buf_cstr(&m), "bad_rev", NULL);
        buf_free(&m);
        return NULL;
    }
    return check_sha_str(s, jstr(v), jstrlen(v), what);
}

/* `_need_msg(value, what)`：只收 str / None，不许 NUL。成功 1（*out 可能为 NULL）。 */
static int need_msg(SOCKET s, const jnode *v, const char *what, char **out) {
    *out = NULL;
    if (!v || bjtype(v) == BJ_NULL) return 1;
    if (!jis_str(v)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, what);
        buf_puts(&m, "必须是字符串,收到 ");
        buf_puts(&m, jtype_name(v));
        send_hist(s, buf_cstr(&m), "bad_message", NULL);
        buf_free(&m);
        return 0;
    }
    if (memchr(jstr(v), 0, jstrlen(v))) {
        buf m;
        buf_init(&m);
        buf_puts(&m, what);
        buf_puts(&m, "里不能包含 NUL 字符");
        send_hist(s, buf_cstr(&m), "bad_message", NULL);
        buf_free(&m);
        return 0;
    }
    *out = xstrdup(jstr(v));
    return 1;
}

/* 把 jnode 当 Python 的 truthiness（`b.get("ref") or "HEAD"` 那一档）。 */
static int jtruthy(const jnode *v) {
    if (!v) return 0;
    switch (bjtype(v)) {
    case BJ_NULL:  return 0;
    case BJ_BOOL:  return jbool(v);
    case BJ_INT:   return jint(v) != 0;
    case BJ_FLOAT: return jfloat(v) != 0.0;
    case BJ_STR:   return jstrlen(v) != 0;
    case BJ_ARR:
    case BJ_OBJ:   return jlen(v) != 0;
    default:       return 0;
    }
}

/* ---------------------------------------------------------------- 确认闸 */

/* `_require_confirm(op, confirmed)` 的失败响应。 */
static void send_needs_confirm(SOCKET s, const char *op) {
    buf m;
    buf_init(&m);
    buf_puts(&m, "「");
    buf_puts(&m, op);
    buf_puts(&m, "」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 "
                 "confirmed=True。这里没有一步到位的入口。");
    buf x;
    buf_init(&x);
    buf_puts(&x, "\"op\":");
    json_str(&x, op);
    send_hist(s, buf_cstr(&m), "needs_confirmation", buf_cstr(&x));
    buf_free(&x);
    buf_free(&m);
}

/* ---------------------------------------------------------------- revert */

static int revert_in_progress(void) {
    char *h = rev_parse("REVERT_HEAD");
    if (h) { free(h); return 1; }
    const char *args[] = { "rev-parse", "--git-path", "sequencer", NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    if (rc != 0) { free(o); return 0; }
    py_strip(o);
    int r = 0;
    if (*o) {
        if (is_dir_rel(o)) r = 1;
        else {
            char *rel = replace_first(o, ".git/");
            if (is_dir_rel(rel)) r = 1;
            free(rel);
        }
    }
    free(o);
    return r;
}

static int do_revert_abort(SOCKET s) {
    if (!revert_in_progress()) {
        send_hist(s, MSG_REVERT_IDLE, "no_revert_in_progress", NULL);
        return 1;
    }
    const char *args[] = { "revert", "--abort", NULL };
    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git revert --abort 失败:");
        buf_puts(&m, out);
        send_hist(s, buf_cstr(&m), "revert_abort_failed", NULL);
        buf_free(&m);
        free(out);
        return 1;
    }
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"aborted\":true,\"output\":");
    json_str(&o, out);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free(out);
    return 1;
}

static int do_revert(SOCKET s, const jnode *b) {
    int no_edit = 1;
    /* 参数求值顺序：`_bb(b, "no_edit", True)` 在进 do_revert 之前就求值了。 */
    if (!ref_bool(s, b, "no_edit", 1, &no_edit)) return 1;
    if (!require_repo(s)) return 1;

    const jnode *sv = jget(b, "sha");
    const jnode *mv = jget(b, "mainline");
    char *real = check_sha(s, sv, "提交");
    if (!real) return 1;

    size_t npar = 0;
    char **pars = parents_of(real, &npar);

    int have_ml = !(!mv || bjtype(mv) == BJ_NULL);
    buf blocker;
    buf_init(&blocker);
    int blocked = 0;

    if (npar > 1) {
        if (!have_ml) {
            buf cand;
            buf_init(&cand);
            for (size_t i = 0; i < npar; i++) {
                char *subj = subject_of(pars[i]);
                char *cut = cut_cp(subj, strlen(subj), 40);
                if (i) buf_puts(&cand, ", ");
                buf_printf(&cand, "%d = %s", (int)(i + 1), cut);
                free(cut);
                free(subj);
            }
            char *s8 = xstrndup(real, strlen(real) < 8 ? strlen(real) : 8);
            buf_printf(&blocker,
                       "%s 是一个 merge 提交,有 %d 个父提交。revert 一个 merge 必须"
                       "用 -m 指定保留哪一侧(mainline),否则结果不确定:不同的 "
                       "mainline 会得到完全相反的逆向改动。候选:%s",
                       s8, (int)npar, buf_cstr(&cand));
            free(s8);
            buf_free(&cand);
            blocked = 1;
        } else if (!jis_int(mv)) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "mainline 必须是整数(1..%d)", (int)npar);
            send_hist(s, buf_cstr(&m), "bad_mainline", NULL);
            buf_free(&m);
            free_strv(pars, npar);
            buf_free(&blocker);
            free(real);
            return 1;
        } else {
            long long ml = jint(mv);
            if (ml < 1 || ml > (long long)npar) {
                buf m;
                buf_init(&m);
                buf_printf(&m, "mainline 必须是 1..%d,收到 %lld", (int)npar, ml);
                send_hist(s, buf_cstr(&m), "bad_mainline", NULL);
                buf_free(&m);
                free_strv(pars, npar);
                buf_free(&blocker);
                free(real);
                return 1;
            }
        }
    } else if (have_ml) {
        char *s8 = xstrndup(real, strlen(real) < 8 ? strlen(real) : 8);
        buf_printf(&blocker,
                   "%s 只有一个父提交,不是 merge 提交,不能指定 mainline。", s8);
        free(s8);
        blocked = 1;
    }

    /* 冲突未解决优先级最高，覆盖前面的 blocker。 */
    if (has_conflicts()) {
        buf_reset(&blocker);
        buf_puts(&blocker, MSG_REVERT_DIRTY);
        blocked = 1;
    }

    if (blocked) {
        send_hist(s, buf_cstr(&blocker), "revert_refused", NULL);
        buf_free(&blocker);
        free_strv(pars, npar);
        free(real);
        return 1;
    }
    buf_free(&blocker);

    /* 真执行：git revert [--no-edit] [-m N] <sha> */
    const char *args[8];
    int ai = 0;
    args[ai++] = "revert";
    if (no_edit) args[ai++] = "--no-edit";
    char mlnum[32];
    if (have_ml) {
        snprintf(mlnum, sizeof(mlnum), "%lld", jint(mv));
        args[ai++] = "-m";
        args[ai++] = mlnum;
    }
    args[ai++] = real;
    args[ai] = NULL;

    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        size_t nc = 0;
        char **cp = conflicted_paths(&nc);
        buf m;
        buf_init(&m);
        buf_puts(&m, "revert 失败:");
        buf_puts(&m, out);
        if (nc) {
            buf_puts(&m, "\n冲突路径:");
            for (size_t i = 0; i < nc; i++) {
                if (i) buf_puts(&m, ", ");
                buf_puts(&m, cp[i]);
            }
        }
        buf_puts(&m, "\n仓库可能停在 revert 中途,用 do_revert_abort() 收场,"
                     "或者手工解冲突后 git revert --continue。");

        buf x;
        buf_init(&x);
        buf_puts(&x, "\"conflicted\":[");
        for (size_t i = 0; i < nc; i++) {
            if (i) buf_putc(&x, ',');
            json_str(&x, cp[i]);
        }
        buf_puts(&x, "],\"output\":");
        json_str(&x, out);
        send_hist(s, buf_cstr(&m), nc ? "revert_conflict" : "revert_failed",
                  buf_cstr(&x));
        buf_free(&x);
        buf_free(&m);
        free_strv(cp, nc);
        free(out);
        free_strv(pars, npar);
        free(real);
        return 1;
    }

    char *new_head = head_sha();
    char *s8 = xstrndup(real, strlen(real) < 8 ? strlen(real) : 8);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"reverted\":");
    json_str(&o, s8);
    buf_puts(&o, ",\"mainline\":");
    if (have_ml) buf_printf(&o, "%lld", jint(mv));
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"new_head\":");
    if (new_head) json_str(&o, new_head);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"output\":");
    json_str(&o, out);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free(new_head);
    free(s8);
    free(out);
    free_strv(pars, npar);
    free(real);
    return 1;
}

/* ---------------------------------------------------------------- 分支改名 */

static int do_branch_rename(SOCKET s, const jnode *b) {
    int confirmed = 0;
    if (!ref_bool(s, b, "confirmed", 0, &confirmed)) return 1;
    if (!confirmed) { send_needs_confirm(s, "rename-branch"); return 1; }

    if (!require_repo(s)) return 1;
    char *old = validate_ref_name(s, jget(b, "old"), "旧分支名");
    if (!old) return 1;
    char *new = validate_ref_name(s, jget(b, "new"), "新分支名");
    if (!new) { free(old); return 1; }

    if (!branch_exists(old)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "没有这个分支:");
        buf_puts(&m, old);
        send_hist(s, buf_cstr(&m), "no_such_branch", NULL);
        buf_free(&m);
        free(old);
        free(new);
        return 1;
    }

    buf blocker;
    buf_init(&blocker);
    int blocked = 0;
    if (!strcmp(old, new)) {
        buf_puts(&blocker, MSG_RENAME_SAME);
        blocked = 1;
    } else if (branch_exists(new)) {
        buf_puts(&blocker, "已经有同名分支 ");
        buf_puts(&blocker, new);
        buf_puts(&blocker, "。改名不会覆盖它 —— 那是另一个人的工作。");
        blocked = 1;
    }
    if (blocked) {
        send_hist(s, buf_cstr(&blocker), "rename_refused", NULL);
        buf_free(&blocker);
        free(old);
        free(new);
        return 1;
    }
    buf_free(&blocker);

    const char *args[] = { "branch", "-m", old, new, NULL };
    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git branch -m 失败:");
        buf_puts(&m, out);
        send_hist(s, buf_cstr(&m), "rename_failed", NULL);
        buf_free(&m);
        free(out);
        free(old);
        free(new);
        return 1;
    }

    char *sha = branch_sha(new);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"old\":");
    json_str(&o, old);
    buf_puts(&o, ",\"new\":");
    json_str(&o, new);
    buf_puts(&o, ",\"sha\":");
    if (sha) json_str(&o, sha);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"output\":");
    json_str(&o, out);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free(sha);
    free(out);
    free(old);
    free(new);
    return 1;
}

/* ---------------------------------------------------------------- 分支删除 */

static int do_branch_delete(SOCKET s, const jnode *b) {
    int force = 0, confirmed = 0;
    /* 参数求值顺序：force 先，confirmed 后。 */
    if (!ref_bool(s, b, "force", 0, &force)) return 1;
    if (!ref_bool(s, b, "confirmed", 0, &confirmed)) return 1;
    if (!confirmed) { send_needs_confirm(s, "delete-branch"); return 1; }

    if (!require_repo(s)) return 1;
    const jnode *nv = jget(b, "name");
    char *name = validate_ref_name(s, nv, "分支名");
    if (!name) return 1;
    /* plan 用 strip 后的名字；真正执行/响应里的 `deleted` 用**原始**名字。 */
    const char *raw = jstr(nv);

    if (!branch_exists(name)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "没有这个分支:");
        buf_puts(&m, name);
        send_hist(s, buf_cstr(&m), "no_such_branch", NULL);
        buf_free(&m);
        free(name);
        return 1;
    }

    size_t nb = 0;
    char **branches = list_branches(&nb);

    buf blocked;
    buf_init(&blocked);
    int is_blocked = 0;
    if (nb <= 1) {
        buf_puts(&blocked, MSG_ONLY_BRANCH);
        is_blocked = 1;
    } else if (is_current_branch(name)) {
        const char *elsewhere = NULL;
        for (size_t i = 0; i < nb; i++)
            if (strcmp(branches[i], name)) { elsewhere = branches[i]; break; }
        if (elsewhere)
            buf_printf(&blocked,
                       "不能删除当前所在的分支 %s。先切到别的分支(git checkout %s),"
                       "再删它。", name, elsewhere);
        else
            buf_printf(&blocked,
                       "不能删除当前所在的分支 %s。先切到别的分支"
                       "(git checkout <其他分支>),再删它。", name);
        is_blocked = 1;
    } else {
        char *dflt = default_branch();
        if (dflt && !strcmp(dflt, name)) {
            buf_printf(&blocked,
                       "不能删除 default 分支 %s。它是别人 clone 之后的落脚点,"
                       "也是判别「这个提交算不算已合并」的基准。想删它,先改"
                       "名字(plan_branch_rename)再删。", name);
            is_blocked = 1;
        }
        free(dflt);
    }

    urow *rows = NULL;
    size_t nrow = 0;
    long nr = unmerged_commits(name, &rows, &nrow);

    /* plan 的 warnings（只有走到 unmerged_branch 的 400 时才进响应） */
    buf warn;
    buf_init(&warn);
    {
        char *dflt = default_branch();
        const char *base = dflt ? dflt : "HEAD";
        if (nr) {
            buf_printf(&warn, "该分支有 %ld 个未合并提交(相对 %s)", nr, base);
            if (force) buf_puts(&warn, ",force=True 会直接丢掉,不可恢复");
            else buf_puts(&warn, ",不加 force 会被拒绝");
            if ((long)nrow < nr)
                buf_printf(&warn, " (只列出前 %zu 条)", nrow);
        } else {
            buf_printf(&warn, "该分支的提交都已并入 %s,删除是安全的。", base);
        }
        free(dflt);
    }
    if (is_blocked) {
        buf_puts(&warn, " 不过先说结论:这一步现在**不能做** —— ");
        buf_puts(&warn, buf_cstr(&blocked));
    }

    if (is_blocked) {
        send_hist(s, buf_cstr(&blocked), "delete_refused", NULL);
        buf_free(&blocked);
        buf_free(&warn);
        free_urows(rows, nrow);
        free_strv(branches, nb);
        free(name);
        return 1;
    }
    buf_free(&blocked);

    if (nr && !force) {
        buf m;
        buf_init(&m);
        buf_printf(&m,
                   "「%s」有 %ld 个未合并提交,不加 force 拒绝删除。\n%s\n"
                   "确认要丢就再调一次,带 force=True(和 confirmed=True)。",
                   raw, nr, buf_cstr(&warn));
        buf x;
        buf_init(&x);
        buf_printf(&x, "\"unmerged_count\":%ld,\"commits\":[", nr);
        for (size_t i = 0; i < nrow; i++) {
            if (i) buf_putc(&x, ',');
            buf_puts(&x, "{\"sha\":");
            json_str(&x, rows[i].sha);
            buf_puts(&x, ",\"short\":");
            {
                size_t sl = strlen(rows[i].sha);
                char *sh = xstrndup(rows[i].sha, sl < 8 ? sl : 8);
                json_str(&x, sh);
                free(sh);
            }
            buf_puts(&x, ",\"author\":");
            json_str(&x, rows[i].author);
            buf_puts(&x, ",\"date\":");
            json_str(&x, rows[i].date);
            buf_puts(&x, ",\"subject\":");
            json_str(&x, rows[i].subject);
            buf_putc(&x, '}');
        }
        buf_puts(&x, "],\"hint\":");
        json_str(&x, "再调一次,带 force=True");
        send_hist(s, buf_cstr(&m), "unmerged_branch", buf_cstr(&x));
        buf_free(&x);
        buf_free(&m);
        buf_free(&warn);
        free_urows(rows, nrow);
        free_strv(branches, nb);
        free(name);
        return 1;
    }
    buf_free(&warn);
    free_urows(rows, nrow);

    const char *flag = force ? "-D" : "-d";
    const char *args[] = { "branch", flag, raw, NULL };
    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "git branch %s 失败:", flag);
        buf_puts(&m, out);
        send_hist(s, buf_cstr(&m), "delete_failed", NULL);
        buf_free(&m);
        free(out);
        free_strv(branches, nb);
        free(name);
        return 1;
    }

    size_t nr2 = 0;
    char **after = list_branches(&nr2);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"deleted\":");
    json_str(&o, raw);
    buf_puts(&o, ",\"forced\":");
    buf_puts(&o, force ? "true" : "false");
    buf_printf(&o, ",\"unmerged_count\":%ld,\"output\":", nr);
    json_str(&o, out);
    buf_puts(&o, ",\"remaining\":[");
    for (size_t i = 0; i < nr2; i++) {
        if (i) buf_putc(&o, ',');
        json_str(&o, after[i]);
    }
    buf_puts(&o, "]}");
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free_strv(after, nr2);
    free(out);
    free_strv(branches, nb);
    free(name);
    return 1;
}

/* ---------------------------------------------------------------- 标签 */

/* `for-each-ref --count=1 refs/tags/<name>` 是否有输出（strip 后非空）。 */
static int tag_exists(const char *name) {
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, "refs/tags/");
    buf_puts(&spec, name);
    const char *args[] = { "for-each-ref", "--count=1", buf_cstr(&spec), NULL };
    int rc = 0;
    char *o = run_out(args, &rc);
    py_strip(o);
    int r = *o != 0;
    free(o);
    buf_free(&spec);
    return r;
}

static int do_tag_create(SOCKET s, const jnode *b) {
    int force = 0;
    /* 参数求值顺序：`_bb(b, "force")` 先于进 do_tag_create。 */
    if (!ref_bool(s, b, "force", 0, &force)) return 1;
    if (!require_repo(s)) return 1;
    if (!require_commits(s, "打标签")) return 1;

    char *name = validate_ref_name(s, jget(b, "name"), "标签名");
    if (!name) return 1;

    const jnode *sv = jget(b, "sha");
    char *target;
    if (jtruthy(sv)) target = check_sha(s, sv, "提交");
    else target = check_sha_str(s, "HEAD", 4, "提交");   /* `sha or "HEAD"` */
    if (!target) { free(name); return 1; }

    char *message = NULL;
    if (!need_msg(s, jget(b, "message"), "标签说明", &message)) {
        free(target);
        free(name);
        return 1;
    }

    if (tag_exists(name) && !force) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "标签 %s 已存在。删了重建,或者传 force=True 覆盖。", name);
        send_hist(s, buf_cstr(&m), "tag_exists", NULL);
        buf_free(&m);
        free(message);
        free(target);
        free(name);
        return 1;
    }

    const char *args[8];
    int ai = 0;
    args[ai++] = "tag";
    if (force) args[ai++] = "-f";
    if (message && *message) {
        args[ai++] = "-a";
        args[ai++] = "-m";
        args[ai++] = message;
    }
    args[ai++] = name;
    args[ai++] = target;
    args[ai] = NULL;

    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git tag 失败:");
        buf_puts(&m, out);
        send_hist(s, buf_cstr(&m), "tag_failed", NULL);
        buf_free(&m);
        free(out);
        free(message);
        free(target);
        free(name);
        return 1;
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"name\":");
    json_str(&o, name);
    buf_puts(&o, ",\"sha\":");
    json_str(&o, target);
    buf_puts(&o, ",\"annotated\":");
    buf_puts(&o, (message && *message) ? "true" : "false");
    buf_puts(&o, ",\"output\":");
    json_str(&o, out);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free(out);
    free(message);
    free(target);
    free(name);
    return 1;
}

static int do_tag_delete(SOCKET s, const jnode *b) {
    int confirmed = 0;
    if (!ref_bool(s, b, "confirmed", 0, &confirmed)) return 1;
    if (!confirmed) { send_needs_confirm(s, "tag-delete"); return 1; }

    if (!require_repo(s)) return 1;
    char *name = validate_ref_name(s, jget(b, "name"), "标签名");
    if (!name) return 1;

    if (!tag_exists(name)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "没有这个标签:");
        buf_puts(&m, name);
        send_hist(s, buf_cstr(&m), "no_such_tag", NULL);
        buf_free(&m);
        free(name);
        return 1;
    }

    const char *rargs[] = { "rev-list", "-1", name, NULL };
    int rc = 0;
    char *sha = run_out(rargs, &rc);
    py_strip(sha);

    const char *args[] = { "tag", "-d", name, NULL };
    char *out = NULL;
    rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git tag -d 失败:");
        buf_puts(&m, out);
        send_hist(s, buf_cstr(&m), "tag_delete_failed", NULL);
        buf_free(&m);
        free(out);
        free(sha);
        free(name);
        return 1;
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"deleted\":");
    json_str(&o, name);
    buf_puts(&o, ",\"sha\":");
    json_str(&o, sha);
    buf_puts(&o, ",\"output\":");
    json_str(&o, out);
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    free(out);
    free(sha);
    free(name);
    return 1;
}

/* ---------------------------------------------------------------- 分派 */

int write_refs_post(const char *path, const char *query, const char *body,
                    size_t body_len, SOCKET s) {
    (void)query;
    int which = 0;
    if (!strcmp(path, "/api/history/revert")) which = 1;
    else if (!strcmp(path, "/api/history/revert/abort")) which = 2;
    else if (!strcmp(path, "/api/history/branch/rename")) which = 3;
    else if (!strcmp(path, "/api/history/branch/delete")) which = 4;
    else if (!strcmp(path, "/api/history/tag/create")) which = 5;
    else if (!strcmp(path, "/api/history/tag/delete")) which = 6;
    if (!which) return 0;

    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    switch (which) {
    case 1: do_revert(s, b); break;
    case 2: do_revert_abort(s); break;
    case 3: do_branch_rename(s, b); break;
    case 4: do_branch_delete(s, b); break;
    case 5: do_tag_create(s, b); break;
    default: do_tag_delete(s, b); break;
    }
    jfree(b);
    return 1;
}
