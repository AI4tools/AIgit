/* write_more.c —— M2 写面第二组：checkout / branch / merge / merge-abort / reset。
 *
 * 对位 Python（server.py::do_POST）：
 *   POST /api/checkout    -> Repo.checkout_and_status(name)      + _ok_with_status
 *   POST /api/branch      -> Repo.new_branch_and_status(name)    + _ok_with_status
 *   POST /api/merge       -> Repo.merge(name)（**ok 来自 merge 自己**）
 *   POST /api/merge-abort -> Repo.abort_merge()                  + _ok_with_status
 *   POST /api/reset       -> mode/confirmed/ref 三道闸 + Repo.reset + _ok_with_status
 *
 * 逐项对齐的要点（都是读 Python 源码确认过的，不是猜）：
 *   * 分支名白名单 `[\w][\w./-]{0,99}`（fullmatch，**长度按码点算**，\w 是
 *     Unicode 的字母数字+下划线）。不合法只回 _BRANCH_HINT，不回显名字。
 *   * checkout 的第二道闸：`refs/heads/<name>` 必须真的存在（`show-ref
 *     --verify --quiet`），否则 `分支不存在,已拒绝 checkout:<name>`。Python 那条
 *     loose-ref 快路径只是省一次 spawn，判据本身仍以 show-ref 为准，所以这里
 *     直接跑 show-ref（结果相同、次数不同）。
 *   * `git` 失败 -> 400，文案 `(stderr or stdout or "git 失败").strip()`。
 *     注意 Python 用 `text=True`，universal newlines 会把 \r\n / \r 折成 \n，
 *     所以 C 侧也要折（否则 merge 的 output 逐字不同）。
 *   * merge 的 `output` 是 **stdout+stderr 拼接后再 strip**，`conflicted` 是
 *     `git diff --name-only --diff-filter=U` 的 stdout（**字符串**，不是数组）。
 *     merge 不看 `status: false`（Python 那边它直接 _json，不过 _ok_with_status）。
 *   * reset 的三道闸**顺序固定**：mode -> confirmed -> ref。mode 缺失/非法一律
 *     400（绝不猜默认值），文案里带 `%r`；confirmed 必须字面 true；ref 取
 *     `b.get("ref") or "HEAD"`（**falsy 一律退化成 HEAD**），非字符串 -> 400，
 *     再过 check_ref_name（`..` 也拒）。
 *   * hard 在 `git reset --hard` 之后还要 `git clean -fd`（check=False）。
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

#define BRANCH_HINT \
    "分支名不合法:只允许字母数字与 _ . / -,且必须以字母数字或 _ 开头(最长 100)"

#define REF_HINT \
    "ref 不合法:只允许 git rev-spec 的常见写法" \
    "(字母数字与 _ . / @ ~ ^ { } + -),首字符不能是 -,不能含 : 与空白"

#define MODE_HINT \
    "mode 必须是 'soft' / 'mixed' / 'hard' 之一(缺省是 400,绝不猜一个默认值),收到 "

#define RESET_NEEDS_CONFIRM \
    "reset 是破坏性操作,需要 confirmed=true;先 GET /api/reset/plan?mode=... 看清要丢什么。"

#define MERGE_EMPTY_NAME "要合并的分支名不能为空"

static void send_400(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
}

/* ---------------------------------------------------------------- 文本口径 */

/* Python `str.strip()` 的空白集（ASCII 那一半就够：git 输出与分支名里的
 * 「空白」不会出现 U+00A0 这种）。 */
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

/* ---------------------------------------------------------------- 名字校验 */

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

/* `_BRANCH_RE = [\w][\w./-]{0,99}` 的 fullmatch。 */
static int branch_name_ok(const char *s, size_t n) {
    if (!s || n == 0) return 0;
    if (memchr(s, 0, n)) return 0;            /* 内嵌 NUL：正则不可能匹配 */
    size_t i = 0, count = 0;
    int first = 1;
    while (i < n) {
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &i, &cp)) return 0;
        int ok = word_cp(cp);
        if (!ok && !first)
            ok = (cp == '.' || cp == '/' || cp == '-');
        if (!ok) return 0;
        first = 0;
        if (++count > 100) return 0;          /* {0,99} + 首字符 = 最多 100 */
    }
    return count >= 1;
}

