/* read_conflict.c —— 冲突/远程只读端点（M1）。
 *
 * 6 个端点，逐字段对照 Python 版：
 *   /api/conflict/state        <- conflicts.py::merge_state
 *   /api/conflict/list         <- conflicts.py::list_conflicts
 *   /api/conflict/inspect      <- conflicts.py::inspect
 *   /api/remote/capability     <- gitops.py::NetGate.describe
 *   /api/remote/list           <- gitops.py::list_remotes
 *   /api/remote/ahead-behind   <- gitops.py::ahead_behind
 *
 * 与 Python 版的对齐点（每一条都是实测过的，不是推测）：
 *   * 文本按 `\n` 拆行、`\r` 留在行内容里（Python 的 _split_lines 就是这样），
 *     所以 CRLF 工作区里的冲突标记**认不出来** —— 这是 Python 的行为，照抄；
 *   * `.git` 下的状态文件按 Python 的 read_text 语义读（universal newlines，
 *     非法 UTF-8 换 U+FFFD）；
 *   * HEAD 三态（分支 / 游离 / 未出生）按 gitrefs.head_state 判定，不 spawn；
 *     `.git` 是**文件**（链接工作区）或布局被环境变量改写时，回退到
 *     `rev-parse HEAD` / `rev-parse --abbrev-ref HEAD` —— 因为那种情况下
 *     refs 在公共目录里，直接读 .git 会读不到（实测踩过：head 变成 null）；
 *   * 锚点对齐按 difflib.SequenceMatcher(autojunk=False) 的
 *     find_longest_match + 递归 get_matching_blocks 实现（纯 CPU）；
 *   * query 参数**保留内嵌 NUL**：Python 明确拒绝含 NUL 的路径（防路径走私），
 *     用 params.c 的 q_get 会静默截断成另一个文件名（实测 `?file=a.txt%00`
 *     会变成 a.txt 并返回 200）。
 *
 * 这里不写任何 500：所有可预期失败都走 400，且文案与 Python 版逐字一致。
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
#include "indexfile.h"
#include "json.h"
#include "params.h"
#include "paths.h"
#include "util.h"

/* ================================================================ 小工具 */

static int is_ws_ascii(int c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

/* Python 的 str.split()：按任意空白切，丢掉空串。返回 malloc 的数组。 */
static char **ws_split(const char *s, size_t *n) {
    char **out = NULL;
    size_t cnt = 0;
    const char *p = s;
    while (p && *p) {
        while (*p && is_ws_ascii((unsigned char)*p)) p++;
        if (!*p) break;
        const char *b = p;
        while (*p && !is_ws_ascii((unsigned char)*p)) p++;
        out = xrealloc(out, (cnt + 1) * sizeof(char *));
        out[cnt++] = xstrndup(b, (size_t)(p - b));
    }
    *n = cnt;
    return out;
}

static void free_strv(char **v, size_t n) {
    if (!v) return;
    for (size_t i = 0; i < n; i++) free(v[i]);
    free(v);
}

/* 合法 sha（40 或 64 位十六进制），与 gitrefs._is_sha 同口径（先 strip）。 */
static int is_sha_text(const char *s) {
    if (!s) return 0;
    char *t = py_strip_dup(s);
    size_t n = strlen(t);
    int ok = (n == 40 || n == 64);
    for (size_t i = 0; ok && i < n; i++) {
        if (!isxdigit((unsigned char)t[i])) ok = 0;
    }
    free(t);
    return ok;
}

/* UTF-8 合法性修正：非法字节换 U+FFFD（Python 的 errors="replace" 口径）。 */
static char *u8_sanitize(const char *s, size_t n) {
    buf b;
    buf_init(&b);
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) {
            if (c == 0) buf_puts(&b, "\xEF\xBF\xBD");
            else buf_putc(&b, (char)c);
            i++;
            continue;
        }
        int len = 0;
        unsigned cp = 0;
        if (c >= 0xC2 && c <= 0xDF) { len = 2; cp = c & 0x1Fu; }
        else if (c >= 0xE0 && c <= 0xEF) { len = 3; cp = c & 0x0Fu; }
        else if (c >= 0xF0 && c <= 0xF4) { len = 4; cp = c & 0x07u; }
        int ok = len && (i + (size_t)len <= n);
        if (ok) {
            for (int k = 1; k < len; k++) {
                unsigned char cc = (unsigned char)s[i + (size_t)k];
                if ((cc & 0xC0) != 0x80) { ok = 0; break; }
                cp = (cp << 6) | (cc & 0x3Fu);
            }
        }
        if (ok) {
            if ((len == 2 && cp < 0x80) || (len == 3 && cp < 0x800) ||
                (len == 4 && cp < 0x10000) || (cp >= 0xD800 && cp <= 0xDFFF) ||
                cp > 0x10FFFF) ok = 0;
        }
        if (ok) {
            buf_append(&b, s + i, (size_t)len);
            i += (size_t)len;
        } else {
            buf_puts(&b, "\xEF\xBF\xBD");
            i++;
        }
    }
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

/* Python repr(str) 的近似（用于错误文案里的 %r），支持内嵌 NUL。 */

/* query 参数解码：保留内嵌 NUL（q_get 的 NUL 会截断，而 Python 版会拒绝
 * 含 NUL 的路径/名字 —— 实测 `?file=a.txt%00` Python 回 400「路径里不能包含
 * NUL 字符」，用 q_get 则会静默变成 a.txt，等于路径走私）。 */
static int q_raw(const char *query, const char *key, char **out, size_t *outlen) {
    int r = q_get_n(query, key, out, outlen);
    if (r == 1 && *outlen > 65536) {
        free(*out);
        *out = NULL;
        *outlen = 0;
        return -1;
    }
    return r;
}

/* ================================================================ SHA-256 */

typedef struct {
    uint32_t h[8];
    uint64_t len;
    unsigned char blk[64];
    size_t blen;
} sha256_ctx;

static const uint32_t K256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u,
    0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u,
    0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
    0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au,
    0x5b9cca4fu, 0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

#define ROR32(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(sha256_ctx *c, const unsigned char *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR32(w[i - 15], 7) ^ ROR32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR32(w[i - 2], 17) ^ ROR32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
    uint32_t e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ROR32(e, 6) ^ ROR32(e, 11) ^ ROR32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = h + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ROR32(a, 2) ^ ROR32(a, 13) ^ ROR32(a, 22);
        uint32_t mj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + mj;
        h = g; g = f; f = e; e = d + t1;
        d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha256_init(sha256_ctx *c) {
    c->h[0] = 0x6a09e667u; c->h[1] = 0xbb67ae85u; c->h[2] = 0x3c6ef372u;
    c->h[3] = 0xa54ff53au; c->h[4] = 0x510e527fu; c->h[5] = 0x9b05688cu;
    c->h[6] = 0x1f83d9abu; c->h[7] = 0x5be0cd19u;
    c->len = 0;
    c->blen = 0;
}

static void sha256_update(sha256_ctx *c, const void *data, size_t n) {
    const unsigned char *p = data;
    c->len += (uint64_t)n;
    while (n) {
        size_t take = 64 - c->blen;
        if (take > n) take = n;
        memcpy(c->blk + c->blen, p, take);
        c->blen += take;
        p += take;
        n -= take;
        if (c->blen == 64) { sha256_block(c, c->blk); c->blen = 0; }
    }
}

static void sha256_hex(const void *data, size_t n, char out[65]) {
    sha256_ctx c;
    sha256_init(&c);
    sha256_update(&c, data, n);
    uint64_t bits = c.len * 8;
    unsigned char pad[72];
    size_t padlen = (c.blen < 56) ? (56 - c.blen) : (120 - c.blen);
    memset(pad, 0, sizeof(pad));
    pad[0] = 0x80;
    for (int i = 0; i < 8; i++) pad[padlen + i] = (unsigned char)(bits >> (56 - 8 * i));
    sha256_update(&c, pad, padlen + 8);
    static const char *hex = "0123456789abcdef";
    for (int i = 0; i < 8; i++) {
        for (int k = 0; k < 4; k++) {
            unsigned char b = (unsigned char)(c.h[i] >> (24 - 8 * k));
            out[i * 8 + k * 2] = hex[b >> 4];
            out[i * 8 + k * 2 + 1] = hex[b & 15];
        }
    }
    out[64] = 0;
}

/* ================================================================ 文件读 */

typedef struct {
    int exists;      /* 是**普通文件**（不是目录） */
    char *raw;
    size_t rawlen;
} filebytes;

static wchar_t *repo_wpath(const char *rel) {
    const wchar_t *root = git_root();
    wchar_t *w = u82w(rel);
    if (!root || !w) { free(w); return NULL; }
    buf b;
    buf_init(&b);
    buf_append(&b, root, wcslen(root) * sizeof(wchar_t));
    buf_append(&b, L"\\", sizeof(wchar_t));
    for (wchar_t *p = w; *p; p++) {
        wchar_t c = (*p == L'/') ? L'\\' : *p;
        buf_append(&b, &c, sizeof(wchar_t));
    }
    wchar_t z = 0;
    buf_append(&b, &z, sizeof(wchar_t));
    free(w);
    return (wchar_t *)b.p;
}

static wchar_t *join_w(const wchar_t *base, const wchar_t *name) {
    buf b;
    buf_init(&b);
    buf_append(&b, base, wcslen(base) * sizeof(wchar_t));
    buf_append(&b, L"\\", sizeof(wchar_t));
    buf_append(&b, name, wcslen(name) * sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&b, &z, sizeof(wchar_t));
    return (wchar_t *)b.p;
}

/* 读整个普通文件。不是普通文件 / 读失败 → exists=0。 */
static void read_plain_file(const wchar_t *path, filebytes *fb) {
    memset(fb, 0, sizeof(*fb));
    DWORD attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_DIRECTORY)) return;
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE |
                           FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    buf b;
    buf_init(&b);
    char tmp[8192];
    DWORD got = 0;
    while (ReadFile(h, tmp, sizeof(tmp), &got, NULL) && got > 0) buf_append(&b, tmp, got);
    CloseHandle(h);
    fb->exists = 1;
    fb->rawlen = b.n;
    fb->raw = xmalloc(b.n + 1);
    if (b.n) memcpy(fb->raw, b.p, b.n);
    fb->raw[b.n] = 0;
    buf_free(&b);
}

/* ================================================================ git 调用 */

