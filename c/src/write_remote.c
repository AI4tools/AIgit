/* write_remote.c —— 远程写端点：fetch / pull / push（M2 最后一块）。
 *
 * 对位 Python：
 *   POST /api/remote/fetch -> gitops.py::GitOps.do_fetch(remote, branch, refspec)
 *   POST /api/remote/pull  -> GitOps.do_pull(remote, branch, confirmed)
 *   POST /api/remote/push  -> GitOps.do_push(remote, branch, force, delete, confirmed)
 *
 * 这三个端点**不走** `_ok_with_status()`：server.py 里它们是 `return self._json(...)`，
 * 提前返回，所以响应里没有 branch/staged/... 那一串刷新后的 status。形状是：
 *   fetch/pull: {"ok":true,"op":...,"remote":...,"output":...,"head":...}
 *   push      : {"ok":true,"op":"push","remote":...,"branch":...,"forced":...,
 *                "deleted":...,"output":...}
 *
 * 逐项对齐的要点（都读 gitops.py 原文确认过）：
 *   * `NetGate` 默认 **off**，读不懂也按 off（fail closed）；优先级
 *     显式参数 > AIGIT_NET > off。server.py 的 remote_ops() **不传闸**，
 *     所以唯一来源就是环境变量。
 *   * 闸的判定点在 `_run` 里（fork 之前），但 do_push / do_pull 在 `_run`
 *     之前**自己又调了一次** `gate.require(WRITE, op=...)`。顺序差别导致：
 *       - fetch：先记一次 COUNT_HOOK 再撞闸 -> net_disabled 时 calls=1；
 *       - push/pull：显式 require 先撞闸 -> net_disabled 时 calls=0。
 *     C 版按这个差别补 `git_count_note`，让 X-aigit-git-calls 也一致。
 *   * 校验顺序（**不是**「先校验再干活」的统一顺序，每个方法各写各的）：
 *       do_fetch: _require_repo -> _check_remote_name -> _check_ref(refspec)
 *       do_pull : _check_remote_name -> gate(WRITE,pull) -> _check_bool -> _require_repo
 *                 -> _check_ref(branch)
 *       do_push : _check_remote_name -> (force/delete isinstance) -> gate(WRITE,push)
 *                 -> _check_bool -> _require_repo -> _check_ref(branch or HEAD)
 *     所以「非仓 + 闸关」时 fetch 报 not_a_repo，push/pull 报 net_disabled。
 *   * `do_fetch` 的 **branch 不校验**（只有 refspec 过 _check_ref）——照抄；
 *     但 refspec 非 None 时即使空串也要校验（空串 -> bad_ref）。
 *   * `confirmed` 只认字面 true（`_bb` 在 server 层已经挡掉非 bool）。
 *   * `output = (stdout + stderr).strip()`；失败文案是
 *     `(stderr or stdout).strip()`（**没有** "git 失败" 兜底，可能为空串）。
 *     Python 用 text=True，universal newlines 把 \r\n / \r 折成 \n，这里同折。
 *   * `_head_branch()` 是 `rev-parse --abbrev-ref HEAD` 的 stdout.strip() or None，
 *     **不看退出码**：未出生 HEAD 时该命令 rc=128 但 stdout 是 "HEAD"，
 *     所以 head 字段是 "HEAD" 而不是 null（照抄这个怪癖）。
 *   * 失败码：fetch_failed / push_failed / pull_failed（带 remote；push 还带 branch），
 *     全部 400；这里不产生 500。
 */
#include "routes.h"
#include "params.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "bodyjson.h"
#include "git.h"
#include "http.h"
#include "json.h"
#include "util.h"
#include "writeutil.h"

/* ================================================================ 文本口径 */

/* Python `str.strip()`（ASCII 空白口径；git 输出与参数都在这个范围）。 */
static char *wr_strip_dup(const char *s) {
    return py_strip_dup(s);
}

