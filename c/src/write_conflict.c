/* write_conflict.c —— M2 写面第三组：冲突解决的 5 个 POST 端点。
 *
 * 与 Python 参考版的接线点（server.py 2351-2389）：
 *     /api/conflict/resolve  -> ConflictResolver.resolve(rel, content, expect_sha256, encoding)
 *     /api/conflict/choose   -> ConflictResolver.choose(rel, side)
 *     /api/conflict/delete   -> ConflictResolver.delete_file(rel, expect_sha256)
 *     /api/conflict/commit   -> ConflictResolver.commit_merge(message)
 *     /api/conflict/abort    -> ConflictResolver.abort()
 *
 * ⚠ 五个端点走的都是 `self._json(...)`，**不是** `_ok_with_status(...)`：
 *   响应体里没有 status 字段，body 里的 `"status": false` 也**不生效**
 *   （实测：带 status:false 的 resolve 与不带时逐字相同）。所以这里不用
 *   wsend_ok，而是直接 http_send_json 发模块自己的 dict。
 *
 * ⚠ 失败一律 400 + `{"ok":false,"error":...}`（ConflictError 与 GitError 在
 *   server.py 里都映射到 `_err(e, 400)`，不带 code 字段）。
 *
 * ⚠ git 调用的形状必须与 conflicts.py 的 `_argv()` 对齐：
 *     git -C <root> --literal-pathspecs -c user.name=aigit -c user.email=aigit@localhost
 *         -c commit.gpgsign=false -c core.autocrlf=false -c core.safecrlf=false <cmd>
 *   * 身份那两条决定 commit_merge 写出的 commit sha（仓里没配 user.name 时也要能提交）；
 *   * `core.autocrlf=false` 不是摆设：本机 Git for Windows 的 **system** 配置默认
 *     `core.autocrlf=true`，git.c 会把它当语义键经 GIT_CONFIG_COUNT（环境配置）
 *     透传，而 `-c` 的优先级更高 —— 不补这条，`git merge --abort` 检出的工作区
 *     会是 CRLF，与 Python 不一致（实测踩到）。
 *   * 代价（如实记）：`-c` 的值是独立 argv 项，git.c 的 cmds 记账会把
 *     `user.name=aigit` 当成子命令名写进 X-aigit-git-cmds 头。那个头不参与
 *     一致性判据（M1 起就明确不比），只是排查时可读性差一点。
 */

#include "routes.h"
#include "params.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "bodyjson.h"
#include "git.h"
#include "http.h"
#include "json.h"
#include "paths.h"
#include "util.h"
#include "writeutil.h"

/* ================================================================ 小工具 */

static void send_bad(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
}

static void send_bad_buf(SOCKET s, buf *m) {
    http_send_err(s, 400, buf_cstr(m), NULL);
}

static int is_py_space(int c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

/* Python `s.strip()` 的等价物（ASCII 空白）。 */
static void free_strv(char **v, size_t n) {
    for (size_t i = 0; i < n; i++) free(v[i]);
    free(v);
}

/* Python repr(str) 的近似（错误文案里的 %r 用），支持内嵌 NUL。 */
/* Python repr 的近似（错误文案里的 %r 用）。
 * 与 writeutil 的 w_repr 的区别：float 走 repr 口径（5.0 不能印成 "5"）。 */
/* 非法 UTF-8 → U+FFFD（Python `bytes.decode("utf-8","replace")` 的口径）。 */
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

/* ================================================================ base64 */

/* `base64.b64decode(s, validate=True)` 的等价实现。
 *
 * 失败文案必须逐字对齐 CPython 3.11 的 binascii（实测口径）：
 *   Only base64 data is allowed / Excess data after padding /
 *   Discontinuous padding not allowed / Leading padding not allowed /
 *   Incorrect padding /
 *   Invalid base64-encoded string: number of data characters (N) cannot be
 *   1 more than a multiple of 4
 * 成功返回 1（*out 是 malloc 的字节，*outlen 长度）；失败返回 0 并写 *err。 */
static int b64_val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

static int b64_decode(const char *s, size_t n, char **out, size_t *outlen, char **err) {
    *out = NULL;
    *outlen = 0;
    *err = NULL;
    for (size_t i = 0; i < n; i++) {
        if ((unsigned char)s[i] >= 0x80) {
            *err = xstrdup("string argument should contain only ASCII characters");
            return 0;
        }
    }
    buf o;
    buf_init(&o);
    size_t n_data = 0;
    int pads = 0;
    unsigned leftchar = 0;
    int leftbits = 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (c == '=') {
            if (n_data == 0) {
                *err = xstrdup("Leading padding not allowed");
                buf_free(&o);
                return 0;
            }
            pads++;
            int q = (int)(n_data % 4);
            if ((q == 2 && pads > 2) || (q == 3 && pads > 1)) {
                *err = xstrdup("Excess data after padding");
                buf_free(&o);
                return 0;
            }
            continue;
        }
        int v = b64_val(c);
        /* ⚠ 顺序：`pads` 必须先判 —— CPython 的 a2b_base64 就是这样排的。
         * 否则 "QQ==\n" 会报 "Only base64 data is allowed"，
         * 而 Python 报的是 "Excess data after padding"（实测）。 */
        if (pads) {
            int q = (int)(n_data % 4);
            if (q == 2 && pads < 2) *err = xstrdup("Discontinuous padding not allowed");
            else if (q == 0 || q == 1) *err = xstrdup("Discontinuous padding not allowed");
            else *err = xstrdup("Excess data after padding");
            buf_free(&o);
            return 0;
        }
        if (v < 0) {
            *err = xstrdup("Only base64 data is allowed");
            buf_free(&o);
            return 0;
        }
        n_data++;
        leftchar = (leftchar << 6) | (unsigned)v;
        leftbits += 6;
        while (leftbits >= 8) {
            leftbits -= 8;
            buf_putc(&o, (char)((leftchar >> leftbits) & 0xFFu));
            leftchar &= (1u << leftbits) - 1u;
        }
    }
    int q = (int)(n_data % 4);
    if (q == 1) {
        buf m;
        buf_init(&m);
        buf_printf(&m, "Invalid base64-encoded string: number of data characters "
                       "(%zu) cannot be 1 more than a multiple of 4", n_data);
        *err = xstrdup(buf_cstr(&m));
        buf_free(&m);
        buf_free(&o);
        return 0;
    }
    if ((q == 2 && pads != 2) || (q == 3 && pads != 1)) {
        *err = xstrdup("Incorrect padding");
        buf_free(&o);
        return 0;
    }
    *out = xmalloc(o.n + 1);
    if (o.n) memcpy(*out, o.p, o.n);
    (*out)[o.n] = 0;
    *outlen = o.n;
    buf_free(&o);
    return 1;
}

