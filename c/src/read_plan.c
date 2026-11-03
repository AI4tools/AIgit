/* read_plan.c —— M1 计划面：reset/plan 与 discard/plan 两条**只读预演**。
 *
 * 对位 Python：
 *   GET /api/reset/plan?mode=soft|mixed|hard[&ref=HEAD]  -> Repo.reset_plan
 *   GET /api/discard/plan?files=a&files=b                -> Repo.discard_plan
 *
 * 两条都**绝不改**工作区/索引/HEAD：只跑 `git log` 与 `git ls-files`。
 *
 * 逐项对齐的要点（都是实测/读源码确认过的，不是猜）：
 *   * query 取值走 urllib.parse.parse_qs 的口径（**不是** q_get 的口径）：
 *       - 没有 `=` 的字段整个丢掉（`?mode` 等于没给）；
 *       - 值为空串的字段丢掉（`?mode=` 等于没给；`?files=` 等于空数组）；
 *       - 重复参数按出现顺序保留全部（files 是数组）；
 *       - 键也 percent-decode；`+` 是空格；非法 %XX 原样保留。
 *     q_get 只看第一个值且把「无 =」当成空串，这两点在这里都是错的，所以自己扫。
 *   * mode 必须先于 ref 校验（Python 是 mode 判定在前），且非法 mode 的文案里
 *     带 `%r`（Python repr）：`mode 必须是 'soft' / 'mixed' / 'hard' 之一,收到 'xx'`。
 *   * ref 走 check_ref_name：白名单 `[\w@^~][\w@^~{}+./-]*`（\w 是 Unicode
 *     的字母数字+下划线，所以中文分支名合法、`。` 这种标点不合法），且不含 `..`。
 *     不合法只回那句 _REF_HINT，**不回显 ref 的值**。
 *   * ref 合法但不存在 -> git log rc!=0，stdout 空 -> 200 + undone_commits=[]。
 *   * discard 的分类逐行照抄 discard_plan 的三分支；`rel` 用的是 **resolve 之后**
 *     的路径（Python 的 Path.resolve() 会顺手把大小写换成磁盘上的真实写法、
 *     并把符号链接换成目标），所以这里也要 rel_real()，否则 `files=A.TXT` 会被
 *     判成「未跟踪 -> 永久删除」而 Python 判「已跟踪 -> 恢复」—— 那是数据丢失级的偏差。
 *   * tracked 集合只能问 git（`ls-files -z`），与 Python 的 _tracked_set 同源；
 *     未跟踪清单是 `ls-files --others --exclude-standard --directory
 *     --no-empty-directory`（-x 不算，被忽略的不虚报），两边都排序。
 *   * 路径越界 / .git / 指向 root 自己 / 内嵌 NUL 的文案与状态码照 Python
 *     （path_rel_in_root 已对位，只有「指向 root 自己」这一档它给的是「路径越界」，
 *     这里先按 Python 的语义拦下来）。
 */
#include "routes.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "git.h"
#include "http.h"
#include "json.h"
#include "paths.h"
#include "util.h"

#define REF_HINT \
    "ref 不合法:只允许 git rev-spec 的常见写法" \
    "(字母数字与 _ . / @ ~ ^ { } + -),首字符不能是 -,不能含 : 与空白"

/* ---------------------------------------------------------------- 小工具 */

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* percent-decode（`+` -> 空格，非法 %XX 原样保留），与 urllib.unquote_plus 同义。
 * 结果可能含内嵌 NUL，所以带长度返回。 */
static char *pct_decode(const char *s, size_t n, size_t *outn) {
    char *o = xmalloc(n + 1);
    size_t oi = 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (c == '+') c = ' ';
        else if (c == '%' && i + 2 < n) {
            int h = hexval(s[i + 1]), l = hexval(s[i + 2]);
            if (h >= 0 && l >= 0) { c = (char)((h << 4) | l); i += 2; }
        }
        o[oi++] = c;
    }
    o[oi] = 0;
    if (outn) *outn = oi;
    return o;
}