static int run_git(buf *out, buf *err, const char *const args[]) {
    if (out) buf_reset(out);
    if (err) buf_reset(err);
    return git_run(args, NULL, 0, out, err);
}
/* Python 的 universal newlines：\r\n 与 \r 都变 \n。 */
static char *normalize_newlines(const char *s, size_t n) {
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

/* ================================================================ .git 目录 */

static wchar_t *g_gd = NULL;
static int g_gd_done = 0;

/* gitrefs.gitdir(root) 的非 None 条件：`.git` 是目录、没有 reftable、没有
 * 环境变量改写布局。只有这时才能直接读 `.git` 里的文件 —— 链接工作区
 * （`.git` 是文件）必须回退去问 git，否则 refs 在**公共**目录里，读不到。 */
static int dotgit_ok(void) {
    if (getenv("GIT_DIR") || getenv("GIT_COMMON_DIR")) return 0;
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t *dotgit = join_w(root, L".git");
    DWORD attr = GetFileAttributesW(dotgit);
    int ok = (attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY));
    if (ok) {
        wchar_t *reftable = join_w(dotgit, L"reftable");
        DWORD ra = GetFileAttributesW(reftable);
        free(reftable);
        if (ra != INVALID_FILE_ATTRIBUTES && (ra & FILE_ATTRIBUTE_DIRECTORY)) ok = 0;
    }
    free(dotgit);
    return ok;
}

static wchar_t *real_path_w(const wchar_t *path) {
    HANDLE h = CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE |
                           FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                           FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED);
    wchar_t *out = NULL;
    if (need) {
        out = xmalloc(((size_t)need + 1) * sizeof(wchar_t));
        DWORD got = GetFinalPathNameByHandleW(h, out, need, FILE_NAME_NORMALIZED);
        if (!got) { free(out); out = NULL; }
        else if (!wcsncmp(out, L"\\\\?\\", 4)) memmove(out, out + 4, (wcslen(out + 4) + 1) * sizeof(wchar_t));
    }
    CloseHandle(h);
    return out;
}

static const wchar_t *gitdir_w(void) {
    if (g_gd_done) return g_gd;
    g_gd_done = 1;
    const wchar_t *root = git_root();
    if (!root) return NULL;
    if (dotgit_ok()) {
        wchar_t *dotgit = join_w(root, L".git");
        wchar_t *rp = real_path_w(dotgit);
        g_gd = rp ? rp : dotgit;
        if (rp) free(dotgit);
        return g_gd;
    }
    /* 回退：问 git 要 gitdir（worktree / 子模块的 .git 是文件） */
    buf out, err;
    buf_init(&out);
    buf_init(&err);
    const char *args[] = { "rev-parse", "--git-dir", NULL };
    int rc = run_git(&out, &err, args);
    if (rc == 0) {
        char *s = py_strip_dup(buf_cstr(&out));
        if (*s) {
            wchar_t *w = u82w(s);
            if (w) {
                if (w[0] == L'\\' || (iswalpha(w[0]) && w[1] == L':')) {
                    wchar_t *rp = real_path_w(w);
                    g_gd = rp ? rp : w;
                    if (rp) free(w);
                } else {
                    wchar_t *joined = join_w(root, w);
                    wchar_t *rp = real_path_w(joined);
                    g_gd = rp ? rp : joined;
                    if (rp) free(joined);
                    free(w);
                }
            }
        }
        free(s);
    }
    buf_free(&out);
    buf_free(&err);
    return g_gd;
}

/* .git 下某个状态文件的**绝对路径**（UTF-8，反斜杠），给 mergeMsgPath 用。 */
static char *gd_path_u8(const char *name) {
    const wchar_t *gd = gitdir_w();
    if (!gd) return NULL;
    wchar_t *w = u82w(name);
    if (!w) return NULL;
    wchar_t *full = join_w(gd, w);
    free(w);
    char *u = w2u8(full);
    free(full);
    return u;
}

/* 读 .git 下的状态文件（read_text 语义）。不存在 → NULL。 */
static char *read_gd_text(const char *name) {
    const wchar_t *gd = gitdir_w();
    if (!gd) return NULL;
    wchar_t *w = u82w(name);
    if (!w) return NULL;
    wchar_t *full = join_w(gd, w);
    free(w);
    filebytes fb;
    read_plain_file(full, &fb);
    free(full);
    if (!fb.exists) { free(fb.raw); return NULL; }
    char *norm = normalize_newlines(fb.raw ? fb.raw : "", fb.rawlen);
    free(fb.raw);
    char *san = u8_sanitize(norm, strlen(norm));
    free(norm);
    return san;
}

/* ================================================================ HEAD 三态 */

typedef struct {
    int kind;      /* 0=unknown 1=branch 2=detached 3=unborn */
    char *branch;  /* branch 名（kind 1/3） */
    char *sha;     /* 提交 sha（kind 1/2） */
} headst;

/* 读 gd/<refname>（松散），没有就查 packed-refs。返回 malloc 的 sha 或 NULL。 */
static char *resolve_ref_loose(const wchar_t *gd, const char *refname) {
    wchar_t *w = u82w(refname);
    if (!w) return NULL;
    wchar_t *cur = _wcsdup(gd);
    /* 逐段拼（refname 里是 '/'） */
    for (wchar_t *p = w; ; ) {
        wchar_t *slash = wcschr(p, L'/');
        size_t seg = slash ? (size_t)(slash - p) : wcslen(p);
        wchar_t *segw = xmalloc((seg + 1) * sizeof(wchar_t));
        memcpy(segw, p, seg * sizeof(wchar_t));
        segw[seg] = 0;
        wchar_t *nxt = join_w(cur, segw);
        free(cur);
        free(segw);
        cur = nxt;
        if (!slash) break;
        p = slash + 1;
    }
    free(w);
    DWORD attr = GetFileAttributesW(cur);
    char *sha = NULL;
    if (attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
        filebytes fb;
        read_plain_file(cur, &fb);
        if (fb.exists) {
            char *t = py_strip_dup(fb.raw ? fb.raw : "");
            if (is_sha_text(t)) sha = xstrdup(t);
            free(t);
        }
        free(fb.raw);
    }
    free(cur);
    if (sha) return sha;

    /* packed-refs */
    wchar_t *packed = join_w(gd, L"packed-refs");
    filebytes fb;
    read_plain_file(packed, &fb);
    free(packed);
    if (!fb.exists) { free(fb.raw); return NULL; }
    char *text = fb.raw ? fb.raw : "";
    char *line = text;
    while (line && *line) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = 0;
        size_t ln = strlen(line);
        if (ln && line[ln - 1] == '\r') line[ln - 1] = 0;
        if (line[0] && line[0] != '#' && line[0] != '^') {
            char *sp = strchr(line, ' ');
            if (sp) {
                *sp = 0;
                if (!strcmp(sp + 1, refname) && is_sha_text(line)) {
                    sha = xstrdup(line);
                    *sp = ' ';
                    break;
                }
                *sp = ' ';
            }
        }
        line = nl ? nl + 1 : NULL;
    }
    free(fb.raw);
    return sha;
}

static void head_state(headst *hs) {
    memset(hs, 0, sizeof(*hs));
    const wchar_t *gd = dotgit_ok() ? gitdir_w() : NULL;
    if (gd) {
        wchar_t *hp = join_w(gd, L"HEAD");
        filebytes fb;
        read_plain_file(hp, &fb);
        free(hp);
        if (fb.exists) {
            char *raw = py_strip_dup(fb.raw ? fb.raw : "");
            free(fb.raw);
            if (!strncmp(raw, "ref: ", 5)) {
                char *ref = py_strip_dup(raw + 5);
                if (!strncmp(ref, "refs/heads/", 11) && ref[11]) {
                    char *sha = resolve_ref_loose(gd, ref);
                    if (!sha) {
                        hs->kind = 3;                       /* unborn */
                        hs->branch = xstrdup(ref + 11);
                    } else if (is_sha_text(sha)) {
                        hs->kind = 1;                       /* branch */
                        hs->branch = xstrdup(ref + 11);
                        hs->sha = sha;
                        sha = NULL;
                    }
                    free(sha);
                }
                free(ref);
            } else if (is_sha_text(raw)) {
                hs->kind = 2;                               /* detached */
                hs->sha = xstrdup(raw);
            }
            free(raw);
            if (hs->kind) return;
        }
    }
    /* 回退：git 命令（与 Python 的 else 分支同两条） */
    buf out, err;
    buf_init(&out);
    buf_init(&err);
    const char *a1[] = { "rev-parse", "--verify", "--quiet", "HEAD", NULL };
    if (run_git(&out, &err, a1) == 0) {
        char *t = py_strip_dup(buf_cstr(&out));
        if (*t) { hs->sha = xstrdup(t); hs->kind = 1; }
        free(t);
    }
    const char *a2[] = { "rev-parse", "--abbrev-ref", "HEAD", NULL };
    /* 未出生 HEAD 时 git 退出码非 0，但 stdout 仍然打印 "HEAD"；
     * Python 版不看 returncode（`q.stdout.strip() or None`），这里必须同口径，
     * 否则 unborn + GIT_DIR 回退会把 currentBranch 变成 null。 */
    run_git(&out, &err, a2);
    {
        char *t = py_strip_dup(buf_cstr(&out));
        if (*t) { hs->branch = xstrdup(t); }
        free(t);
    }
    buf_free(&out);
    buf_free(&err);
    if (hs->sha && !hs->branch) hs->kind = 2;
    else if (!hs->sha && hs->branch) hs->kind = 3;
    else if (!hs->sha && !hs->branch) hs->kind = 0;
    else hs->kind = 1;
}

static void head_state_free(headst *hs) {
    free(hs->branch);
    free(hs->sha);
    memset(hs, 0, sizeof(*hs));
}

/* ================================================================ 行处理 */

typedef struct {
    char **v;
    size_t n;
    int trailing_nl;
} lines_t;

static lines_t split_lines(const char *s, size_t n) {
    lines_t L;
    memset(&L, 0, sizeof(L));
    if (n == 0) return L;
    L.trailing_nl = (s[n - 1] == '\n');
    size_t cnt = 0;
    for (size_t i = 0; i < n; i++) if (s[i] == '\n') cnt++;
    if (!L.trailing_nl) cnt++;
    L.v = xmalloc(cnt * sizeof(char *));
    size_t pos = 0, k = 0;
    for (size_t i = 0; i <= n; i++) {
        if (i == n || s[i] == '\n') {
            if (i == n && L.trailing_nl) break;
            L.v[k++] = xstrndup(s + pos, i - pos);
            pos = i + 1;
        }
    }
    L.n = k;
    return L;
}

static void lines_free(lines_t *L) {
    if (!L->v) return;
    for (size_t i = 0; i < L->n; i++) free(L->v[i]);
    free(L->v);
    L->v = NULL;
    L->n = 0;
}