/* `_REF_RE = [\w@^~][\w@^~{}+./-]*` 的 fullmatch，外加「不含 ..」。 */
static int ref_name_ok(const char *ref, size_t n) {
    if (!ref || n == 0) return 0;
    if (memchr(ref, 0, n)) return 0;
    size_t i = 0;
    int first = 1;
    while (i < n) {
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)ref, n, &i, &cp)) return 0;
        int ok;
        if (first) {
            ok = word_cp(cp) || cp == '@' || cp == '^' || cp == '~';
            first = 0;
        } else {
            ok = word_cp(cp) || cp == '@' || cp == '^' || cp == '~' ||
                 cp == '{' || cp == '}' || cp == '+' || cp == '.' ||
                 cp == '/' || cp == '-';
        }
        if (!ok) return 0;
    }
    for (size_t k = 0; k + 1 < n; k++)
        if (ref[k] == '.' && ref[k + 1] == '.') return 0;
    return 1;
}

/* Python `str.strip()` 的空白判定，含非 ASCII 空白（U+00A0 / U+3000 …）。 */
static int py_space_cp(unsigned cp) {
    if (cp < 0x80) return py_space((unsigned char)cp);
    return cp == 0x85 || cp == 0xa0 || cp == 0x1680 ||
           (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 ||
           cp == 0x202f || cp == 0x205f || cp == 0x3000;
}

/* 整个串是不是「只有空白」（空串也算）—— 对位 `not name.strip()`。 */
static int all_space_utf8(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &i, &cp)) return 0;
        if (!py_space_cp(cp)) return 0;
    }
    return 1;
}

/* Python repr 的常用子集（错误文案里的 `%r` 用）。float 单独处理：
 * w_repr 的 `%g` 会把 5.0 打成 "5"，而 Python repr 是 "5.0"。 */
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

/* ---------------------------------------------------------------- git 封装 */

/* Python `(p.stderr or p.stdout or "git 失败").strip()`，含 universal newlines。 */
static void send_git_error(SOCKET s, const buf *out, const buf *err) {
    const char *src = NULL;
    size_t n = 0;
    if (err && err->n) { src = err->p; n = err->n; }
    else if (out && out->n) { src = out->p; n = out->n; }
    if (!src) { send_400(s, "git 失败"); return; }
    char *t = py_text(src, n);
    py_strip(t);
    send_400(s, t);
    free(t);
}

/* 跑一条 git；成功返回 1，失败已发 400 并返回 0。 */
static int run_or_400(SOCKET s, const char *const args[]) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);
    int ok = 1;
    if (rc != 0) {
        send_git_error(s, &o, &e);
        ok = 0;
    }
    buf_free(&o);
    buf_free(&e);
    return ok;
}

/* refs/heads/<name> 是否真的存在（Python 的 show-ref --verify 那一档）。 */
static int branch_exists(const char *name) {
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, "refs/heads/");
    buf_puts(&spec, name);
    const char *args[] = { "show-ref", "--verify", "--quiet", buf_cstr(&spec), NULL };
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(args, NULL, 0, &o, &e);
    buf_free(&o);
    buf_free(&e);
    buf_free(&spec);
    return rc == 0;
}

/* ---------------------------------------------------------------- 快路径 */

/* 宽字符路径拼接（`a\b`）。 */
static wchar_t *wjoin(const wchar_t *a, const wchar_t *b) {
    size_t n = wcslen(a) + wcslen(b) + 2;
    wchar_t *o = xmalloc(n * sizeof(wchar_t));
    _snwprintf(o, n, L"%ls\\%ls", a, b);
    return o;
}

/* Python `gitrefs.gitdir()`：`.git` 是目录、没有 reftable、没有 GIT_DIR /
 * GIT_COMMON_DIR 改写布局 -> 那个目录；否则 NULL（回退去问 git）。
 * 与 Python 一样**不 spawn**，否则这条快路径就没有意义了。 */