/* query 里的一个值（带长度：可能含内嵌 NUL） */
typedef struct { char *s; size_t n; } qval;

typedef struct { qval *v; size_t n, cap; } qlist;

static void qlist_push(qlist *l, char *s, size_t n) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 4;
        l->v = xrealloc(l->v, l->cap * sizeof(qval));
    }
    l->v[l->n].s = s;
    l->v[l->n].n = n;
    l->n++;
}

static void qlist_free(qlist *l) {
    for (size_t i = 0; i < l->n; i++) free(l->v[i].s);
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

/* 收集 key 的全部值，语义对齐 urllib.parse.parse_qs（keep_blank_values=False）：
 *   * 字段按 `&` 切（Python 3.11 不再认 `;`）；
 *   * 没有 `=` 的字段整个丢掉；
 *   * 解码后为空串的字段丢掉；
 *   * 键同样 percent-decode。 */
static void qvals(const char *query, const char *key, qlist *out) {
    out->v = NULL;
    out->n = out->cap = 0;
    if (!query || !*query) return;
    size_t klen = strlen(key);
    const char *p = query;
    while (p && *p) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        if (eq) {
            size_t kn = 0;
            char *k = pct_decode(p, (size_t)(eq - p), &kn);
            if (kn == klen && !memcmp(k, key, klen)) {
                size_t vn = 0;
                char *v = pct_decode(eq + 1, (size_t)(end - eq - 1), &vn);
                if (vn) qlist_push(out, v, vn);
                else free(v);
            }
            free(k);
        }
        p = amp ? amp + 1 : NULL;
    }
}

/* Python repr(str) 的常用子集：默认单引号，串里有 ' 且没有 " 时改双引号，
 * 反斜杠与引号转义，控制字符 \n\r\t\xNN。非 ASCII 可打印字符原样输出
 * （Python 3 的 repr 也是这样）。只用于错误文案里回显 mode 与含 NUL 的路径。 */
static char *py_repr(const char *s, size_t n) {
    int has_sq = 0, has_dq = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] == '\'') has_sq = 1;
        else if (s[i] == '"') has_dq = 1;
    }
    char q = (has_sq && !has_dq) ? '"' : '\'';
    buf o;
    buf_init(&o);
    buf_putc(&o, q);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == (unsigned char)q || c == '\\') { buf_putc(&o, '\\'); buf_putc(&o, (char)c); }
        else if (c == '\n') buf_puts(&o, "\\n");
        else if (c == '\r') buf_puts(&o, "\\r");
        else if (c == '\t') buf_puts(&o, "\\t");
        else if (c < 0x20 || c == 0x7f) buf_printf(&o, "\\x%02x", (unsigned)c);
        else buf_putc(&o, (char)c);
    }
    buf_putc(&o, q);
    char *r = xstrdup(buf_cstr(&o));
    buf_free(&o);
    return r;
}

/* ---------------------------------------------------------------- ref 校验 */

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

/* Python 的 `\w`（str 模式，Unicode）：字母/数字/下划线。
 * ASCII 自己判；其余交给 Windows 的 CT_CTYPE1（C1_ALPHA / C1_DIGIT）。 */
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

static int ref_ok(const char *ref, size_t n) {
    if (!ref || n == 0) return 0;
    if (memchr(ref, 0, n)) return 0;          /* 内嵌 NUL：正则不可能匹配 */
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

/* ---------------------------------------------------------------- 路径 */

/* Python 的 `Path` 会把这些输入解析成 root 自己（'' / '.' / './' / 'a/..'），
 * 然后 resolve_in_root 报「路径指向仓库根目录本身」；path_rel_in_root 对这种
 * 输入报的是「路径越界」。为了文案一致，先按词法判一次。 */
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
            if (depth == 0) return 0;          /* 越界，交给 path_rel_in_root */
            depth--;
        } else {
            depth++;
        }
        if (!*e) break;
        p = e + 1;
    }
    return depth == 0;
}