/* ================================================================ git 调用 */

/* conflicts.py 的 `_GIT_CONFIG`（身份 / 换行）+ `--literal-pathspecs`。
 *
 * 身份那两条决定 commit_merge 写出的 commit sha；`core.autocrlf=false` 与
 * `core.safecrlf=false` 也不是摆设 —— 本机 Git for Windows 的 **system** 配置里
 * 默认 `core.autocrlf=true`，git.c 的 load_semantic 会把它当语义键经
 * `GIT_CONFIG_COUNT` 环境变量透传（环境配置），而 `-c` 的优先级更高。
 * Python 侧 `_GIT_CONFIG` 里的 `-c core.autocrlf=false` 正是这样压住它的
 * （实测：不补这两条，`merge --abort` 检出的工作区是 CRLF，与 Python 不一致）。
 *
 * 代价（如实记）：`-c` 的值是独立 argv 项，git.c 的 cmds 记账会把
 * `user.name=aigit` 当成子命令名写进 X-aigit-git-cmds 头。那个头**不参与**
 * 一致性判据（M1 起就明确不比），只是排查时可读性差一点。 */
static int wc_git(const char *const *args, buf *out, buf *err) {
    static const char *pre[] = {
        "--literal-pathspecs",
        "-c", "user.name=aigit",
        "-c", "user.email=aigit@localhost",
        "-c", "commit.gpgsign=false",
        "-c", "core.autocrlf=false",
        "-c", "core.safecrlf=false",
    };
    size_t np = sizeof(pre) / sizeof(pre[0]);
    size_t n = 0;
    while (args[n]) n++;
    const char **v = xmalloc((n + np + 1) * sizeof(char *));
    for (size_t i = 0; i < np; i++) v[i] = pre[i];
    for (size_t i = 0; i <= n; i++) v[np + i] = args[i];
    if (out) buf_reset(out);
    if (err) buf_reset(err);
    int rc = git_run(v, NULL, 0, out, err);
    free(v);
    return rc;
}

/* `"git %s 失败(rc=%d): %s" % (" ".join(args[:3]), rc, msg)`。
 * msg = (stderr or stdout).strip()，且把仓根绝对路径抹成 <repo>。 */
static char *git_fail_msg(const char *const *args, int rc, const char *errp, size_t errn,
                          const char *outp, size_t outn) {
    const char *src = (errn ? errp : outp);
    size_t srcn = (errn ? errn : outn);
    char *raw = xstrndup(src ? src : "", src ? srcn : 0);
    char *msg = py_strip_dup(raw);
    free(raw);
    /* 绝对路径脱敏（Python 版两种分隔符都抹） */
    char *root = w2u8(git_root());
    if (root && *root) {
        buf cleaned;
        buf_init(&cleaned);
        char *alt = xstrdup(root);
        for (char *q = alt; *q; q++) if (*q == '\\') *q = '/';
        const char *p = msg;
        while (*p) {
            if (!strncmp(p, root, strlen(root))) { buf_puts(&cleaned, "<repo>"); p += strlen(root); }
            else if (!strncmp(p, alt, strlen(alt))) { buf_puts(&cleaned, "<repo>"); p += strlen(alt); }
            else buf_putc(&cleaned, *p++);
        }
        free(alt);
        free(msg);
        msg = xstrdup(buf_cstr(&cleaned));
        buf_free(&cleaned);
    }
    free(root);
    buf m;
    buf_init(&m);
    buf_puts(&m, "git ");
    for (int i = 0; i < 3 && args[i]; i++) {
        if (i) buf_putc(&m, ' ');
        buf_puts(&m, args[i]);
    }
    buf_puts(&m, " 失败(rc=");
    buf_printf(&m, "%d): ", rc);
    buf_puts(&m, msg);
    free(msg);
    char *r = xstrdup(buf_cstr(&m));
    buf_free(&m);
    return r;
}

/* ================================================================ .git 目录 */

static wchar_t *g_gd = NULL;
static int g_gd_done = 0;

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
        else if (!wcsncmp(out, L"\\\\?\\", 4))
            memmove(out, out + 4, (wcslen(out + 4) + 1) * sizeof(wchar_t));
    }
    CloseHandle(h);
    return out;
}

/* gitrefs.gitdir 的判据：`.git` 是目录、没有 reftable、没有环境变量改写布局。 */
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
    buf out, err;
    buf_init(&out);
    buf_init(&err);
    const char *args[] = { "rev-parse", "--git-dir", NULL };
    if (git_run(args, NULL, 0, &out, &err) == 0) {
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

/* 读整个普通文件。不是普通文件 / 读失败 → 返回 0。 */
static int read_whole_w(const wchar_t *path, char **out, size_t *outlen) {
    *out = NULL;
    *outlen = 0;
    DWORD attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE |
                           FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    buf b;
    buf_init(&b);
    char tmp[8192];
    DWORD got = 0;
    while (ReadFile(h, tmp, sizeof(tmp), &got, NULL) && got > 0) buf_append(&b, tmp, got);
    CloseHandle(h);
    *outlen = b.n;
    *out = xmalloc(b.n + 1);
    if (b.n) memcpy(*out, b.p, b.n);
    (*out)[b.n] = 0;
    buf_free(&b);
    return 1;
}

/* Python `Path.read_text(encoding="utf-8", errors="replace")`：不存在 → NULL。 */
static char *read_gd(const char *name) {
    const wchar_t *gd = gitdir_w();
    if (!gd) return NULL;
    wchar_t *w = u82w(name);
    if (!w) return NULL;
    wchar_t *full = join_w(gd, w);
    free(w);
    char *raw = NULL;
    size_t rawn = 0;
    int ok = read_whole_w(full, &raw, &rawn);
    free(full);
    if (!ok) { free(raw); return NULL; }
    /* universal newlines：\r\n 与 \r 都变 \n */
    buf norm;
    buf_init(&norm);
    for (size_t i = 0; i < rawn; i++) {
        if (raw[i] == '\r') {
            if (i + 1 < rawn && raw[i + 1] == '\n') i++;
            buf_putc(&norm, '\n');
        } else buf_putc(&norm, raw[i]);
    }
    char *san = u8_sanitize(buf_cstr(&norm), norm.n);
    buf_free(&norm);
    free(raw);
    return san;
}

/* ================================================================ 请求参数 */

/* Python `(root / rel).resolve()` 是否落在 root 本身（"" / "." / "./." /
 * " " / "a.txt/.." 都是）。paths.c 对这类输入报的是「路径越界」，
 * 与 Python 的「路径指向仓库根目录本身」不同 —— 这一层先把它们挑出来。 */
static int resolves_to_root(const char *s, size_t n) {
    int depth = 0, escaped = 0;
    size_t i = 0;
    for (;;) {
        size_t start = i;
        while (i < n && s[i] != '/' && s[i] != '\\') i++;
        size_t len = i - start;
        if (len == 2 && s[start] == '.' && s[start + 1] == '.') {
            if (depth == 0) escaped = 1;
            else depth--;
        } else if (len == 1 && s[start] == '.') {
            /* skip */
        } else {
            /* Windows 忽略段尾的空格与点（" " 会被当成空段） */
            while (len && (s[start + len - 1] == ' ' || s[start + len - 1] == '.')) len--;
            if (len) depth++;
        }
        if (i >= n) break;
        i++;
    }
    return !escaped && depth == 0;
}

/* 请求体里的路径参数 → 仓内相对路径（'/'-分隔）。失败写 *err 并返回 NULL。 */
static char *path_from_arg(const char *s, size_t n, char **err) {
    *err = NULL;
    if (memchr(s, 0, n)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "路径里不能包含 NUL 字符,已拒绝:");
        char *r = py_repr_n(s, n);
        buf_puts(&m, r);
        free(r);
        *err = xstrdup(buf_cstr(&m));
        buf_free(&m);
        return NULL;
    }
    if (n == 0 || resolves_to_root(s, n)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "路径指向仓库根目录本身,已拒绝:");
        buf_append(&m, s, n);
        *err = xstrdup(buf_cstr(&m));
        buf_free(&m);
        return NULL;
    }
    char *tmp = xstrndup(s, n);
    char *perr = NULL;
    char *rel = path_rel_in_root(tmp, &perr);
    free(tmp);
    if (!rel) { *err = perr; return NULL; }
    char *out = py_strip_dup(rel);
    free(rel);
    for (char *p = out; *p; p++) if (*p == '\\') *p = '/';
    if (!*out) {
        free(out);
        *err = xstrdup("路径为空");
        return NULL;
    }
    return out;
}

