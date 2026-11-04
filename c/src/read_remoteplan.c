/* read_remoteplan.c —— 远程计划面只读端点（M1）。
 *
 * 两个端点，逐字段对照 Python 版：
 *   /api/remote/heads?remote=origin     <- gitops.py::GitOps.ls_remote
 *   /api/remote/plan/<op>?remote=&branch=&force=&delete=
 *                                       <- server.py::_remote_plan
 *                                          -> gitops.py::plan_fetch / plan_push
 *
 * 只读：这里只 spawn `rev-parse` / `rev-list` / `log` / `ls-remote`，
 * 不写任何 ref、不碰工作区。`ls-remote` 是唯一会走出仓库的动词，所以它过
 * 能力闸（NetGate 默认 off -> 400 + code=net_disabled，形状与 Python 的
 * NetError.to_dict() 逐字对齐，含 required / current / op 三个 extra）。
 * `plan_*` 是**纯本地预演**，闸关着也能看 —— 这与 Python 版一致
 * （plan_fetch 只读 rev-parse/rev-list，plan_push 只读 rev-parse/log）。
 *
 * 与 Python 版的对齐点（都是照 gitops.py 原文抄的，不是推测）：
 *   * `_require_repo` 先于远程名校验，远程名校验先于闸 —— 所以非仓里
 *     永远先拿到 not_a_repo；
 *   * 闸的判定点在 `_run` 里、**fork 之前**，但计数钩子在那之前就记了一笔：
 *     Python 在 net_disabled 时 `X-aigit-git-calls` 仍是 1。C 版不能在
 *     git.c 里加计数入口（不许改其它文件），所以这一种情况下头里的计数
 *     会比 Python 少 1 —— **JSON 与状态码不受影响**，见交付说明；
 *   * `_qflag` 只认 "1"/"true"/"yes"/"on"（大小写敏感，`TRUE` 是假）；
 *   * `plan_fetch` 的 `branch` **不校验**（只有 ahead_behind 里才 _check_ref），
 *     所以 `?branch=bad..ref` 在有跟踪 ref 时才会 400；
 *   * `_head_branch()` 的空输出当 None，未出生 HEAD 时它返回 "HEAD"。
 *
 * 这里不写任何 500：所有可预期失败都走 400，文案与 Python 版逐字一致。
 */
#include "routes.h"

#include <ctype.h>
#include <stdint.h>
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

/* ================================================================ 小工具 */

static int rp_is_ws(int c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

/* Python 的 str.strip()（ASCII 空白口径，够用：git 输出与参数都是这个范围）。 */
static char *rp_strip_dup(const char *s) {
    return py_strip_dup(s);
}

/* Python 的 universal newlines：\r\n 与 \r 都变 \n。 */
static char *rp_normalize_newlines(const char *s, size_t n) {
    buf b;
    buf_init(&b);
    for (size_t i = 0; i < n; i++) {
        if (s[i] == '\r') {
            if (i + 1 < n && s[i + 1] == '\n') i++;
            buf_putc(&b, '\n');
        } else {
            buf_putc(&b, s[i]);
        }
    }
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

/* Python repr(str) 的近似（错误文案里的 %r），支持内嵌 NUL。 */
static char *rp_py_repr_n(const char *s, size_t n) {
    return py_repr_n(s, n);
}

static char *rp_py_repr(const char *s) { return rp_py_repr_n(s, strlen(s)); }

/* query 参数解码：保留内嵌 NUL（Python 的 parse_qs 也不截断，名字校验会
 * 把它按 %r 打出来）。返回 1=找到 0=没有 -1=值过大。 */
static int rp_q_raw(const char *query, const char *key, char **out, size_t *outlen) {
    int r = q_get_n(query, key, out, outlen);
    if (r == 1 && *outlen > 65536) {
        free(*out);
        *out = NULL;
        *outlen = 0;
        return -1;
    }
    return r;
}

static void rp_j_bool(buf *o, int v) { buf_puts(o, v ? "true" : "false"); }

static void rp_j_str_or_null(buf *o, const char *s) {
    if (s) json_str(o, s);
    else buf_puts(o, "null");
}

static int rp_run_git(buf *out, buf *err, const char *const args[]) {
    if (out) buf_reset(out);
    if (err) buf_reset(err);
    return git_run(args, NULL, 0, out, err);
}

static void rp_send_json_buf(SOCKET s, buf *o, int code) {
    http_send_json(s, code, buf_cstr(o), o->n);
}

/* Python 的 `len(s.split())`：按任意空白切、丢空串。 */
static long long rp_token_count(const char *s) {
    long long n = 0;
    const char *p = s;
    while (p && *p) {
        while (*p && rp_is_ws((unsigned char)*p)) p++;
        if (!*p) break;
        n++;
        while (*p && !rp_is_ws((unsigned char)*p)) p++;
    }
    return n;
}

/* ================================================================ 错误响应 */

/* NetError.to_dict() 的三键形（bad_remote / bad_ref） */
static void rp_send_neterr(SOCKET s, const char *msg, const char *code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":");
    json_str(&o, code);
    buf_putc(&o, '}');
    rp_send_json_buf(s, &o, 400);
    buf_free(&o);
}

/* gitops._run(check=True) 失败：code=git_failed, verb, returncode */
static void rp_send_git_failed(SOCKET s, buf *err, buf *out, const char *verb, int rc) {
    const char *src = NULL;
    if (err && err->n) src = buf_cstr(err);
    else if (out && out->n) src = buf_cstr(out);
    if (!src) src = "git 失败";
    char *norm = rp_normalize_newlines(src, strlen(src));
    char *msg = rp_strip_dup(norm);
    free(norm);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":\"git_failed\",\"verb\":");
    json_str(&o, verb);
    buf_printf(&o, ",\"returncode\":%d}", rc);
    rp_send_json_buf(s, &o, 400);
    buf_free(&o);
    free(msg);
}