/* `text=True` 的 universal newlines：\r\n 与孤立 \r 都折成 \n。 */
static char *wr_text(const char *p, size_t n) {
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

/* Python repr(str) 的近似（错误文案里的 %r），支持内嵌 NUL。 */
static char *wr_py_repr_n(const char *s, size_t n) {
    return py_repr_n(s, n);
}

/* 请求体里某个值的 `%r`：字符串走 repr(str)，其它类型走 w_repr（同 Python repr）。 */
static void wr_repr_node(const jnode *v, buf *o) {
    if (jis_str(v)) {
        char *r = wr_py_repr_n(jstr(v), jstrlen(v));
        buf_puts(o, r);
        free(r);
    } else {
        w_repr(v, o);
    }
}

/* Python 真值：None / False / 0 / 0.0 / "" / [] / {} 为假。 */
static int wr_truthy(const jnode *v) {
    if (!v) return 0;
    switch (bjtype(v)) {
    case BJ_NULL: return 0;
    case BJ_BOOL: return jbool(v);
    case BJ_INT: return jint(v) != 0;
    case BJ_FLOAT: return jfloat(v) != 0.0;
    case BJ_STR: return jstrlen(v) != 0;
    case BJ_ARR: return jlen(v) != 0;
    case BJ_OBJ: return jlen(v) != 0;
    default: return 0;
    }
}

/* Python `str(value)`（branch 在 fetch 里**不过校验**，直接拼进 argv）。 */
static char *wr_pystr(const jnode *v) {
    if (jis_str(v)) return xstrndup(jstr(v), jstrlen(v));
    buf b;
    buf_init(&b);
    w_repr(v, &b);
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

/* ================================================================ 错误响应 */

static void wr_send_json400(SOCKET s, buf *o) {
    http_send_json(s, 400, buf_cstr(o), o->n);
}

/* NetError.to_dict() 的 {"ok","error","code"} 三键形（bad_remote / bad_ref /
 * not_a_repo 用）。 */
static void wr_send_err3(SOCKET s, const char *msg, const char *code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg ? msg : "");
    buf_puts(&o, ",\"code\":");
    json_str(&o, code);
    buf_putc(&o, '}');
    wr_send_json400(s, &o);
    buf_free(&o);
}

/* 失败文案：`(stderr or stdout).strip()`（先 universal newlines，再 strip）。 */
static char *wr_git_msg(buf *out, buf *err) {
    const char *src = "";
    size_t n = 0;
    if (err && err->n) { src = buf_cstr(err); n = strlen(src); }
    else if (out && out->n) { src = buf_cstr(out); n = strlen(src); }
    char *t = wr_text(src, n);
    char *r = wr_strip_dup(t);
    free(t);
    return r;
}

/* 成功 output：`(stdout + stderr).strip()`。 */
static char *wr_git_output(buf *out, buf *err) {
    buf c;
    buf_init(&c);
    if (out && out->n) buf_append(&c, out->p, out->n);
    if (err && err->n) buf_append(&c, err->p, err->n);
    char *t = wr_text(c.p ? c.p : "", c.n);
    char *r = wr_strip_dup(t);
    free(t);
    buf_free(&c);
    return r;
}

/* fetch_failed / pull_failed / push_failed（push 多一个 branch）。 */
static void wr_send_failed(SOCKET s, buf *out, buf *err, const char *code,
                           const char *remote, const char *branch) {
    char *msg = wr_git_msg(out, err);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":");
    json_str(&o, code);
    buf_puts(&o, ",\"remote\":");
    json_str(&o, remote);
    if (branch) {
        buf_puts(&o, ",\"branch\":");
        json_str(&o, branch);
    }
    buf_putc(&o, '}');
    wr_send_json400(s, &o);
    buf_free(&o);
    free(msg);
}

/* ================================================================ 能力闸 */

typedef struct {
    const char *level;      /* "off" / "read" / "write" */
    const char *source;     /* "default" / "env:AIGIT_NET" */
} wr_gate;