/* server.py 的 `REPO.rel_in_root(b.get("file", ""))`。 */
static char *arg_rel(const jnode *b, const char *key, char **err) {
    *err = NULL;
    const jnode *v = jget(b, key);
    if (!v) return path_from_arg("", 0, err);
    if (bjtype(v) == BJ_NULL) { *err = xstrdup("路径为空"); return NULL; }
    if (!jis_str(v)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "路径必须是字符串,收到 ");
        buf_puts(&m, jtype_name(v));
        *err = xstrdup(buf_cstr(&m));
        buf_free(&m);
        return NULL;
    }
    return path_from_arg(jstr(v), jstrlen(v), err);
}

/* ================================================================ 索引 stage */

typedef struct {
    char *path;
    char *mode[4];
    char *sha[4];
} wcstage;

static void wcstage_free(wcstage *v, size_t n) {
    for (size_t i = 0; i < n; i++) {
        free(v[i].path);
        for (int k = 1; k <= 3; k++) { free(v[i].mode[k]); free(v[i].sha[k]); }
    }
    free(v);
}

/* `git ls-files -u -z -- <rel>` → 每个路径的 stage 1/2/3。 */
static wcstage *stages_of(const char *rel, size_t *out_n) {
    *out_n = 0;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "ls-files", "-u", "-z", "--", rel, NULL };
    wc_git(args, &o, &e);
    wcstage *out = NULL;
    size_t n = 0;
    const char *p = buf_cstr(&o);
    size_t len = o.n, pos = 0;
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
        /* meta = "mode sha stage"（空格分隔） */
        char *f[3];
        int nf = 0;
        char *q = meta;
        while (nf < 3) {
            while (*q == ' ' || *q == '\t') q++;
            if (!*q) break;
            f[nf++] = q;
            while (*q && *q != ' ' && *q != '\t') q++;
            if (*q) *q++ = 0;
        }
        if (nf != 3) { free(meta); free(name); continue; }
        int stage = atoi(f[2]);
        if (stage < 1 || stage > 3) { free(meta); free(name); continue; }
        size_t idx = (size_t)-1;
        for (size_t k = 0; k < n; k++) if (!strcmp(out[k].path, name)) { idx = k; break; }
        if (idx == (size_t)-1) {
            out = xrealloc(out, (n + 1) * sizeof(wcstage));
            memset(&out[n], 0, sizeof(wcstage));
            out[n].path = name;
            idx = n;
            n++;
        } else {
            free(name);
        }
        free(out[idx].mode[stage]);
        free(out[idx].sha[stage]);
        out[idx].mode[stage] = xstrdup(f[0]);
        out[idx].sha[stage] = xstrdup(f[1]);
        free(meta);
    }
    buf_free(&o);
    buf_free(&e);
    *out_n = n;
    return out;
}

/* ================================================================ 冲突清单 */

/* `git diff --name-only --diff-filter=U -z` → 未合并路径（list_conflicts 的 paths）。 */
static char **conflict_paths(size_t *out_n) {
    *out_n = 0;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "diff", "--name-only", "--diff-filter=U", "-z", NULL };
    wc_git(args, &o, &e);
    char **v = NULL;
    size_t n = 0;
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
            v = xrealloc(v, (n + 1) * sizeof(char *));
            v[n++] = s;
        } else free(s);
    }
    buf_free(&o);
    buf_free(&e);
    *out_n = n;
    return v;
}

/* 追加 `"remainingCount":N,"remaining":[...]`。 */
static void put_remaining(buf *o, char **paths, size_t n) {
    buf_printf(o, ",\"remainingCount\":%zu,\"remaining\":[", n);
    for (size_t i = 0; i < n; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, paths[i]);
    }
    buf_putc(o, ']');
}

/* ================================================================ 合并态 */

static const char *k_no_merge =
    "当前不在合并态(.git/MERGE_HEAD 不存在) —— "
    "合并可能已经完成、被 --abort 撤销,或压根没开始。"
    "已拒绝写入,不会动工作区。";

/* require_merge()：不在合并态 → 0 且写 *err；在合并态 → 1，
 * *mh_out = MERGE_HEAD 第一个 token（空文件时是 NULL，对应 Python 的 None）。 */
static int require_merge(char **mh_out, char **err) {
    *mh_out = NULL;
    *err = NULL;
    char *mh = read_gd("MERGE_HEAD");
    if (!mh || !*mh) {
        free(mh);
        *err = xstrdup(k_no_merge);
        return 0;
    }
    const char *p = mh;
    while (*p && is_py_space((unsigned char)*p)) p++;
    const char *b = p;
    while (*p && !is_py_space((unsigned char)*p)) p++;
    if (p > b) *mh_out = xstrndup(b, (size_t)(p - b));
    free(mh);
    return 1;
}

/* ================================================================ 文件写 */