static wchar_t *fast_gitdir(void) {
    if (getenv("GIT_DIR") || getenv("GIT_COMMON_DIR")) return NULL;
    const wchar_t *root = git_root();
    if (!root) return NULL;
    wchar_t *gd = wjoin(root, L".git");
    DWORD a = GetFileAttributesW(gd);
    if (a == INVALID_FILE_ATTRIBUTES || !(a & FILE_ATTRIBUTE_DIRECTORY)) {
        free(gd);
        return NULL;
    }
    wchar_t *rt = wjoin(gd, L"reftable");
    DWORD ra = GetFileAttributesW(rt);
    free(rt);
    if (ra != INVALID_FILE_ATTRIBUTES && (ra & FILE_ATTRIBUTE_DIRECTORY)) {
        free(gd);
        return NULL;
    }
    return gd;
}

static int utf8_ok(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned cp = 0;
        if (!next_cp((const unsigned char *)s, n, &i, &cp)) return 0;
    }
    return 1;
}

/* 整个文件读进内存；读不到 / 不是合法 UTF-8 -> NULL（Python 的 OSError /
 * UnicodeDecodeError 都退化成 None，也就是回退 show-ref）。 */
static char *read_text(const wchar_t *path, size_t *outn) {
    FILE *f = _wfopen(path, L"rb");
    if (!f) return NULL;
    buf b;
    buf_init(&b);
    char tmp[4096];
    size_t got;
    while ((got = fread(tmp, 1, sizeof(tmp), f)) > 0) buf_append(&b, tmp, got);
    fclose(f);
    if (!utf8_ok(b.p ? b.p : "", b.n)) {
        buf_free(&b);
        return NULL;
    }
    char *out = buf_cstr(&b);      /* 空文件也保证非 NULL 且 NUL 结尾 */
    *outn = b.n;
    return out;
}

/* Python `looks_like_ref`：`ref: <带斜杠的东西>` 或 40/64 位十六进制。 */
static int looks_like_ref(const char *s, size_t n) {
    size_t a = 0, b = n;
    while (a < b && py_space((unsigned char)s[a])) a++;
    while (b > a && py_space((unsigned char)s[b - 1])) b--;
    size_t len = b - a;
    const char *t = s + a;
    if (len >= 5 && !memcmp(t, "ref: ", 5)) {
        if (len <= 5) return 0;
        return memchr(t, '/', len) != NULL;
    }
    if (len != 40 && len != 64) return 0;
    for (size_t i = 0; i < len; i++) {
        char c = t[i];
        int hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                  (c >= 'A' && c <= 'F');
        if (!hex) return 0;
    }
    return 1;
}

/* Python `Repo._local_branch_exists`：True 当且仅当「从文件层面就能确信
 * refs/heads/<name> 存在且内容像一条 ref」。返回 0 表示**不确定**，调用方
 * 必须回退到 `show-ref --verify`（它才是权威）。 */
static int local_branch_exists(const char *name) {
    wchar_t *gd = fast_gitdir();
    if (!gd) return 0;
    int exists = 0;
    wchar_t *namew = u82w(name);
    if (!namew) { free(gd); return 0; }
    size_t cap = wcslen(gd) + wcslen(namew) + 16;
    wchar_t *p = xmalloc(cap * sizeof(wchar_t));
    _snwprintf(p, cap, L"%ls\\refs\\heads\\%ls", gd, namew);
    free(namew);

    DWORD a = GetFileAttributesW(p);
    if (a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY)) {
        /* 松散 ref：Python 看到文件就**直接返回**，不再看 packed-refs。 */
        size_t n = 0;
        char *txt = read_text(p, &n);
        if (txt) {
            exists = looks_like_ref(txt, n);
            free(txt);
        }
        free(p);
        free(gd);
        return exists;
    }
    free(p);

    wchar_t *packed = wjoin(gd, L"packed-refs");
    size_t pn = 0;
    char *txt = read_text(packed, &pn);
    free(packed);
    free(gd);
    if (!txt) return 0;

    buf target;
    buf_init(&target);
    buf_puts(&target, "refs/heads/");
    buf_puts(&target, name);
    size_t tlen = target.n;

    size_t pos = 0;
    while (pos < pn && !exists) {
        size_t end = pos;
        while (end < pn && txt[end] != '\n') end++;
        const char *line = txt + pos;
        size_t ln = end - pos;
        pos = end + 1;
        /* line.strip() */
        size_t la = 0, lb = ln;
        while (la < lb && py_space((unsigned char)line[la])) la++;
        while (lb > la && py_space((unsigned char)line[lb - 1])) lb--;
        line += la;
        ln = lb - la;
        if (ln == 0 || line[0] == '#' || line[0] == '^') continue;
        const char *sp = memchr(line, ' ', ln);
        if (!sp) continue;
        size_t shan = (size_t)(sp - line);
        const char *refname = sp + 1;
        size_t rn = ln - shan - 1;
        if (rn == tlen && !memcmp(refname, target.p, tlen))
            exists = looks_like_ref(line, shan);
    }
    buf_free(&target);
    free(txt);
    return exists;
}