static int wr_ascii_lower_eq(const char *a, const char *b) {
    while (*a && *b) {
        int ca = tolower((unsigned char)*a), cb = tolower((unsigned char)*b);
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

/* NetGate.__init__：显式参数 > AIGIT_NET > off；读不懂按 off（fail closed）。 */
static void wr_gate_resolve(wr_gate *g) {
    g->level = "off";
    g->source = "default";
    const char *raw = getenv("AIGIT_NET");
    if (!raw) return;
    g->source = "env:AIGIT_NET";
    char *t = wr_strip_dup(raw);
    const char *lv = NULL;
    if (!*t || !strcmp(t, "0") || wr_ascii_lower_eq(t, "off") ||
        wr_ascii_lower_eq(t, "none") || wr_ascii_lower_eq(t, "no") ||
        wr_ascii_lower_eq(t, "false")) lv = "off";
    else if (!strcmp(t, "1") || wr_ascii_lower_eq(t, "read") ||
             wr_ascii_lower_eq(t, "on") || wr_ascii_lower_eq(t, "yes") ||
             wr_ascii_lower_eq(t, "true") || wr_ascii_lower_eq(t, "ro")) lv = "read";
    else if (!strcmp(t, "2") || wr_ascii_lower_eq(t, "write") ||
             wr_ascii_lower_eq(t, "rw") || wr_ascii_lower_eq(t, "push")) lv = "write";
    if (!lv) lv = "off";            /* 拼错 = off，且不报错（与 Python 一致） */
    g->level = lv;
    free(t);
}

static int wr_gate_allows(const wr_gate *g, const char *need) {
    static const char *LV[] = { "off", "read", "write" };
    int cur = 0, req = 0;
    for (int i = 0; i < 3; i++) {
        if (!strcmp(g->level, LV[i])) cur = i;
        if (!strcmp(need, LV[i])) req = i;
    }
    return cur >= req;
}

/* NetGate.require(need, op)：400 + code=net_disabled + required/current/op。 */
static void wr_send_net_disabled(SOCKET s, const wr_gate *g, const char *need,
                                 const char *op) {
    buf m;
    buf_init(&m);
    buf_printf(&m,
               "远程能力「%s」当前是 **%s**,拒绝执行(%s)。"
               "aigit 默认不联网;要开就显式设 AIGIT_NET=%s(本进程默认 %s)。",
               need, g->level, op, need, g->source);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, buf_cstr(&m));
    buf_puts(&o, ",\"code\":\"net_disabled\",\"required\":");
    json_str(&o, need);
    buf_puts(&o, ",\"current\":");
    json_str(&o, g->level);
    buf_puts(&o, ",\"op\":");
    json_str(&o, op);
    buf_putc(&o, '}');
    wr_send_json400(s, &o);
    buf_free(&o);
    buf_free(&m);
}

/* gitops._check_bool：只认字面 True。 */
static void wr_send_needs_confirm(SOCKET s, const char *op) {
    buf m;
    buf_init(&m);
    buf_printf(&m,
               "「%s」会写到仓库之外,不可撤销。必须先调 plan_push 看清将推送哪些提交,"
               "再显式传 confirmed=True。这里没有一步到位的入口。", op);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, buf_cstr(&m));
    buf_puts(&o, ",\"code\":\"needs_confirmation\",\"op\":");
    json_str(&o, op);
    buf_putc(&o, '}');
    wr_send_json400(s, &o);
    buf_free(&o);
    buf_free(&m);
}

/* ================================================================ 参数校验 */

/* gitops._REMOTE_NAME_RE：[A-Za-z0-9][A-Za-z0-9._-]{0,99} */
static int wr_remote_ok(const char *s, size_t n) {
    if (n < 1 || n > 100) return 0;
    if (!isalnum((unsigned char)s[0])) return 0;
    for (size_t i = 1; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (!isalnum(c) && c != '.' && c != '_' && c != '-') return 0;
    }
    return 1;
}

/* `q.get("remote","origin")` + `_check_remote_name`。成功返回 1 并给出 malloc 串。 */
static int wr_get_remote(SOCKET s, const jnode *b, char **out) {
    *out = NULL;
    const jnode *v = jget(b, "remote");
    if (!v) { *out = xstrdup("origin"); return 1; }
    if (jis_str(v) && wr_remote_ok(jstr(v), jstrlen(v))) {
        *out = xstrndup(jstr(v), jstrlen(v));
        return 1;
    }
    buf m;
    buf_init(&m);
    buf_puts(&m, "远程名不合法:");
    wr_repr_node(v, &m);
    buf_puts(&m, "(只允许字母数字与 . _ -, 字母数字开头, 最长 100)");
    wr_send_err3(s, buf_cstr(&m), "bad_remote");
    buf_free(&m);
    return 0;
}

