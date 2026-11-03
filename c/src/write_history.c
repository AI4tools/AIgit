/* write_history.c —— M3 历史写第一组：amend / undo-last-commit / reflog 恢复。
 *
 * 与 Python 版的对位（server.py 与 history_ops.py 逐行照抄语义）：
 *
 *   POST /api/history/amend             server.py:2421 -> do_amend(message, confirmed)
 *   POST /api/history/undo-last-commit  server.py:2424 -> do_undo_last_commit(drop_changes, confirmed)
 *   POST /api/history/reflog/restore    server.py:2427 -> do_restore_from_reflog(index, mode, confirmed)
 *
 * 三个都是**改写历史**的破坏性操作。`confirmed=true` 的护栏在模块里
 * （`HistoryOps._require_confirm`），接线层只把参数原样递过去 —— 这里既不
 * 额外加闸，也不偷偷减闸。
 *
 * 逐条对齐的要点（都是 Python 那边有明确写法的，别自作聪明）：
 *
 *   * **参数求值顺序**：Python 的 `do_amend(b.get("message"), _bb(b,"confirmed"))`
 *     在**调用前**先算实参，所以「confirmed 不是 bool」的 400 优先于
 *     「needs_confirmation」；undo 是 `_bb(drop_changes)` 先于 `_bb(confirmed)`。
 *     do_* 内部又先 `_require_confirm` 再校验 message / index / mode。
 *     这个顺序在本文件里逐条保留（错了就是另一个 400）。
 *   * **错误形状**：HistoryError -> `{ok:false, error, code, ...extra}`（400，
 *     extra 里 amend 的 `op` 要透出去）；GitError（`_bb` 失败）->
 *     `{ok:false, error}`（400，**没有 code**）。
 *   * **成功响应**：模块自己返回的 dict 直接 `_json(...)` —— 不带 status
 *     字段，也不认 `"status": false` 的 opt-out（那是 `_ok_with_status` 的事）。
 *   * `output` = `_ok()` = `(stdout + stderr).strip()`，两边都先过 universal
 *     newlines（`\r\n` / 单独 `\r` 都变 `\n`）。
 *   * undo 的 `recoverable_via` 必须在 `reset` **之前**算：reset 自己会往
 *     reflog 追加一条，之后序号整体后移，算出来的 HEAD@{i} 就指错了。
 *   * amend **不带** `-a/--all`：只提交 index 里的内容，工作区未暂存的改动
 *     原样留下（响应里的 `unstaged_kept` 就是它们）。
 */
#include "routes.h"

#include <ctype.h>
#include <float.h>
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

#define FS "\x1f"

/* ================================================================ 小工具 */

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