static int mkdirs_w(const wchar_t *path) {
    size_t n = wcslen(path);
    if (n < 3) return 1;
    wchar_t *tmp = _wcsdup(path);
    for (size_t i = 3; i < n; i++) {
        if (tmp[i] == L'\\' || tmp[i] == L'/') {
            wchar_t save = tmp[i];
            tmp[i] = 0;
            CreateDirectoryW(tmp, NULL);
            tmp[i] = save;
        }
    }
    free(tmp);
    return 1;
}

static int write_whole_w(const wchar_t *path, const char *data, size_t n, char **oserr) {
    *oserr = NULL;
    mkdirs_w(path);
    HANDLE h = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        *oserr = xstrdup(strerror((int)GetLastError()));
        return 0;
    }
    size_t off = 0;
    while (off < n) {
        DWORD chunk = (DWORD)(n - off > 1u << 20 ? 1u << 20 : n - off);
        DWORD wrote = 0;
        if (!WriteFile(h, data + off, chunk, &wrote, NULL) || wrote == 0) {
            CloseHandle(h);
            *oserr = xstrdup(strerror((int)GetLastError()));
            return 0;
        }
        off += wrote;
    }
    CloseHandle(h);
    return 1;
}

/* ================================================================ 仓外联接 */

typedef struct {
    char *path;
    char *target;
} leak_t;

static void leak_free(leak_t *v, size_t n) {
    for (size_t i = 0; i < n; i++) { free(v[i].path); free(v[i].target); }
    free(v);
}

static int wcs_prefix_ci(const wchar_t *s, const wchar_t *pre) {
    size_t n = wcslen(pre);
    if (_wcsnicmp(s, pre, n) != 0) return 0;
    /* Python 的 relative_to 是**按段**比，不是按字符串前缀：/root2 不算 /root 内 */
    return s[n] == 0 || s[n] == L'\\' || s[n] == L'/';
}

/* server.Repo.escaping_links 的对位：仓里但指向仓外的 reparse point（前 16 个）。
 * 只做「普通目录往下走、遇联接不递归」，与 Python 版一致。 */
static leak_t *escaping_links(size_t *out_n, size_t limit) {
    *out_n = 0;
    const wchar_t *root = git_root();
    if (!root) return NULL;
    wchar_t *root_real = real_path_w(root);
    const wchar_t *rbase = root_real ? root_real : root;
    leak_t *found = NULL;
    size_t n = 0;
    wchar_t **stack = xmalloc(sizeof(wchar_t *));
    size_t sp = 0;
    stack[sp++] = _wcsdup(root);
    while (sp && n < limit) {
        wchar_t *dir = stack[--sp];
        buf pat;
        buf_init(&pat);
        buf_append(&pat, dir, wcslen(dir) * sizeof(wchar_t));
        buf_append(&pat, L"\\*", 3 * sizeof(wchar_t));
        wchar_t z = 0;
        buf_append(&pat, &z, sizeof(wchar_t));
        WIN32_FIND_DATAW fd;
        HANDLE h = FindFirstFileW((wchar_t *)pat.p, &fd);
        buf_free(&pat);
        if (h == INVALID_HANDLE_VALUE) { free(dir); continue; }
        do {
            if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
            if (!wcscmp(fd.cFileName, L".git")) continue;
            wchar_t *full = join_w(dir, fd.cFileName);
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
                wchar_t *rp = real_path_w(full);
                int inside = rp && wcs_prefix_ci(rp, rbase);
                if (rp && !inside) {
                    found = xrealloc(found, (n + 1) * sizeof(leak_t));
                    found[n].path = w2u8(full);
                    found[n].target = w2u8(rp);
                    if (found[n].path && found[n].target) n++;
                    else { free(found[n].path); free(found[n].target); }
                }
                free(rp);
                free(full);
                continue;                 /* 联接一律不再往下走 */
            }
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                stack = xrealloc(stack, (sp + 1) * sizeof(wchar_t *));
                stack[sp++] = full;
                continue;
            }
            free(full);
        } while (FindNextFileW(h, &fd));
        FindClose(h);
        free(dir);
    }
    for (size_t i = 0; i < sp; i++) free(stack[i]);
    free(stack);
    free(root_real);
    *out_n = n;
    return found;
}

/* ================================================================ 合并提交的 add 作用域 */

/* `_merge_add_scope()`：三条 git 的「已跟踪」视图，去重 + 排序。
 * 未跟踪的一律不进（目录联接下的仓外文件属于未跟踪）。 */
