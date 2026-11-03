/* write_basic.c —— M2 写面第一组：/api/stage /api/unstage /api/discard /api/commit
 *
 * 与 Python server.py 的对位（逐条照抄语义，不「差不多」）：
 *   /api/stage    -> Repo.stage(as_file_list(b.get("files")))
 *   /api/unstage  -> Repo.unstage(...)
 *   /api/discard  -> _bb(confirmed) 必须 is True，否则 400 needs_confirmation
 *   /api/commit   -> commit_and_status(msg or "")，响应必须带 output
 *
 * 关键点（都在 Python 那边有实测记录，这里不重复理由）：
 *   * files 先**全部** resolve（rel_in_root），再按「存在 / 是链接 / 已跟踪」过滤；
 *     一个非法路径就让整个请求 400，哪怕它在列表末尾。
 *   * 路径一律用 resolve 之后的**规范化相对路径**喂 git，绝不喂请求原串
 *     （原串会被当 pathspec magic，且大小写对不上索引）。
 *   * 路径列表走 stdin（--pathspec-from-file=- --pathspec-file-nul），不拼命令行。
 *   * discard 是破坏性的：全量校验 -> checkout 恢复 -> 握祖先链 -> 句柄判链 +
 *     重新 resolve -> 删；目录里含已跟踪文件整条拒绝。
 *   * commit 不是 `git add -A`：自己枚举工作区（不穿联接）、只交逐个字面文件、
 *     add 之后核对索引（多出来的撤下并拒绝）。
 *   * 任何失败都是 400，绝无 500。
 */
#include "routes.h"

#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "bodyjson.h"
#include "git.h"
#include "http.h"
#include "indexfile.h"
#include "json.h"
#include "paths.h"
#include "status.h"
#include "util.h"
#include "writeutil.h"

/* ================================================================ 字符串表 */

typedef struct { char **v; size_t n, cap; } slist;

static void sl_push(slist *l, char *own) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 16;
        l->v = xrealloc(l->v, l->cap * sizeof(char *));
    }
    l->v[l->n++] = own;
}

