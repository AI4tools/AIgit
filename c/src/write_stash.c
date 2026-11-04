/* write_stash.c —— M3 写面第三组：history/stash + stash apply/pop/drop。
 *
 * 对位 Python（server.py::do_POST 的 4 条路由 -> history_ops.py）：
 *   POST /api/history/stash       -> do_stash(message, include_untracked, keep_index)
 *   POST /api/history/stash/apply -> do_stash_apply(index)
 *   POST /api/history/stash/pop   -> 接线层 confirmed 闸 + do_stash_pop(index)
 *   POST /api/history/stash/drop  -> do_stash_drop(index, confirmed)
 *
 * 逐项对齐的要点（都是读 Python 源码确认过的）：
 *   * 参数求值顺序 = Python 的实参顺序。`do_stash(b.get("message"),
 *     _bb(b,"include_untracked"), _bb(b,"keep_index"))` 先算两个 _bb，再进函数
 *     体做 `_need_msg`。所以「include_untracked 类型错」优先于「message 类型错」。
 *     drop 是 `do_stash_drop(b.get("index",0), _bb(b,"confirmed"))`：confirmed 的
 *     **类型**错先于 index 的任何校验；confirmed 为 false 的 needs_confirmation
 *     又先于 index 校验。
 *   * `index` 不走 `_bi`：`b.get("index", 0)` 原样交给 `_stash_ref`，那里
 *     `isinstance(index, int) and not isinstance(index, bool)` —— JSON 的
 *     `1.0` / `"1"` / `true` / `null` 全是 400 bad_index，缺省才是 0。
 *   * `dirty()` 用 `status --porcelain=v1 -z --untracked-files=all`，解析口径
 *     逐行照抄（包括 rename 的第二段被当成独立记录这个 Python 行为）。
 *   * `list_stashes` 的行格式就是 `stash@{n}: <reflog msg>`；index 是位置下标，
 *     ref 用行里解析出来的 n 拼。`stash_count` 用默认 limit=50，`_stash_ref`
 *     用 limit=200。
 *   * apply 的失败判据是 `rc != 0 or conflicted`，错误里同时带 conflicted 与
 *     output；pop = apply 成功后再 drop，apply 的 dict 整个嵌进 popped 响应。
 *   * 所有成功响应都是模块自己的 dict（**不走** _ok_with_status，不带 status 字段）。
 */
#include "routes.h"
#include "params.h"

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

#define MSG_NOTHING_TO_STASH \
    "工作区是干净的,没有东西可以暂存。git stash 在这种情况下会直接" \
    "报错(而不是塞一个空条目进 stash 列表),所以这里提前拦下。"

#define MSG_DROP_NEEDS_CONFIRM \
    "「stash-drop」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 " \
    "confirmed=True。这里没有一步到位的入口。"

#define MSG_POP_NEEDS_CONFIRM_A \
    "stash pop 会把条目删掉(不可撤销),需要 confirmed=true;先 GET " \
    "/api/history/plan/stash-drop?index="

#define MSG_POP_NEEDS_CONFIRM_B " 看清要丢什么。"

#define MSG_APPLY_FAILED \
    "stash 贴回失败(仓库已保留原样,这个 stash 没被删):\n"

#define MSG_POP_DROP_FAILED \
    "stash 已贴回但删除失败(内容已在工作区,stash 条目还在):"

/* ---------------------------------------------------------------- 文本口径 */