static char *mk_err(const char *fmt, const char *arg) {
    buf b;
    buf_init(&b);
    buf_printf(&b, fmt, arg ? arg : "");
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

static char *rel_real(const char *rel);

/* 请求里的路径 -> 仓内相对路径（Python 的 resolve_in_root().relative_to(root)）
 *
 * 额外两档是 rel_real 之后才看得出来的（Python 的 resolve 先做完才判）：
 *   * 真实路径的第一段是 .git（顺着联接/符号链接进去的）-> 与 Python 同一条拒绝；
 *   * 真实路径就是 root 自己（`link -> .` 这种）-> 同「指向仓库根目录本身」。 */
static char *resolve_rel(const char *input, char **err) {
    if (err) *err = NULL;
    if (lexically_root(input)) {
        if (err) *err = mk_err("路径指向仓库根目录本身,已拒绝:%s", input);
        return NULL;
    }
    char *rel0 = path_rel_in_root(input, err);
    if (!rel0) return NULL;
    /* GetFullPathNameW 会保留目录的尾随分隔符（"sub/" -> "...\sub\"），
     * Python 的 Path 会吃掉它 -> rel 是 "sub"。 */
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

/* root\rel 的宽字符绝对路径（'/' 转 '\'）。 */
static wchar_t *abs_win(const char *rel) {
    const wchar_t *root = git_root();
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
        const wchar_t *rp = real;
        if (rp[0] == L'\\' && rp[1] == L'\\' && rp[2] == L'?' && rp[3] == L'\\') rp += 4;
        /* rp 可能**正好是 root**（`selflink -> .` 这种联接/符号链接），
         * 那时尾段是空串，剩余段原样接上（Python 的 resolve 同语义）。 */
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

/* ---------------------------------------------------------------- 字符串表 */

typedef struct { char **v; size_t n, cap; } slist;

static void slist_push(slist *l, char *s) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 16;
        l->v = xrealloc(l->v, l->cap * sizeof(char *));
    }
    l->v[l->n++] = s;
}

static void slist_free(slist *l) {
    for (size_t i = 0; i < l->n; i++) free(l->v[i]);
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

static int slist_has(const slist *l, const char *s) {
    for (size_t i = 0; i < l->n; i++)
        if (!strcmp(l->v[i], s)) return 1;
    return 0;
}

static int cmp_str(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

/* 把 buf 里的 '\n' 就地改成 NUL（Python 那边是 splitlines()），返回首地址。
 * 少了这一步 strlen 会一路跑到缓冲区末尾，整段输出被当成一行。 */
static char *nl_to_nul(buf *b) {
    char *s = buf_cstr(b);
    for (size_t i = 0; i < b->n; i++)
        if (s[i] == '\n') s[i] = 0;
    return s;
}

/* git 的 stdout -> 按 '\n' 切、吃行尾 '\r'、丢空行的字符串表（各元素 malloc）。 */
static void lines_into(slist *l, buf *out) {
    char *s = nl_to_nul(out);
    size_t pos = 0;
    while (pos < out->n) {
        char *line = s + pos;
        size_t ll = strlen(line);
        pos += ll + 1;
        if (ll && line[ll - 1] == '\r') line[--ll] = 0;
        if (!ll) continue;
        slist_push(l, xstrdup(line));
    }
}

/* `git ls-files -z` -> 路径表 */
static void nul_into(slist *l, buf *out) {
    char *s = buf_cstr(out);
    size_t pos = 0;
    while (pos < out->n) {
        char *e = s + pos;
        size_t ll = strlen(e);
        pos += ll + 1;
        if (!ll) continue;
        slist_push(l, xstrdup(e));
    }
}

/* Python 的 _tracked_set()：只许问 git。 */
static void tracked_set(slist *l) {
    const char *args[] = { "ls-files", "-z", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    (void)git_run(args, NULL, 0, &out, &er);      /* check=False */
    buf_free(&er);
    nul_into(l, &out);
    buf_free(&out);
}

/* Python 的 _untracked_list()：-x 不算（被忽略的不是 clean -fd 的目标）。 */
static void untracked_list(slist *l) {
    const char *args[] = { "ls-files", "--others", "--exclude-standard",
                           "--directory", "--no-empty-directory", NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    (void)git_run(args, NULL, 0, &out, &er);      /* check=False */
    buf_free(&er);
    lines_into(l, &out);
    buf_free(&out);
    qsort(l->v, l->n, sizeof(char *), cmp_str);
}

/* ---------------------------------------------------------------- reset/plan */

static void handle_reset_plan(const char *query, SOCKET s) {
    qlist mode, ref;
    qvals(query, "mode", &mode);
    qvals(query, "ref", &ref);

    if (mode.n == 0) {
        http_send_err(s, 400, "缺少 mode 参数(soft / mixed / hard)", NULL);
        qlist_free(&mode);
        qlist_free(&ref);
        return;
    }
    const char *m = mode.v[0].s;
    size_t mn = mode.v[0].n;
    int is_soft = (mn == 4 && !memcmp(m, "soft", 4));
    int is_mixed = (mn == 5 && !memcmp(m, "mixed", 5));
    int is_hard = (mn == 4 && !memcmp(m, "hard", 4));
    if (!is_soft && !is_mixed && !is_hard) {
        char *rp = py_repr(m, mn);
        buf msg;
        buf_init(&msg);
        buf_puts(&msg, "mode 必须是 'soft' / 'mixed' / 'hard' 之一,收到 ");
        buf_puts(&msg, rp);
        http_send_err(s, 400, buf_cstr(&msg), NULL);
        buf_free(&msg);
        free(rp);
        qlist_free(&mode);
        qlist_free(&ref);
        return;
    }

    const char *rf = ref.n ? ref.v[0].s : "HEAD";
    size_t rn = ref.n ? ref.v[0].n : 4;
    if (!ref_ok(rf, rn)) {
        http_send_err(s, 400, REF_HINT, NULL);
        qlist_free(&mode);
        qlist_free(&ref);
        return;
    }

    /* git log --format=%H%x1f%h%x1f%s <ref>..HEAD  （check=False） */
    buf spec;
    buf_init(&spec);
    buf_puts(&spec, rf);
    buf_puts(&spec, "..HEAD");
    const char *args[] = { "log", "--format=%H%x1f%h%x1f%s", buf_cstr(&spec), NULL };
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    (void)git_run(args, NULL, 0, &out, &er);
    buf_free(&er);
    buf_free(&spec);

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"op\":\"reset\",\"mode\":");
    json_str(&o, m);
    buf_puts(&o, ",\"ref\":");
    json_str(&o, rf);
    buf_puts(&o, ",\"undone_commits\":[");

    {
        char *s0 = nl_to_nul(&out);
        size_t pos = 0;
        size_t nline = 0;
        int first = 1;
        while (pos < out.n && nline < 50) {
            char *line = s0 + pos;
            size_t ll = strlen(line);
            pos += ll + 1;
            nline++;
            if (ll && line[ll - 1] == '\r') line[--ll] = 0;
            /* Python: parts = line.split("\x1f"); len(parts) == 3 才收 */
            char *sp1 = strchr(line, 0x1f);
            if (!sp1) continue;
            *sp1 = 0;
            char *sp2 = strchr(sp1 + 1, 0x1f);
            if (!sp2) continue;
            *sp2 = 0;
            if (strchr(sp2 + 1, 0x1f)) continue;
            buf_puts(&o, first ? "" : ",");
            first = 0;
            buf_puts(&o, "{\"sha\":");
            json_str(&o, line);
            buf_puts(&o, ",\"short\":");
            json_str(&o, sp1 + 1);
            buf_puts(&o, ",\"summary\":");
            json_str(&o, sp2 + 1);
            buf_putc(&o, '}');
        }
    }
    buf_puts(&o, "],\"note\":");
    {
        buf note;
        buf_init(&note);
        if (is_soft)
            buf_printf(&note, "soft: 移动 HEAD 到 %s,改动留在暂存区,工作区不动", rf);
        else if (is_mixed)
            buf_printf(&note, "mixed: 移动 HEAD 到 %s,改动留在工作区(默认 git 行为)", rf);
        else
            buf_puts(&note, "hard: 未提交的改动将永久丢失;git clean -fd"
                            " 会删掉所有未跟踪文件和目录");
        json_str(&o, buf_cstr(&note));
        buf_free(&note);
    }
    if (is_hard) buf_puts(&o, ",\"warning\":\"未提交的改动将永久丢失\"");
    buf_putc(&o, '}');

    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
    buf_free(&out);
    qlist_free(&mode);
    qlist_free(&ref);
}

/* ---------------------------------------------------------------- discard/plan */

typedef struct {
    char  *path;
    char  *action;
    char  *detail;
    char **inside;      /* 只有 blocked 用 */
    size_t ninside;
    int    blocked;
} dentry;

typedef struct { dentry *v; size_t n, cap; } dlist;

static dentry *dlist_push(dlist *l) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 8;
        l->v = xrealloc(l->v, l->cap * sizeof(dentry));
    }
    dentry *d = &l->v[l->n++];
    memset(d, 0, sizeof(*d));
    return d;
}

static void dlist_free(dlist *l) {
    for (size_t i = 0; i < l->n; i++) {
        free(l->v[i].path);
        free(l->v[i].detail);
        for (size_t k = 0; k < l->v[i].ninside; k++) free(l->v[i].inside[k]);
        free(l->v[i].inside);
    }
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

static void emit_entry(buf *o, const dentry *d, int *first) {
    buf_puts(o, *first ? "" : ",");
    *first = 0;
    buf_puts(o, "{\"path\":");
    json_str(o, d->path);
    buf_puts(o, ",\"action\":");
    json_str(o, d->action);
    buf_puts(o, ",\"detail\":");
    json_str(o, d->detail);
    if (d->blocked) {
        buf_puts(o, ",\"tracked_inside\":[");
        for (size_t i = 0; i < d->ninside; i++) {
            if (i) buf_putc(o, ',');
            json_str(o, d->inside[i]);
        }
        buf_putc(o, ']');
    }
    buf_putc(o, '}');
}

static void handle_discard_plan(const char *query, SOCKET s) {
    qlist files;
    qvals(query, "files", &files);

    slist tracked, untracked;
    memset(&tracked, 0, sizeof(tracked));
    memset(&untracked, 0, sizeof(untracked));
    dlist entries, blocked;
    memset(&entries, 0, sizeof(entries));
    memset(&blocked, 0, sizeof(blocked));
    int bad = 0;

    tracked_set(&tracked);
    untracked_list(&untracked);

    for (size_t i = 0; i < files.n && !bad; i++) {
        const char *raw = files.v[i].s;
        size_t rawn = files.v[i].n;
        if (memchr(raw, 0, rawn)) {
            char *rp = py_repr(raw, rawn);
            buf msg;
            buf_init(&msg);
            buf_puts(&msg, "路径里不能包含 NUL 字符,已拒绝:");
            buf_puts(&msg, rp);
            http_send_err(s, 400, buf_cstr(&msg), NULL);
            buf_free(&msg);
            free(rp);
            bad = 1;
            break;
        }
        char *err = NULL;
        char *rel = resolve_rel(raw, &err);
        if (!rel) {
            http_send_err(s, 400, err ? err : "路径不合法", NULL);
            free(err);
            bad = 1;
            break;
        }

        wchar_t *wp = abs_win(rel);
        DWORD attr = GetFileAttributesW(wp);
        free(wp);
        int exists = (attr != INVALID_FILE_ATTRIBUTES);
        int is_dir = exists && (attr & FILE_ATTRIBUTE_DIRECTORY);

        if (!exists) {
            dentry *d = dlist_push(&entries);
            d->path = rel;
            d->action = (char *)"missing";
            d->detail = xstrdup("路径不存在,discard 什么也不会做");
            continue;
        }
        if (slist_has(&tracked, rel)) {
            dentry *d = dlist_push(&entries);
            d->path = rel;
            d->action = (char *)"restore";
            d->detail = xstrdup("已跟踪文件 -> 从 HEAD 恢复,改动丢失");
            continue;
        }
        if (is_dir) {
            size_t plen = strlen(rel);
            slist inner;
            memset(&inner, 0, sizeof(inner));
            for (size_t k = 0; k < tracked.n; k++) {
                const char *t = tracked.v[k];
                if (strncmp(t, rel, plen) == 0 && t[plen] == '/')
                    slist_push(&inner, xstrdup(t));
            }
            if (inner.n) {
                qsort(inner.v, inner.n, sizeof(char *), cmp_str);
                dentry *d = dlist_push(&blocked);
                d->path = rel;
                d->action = (char *)"blocked";
                d->blocked = 1;
                {
                    buf det;
                    buf_init(&det);
                    buf_printf(&det, "目录内含 %zu 个已跟踪文件,discard 会整条拒绝",
                               inner.n);
                    d->detail = xstrdup(buf_cstr(&det));
                    buf_free(&det);
                }
                size_t keep = inner.n < 20 ? inner.n : 20;
                d->inside = xmalloc((keep ? keep : 1) * sizeof(char *));
                d->ninside = keep;
                for (size_t k = 0; k < keep; k++) d->inside[k] = xstrdup(inner.v[k]);
                slist_free(&inner);
            } else {
                dentry *d = dlist_push(&entries);
                d->path = rel;
                d->action = (char *)"delete-tree";
                d->detail = xstrdup("未跟踪目录 -> 整棵树永久删除,"
                                    "git 里没有它,删了找不回来");
            }
            continue;
        }
        {
            dentry *d = dlist_push(&entries);
            d->path = rel;
            d->action = (char *)"delete-file";
            d->detail = xstrdup("未跟踪文件 -> 永久删除,git 里没有它,删了找不回来");
        }
    }

    if (bad) {
        dlist_free(&entries);
        dlist_free(&blocked);
        slist_free(&tracked);
        slist_free(&untracked);
        qlist_free(&files);
        return;
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"op\":\"discard\",\"entries\":[");
    {
        int first = 1;
        for (size_t i = 0; i < entries.n; i++) emit_entry(&o, &entries.v[i], &first);
    }
    buf_puts(&o, "],\"blocked\":[");
    {
        int first = 1;
        for (size_t i = 0; i < blocked.n; i++) emit_entry(&o, &blocked.v[i], &first);
    }
    buf_puts(&o, "],\"untracked_total\":[");
    for (size_t i = 0; i < untracked.n; i++) {
        if (i) buf_putc(&o, ',');
        json_str(&o, untracked.v[i]);
    }
    buf_printf(&o, "],\"untracked_count\":%zu,\"note\":", untracked.n);
    {
        buf note;
        buf_init(&note);
        buf_puts(&note, "action=restore 的丢的是**未提交的改动**; "
                        "action=delete-* 的是 git 从没记过、删了就找不回的东西。");
        if (untracked.n)
            buf_printf(&note, "另有 %zu 个未跟踪文件/目录,「丢弃全部」会一并删掉。",
                       untracked.n);
        json_str(&o, buf_cstr(&note));
        buf_free(&note);
    }
    buf_putc(&o, '}');

    http_send_json(s, 200, buf_cstr(&o), o.n);

    buf_free(&o);
    dlist_free(&entries);
    dlist_free(&blocked);
    slist_free(&tracked);
    slist_free(&untracked);
    qlist_free(&files);
}

/* ---------------------------------------------------------------- 分派 */

int read_plan_get(const char *path, const char *query, SOCKET s) {
    if (!path) return 0;
    if (!strcmp(path, "/api/reset/plan"))   { handle_reset_plan(query, s);   return 1; }
    if (!strcmp(path, "/api/discard/plan")) { handle_discard_plan(query, s); return 1; }
    return 0;
}