static void sl_free(slist *l) {
    for (size_t i = 0; i < l->n; i++) free(l->v[i]);
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

static int cmp_str(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

static void sl_sort(slist *l) {
    if (l->n > 1) qsort(l->v, l->n, sizeof(char *), cmp_str);
}

/* ---------------------------------------------------------------- 哈希集合 */

typedef struct { char **slots; size_t cap, n; } skset;

static unsigned long hstr(const char *s) {
    unsigned long h = 2166136261u;
    while (*s) { h ^= (unsigned char)*s++; h *= 16777619u; }
    return h;
}

static void ss_rehash(skset *s, size_t cap) {
    char **old = s->slots;
    size_t oldcap = s->cap;
    s->slots = xmalloc(cap * sizeof(char *));
    memset(s->slots, 0, cap * sizeof(char *));
    s->cap = cap;
    s->n = 0;
    for (size_t i = 0; i < oldcap; i++) {
        if (!old[i]) continue;
        size_t j = hstr(old[i]) & (cap - 1);
        while (s->slots[j]) j = (j + 1) & (cap - 1);
        s->slots[j] = old[i];
        s->n++;
    }
    free(old);
}

static void ss_init(skset *s) {
    s->cap = 256;
    s->n = 0;
    s->slots = xmalloc(s->cap * sizeof(char *));
    memset(s->slots, 0, s->cap * sizeof(char *));
}

static void ss_free(skset *s) {
    for (size_t i = 0; i < s->cap; i++) free(s->slots[i]);
    free(s->slots);
    s->slots = NULL;
    s->cap = s->n = 0;
}

static int ss_has(const skset *s, const char *k) {
    if (!s->cap) return 0;
    size_t j = hstr(k) & (s->cap - 1);
    while (s->slots[j]) {
        if (!strcmp(s->slots[j], k)) return 1;
        j = (j + 1) & (s->cap - 1);
    }
    return 0;
}

/* 已存在返回 0（不接管 own）；插入返回 1。 */
static int ss_add(skset *s, char *own) {
    if (s->n * 10 >= s->cap * 7) ss_rehash(s, s->cap * 2);
    size_t j = hstr(own) & (s->cap - 1);
    while (s->slots[j]) {
        if (!strcmp(s->slots[j], own)) return 0;
        j = (j + 1) & (s->cap - 1);
    }
    s->slots[j] = own;
    s->n++;
    return 1;
}

/* ================================================================ 路径 */

static char *mk_err(const char *fmt, const char *arg) {
    buf b;
    buf_init(&b);
    buf_printf(&b, fmt, arg ? arg : "");
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

static char *rel_real(const char *rel);

/* Python 的 `Path` 会把这些输入解析成 root 自己（'' / '.' / './' / 'a/..'）。 */
static int lexically_root(const char *s) {
    long depth = 0;
    const char *p = s;
    while (1) {
        const char *e = p;
        while (*e && *e != '/' && *e != '\\') e++;
        size_t n = (size_t)(e - p);
        if (n == 0 || (n == 1 && p[0] == '.')) {
            /* 空段 / "."：不动 */
        } else if (n == 2 && p[0] == '.' && p[1] == '.') {
            if (depth == 0) return 0;
            depth--;
        } else {
            depth++;
        }
        if (!*e) break;
        p = e + 1;
    }
    return depth == 0;
}

/* root\rel 的宽字符绝对路径（'/' 转 '\'）。rel 为空时就是 root 自己。 */
static wchar_t *abs_win(const char *rel) {
    const wchar_t *root = git_root();
    if (!rel || !*rel) return _wcsdup(root);
    buf w;
    buf_init(&w);
    buf_append(&w, root, wcslen(root) * sizeof(wchar_t));
    wchar_t sep = L'\\';
    buf_append(&w, &sep, sizeof(wchar_t));
    wchar_t *rw = u82w(rel);
    if (rw) {
        for (wchar_t *p = rw; *p; p++)
            if (*p == L'/') *p = L'\\';
        buf_append(&w, rw, wcslen(rw) * sizeof(wchar_t));
        free(rw);
    }
    wchar_t z = 0;
    buf_append(&w, &z, sizeof(wchar_t));
    return (wchar_t *)w.p;
}

/* 句柄级真实路径（会穿过符号链接/联接，且是磁盘上的真实大小写）。 */
static wchar_t *final_path_of(const wchar_t *path) {
    HANDLE h = CreateFileW(path, 0,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED);
    wchar_t *out = NULL;
    if (need) {
        out = xmalloc(((size_t)need + 2) * sizeof(wchar_t));
        DWORD got = GetFinalPathNameByHandleW(h, out, need + 1, FILE_NAME_NORMALIZED);
        if (!got) { free(out); out = NULL; }
    }
    CloseHandle(h);
    return out;
}

static const wchar_t *strip_ns(const wchar_t *p) {
    if (p && p[0] == L'\\' && p[1] == L'\\' && p[2] == L'?' && p[3] == L'\\') return p + 4;
    return p;
}

/* 把 rel 换成 Python resolve() 会给的那串：最长的已存在祖先取真实路径
 * （真实大小写 + 跟随符号链接），剩下不存在的部分原样拼回去。 */
static char *rel_real(const char *rel) {
    char *work = xstrdup(rel);
    size_t nseg = 0;
    char **seg = split_inplace(work, '/', &nseg);
    const wchar_t *root = git_root();
    size_t rl = wcslen(root);
    char *result = NULL;

    for (size_t keep = nseg + 1; keep-- > 0;) {
        buf w;
        buf_init(&w);
        buf_append(&w, root, rl * sizeof(wchar_t));
        for (size_t i = 0; i < keep; i++) {
            wchar_t sep = L'\\';
            buf_append(&w, &sep, sizeof(wchar_t));
            wchar_t *sw = u82w(seg[i]);
            if (sw) { buf_append(&w, sw, wcslen(sw) * sizeof(wchar_t)); free(sw); }
        }
        wchar_t z = 0;
        buf_append(&w, &z, sizeof(wchar_t));
        wchar_t *real = final_path_of((wchar_t *)w.p);
        buf_free(&w);
        if (!real) continue;
        const wchar_t *rp = strip_ns(real);
        if (_wcsnicmp(rp, root, rl) != 0 || (rp[rl] != L'\\' && rp[rl] != 0)) {
            free(real);
            continue;
        }
        wchar_t *tail = _wcsdup(rp[rl] ? rp + rl + 1 : rp + rl);
        for (wchar_t *q = tail; *q; q++)
            if (*q == L'\\') *q = L'/';
        char *t8 = w2u8(tail);
        free(tail);
        free(real);
        buf o;
        buf_init(&o);
        buf_puts(&o, t8 ? t8 : "");
        free(t8);
        for (size_t i = keep; i < nseg; i++) {
            if (o.n) buf_putc(&o, '/');
            buf_puts(&o, seg[i]);
        }
        result = xstrdup(buf_cstr(&o));
        buf_free(&o);
        break;
    }
    free(seg);
    free(work);
    if (!result) result = xstrdup(rel);
    return result;
}

/* 请求里的路径 -> 仓内相对路径（Python 的 resolve_in_root().relative_to(root)） */
static char *resolve_rel(const char *input, char **err) {
    if (err) *err = NULL;
    if (lexically_root(input)) {
        if (err) *err = mk_err("路径指向仓库根目录本身,已拒绝:%s", input);
        return NULL;
    }
    char *rel0 = path_rel_in_root(input, err);
    if (!rel0) return NULL;
    size_t n = strlen(rel0);
    while (n && rel0[n - 1] == '/') rel0[--n] = 0;
    char *rel = rel_real(rel0);
    free(rel0);
    if (!rel[0]) {
        free(rel);
        if (err) *err = mk_err("路径指向仓库根目录本身,已拒绝:%s", input);
        return NULL;
    }
    if (_strnicmp(rel, ".git", 4) == 0 && (rel[4] == 0 || rel[4] == '/')) {
        free(rel);
        if (err) *err = mk_err("不允许操作 .git,已拒绝:%s", input);
        return NULL;
    }
    return rel;
}

/* ================================================================ 文件系统 */

static DWORD attrs_of(const wchar_t *p) { return GetFileAttributesW(p); }

static int exists_w(const wchar_t *p) { return attrs_of(p) != INVALID_FILE_ATTRIBUTES; }

static int is_reparse_w(const wchar_t *p) {
    DWORD a = attrs_of(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_REPARSE_POINT);
}

/* Python 的 abs_p.is_dir()：跟随链接。 */
static int is_dir_w(const wchar_t *p) {
    DWORD a = attrs_of(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

/* 真目录（目录项自己不是 reparse point） */
static int is_plain_dir_w(const wchar_t *p) {
    DWORD a = attrs_of(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) &&
           !(a & FILE_ATTRIBUTE_REPARSE_POINT);
}

static int inside_root_w(const wchar_t *p) {
    const wchar_t *q = strip_ns(p);
    const wchar_t *root = git_root();
    size_t rl = wcslen(root);
    if (_wcsnicmp(q, root, rl) != 0) return 0;
    return q[rl] == L'\\' || q[rl] == 0;
}

/* 绝对路径 -> 仓内相对路径（posix，UTF-8）。不在仓内返回 NULL。 */
static char *rel_from_abs(const wchar_t *abs) {
    const wchar_t *q = strip_ns(abs);
    const wchar_t *root = git_root();
    size_t rl = wcslen(root);
    if (_wcsnicmp(q, root, rl) != 0) return NULL;
    const wchar_t *tail = q[rl] ? q + rl + 1 : q + rl;
    wchar_t *tmp = _wcsdup(tail);
    for (wchar_t *p = tmp; *p; p++)
        if (*p == L'\\') *p = L'/';
    char *out = w2u8(tmp);
    free(tmp);
    return out;
}

/* ---------------------------------------------------------------- 握目录 */

static HANDLE pin_open(const wchar_t *path) {
    return CreateFileW(path, GENERIC_READ,
                       FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                       FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
}

/* Python 的 _try_pin_dir：非 root 握不住先重试（瞬时占用很常见）。 */
static HANDLE pin_open_retry(const wchar_t *path, int must) {
    HANDLE h = INVALID_HANDLE_VALUE;
    for (int i = 0; i < 3; i++) {
        h = pin_open(path);
        if (h != INVALID_HANDLE_VALUE) return h;
        if (must) return h;
        if (i < 2) Sleep(20);
    }
    return h;
}

static char *pin_fail_msg(const wchar_t *path) {
    DWORD e = GetLastError();
    buf m;
    buf_init(&m);
    char *au = w2u8(path);
    buf_printf(&m, "无法握住目录 %s(WinError %lu),已拒绝动手 —— "
                   "握不住就没法保证删除不越界", au ? au : "", (unsigned long)e);
    free(au);
    char *r = xstrdup(buf_cstr(&m));
    buf_free(&m);
    return r;
}

static int pin_is_reparse(HANDLE h) {
    FILE_ATTRIBUTE_TAG_INFO info;
    if (!GetFileInformationByHandleEx(h, FileAttributeTagInfo, &info, sizeof(info))) return 0;
    return (info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
}

typedef struct {
    HANDLE *h;
    char  **rel;       /* 仓内相对路径（posix）；root 用 "." */
    size_t  n, cap;
} pinlist;

static void pl_free(pinlist *p) {
    for (size_t i = 0; i < p->n; i++) {
        if (p->h[i] != INVALID_HANDLE_VALUE) CloseHandle(p->h[i]);
        free(p->rel[i]);
    }
    free(p->h);
    free(p->rel);
    p->h = NULL;
    p->rel = NULL;
    p->n = p->cap = 0;
}

static void pl_add(pinlist *p, HANDLE h, char *rel) {
    if (p->n == p->cap) {
        p->cap = p->cap ? p->cap * 2 : 8;
        p->h = xrealloc(p->h, p->cap * sizeof(HANDLE));
        p->rel = xrealloc(p->rel, p->cap * sizeof(char *));
    }
    p->h[p->n] = h;
    p->rel[p->n] = rel;
    p->n++;
}

/* 握住 rel 的祖先链（root -> 最深），不含 rel 自己。返回 NULL 或错误文案。 */
static char *pin_ancestors(pinlist *pl, const char *rel) {
    char *work = xstrdup(rel);
    size_t nseg = 0;
    char **seg = split_inplace(work, '/', &nseg);
    char *err = NULL;

    /* 逐级：root, seg[0], seg[0]/seg[1], ... seg[0..n-2] */
    for (size_t depth = 0; depth < nseg; depth++) {
        /* depth = 已经吃掉的段数；目录 = root + 前 depth 段 */
        buf r;
        buf_init(&r);
        for (size_t i = 0; i < depth; i++) {
            if (i) buf_putc(&r, '/');
            buf_puts(&r, seg[i]);
        }
        char *drel = xstrdup(buf_cstr(&r));
        buf_free(&r);
        /* 去重：同一条链只握一次 */
        int seen = 0;
        for (size_t i = 0; i < pl->n; i++)
            if (!_stricmp(pl->rel[i], drel[0] ? drel : ".")) { seen = 1; break; }
        if (seen) { free(drel); continue; }

        wchar_t *abs = abs_win(drel[0] ? drel : "");
        HANDLE h = pin_open_retry(abs, 0);
        if (h == INVALID_HANDLE_VALUE) {
            err = pin_fail_msg(abs);
            free(abs);
            free(drel);
            break;
        }
        pl_add(pl, h, drel[0] ? xstrdup(drel) : xstrdup("."));
        free(abs);
        free(drel);
    }
    free(seg);
    free(work);
    return err;
}

/* ================================================================ git 调用 */

static char *strip_dup(const char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) n--;
    return xstrndup(s, n);
}

/* (p.stderr or p.stdout or "git 失败").strip() */
static char *git_fail_text(buf *out, buf *err) {
    if (err && err->n) return strip_dup(buf_cstr(err));
    if (out && out->n) return strip_dup(buf_cstr(out));
    return xstrdup("git 失败");
}

static void nul_split(const buf *b, slist *out) {
    if (!b->p) return;
    const char *s = b->p;
    size_t pos = 0;
    while (pos < b->n) {
        const char *e = s + pos;
        size_t ll = strlen(e);
        pos += ll + 1;
        if (!ll) continue;
        sl_push(out, xstrndup(e, ll));
    }
}

static char *normcase_dup(const char *s) {
    char *d = xstrdup(s);
    for (char *p = d; *p; p++) {
        if (*p >= 'A' && *p <= 'Z') *p = (char)(*p - 'A' + 'a');
        else if (*p == '/') *p = '\\';
    }
    return d;
}

/* 把一批相对路径喂给 git（stdin，不拼命令行）。返回退出码。 */
static int run_paths(const char *const head[], const slist *rels, buf *out, buf *err) {
    buf in;
    buf_init(&in);
    for (size_t i = 0; i < rels->n; i++) {
        buf_puts(&in, rels->v[i]);
        buf_putc(&in, '\0');
    }
    const char *args[16];
    int k = 0;
    args[k++] = "--literal-pathspecs";
    for (const char *const *h = head; *h; h++) args[k++] = *h;
    args[k++] = "--pathspec-from-file=-";
    args[k++] = "--pathspec-file-nul";
    args[k] = NULL;
    int rc = git_run(args, in.p ? in.p : "", in.n, out, err);
    buf_free(&in);
    return rc;
}

/* Python 的 _tracked_for_filter()：先读 .git/index；不认识才 spawn ls-files。 */
static void tracked_set(skset *set, slist *ordered) {
    idx_names ix;
    if (idx_read(&ix)) {
        for (size_t i = 0; i < ix.n; i++) {
            char *cp = xstrdup(ix.paths[i]);
            if (ss_add(set, cp)) {
                if (ordered) sl_push(ordered, xstrdup(cp));
            } else {
                free(cp);
            }
        }
        idx_free(&ix);
        return;
    }
    const char *args[] = { "ls-files", "-z", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    (void)git_run(args, NULL, 0, &out, &er);
    buf_free(&er);
    slist tmp = {0};
    nul_split(&out, &tmp);
    buf_free(&out);
    for (size_t i = 0; i < tmp.n; i++) {
        if (ss_add(set, tmp.v[i])) {
            if (ordered) sl_push(ordered, xstrdup(tmp.v[i]));
        } else {
            free(tmp.v[i]);
        }
    }
    free(tmp.v);
}

/* ================================================================ files 取值 */

/* as_file_list + resolve_in_root：两遍（先全部验类型，再逐个 resolve）。 */
static int get_files(const jnode *b, SOCKET s, slist *out) {
    const jnode *v = jget(b, "files");
    if (!v || bjtype(v) == BJ_NULL) return 1;
    if (bjtype(v) != BJ_ARR) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "files 必须是字符串数组,收到 ");
        buf_puts(&m, jtype_name(v));
        http_send_err(s, 400, buf_cstr(&m), NULL);
        buf_free(&m);
        return 0;
    }
    size_t cnt = jlen(v);
    for (size_t i = 0; i < cnt; i++) {
        if (!jis_str(jat(v, i))) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "files 里每一项都必须是字符串,收到 ");
            buf_puts(&m, jtype_name(jat(v, i)));
            http_send_err(s, 400, buf_cstr(&m), NULL);
            buf_free(&m);
            return 0;
        }
    }
    for (size_t i = 0; i < cnt; i++) {
        const jnode *it = jat(v, i);
        const char *raw = jstr(it);
        size_t rn = jstrlen(it);
        /* Python resolve_in_root：NUL 必须在碰 Path 之前拒掉（否则静默截断） */
        if (memchr(raw, 0, rn)) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "路径里不能包含 NUL 字符,已拒绝:");
            w_repr(it, &m);
            http_send_err(s, 400, buf_cstr(&m), NULL);
            buf_free(&m);
            return 0;
        }
        char *err = NULL;
        char *rel = resolve_rel(raw, &err);
        if (!rel) {
            http_send_err(s, 400, err ? err : "路径不合法", NULL);
            free(err);
            return 0;
        }
        free(err);
        sl_push(out, rel);
    }
    return 1;
}

/* ================================================================ stage/unstage */

static void do_stage_like(const jnode *b, SOCKET s, int unstage) {
    slist rels = {0};
    if (!get_files(b, s, &rels)) { sl_free(&rels); return; }
    if (!rels.n) { sl_free(&rels); wsend_ok(s, b, NULL); return; }

    skset tracked;
    ss_init(&tracked);
    tracked_set(&tracked, NULL);

    /* _live_paths：既不在工作区、也没被跟踪的路径直接丢掉（git 会 rc=128）。 */
    slist live = {0};
    for (size_t i = 0; i < rels.n; i++) {
        wchar_t *abs = abs_win(rels.v[i]);
        int keep = exists_w(abs) || is_reparse_w(abs) || ss_has(&tracked, rels.v[i]);
        free(abs);
        if (keep) sl_push(&live, xstrdup(rels.v[i]));
    }

    if (live.n) {
        const char *head_add[] = { "add", NULL };
        const char *head_reset[] = { "reset", "HEAD", NULL };
        buf out, er;
        buf_init(&out);
        buf_init(&er);
        int rc = run_paths(unstage ? head_reset : head_add, &live, &out, &er);
        if (rc != 0) {
            char *msg = git_fail_text(&out, &er);
            http_send_err(s, 400, msg, NULL);
            free(msg);
            buf_free(&out);
            buf_free(&er);
            sl_free(&live);
            ss_free(&tracked);
            sl_free(&rels);
            return;
        }
        buf_free(&out);
        buf_free(&er);
    }

    sl_free(&live);
    ss_free(&tracked);
    sl_free(&rels);
    wsend_ok(s, b, NULL);
}

/* ================================================================ discard */

typedef struct { char *rel; int is_dir; } wtarget;

static char *delete_tree(const char *rel);

/* 删一棵目录树：握自己 -> 判链 -> 逐项删（联接一律拒）-> 松手 -> rmdir。 */
static char *delete_tree(const char *rel) {
    wchar_t *abs = abs_win(rel);
    HANDLE h = pin_open(abs);
    if (h == INVALID_HANDLE_VALUE) {
        char *r = pin_fail_msg(abs);
        free(abs);
        return r;
    }
    if (pin_is_reparse(h)) {
        CloseHandle(h);
        free(abs);
        return mk_err("路径上有目录联接/符号链接,已拒绝动手:%s", rel);
    }

    buf pat;
    buf_init(&pat);
    buf_append(&pat, abs, wcslen(abs) * sizeof(wchar_t));
    buf_append(&pat, L"\\*", 2 * sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&pat, &z, sizeof(wchar_t));

    WIN32_FIND_DATAW fd;
    HANDLE fh = FindFirstFileW((wchar_t *)pat.p, &fd);
    char *err = NULL;
    if (fh != INVALID_HANDLE_VALUE) {
        /* Python: for name in sorted(os.listdir(...)) —— 先收名字再排序 */
        wchar_t **names = NULL;
        size_t nn = 0, nc = 0;
        do {
            if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
            if (nn == nc) { nc = nc ? nc * 2 : 16; names = xrealloc(names, nc * sizeof(wchar_t *)); }
            names[nn++] = _wcsdup(fd.cFileName);
        } while (FindNextFileW(fh, &fd));
        FindClose(fh);
        /* 插入排序（目录项通常不多） */
        for (size_t i = 1; i < nn; i++) {
            wchar_t *k = names[i];
            size_t j = i;
            while (j > 0 && wcscmp(names[j - 1], k) > 0) { names[j] = names[j - 1]; j--; }
            names[j] = k;
        }
        for (size_t i = 0; i < nn && !err; i++) {
            buf child;
            buf_init(&child);
            buf_append(&child, abs, wcslen(abs) * sizeof(wchar_t));
            buf_append(&child, L"\\", sizeof(wchar_t));
            buf_append(&child, names[i], wcslen(names[i]) * sizeof(wchar_t));
            wchar_t cz = 0;
            buf_append(&child, &cz, sizeof(wchar_t));
            wchar_t *cw = (wchar_t *)child.p;

            if (is_reparse_w(cw)) {
                char *cu = w2u8(cw);
                buf m;
                buf_init(&m);
                buf_printf(&m, "树里有目录联接/符号链接,已拒绝删除:%s", cu ? cu : "");
                free(cu);
                err = xstrdup(buf_cstr(&m));
                buf_free(&m);
            } else if (is_plain_dir_w(cw)) {
                char *n8 = w2u8(names[i]);
                buf rr;
                buf_init(&rr);
                buf_puts(&rr, rel);
                buf_putc(&rr, '/');
                buf_puts(&rr, n8 ? n8 : "");
                free(n8);
                err = delete_tree(buf_cstr(&rr));
                buf_free(&rr);
            } else {
                if (!DeleteFileW(cw)) {
                    DWORD e = GetLastError();
                    char *cu = w2u8(cw);
                    buf m;
                    buf_init(&m);
                    buf_printf(&m, "删除失败:%s(%lu)", cu ? cu : "", (unsigned long)e);
                    free(cu);
                    err = xstrdup(buf_cstr(&m));
                    buf_free(&m);
                }
            }
            buf_free(&child);
        }
        for (size_t i = 0; i < nn; i++) free(names[i]);
        free(names);
    }
    buf_free(&pat);

    CloseHandle(h);            /* 先松手再 rmdir（被握住的目录项没有 DELETE 权限） */
    if (!err) {
        if (!RemoveDirectoryW(abs)) {
            DWORD e = GetLastError();
            buf m;
            buf_init(&m);
            char *au = w2u8(abs);
            buf_printf(&m, "删除目录失败:%s(%lu)", au ? au : "", (unsigned long)e);
            free(au);
            err = xstrdup(buf_cstr(&m));
            buf_free(&m);
        }
    }
    free(abs);
    return err;
}

static void do_discard(const jnode *b, SOCKET s) {
    int conf = 0;
    if (!wbody_bool(s, b, "confirmed", 0, &conf)) return;
    if (conf != 1) {
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":false,\"code\":\"needs_confirmation\","
                     "\"op\":\"discard\",\"error\":");
        json_str(&o, "discard 是破坏性操作,需要 confirmed=true;"
                     "先 GET /api/discard/plan 看清要丢什么。");
        buf_putc(&o, '}');
        http_send_json(s, 400, buf_cstr(&o), o.n);
        buf_free(&o);
        return;
    }

    slist rels = {0};
    if (!get_files(b, s, &rels)) { sl_free(&rels); return; }
    if (!rels.n) { sl_free(&rels); wsend_ok(s, b, NULL); return; }

    skset tracked;
    ss_init(&tracked);
    slist tracked_ordered = {0};
    tracked_set(&tracked, &tracked_ordered);
    sl_sort(&tracked_ordered);

    slist restore = {0};
    wtarget *wipe = NULL;
    size_t wn = 0, wc = 0;
    char *fatal = NULL;

    for (size_t i = 0; i < rels.n && !fatal; i++) {
        const char *rel = rels.v[i];
        if (ss_has(&tracked, rel)) {
            sl_push(&restore, xstrdup(rel));
            continue;
        }
        wchar_t *abs = abs_win(rel);
        int isdir = is_dir_w(abs);
        free(abs);
        if (isdir) {
            /* 目录里可能有已跟踪文件 —— 整目录删掉会把它们从工作区抹掉 */
            buf pre;
            buf_init(&pre);
            buf_puts(&pre, rel);
            buf_putc(&pre, '/');
            size_t plen = pre.n;
            slist inner = {0};
            for (size_t k = 0; k < tracked_ordered.n; k++)
                if (!strncmp(tracked_ordered.v[k], buf_cstr(&pre), plen))
                    sl_push(&inner, xstrdup(tracked_ordered.v[k]));
            buf_free(&pre);
            if (inner.n) {
                sl_sort(&inner);
                buf m;
                buf_init(&m);
                buf_printf(&m, "目录内含已跟踪文件,拒绝整目录丢弃:%s(%d 个,例如 %s)",
                           rel, (int)inner.n, inner.v[0]);
                fatal = xstrdup(buf_cstr(&m));
                buf_free(&m);
                sl_free(&inner);
                break;
            }
            sl_free(&inner);
        }
        if (wn == wc) {
            wc = wc ? wc * 2 : 8;
            wipe = xrealloc(wipe, wc * sizeof(wtarget));
        }
        wipe[wn].rel = xstrdup(rel);
        wipe[wn].is_dir = isdir;
        wn++;
    }

    if (!fatal && restore.n) {
        const char *head[] = { "checkout", NULL };
        buf out, er;
        buf_init(&out);
        buf_init(&er);
        int rc = run_paths(head, &restore, &out, &er);
        if (rc != 0) {
            fatal = git_fail_text(&out, &er);
        }
        buf_free(&out);
        buf_free(&er);
    }

    pinlist pins = {0};
    if (!fatal && wn) {
        /* 握祖先链 -> 句柄判链 -> 重新 resolve -> 删 */
        for (size_t i = 0; i < wn && !fatal; i++)
            fatal = pin_ancestors(&pins, wipe[i].rel);
        for (size_t i = 0; i < pins.n && !fatal; i++) {
            if (pin_is_reparse(pins.h[i]))
                fatal = mk_err("路径上有目录联接/符号链接,已拒绝动手:%s", pins.rel[i]);
        }
        for (size_t i = 0; i < wn && !fatal; i++) {
            char *err = NULL;
            char *again = resolve_rel(wipe[i].rel, &err);
            free(err);
            if (!again) {
                fatal = mk_err("路径在校验与动手之间被换掉了,已拒绝:%s", wipe[i].rel);
                break;
            }
            if (_stricmp(again, wipe[i].rel) != 0) {
                free(again);
                fatal = mk_err("路径在校验与动手之间被换掉了,已拒绝:%s", wipe[i].rel);
                break;
            }
            free(again);
        }
        for (size_t i = 0; i < wn && !fatal; i++) {
            if (wipe[i].is_dir) {
                fatal = delete_tree(wipe[i].rel);
            } else {
                wchar_t *abs = abs_win(wipe[i].rel);
                if (exists_w(abs) || is_reparse_w(abs)) {
                    if (!DeleteFileW(abs)) {
                        DWORD e = GetLastError();
                        buf m;
                        buf_init(&m);
                        char *au = w2u8(abs);
                        buf_printf(&m, "删除失败:%s(%lu)", au ? au : "", (unsigned long)e);
                        free(au);
                        fatal = xstrdup(buf_cstr(&m));
                        buf_free(&m);
                    }
                }
                free(abs);
            }
        }
    }
    pl_free(&pins);

    for (size_t i = 0; i < wn; i++) free(wipe[i].rel);
    free(wipe);
    sl_free(&restore);
    sl_free(&tracked_ordered);
    ss_free(&tracked);
    sl_free(&rels);

    if (fatal) {
        http_send_err(s, 400, fatal, NULL);
        free(fatal);
        return;
    }
    wsend_ok(s, b, NULL);
}