/* Python str.isspace() 的码点集（用于 ref 校验的「空白或控制字符」那一档）。 */
static int wr_cp_isspace(unsigned cp) {
    return cp == 0x09 || cp == 0x0a || cp == 0x0b || cp == 0x0c || cp == 0x0d ||
           cp == 0x1c || cp == 0x1d || cp == 0x1e || cp == 0x1f || cp == 0x20 ||
           cp == 0x85 || cp == 0xa0 || cp == 0x1680 ||
           (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 ||
           cp == 0x202f || cp == 0x205f || cp == 0x3000;
}

/* 解一个 UTF-8 码点；非法序列返回 0 并把 i 前进 1（Python 侧是 U+FFFD）。 */
static int wr_next_cp(const unsigned char *s, size_t n, size_t *i, unsigned *cp) {
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

/* _REF_RE 的正文：首字符字母数字，其余 [A-Za-z0-9._/-]，长 1..200。 */
static int wr_ref_body_ok(const char *s, size_t n) {
    if (n < 1 || n > 200) return 0;
    if (!isalnum((unsigned char)s[0])) return 0;
    for (size_t i = 1; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (!isalnum(c) && c != '.' && c != '_' && c != '/' && c != '-') return 0;
    }
    return 1;
}

/* gitops._check_ref，检查顺序与文案逐条对齐（含 isinstance 那一档）。 */
static int wr_check_ref_str(SOCKET s, const char *ref, size_t n, const char *what,
                            char **out) {
    *out = NULL;
    buf m;
    buf_init(&m);
    if (n && ref[0] == '-') {
        char *rp = wr_py_repr_n(ref, n);
        buf_printf(&m, "%s 不能以 - 开头(git 会把它当选项读):%s", what, rp);
        free(rp);
    } else if (memchr(ref, ':', n)) {
        char *rp = wr_py_repr_n(ref, n);
        buf_printf(&m,
                   "%s 不能含 ':'(那是 refspec 的分隔符, 会让调用方越权指定 ref):%s",
                   what, rp);
        free(rp);
    } else {
        int bad = 0;
        size_t i = 0;
        while (i < n) {
            size_t j = i;
            unsigned cp = 0;
            int okcp = wr_next_cp((const unsigned char *)ref, n, &j, &cp);
            if ((okcp && cp < 0x20) || (okcp && wr_cp_isspace(cp))) { bad = 1; break; }
            if (!okcp && (unsigned char)ref[i] < 0x20) { bad = 1; break; }
            i = j > i ? j : i + 1;
        }
        if (bad) {
            char *rp = wr_py_repr_n(ref, n);
            buf_printf(&m, "%s 不能含空白或控制字符:%s", what, rp);
            free(rp);
        } else {
            int ok = 0;
            if (n >= 11 && !strncmp(ref, "refs/heads/", 11))
                ok = wr_ref_body_ok(ref + 11, n - 11);
            if (!ok && n >= 10 && !strncmp(ref, "refs/tags/", 10))
                ok = wr_ref_body_ok(ref + 10, n - 10);
            if (!ok) ok = wr_ref_body_ok(ref, n);
            if (ok) {
                *out = xstrndup(ref, n);
                buf_free(&m);
                return 1;
            }
            char *rp = wr_py_repr_n(ref, n);
            buf_printf(&m, "%s 不合法:%s", what, rp);
            free(rp);
        }
    }
    wr_send_err3(s, buf_cstr(&m), "bad_ref");
    buf_free(&m);
    return 0;
}

/* 请求体里的 ref/branch 值：先 isinstance 再 _check_ref。 */
static int wr_check_ref_node(SOCKET s, const jnode *v, const char *what, char **out) {
    *out = NULL;
    if (!jis_str(v)) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "%s 必须是字符串,收到 %s", what, jtype_name(v));
        wr_send_err3(s, buf_cstr(&m), "bad_ref");
        buf_free(&m);
        return 0;
    }
    return wr_check_ref_str(s, jstr(v), jstrlen(v), what, out);
}

/* ================================================================ 仓库前置 */

/* GitOps._require_repo：root/.git 必须存在（.git 是文件也算，链接工作区）。 */
static int wr_require_repo(SOCKET s) {
    size_t need = wcslen(git_root()) + 8;
    wchar_t *dotgit = xmalloc(need * sizeof(wchar_t));
    _snwprintf(dotgit, need, L"%ls\\.git", git_root());
    DWORD attr = GetFileAttributesW(dotgit);
    free(dotgit);
    if (attr != INVALID_FILE_ATTRIBUTES) return 1;
    buf m;
    buf_init(&m);
    char *r = w2u8(git_root());
    buf_printf(&m, "不是一个 git 仓:%s", r ? r : "");
    free(r);
    wr_send_err3(s, buf_cstr(&m), "not_a_repo");
    buf_free(&m);
    return 0;
}