static int cmp_str(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

static char **merge_add_scope(size_t *out_n) {
    *out_n = 0;
    char **v = NULL;
    size_t n = 0;
    const char *const cmds[3][5] = {
        { "diff", "--cached", "--name-only", "-z", NULL },
        { "diff", "--name-only", "-z", NULL, NULL },
        { "diff", "--diff-filter=D", "--name-only", "-z", NULL },
    };
    for (int k = 0; k < 3; k++) {
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        wc_git(cmds[k], &o, &e);
        const char *p = buf_cstr(&o);
        size_t len = o.n, pos = 0;
        while (pos < len) {
            const char *rec = p + pos;
            size_t rl = 0;
            while (pos + rl < len && rec[rl]) rl++;
            pos += rl + 1;
            if (!rl) continue;
            char *t = xstrndup(rec, rl);
            int dup = 0;
            for (size_t i = 0; i < n; i++) if (!strcmp(v[i], t)) { dup = 1; break; }
            if (dup) { free(t); continue; }
            v = xrealloc(v, (n + 1) * sizeof(char *));
            v[n++] = t;
        }
        buf_free(&o);
        buf_free(&e);
    }
    /* Python 的 sorted() 按码点；UTF-8 字节序与码点序一致 */
    qsort(v, n, sizeof(char *), cmp_str);
    *out_n = n;
    return v;
}

/* ================================================================ MERGE_MSG 解析 */

/* `_branches_from_merge_msg`：从首行抽 'xxx'，过滤纯 7-40 位十六进制。 */
static char **branches_from_msg(const char *msg, size_t *out_n) {
    *out_n = 0;
    if (!msg || !*msg) return NULL;
    const char *nl = strchr(msg, '\n');
    size_t headn = nl ? (size_t)(nl - msg) : strlen(msg);
    if (headn < 6 || memcmp(msg, "Merge", 5) != 0) return NULL;
    if (!is_py_space((unsigned char)msg[5])) return NULL;
    /* group(1) = 去掉首尾空白、再去掉一个尾部 ':' 的部分 */
    size_t b = 6;
    while (b < headn && is_py_space((unsigned char)msg[b])) b++;
    size_t e = headn;
    while (e > b && is_py_space((unsigned char)msg[e - 1])) e--;
    if (e - b > 1 && msg[e - 1] == ':') e--;
    char **v = NULL;
    size_t n = 0;
    size_t i = b;
    while (i < e) {
        if (msg[i] != '\'') { i++; continue; }
        size_t q = i + 1;
        while (q < e && msg[q] != '\'') q++;
        if (q >= e) break;                       /* 没有收尾引号 */
        if (q > i + 1) {
            size_t len = q - i - 1;
            int hexonly = (len >= 7 && len <= 40);
            if (hexonly) {
                for (size_t k = i + 1; k < q; k++) {
                    char c = msg[k];
                    int ishex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                                (c >= 'A' && c <= 'F');
                    if (!ishex) { hexonly = 0; break; }
                }
            }
            if (!hexonly) {
                v = xrealloc(v, (n + 1) * sizeof(char *));
                v[n++] = xstrndup(msg + i + 1, len);
            }
        }
        i = q + 1;
    }
    *out_n = n;
    return v;
}

/* ================================================================ 响应 */

static void send_obj(SOCKET s, buf *o) {
    http_send_json(s, 200, buf_cstr(o), o->n);
}

static void put_merge_head(buf *o, const char *mh) {
    buf_puts(o, ",\"mergeHead\":");
    if (mh) json_str(o, mh);
    else buf_puts(o, "null");
}

/* ================================================================ resolve 内核 */

/* Python `_resolve_locked` 写完 `git add` 之后的那段：remaining + 响应。 */
static void resolve_tail(SOCKET s, const char *rel, const char *mh,
                         const char *raw, size_t rawn) {
    char **paths = NULL;
    size_t np = 0;
    paths = conflict_paths(&np);
    int still = 0;
    for (size_t i = 0; i < np; i++) if (!strcmp(paths[i], rel)) { still = 1; break; }
    char hex[65];
    sha256_hex(raw, rawn, hex);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    json_str(&o, rel);
    buf_puts(&o, ",\"staged\":true,\"stillUnmerged\":");
    buf_puts(&o, still ? "true" : "false");
    put_remaining(&o, paths, np);
    put_merge_head(&o, mh);
    buf_printf(&o, ",\"byteLength\":%zu,\"sha256\":", rawn);
    json_str(&o, hex);
    buf_putc(&o, '}');
    send_obj(s, &o);
    buf_free(&o);
    free_strv(paths, np);
}

/* 写回 + git add。raw 是**最终字节**（已经过 encoding 处理）。 */
static int do_resolve(SOCKET s, const char *rel, const char *mh,
                      const char *raw, size_t rawn) {
    wchar_t *ap = repo_wpath(rel);
    char *oserr = NULL;
    if (!ap || !write_whole_w(ap, raw, rawn, &oserr)) {
        free(ap);
        buf m;
        buf_init(&m);
        buf_puts(&m, "写回工作区失败 —— ");
        buf_puts(&m, rel);
        buf_puts(&m, ": ");
        buf_puts(&m, (oserr && *oserr) ? oserr : "OSError");
        free(oserr);
        send_bad_buf(s, &m);
        buf_free(&m);
        return 1;
    }
    free(ap);

    const char *args[] = { "add", "--", rel, NULL };
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = wc_git(args, &o, &e);
    if (rc != 0) {
        char *msg = git_fail_msg(args, rc, buf_cstr(&e), e.n, buf_cstr(&o), o.n);
        send_bad(s, msg);
        free(msg);
        buf_free(&o);
        buf_free(&e);
        return 1;
    }
    buf_free(&o);
    buf_free(&e);
    resolve_tail(s, rel, mh, raw, rawn);
    return 1;
}

/* ================================================================ /api/conflict/resolve */

static int h_resolve(SOCKET s, const jnode *b) {
    char *perr = NULL;
    char *rel = arg_rel(b, "file", &perr);
    if (!rel) { send_bad(s, perr); free(perr); return 1; }

    const jnode *cv = jget(b, "content");
    if (!cv) {
        send_bad(s, "缺少 content 字段(要清空文件请显式传空串)");
        free(rel);
        return 1;
    }
    if (!jis_str(cv)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "content 必须是字符串,收到 ");
        buf_puts(&m, jtype_name(cv));
        send_bad_buf(s, &m);
        buf_free(&m);
        free(rel);
        return 1;
    }
    const char *content = jstr(cv);
    size_t content_n = jstrlen(cv);

    char *mh = NULL, *merr = NULL;
    if (!require_merge(&mh, &merr)) {
        send_bad(s, merr);
        free(merr);
        free(rel);
        return 1;
    }

    size_t nst = 0;
    wcstage *st = stages_of(rel, &nst);
    wcstage_free(st, nst);
    if (nst == 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "该路径不是未合并路径,拒绝写入:");
        buf_puts(&m, rel);
        send_bad_buf(s, &m);
        buf_free(&m);
        free(mh);
        free(rel);
        return 1;
    }

    /* encoding: b.get("encoding", "utf-8") */
    const jnode *encv = jget(b, "encoding");
    int enc_base64 = 0;
    if (encv && bjtype(encv) == BJ_STR && !strcmp(jstr(encv), "base64")) {
        enc_base64 = 1;
    } else if (!encv || bjtype(encv) == BJ_NULL ||
               (bjtype(encv) == BJ_STR &&
                (!strcmp(jstr(encv), "utf-8") || !strcmp(jstr(encv), "text")))) {
        enc_base64 = 0;
    } else {
        buf m;
        buf_init(&m);
        buf_puts(&m, "不支持的 encoding:");
        w_repr(encv, &m);
        buf_puts(&m, "(只支持 utf-8 / base64)");
        send_bad_buf(s, &m);
        buf_free(&m);
        free(mh);
        free(rel);
        return 1;
    }

    char *raw = NULL;
    size_t rawn = 0;
    if (enc_base64) {
        char *berr = NULL;
        if (!b64_decode(content, content_n, &raw, &rawn, &berr)) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "base64 解码失败:");
            buf_puts(&m, berr);
            send_bad_buf(s, &m);
            buf_free(&m);
            free(berr);
            free(mh);
            free(rel);
            return 1;
        }
    } else {
        raw = xmalloc(content_n + 1);
        if (content_n) memcpy(raw, content, content_n);
        raw[content_n] = 0;
        rawn = content_n;
    }

    /* expect_sha256：乐观并发护栏 */
    const jnode *sv = jget(b, "expect_sha256");
    int truthy = 0;
    if (sv) {
        switch (bjtype(sv)) {
        case BJ_NULL: truthy = 0; break;
        case BJ_BOOL: truthy = jbool(sv); break;
        case BJ_INT: truthy = jint(sv) != 0; break;
        case BJ_FLOAT: truthy = jfloat(sv) != 0.0; break;
        case BJ_STR: truthy = jstrlen(sv) != 0; break;
        case BJ_ARR: truthy = jlen(sv) != 0; break;
        case BJ_OBJ: truthy = jlen(sv) != 0; break;
        default: truthy = 0; break;
        }
    }
    if (truthy) {
        wchar_t *ap = repo_wpath(rel);
        char *cur = NULL;
        size_t curn = 0;
        if (ap) read_whole_w(ap, &cur, &curn);
        free(ap);
        char hex[65];
        sha256_hex(cur ? cur : "", cur ? curn : 0, hex);
        free(cur);
        if (bjtype(sv) != BJ_STR) {
            /* Python 在这里会 TypeError，被 server 兜成 400：
             *   int/bool/float → "'int' object is not subscriptable"
             *   dict           → "unhashable type: 'slice'"
             *   list           → 可切片，正常走 sha 不符文案 */
            if (bjtype(sv) == BJ_ARR) {
                buf m;
                buf_init(&m);
                buf_puts(&m, "工作区已被改动(期望 sha256=");
                size_t lim = jlen(sv) < 12 ? jlen(sv) : 12;
                buf_putc(&m, '[');
                for (size_t i = 0; i < lim; i++) {
                    if (i) buf_puts(&m, ", ");
                    w_repr(jat(sv, i), &m);
                }
                buf_putc(&m, ']');
                buf_puts(&m, ",实际 ");
                buf_append(&m, hex, 12);
                buf_puts(&m, "),拒绝覆盖。请重新读取文件再提交");
                send_bad_buf(s, &m);
                buf_free(&m);
            } else if (bjtype(sv) == BJ_OBJ) {
                send_bad(s, "请求参数不合法:unhashable type: 'slice'");
            } else {
                buf m;
                buf_init(&m);
                buf_puts(&m, "请求参数不合法:'");
                buf_puts(&m, jtype_name(sv));
                buf_puts(&m, "' object is not subscriptable");
                send_bad_buf(s, &m);
                buf_free(&m);
            }
            free(raw);
            free(mh);
            free(rel);
            return 1;
        }
        if (strcmp(hex, jstr(sv)) != 0) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "工作区已被改动(期望 sha256=");
            size_t el = jstrlen(sv) < 12 ? jstrlen(sv) : 12;
            buf_append(&m, jstr(sv), el);
            buf_puts(&m, ",实际 ");
            buf_append(&m, hex, 12);
            buf_puts(&m, "),拒绝覆盖。请重新读取文件再提交");
            send_bad_buf(s, &m);
            buf_free(&m);
            free(raw);
            free(mh);
            free(rel);
            return 1;
        }
    }

    int rc = do_resolve(s, rel, mh, raw, rawn);
    free(raw);
    free(mh);
    free(rel);
    return rc;
}