/* gitops.ls_remote 的 `_need(rc == 0, msg, code="unreachable", remote=...)` */
static void rp_send_unreachable(SOCKET s, buf *err, buf *out, const char *remote) {
    const char *src = NULL;
    if (err && err->n) src = buf_cstr(err);
    else if (out && out->n) src = buf_cstr(out);
    if (!src) src = "ls-remote 失败";
    char *norm = rp_normalize_newlines(src, strlen(src));
    char *msg = rp_strip_dup(norm);
    free(norm);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":\"unreachable\",\"remote\":");
    json_str(&o, remote);
    buf_putc(&o, '}');
    rp_send_json_buf(s, &o, 400);
    buf_free(&o);
    free(msg);
}

/* ================================================================ 参数校验 */

/* gitops._check_remote_name：[A-Za-z0-9][A-Za-z0-9._-]{0,99} */
static int rp_check_remote_name_n(const char *name, size_t n, char **msg) {
    int ok = n >= 1 && n <= 100 && isalnum((unsigned char)name[0]);
    for (size_t i = 1; ok && i < n; i++) {
        char c = name[i];
        if (!isalnum((unsigned char)c) && c != '.' && c != '_' && c != '-') ok = 0;
    }
    if (!ok) {
        char *rp = rp_py_repr_n(name, n);
        buf b;
        buf_init(&b);
        buf_printf(&b, "远程名不合法:%s(只允许字母数字与 . _ -, 字母数字开头, 最长 100)", rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        free(rp);
        return 0;
    }
    return 1;
}

/* _REF_RE 的正文部分：首字符字母数字，其余 [A-Za-z0-9._/-]，长 1..200 */
static int rp_ref_body_ok(const char *s, size_t n) {
    if (n < 1 || n > 200) return 0;
    if (!isalnum((unsigned char)s[0])) return 0;
    for (size_t i = 1; i < n; i++) {
        char c = s[i];
        if (!isalnum((unsigned char)c) && c != '.' && c != '_' && c != '/' && c != '-')
            return 0;
    }
    return 1;
}

/* gitops._check_ref，检查顺序与文案逐条对齐。 */
static int rp_check_ref_n(const char *ref, size_t n, const char *what,
                          char **msg, const char **code) {
    buf b;
    buf_init(&b);
    *code = "bad_ref";
    if (n && ref[0] == '-') {
        char *rp = rp_py_repr_n(ref, n);
        buf_printf(&b, "%s 不能以 - 开头(git 会把它当选项读):%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    if (memchr(ref, ':', n)) {
        char *rp = rp_py_repr_n(ref, n);
        buf_printf(&b, "%s 不能含 ':'(那是 refspec 的分隔符, 会让调用方越权指定 ref):%s",
                   what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    if (py_has_space_or_ctrl(ref, n)) {
        char *rp = rp_py_repr_n(ref, n);
        buf_printf(&b, "%s 不能含空白或控制字符:%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    int ok = 0;
    if (n >= 11 && !strncmp(ref, "refs/heads/", 11)) ok = rp_ref_body_ok(ref + 11, n - 11);
    if (!ok && n >= 10 && !strncmp(ref, "refs/tags/", 10)) ok = rp_ref_body_ok(ref + 10, n - 10);
    if (!ok) ok = rp_ref_body_ok(ref, n);
    if (!ok) {
        char *rp = rp_py_repr_n(ref, n);
        buf_printf(&b, "%s 不合法:%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    buf_free(&b);
    return 1;
}

static int rp_check_ref(const char *ref, const char *what, char **msg, const char **code) {
    return rp_check_ref_n(ref, strlen(ref), what, msg, code);
}

/* ================================================================ 能力闸 */

typedef struct {
    const char *level;      /* "off" / "read" / "write" */
    const char *source;     /* "default" / "env:AIGIT_NET" */
    char       *note;       /* 环境变量读不懂时的降级说明，否则 NULL */
} rp_gate;

static int rp_ascii_lower_eq(const char *a, const char *b) {
    while (*a && *b) {
        int ca = tolower((unsigned char)*a), cb = tolower((unsigned char)*b);
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

/* NetGate.__init__：显式参数 > AIGIT_NET > off，读不懂按 off（fail closed）。 */
static void rp_gate_resolve(rp_gate *g) {
    g->level = "off";
    g->source = "default";
    g->note = NULL;
    const char *raw = getenv("AIGIT_NET");
    if (!raw) return;
    g->source = "env:AIGIT_NET";
    char *t = rp_strip_dup(raw);
    const char *lv = NULL;
    if (!*t || !strcmp(t, "0") || rp_ascii_lower_eq(t, "off") ||
        rp_ascii_lower_eq(t, "none") || rp_ascii_lower_eq(t, "no") ||
        rp_ascii_lower_eq(t, "false")) lv = "off";
    else if (!strcmp(t, "1") || rp_ascii_lower_eq(t, "read") ||
             rp_ascii_lower_eq(t, "on") || rp_ascii_lower_eq(t, "yes") ||
             rp_ascii_lower_eq(t, "true") || rp_ascii_lower_eq(t, "ro")) lv = "read";
    else if (!strcmp(t, "2") || rp_ascii_lower_eq(t, "write") ||
             rp_ascii_lower_eq(t, "rw") || rp_ascii_lower_eq(t, "push")) lv = "write";
    if (!lv) {
        lv = "off";
        char *rp = rp_py_repr(raw);
        buf nb;
        buf_init(&nb);
        buf_printf(&nb, "AIGIT_NET=%s 读不懂(只认 off/read/write),已按 off 处理。", rp);
        g->note = xstrdup(buf_cstr(&nb));
        buf_free(&nb);
        free(rp);
    }
    g->level = lv;
    free(t);
}

static void rp_gate_free(rp_gate *g) {
    free(g->note);
    g->note = NULL;
}

static int rp_gate_allows(const rp_gate *g, const char *need) {
    static const char *LV[] = { "off", "read", "write" };
    int cur = 0, req = 0;
    for (int i = 0; i < 3; i++) {
        if (!strcmp(g->level, LV[i])) cur = i;
        if (!strcmp(need, LV[i])) req = i;
    }
    return cur >= req;
}

/* NetGate.describe()（与 read_conflict.c 的同名逻辑同源，这里是本文件私有副本） */
static const char *RP_READ_OPS[] = {
    "archive --exec", "archive --remote", "fetch", "fetch-pack", "ls-remote",
    "remote show", "remote update", "remote-ext", "remote-fd", NULL
};
static const char *RP_WRITE_OPS[] = {
    "clone", "credential", "credential-cache", "credential-store", "daemon",
    "http-backend", "pull", "push", "receive-pack", "request-pull",
    "send-pack", "submodule", "upload-pack", NULL
};
#define RP_LOCAL_VERBS_COUNT 55

static void rp_gate_json(buf *o, const rp_gate *g) {
    int allows_read = strcmp(g->level, "read") == 0 || strcmp(g->level, "write") == 0;
    int allows_write = strcmp(g->level, "write") == 0;
    buf_puts(o, "{\"level\":");
    json_str(o, g->level);
    buf_puts(o, ",\"source\":");
    json_str(o, g->source);
    buf_puts(o, ",\"env_var\":\"AIGIT_NET\",\"levels\":[\"off\",\"read\",\"write\"],\"allows_read\":");
    rp_j_bool(o, allows_read);
    buf_puts(o, ",\"allows_write\":");
    rp_j_bool(o, allows_write);
    buf_puts(o, ",\"read_ops\":[");
    for (int i = 0; RP_READ_OPS[i]; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, RP_READ_OPS[i]);
    }
    buf_puts(o, "],\"write_ops\":[");
    for (int i = 0; RP_WRITE_OPS[i]; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, RP_WRITE_OPS[i]);
    }
    buf_printf(o, "],\"local_verbs\":%d", RP_LOCAL_VERBS_COUNT);
    if (g->note) {
        buf_puts(o, ",\"note\":");
        json_str(o, g->note);
    }
    buf_putc(o, '}');
}

/* NetGate.require(need, op)：不满足就 400 + net_disabled（含三个 extra）。 */
static void rp_send_net_disabled(SOCKET s, const rp_gate *g, const char *need,
                                 const char *op) {
    buf opb;
    buf_init(&opb);
    if (op) buf_printf(&opb, "(%s)", op);
    buf m;
    buf_init(&m);
    buf_printf(&m,
               "远程能力「%s」当前是 **%s**,拒绝执行%s。"
               "aigit 默认不联网;要开就显式设 AIGIT_NET=%s(本进程默认 %s)。",
               need, g->level, buf_cstr(&opb), need, g->source);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, buf_cstr(&m));
    buf_puts(&o, ",\"code\":\"net_disabled\",\"required\":");
    json_str(&o, need);
    buf_puts(&o, ",\"current\":");
    json_str(&o, g->level);
    buf_puts(&o, ",\"op\":");
    rp_j_str_or_null(&o, op);
    buf_putc(&o, '}');
    rp_send_json_buf(s, &o, 400);
    buf_free(&o);
    buf_free(&m);
    buf_free(&opb);
}

/* ================================================================ 仓库前置 */

/* GitOps._require_repo：root/.git 必须存在（.git 是文件也算，链接工作区）。 */
static int rp_require_repo(SOCKET s) {
    wchar_t *dotgit = xmalloc((wcslen(git_root()) + 8) * sizeof(wchar_t));
    _snwprintf(dotgit, wcslen(git_root()) + 8, L"%ls\\.git", git_root());
    DWORD attr = GetFileAttributesW(dotgit);
    free(dotgit);
    if (attr != INVALID_FILE_ATTRIBUTES) return 1;
    buf m;
    buf_init(&m);
    char *r = w2u8(git_root());
    buf_printf(&m, "不是一个 git 仓:%s", r ? r : "");
    free(r);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, buf_cstr(&m));
    buf_puts(&o, ",\"code\":\"not_a_repo\"}");
    rp_send_json_buf(s, &o, 400);
    buf_free(&o);
    buf_free(&m);
    return 0;
}

/* GitOps._head_branch：rev-parse --abbrev-ref HEAD，空输出当 None。 */
static char *rp_head_branch(void) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "rev-parse", "--abbrev-ref", "HEAD", NULL };
    char *out = NULL;
    if (rp_run_git(&o, &e, args) == 0) {
        char *t = rp_strip_dup(buf_cstr(&o));
        if (*t) out = t;
        else free(t);
    }
    buf_free(&o);
    buf_free(&e);
    return out;
}

/* `branch or _head_branch() or "HEAD"`（branch 为 NULL/空都算没给）。 */
static char *rp_branch_or_head(const char *branch_in) {
    if (branch_in && *branch_in) return xstrdup(branch_in);
    char *hb = rp_head_branch();
    if (hb && *hb) return hb;
    free(hb);
    return xstrdup("HEAD");
}

/* 拼 "<remote>/<branch>" */
static char *rp_up_name(const char *remote, const char *branch) {
    buf b;
    buf_init(&b);
    buf_printf(&b, "%s/%s", remote, branch);
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

static int rp_rev_parse_verify(const char *ref) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "rev-parse", "--verify", "--quiet", ref, NULL };
    int rc = rp_run_git(&o, &e, args);
    buf_free(&o);
    buf_free(&e);
    return rc == 0;
}

/* GitOps._has_tracking */
static int rp_has_tracking(const char *remote, const char *branch_in) {
    char *b = rp_branch_or_head(branch_in);
    char *up = rp_up_name(remote, b);
    int has = rp_rev_parse_verify(up);
    free(up);
    free(b);
    return has;
}

/* ================================================================ ahead_behind */

typedef struct {
    char *branch;        /* 实际用的分支名（已校验） */
    int   tracking;      /* trackingRefExists */
    long long ahead;
    int   behind_null;   /* 无跟踪 ref 时 behind 是 null */
    long long behind;
    int   not_tracking;  /* 走的是「本地没有这个跟踪 ref」那一支 */
} rp_ab;

static void rp_ab_free(rp_ab *a) {
    free(a->branch);
    a->branch = NULL;
}

/* 把 ahead_behind 的对象写进 o。返回 0 表示已经发过错误响应。 */
static int rp_ahead_behind(const char *remote, const char *branch_in, rp_ab *out,
                           buf *o, SOCKET s) {
    memset(out, 0, sizeof(*out));
    char *branch = rp_branch_or_head(branch_in);

    char *emsg = NULL;
    const char *code = NULL;
    if (!rp_check_ref(branch, "分支", &emsg, &code)) {
        rp_send_neterr(s, emsg, code);
        free(emsg);
        free(branch);
        return 0;
    }
    out->branch = branch;
    char *up = rp_up_name(remote, branch);

    if (!rp_rev_parse_verify(up)) {
        /* 没有跟踪 ref：rev-list <branch>（check=True，失败就是 git_failed） */
        buf so, se;
        buf_init(&so);
        buf_init(&se);
        const char *args[] = { "rev-list", branch, NULL };
        int rc = rp_run_git(&so, &se, args);
        if (rc != 0) {
            rp_send_git_failed(s, &se, &so, "rev-list", rc);
            buf_free(&so);
            buf_free(&se);
            free(up);
            return 0;
        }
        out->tracking = 0;
        out->not_tracking = 1;
        out->ahead = rp_token_count(buf_cstr(&so));
        out->behind_null = 1;
        buf_free(&so);
        buf_free(&se);

        buf_puts(o, "{\"ok\":true,\"remote\":");
        json_str(o, remote);
        buf_puts(o, ",\"branch\":");
        json_str(o, branch);
        buf_puts(o, ",\"basis\":");
        buf b1;
        buf_init(&b1);
        buf_printf(&b1, "本地没有 %s 这个远程跟踪 ref", up);
        json_str(o, buf_cstr(&b1));
        buf_free(&b1);
        buf_puts(o, ",\"trackingRefExists\":false,\"ahead\":");
        buf_printf(o, "%lld", out->ahead);
        buf_puts(o, ",\"behind\":null,\"note\":");
        buf b2;
        buf_init(&b2);
        buf_printf(&b2, "本地没有 %s 这个远程跟踪 ref,上面所有提交都算「待推」", up);
        json_str(o, buf_cstr(&b2));
        buf_free(&b2);
        buf_putc(o, '}');
        free(up);
        return 1;
    }

    buf so, se;
    buf_init(&so);
    buf_init(&se);
    buf range;
    buf_init(&range);
    buf_printf(&range, "%s...%s", up, branch);
    const char *args[] = { "rev-list", "--left-right", "--count", buf_cstr(&range), NULL };
    rp_run_git(&so, &se, args);
    buf_free(&range);
    /* behind, ahead = (int(x) for x in out.split()[:2]) */
    long long nums[2] = { 0, 0 };
    {
        int k = 0;
        const char *p = buf_cstr(&so);
        while (k < 2 && p && *p) {
            while (*p && rp_is_ws((unsigned char)*p)) p++;
            if (!*p) break;
            const char *b = p;
            while (*p && !rp_is_ws((unsigned char)*p)) p++;
            char *tok = xstrndup(b, (size_t)(p - b));
            nums[k++] = strtoll(tok, NULL, 10);
            free(tok);
        }
    }
    buf_free(&so);
    buf_free(&se);
    out->tracking = 1;
    out->behind = nums[0];
    out->ahead = nums[1];

    buf_puts(o, "{\"ok\":true,\"remote\":");
    json_str(o, remote);
    buf_puts(o, ",\"branch\":");
    json_str(o, branch);
    buf_puts(o, ",\"basis\":");
    buf b1;
    buf_init(&b1);
    buf_printf(&b1, "本地远程跟踪 ref %s(可能陈旧; 要知道远程真实现状请先 fetch)", up);
    json_str(o, buf_cstr(&b1));
    buf_free(&b1);
    buf_puts(o, ",\"trackingRefExists\":true,\"ahead\":");
    buf_printf(o, "%lld", out->ahead);
    buf_puts(o, ",\"behind\":");
    buf_printf(o, "%lld", out->behind);
    buf_putc(o, '}');
    free(up);
    return 1;
}

/* ================================================================ remote 参数 */

/* `q.get("remote", ["origin"])[0]` + `_check_remote_name`。
 * 校验失败返回 NULL 并已发 400；成功返回 malloc 的名字（缺省 "origin"）。 */
static char *rp_resolve_remote(const char *rval, size_t rlen, int gr, SOCKET s) {
    if (gr < 0) {
        http_send_err(s, 400, "请求参数不合法:参数过长", NULL);
        return NULL;
    }
    if (gr == 1 && rlen > 0) {
        char *emsg = NULL;
        if (!rp_check_remote_name_n(rval, rlen, &emsg)) {
            rp_send_neterr(s, emsg, "bad_remote");
            free(emsg);
            return NULL;
        }
        return xstrndup(rval, rlen);
    }
    return xstrdup("origin");
}

/* ================================================================ /api/remote/heads */

static int h_heads(const char *query, SOCKET s) {
    if (!rp_require_repo(s)) return 1;

    char *rval = NULL;
    size_t rlen = 0;
    int gr = rp_q_raw(query, "remote", &rval, &rlen);
    char *remote_buf = rp_resolve_remote(rval, rlen, gr, s);
    free(rval);
    if (!remote_buf) return 1;
    const char *remote = remote_buf;

    /* _run 的第一道：能力闸（在 fork 之前） */
    rp_gate g;
    rp_gate_resolve(&g);
    if (!rp_gate_allows(&g, "read")) {
        /* Python 的 COUNT_HOOK 在闸之前就记了 ls-remote；这里只记账不 spawn。 */
        git_count_note("ls-remote");
        rp_send_net_disabled(s, &g, "read", "ls-remote");
        rp_gate_free(&g);
        free(remote_buf);
        return 1;
    }
    rp_gate_free(&g);

    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "ls-remote", "--heads", remote, NULL };
    int rc = rp_run_git(&o, &e, args);
    if (rc != 0) {
        rp_send_unreachable(s, &e, &o, remote);
        buf_free(&o);
        buf_free(&e);
        free(remote_buf);
        return 1;
    }

    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"ok\":true,\"remote\":");
    json_str(&r, remote);
    buf_puts(&r, ",\"heads\":[");
    long long count = 0;
    {
        char *txt = rp_normalize_newlines(buf_cstr(&o), o.n);
        const char *line = txt;
        while (line) {
            const char *nl = strchr(line, '\n');
            size_t ln = nl ? (size_t)(nl - line) : strlen(line);
            /* sha, _, ref = line.partition("\t") */
            const char *tab = memchr(line, '\t', ln);
            size_t shan = tab ? (size_t)(tab - line) : ln;
            const char *refp = tab ? tab + 1 : line + ln;
            size_t refn = (size_t)(line + ln - refp);
            if (refn >= 11 && !strncmp(refp, "refs/heads/", 11)) {
                char *sha = xstrndup(line, shan);
                char *ref = xstrndup(refp, refn);
                if (count) buf_putc(&r, ',');
                buf_puts(&r, "{\"sha\":");
                json_str(&r, sha);
                buf_puts(&r, ",\"ref\":");
                json_str(&r, ref);
                buf_puts(&r, ",\"branch\":");
                json_str(&r, ref + 11);
                buf_putc(&r, '}');
                free(sha);
                free(ref);
                count++;
            }
            line = nl ? nl + 1 : NULL;
        }
        free(txt);
    }
    buf_puts(&r, "],\"count\":");
    buf_printf(&r, "%lld", count);
    buf_putc(&r, '}');
    rp_send_json_buf(s, &r, 200);
    buf_free(&r);
    buf_free(&o);
    buf_free(&e);
    free(remote_buf);
    return 1;
}

/* ================================================================ plan_fetch */

static int h_plan_fetch(const char *remote, const char *branch, SOCKET s) {
    int tracking = rp_has_tracking(remote, branch);

    buf r;
    buf_init(&r);
    rp_ab ab;
    memset(&ab, 0, sizeof(ab));
    buf abj;
    buf_init(&abj);
    int ab_ok = 1;
    if (tracking) {
        ab_ok = rp_ahead_behind(remote, branch, &ab, &abj, s);
        if (!ab_ok) {
            buf_free(&abj);
            buf_free(&r);
            rp_ab_free(&ab);
            return 1;      /* 已经发过错误响应 */
        }
    }

    buf_puts(&r, "{\"op\":\"fetch\",\"safe\":true,\"warning\":null,\"blocked\":null,"
                 "\"executable\":true,\"requires_confirmation\":false,\"remote\":");
    json_str(&r, remote);
    buf_puts(&r, ",\"branch\":");
    rp_j_str_or_null(&r, branch);
    buf_puts(&r, ",\"what\":");
    {
        const char *b = (branch && *branch) ? branch : "?";
        buf w;
        buf_init(&w);
        buf_printf(&w,
                   "只会往本地写 %s/%s 这个远程跟踪 ref 与 FETCH_HEAD。"
                   "不碰工作区, 不推任何东西, 撤销 = git reset --hard %s/%s",
                   remote, b, remote, b);
        json_str(&r, buf_cstr(&w));
        buf_free(&w);
    }
    buf_puts(&r, ",\"aheadBehind\":");
    if (tracking) buf_append(&r, abj.p, abj.n);
    else buf_puts(&r, "null");
    buf_putc(&r, '}');
    rp_send_json_buf(s, &r, 200);

    buf_free(&abj);
    buf_free(&r);
    rp_ab_free(&ab);
    return 1;
}

/* ================================================================ plan_push */

static int h_plan_push(const char *remote, const char *branch_in, int force,
                       int del, SOCKET s) {
    rp_gate g;
    rp_gate_resolve(&g);

    char *branch = rp_branch_or_head(branch_in);
    char *emsg = NULL;
    const char *code = NULL;
    if (!rp_check_ref(branch, "分支", &emsg, &code)) {
        rp_send_neterr(s, emsg, code);
        free(emsg);
        free(branch);
        rp_gate_free(&g);
        return 1;
    }

    int executable = rp_gate_allows(&g, "write");

    if (del) {
        buf r;
        buf_init(&r);
        buf_puts(&r, "{\"op\":\"push-delete\",\"safe\":false,\"warning\":");
        {
            buf w;
            buf_init(&w);
            buf_printf(&w, "会**删除远程分支** %s/%s。删掉的东西要别人重新推才能回来。",
                       remote, branch);
            json_str(&r, buf_cstr(&w));
            buf_free(&w);
        }
        buf_puts(&r, ",\"blocked\":null,\"executable\":");
        rp_j_bool(&r, executable);
        buf_puts(&r, ",\"requires_confirmation\":true,\"remote\":");
        json_str(&r, remote);
        buf_puts(&r, ",\"branch\":");
        json_str(&r, branch);
        buf_puts(&r, ",\"commits\":[],\"count\":0,\"gate\":");
        rp_gate_json(&r, &g);
        buf_putc(&r, '}');
        rp_send_json_buf(s, &r, 200);
        buf_free(&r);
        free(branch);
        rp_gate_free(&g);
        return 1;
    }

    /* GitOps._unpushed：纯本地（rev-parse + log） */
    char *up = rp_up_name(remote, branch);
    int has_up = rp_rev_parse_verify(up);
    buf rng;
    buf_init(&rng);
    if (has_up) buf_printf(&rng, "%s..%s", up, branch);
    else buf_puts(&rng, branch);

    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "log", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s",
                           "-n", "200", buf_cstr(&rng), NULL };
    int rc = rp_run_git(&o, &e, args);
    if (rc != 0) {
        rp_send_git_failed(s, &e, &o, "log", rc);
        buf_free(&o);
        buf_free(&e);
        buf_free(&rng);
        free(up);
        free(branch);
        rp_gate_free(&g);
        return 1;
    }

    /* 逐行拆 \x1f，只有正好 5 段的才算一条提交 */
    buf rows;
    buf_init(&rows);
    long long ncommits = 0;
    {
        char *txt = rp_normalize_newlines(buf_cstr(&o), o.n);
        const char *line = txt;
        while (line) {
            const char *nl = strchr(line, '\n');
            size_t ln = nl ? (size_t)(nl - line) : strlen(line);
            const char *f[6];
            size_t fl[6];
            int nf = 0;
            const char *p = line, *endp = line + ln;
            while (nf < 6) {
                const char *sep = memchr(p, 0x1f, (size_t)(endp - p));
                f[nf] = p;
                fl[nf] = sep ? (size_t)(sep - p) : (size_t)(endp - p);
                nf++;
                if (!sep) break;
                p = sep + 1;
            }
            if (nf == 5) {
                if (ncommits) buf_putc(&rows, ',');
                buf_puts(&rows, "{\"sha\":");
                char *t0 = xstrndup(f[0], fl[0]); json_str(&rows, t0); free(t0);
                buf_puts(&rows, ",\"short\":");
                char *t1 = xstrndup(f[1], fl[1]); json_str(&rows, t1); free(t1);
                buf_puts(&rows, ",\"author\":");
                char *t2 = xstrndup(f[2], fl[2]); json_str(&rows, t2); free(t2);
                buf_puts(&rows, ",\"date\":");
                char *t3 = xstrndup(f[3], fl[3]); json_str(&rows, t3); free(t3);
                buf_puts(&rows, ",\"subject\":");
                char *t4 = xstrndup(f[4], fl[4]); json_str(&rows, t4); free(t4);
                buf_putc(&rows, '}');
                ncommits++;
            }
            line = nl ? nl + 1 : NULL;
        }
        free(txt);
    }

    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"op\":\"push\",\"safe\":false,\"warning\":");
    if (force) {
        buf w;
        buf_init(&w);
        buf_puts(&w, "**force push**: 远程上那条分支指针会被**无条件搬走**。"
                     "别人基于旧指针做的改动就此丢失, reflog 在**远程**上, "
                     "你这边救不回来。");
        json_str(&r, buf_cstr(&w));
        buf_free(&w);
    } else if (ncommits) {
        buf w;
        buf_init(&w);
        buf_printf(&w, "将把这 %lld 个提交推到 %s/%s。推出去撤不回来 —— 传输层"
                       "没有「撤回」这个概念。", ncommits, remote, branch);
        json_str(&r, buf_cstr(&w));
        buf_free(&w);
    } else {
        buf_puts(&r, "null");
    }
    buf_puts(&r, ",\"blocked\":null,\"executable\":");
    rp_j_bool(&r, executable);
    buf_puts(&r, ",\"requires_confirmation\":true,\"remote\":");
    json_str(&r, remote);
    buf_puts(&r, ",\"branch\":");
    json_str(&r, branch);
    buf_puts(&r, ",\"force\":");
    rp_j_bool(&r, force);
    buf_puts(&r, ",\"revRange\":");
    json_str(&r, buf_cstr(&rng));
    buf_puts(&r, ",\"commits\":[");
    buf_append(&r, rows.p ? rows.p : "", rows.n);
    buf_puts(&r, "],\"count\":");
    buf_printf(&r, "%lld", ncommits);
    buf_puts(&r, ",\"gate\":");
    rp_gate_json(&r, &g);
    buf_putc(&r, '}');
    rp_send_json_buf(s, &r, 200);

    buf_free(&r);
    buf_free(&rows);
    buf_free(&o);
    buf_free(&e);
    buf_free(&rng);
    free(up);
    free(branch);
    rp_gate_free(&g);
    return 1;
}