/* Python `str.strip()` 的空白集（ASCII 那一半）。 */
static int py_space(unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

/* `text=True` 的 universal newlines：\r\n 与孤立 \r 都折成 \n。
 * *outn 回传长度（内容里可能有 NUL，不能靠 strlen）。 */
static char *py_text_n(const char *p, size_t n, size_t *outn) {
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
    *outn = k;
    return o;
}

/* 就地 strip（前后都去），返回同一个指针。 */
/* malloc 一份 [s,s+n) 并 strip。 */
/* Python repr 的常用子集（错误文案里的 %r 用）。float 单独处理：
 * w_repr 的 `%g` 会把 5.0 打成 "5"，而 Python repr 是 "5.0"。 */
static void py_float(buf *o, double d) {
    long long ll = (long long)d;
    if ((double)ll == d && d > -1e16 && d < 1e16) buf_printf(o, "%lld.0", ll);
    else buf_printf(o, "%g", d);
}

/* Python `str(v)`（pop 的 needs_confirmation 文案里 `%s` 用）：字符串不加引号，
 * 其余与 repr 一致。 */
static void py_str_value(const jnode *v, buf *o) {
    if (!v) { buf_puts(o, "None"); return; }
    switch (bjtype(v)) {
    case BJ_NULL:  buf_puts(o, "None"); break;
    case BJ_BOOL:  buf_puts(o, jbool(v) ? "True" : "False"); break;
    case BJ_INT: {
        /* Python 的 int 无上限：溢出时用 JSON 里保留的字面量。 */
        const char *lit = jint_lit(v);
        char *c = lit ? py_int_canon(lit) : NULL;
        if (c) buf_puts(o, c);
        else buf_printf(o, "%lld", jint(v));
        free(c);
        break;
    }
    case BJ_FLOAT: py_float(o, jfloat(v)); break;
    case BJ_STR:   buf_append(o, jstr(v), jstrlen(v)); break;
    default:       w_repr(v, o); break;
    }
}

/* `_bb` 的本地副本：语义与 writeutil.c 的 wbody_bool 相同，只把错误文案里的
 * `%r` 换成 float 感知的 repr_value（wbody_bool 用 w_repr，1.0 会打成 "1"）。 */
/* ---------------------------------------------------------------- 错误响应 */

/* {"ok":false,"error":...,"code":...,<extra>}（HistoryError.to_dict 的形状）。 */
static void send_err_extra(SOCKET s, const char *msg, const char *code,
                           const char *extra_json) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg ? msg : "");
    buf_puts(&o, ",\"code\":");
    json_str(&o, code);
    if (extra_json && *extra_json) {
        buf_putc(&o, ',');
        buf_puts(&o, extra_json);
    }
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
}

/* 跑一条只关心成败的 git，返回 (rc, (stdout+stderr).strip())。 */
static int run_ok(const char *const args[], char **out) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);

    size_t on = 0, en = 0;
    char *so = py_text_n(o.p ? o.p : "", o.n, &on);
    char *se = py_text_n(e.p ? e.p : "", e.n, &en);
    buf cat;
    buf_init(&cat);
    buf_append(&cat, so, on);
    buf_append(&cat, se, en);
    (void)buf_cstr(&cat);
    py_strip(buf_cstr(&cat));
    *out = xstrdup(buf_cstr(&cat));

    buf_free(&cat);
    free(so);
    free(se);
    buf_free(&o);
    buf_free(&e);
    return rc;
}

/* ---------------------------------------------------------------- dirty */

typedef struct {
    char  **staged;    size_t ns;
    char  **unstaged;  size_t nu;
    char  **untracked; size_t nunt;
    char  **conf;      size_t nc;
} dirty_t;

static void strv_push(char ***arr, size_t *n, const char *s, size_t len) {
    *arr = xrealloc(*arr, (*n + 1) * sizeof(char *));
    (*arr)[(*n)++] = xstrndup(s, len);
}

static void strv_free(char **v, size_t n) {
    for (size_t i = 0; i < n; i++) free(v[i]);
    free(v);
}

static void dirty_free(dirty_t *d) {
    strv_free(d->staged, d->ns);
    strv_free(d->unstaged, d->nu);
    strv_free(d->untracked, d->nunt);
    strv_free(d->conf, d->nc);
    memset(d, 0, sizeof(*d));
}