/* ================================================================ /api/conflict/choose */

/* `git cat-file blob <sha>` → raw。取不到返回 0。 */
static int blob_get(const char *sha, char **raw, size_t *rawn) {
    *raw = NULL;
    *rawn = 0;
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "cat-file", "blob", sha, NULL };
    int rc = wc_git(args, &o, &e);
    buf_free(&e);
    if (rc != 0) { buf_free(&o); return 0; }
    *rawn = o.n;
    *raw = xmalloc(o.n + 1);
    if (o.n) memcpy(*raw, o.p, o.n);
    (*raw)[o.n] = 0;
    buf_free(&o);
    return 1;
}

static int h_choose(SOCKET s, const jnode *b) {
    char *perr = NULL;
    char *rel = arg_rel(b, "file", &perr);
    if (!rel) { send_bad(s, perr); free(perr); return 1; }

    char *mh = NULL, *merr = NULL;
    if (!require_merge(&mh, &merr)) {
        send_bad(s, merr);
        free(merr);
        free(rel);
        return 1;
    }

    size_t nst = 0;
    wcstage *st = stages_of(rel, &nst);
    if (nst == 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "该路径没有未合并的索引条目,不是冲突文件:");
        buf_puts(&m, rel);
        send_bad_buf(s, &m);
        buf_free(&m);
        wcstage_free(st, nst);
        free(mh);
        free(rel);
        return 1;
    }

    /* side: b.get("side", "") —— 列表/字典是 unhashable（Python 里 400） */
    const jnode *sv = jget(b, "side");
    const char *side = "";
    if (sv && bjtype(sv) == BJ_ARR) {
        send_bad(s, "请求参数不合法:unhashable type: 'list'");
        wcstage_free(st, nst);
        free(mh);
        free(rel);
        return 1;
    }
    if (sv && bjtype(sv) == BJ_OBJ) {
        send_bad(s, "请求参数不合法:unhashable type: 'dict'");
        wcstage_free(st, nst);
        free(mh);
        free(rel);
        return 1;
    }
    if (sv && bjtype(sv) == BJ_STR) side = jstr(sv);
    int stage = 0;
    if (!strcmp(side, "ours")) stage = 2;
    else if (!strcmp(side, "theirs")) stage = 3;
    else if (!strcmp(side, "base")) stage = 1;
    else {
        buf m;
        buf_init(&m);
        buf_puts(&m, "side 只能是 ours/theirs/base,收到 ");
        if (sv) w_repr(sv, &m);
        else buf_puts(&m, "''");
        send_bad_buf(s, &m);
        buf_free(&m);
        wcstage_free(st, nst);
        free(mh);
        free(rel);
        return 1;
    }

    const char *sha = st[nst - 1].sha[stage];
    char *raw = NULL;
    size_t rawn = 0;
    int exists = 0;
    int binary = 0;
    if (sha) {
        exists = blob_get(sha, &raw, &rawn);
        if (exists) {
            size_t lim = rawn < 8000 ? rawn : 8000;
            binary = memchr(raw, 0, lim) != NULL;
        }
    }
    if (!exists) binary = 1;

    if (!exists || binary) {
        buf m;
        buf_init(&m);
        buf_puts(&m, side);
        buf_puts(&m, " 侧在 base 之上不存在(");
        buf_puts(&m, exists ? "二进制" : "被删除");
        buf_puts(&m, "),无法整份取用 —— 要删掉这个文件请用 delete 动作。");
        send_bad_buf(s, &m);
        buf_free(&m);
        free(raw);
        wcstage_free(st, nst);
        free(mh);
        free(rel);
        return 1;
    }

    /* 文本口径：decode("utf-8", "replace") 再 encode("utf-8") */
    char *text = u8_sanitize(raw, rawn);
    size_t textn = strlen(text);
    free(raw);
    wcstage_free(st, nst);
    int rc = do_resolve(s, rel, mh, text, textn);
    free(text);
    free(mh);
    free(rel);
    return rc;
}

/* ================================================================ /api/conflict/delete */