static int blank(const char *s) {
    for (; *s; s++)
        if (!is_ws((unsigned char)*s)) return 0;
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

/* ---------------------------------------------------------------- repr */

/* Python repr(float) 的「最短往返」表示（CPython float_repr_style=short）：
 * 取能还原该 double 的最少有效位数，然后按 decpt 选定点/指数写法，
 * 定点时整数补 ".0"。`%g` 会把 5.0 印成 "5"、1e15 印成 "1e+15"，都对不上。 */
static void py_repr_float(double v, buf *o) {
    if (v != v) { buf_puts(o, "nan"); return; }
    if (v > DBL_MAX) { buf_puts(o, "inf"); return; }
    if (v < -DBL_MAX) { buf_puts(o, "-inf"); return; }

    char tmp[64];
    int prec;
    for (prec = 1; prec <= 17; prec++) {
        snprintf(tmp, sizeof(tmp), "%.*e", prec - 1, v);
        if (strtod(tmp, NULL) == v) break;
    }

    const char *p = tmp;
    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    char digits[40];
    size_t nd = 0;
    while (*p && *p != 'e' && *p != 'E') {
        if (*p >= '0' && *p <= '9') digits[nd++] = *p;
        p++;
    }
    digits[nd] = 0;
    int exp10 = 0;
    if (*p == 'e' || *p == 'E') exp10 = atoi(p + 1);
    while (nd > 1 && digits[nd - 1] == '0') digits[--nd] = 0;

    int decpt = exp10 + 1;      /* 小数点在第 decpt 位之后 */
    if (neg) buf_putc(o, '-');
    if (decpt <= -4 || decpt > 16) {
        /* 指数写法：d[.ddd]e±XX（指数至少两位） */
        buf_putc(o, digits[0]);
        if (nd > 1) {
            buf_putc(o, '.');
            buf_puts(o, digits + 1);
        }
        int e = decpt - 1;
        buf_printf(o, "e%c%02d", e < 0 ? '-' : '+', e < 0 ? -e : e);
    } else if (decpt <= 0) {
        buf_puts(o, "0.");
        for (int i = 0; i < -decpt; i++) buf_putc(o, '0');
        buf_puts(o, digits);
    } else if ((size_t)decpt >= nd) {
        buf_puts(o, digits);
        for (size_t i = nd; i < (size_t)decpt; i++) buf_putc(o, '0');
        buf_puts(o, ".0");
    } else {
        buf_append(o, digits, (size_t)decpt);
        buf_putc(o, '.');
        buf_puts(o, digits + decpt);
    }
}

/* Python repr(value)：标量走 w_repr（共享层），float 换成上面的精确写法。 */
static void repr_val(const jnode *v, buf *o) {
    if (bjtype(v) == BJ_FLOAT) {
        py_repr_float(jfloat(v), o);
        return;
    }
    w_repr(v, o);
}

/* ================================================================ git 调用 */

static int run_out(const char *const args[], buf *out) {
    buf er;
    buf_init(&er);
    int rc = git_run(args, NULL, 0, out, &er);
    buf_free(&er);
    crlf_norm(out);
    return rc;
}

/* `_ok()`：返回 rc，*text 收 (stdout + stderr).strip()。 */
static int run_ok_full(const char *const args[], const char *stdin_data,
                       size_t stdin_len, buf *text) {
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, stdin_data, stdin_len, &out, &er);
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

static int run_ok(const char *const args[], buf *text) {
    return run_ok_full(args, NULL, 0, text);
}

/* 带 stdin 的 `_ok()`（amend 的空消息走 `-F -`）。 */
static int run_ok_stdin(const char *const args[], const char *data, size_t len,
                        buf *text) {
    return run_ok_full(args, data ? data : "", len, text);
}

/* `_out()`：只取 stdout，rc 忽略。 */
static void out_only(const char *const args[], buf *out) {
    (void)run_out(args, out);
}

/* ================================================================ 响应 */

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

/* ================================================================ 仓库状态 */

static int is_repo(void) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t p[4096];
    _snwprintf(p, 4096, L"%ls\\.git", root);
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

static char *short8(const char *sha) {
    size_t n = strlen(sha);
    return xstrndup(sha, n < 8 ? n : 8);
}

/* ================================================================ dirty */

/* 只要 unstaged（amend 响应里的 unstaged_kept）。解析口径与 Python 的
 * `dirty()` 同一套 --porcelain=v1 -z。 */
static void dirty_unstaged(strlist *out) {
    const char *args[] = { "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", NULL };
    buf o;
    buf_init(&o);
    out_only(args, &o);
    crlf_norm(&o);

    char *s = o.p ? o.p : (char *)"";
    size_t n = o.n, i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && s[j]) j++;
        size_t len = j - i;
        if (len >= 3) {
            char x = s[i], y = s[i + 1];
            char *path = xstrndup(s + i + 3, len - 3);
            /* Python: if " -> " in path: path = path.split(" -> ")[-1] */
            char *last = path, *p2 = path;
            while ((p2 = strstr(p2, " -> ")) != NULL) { last = p2 + 4; p2 += 4; }
            if (last != path) memmove(path, last, strlen(last) + 1);
            if (x == '?' && y == '?') {
                free(path);
                i = j + 1;
                continue;
            }
            if (y != ' ' && y != '?') sl_push(out, path);
            free(path);
        }
        i = j + 1;
    }
    buf_free(&o);
}

/* ================================================================ reflog */

typedef struct {
    char *sha;
    char *shortsha;
    char *action;
} rlog_row;

/* list_reflog(limit)：走 git 的 reflog 格式化输出（与 Python 的回退路径同源）。
 * 空仓（rc != 0）返回 NULL 且 *out_n = 0 —— Python 那边也是「失败 -> []」。 */