/* ================================================================ commit */

/* ---- escaping_links：仓里指向仓外的联接/符号链接（拒收门） ---- */

typedef struct { char *path; char *target; } leak_t;

static void leaks_scan(const wchar_t *dir, leak_t *found, size_t *nf, size_t limit) {
    if (*nf >= limit) return;
    buf pat;
    buf_init(&pat);
    buf_append(&pat, dir, wcslen(dir) * sizeof(wchar_t));
    buf_append(&pat, L"\\*", 2 * sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&pat, &z, sizeof(wchar_t));

    WIN32_FIND_DATAW fd;
    HANDLE fh = FindFirstFileW((wchar_t *)pat.p, &fd);
    if (fh == INVALID_HANDLE_VALUE) { buf_free(&pat); return; }

    /* 先收本层，再决定往哪走（Python 的 scandir 顺序 + stack，这里顺序无关） */
    wchar_t **dirs = NULL;
    size_t nd = 0, dc = 0;
    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
        if (!wcscmp(fd.cFileName, L".git")) continue;
        buf child;
        buf_init(&child);
        buf_append(&child, dir, wcslen(dir) * sizeof(wchar_t));
        buf_append(&child, L"\\", sizeof(wchar_t));
        buf_append(&child, fd.cFileName, wcslen(fd.cFileName) * sizeof(wchar_t));
        wchar_t cz = 0;
        buf_append(&child, &cz, sizeof(wchar_t));
        wchar_t *cw = (wchar_t *)child.p;

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
            wchar_t *real = final_path_of(cw);
            if (real) {
                if (!inside_root_w(real) && *nf < limit) {
                    char *cu = w2u8(cw);
                    char *ru = w2u8(strip_ns(real));
                    found[*nf].path = cu ? cu : xstrdup("");
                    found[*nf].target = ru ? ru : xstrdup("");
                    (*nf)++;
                }
                free(real);
            }
            buf_free(&child);
            continue;                       /* 联接一律不再往下走 */
        }
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (nd == dc) { dc = dc ? dc * 2 : 16; dirs = xrealloc(dirs, dc * sizeof(wchar_t *)); }
            dirs[nd++] = _wcsdup(cw);
        }
        buf_free(&child);
    } while (FindNextFileW(fh, &fd));
    FindClose(fh);
    buf_free(&pat);

    for (size_t i = 0; i < nd && *nf < limit; i++) leaks_scan(dirs[i], found, nf, limit);
    for (size_t i = 0; i < nd; i++) free(dirs[i]);
    free(dirs);
}