static int h_delete(SOCKET s, const jnode *b) {
    char *perr = NULL;
    char *rel = arg_rel(b, "file", &perr);
    if (!rel) { send_bad(s, perr); free(perr); return 1; }

    char *mh = NULL, *merr = NULL;
    if (!require_merge(&mh, &merr)) {
        send_bad(s, merr);
        free(merr);
        free(rel);
        return 1;
    }

    size_t nst = 0;
    wcstage *st = stages_of(rel, &nst);
    wcstage_free(st, nst);
    if (nst == 0) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "该路径不是未合并路径,拒绝删除:");
        buf_puts(&m, rel);
        send_bad_buf(s, &m);
        buf_free(&m);
        free(mh);
        free(rel);
        return 1;
    }

    const jnode *sv = jget(b, "expect_sha256");
    int truthy = 0;
    if (sv) {
        switch (bjtype(sv)) {
        case BJ_NULL: truthy = 0; break;
        case BJ_BOOL: truthy = jbool(sv); break;
        case BJ_INT: truthy = jint(sv) != 0; break;
        case BJ_FLOAT: truthy = jfloat(sv) != 0.0; break;
        case BJ_STR: truthy = jstrlen(sv) != 0; break;
        case BJ_ARR: truthy = jlen(sv) != 0; break;
        case BJ_OBJ: truthy = jlen(sv) != 0; break;
        default: truthy = 0; break;
        }
    }
    if (truthy) {
        wchar_t *ap = repo_wpath(rel);
        char *cur = NULL;
        size_t curn = 0;
        if (ap) read_whole_w(ap, &cur, &curn);
        free(ap);
        char hex[65];
        sha256_hex(cur ? cur : "", cur ? curn : 0, hex);
        free(cur);
        if (bjtype(sv) != BJ_STR || strcmp(hex, jstr(sv)) != 0) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "工作区已被改动,拒绝删除。请重新读取:");
            buf_puts(&m, rel);
            send_bad_buf(s, &m);
            buf_free(&m);
            free(mh);
            free(rel);
            return 1;
        }
    }

    wchar_t *ap = repo_wpath(rel);
    if (ap) {
        DWORD attr = GetFileAttributesW(ap);
        if (attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
            if (!DeleteFileW(ap)) {
                DWORD e = GetLastError();
                buf m;
                buf_init(&m);
                buf_puts(&m, "删除工作区文件失败 —— ");
                buf_puts(&m, rel);
                buf_puts(&m, ": ");
                buf_puts(&m, strerror((int)e));
                send_bad_buf(s, &m);
                buf_free(&m);
                free(ap);
                free(mh);
                free(rel);
                return 1;
            }
        }
        free(ap);
    }

    {
        const char *args[] = { "rm", "-q", "--", rel, NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        wc_git(args, &o, &e);          /* check=False */
        buf_free(&o);
        buf_free(&e);
    }
    {
        const char *args[] = { "add", "--", rel, NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        wc_git(args, &o, &e);          /* check=False */
        buf_free(&o);
        buf_free(&e);
    }

    char **paths = NULL;
    size_t np = 0;
    paths = conflict_paths(&np);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"path\":");
    json_str(&o, rel);
    buf_puts(&o, ",\"deleted\":true");
    put_remaining(&o, paths, np);
    put_merge_head(&o, mh);
    buf_putc(&o, '}');
    send_obj(s, &o);
    buf_free(&o);
    free_strv(paths, np);
    free(mh);
    free(rel);
    return 1;
}

/* ================================================================ /api/conflict/commit */

/* `git rev-parse --verify --quiet HEAD` → sha 或 NULL。 */
static char *rev_parse_head(void) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *args[] = { "rev-parse", "--verify", "--quiet", "HEAD", NULL };
    int rc = wc_git(args, &o, &e);
    buf_free(&e);
    char *r = NULL;
    if (rc == 0) {
        char *s = py_strip_dup(buf_cstr(&o));
        if (*s) r = s;
        else free(s);
    }
    buf_free(&o);
    return r;
}