/* 与 history_ops.py::dirty 逐行对齐（`--porcelain=v1 -z -uall` + split("\0")）。 */
static void dirty_compute(dirty_t *d) {
    memset(d, 0, sizeof(*d));
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", NULL };
    (void)git_run(args, NULL, 0, &o, &e);           /* check=False */
    size_t n = 0;
    char *t = py_text_n(o.p ? o.p : "", o.n, &n);

    size_t i = 0;
    while (i <= n) {
        size_t j = i;
        while (j < n && t[j]) j++;
        size_t len = j - i;
        const char *rec = t + i;
        if (len >= 3) {
            char x = rec[0], y = rec[1];
            const char *path = rec + 3;
            size_t plen = len - 3;

            /* Python: if " -> " in path: path = path.split(" -> ")[-1]
             * （-z 下不会出现，照抄以防万一）。 */
            size_t cut = 0;
            int found = 0;
            for (size_t k = 0; k + 4 <= plen; k++) {
                if (!memcmp(path + k, " -> ", 4)) { cut = k; found = 1; }
            }
            if (found) { path += cut + 4; plen -= cut + 4; }

            if (x == '?' && y == '?') {
                strv_push(&d->untracked, &d->nunt, path, plen);
            } else {
                if (x == 'U' || y == 'U' || (x == 'A' && y == 'A') ||
                    (x == 'D' && y == 'D'))
                    strv_push(&d->conf, &d->nc, path, plen);
                if (x != ' ' && x != '?')
                    strv_push(&d->staged, &d->ns, path, plen);
                if (y != ' ' && y != '?')
                    strv_push(&d->unstaged, &d->nu, path, plen);
            }
        }
        i = j + 1;
    }

    free(t);
    buf_free(&o);
    buf_free(&e);
}

static void json_strv(buf *o, char **v, size_t n) {
    buf_putc(o, '[');
    for (size_t i = 0; i < n; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, v[i]);
    }
    buf_putc(o, ']');
}

/* `{"staged":[...],"unstaged":[...],"untracked":[...],"conflicted":[...]}` */
static void dirty_json(buf *o, const dirty_t *d) {
    buf_puts(o, "\"staged\":");
    json_strv(o, d->staged, d->ns);
    buf_puts(o, ",\"unstaged\":");
    json_strv(o, d->unstaged, d->nu);
    buf_puts(o, ",\"untracked\":");
    json_strv(o, d->untracked, d->nunt);
    buf_puts(o, ",\"conflicted\":");
    json_strv(o, d->conf, d->nc);
}

static int dirty_clean(const dirty_t *d) {
    return d->ns == 0 && d->nu == 0 && d->nunt == 0 && d->nc == 0;
}

/* ---------------------------------------------------------------- stash list */

typedef struct {
    long long index;
    char *ref;
    char *name;
    char *subject;
    char *raw;
} srow;

typedef struct {
    srow  *v;
    size_t n;
} slist;

static void slist_free(slist *l) {
    for (size_t i = 0; i < l->n; i++) {
        free(l->v[i].ref);
        free(l->v[i].name);
        free(l->v[i].subject);
        free(l->v[i].raw);
    }
    free(l->v);
    l->v = NULL;
    l->n = 0;
}

/* `re.match(r"stash@\{(\d+)\}:\s*(.*)", line)`。匹配成功时 desc 指向第一个
 * 非空白字符（`\s*` 贪婪），descn 是它到行尾的长度。 */
static int match_stash_line(const char *line, size_t n, long long *idx,
                            const char **desc, size_t *descn) {
    static const char pre[] = "stash@{";
    if (n < sizeof(pre) - 1 || memcmp(line, pre, sizeof(pre) - 1)) return 0;
    size_t i = sizeof(pre) - 1;
    if (i >= n) return 0;
    long long v = 0;
    size_t digits = 0;
    while (i < n && line[i] >= '0' && line[i] <= '9') {
        if (v < 1000000000000000LL) v = v * 10 + (line[i] - '0');
        i++;
        digits++;
    }
    if (!digits) return 0;
    if (i + 1 >= n || line[i] != '}' || line[i + 1] != ':') return 0;
    i += 2;
    while (i < n && py_space((unsigned char)line[i])) i++;
    *idx = v;
    *desc = line + i;
    *descn = n - i;
    return 1;
}