static rlog_row *list_reflog(long long lim, size_t *out_n) {
    char larg[64];
    snprintf(larg, sizeof(larg), "-%lld", lim);
    const char *args[] = { "reflog", larg,
                           "--pretty=format:%H" FS "%h" FS "%gs", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    buf_free(&er);
    *out_n = 0;
    if (rc != 0) {
        buf_free(&out);
        return NULL;
    }
    crlf_norm(&out);

    size_t cap = 8, n = 0;
    rlog_row *rows = xmalloc(cap * sizeof(rlog_row));
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
            rows = xrealloc(rows, cap * sizeof(rlog_row));
        }
        rows[n].sha = xstrdup(f[0]);
        rows[n].shortsha = xstrdup(f[1]);
        rows[n].action = xstrdup(f[2]);
        n++;
    }
    buf_free(&out);
    *out_n = n;
    return rows;
}

static void reflog_free(rlog_row *r, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(r[i].sha);
        free(r[i].shortsha);
        free(r[i].action);
    }
    free(r);
}

/* _reflog_hint(sha, mode)：在 reflog 里找这条提交，给一句能粘贴的恢复命令。
 * flag 只在四个合法 mode 时用原值，否则退回 --soft（与 Python 同）。 */
static char *reflog_hint(const char *sha, const char *mode) {
    const char *flag = mode;
    if (strcmp(mode, "soft") && strcmp(mode, "mixed") &&
        strcmp(mode, "hard") && strcmp(mode, "merge"))
        flag = "soft";

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
            buf_printf(&r, "git reset --%s HEAD@{%lld}", flag, i);
            res = xstrdup(buf_cstr(&r));
            buf_free(&r);
            break;
        }
        i++;
    }
    buf_free(&out);
    return res ? res : xstrdup("git reflog");
}

/* ================================================================ 参数 */

/* `_bb()`：只认真 bool。缺省用 dflt；其余（含 null / 数字 / 字符串）一律 400。
 * 与 writeutil 的 wbody_bool 同形，只把 float 的 %r 换成 Python 的写法。 */
static int hist_bb(SOCKET s, const jnode *b, const char *key, int dflt, int *out) {
    const jnode *v = jget(b, key);
    if (!v) { *out = dflt; return 1; }
    if (jis_bool(v)) { *out = jbool(v); return 1; }
    buf m;
    buf_init(&m);
    buf_puts(&m, key);
    buf_puts(&m, " 必须是 true 或 false,收到 ");
    repr_val(v, &m);
    send_giterr(s, buf_cstr(&m));
    buf_free(&m);
    return 0;
}

/* `_require_confirm(op, confirmed)`：唯一的两步闸执行点。 */
static void send_needs_confirm(SOCKET s, const char *op) {
    buf m;
    buf_init(&m);
    buf_printf(&m,
               "「%s」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 "
               "confirmed=True。这里没有一步到位的入口。", op);
    buf extra;
    buf_init(&extra);
    buf_puts(&extra, ",\"op\":");
    json_str(&extra, op);
    send_hist(s, buf_cstr(&m), "needs_confirmation", buf_cstr(&extra));
    buf_free(&extra);
    buf_free(&m);
}

/* ================================================================ amend */