static char *escaping_links_error(void) {
    leak_t found[16];
    size_t nf = 0;
    leaks_scan(git_root(), found, &nf, 16);
    if (!nf) return NULL;

    buf names;
    buf_init(&names);
    for (size_t i = 0; i < nf && i < 8; i++) {
        if (i) buf_puts(&names, ", ");
        buf_puts(&names, found[i].path);
        buf_puts(&names, " -> ");
        buf_puts(&names, found[i].target);
    }
    buf m;
    buf_init(&m);
    buf_printf(&m,
               "仓里有指向仓外的链接(%s),已拒绝提交 —— 它会把仓外的文件收进"
               "索引、再写进历史, 而删掉链接也删不掉历史。"
               "请先把它移出工作区, 或把它移进仓内。共发现 %d 个。",
               buf_cstr(&names), (int)nf);
    char *out = xstrdup(buf_cstr(&m));
    buf_free(&m);
    buf_free(&names);
    for (size_t i = 0; i < nf; i++) {
        free(found[i].path);
        free(found[i].target);
    }
    return out;
}

/* ---- _walk_add_scope：自己枚举，只收普通文件，沿途握目录 ---- */

typedef struct {
    slist   *rels;
    slist   *unpinned;
    HANDLE  *pins;
    wchar_t **pinpath;
    size_t   np, pc;
    char    *err;
} walkctx;