/* _join_lines：把若干行用 \n 连起来，trailing 为真时补一个 \n。 */
static char *join_lines(char *const *v, size_t n, int trailing) {
    buf b;
    buf_init(&b);
    for (size_t i = 0; i < n; i++) {
        if (i) buf_putc(&b, '\n');
        buf_puts(&b, v[i]);
    }
    if (trailing) buf_putc(&b, '\n');
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

/* 引用式行数组（不拥有字符串） */
typedef struct {
    const char **v;
    size_t n;
    size_t cap;
} rvec;

static void rvec_push(rvec *v, const char *s) {
    if (v->n == v->cap) {
        v->cap = v->cap ? v->cap * 2 : 8;
        v->v = xrealloc(v->v, v->cap * sizeof(char *));
    }
    v->v[v->n++] = s;
}

/* ================================================================ 标记匹配 */

/* 去掉行尾的空格/制表，返回 malloc 的组（空串 → NULL）。 */
static char *group_tail(const char *rest) {
    size_t n = strlen(rest);
    while (n && (rest[n - 1] == ' ' || rest[n - 1] == '\t')) n--;
    return n ? xstrndup(rest, n) : NULL;
}

/* ^<{7,}[ \t]*(.*?)[ \t]*$ */
static int m_ours(const char *line, char **label) {
    size_t i = 0;
    while (line[i] == '<') i++;
    if (i < 7) return 0;
    while (line[i] == ' ' || line[i] == '\t') i++;
    *label = group_tail(line + i);
    return 1;
}

/* ^={7,}[ \t]*$ */
static int m_sep(const char *line) {
    size_t i = 0;
    while (line[i] == '=') i++;
    if (i < 7) return 0;
    while (line[i] == ' ' || line[i] == '\t') i++;
    return line[i] == 0;
}

/* ^>{7,}[ \t]*:?[ \t]*(.*?)[ \t]*$ */
static int m_theirs(const char *line, char **label) {
    size_t i = 0;
    while (line[i] == '>') i++;
    if (i < 7) return 0;
    while (line[i] == ' ' || line[i] == '\t') i++;
    if (line[i] == ':') i++;
    while (line[i] == ' ' || line[i] == '\t') i++;
    *label = group_tail(line + i);
    return 1;
}

/* ^\|{7,}[ \t]*(.*?)[ \t]*$ */
static int m_base(const char *line) {
    size_t i = 0;
    while (line[i] == '|') i++;
    if (i < 7) return 0;
    return 1;
}

/* ================================================================ region */

typedef struct {
    long index;
    long oursMarkerLine;
    long sepLine;          /* -1 = null */
    long theirsMarkerLine;
    char *oursLabel;       /* NULL = null */
    char *theirsLabel;
    const char *style;
    char *oursText;
    char *theirsText;
    char *baseText;        /* NULL = null */
    char *baseOursText;
    char *baseTheirsText;
    long oursStart, oursEnd, theirsStart, theirsEnd, oursLines, theirsLines;

    long oursLineStart, oursLineEnd;
    long theirsLineStart, theirsLineEnd;
    long baseLineStart, baseLineEnd;
    char *oursLocatedText;
    char *theirsLocatedText;
    char *baseLocatedText;
    int verified;
} region;

static void region_free(region *r) {
    free(r->oursLabel);
    free(r->theirsLabel);
    free(r->oursText);
    free(r->theirsText);
    free(r->baseText);
    free(r->baseOursText);
    free(r->baseTheirsText);
    free(r->oursLocatedText);
    free(r->theirsLocatedText);
    free(r->baseLocatedText);
}

/* parse_markers 的忠实移植。anchors 收集「公共行」的 (1-based 行号, 文本)。 */
static void parse_markers(const lines_t *L, region **out_regs, size_t *out_n,
                          long **a_idx, const char ***a_txt, size_t *a_n) {
    region *regs = NULL;
    size_t nregs = 0;
    long *aidx = NULL;
    const char **atxt = NULL;
    size_t an = 0;

    rvec common = {0};
    size_t i = 0, n = L->n;
    while (i < n) {
        char *lab = NULL;
        if (!m_ours(L->v[i], &lab)) {
            /* 公共行：直接进锚点（顺序与 skeleton 里的 common 一致） */
            aidx = xrealloc(aidx, (an + 1) * sizeof(long));
            atxt = xrealloc(atxt, (an + 1) * sizeof(char *));
            aidx[an] = (long)i + 1;
            atxt[an] = L->v[i];
            an++;
            i++;
            continue;
        }
        char *ours_label = lab;
        long start = (long)i + 1;
        size_t j = i + 1;
        rvec ours = {0}, base = {0}, theirs = {0};
        const char *style = "merge";
        int seen_sep = 0;
        while (j < n) {
            char *l2 = NULL;
            if (m_ours(L->v[j], &l2)) { free(l2); break; }
            if (m_sep(L->v[j])) {
                if (!seen_sep) { seen_sep = 1; j++; continue; }
                rvec_push(&theirs, L->v[j]);
                j++;
                continue;
            }
            if (m_base(L->v[j]) && !seen_sep) {
                if (!strcmp(style, "merge")) style = "diff3";
                if (base.n == 0) {
                    j++;
                    while (j < n && !m_sep(L->v[j])) {
                        char *lt = NULL;
                        int is_t = m_theirs(L->v[j], &lt);
                        free(lt);
                        if (is_t) break;
                        if (m_base(L->v[j])) { base.n = 0; j++; continue; }
                        rvec_push(&base, L->v[j]);
                        j++;
                    }
                    continue;
                }
                j++;
                continue;
            }
            {
                char *lt = NULL;
                int is_t = m_theirs(L->v[j], &lt);
                free(lt);
                if (is_t && seen_sep) break;
            }
            if (seen_sep) rvec_push(&theirs, L->v[j]);
            else rvec_push(&ours, L->v[j]);
            j++;
        }

        long end = (j < n) ? (long)j + 1 : (long)j;
        char *theirs_label = NULL;
        if (j < n) {
            char *lt = NULL;
            if (m_theirs(L->v[j], &lt)) theirs_label = lt;
            else free(lt);
        }

        regs = xrealloc(regs, (nregs + 1) * sizeof(region));
        region *r = &regs[nregs];
        memset(r, 0, sizeof(*r));
        r->index = (long)nregs;
        r->oursMarkerLine = start;
        r->sepLine = seen_sep ? (start + 1 + (long)ours.n) : -1;
        r->theirsMarkerLine = end;
        r->oursLabel = ours_label;
        r->theirsLabel = theirs_label;
        r->style = style;
        r->oursText = join_lines((char *const *)ours.v, ours.n, ours.n > 0);
        r->theirsText = join_lines((char *const *)theirs.v, theirs.n, theirs.n > 0);
        r->baseText = strcmp(style, "merge") ? join_lines((char *const *)base.v, base.n, base.n > 0) : NULL;
        r->baseOursText = NULL;      /* Python 版这条分支实际不可达，恒 null */
        r->baseTheirsText = NULL;
        r->oursStart = start + 1;
        r->oursEnd = start + (long)ours.n;
        r->theirsStart = seen_sep ? (start + 1 + (long)ours.n + 1)
                                  : (start + (long)ours.n + 1);
        r->theirsEnd = seen_sep ? (start + 1 + (long)ours.n + 1 + (long)theirs.n - 1)
                                : (start + (long)ours.n + 1 + (long)theirs.n - 1);
        r->oursLines = (long)ours.n;
        r->theirsLines = (long)theirs.n;
        r->oursLineStart = r->oursLineEnd = -1;
        r->theirsLineStart = r->theirsLineEnd = -1;
        r->baseLineStart = r->baseLineEnd = -1;
        nregs++;

        free(ours.v);
        free(base.v);
        free(theirs.v);
        free(common.v);
        common.v = NULL; common.n = common.cap = 0;

        i = (j < n) ? j + 1 : n;
    }
    free(common.v);
    *out_regs = regs;
    *out_n = nregs;
    *a_idx = aidx;
    *a_txt = atxt;
    *a_n = an;
}

/* ================================================================ 对齐 */

/* difflib.find_longest_match 的等价实现（无 junk，故后缀扩展是空操作）。 */
static void flm(const char **a, long alo, long ahi, char **b, long blo, long bhi,
                long *oi, long *oj, long *ok) {
    long besti = alo, bestj = blo, bestsize = 0;
    long m = bhi - blo;
    if (m <= 0 || ahi <= alo) { *oi = besti; *oj = bestj; *ok = 0; return; }
    long *prev = xmalloc((size_t)m * sizeof(long));
    long *cur = xmalloc((size_t)m * sizeof(long));
    memset(prev, 0, (size_t)m * sizeof(long));
    for (long i = alo; i < ahi; i++) {
        memset(cur, 0, (size_t)m * sizeof(long));
        for (long j = blo; j < bhi; j++) {
            if (!strcmp(a[i], b[j])) {
                long k = (j > blo ? prev[j - blo - 1] : 0) + 1;
                cur[j - blo] = k;
                if (k > bestsize) {
                    besti = i - k + 1;
                    bestj = j - k + 1;
                    bestsize = k;
                }
            }
        }
        long *t = prev;
        prev = cur;
        cur = t;
    }
    free(prev);
    free(cur);
    *oi = besti;
    *oj = bestj;
    *ok = bestsize;
}

typedef struct { long alo, ahi, blo, bhi; } rng;

/* 超大文件（na*nb > 4e6）时的降级：单调贪心匹配。正常冲突文件走不到这里。 */
static void align_greedy(const char **a, size_t na, char **b, size_t nb, long *map) {
    size_t j = 0;
    for (size_t i = 0; i < na; i++) {
        for (size_t k = j; k < nb; k++) {
            if (!strcmp(a[i], b[k])) { map[i] = (long)k; j = k + 1; break; }
        }
    }
}

static void align_map(const char **a, size_t na, char **b, size_t nb, long *map) {
    for (size_t k = 0; k < na; k++) map[k] = -1;
    if (!na || !nb) return;
    if ((double)na * (double)nb > 4000000.0) { align_greedy(a, na, b, nb, map); return; }
    rng *stack = NULL;
    size_t sn = 0, scap = 0;
    stack = xrealloc(stack, sizeof(rng));
    scap = 1;
    stack[sn++] = (rng){0, (long)na, 0, (long)nb};
    while (sn) {
        rng r = stack[--sn];
        long i, j, k;
        flm(a, r.alo, r.ahi, b, r.blo, r.bhi, &i, &j, &k);
        if (!k) continue;
        for (long t = 0; t < k; t++) map[i + t] = j + t;
        if (r.alo < i && r.blo < j) {
            if (sn == scap) { scap *= 2; stack = xrealloc(stack, scap * sizeof(rng)); }
            stack[sn++] = (rng){r.alo, i, r.blo, j};
        }
        if (i + k < r.ahi && j + k < r.bhi) {
            if (sn == scap) { scap *= 2; stack = xrealloc(stack, scap * sizeof(rng)); }
            stack[sn++] = (rng){i + k, r.ahi, j + k, r.bhi};
        }
    }
    free(stack);
}

/* _locate 的移植。返回 0 表示 null 三元组。 */
static int locate_region(const region *r, const lines_t *T, const long *map,
                         const long *aidx, size_t an,
                         long *s, long *e, char **text) {
    *s = *e = -1;
    *text = NULL;
    if (!T->n) return 0;
    long before = -1, after = -1;
    for (size_t k = 0; k < an; k++) {
        if (aidx[k] >= r->oursMarkerLine) break;
        if (map[k] >= 0) before = (long)k;
    }
    for (size_t k = an; k-- > 0; ) {
        if (aidx[k] <= r->theirsMarkerLine) break;
        if (map[k] >= 0) { after = (long)k; }
    }
    long b_start = (before >= 0) ? map[before] + 1 : 0;
    long b_end = (after >= 0) ? map[after] - 1 : (long)T->n - 1;
    if (b_end < b_start - 1) return 0;
    long cnt = b_end - b_start + 1;
    if (cnt < 0) cnt = 0;
    buf b;
    buf_init(&b);
    for (long t = 0; t < cnt; t++) {
        if (t) buf_putc(&b, '\n');
        buf_puts(&b, T->v[b_start + t]);
    }
    int trailing = (b_end + 1 < (long)T->n);
    if (trailing) buf_putc(&b, '\n');
    *text = xstrdup(buf_cstr(&b));
    buf_free(&b);
    *s = b_start + 1;
    *e = b_end + 1;
    return 1;
}

static int rstrip_nl_eq(const char *a, const char *b) {
    if (!a || !b) return 0;
    size_t na = strlen(a), nb = strlen(b);
    while (na && a[na - 1] == '\n') na--;
    while (nb && b[nb - 1] == '\n') nb--;
    return na == nb && !memcmp(a, b, na);
}

/* ================================================================ blob */

typedef struct {
    int exists;
    char *sha;
    char *raw;
    size_t rawlen;
    int binary;
    int stage;
    char *mode;
} stage_blob;

static void blob_free(stage_blob *b) {
    free(b->sha);
    free(b->raw);
    free(b->mode);
    memset(b, 0, sizeof(*b));
}

/* git cat-file blob <sha>；失败 → (None, True)（与 Python _blob 同语义）。 */
static void fetch_blob(const char *sha, stage_blob *out) {
    out->exists = 0;
    out->sha = xstrdup(sha);
    out->raw = NULL;
    out->rawlen = 0;
    out->binary = 1;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "cat-file", "blob", sha, NULL };
    int rc = run_git(&o, &e, args);
    if (rc == 0) {
        out->exists = 1;
        out->raw = xmalloc(o.n + 1);
        if (o.n) memcpy(out->raw, o.p, o.n);
        out->raw[o.n] = 0;
        out->rawlen = o.n;
        out->binary = 0;
        size_t probe = o.n < 8000 ? o.n : 8000;
        for (size_t i = 0; i < probe; i++) if (out->raw[i] == 0) { out->binary = 1; break; }
    }
    buf_free(&o);
    buf_free(&e);
}