static void handle_amend(SOCKET s, const jnode *b) {
    int confirmed = 0;
    /* 实参先算：confirmed 不是 bool 的 400 优先于 needs_confirmation。 */
    if (!hist_bb(s, b, "confirmed", 0, &confirmed)) return;

    const jnode *mv = jget(b, "message");
    const char *msg = NULL;

    /* _require_confirm("amend", confirmed) */
    if (!confirmed) {
        send_needs_confirm(s, "amend");
        return;
    }

    /* _need_msg(message, "提交信息") */
    if (mv && bjtype(mv) != BJ_NULL) {
        if (!jis_str(mv)) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "提交信息必须是字符串,收到 %s", jtype_name(mv));
            send_hist(s, buf_cstr(&m), "bad_message", NULL);
            buf_free(&m);
            return;
        }
        if (memchr(jstr(mv), 0, jstrlen(mv))) {
            send_hist(s, "提交信息里不能包含 NUL 字符", "bad_message", NULL);
            return;
        }
        msg = jstr(mv);
    }

    /* plan_amend()：仓库与提交的前置检查（顺序同 Python）。 */
    if (!require_repo(s)) return;
    if (!require_commits(s, "amend")) return;

    char *old_sha = head_sha();

    /* 空消息要走 `-F -`：Windows 的命令行拼串（git.c 的 cmdline_add）对**空参数**
     * 不写 `""`，`-m ""` 会退化成光秃秃的 `-m`（git 报 "switch `m' requires a
     * value"）。实测 `-m ""` / `-F -`（空 stdin）/ `-m " "` 三条写出的提交 sha
     * 逐字相同，所以这里换成 stdin 把空消息原样送进去，语义不变。 */
    int empty_msg = (msg && msg[0] == 0);

    const char *args[8];
    int ai = 0;
    args[ai++] = "commit";
    args[ai++] = "--amend";
    args[ai++] = "--no-verify";
    args[ai++] = "--allow-empty-message";
    if (empty_msg) {
        args[ai++] = "-F";
        args[ai++] = "-";
    } else if (msg) {
        args[ai++] = "-m";
        args[ai++] = msg;
    }
    args[ai] = NULL;

    buf text;
    buf_init(&text);
    int rc = empty_msg ? run_ok_stdin(args, "", 0, &text) : run_ok(args, &text);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git commit --amend 失败:");
        buf_puts(&m, buf_cstr(&text));
        send_hist(s, buf_cstr(&m), "amend_failed", NULL);
        buf_free(&m);
        buf_free(&text);
        free(old_sha);
        return;
    }

    char *new_sha = head_sha();
    char *subject = subject_of(new_sha);

    strlist kept;
    sl_init(&kept);
    dirty_unstaged(&kept);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"old_sha\":");
    json_str(&o, old_sha);
    buf_puts(&o, ",\"new_sha\":");
    json_str(&o, new_sha);
    buf_printf(&o, ",\"rewritten\":%s", strcmp(old_sha, new_sha) ? "true" : "false");
    buf_puts(&o, ",\"subject\":");
    json_str(&o, subject);
    buf_puts(&o, ",\"unstaged_kept\":");
    json_strlist(&o, &kept);
    buf_puts(&o, ",\"output\":");
    json_str(&o, buf_cstr(&text));
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&text);
    sl_free(&kept);
    free(old_sha);
    free(new_sha);
    free(subject);
}

/* ================================================================ undo-last-commit */

static void handle_undo(SOCKET s, const jnode *b) {
    int drop = 0, confirmed = 0;
    /* 实参顺序：_bb(drop_changes) 先于 _bb(confirmed)。 */
    if (!hist_bb(s, b, "drop_changes", 0, &drop)) return;
    if (!hist_bb(s, b, "confirmed", 0, &confirmed)) return;

    const char *op = drop ? "undo-dropped" : "undo-last-commit";
    if (!confirmed) {
        send_needs_confirm(s, op);
        return;
    }

    if (!require_repo(s)) return;
    if (!require_commits(s, "撤销上一次提交")) return;

    char *sha = head_sha();
    const char *mode = drop ? "hard" : "soft";
    /* 必须在 reset 之前算：reset 会往 reflog 追加一条，序号整体后移。 */
    char *hint = reflog_hint(sha, mode);

    const char *args[] = { "reset", drop ? "--hard" : "--soft", "HEAD~1", NULL };
    buf text;
    buf_init(&text);
    int rc = run_ok(args, &text);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "git reset 失败:");
        buf_puts(&m, buf_cstr(&text));
        send_hist(s, buf_cstr(&m), "reset_failed", NULL);
        buf_free(&m);
        buf_free(&text);
        free(sha);
        free(hint);
        return;
    }

    char *head_now = head_sha();
    char *sh8 = short8(sha);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"undone\":");
    json_str(&o, sha);
    buf_puts(&o, ",\"undone_short\":");
    json_str(&o, sh8);
    buf_puts(&o, ",\"mode\":");
    json_str(&o, mode);
    buf_puts(&o, ",\"head_now\":");
    json_str(&o, head_now);
    buf_puts(&o, ",\"recoverable_via\":");
    json_str(&o, hint);
    buf_puts(&o, ",\"output\":");
    json_str(&o, buf_cstr(&text));
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&text);
    free(sha);
    free(hint);
    free(head_now);
    free(sh8);
}

/* ================================================================ reflog restore */

static int mode_ok(const jnode *v) {
    if (!jis_str(v)) return 0;
    size_t n = jstrlen(v);
    const char *s = jstr(v);
    static const char *MODES[] = { "soft", "mixed", "hard", "merge" };
    for (size_t i = 0; i < 4; i++) {
        if (n == strlen(MODES[i]) && !memcmp(s, MODES[i], n)) return 1;
    }
    return 0;
}