/* ---------------------------------------------------------------- checkout */

static int do_checkout(SOCKET s, const jnode *b) {
    const jnode *nv = jget(b, "name");
    const char *name = nv ? jstr(nv) : "";
    size_t nlen = nv ? jstrlen(nv) : 0;
    if (!branch_name_ok(name, nlen)) {
        send_400(s, BRANCH_HINT);
        return 1;
    }
    /* 快路径：refs 就在文件里看得见就不 spawn show-ref（与 Python 同形，
     * 省一次 ~40ms）。看不见 -> show-ref 才是权威。 */
    if (!local_branch_exists(name) && !branch_exists(name)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "分支不存在,已拒绝 checkout:");
        buf_puts(&m, name);
        send_400(s, buf_cstr(&m));
        buf_free(&m);
        return 1;
    }
    const char *args[] = { "checkout", name, NULL };
    if (!run_or_400(s, args)) return 1;
    wsend_ok(s, b, NULL);
    return 1;
}

/* ---------------------------------------------------------------- branch */

static int do_branch(SOCKET s, const jnode *b) {
    const jnode *nv = jget(b, "name");
    const char *name = nv ? jstr(nv) : "";
    size_t nlen = nv ? jstrlen(nv) : 0;
    if (!branch_name_ok(name, nlen)) {
        send_400(s, BRANCH_HINT);
        return 1;
    }
    const char *args[] = { "checkout", "-b", name, NULL };
    if (!run_or_400(s, args)) return 1;
    wsend_ok(s, b, NULL);
    return 1;
}

/* ---------------------------------------------------------------- merge */

static int do_merge(SOCKET s, const jnode *b) {
    const jnode *nv = jget(b, "name");
    if (!nv || !jis_str(nv)) {
        send_400(s, MERGE_EMPTY_NAME);
        return 1;
    }
    const char *name = jstr(nv);
    size_t nlen = jstrlen(nv);
    /* Python 先 `name.strip()` 判空，再把名字交给 subprocess；带内嵌 NUL 的名字
     * 会在 subprocess 里抛 ValueError("embedded null character")（Windows 上的
     * 文案就是这个，不是 "null byte"），被 do_POST 兜成 400「请求参数不合法:...」。
     * 顺序照抄：先判空，再判 NUL。 */
    if (all_space_utf8(name, nlen)) {
        send_400(s, MERGE_EMPTY_NAME);
        return 1;
    }
    if (memchr(name, 0, nlen)) {
        send_400(s, "请求参数不合法:embedded null character");
        return 1;
    }

    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *margs[] = { "merge", "--no-edit", name, NULL };
    int rc = git_run(margs, NULL, 0, &o, &e);

    /* output = (stdout + stderr).strip() */
    char *so = py_text(o.p ? o.p : "", o.n);
    char *se = py_text(e.p ? e.p : "", e.n);
    buf cat;
    buf_init(&cat);
    buf_puts(&cat, so);
    buf_puts(&cat, se);
    free(so);
    free(se);
    py_strip(buf_cstr(&cat));

    buf o2, e2;
    buf_init(&o2);
    buf_init(&e2);
    const char *dargs[] = { "diff", "--name-only", "--diff-filter=U", NULL };
    (void)git_run(dargs, NULL, 0, &o2, &e2);          /* check=False */
    char *conf = py_text(o2.p ? o2.p : "", o2.n);
    py_strip(conf);

    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"ok\":");
    buf_puts(&r, rc == 0 ? "true" : "false");
    buf_puts(&r, ",\"output\":");
    json_str(&r, buf_cstr(&cat));
    buf_puts(&r, ",\"conflicted\":");
    json_str(&r, conf);
    buf_putc(&r, '}');
    http_send_json(s, 200, buf_cstr(&r), r.n);
    buf_free(&r);

    free(conf);
    buf_free(&cat);
    buf_free(&o);
    buf_free(&e);
    buf_free(&o2);
    buf_free(&e2);
    return 1;
}