/* git show <rev>:<path>；失败 → exists=0。 */
static void fetch_show(const char *rev, const char *path, int *exists, int *binary,
                       char **raw, size_t *rawlen) {
    buf spec;
    buf_init(&spec);
    buf_printf(&spec, "%s:%s", rev, path);
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "show", "--no-ext-diff", "--no-textconv", buf_cstr(&spec), NULL };
    int rc = run_git(&o, &e, args);
    *exists = 0;
    *binary = 0;
    *raw = NULL;
    *rawlen = 0;
    if (rc == 0) {
        *exists = 1;
        *raw = xmalloc(o.n + 1);
        if (o.n) memcpy(*raw, o.p, o.n);
        (*raw)[o.n] = 0;
        *rawlen = o.n;
        size_t probe = o.n < 8000 ? o.n : 8000;
        for (size_t i = 0; i < probe; i++) if ((*raw)[i] == 0) { *binary = 1; break; }
    }
    buf_free(&o);
    buf_free(&e);
    buf_free(&spec);
}

/* merge-base HEAD MERGE_HEAD → (sha|NULL, reason|NULL) */
static char *merge_base(char **reason) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "merge-base", "HEAD", "MERGE_HEAD", NULL };
    int rc = run_git(&o, &e, args);
    char *t = py_strip_dup(buf_cstr(&o));
    char *out = NULL;
    *reason = NULL;
    if (rc == 0 && *t) out = xstrdup(t);
    else {
        buf r;
        buf_init(&r);
        buf_printf(&r, "no-merge-base(rc=%d)", rc);
        *reason = xstrdup(buf_cstr(&r));
        buf_free(&r);
    }
    free(t);
    buf_free(&o);
    buf_free(&e);
    return out;
}

/* ================================================================ 解析+对齐 */

typedef struct {
    region *regs;
    size_t n;
    int has_markers;
    int verified;       /* -1 = null */
    const char *ours_label;
    const char *theirs_label;
} parsed;

static void parsed_free(parsed *p) {
    for (size_t i = 0; i < p->n; i++) region_free(&p->regs[i]);
    free(p->regs);
    memset(p, 0, sizeof(*p));
}

static void parse_and_attach(const char *wt_text, const stage_blob *base,
                             const stage_blob *ours, const stage_blob *theirs,
                             parsed *p) {
    memset(p, 0, sizeof(*p));
    p->verified = -1;
    lines_t wt = split_lines(wt_text ? wt_text : "", wt_text ? strlen(wt_text) : 0);
    long *aidx = NULL;
    const char **atxt = NULL;
    size_t an = 0;
    parse_markers(&wt, &p->regs, &p->n, &aidx, &atxt, &an);
    p->has_markers = p->n > 0;

    /* 三侧行 */
    lines_t L[3];
    int have[3];
    const stage_blob *bs[3] = { ours, theirs, base };
    for (int k = 0; k < 3; k++) {
        memset(&L[k], 0, sizeof(L[k]));
        have[k] = (bs[k] && bs[k]->raw && !bs[k]->binary);
        if (have[k]) {
            char *txt = u8_sanitize(bs[k]->raw, bs[k]->rawlen);
            L[k] = split_lines(txt, strlen(txt));
            free(txt);
        }
    }
    long *maps[3] = { NULL, NULL, NULL };
    for (int k = 0; k < 3; k++) {
        if (have[k] && L[k].n) {
            maps[k] = xmalloc((an ? an : 1) * sizeof(long));
            align_map(atxt, an, L[k].v, L[k].n, maps[k]);
        }
    }
    for (size_t ri = 0; ri < p->n; ri++) {
        region *r = &p->regs[ri];
        char *got[3] = { NULL, NULL, NULL };
        long *st[3] = { &r->oursLineStart, &r->theirsLineStart, &r->baseLineStart };
        long *en[3] = { &r->oursLineEnd, &r->theirsLineEnd, &r->baseLineEnd };
        char **tx[3] = { &r->oursLocatedText, &r->theirsLocatedText, &r->baseLocatedText };
        for (int k = 0; k < 3; k++) {
            if (!have[k]) { *st[k] = -1; *en[k] = -1; *tx[k] = NULL; continue; }
            locate_region(r, &L[k], maps[k] ? maps[k] : NULL, aidx, an, st[k], en[k], tx[k]);
            got[k] = *tx[k];
        }
        int ok = 1;
        for (int k = 0; k < 2; k++) {
            if (!got[k]) { ok = 0; break; }
            const char *want = (k == 0) ? r->oursText : r->theirsText;
            if (!rstrip_nl_eq(got[k], want)) { ok = 0; break; }
        }
        if (ok && base && base->raw && !base->binary && !got[2]) ok = 0;
        r->verified = ok;
    }
    int all = 1;
    for (size_t ri = 0; ri < p->n; ri++) if (!p->regs[ri].verified) all = 0;
    p->verified = p->n ? all : -1;
    p->ours_label = p->n ? p->regs[0].oursLabel : NULL;
    p->theirs_label = p->n ? p->regs[0].theirsLabel : NULL;

    for (int k = 0; k < 3; k++) {
        free(maps[k]);
        lines_free(&L[k]);
    }
    free(aidx);
    free(atxt);
    lines_free(&wt);
}

/* ================================================================ JSON 片段 */

static void j_str_or_null(buf *o, const char *s) {
    if (s) json_str(o, s);
    else buf_puts(o, "null");
}

static void j_long_or_null(buf *o, long v) {
    if (v < 0) buf_puts(o, "null");
    else buf_printf(o, "%ld", v);
}

static void j_bool(buf *o, int v) { buf_puts(o, v ? "true" : "false"); }

/* Python 的 `raw == idx[key]["raw"]`：两边都是 None 时算相等（删除/新增混合）。 */
static int raw_eq(int ax, const char *a, size_t an, int bx, const char *b, size_t bn) {
    if (!ax && !bx) return 1;
    if (!ax || !bx) return 0;
    if (an != bn) return 0;
    return an == 0 || !memcmp(a, b, an);
}

/* ================================================================ 远程能力闸 */

static const char *READ_OPS[] = {
    "archive --exec", "archive --remote", "fetch", "fetch-pack", "ls-remote",
    "remote show", "remote update", "remote-ext", "remote-fd", NULL
};
static const char *WRITE_OPS[] = {
    "clone", "credential", "credential-cache", "credential-store", "daemon",
    "http-backend", "pull", "push", "receive-pack", "request-pull",
    "send-pack", "submodule", "upload-pack", NULL
};
#define LOCAL_VERBS_COUNT 55