static void handle_restore(SOCKET s, const jnode *b) {
    int confirmed = 0;
    if (!hist_bb(s, b, "confirmed", 0, &confirmed)) return;
    if (!confirmed) {
        send_needs_confirm(s, "restore-from-reflog");
        return;
    }

    if (!require_repo(s)) return;

    /* plan_restore_from_reflog：reflog 空 -> 先拒（在 index 类型校验之前）。 */
    size_t nrow = 0;
    rlog_row *rows = list_reflog(500, &nrow);
    if (nrow == 0) {
        send_hist(s,
                  "reflog 是空的(仓库还没有提交,或 reflog 被 gc 清掉),"
                  "没有可恢复的历史。", "empty_reflog", NULL);
        reflog_free(rows, nrow);
        return;
    }

    /* index：b.get("index", 0)，**原样**收（不是 _bi），类型在模块里校验。 */
    const jnode *iv = jget(b, "index");
    long long index = 0;
    if (!iv) {
        index = 0;
    } else if (jis_int(iv)) {
        index = jint(iv);
    } else {
        buf m;
        buf_init(&m);
        buf_puts(&m, "reflog 索引必须是整数,收到 ");
        repr_val(iv, &m);
        send_hist(s, buf_cstr(&m), "bad_index", NULL);
        buf_free(&m);
        reflog_free(rows, nrow);
        return;
    }

    if (index < 0 || index >= (long long)nrow) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "reflog 里没有第 %lld 条(共 %zu 条)", index, nrow);
        send_hist(s, buf_cstr(&m), "bad_index", NULL);
        buf_free(&m);
        reflog_free(rows, nrow);
        return;
    }

    /* mode：b.get("mode", "soft")，原样收；不在四个值里就是 bad_mode。 */
    const jnode *mv = jget(b, "mode");
    const char *mode = "soft";
    if (mv) {
        if (!mode_ok(mv)) {
            send_hist(s, "mode 只能是 soft / mixed / hard / merge", "bad_mode", NULL);
            reflog_free(rows, nrow);
            return;
        }
        mode = jstr(mv);
    }

    char *sha = xstrdup(rows[index].sha);
    char *sh = xstrdup(rows[index].shortsha);
    char *act = xstrdup(rows[index].action);
    reflog_free(rows, nrow);

    buf flag;
    buf_init(&flag);
    buf_puts(&flag, "--");
    buf_puts(&flag, mode);

    const char *args[] = { "reset", buf_cstr(&flag), sha, NULL };
    buf text;
    buf_init(&text);
    int rc = run_ok(args, &text);
    if (rc != 0) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "git reset --%s 失败:%s", mode, buf_cstr(&text));
        send_hist(s, buf_cstr(&m), "reset_failed", NULL);
        buf_free(&m);
        buf_free(&text);
        buf_free(&flag);
        free(sha);
        free(sh);
        free(act);
        return;
    }

    char *head_now = head_sha();

    buf sel;
    buf_init(&sel);
    buf_printf(&sel, "HEAD@{%lld}", index);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"restored\":{\"index\":");
    buf_printf(&o, "%lld", index);
    buf_puts(&o, ",\"sha\":");
    json_str(&o, sha);
    buf_puts(&o, ",\"short\":");
    json_str(&o, sh);
    buf_puts(&o, ",\"selector\":");
    json_str(&o, buf_cstr(&sel));
    buf_puts(&o, ",\"action\":");
    json_str(&o, act);
    buf_puts(&o, "},\"mode\":");
    json_str(&o, mode);
    buf_puts(&o, ",\"head_now\":");
    json_str(&o, head_now);
    buf_puts(&o, ",\"output\":");
    json_str(&o, buf_cstr(&text));
    buf_putc(&o, '}');
    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    buf_free(&sel);
    buf_free(&text);
    buf_free(&flag);
    free(sha);
    free(sh);
    free(act);
    free(head_now);
}

/* ================================================================ 分派 */

int write_history_post(const char *path, const char *query, const char *body,
                       size_t body_len, SOCKET s) {
    (void)query;
    if (!path) return 0;

    int which;
    if (!strcmp(path, "/api/history/amend")) which = 0;
    else if (!strcmp(path, "/api/history/undo-last-commit")) which = 1;
    else if (!strcmp(path, "/api/history/reflog/restore")) which = 2;
    else return 0;

    /* Python 的 do_POST 先 `b = self._body()` 再分派：坏 body 一律 400。 */
    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;

    if (which == 0) handle_amend(s, b);
    else if (which == 1) handle_undo(s, b);
    else handle_restore(s, b);

    jfree(b);
    return 1;
}