/* `list_stashes(limit)`：跑 `git stash list -<limit>` 再按 Python 的解析器拆行。 */
static void stash_list(slist *l, int limit) {
    l->v = NULL;
    l->n = 0;
    char flag[32];
    _snprintf(flag, sizeof(flag), "-%d", limit);
    const char *args[] = { "stash", "list", flag, NULL };
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);
    buf_free(&e);
    if (rc != 0) { buf_free(&o); return; }

    size_t n = 0;
    char *t = py_text_n(o.p ? o.p : "", o.n, &n);
    size_t i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && t[j] != '\n') j++;
        const char *line = t + i;
        size_t ln = j - i;
        i = j + 1;

        char *blank = xstrndup(line, ln);
        int is_blank = !*py_strip(blank);
        free(blank);
        if (is_blank) continue;

        long long idx = 0;
        const char *desc = NULL;
        size_t descn = 0;
        int matched = match_stash_line(line, ln, &idx, &desc, &descn);
        if (!matched) {
            idx = (long long)l->n;
            desc = line;
            descn = ln;
        }

        /* sha, sep, subject = desc.partition(":") */
        const char *colon = desc ? memchr(desc, ':', descn) : NULL;
        size_t sha_n = colon ? (size_t)(colon - desc) : descn;
        const char *sub = colon ? colon + 1 : desc + descn;
        size_t sub_n = colon ? descn - sha_n - 1 : 0;

        char *name = py_strip_dup_n(desc ? desc : "", sha_n, NULL);
        char *subject = py_strip_dup_n(sub, sub_n, NULL);
        if (!*subject) {
            free(subject);
            subject = py_strip_dup_n(desc ? desc : "", descn, NULL);
        }
        char *raw = py_strip_dup_n(line, ln, NULL);

        buf ref;
        buf_init(&ref);
        buf_printf(&ref, "stash@{%lld}", idx);

        l->v = xrealloc(l->v, (l->n + 1) * sizeof(srow));
        srow *r = &l->v[l->n++];
        r->index = idx;
        r->ref = xstrdup(buf_cstr(&ref));
        r->name = name;
        r->subject = subject;
        r->raw = raw;
        buf_free(&ref);
    }
    free(t);
    buf_free(&o);
}

/* `len(self.list_stashes())` —— 默认 limit=50。 */
static long long stash_count(void) {
    slist l;
    stash_list(&l, 50);
    long long n = (long long)l.n;
    slist_free(&l);
    return n;
}

/* Python 的 int 没有 2^126 上限，错误文案要原样回显；这里优先用 JSON 里
 * 保留下来的十进制字面量，没有溢出时退回内部值。 */
static char *index_echo_of(const jnode *idxv, long long index) {
    const char *lit = idxv ? jint_lit(idxv) : NULL;
    char *c = lit ? py_int_canon(lit) : NULL;
    if (c) return c;
    char t[32];
    snprintf(t, sizeof(t), "%lld", index);
    return xstrdup(t);
}

/* `_stash_ref(index)`：类型/负数/越界三道闸，成功时 *pos 是位置下标。 */
static int stash_lookup(SOCKET s, const jnode *idxv, slist *l, size_t *pos) {
    long long index;
    if (!idxv) {
        index = 0;
    } else if (bjtype(idxv) == BJ_INT) {
        index = jint(idxv);
    } else {
        send_err_extra(s, "stash 索引必须是整数", "bad_index", NULL);
        return 0;
    }
    if (index < 0) {
        send_err_extra(s, "stash 索引不能为负", "bad_index", NULL);
        return 0;
    }
    stash_list(l, 200);
    if (index >= (long long)l->n) {
        buf m;
        buf_init(&m);
        char *echo = index_echo_of(idxv, index);
        buf_printf(&m, "没有第 %s 个 stash(共 %lld 个)", echo, (long long)l->n);
        free(echo);
        send_err_extra(s, buf_cstr(&m), "no_such_stash", NULL);
        buf_free(&m);
        slist_free(l);
        return 0;
    }
    *pos = (size_t)index;
    return 1;
}