static int ascii_lower_eq(const char *a, const char *b) {
    while (*a && *b) {
        int ca = tolower((unsigned char)*a), cb = tolower((unsigned char)*b);
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

static void gate_json(buf *o) {
    const char *level = "off";
    const char *source = "default";
    char *note = NULL;
    const char *raw = getenv("AIGIT_NET");
    if (raw) {
        source = "env:AIGIT_NET";
        char *t = py_strip_dup(raw);
        const char *lv = NULL;
        if (!*t || !strcmp(t, "0") || ascii_lower_eq(t, "off") ||
            ascii_lower_eq(t, "none") || ascii_lower_eq(t, "no") ||
            ascii_lower_eq(t, "false")) lv = "off";
        else if (!strcmp(t, "1") || ascii_lower_eq(t, "read") ||
                 ascii_lower_eq(t, "on") || ascii_lower_eq(t, "yes") ||
                 ascii_lower_eq(t, "true") || ascii_lower_eq(t, "ro")) lv = "read";
        else if (!strcmp(t, "2") || ascii_lower_eq(t, "write") ||
                 ascii_lower_eq(t, "rw") || ascii_lower_eq(t, "push")) lv = "write";
        if (!lv) {
            lv = "off";
            char *rp = py_repr_n(raw, strlen(raw));
            buf nb;
            buf_init(&nb);
            buf_printf(&nb, "AIGIT_NET=%s 读不懂(只认 off/read/write),已按 off 处理。", rp);
            note = xstrdup(buf_cstr(&nb));
            buf_free(&nb);
            free(rp);
        }
        level = lv;
        free(t);
    }
    int allows_read = strcmp(level, "read") == 0 || strcmp(level, "write") == 0;
    int allows_write = strcmp(level, "write") == 0;
    buf_puts(o, "{\"level\":");
    json_str(o, level);
    buf_puts(o, ",\"source\":");
    json_str(o, source);
    buf_puts(o, ",\"env_var\":\"AIGIT_NET\",\"levels\":[\"off\",\"read\",\"write\"],\"allows_read\":");
    j_bool(o, allows_read);
    buf_puts(o, ",\"allows_write\":");
    j_bool(o, allows_write);
    buf_puts(o, ",\"read_ops\":[");
    for (int i = 0; READ_OPS[i]; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, READ_OPS[i]);
    }
    buf_puts(o, "],\"write_ops\":[");
    for (int i = 0; WRITE_OPS[i]; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, WRITE_OPS[i]);
    }
    buf_printf(o, "],\"local_verbs\":%d", LOCAL_VERBS_COUNT);
    if (note) {
        buf_puts(o, ",\"note\":");
        json_str(o, note);
        free(note);
    }
    buf_putc(o, '}');
}

/* ================================================================ 冲突清单 */

typedef struct {
    char *path;
    char *mode[4];
    char *sha[4];
} stpath;

static void stpaths_free(stpath *v, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(v[i].path);
        for (int k = 1; k <= 3; k++) { free(v[i].mode[k]); free(v[i].sha[k]); }
    }
    free(v);
}

/* git ls-files -u -z（可带 path 过滤）→ stpath 数组 */
static stpath *ls_files_unmerged(const char *path_filter, size_t *out_n) {
    *out_n = 0;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[8];
    int ai = 0;
    args[ai++] = "--literal-pathspecs";
    args[ai++] = "ls-files";
    args[ai++] = "-u";
    args[ai++] = "-z";
    if (path_filter) { args[ai++] = "--"; args[ai++] = path_filter; }
    args[ai] = NULL;
    run_git(&o, &e, args);
    stpath *out = NULL;
    size_t n = 0;
    const char *p = buf_cstr(&o);
    size_t len = o.n;
    size_t pos = 0;
    while (pos < len) {
        const char *rec = p + pos;
        size_t rl = 0;
        while (pos + rl < len && rec[rl]) rl++;
        pos += rl + 1;
        if (!rl) continue;
        const char *tab = memchr(rec, '\t', rl);
        if (!tab) continue;
        size_t mlen = (size_t)(tab - rec);
        char *meta = xstrndup(rec, mlen);
        char *name = xstrndup(tab + 1, rl - mlen - 1);
        size_t nf = 0;
        char **f = ws_split(meta, &nf);
        free(meta);
        if (nf != 3) { free_strv(f, nf); free(name); continue; }
        int stage = atoi(f[2]);
        if (stage < 1 || stage > 3) { free_strv(f, nf); free(name); continue; }
        size_t idx = (size_t)-1;
        for (size_t k = 0; k < n; k++) if (!strcmp(out[k].path, name)) { idx = k; break; }
        if (idx == (size_t)-1) {
            out = xrealloc(out, (n + 1) * sizeof(stpath));
            memset(&out[n], 0, sizeof(stpath));
            out[n].path = xstrdup(name);
            idx = n;
            n++;
        }
        free(out[idx].mode[stage]);
        free(out[idx].sha[stage]);
        out[idx].mode[stage] = xstrdup(f[0]);
        out[idx].sha[stage] = xstrdup(f[1]);
        free_strv(f, nf);
        free(name);
    }
    buf_free(&o);
    buf_free(&e);
    *out_n = n;
    return out;
}

static int merge_in_progress(void) {
    char *mh = read_gd_text("MERGE_HEAD");
    if (!mh) return 0;
    size_t n = 0;
    char **f = ws_split(mh, &n);
    int in_merge = n > 0;
    free_strv(f, n);
    free(mh);
    return in_merge;
}