/* ---------------------------------------------------------------- merge-abort */

static int do_merge_abort(SOCKET s, const jnode *b) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "merge", "--abort", NULL };
    (void)git_run(args, NULL, 0, &o, &e);             /* check=False */
    buf_free(&o);
    buf_free(&e);
    wsend_ok(s, b, NULL);
    return 1;
}

/* ---------------------------------------------------------------- reset */

static void send_needs_confirm(SOCKET s) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"code\":\"needs_confirmation\",\"op\":\"reset\",\"error\":");
    json_str(&o, RESET_NEEDS_CONFIRM);
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
}

/* Python 的 `b.get("ref") or "HEAD"`：falsy 一律退化成 HEAD。 */
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

static int do_reset(SOCKET s, const jnode *b) {
    /* 1) mode：必须显式给三态之一，缺省一律 400。 */
    const jnode *mv = jget(b, "mode");
    const char *mode = NULL;
    if (mv && jis_str(mv)) {
        const char *m = jstr(mv);
        size_t mn = jstrlen(mv);
        if (mn == 4 && !memcmp(m, "soft", 4)) mode = "soft";
        else if (mn == 5 && !memcmp(m, "mixed", 5)) mode = "mixed";
        else if (mn == 4 && !memcmp(m, "hard", 4)) mode = "hard";
    }
    if (!mode) {
        buf m;
        buf_init(&m);
        buf_puts(&m, MODE_HINT);
        repr_value(mv, &m);
        send_400(s, buf_cstr(&m));
        buf_free(&m);
        return 1;
    }

    /* 2) confirmed 必须字面 true（_bb：非 bool 一律 400）。 */
    int conf = 0;
    if (!wbody_bool(s, b, "confirmed", 0, &conf)) return 1;
    if (!conf) {
        send_needs_confirm(s);
        return 1;
    }

    /* 3) ref：`or "HEAD"` -> 必须字符串 -> check_ref_name。 */
    const jnode *rv = jget(b, "ref");
    const char *ref;
    size_t rlen;
    if (!jtruthy(rv)) {
        ref = "HEAD";
        rlen = 4;
    } else {
        if (!jis_str(rv)) {
            send_400(s, "ref 必须是字符串");
            return 1;
        }
        ref = jstr(rv);
        rlen = jstrlen(rv);
    }
    if (!ref_name_ok(ref, rlen)) {
        send_400(s, REF_HINT);
        return 1;
    }

    buf flag;
    buf_init(&flag);
    buf_puts(&flag, "--");
    buf_puts(&flag, mode);
    const char *args[] = { "reset", buf_cstr(&flag), ref, NULL };
    if (!run_or_400(s, args)) {
        buf_free(&flag);
        return 1;
    }
    buf_free(&flag);

    if (!strcmp(mode, "hard")) {
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        const char *cargs[] = { "clean", "-fd", NULL };
        (void)git_run(cargs, NULL, 0, &o, &e);        /* check=False */
        buf_free(&o);
        buf_free(&e);
    }

    wsend_ok(s, b, NULL);
    return 1;
}

/* ---------------------------------------------------------------- 分派 */

int write_more_post(const char *path, const char *query, const char *body,
                    size_t body_len, SOCKET s) {
    (void)query;
    int which = 0;
    if (!strcmp(path, "/api/checkout")) which = 1;
    else if (!strcmp(path, "/api/branch")) which = 2;
    else if (!strcmp(path, "/api/merge")) which = 3;
    else if (!strcmp(path, "/api/merge-abort")) which = 4;
    else if (!strcmp(path, "/api/reset")) which = 5;
    if (!which) return 0;

    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    switch (which) {
    case 1: do_checkout(s, b); break;
    case 2: do_branch(s, b); break;
    case 3: do_merge(s, b); break;
    case 4: do_merge_abort(s, b); break;
    default: do_reset(s, b); break;
    }
    jfree(b);
    return 1;
}