/* ================================================================ 分派 */

/* server.py 的 `_qflag`：缺省 false，只认 1/true/yes/on（大小写敏感）。 */
static int rp_qflag(const char *query, const char *key) {
    char *v = NULL;
    size_t vn = 0;
    int g = rp_q_raw(query, key, &v, &vn);
    if (g < 0) { free(v); return 0; }
    if (g == 0) { free(v); return 0; }
    int on = !strcmp(v, "1") || !strcmp(v, "true") || !strcmp(v, "yes") ||
             !strcmp(v, "on");
    free(v);
    return on;
}

static int h_plan(const char *path, const char *query, SOCKET s) {
    const char *op = strrchr(path, '/');
    op = op ? op + 1 : path;

    /* _remote_plan：先解析参数，再按 op 分派；不认识的 op 是 GitError（400，无 code） */
    if (strcmp(op, "fetch") && strcmp(op, "push")) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "没有这个预演:%s", op);
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":false,\"error\":");
        json_str(&o, buf_cstr(&m));
        buf_putc(&o, '}');
        rp_send_json_buf(s, &o, 400);
        buf_free(&o);
        buf_free(&m);
        return 1;
    }

    if (!rp_require_repo(s)) return 1;

    char *rval = NULL, *bval = NULL;
    size_t rlen = 0, blen = 0;
    int gr = rp_q_raw(query, "remote", &rval, &rlen);
    int gb = rp_q_raw(query, "branch", &bval, &blen);
    if (gr < 0 || gb < 0) {
        free(rval);
        free(bval);
        http_send_err(s, 400, "请求参数不合法:参数过长", NULL);
        return 1;
    }
    char *remote_buf = rp_resolve_remote(rval, rlen, gr, s);
    free(rval);
    if (!remote_buf) {
        free(bval);
        return 1;
    }
    const char *remote = remote_buf;
    const char *branch = (gb == 1 && blen > 0) ? bval : NULL;

    int rc;
    if (!strcmp(op, "fetch")) {
        rc = h_plan_fetch(remote, branch, s);
    } else {
        int force = rp_qflag(query, "force");
        int del = rp_qflag(query, "delete");
        rc = h_plan_push(remote, branch, force, del, s);
    }
    free(remote_buf);
    free(bval);
    return rc;
}

int read_remoteplan_get(const char *path, const char *query, SOCKET s) {
    if (!strcmp(path, "/api/remote/heads")) return h_heads(query, s);
    if (!strncmp(path, "/api/remote/plan/", 17)) return h_plan(path, query, s);
    return 0;
}