static void walkctx_free(walkctx *c) {
    for (size_t i = 0; i < c->np; i++) CloseHandle(c->pins[i]);
    for (size_t i = 0; i < c->np; i++) free(c->pinpath[i]);
    free(c->pins);
    free(c->pinpath);
    c->pins = NULL;
    c->pinpath = NULL;
    c->np = c->pc = 0;
}

static wchar_t *child_of(const wchar_t *dir, const wchar_t *name) {
    buf b;
    buf_init(&b);
    buf_append(&b, dir, wcslen(dir) * sizeof(wchar_t));
    buf_append(&b, L"\\", sizeof(wchar_t));
    buf_append(&b, name, wcslen(name) * sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&b, &z, sizeof(wchar_t));
    return (wchar_t *)b.p;
}

static int walk_dir(walkctx *c, const char *rel, int is_root) {
    wchar_t *abs = abs_win(rel);
    HANDLE h = pin_open_retry(abs, is_root);
    if (h == INVALID_HANDLE_VALUE) {
        if (is_root) {
            c->err = pin_fail_msg(abs);
            free(abs);
            return 0;
        }
        /* 握不住 = 这棵子树不收（跳过是安全，不是绕过） */
        char *au = w2u8(abs);
        sl_push(c->unpinned, au ? au : xstrdup(""));
        free(abs);
        return 1;
    }
    if (c->np == c->pc) {
        c->pc = c->pc ? c->pc * 2 : 16;
        c->pins = xrealloc(c->pins, c->pc * sizeof(HANDLE));
        c->pinpath = xrealloc(c->pinpath, c->pc * sizeof(wchar_t *));
    }
    c->pins[c->np] = h;
    c->pinpath[c->np] = _wcsdup(abs);
    c->np++;
    if (pin_is_reparse(h)) { free(abs); return 1; }   /* 自己就是联接：不往里走 */

    buf pat;
    buf_init(&pat);
    buf_append(&pat, abs, wcslen(abs) * sizeof(wchar_t));
    buf_append(&pat, L"\\*", 2 * sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&pat, &z, sizeof(wchar_t));

    WIN32_FIND_DATAW fd;
    HANDLE fh = FindFirstFileW((wchar_t *)pat.p, &fd);
    if (fh == INVALID_HANDLE_VALUE) { buf_free(&pat); free(abs); return 1; }

    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
        if (!wcscmp(fd.cFileName, L".git")) continue;
        char *name8 = w2u8(fd.cFileName);
        if (!name8) continue;
        buf rr;
        buf_init(&rr);
        if (rel && *rel) { buf_puts(&rr, rel); buf_putc(&rr, '/'); }
        buf_puts(&rr, name8);
        char *relc = xstrdup(buf_cstr(&rr));
        buf_free(&rr);
        free(name8);

        wchar_t *cw = child_of(abs, fd.cFileName);

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
            wchar_t *real = final_path_of(cw);
            int inside = real && inside_root_w(real);
            int isdir = is_dir_w(cw);
            free(real);
            free(cw);
            if (!inside || isdir) { free(relc); continue; }
            sl_push(c->rels, relc);                 /* 仓内文件符号链接：收它自己 */
            continue;
        }

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            /* 子模块：含 .git 的目录只把目录自己交给 git，不下探 */
            wchar_t *g = child_of(cw, L".git");
            int is_sub = exists_w(g);
            free(g);
            free(cw);
            if (is_sub) { sl_push(c->rels, relc); continue; }
            int ok = walk_dir(c, relc, 0);
            free(relc);
            if (!ok) { FindClose(fh); buf_free(&pat); free(abs); return 0; }
            continue;
        }
        free(cw);
        sl_push(c->rels, relc);
    } while (FindNextFileW(fh, &fd));
    FindClose(fh);
    buf_free(&pat);
    free(abs);
    return 1;
}