/* 单个冲突文件的摘要 + 全量（inspect 用）。返回 0 = 没有 stage（调用方回 400）。 */
static int build_file_detail(const char *rel, stpath *st, buf *out, int full) {
    stage_blob idx[3];
    memset(idx, 0, sizeof(idx));
    const int stages[3] = { 1, 2, 3 };
    for (int k = 0; k < 3; k++) {
        int sg = stages[k];
        idx[k].stage = sg;
        if (!st->sha[sg]) {
            idx[k].exists = 0;
            idx[k].sha = NULL;
            idx[k].raw = NULL;
            idx[k].binary = 0;
            idx[k].mode = NULL;
            continue;
        }
        fetch_blob(st->sha[sg], &idx[k]);
        idx[k].mode = xstrdup(st->mode[sg]);
    }
    const stage_blob *base = &idx[0], *ours = &idx[1], *theirs = &idx[2];

    char *mb_reason = NULL;
    char *mb = merge_base(&mb_reason);

    /* 交叉校验 */
    int ox = 0, ob = 0, tx = 0, tb = 0, bx = 0, bb = 0;
    char *oraw = NULL, *traw = NULL, *braw = NULL;
    size_t orl = 0, trl = 0, brl = 0;
    fetch_show("HEAD", rel, &ox, &ob, &oraw, &orl);
    fetch_show("MERGE_HEAD", rel, &tx, &tb, &traw, &trl);
    if (mb) fetch_show(mb, rel, &bx, &bb, &braw, &brl);

    int mismatch_ours = (ox && !ours->exists);
    int mismatch_theirs = (tx && !theirs->exists);
    int mismatch_base = (mb && bx && !base->exists);

    int no_base = (base->raw == NULL);
    int binary = base->binary || ours->binary || theirs->binary;

    /* 工作区 */
    wchar_t *wp = repo_wpath(rel);
    filebytes fb;
    memset(&fb, 0, sizeof(fb));
    if (wp) read_plain_file(wp, &fb);
    free(wp);
    char *wt_text = NULL;
    if (!binary) {
        char *dec = u8_sanitize(fb.raw ? fb.raw : "", fb.rawlen);
        wt_text = dec;
    }
    parsed p;
    parse_and_attach(wt_text, base, ours, theirs, &p);

    long wt_lines = -1;
    if (wt_text) {
        lines_t L = split_lines(wt_text, strlen(wt_text));
        wt_lines = (long)L.n;
        lines_free(&L);
    }
    char shahex[65];
    sha256_hex(fb.raw ? fb.raw : "", fb.rawlen, shahex);

    if (!full) {
        /* ---- list_conflicts 的一项 ---- */
        buf_puts(out, "{\"path\":");
        json_str(out, rel);
        buf_puts(out, ",\"name\":");
        const char *slash = strrchr(rel, '/');
        json_str(out, slash ? slash + 1 : rel);
        buf_puts(out, ",\"noBase\":");
        j_bool(out, no_base);
        buf_puts(out, ",\"binary\":");
        j_bool(out, binary);
        buf_printf(out, ",\"regionCount\":%zu", p.n);
        buf_puts(out, ",\"manuallyEdited\":");
        j_bool(out, p.n == 0 && st->sha[1]);
        buf_puts(out, ",\"verified\":");
        if (p.verified < 0) buf_puts(out, "null"); else j_bool(out, p.verified);
        buf_puts(out, ",\"hasMarkers\":");
        j_bool(out, p.has_markers);
        buf_printf(out, ",\"columns\":%d", no_base ? 2 : 3);
        buf_puts(out, ",\"oursLabel\":");
        j_str_or_null(out, p.ours_label);
        buf_puts(out, ",\"theirsLabel\":");
        j_str_or_null(out, p.theirs_label);
        buf_puts(out, ",\"baseDeleted\":");
        j_bool(out, !base->exists);
        buf_puts(out, ",\"oursDeleted\":");
        j_bool(out, !ours->exists);
        buf_puts(out, ",\"theirsDeleted\":");
        j_bool(out, !theirs->exists);
        buf_puts(out, ",\"crossCheckMismatch\":[");
        int first = 1;
        if (mismatch_ours) { buf_puts(out, "\"ours\""); first = 0; }
        if (mismatch_theirs) { if (!first) buf_putc(out, ','); buf_puts(out, "\"theirs\""); first = 0; }
        if (mismatch_base) { if (!first) buf_putc(out, ','); buf_puts(out, "\"base\""); }
        buf_puts(out, "]}");
    } else {
        /* ---- inspect 全量 ---- */
        buf_puts(out, "{\"ok\":true,\"path\":");
        json_str(out, rel);
        buf_puts(out, ",\"inMerge\":true,\"noBase\":");
        j_bool(out, no_base);
        buf_puts(out, ",\"noBaseReason\":");
        j_str_or_null(out, mb ? NULL : mb_reason);
        buf_puts(out, ",\"binary\":");
        j_bool(out, binary);
        buf_puts(out, ",\"mode\":");
        if (st->mode[2]) j_str_or_null(out, st->mode[2]);
        else j_str_or_null(out, st->mode[3]);
        buf_puts(out, ",\"mergeBase\":");
        j_str_or_null(out, mb);
        buf_puts(out, ",\"theirsLabel\":");
        j_str_or_null(out, p.theirs_label);
        buf_puts(out, ",\"oursLabel\":");
        j_str_or_null(out, p.ours_label);
        buf_printf(out, ",\"columns\":%d", no_base ? 2 : 3);

        buf_puts(out, ",\"stages\":{");
        int first = 1;
        for (int sg = 1; sg <= 3; sg++) {
            if (!st->sha[sg]) continue;
            if (!first) buf_putc(out, ',');
            first = 0;
            buf_printf(out, "\"%d\":{\"mode\":", sg);
            json_str(out, st->mode[sg]);
            buf_puts(out, ",\"sha\":");
            json_str(out, st->sha[sg]);
            buf_puts(out, ",\"path\":");
            json_str(out, rel);
            buf_putc(out, '}');
        }
        buf_puts(out, "},\"sides\":{");

        const char *keys[3] = { "ours", "theirs", "base" };
        const stage_blob *blobs[3] = { ours, theirs, base };
        for (int k = 0; k < 3; k++) {
            if (k) buf_putc(out, ',');
            const stage_blob *b = blobs[k];
            buf_puts(out, "\"");
            buf_puts(out, keys[k]);
            buf_puts(out, "\":{\"key\":\"");
            buf_puts(out, keys[k]);
            buf_puts(out, "\",\"ref\":");
            if (k == 0) json_str(out, "HEAD");
            else if (k == 1) json_str(out, "MERGE_HEAD");
            else j_str_or_null(out, mb);
            buf_puts(out, ",\"label\":");
            if (k == 0) j_str_or_null(out, p.ours_label ? p.ours_label : "HEAD");
            else if (k == 1) j_str_or_null(out, p.theirs_label);
            else if (no_base) buf_puts(out, "null");
            else {
                char tmp[11];
                size_t n = strlen(mb);
                if (n > 10) n = 10;
                memcpy(tmp, mb, n);
                tmp[n] = 0;
                json_str(out, tmp);
            }
            buf_puts(out, ",\"exists\":");
            j_bool(out, b->exists);
            buf_puts(out, ",\"deleted\":");
            j_bool(out, !b->exists);
            buf_puts(out, ",\"binary\":");
            j_bool(out, b->binary);
            buf_puts(out, ",\"mode\":");
            j_str_or_null(out, b->mode);
            buf_puts(out, ",\"sha\":");
            j_str_or_null(out, b->sha);
            buf_printf(out, ",\"stage\":%d,\"text\":", b->stage);
            char *side_text = NULL;
            long side_lines = -1;
            if (b->raw && !b->binary) {
                side_text = u8_sanitize(b->raw, b->rawlen);
                if (b->rawlen > 0) {
                    lines_t L = split_lines(side_text, strlen(side_text));
                    side_lines = (long)L.n;
                    lines_free(&L);
                }
            }
            j_str_or_null(out, side_text);
            buf_puts(out, ",\"lineCount\":");
            j_long_or_null(out, side_lines);
            buf_putc(out, '}');
            free(side_text);
        }
        buf_puts(out, "},\"worktree\":{\"exists\":");
        j_bool(out, fb.exists);
        buf_puts(out, ",\"text\":");
        j_str_or_null(out, wt_text);
        buf_puts(out, ",\"lineCount\":");
        j_long_or_null(out, wt_lines);
        buf_puts(out, ",\"sha256\":\"");
        buf_puts(out, shahex);
        buf_printf(out, "\",\"byteLength\":%zu,\"hasMarkers\":", fb.rawlen);
        j_bool(out, p.has_markers);
        buf_puts(out, ",\"manuallyEdited\":");
        j_bool(out, p.n == 0 && st->sha[1]);
        buf_puts(out, "},\"regions\":[");
        for (size_t ri = 0; ri < p.n; ri++) {
            region *r = &p.regs[ri];
            if (ri) buf_putc(out, ',');
            buf_printf(out, "{\"index\":%ld,\"oursMarkerLine\":%ld,\"sepLine\":", r->index, r->oursMarkerLine);
            j_long_or_null(out, r->sepLine);
            buf_printf(out, ",\"theirsMarkerLine\":%ld,\"oursLabel\":", r->theirsMarkerLine);
            j_str_or_null(out, r->oursLabel);
            buf_puts(out, ",\"theirsLabel\":");
            j_str_or_null(out, r->theirsLabel);
            buf_puts(out, ",\"style\":");
            json_str(out, r->style);
            buf_puts(out, ",\"oursText\":");
            json_str(out, r->oursText);
            buf_puts(out, ",\"theirsText\":");
            json_str(out, r->theirsText);
            buf_puts(out, ",\"baseText\":");
            j_str_or_null(out, r->baseText);
            buf_puts(out, ",\"baseOursText\":");
            j_str_or_null(out, r->baseOursText);
            buf_puts(out, ",\"baseTheirsText\":");
            j_str_or_null(out, r->baseTheirsText);
            buf_printf(out, ",\"oursStart\":%ld,\"oursEnd\":%ld,\"theirsStart\":%ld,\"theirsEnd\":%ld",
                       r->oursStart, r->oursEnd, r->theirsStart, r->theirsEnd);
            buf_printf(out, ",\"oursLines\":%ld,\"theirsLines\":%ld", r->oursLines, r->theirsLines);
            buf_puts(out, ",\"oursLineStart\":");
            j_long_or_null(out, r->oursLineStart);
            buf_puts(out, ",\"oursLineEnd\":");
            j_long_or_null(out, r->oursLineEnd);
            buf_puts(out, ",\"oursLocatedText\":");
            j_str_or_null(out, r->oursLocatedText);
            buf_puts(out, ",\"theirsLineStart\":");
            j_long_or_null(out, r->theirsLineStart);
            buf_puts(out, ",\"theirsLineEnd\":");
            j_long_or_null(out, r->theirsLineEnd);
            buf_puts(out, ",\"theirsLocatedText\":");
            j_str_or_null(out, r->theirsLocatedText);
            buf_puts(out, ",\"baseLineStart\":");
            j_long_or_null(out, r->baseLineStart);
            buf_puts(out, ",\"baseLineEnd\":");
            j_long_or_null(out, r->baseLineEnd);
            buf_puts(out, ",\"baseLocatedText\":");
            j_str_or_null(out, r->baseLocatedText);
            buf_puts(out, ",\"verified\":");
            j_bool(out, r->verified);
            buf_putc(out, '}');
        }
        buf_printf(out, "],\"regionCount\":%zu,\"verified\":", p.n);
        if (p.verified < 0) buf_puts(out, "null"); else j_bool(out, p.verified);

        buf_puts(out, ",\"crossCheck\":{\"ours\":{\"ref\":\"HEAD\",\"exists\":");
        j_bool(out, ox);
        buf_puts(out, ",\"binary\":");
        j_bool(out, ob);
        buf_puts(out, ",\"matchesIndex\":");
        j_bool(out, raw_eq(ours->exists, ours->raw, ours->rawlen, ox, oraw, orl));
        buf_puts(out, "},\"theirs\":{\"ref\":\"MERGE_HEAD\",\"exists\":");
        j_bool(out, tx);
        buf_puts(out, ",\"binary\":");
        j_bool(out, tb);
        buf_puts(out, ",\"matchesIndex\":");
        j_bool(out, raw_eq(theirs->exists, theirs->raw, theirs->rawlen, tx, traw, trl));
        buf_puts(out, "},\"base\":");
        if (mb) {
            buf_puts(out, "{\"ref\":");
            json_str(out, mb);
            buf_puts(out, ",\"exists\":");
            j_bool(out, bx);
            buf_puts(out, ",\"binary\":");
            j_bool(out, bb);
            buf_puts(out, ",\"matchesIndex\":");
            j_bool(out, raw_eq(base->exists, base->raw, base->rawlen, bx, braw, brl));
            buf_putc(out, '}');
        } else {
            buf_puts(out, "{\"ref\":null,\"exists\":false,\"binary\":false,\"matchesIndex\":");
            j_bool(out, base->raw == NULL);
            buf_puts(out, ",\"reason\":");
            json_str(out, mb_reason);
            buf_putc(out, '}');
        }
        buf_puts(out, "},\"crossCheckMismatch\":[");
        {
            int firstm = 1;
            if (mismatch_ours) { buf_puts(out, "\"ours\""); firstm = 0; }
            if (mismatch_theirs) { if (!firstm) buf_putc(out, ','); buf_puts(out, "\"theirs\""); firstm = 0; }
            if (mismatch_base) { if (!firstm) buf_putc(out, ','); buf_puts(out, "\"base\""); }
        }
        buf_puts(out, "]}");
    }

    parsed_free(&p);
    free(wt_text);
    free(fb.raw);
    free(oraw);
    free(traw);
    free(braw);
    free(mb);
    free(mb_reason);
    for (int k = 0; k < 3; k++) blob_free(&idx[k]);
    return 1;
}

/* 冲突文件清单 → files 数组 JSON；*count 是文件数。 */
static void conflict_files_json(buf *out, long *count) {
    *count = 0;
    buf_puts(out, "[");
    /* Python 的空清单快路径：索引里没有 stage≠0 就是空，0 spawn。 */
    {
        idx_names ix;
        if (idx_read(&ix)) {
            int any = ix.has_unmerged;
            idx_free(&ix);
            if (!any) { buf_puts(out, "]"); return; }
        }
    }
    size_t nst = 0;
    stpath *st = ls_files_unmerged(NULL, &nst);
    if (nst == 0) {
        stpaths_free(st, nst);
        buf_puts(out, "]");
        return;
    }
    /* diff --name-only --diff-filter=U -z */
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "--literal-pathspecs", "diff", "--name-only",
                           "--diff-filter=U", "-z", NULL };
    run_git(&o, &e, args);
    char **paths = NULL;
    size_t np = 0;
    const char *p = buf_cstr(&o);
    size_t len = o.n, pos = 0;
    while (pos < len) {
        const char *rec = p + pos;
        size_t rl = 0;
        while (pos + rl < len && rec[rl]) rl++;
        pos += rl + 1;
        if (!rl) continue;
        char *t = xstrndup(rec, rl);
        char *s = py_strip_dup(t);
        free(t);
        if (*s) {
            paths = xrealloc(paths, (np + 1) * sizeof(char *));
            paths[np++] = s;
        } else free(s);
    }
    buf_free(&o);
    buf_free(&e);
    if (np == 0) {
        stpaths_free(st, nst);
        free_strv(paths, np);
        buf_puts(out, "]");
        return;
    }
    long first = 1;
    for (size_t i = 0; i < np; i++) {
        stpath *hit = NULL;
        for (size_t k = 0; k < nst; k++) if (!strcmp(st[k].path, paths[i])) { hit = &st[k]; break; }
        if (!first) buf_putc(out, ',');
        first = 0;
        if (!hit) {
            buf_puts(out, "{\"path\":");
            json_str(out, paths[i]);
            buf_puts(out, ",\"name\":");
            const char *slash = strrchr(paths[i], '/');
            json_str(out, slash ? slash + 1 : paths[i]);
            buf_puts(out, ",\"error\":");
            buf mp;
            buf_init(&mp);
            buf_printf(&mp, "该路径没有未合并的索引条目,不是冲突文件:%s", paths[i]);
            json_str(out, buf_cstr(&mp));
            buf_free(&mp);
            buf_putc(out, '}');
            (*count)++;
            continue;
        }
        build_file_detail(paths[i], hit, out, 0);
        (*count)++;
    }
    stpaths_free(st, nst);
    free_strv(paths, np);
    buf_puts(out, "]");
}