static int h_commit(SOCKET s, const jnode *b) {
    /* message 类型：None 合法（用 MERGE_MSG 的默认信息），非字符串 → 400 */
    const jnode *mv = jget(b, "message");
    if (mv && bjtype(mv) != BJ_NULL && !jis_str(mv)) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "message 必须是字符串,收到 ");
        buf_puts(&m, jtype_name(mv));
        send_bad_buf(s, &m);
        buf_free(&m);
        return 1;
    }
    const char *message = (mv && jis_str(mv)) ? jstr(mv) : NULL;
    size_t message_n = (mv && jis_str(mv)) ? jstrlen(mv) : 0;
    int message_truthy = 0;
    if (message) message_truthy = message_n != 0;

    char *mh = NULL, *merr = NULL;
    if (!require_merge(&mh, &merr)) {
        send_bad(s, merr);
        free(merr);
        return 1;
    }

    char *msg_raw = read_gd("MERGE_MSG");
    if (!msg_raw) msg_raw = xstrdup("");

    size_t nb = 0;
    char **branches = branches_from_msg(msg_raw, &nb);

    /* default = 第一个非空行（strip 后） */
    char *dflt = NULL;
    {
        const char *p = msg_raw;
        while (*p) {
            const char *nl = strchr(p, '\n');
            size_t ln = nl ? (size_t)(nl - p) : strlen(p);
            char *line = xstrndup(p, ln);
            char *st = py_strip_dup(line);
            free(line);
            if (*st) { dflt = st; break; }
            free(st);
            if (!nl) break;
            p = nl + 1;
        }
    }
    if (!dflt) dflt = xstrdup("");
    if (!*dflt) {
        send_bad(s, ".git/MERGE_MSG 缺失或为空,拿不到默认提交信息,已拒绝提交");
        free(dflt);
        free_strv(branches, nb);
        free(msg_raw);
        free(mh);
        return 1;
    }

    {
        size_t np = 0;
        char **paths = conflict_paths(&np);
        if (np) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "还有 %zu 个文件没解决,已拒绝提交:\n      ", np);
            for (size_t i = 0; i < np; i++) {
                if (i) buf_puts(&m, "\n      ");
                buf_puts(&m, paths[i]);
            }
            send_bad_buf(s, &m);
            buf_free(&m);
            free_strv(paths, np);
            free(dflt);
            free_strv(branches, nb);
            free(msg_raw);
            free(mh);
            return 1;
        }
        free_strv(paths, np);
    }

    /* F-06 防线：仓里有指向仓外的联接就拒绝提交 */
    {
        size_t nl = 0;
        leak_t *leaks = escaping_links(&nl, 16);
        if (nl) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "仓里有指向仓外的链接(");
            for (size_t i = 0; i < nl && i < 8; i++) {
                if (i) buf_puts(&m, ", ");
                buf_puts(&m, leaks[i].path ? leaks[i].path : "");
                buf_puts(&m, " -> ");
                buf_puts(&m, leaks[i].target ? leaks[i].target : "");
            }
            buf_puts(&m, "),已拒绝提交。git add -A 会穿过目录联接,"
                         "把仓外的文件收进索引、再写进历史, 且删掉链接也删不掉历史。"
                         "请先把它移出工作区, 或把它移进仓内。共发现 ");
            buf_printf(&m, "%zu", nl);
            buf_puts(&m, " 个。");
            send_bad_buf(s, &m);
            buf_free(&m);
            leak_free(leaks, nl);
            free(dflt);
            free_strv(branches, nb);
            free(msg_raw);
            free(mh);
            return 1;
        }
        leak_free(leaks, nl);
    }

    /* text = (message or "").strip() or default */
    char *msg_stripped = message ? py_strip_dup(message) : xstrdup("");
    char *text = xstrdup(*msg_stripped ? msg_stripped : dflt);
    /* messageSource 看的是**去空白后**的 message，不是原始真值：
     * `{"message": "   "}` 在 Python 里 messageSource 是 "MERGE_MSG"。 */
    int src_merge_msg = !*msg_stripped;
    if (message_truthy && nb) {
        int hit = 0;
        for (size_t i = 0; i < nb; i++) {
            if (strstr(text, branches[i])) { hit = 1; break; }
        }
        if (!hit) {
            buf t;
            buf_init(&t);
            buf_puts(&t, text);
            buf_puts(&t, "\n\nMerged: ");
            for (size_t i = 0; i < nb; i++) {
                if (i) buf_puts(&t, ", ");
                buf_puts(&t, branches[i]);
            }
            free(text);
            text = xstrdup(buf_cstr(&t));
            buf_free(&t);
        }
    }

    /* 作用域受限的 add（替代裸 `add -A`） */
    {
        size_t nsc = 0;
        char **scope = merge_add_scope(&nsc);
        if (nsc) {
            const char **args = xmalloc((nsc + 3) * sizeof(char *));
            args[0] = "add";
            args[1] = "--";
            for (size_t i = 0; i < nsc; i++) args[2 + i] = scope[i];
            args[2 + nsc] = NULL;
            buf o, e;
            buf_init(&o);
            buf_init(&e);
            int rc = wc_git(args, &o, &e);
            if (rc != 0) {
                char *msg = git_fail_msg(args, rc, buf_cstr(&e), e.n, buf_cstr(&o), o.n);
                send_bad(s, msg);
                free(msg);
                buf_free(&o);
                buf_free(&e);
                free(args);
                free_strv(scope, nsc);
                free(msg_stripped);
                free(text);
                free(dflt);
                free_strv(branches, nb);
                free(msg_raw);
                free(mh);
                return 1;
            }
            buf_free(&o);
            buf_free(&e);
            free(args);
        }
        free_strv(scope, nsc);
    }

    {
        const char *args[] = { "commit", "-m", text, NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        int rc = wc_git(args, &o, &e);
        if (rc != 0) {
            char *msg = git_fail_msg(args, rc, buf_cstr(&e), e.n, buf_cstr(&o), o.n);
            send_bad(s, msg);
            free(msg);
            buf_free(&o);
            buf_free(&e);
            free(msg_stripped);
            free(text);
            free(dflt);
            free_strv(branches, nb);
            free(msg_raw);
            free(mh);
            return 1;
        }
        buf_free(&o);
        buf_free(&e);
    }

    char *head = rev_parse_head();
    char *subject = NULL;
    {
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        const char *args[] = { "log", "-1", "--pretty=%s", NULL };
        wc_git(args, &o, &e);
        buf_free(&e);
        subject = py_strip_dup(buf_cstr(&o));
        buf_free(&o);
    }

    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"committed\":true,\"commit\":");
    if (head) json_str(&o, head);
    else buf_puts(&o, "null");
    buf_puts(&o, ",\"shortCommit\":");
    {
        char sc[11];
        size_t n = head ? strlen(head) : 0;
        if (n > 10) n = 10;
        if (n) memcpy(sc, head, n);
        sc[n] = 0;
        json_str(&o, sc);
    }
    buf_puts(&o, ",\"message\":");
    json_str(&o, text);
    buf_puts(&o, ",\"messageSource\":");
    json_str(&o, src_merge_msg ? "MERGE_MSG" : "caller");
    buf_puts(&o, ",\"defaultMessage\":");
    json_str(&o, dflt);
    buf_puts(&o, ",\"branches\":[");
    for (size_t i = 0; i < nb; i++) {
        if (i) buf_putc(&o, ',');
        json_str(&o, branches[i]);
    }
    buf_puts(&o, "]");
    put_merge_head(&o, mh);
    buf_puts(&o, ",\"subject\":");
    json_str(&o, subject ? subject : "");
    buf_putc(&o, '}');
    send_obj(s, &o);
    buf_free(&o);

    free(subject);
    free(head);
    free(msg_stripped);
    free(text);
    free(dflt);
    free_strv(branches, nb);
    free(msg_raw);
    free(mh);
    return 1;
}

/* ================================================================ /api/conflict/abort */

static int h_abort(SOCKET s, const jnode *b) {
    (void)b;
    {
        const char *args[] = { "merge", "--abort", NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        wc_git(args, &o, &e);          /* check=False：不在合并态也回 ok */
        buf_free(&o);
        buf_free(&e);
    }
    /* inMerge = bool(self._read_gd("MERGE_HEAD")) —— 文件存在且非空即为真 */
    char *mh = read_gd("MERGE_HEAD");
    int in_merge = (mh && *mh);
    free(mh);
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"aborted\":true,\"inMerge\":");
    buf_puts(&o, in_merge ? "true" : "false");
    buf_putc(&o, '}');
    send_obj(s, &o);
    buf_free(&o);
    return 1;
}

/* ================================================================ 入口 */

int write_conflict_post(const char *path, const char *query, const char *body,
                        size_t body_len, SOCKET s) {
    (void)query;
    if (strcmp(path, "/api/conflict/resolve") && strcmp(path, "/api/conflict/choose") &&
        strcmp(path, "/api/conflict/delete") && strcmp(path, "/api/conflict/commit") &&
        strcmp(path, "/api/conflict/abort")) {
        return 0;
    }
    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    int rc = 1;
    if (!strcmp(path, "/api/conflict/resolve")) rc = h_resolve(s, b);
    else if (!strcmp(path, "/api/conflict/choose")) rc = h_choose(s, b);
    else if (!strcmp(path, "/api/conflict/delete")) rc = h_delete(s, b);
    else if (!strcmp(path, "/api/conflict/commit")) rc = h_commit(s, b);
    else if (!strcmp(path, "/api/conflict/abort")) rc = h_abort(s, b);
    jfree(b);
    return rc;
}