/* `_conflicted_paths()`：`git diff --name-only --diff-filter=U` 的非空行。 */
static void conflicted_paths(char ***v, size_t *count) {
    *v = NULL;
    *count = 0;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "diff", "--name-only", "--diff-filter=U", NULL };
    (void)git_run(args, NULL, 0, &o, &e);           /* check=False */
    buf_free(&e);
    size_t n = 0;
    char *t = py_text_n(o.p ? o.p : "", o.n, &n);

    size_t i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && t[j] != '\n') j++;
        char *line = py_strip_dup_n(t + i, j - i, NULL);
        if (*line) strv_push(v, count, line, strlen(line));
        free(line);
        i = j + 1;
    }

    free(t);
    buf_free(&o);
}

/* ---------------------------------------------------------------- 端点 */

/* 成功响应里嵌套用的 apply dict：
 * {"ok":true,"applied":ref,"output":...,"stash_count":N} */
static int apply_core(SOCKET s, const jnode *idxv, buf *dict) {
    slist l;
    size_t pos = 0;
    if (!stash_lookup(s, idxv, &l, &pos)) return 0;
    const srow *row = &l.v[pos];

    const char *args[] = { "stash", "apply", "--index", row->ref, NULL };
    char *out = NULL;
    int rc = run_ok(args, &out);

    char **cf = NULL;
    size_t nconf = 0;
    conflicted_paths(&cf, &nconf);

    if (rc != 0 || nconf) {
        buf m;
        buf_init(&m);
        buf_puts(&m, MSG_APPLY_FAILED);
        buf_puts(&m, out);
        if (nconf) {
            buf_puts(&m, "\n冲突路径:");
            for (size_t i = 0; i < nconf; i++) {
                if (i) buf_puts(&m, ", ");
                buf_puts(&m, cf[i]);
            }
        }
        buf extra;
        buf_init(&extra);
        buf_puts(&extra, "\"conflicted\":");
        json_strv(&extra, cf, nconf);
        buf_puts(&extra, ",\"output\":");
        json_str(&extra, out);
        send_err_extra(s, buf_cstr(&m), "stash_conflict", buf_cstr(&extra));
        buf_free(&extra);
        buf_free(&m);
        strv_free(cf, nconf);
        free(out);
        slist_free(&l);
        return 0;
    }

    long long cnt = stash_count();
    buf_puts(dict, "{\"ok\":true,\"applied\":");
    json_str(dict, row->ref);
    buf_puts(dict, ",\"output\":");
    json_str(dict, out);
    buf_printf(dict, ",\"stash_count\":%lld}", cnt);

    strv_free(cf, nconf);
    free(out);
    slist_free(&l);
    return 1;
}