/* ================================================================ 端点 */

static void send_json_buf(SOCKET s, buf *o) {
    http_send_json(s, 200, buf_cstr(o), o->n);
}

static int h_state(SOCKET s) {
    char *mh = read_gd_text("MERGE_HEAD");
    char *msg = read_gd_text("MERGE_MSG");
    char *orig = read_gd_text("ORIG_HEAD");
    int in_merge = 0;
    if (mh) {
        size_t n = 0;
        char **f = ws_split(mh, &n);
        in_merge = n > 0;
        free_strv(f, n);
    }
    char *first_line = NULL;
    if (msg) {
        const char *p = msg;
        while (p && *p) {
            const char *nl = strchr(p, '\n');
            size_t ln = nl ? (size_t)(nl - p) : strlen(p);
            char *t = xstrndup(p, ln);
            char *st = py_strip_dup(t);
            free(t);
            if (*st) { first_line = st; break; }
            free(st);
            p = nl ? nl + 1 : NULL;
        }
        if (!first_line) first_line = xstrdup("");
    } else {
        first_line = xstrdup("");
    }

    /* 参与分支：MERGE_MSG 首行里的引号名，去掉纯 sha */
    buf branches;
    buf_init(&branches);
    buf_putc(&branches, '[');
    int nbranch = 0;
    if (msg && *msg) {
        const char *nl = strchr(msg, '\n');
        size_t hl = nl ? (size_t)(nl - msg) : strlen(msg);
        char *head = xstrndup(msg, hl);
        if (hl && head[hl - 1] == '\r') head[hl - 1] = 0;
        /* ^Merge\s+(.+?):?\s*$ */
        if (!strncmp(head, "Merge", 5)) {
            const char *q = head + 5;
            if (*q && is_ws_ascii((unsigned char)*q)) {
                while (*q && is_ws_ascii((unsigned char)*q)) q++;
                const char *body = q;
                /* 去掉末尾的 ':' 与空白 */
                size_t bl = strlen(body);
                while (bl && is_ws_ascii((unsigned char)body[bl - 1])) bl--;
                if (bl && body[bl - 1] == ':') bl--;
                while (bl && is_ws_ascii((unsigned char)body[bl - 1])) bl--;
                if (bl) {
                    const char *pp = body;
                    const char *bend = body + bl;
                    while (pp < bend) {
                        const char *s1 = memchr(pp, '\'', (size_t)(bend - pp));
                        if (!s1) break;
                        const char *s2 = memchr(s1 + 1, '\'', (size_t)(bend - s1 - 1));
                        if (!s2) break;
                        size_t nlen = (size_t)(s2 - s1 - 1);
                        if (nlen > 0) {
                            char *name = xstrndup(s1 + 1, nlen);
                            int is_sha = (nlen >= 7 && nlen <= 40);
                            if (is_sha) {
                                for (size_t k = 0; k < nlen; k++)
                                    if (!isxdigit((unsigned char)name[k])) { is_sha = 0; break; }
                            }
                            if (!is_sha) {
                                if (nbranch) buf_putc(&branches, ',');
                                json_str(&branches, name);
                                nbranch++;
                            }
                            free(name);
                        }
                        pp = s2 + 1;
                    }
                }
            }
        }
        free(head);
    }
    buf_putc(&branches, ']');

    headst hs;
    head_state(&hs);

    char *merge_head_val = NULL;
    if (in_merge && mh) {
        size_t n = 0;
        char **f = ws_split(mh, &n);
        if (n) merge_head_val = xstrdup(f[0]);
        free_strv(f, n);
    }
    char *orig_val = NULL;
    if (orig && *orig) {
        size_t n = 0;
        char **f = ws_split(orig, &n);
        if (n) orig_val = xstrdup(f[0]);
        free_strv(f, n);
    }
    char *msg_path = gd_path_u8("MERGE_MSG");

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"inMerge\":");
    j_bool(&o, in_merge);
    buf_puts(&o, ",\"mergeHead\":");
    j_str_or_null(&o, merge_head_val);
    buf_puts(&o, ",\"origHead\":");
    j_str_or_null(&o, orig_val);
    buf_puts(&o, ",\"head\":");
    j_str_or_null(&o, hs.sha);
    buf_puts(&o, ",\"currentBranch\":");
    /* 与 rev-parse --abbrev-ref HEAD 的可观察输出对齐：
     * 正常 → 分支名；游离 / 未出生 → "HEAD"（Python 版同一条三元） */
    if (hs.kind == 1) j_str_or_null(&o, hs.branch);
    else if (hs.kind == 2 || hs.kind == 3) json_str(&o, "HEAD");
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"branches\":");
    buf_puts(&o, buf_cstr(&branches));
    buf_puts(&o, ",\"defaultMessage\":");
    json_str(&o, first_line);
    buf_puts(&o, ",\"mergeMsgPath\":");
    j_str_or_null(&o, msg_path);
    buf_puts(&o, ",\"remaining\":");
    long count = 0;
    buf files;
    buf_init(&files);
    if (in_merge) conflict_files_json(&files, &count);
    else buf_puts(&files, "[]");
    buf_printf(&o, "%ld,\"files\":", count);
    buf_puts(&o, buf_cstr(&files));
    buf_putc(&o, '}');
    send_json_buf(s, &o);

    buf_free(&files);
    buf_free(&o);
    buf_free(&branches);
    free(msg_path);
    free(merge_head_val);
    free(orig_val);
    free(first_line);
    free(mh);
    free(msg);
    free(orig);
    head_state_free(&hs);
    return 1;
}

static int h_list(SOCKET s) {
    int in_merge = merge_in_progress();
    long count = 0;
    buf files;
    buf_init(&files);
    conflict_files_json(&files, &count);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"inMerge\":");
    j_bool(&o, in_merge);
    buf_printf(&o, ",\"count\":%ld,\"files\":", count);
    buf_puts(&o, buf_cstr(&files));
    buf_putc(&o, '}');
    send_json_buf(s, &o);
    buf_free(&o);
    buf_free(&files);
    return 1;
}

static int h_inspect(const char *query, SOCKET s) {
    char *val = NULL;
    size_t vlen = 0;
    int g = q_raw(query, "file", &val, &vlen);
    if (g == 0 || (g == 1 && vlen == 0)) {
        free(val);
        http_send_err(s, 400, "缺少 file 参数", NULL);
        return 1;
    }
    if (g < 0) {
        http_send_err(s, 400, "请求参数不合法:参数过长", NULL);
        return 1;
    }
    if (memchr(val, 0, vlen)) {
        /* 与 server.resolve_in_root 的 NUL 拦截同文案同口径 */
        char *rp = py_repr_n(val, vlen);
        buf m;
        buf_init(&m);
        buf_printf(&m, "路径里不能包含 NUL 字符,已拒绝:%s", rp);
        http_send_err(s, 400, buf_cstr(&m), NULL);
        buf_free(&m);
        free(rp);
        free(val);
        return 1;
    }
    char *err = NULL;
    char *rel = path_rel_in_root(val, &err);
    free(val);
    if (!rel) {
        http_send_err(s, 400, err ? err : "路径不合法,已拒绝", NULL);
        free(err);
        return 1;
    }
    size_t nst = 0;
    stpath *st = ls_files_unmerged(rel, &nst);
    if (nst == 0) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "该路径没有未合并的索引条目,不是冲突文件:%s", rel);
        http_send_err(s, 400, buf_cstr(&m), NULL);
        buf_free(&m);
        stpaths_free(st, nst);
        free(rel);
        return 1;
    }
    buf o;
    buf_init(&o);
    build_file_detail(rel, &st[0], &o, 1);
    send_json_buf(s, &o);
    buf_free(&o);
    stpaths_free(st, nst);
    free(rel);
    return 1;
}

static int require_repo(SOCKET s) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    wchar_t *dotgit = join_w(root, L".git");
    DWORD attr = GetFileAttributesW(dotgit);
    free(dotgit);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        buf m;
        buf_init(&m);
        char *r = w2u8(root);
        buf_printf(&m, "不是一个 git 仓:%s", r ? r : "");
        free(r);
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":false,\"error\":");
        json_str(&o, buf_cstr(&m));
        buf_puts(&o, ",\"code\":\"not_a_repo\"}");
        http_send_json(s, 400, buf_cstr(&o), o.n);
        buf_free(&o);
        buf_free(&m);
        return 0;
    }
    return 1;
}

/* git_failed 形状：{"ok":false,"error":msg,"code":"git_failed","verb":v,"returncode":rc} */
static void send_git_failed(SOCKET s, buf *err, buf *out, const char *verb, int rc) {
    const char *src = NULL;
    if (err && err->n) src = buf_cstr(err);
    else if (out && out->n) src = buf_cstr(out);
    if (!src) src = "git 失败";
    char *norm = normalize_newlines(src, strlen(src));
    char *msg = py_strip_dup(norm);
    free(norm);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":\"git_failed\",\"verb\":");
    json_str(&o, verb);
    buf_printf(&o, ",\"returncode\":%d}", rc);
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
    free(msg);
}

/* NetError 形状（bad_remote / bad_ref）：{"ok":false,"error":msg,"code":code} */
static void send_neterr(SOCKET s, const char *msg, const char *code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg);
    buf_puts(&o, ",\"code\":");
    json_str(&o, code);
    buf_putc(&o, '}');
    http_send_json(s, 400, buf_cstr(&o), o.n);
    buf_free(&o);
}

static int h_capability(SOCKET s) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"op\":\"capability\",\"gate\":");
    gate_json(&o);
    buf_putc(&o, '}');
    send_json_buf(s, &o);
    buf_free(&o);
    return 1;
}