/* GitOps._head_branch：`rev-parse --abbrev-ref HEAD` 的 stdout.strip() or None。
 * **不看退出码**（未出生 HEAD 时 rc=128 但 stdout 是 "HEAD"）。 */
static char *wr_head_branch(void) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "rev-parse", "--abbrev-ref", "HEAD", NULL };
    git_run(args, NULL, 0, &o, &e);
    char *t = wr_text(o.p ? o.p : "", o.n);
    char *r = wr_strip_dup(t);
    free(t);
    buf_free(&o);
    buf_free(&e);
    if (!*r) { free(r); return NULL; }
    return r;
}

/* ================================================================ fetch */

static void wr_do_fetch(SOCKET s, const jnode *b) {
    if (!wr_require_repo(s)) return;

    char *remote = NULL;
    if (!wr_get_remote(s, b, &remote)) return;

    /* `if refspec is not None: _check_ref(refspec, "refspec")`；然后
     * `if refspec: ... elif branch: ...`。branch **不校验**。 */
    char *extra = NULL;
    const jnode *rs = jget(b, "refspec");
    const jnode *bv = jget(b, "branch");
    if (rs && bjtype(rs) != BJ_NULL) {
        if (!wr_check_ref_node(s, rs, "refspec", &extra)) {
            free(remote);
            return;
        }
    } else if (wr_truthy(bv)) {
        extra = wr_pystr(bv);
    }

    wr_gate g;
    wr_gate_resolve(&g);
    if (!wr_gate_allows(&g, "read")) {
        /* Python 的 _run 在撞闸前已经记过一笔 COUNT_HOOK。 */
        git_count_note("fetch");
        wr_send_net_disabled(s, &g, "read", "fetch");
        free(remote);
        free(extra);
        return;
    }

    const char *args[4];
    int k = 0;
    args[k++] = "fetch";
    args[k++] = remote;
    if (extra) args[k++] = extra;
    args[k] = NULL;

    buf out, err;
    buf_init(&out);
    buf_init(&err);
    int rc = git_run(args, NULL, 0, &out, &err);
    if (rc != 0) {
        wr_send_failed(s, &out, &err, "fetch_failed", remote, NULL);
    } else {
        char *output = wr_git_output(&out, &err);
        char *head = wr_head_branch();
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":true,\"op\":\"fetch\",\"remote\":");
        json_str(&o, remote);
        buf_puts(&o, ",\"output\":");
        json_str(&o, output);
        buf_puts(&o, ",\"head\":");
        if (head) json_str(&o, head);
        else buf_puts(&o, "null");
        buf_putc(&o, '}');
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
        free(output);
        free(head);
    }
    buf_free(&out);
    buf_free(&err);
    free(remote);
    free(extra);
}

/* ================================================================ pull */

static void wr_do_pull(SOCKET s, const jnode *b, int confirmed) {
    char *remote = NULL;
    if (!wr_get_remote(s, b, &remote)) return;

    wr_gate g;
    wr_gate_resolve(&g);
    if (!wr_gate_allows(&g, "write")) {
        wr_send_net_disabled(s, &g, "write", "pull");
        free(remote);
        return;
    }
    if (!confirmed) {
        wr_send_needs_confirm(s, "pull");
        free(remote);
        return;
    }
    if (!wr_require_repo(s)) { free(remote); return; }

    char *branch = NULL;
    const jnode *bv = jget(b, "branch");
    if (wr_truthy(bv)) {
        if (!wr_check_ref_node(s, bv, "分支", &branch)) { free(remote); return; }
    }

    const char *args[4];
    int k = 0;
    args[k++] = "pull";
    args[k++] = remote;
    if (branch) args[k++] = branch;
    args[k] = NULL;

    buf out, err;
    buf_init(&out);
    buf_init(&err);
    int rc = git_run(args, NULL, 0, &out, &err);
    if (rc != 0) {
        wr_send_failed(s, &out, &err, "pull_failed", remote, NULL);
    } else {
        char *output = wr_git_output(&out, &err);
        char *head = wr_head_branch();
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":true,\"op\":\"pull\",\"remote\":");
        json_str(&o, remote);
        buf_puts(&o, ",\"output\":");
        json_str(&o, output);
        buf_puts(&o, ",\"head\":");
        if (head) json_str(&o, head);
        else buf_puts(&o, "null");
        buf_putc(&o, '}');
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
        free(output);
        free(head);
    }
    buf_free(&out);
    buf_free(&err);
    free(remote);
    free(branch);
}