/* POST /api/history/stash */
static void do_stash(SOCKET s, const jnode *b) {
    int inc = 0, keep = 0;
    if (!wbody_bool(s, b, "include_untracked", 0, &inc)) return;
    if (!wbody_bool(s, b, "keep_index", 0, &keep)) return;

    /* _need_msg(b.get("message"), "stash 说明") */
    const jnode *mv = jget(b, "message");
    const char *msg = NULL;
    size_t msglen = 0;
    if (mv && bjtype(mv) != BJ_NULL) {
        if (!jis_str(mv)) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "stash 说明必须是字符串,收到 ");
            buf_puts(&m, jtype_name(mv));
            http_send_err(s, 400, buf_cstr(&m), "bad_message");
            buf_free(&m);
            return;
        }
        if (memchr(jstr(mv), 0, jstrlen(mv))) {
            http_send_err(s, 400, "stash 说明里不能包含 NUL 字符", "bad_message");
            return;
        }
        msg = jstr(mv);
        msglen = jstrlen(mv);
    }

    /* plan_stash: _require_repo -> dirty -> 有没有东西可暂存 */
    size_t rootlen = wcslen(git_root());
    wchar_t *dotgit = xmalloc((rootlen + 7) * sizeof(wchar_t));
    _snwprintf(dotgit, rootlen + 7, L"%ls\\.git", git_root());
    DWORD dotgit_attr = GetFileAttributesW(dotgit);
    free(dotgit);
    if (dotgit_attr == INVALID_FILE_ATTRIBUTES) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "不是一个 git 仓库(或路径不存在):");
        char *root = w2u8(git_root());
        buf_puts(&m, root ? root : "");
        free(root);
        http_send_err(s, 400, buf_cstr(&m), "not_a_repo");
        buf_free(&m);
        return;
    }

    dirty_t d;
    dirty_compute(&d);
    size_t changes = d.ns + d.nu + d.nc + (inc ? d.nunt : 0);
    if (!changes) {
        buf extra;
        buf_init(&extra);
        buf_puts(&extra, "\"dirty\":{");
        dirty_json(&extra, &d);
        buf_putc(&extra, '}');
        send_err_extra(s, MSG_NOTHING_TO_STASH, "nothing_to_stash",
                       buf_cstr(&extra));
        buf_free(&extra);
        dirty_free(&d);
        return;
    }
    long long will_stash = (long long)changes;
    dirty_free(&d);

    const char *args[8];
    int n = 0;
    args[n++] = "stash";
    args[n++] = "push";
    if (inc) args[n++] = "--include-untracked";
    if (keep) args[n++] = "--keep-index";
    if (msg && msglen) { args[n++] = "-m"; args[n++] = msg; }
    args[n] = NULL;

    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git stash push 失败:");
        buf_puts(&m, out);
        send_err_extra(s, buf_cstr(&m), "stash_failed", NULL);
        buf_free(&m);
        free(out);
        return;
    }

    dirty_t after;
    dirty_compute(&after);
    int clean = dirty_clean(&after);
    dirty_free(&after);
    long long cnt = stash_count();

    buf r;
    buf_init(&r);
    buf_printf(&r, "{\"ok\":true,\"stashed\":%lld,\"now_clean\":%s,\"output\":",
               will_stash, clean ? "true" : "false");
    json_str(&r, out);
    buf_printf(&r, ",\"stash_count\":%lld}", cnt);
    http_send_json(s, 200, buf_cstr(&r), r.n);
    buf_free(&r);
    free(out);
}

/* POST /api/history/stash/apply */
static void do_stash_apply(SOCKET s, const jnode *b) {
    buf dict;
    buf_init(&dict);
    if (apply_core(s, jget(b, "index"), &dict))
        http_send_json(s, 200, buf_cstr(&dict), dict.n);
    buf_free(&dict);
}