/* 远程名白名单：[A-Za-z0-9][A-Za-z0-9._-]{0,99} */
static int check_remote_name_n(const char *name, size_t n, char **msg) {
    int ok = n >= 1 && n <= 100 && isalnum((unsigned char)name[0]);
    for (size_t i = 1; ok && i < n; i++) {
        char c = name[i];
        if (!isalnum((unsigned char)c) && c != '.' && c != '_' && c != '-') ok = 0;
    }
    if (!ok) {
        char *rp = py_repr_n(name, n);
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

/* ref 白名单，与 gitops._check_ref 同序同文案。 */
static int ref_body_ok(const char *s, size_t n) {
    /* Python 的 _REF_RE 是 **ASCII-only**：别换成 isalnum()，那受 locale 影响。 */
    if (n < 1 || n > 200) return 0;
    unsigned char c0 = (unsigned char)s[0];
    if (!((c0 >= '0' && c0 <= '9') || (c0 >= 'A' && c0 <= 'Z') ||
          (c0 >= 'a' && c0 <= 'z')))
        return 0;
    for (size_t i = 1; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        int alnum = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') ||
                    (c >= 'a' && c <= 'z');
        if (!alnum && c != '.' && c != '_' && c != '/' && c != '-') return 0;
    }
    return 1;
}

static int check_ref_n(const char *ref, size_t n, const char *what, char **msg, const char **code) {
    buf b;
    buf_init(&b);
    *code = "bad_ref";
    if (n && ref[0] == '-') {
        char *rp = py_repr_n(ref, n);
        buf_printf(&b, "%s 不能以 - 开头(git 会把它当选项读):%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    if (memchr(ref, ':', n)) {
        char *rp = py_repr_n(ref, n);
        buf_printf(&b, "%s 不能含 ':'(那是 refspec 的分隔符, 会让调用方越权指定 ref):%s",
                   what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    if (py_has_space_or_ctrl(ref, n)) {
        char *rp = py_repr_n(ref, n);
        buf_printf(&b, "%s 不能含空白或控制字符:%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    int ok = 0;
    if (n >= 11 && !strncmp(ref, "refs/heads/", 11)) ok = ref_body_ok(ref + 11, n - 11);
    if (!ok && n >= 10 && !strncmp(ref, "refs/tags/", 10)) ok = ref_body_ok(ref + 10, n - 10);
    if (!ok) ok = ref_body_ok(ref, n);
    if (!ok) {
        char *rp = py_repr_n(ref, n);
        buf_printf(&b, "%s 不合法:%s", what, rp);
        free(rp);
        *msg = xstrdup(buf_cstr(&b));
        buf_free(&b);
        return 0;
    }
    buf_free(&b);
    return 1;
}

static int check_ref(const char *ref, const char *what, char **msg, const char **code) {
    return check_ref_n(ref, strlen(ref), what, msg, code);
}

static int h_remote_list(SOCKET s) {
    if (!require_repo(s)) return 1;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "remote", "-v", NULL };
    int rc = run_git(&o, &e, args);
    if (rc != 0) {
        send_git_failed(s, &e, &o, "remote", rc);
        buf_free(&o);
        buf_free(&e);
        return 1;
    }
    /* 按首次出现顺序聚合；一行两条（fetch / push） */
    char **names = NULL;
    size_t nn = 0;
    char **fetch = NULL, **push = NULL;
    const char *p = buf_cstr(&o);
    const char *line = p;
    while (line && *line) {
        const char *nl = strchr(line, '\n');
        size_t ln = nl ? (size_t)(nl - line) : strlen(line);
        char *one = xstrndup(line, ln);
        size_t nf = 0;
        char **f = ws_split(one, &nf);
        free(one);
        if (nf >= 2) {
            const char *tag = (nf > 2) ? f[2] : "";
            size_t idx = (size_t)-1;
            for (size_t k = 0; k < nn; k++) if (!strcmp(names[k], f[0])) { idx = k; break; }
            if (idx == (size_t)-1) {
                names = xrealloc(names, (nn + 1) * sizeof(char *));
                fetch = xrealloc(fetch, (nn + 1) * sizeof(char *));
                push = xrealloc(push, (nn + 1) * sizeof(char *));
                names[nn] = xstrdup(f[0]);
                fetch[nn] = NULL;
                push[nn] = NULL;
                idx = nn;
                nn++;
            }
            if (!strcmp(tag, "(push)")) {
                free(push[idx]);
                push[idx] = xstrdup(f[1]);
            } else {
                free(fetch[idx]);
                fetch[idx] = xstrdup(f[1]);
            }
        }
        free_strv(f, nf);
        line = nl ? nl + 1 : NULL;
    }

    buf r;
    buf_init(&r);
    buf_puts(&r, "{\"ok\":true,\"op\":\"remotes\",\"remotes\":[");
    for (size_t i = 0; i < nn; i++) {
        if (i) buf_putc(&r, ',');
        buf_puts(&r, "{\"name\":");
        json_str(&r, names[i]);
        buf_puts(&r, ",\"fetchUrl\":");
        j_str_or_null(&r, fetch[i]);
        buf_puts(&r, ",\"pushUrl\":");
        j_str_or_null(&r, push[i]);
        buf_putc(&r, '}');
    }
    buf_puts(&r, "],\"gate\":");
    gate_json(&r);
    buf_putc(&r, '}');
    send_json_buf(s, &r);
    buf_free(&r);
    for (size_t i = 0; i < nn; i++) {
        free(names[i]);
        free(fetch[i]);
        free(push[i]);
    }
    free(names);
    free(fetch);
    free(push);
    buf_free(&o);
    buf_free(&e);
    return 1;
}

/* rev-parse --abbrev-ref HEAD（失败/空 → NULL） */
static char *head_branch_name(void) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "rev-parse", "--abbrev-ref", "HEAD", NULL };
    char *out = NULL;
    if (run_git(&o, &e, args) == 0) {
        char *t = py_strip_dup(buf_cstr(&o));
        if (*t) out = t;
        else free(t);
    }
    buf_free(&o);
    buf_free(&e);
    return out;
}

static int h_ahead_behind(const char *query, SOCKET s) {
    if (!require_repo(s)) return 1;
    char *rval = NULL, *bval = NULL;
    size_t rlen = 0, blen = 0;
    int gr = q_raw(query, "remote", &rval, &rlen);
    int gb = q_raw(query, "branch", &bval, &blen);
    if (gr < 0 || gb < 0) {
        free(rval);
        free(bval);
        http_send_err(s, 400, "请求参数不合法:参数过长", NULL);
        return 1;
    }
    const char *remote = "origin";
    char *emsg = NULL;
    if (gr == 1 && rlen > 0) {
        if (!check_remote_name_n(rval, rlen, &emsg)) {
            send_neterr(s, emsg, "bad_remote");
            free(emsg);
            free(rval);
            free(bval);
            return 1;
        }
        remote = rval;
    }
    char *branch = NULL;
    if (gb == 1 && blen > 0) branch = xstrdup(bval);
    else {
        char *hb = head_branch_name();
        branch = hb ? hb : xstrdup("HEAD");
    }
    const char *code = NULL;
    int ref_ok = (gb == 1 && blen > 0)
        ? check_ref_n(bval, blen, "分支", &emsg, &code)
        : check_ref(branch, "分支", &emsg, &code);
    if (!ref_ok) {
        send_neterr(s, emsg, code);
        free(emsg);
        free(branch);
        free(rval);
        free(bval);
        return 1;
    }
    buf up;
    buf_init(&up);
    buf_printf(&up, "%s/%s", remote, branch);

    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *vargs[] = { "rev-parse", "--verify", "--quiet", buf_cstr(&up), NULL };
    int has = (run_git(&o, &e, vargs) == 0);

    buf r;
    buf_init(&r);
    if (!has) {
        const char *rargs[] = { "rev-list", branch, NULL };
        int rc = run_git(&o, &e, rargs);
        if (rc != 0) {
            send_git_failed(s, &e, &o, "rev-list", rc);
            buf_free(&r);
            buf_free(&o);
            buf_free(&e);
            buf_free(&up);
            free(branch);
            free(rval);
            free(bval);
            return 1;
        }
        size_t n = 0;
        char **tok = ws_split(buf_cstr(&o), &n);
        long ahead = (long)n;
        free_strv(tok, n);
        buf_puts(&r, "{\"ok\":true,\"remote\":");
        json_str(&r, remote);
        buf_puts(&r, ",\"branch\":");
        json_str(&r, branch);
        buf b1, b2;
        buf_init(&b1);
        buf_init(&b2);
        buf_printf(&b1, "本地没有 %s 这个远程跟踪 ref", buf_cstr(&up));
        buf_printf(&b2, "本地没有 %s 这个远程跟踪 ref,上面所有提交都算「待推」", buf_cstr(&up));
        buf_puts(&r, ",\"basis\":");
        json_str(&r, buf_cstr(&b1));
        buf_puts(&r, ",\"trackingRefExists\":false,\"ahead\":");
        buf_printf(&r, "%ld", ahead);
        buf_puts(&r, ",\"behind\":null,\"note\":");
        json_str(&r, buf_cstr(&b2));
        buf_putc(&r, '}');
        buf_free(&b1);
        buf_free(&b2);
    } else {
        buf range;
        buf_init(&range);
        buf_printf(&range, "%s...%s", buf_cstr(&up), branch);
        const char *cargs[] = { "rev-list", "--left-right", "--count",
                                buf_cstr(&range), NULL };
        run_git(&o, &e, cargs);
        size_t n = 0;
        char **tok = ws_split(buf_cstr(&o), &n);
        long behind = (n > 0) ? atol(tok[0]) : 0;
        long ahead = (n > 1) ? atol(tok[1]) : 0;
        free_strv(tok, n);
        buf_puts(&r, "{\"ok\":true,\"remote\":");
        json_str(&r, remote);
        buf_puts(&r, ",\"branch\":");
        json_str(&r, branch);
        buf b1;
        buf_init(&b1);
        buf_printf(&b1, "本地远程跟踪 ref %s(可能陈旧; 要知道远程真实现状请先 fetch)",
                   buf_cstr(&up));
        buf_puts(&r, ",\"basis\":");
        json_str(&r, buf_cstr(&b1));
        buf_puts(&r, ",\"trackingRefExists\":true,\"ahead\":");
        buf_printf(&r, "%ld,\"behind\":%ld}", ahead, behind);
        buf_free(&b1);
        buf_free(&range);
    }
    send_json_buf(s, &r);
    buf_free(&r);
    buf_free(&o);
    buf_free(&e);
    buf_free(&up);
    free(branch);
    free(rval);
    free(bval);
    return 1;
}

/* ================================================================ 入口 */

int read_conflict_get(const char *path, const char *query, SOCKET s) {
    if (!strcmp(path, "/api/conflict/state")) return h_state(s);
    if (!strcmp(path, "/api/conflict/list")) return h_list(s);
    if (!strcmp(path, "/api/conflict/inspect")) return h_inspect(query, s);
    if (!strcmp(path, "/api/remote/capability")) return h_capability(s);
    if (!strcmp(path, "/api/remote/list")) return h_remote_list(s);
    if (!strcmp(path, "/api/remote/ahead-behind")) return h_ahead_behind(query, s);
    return 0;
}