/* ================================================================ push */

static void wr_do_push(SOCKET s, const jnode *b, int force, int del, int confirmed) {
    char *remote = NULL;
    if (!wr_get_remote(s, b, &remote)) return;

    /* 先要闸，后要确认：闸关着时报的必须是 net_disabled，不是 needs_confirmation。 */
    wr_gate g;
    wr_gate_resolve(&g);
    if (!wr_gate_allows(&g, "write")) {
        wr_send_net_disabled(s, &g, "write", "push");
        free(remote);
        return;
    }
    if (!confirmed) {
        wr_send_needs_confirm(s, "push");
        free(remote);
        return;
    }
    if (!wr_require_repo(s)) { free(remote); return; }

    /* branch = _check_ref(branch or _head_branch() or "HEAD", "分支") */
    char *branch = NULL;
    const jnode *bv = jget(b, "branch");
    if (wr_truthy(bv)) {
        if (!wr_check_ref_node(s, bv, "分支", &branch)) { free(remote); return; }
    } else {
        char *hb = wr_head_branch();
        const char *name = (hb && *hb) ? hb : "HEAD";
        int ok = wr_check_ref_str(s, name, strlen(name), "分支", &branch);
        free(hb);
        if (!ok) { free(remote); return; }
    }

    const char *args[5];
    int k = 0;
    args[k++] = "push";
    if (del) {
        args[k++] = remote;
        args[k++] = "--delete";
        args[k++] = branch;
    } else if (force) {
        args[k++] = "--force";
        args[k++] = remote;
        args[k++] = branch;
    } else {
        args[k++] = remote;
        args[k++] = branch;
    }
    args[k] = NULL;

    buf out, err;
    buf_init(&out);
    buf_init(&err);
    int rc = git_run(args, NULL, 0, &out, &err);
    if (rc != 0) {
        wr_send_failed(s, &out, &err, "push_failed", remote, branch);
    } else {
        char *output = wr_git_output(&out, &err);
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":true,\"op\":\"push\",\"remote\":");
        json_str(&o, remote);
        buf_puts(&o, ",\"branch\":");
        json_str(&o, branch);
        buf_puts(&o, ",\"forced\":");
        buf_puts(&o, force ? "true" : "false");
        buf_puts(&o, ",\"deleted\":");
        buf_puts(&o, del ? "true" : "false");
        buf_puts(&o, ",\"output\":");
        json_str(&o, output);
        buf_putc(&o, '}');
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
        free(output);
    }
    buf_free(&out);
    buf_free(&err);
    free(remote);
    free(branch);
}

/* ================================================================ 分派 */

int write_remote_post(const char *path, const char *query, const char *body,
                      size_t body_len, SOCKET s) {
    (void)query;
    int which = 0;
    if (!strcmp(path, "/api/remote/fetch")) which = 1;
    else if (!strcmp(path, "/api/remote/push")) which = 2;
    else if (!strcmp(path, "/api/remote/pull")) which = 3;
    if (!which) return 0;

    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;

    if (which == 1) {
        wr_do_fetch(s, b);
    } else if (which == 3) {
        int confirmed = 0;
        if (wbody_bool(s, b, "confirmed", 0, &confirmed))
            wr_do_pull(s, b, confirmed);
    } else {
        /* server.py 的参数求值顺序：_bb(force) -> _bb(delete) -> _bb(confirmed)。 */
        int force = 0, del = 0, confirmed = 0;
        if (wbody_bool(s, b, "force", 0, &force) &&
            wbody_bool(s, b, "delete", 0, &del) &&
            wbody_bool(s, b, "confirmed", 0, &confirmed)) {
            wr_do_push(s, b, force, del, confirmed);
        }
    }
    jfree(b);
    return 1;
}