/* POST /api/history/stash/pop —— 接线层的 confirmed 闸先于一切。 */
static void do_stash_pop(SOCKET s, const jnode *b) {
    int conf = 0;
    if (!wbody_bool(s, b, "confirmed", 0, &conf)) return;
    if (!conf) {
        buf m;
        buf_init(&m);
        buf_puts(&m, MSG_POP_NEEDS_CONFIRM_A);
        /* Python 是 `b.get("index", 0)`：缺键才退化成 0，显式 null 渲染成 None。 */
        const jnode *iv = jget(b, "index");
        if (iv) py_str_value(iv, &m);
        else buf_putc(&m, '0');
        buf_puts(&m, MSG_POP_NEEDS_CONFIRM_B);
        send_err_extra(s, buf_cstr(&m), "needs_confirmation",
                       "\"op\":\"stash-pop\"");
        buf_free(&m);
        return;
    }

    const jnode *idxv = jget(b, "index");
    slist l;
    size_t pos = 0;
    if (!stash_lookup(s, idxv, &l, &pos)) return;
    char *ref = xstrdup(l.v[pos].ref);
    slist_free(&l);

    buf applied;
    buf_init(&applied);
    if (!apply_core(s, idxv, &applied)) {
        buf_free(&applied);
        free(ref);
        return;
    }

    const char *dargs[] = { "stash", "drop", ref, NULL };
    char *out = NULL;
    int rc = run_ok(dargs, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, MSG_POP_DROP_FAILED);
        buf_puts(&m, out);
        buf extra;
        buf_init(&extra);
        buf_puts(&extra, "\"output\":");
        json_str(&extra, out);
        send_err_extra(s, buf_cstr(&m), "stash_drop_failed", buf_cstr(&extra));
        buf_free(&extra);
        buf_free(&m);
        buf_free(&applied);
        free(out);
        free(ref);
        return;
    }

    long long cnt = stash_count();
    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"ok\":true,\"popped\":");
    json_str(&r, ref);
    buf_puts(&r, ",\"applied\":");
    buf_puts(&r, buf_cstr(&applied));
    buf_printf(&r, ",\"dropped\":true,\"stash_count\":%lld}", cnt);
    http_send_json(s, 200, buf_cstr(&r), r.n);
    buf_free(&r);
    buf_free(&applied);
    free(out);
    free(ref);
}

/* POST /api/history/stash/drop */
static void do_stash_drop(SOCKET s, const jnode *b) {
    int conf = 0;
    if (!wbody_bool(s, b, "confirmed", 0, &conf)) return;
    if (!conf) {
        send_err_extra(s, MSG_DROP_NEEDS_CONFIRM, "needs_confirmation",
                       "\"op\":\"stash-drop\"");
        return;
    }

    const jnode *idxv = jget(b, "index");
    slist l;
    size_t pos = 0;
    if (!stash_lookup(s, idxv, &l, &pos)) return;
    const srow *row = &l.v[pos];

    const char *args[] = { "stash", "drop", row->ref, NULL };
    char *out = NULL;
    int rc = run_ok(args, &out);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git stash drop 失败:");
        buf_puts(&m, out);
        send_err_extra(s, buf_cstr(&m), "stash_drop_failed", NULL);
        buf_free(&m);
        free(out);
        slist_free(&l);
        return;
    }

    long long cnt = stash_count();
    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"ok\":true,\"dropped\":{\"index\":");
    buf_printf(&r, "%lld", row->index);
    buf_puts(&r, ",\"ref\":");
    json_str(&r, row->ref);
    buf_puts(&r, ",\"name\":");
    json_str(&r, row->name);
    buf_puts(&r, ",\"subject\":");
    json_str(&r, row->subject);
    buf_puts(&r, "},\"output\":");
    json_str(&r, out);
    buf_printf(&r, ",\"stash_count\":%lld}", cnt);
    http_send_json(s, 200, buf_cstr(&r), r.n);
    buf_free(&r);
    free(out);
    slist_free(&l);
}

/* ---------------------------------------------------------------- 分派 */

int write_stash_post(const char *path, const char *query, const char *body,
                     size_t body_len, SOCKET s) {
    (void)query;
    int which = 0;
    if (!strcmp(path, "/api/history/stash")) which = 1;
    else if (!strcmp(path, "/api/history/stash/apply")) which = 2;
    else if (!strcmp(path, "/api/history/stash/pop")) which = 3;
    else if (!strcmp(path, "/api/history/stash/drop")) which = 4;
    if (!which) return 0;

    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    switch (which) {
    case 1: do_stash(s, b); break;
    case 2: do_stash_apply(s, b); break;
    case 3: do_stash_pop(s, b); break;
    default: do_stash_drop(s, b); break;
    }
    jfree(b);
    return 1;
}