static int ancestors_plain(const char *rel) {
    char *work = xstrdup(rel);
    size_t nseg = 0;
    char **seg = split_inplace(work, '\\', &nseg);
    buf cur;
    buf_init(&cur);
    int plain = 1;
    for (size_t i = 0; i + 1 < nseg && plain; i++) {
        if (cur.n) buf_putc(&cur, '\\');
        buf_puts(&cur, seg[i]);
        wchar_t *abs = abs_win(buf_cstr(&cur));
        if (is_reparse_w(abs)) plain = 0;
        free(abs);
    }
    buf_free(&cur);
    free(seg);
    free(work);
    return plain;
}

/* ---- _add_scope：问 git「哪些该进这次提交」 ---- */

static int add_scope(skset *wanted, slist *tracked_nc, char **errmsg) {
    *errmsg = NULL;
    const char *a[] = { "ls-files", "-t", "-z", "--cached", "--others",
                        "--exclude-standard", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(a, NULL, 0, &out, &er);
    if (rc != 0) {
        char *t = strip_dup(er.n ? buf_cstr(&er) : buf_cstr(&out));
        if (strlen(t) > 160) t[160] = 0;
        buf m;
        buf_init(&m);
        if (t[0]) buf_printf(&m, "读不到已跟踪/未跟踪文件清单(%s),已拒绝提交", t);
        else buf_printf(&m, "读不到已跟踪/未跟踪文件清单(git rc=%d),已拒绝提交", rc);
        *errmsg = xstrdup(buf_cstr(&m));
        buf_free(&m);
        free(t);
        buf_free(&out);
        buf_free(&er);
        return 0;
    }
    skset untracked;
    ss_init(&untracked);
    skset seen_tracked;
    ss_init(&seen_tracked);
    slist tmp = {0};
    nul_split(&out, &tmp);
    for (size_t i = 0; i < tmp.n; i++) {
        const char *e = tmp.v[i];
        size_t el = strlen(e);
        if (el < 3) continue;
        char *nc = normcase_dup(e + 2);
        if (e[0] == '?') {
            ss_add(&untracked, nc);
        } else if (ss_add(&seen_tracked, nc)) {
            sl_push(tracked_nc, xstrdup(nc));      /* Python: normcase 之后进 tracked 集合 */
        } else {
            free(nc);
        }
    }
    ss_free(&seen_tracked);
    sl_free(&tmp);
    buf_free(&out);
    buf_free(&er);

    const char *d[] = { "diff", "--no-renames", "--name-only", "-z", NULL };
    buf dout, der;
    buf_init(&dout);
    buf_init(&der);
    rc = git_run(d, NULL, 0, &dout, &der);
    if (rc != 0) {
        char *t = strip_dup(der.n ? buf_cstr(&der) : buf_cstr(&dout));
        if (strlen(t) > 160) t[160] = 0;
        buf m;
        buf_init(&m);
        if (t[0]) buf_printf(&m, "读不到工作区与索引的差异(%s),已拒绝提交", t);
        else buf_printf(&m, "读不到工作区与索引的差异(git rc=%d),已拒绝提交", rc);
        *errmsg = xstrdup(buf_cstr(&m));
        buf_free(&m);
        free(t);
        buf_free(&dout);
        buf_free(&der);
        ss_free(&untracked);
        return 0;
    }
    slist dtmp = {0};
    nul_split(&dout, &dtmp);
    for (size_t i = 0; i < dtmp.n; i++) {
        char *nc = normcase_dup(dtmp.v[i]);
        if (!ss_add(wanted, nc)) free(nc);
    }
    sl_free(&dtmp);
    buf_free(&dout);
    buf_free(&der);
    /* wanted |= untracked */
    for (size_t i = 0; i < untracked.cap; i++) {
        if (!untracked.slots[i]) continue;
        char *k = untracked.slots[i];
        if (ss_has(wanted, k)) continue;
        ss_add(wanted, xstrdup(k));
    }
    ss_free(&untracked);
    return 1;
}

/* _index_delta：索引里「与 HEAD 不一致」的路径集合（normcase）。 */
static int index_delta(int head_exists, const slist *index_all, skset *out,
                       char **errmsg) {
    *errmsg = NULL;
    if (!head_exists) {
        if (index_all) {
            for (size_t i = 0; i < index_all->n; i++)
                ss_add(out, xstrdup(index_all->v[i]));
            return 1;
        }
        const char *a[] = { "ls-files", "-z", NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        (void)git_run(a, NULL, 0, &o, &e);
        buf_free(&e);
        slist t = {0};
        nul_split(&o, &t);
        for (size_t i = 0; i < t.n; i++) {
            char *nc = normcase_dup(t.v[i]);
            if (!ss_add(out, nc)) free(nc);
        }
        sl_free(&t);
        buf_free(&o);
        return 1;
    }
    const char *a[] = { "diff", "--cached", "--no-renames", "--name-only", "-z",
                        "HEAD", NULL };
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = git_run(a, NULL, 0, &o, &e);
    if (rc != 0) {
        char *t = strip_dup(e.n ? buf_cstr(&e) : buf_cstr(&o));
        if (strlen(t) > 160) t[160] = 0;
        buf m;
        buf_init(&m);
        if (t[0]) buf_printf(&m, "核对暂存区失败(%s),已拒绝提交", t);
        else buf_printf(&m, "核对暂存区失败(git rc=%d),已拒绝提交", rc);
        *errmsg = xstrdup(buf_cstr(&m));
        buf_free(&m);
        free(t);
        buf_free(&o);
        buf_free(&e);
        return 0;
    }
    slist t = {0};
    nul_split(&o, &t);
    for (size_t i = 0; i < t.n; i++) {
        char *nc = normcase_dup(t.v[i]);
        if (!ss_add(out, nc)) free(nc);
    }
    sl_free(&t);
    buf_free(&o);
    buf_free(&e);
    return 1;
}

/* 暂存全部「该进这次提交的东西」；返回 NULL 或错误文案。
 * *skipped 收「握不住因而没暂存」的子树相对路径。 */
static char *stage_everything_safely(slist *skipped) {
    char *err = NULL;
    skset wanted, allowed;
    ss_init(&wanted);
    ss_init(&allowed);
    slist tracked_ordered = {0};
    slist rels = {0}, unpinned = {0};
    slist avals = {0};
    walkctx wc = {0};

    if (!add_scope(&wanted, &tracked_ordered, &err)) goto done;

    wc.rels = &rels;
    wc.unpinned = &unpinned;
    if (!walk_dir(&wc, "", 1)) { err = xstrdup(wc.err ? wc.err : "枚举工作区失败"); goto done; }

    /* allow = { normcase(r): r | r ∈ rels 且 normcase(r) ∈ wanted } */
    for (size_t i = 0; i < rels.n; i++) {
        char *nc = normcase_dup(rels.v[i]);
        if (ss_has(&wanted, nc)) {
            if (ss_add(&allowed, nc)) sl_push(&avals, xstrdup(rels.v[i]));
            else free(nc);
        } else {
            free(nc);
        }
    }
    /* 已跟踪但工作区里已经没了的：这次删除也要进提交 */
    sl_sort(&tracked_ordered);
    for (size_t i = 0; i < tracked_ordered.n; i++) {
        const char *r = tracked_ordered.v[i];
        if (ss_has(&allowed, r)) continue;
        char *nc = normcase_dup(r);
        wchar_t *abs = abs_win(r);
        int lex = exists_w(abs);
        free(abs);
        if (lex) { free(nc); continue; }
        if (ss_has(&wanted, nc) && ancestors_plain(r)) {
            if (ss_add(&allowed, nc)) sl_push(&avals, xstrdup(r));
            else free(nc);
        } else {
            free(nc);
        }
    }
    /* 枚举与暂存之间有目录被换成联接 —— 拒绝 */
    for (size_t i = 0; i < wc.np; i++) {
        if (pin_is_reparse(wc.pins[i])) {
            char *rp = rel_from_abs(wc.pinpath[i]);
            for (char *q = rp; q && *q; q++) if (*q == '/') *q = '\\';
            buf m;
            buf_init(&m);
            buf_printf(&m, "枚举与暂存之间有目录被换成了联接(%s),已拒绝提交",
                       rp ? rp : "");
            err = xstrdup(buf_cstr(&m));
            buf_free(&m);
            free(rp);
            goto done;
        }
    }

    int head_exists = 0;
    {
        const char *a[] = { "rev-parse", "--verify", "--quiet", "HEAD", NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        head_exists = git_run(a, NULL, 0, &o, &e) == 0;
        buf_free(&o);
        buf_free(&e);
    }

    skset before;
    ss_init(&before);
    if (!index_delta(head_exists, head_exists ? NULL : &tracked_ordered, &before, &err)) {
        ss_free(&before);
        goto done;
    }

    sl_sort(&avals);
    if (avals.n) {
        const char *head[] = { "add", NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        int rc = run_paths(head, &avals, &o, &e);
        if (rc != 0) {
            err = git_fail_text(&o, &e);
            buf_free(&o);
            buf_free(&e);
            ss_free(&before);
            goto done;
        }
        buf_free(&o);
        buf_free(&e);
    }

    /* _assert_index_scope：after - before 必须 ⊆ allowed */
    {
        skset after;
        ss_init(&after);
        char *ierr = NULL;
        if (!index_delta(head_exists, NULL, &after, &ierr)) {
            err = ierr ? ierr : xstrdup("核对暂存区失败");
            ss_free(&after);
            ss_free(&before);
            goto done;
        }
        slist extra = {0};
        for (size_t i = 0; i < after.cap; i++) {
            if (!after.slots[i]) continue;
            if (ss_has(&before, after.slots[i])) continue;
            if (ss_has(&allowed, after.slots[i])) continue;
            sl_push(&extra, xstrdup(after.slots[i]));
        }
        ss_free(&after);
        ss_free(&before);
        if (extra.n) {
            sl_sort(&extra);
            const char *head[] = { "reset", "-q", "HEAD", NULL };
            buf o, e;
            buf_init(&o);
            buf_init(&e);
            (void)run_paths(head, &extra, &o, &e);
            buf_free(&o);
            buf_free(&e);
            buf m;
            buf_init(&m);
            buf_printf(&m,
                       "暂存区里出现了 %d 个未经校验的路径(如 %s),已撤销并拒绝提交。"
                       "这是防线自己报的信, 不是操作失败 —— 请把上面这个文件移出工作区后重试。",
                       (int)extra.n, extra.v[0]);
            err = xstrdup(buf_cstr(&m));
            buf_free(&m);
            sl_free(&extra);
            goto done;
        }
        sl_free(&extra);
    }

    /* 握不住的子树：报出来（不是错误） */
    for (size_t i = 0; i < unpinned.n; i++) {
        char *rel = NULL;
        wchar_t *w = u82w(unpinned.v[i]);
        if (w) { rel = rel_from_abs(w); free(w); }
        if (!rel) continue;
        for (char *p = rel; *p; p++) if (*p == '\\') *p = '/';
        sl_push(skipped, rel);
    }

done:
    if (wc.err) free(wc.err);
    walkctx_free(&wc);
    ss_free(&wanted);
    ss_free(&allowed);
    sl_free(&tracked_ordered);
    sl_free(&rels);
    sl_free(&unpinned);
    sl_free(&avals);
    return err;
}

/* Python str.isspace() 的近似（只用于「提交信息是不是全空白」这一处）。 */
static int py_space_cp(unsigned cp) {
    if (cp == 0x09 || cp == 0x0A || cp == 0x0B || cp == 0x0C || cp == 0x0D ||
        cp == 0x1C || cp == 0x1D || cp == 0x1E || cp == 0x1F || cp == 0x20)
        return 1;
    if (cp == 0x85 || cp == 0xA0 || cp == 0x1680) return 1;
    if (cp >= 0x2000 && cp <= 0x200A) return 1;
    return cp == 0x2028 || cp == 0x2029 || cp == 0x202F || cp == 0x205F || cp == 0x3000;
}

static int py_blank(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        unsigned cp = c;
        size_t used = 1;
        if (c >= 0xF0) { used = 4; cp = c & 0x07u; }
        else if (c >= 0xE0) { used = 3; cp = c & 0x0Fu; }
        else if (c >= 0xC0) { used = 2; cp = c & 0x1Fu; }
        if (used > 1) {
            if (i + used > n) return 0;
            for (size_t k = 1; k < used; k++) {
                unsigned char t = (unsigned char)s[i + k];
                if ((t & 0xC0) != 0x80) return 0;
                cp = (cp << 6) | (t & 0x3Fu);
            }
        }
        if (!py_space_cp(cp)) return 0;
        i += used;
    }
    return 1;
}

static void do_commit(const jnode *b, SOCKET s) {
    const jnode *mv = jget(b, "message");
    if (mv && bjtype(mv) != BJ_NULL && !jis_str(mv)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "message 必须是字符串,收到 ");
        buf_puts(&m, jtype_name(mv));
        http_send_err(s, 400, buf_cstr(&m), NULL);
        buf_free(&m);
        return;
    }
    const char *msg = (mv && jis_str(mv)) ? jstr(mv) : "";
    size_t mlen = (mv && jis_str(mv)) ? jstrlen(mv) : 0;

    int blank = py_blank(msg, mlen);
    if (blank) {
        http_send_err(s, 400, "提交信息不能为空", NULL);
        return;
    }
    if (memchr(msg, 0, mlen)) {
        http_send_err(s, 400, "提交信息里不能包含 NUL 字符,已拒绝", NULL);
        return;
    }

    /* 1) 仓外链接拒收门 */
    char *leak = escaping_links_error();
    if (leak) {
        http_send_err(s, 400, leak, NULL);
        free(leak);
        return;
    }

    /* 2) 自己枚举 + 暂存 + 核对索引 */
    slist skipped = {0};
    char *serr = stage_everything_safely(&skipped);
    if (serr) {
        http_send_err(s, 400, serr, NULL);
        free(serr);
        sl_free(&skipped);
        return;
    }

    /* 3) commit */
    char *mz = xstrndup(msg, mlen);
    const char *args[] = { "commit", "-m", mz, NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    int rc = git_run(args, NULL, 0, &out, &er);
    free(mz);

    buf both;
    buf_init(&both);
    if (out.n) buf_append(&both, out.p, out.n);
    if (er.n) buf_append(&both, er.p, er.n);
    char *text = strip_dup(both.p ? buf_cstr(&both) : "");
    buf_free(&both);
    buf_free(&out);
    buf_free(&er);

    if (rc != 0) {
        buf m;
        buf_init(&m);
        if (strstr(text, "nothing to commit") || strstr(text, "no changes added to commit")) {
            buf_puts(&m, "没有可提交的改动(工作区与索引都没有变化)");
        } else if (text[0]) {
            char *cut = xstrndup(text, 400);
            buf_printf(&m, "提交失败:%s", cut);
            free(cut);
        } else {
            buf_printf(&m, "提交失败:git rc=%d", rc);
        }
        http_send_err(s, 400, buf_cstr(&m), NULL);
        buf_free(&m);
        free(text);
        sl_free(&skipped);
        return;
    }

    buf ot;
    buf_init(&ot);
    if (skipped.n) {
        buf_printf(&ot, "注意: 有 %d 个目录握不住(通常是别的程序正占着它),"
                        "它们的内容**没有**进这次提交: ",
                   (int)skipped.n);
        for (size_t i = 0; i < skipped.n && i < 5; i++) {
            if (i) buf_puts(&ot, ", ");
            buf_puts(&ot, skipped.v[i]);
        }
        buf_putc(&ot, '\n');
    }
    buf_puts(&ot, text[0] ? text : "(git 没有输出)");
    free(text);
    sl_free(&skipped);

    buf extra;
    buf_init(&extra);
    buf_puts(&extra, "\"output\":");
    json_str(&extra, buf_cstr(&ot));
    buf_free(&ot);

    wsend_ok(s, b, buf_cstr(&extra));
    buf_free(&extra);
}

/* ================================================================ 分派 */

int write_basic_post(const char *path, const char *query, const char *body,
                     size_t body_len, SOCKET s) {
    (void)query;
    int is_stage = !strcmp(path, "/api/stage");
    int is_unstage = !strcmp(path, "/api/unstage");
    int is_discard = !strcmp(path, "/api/discard");
    int is_commit = !strcmp(path, "/api/commit");
    if (!is_stage && !is_unstage && !is_discard && !is_commit) return 0;

    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    if (is_stage) do_stage_like(b, s, 0);
    else if (is_unstage) do_stage_like(b, s, 1);
    else if (is_discard) do_discard(b, s);
    else do_commit(b, s);
    jfree(b);
    return 1;
}
