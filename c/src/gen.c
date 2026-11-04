/* gen.c —— POST /api/gen：随机测试数据生成器（与 gen.py::ScenarioGen 对位）。
 *
 * 读的参考：
 *   * gen.py:524 generate() —— kind/count 校验、脏仓护栏、分支撞名、结果整形；
 *   * gen.py:589 _make_scenario() —— 每个场景的 base/A/B 三段提交 + 真跑 merge；
 *   * gen.py:154~249 —— 内容生成（_name/_phrase/_stmt/_code_block）；
 *   * gen.py:383~459 —— 四类场景「两个分支各改什么」；
 *   * server.py:1898 Repo.generate() —— 分支前缀里加 `/s<seed>` + 把 scenarios
 *     映射成旧前端的 branches=[{file,branches,expect,id,verified}] + note=summary；
 *   * server.py:2490 的接线：kind 缺省 "all"、count 走 _bi（缺省 3）、seed 原样传。
 *
 * 与 Python **故意不同**的两处（都写在下面的注释与交付说明里）：
 *   1. 随机序列不同。Python 的 random.Random(int) 依赖 Mersenne Twister 的播种，
 *      这里用 FNV-1a 64 派生种子 + splitmix64。要求只是「同 seed 同结果、
 *      不同 seed 不同结果」，不是逐字节复刻 gen.py 的随机流。
 *   2. count 有上限 GEN_MAX_COUNT。Python 没有上限，count=10^6 时它会把
 *      200 万个分支名逐个 rev-parse（实测量级：一天也跑不完），这里直接 400。
 *      1..100 的语义与 Python 完全一致。
 *
 * 响应形状、错误码、错误文案逐字对齐 Python（`_err(e, 400)` → {"ok":false,"error":...}，
 * **没有** code 字段）。所有失败路径都是 400，绝不 500。
 */
#include "routes.h"

#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "bodyjson.h"
#include "git.h"
#include "http.h"
#include "json.h"
#include "params.h"
#include "util.h"
#include "writeutil.h"

/* ---------------------------------------------------------------- 常量 */

#define GEN_PREFIX        "aigit_gen_"
#define GEN_BRANCH_PREFIX "aigit_gen"
#define GEN_COMMIT_TAG    "aigit"

/*: count 上限（Python 无上限，见文件头说明）。 */
#define GEN_MAX_COUNT 100

#define GEN_KIND_HINT \
    "kind 只能是 mergeable / unrelated / adddel / conflict 或 all"

#define GEN_EXPECT_CLEAN    "合并成功:status 干净且 U 为空"
#define GEN_EXPECT_CONFLICT "合并必然冲突:U 列表非空"

static const char *const GEN_KINDS[] = {
    "mergeable", "unrelated", "adddel", "conflict"
};
#define GEN_NKINDS (sizeof(GEN_KINDS) / sizeof(GEN_KINDS[0]))

/* ---------------------------------------------------------------- 小工具 */

static char *fmt_alloc(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    char tmp[8192];
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n < 0) return xstrdup("");
    if ((size_t)n < sizeof(tmp)) return xstrdup(tmp);
    char *o = xmalloc((size_t)n + 1);
    va_start(ap, fmt);
    vsnprintf(o, (size_t)n + 1, fmt, ap);
    va_end(ap);
    return o;
}

/* `text=True` 的 universal newlines：\r\n 与孤立 \r 折成 \n。 */
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

/* 就地 strip（Python str.strip 的 ASCII 部分）。 */
/* 把 buf 里的 git 输出转成 Python `text=True` + `.strip()` 的字符串。 */
static char *text_strip(const buf *b) {
    if (!b || !b->p) return xstrdup("");
    char *t = py_text(b->p, b->n);
    return py_strip(t);
}

/* Python `int(str)` 的语义（只做整数那一档）：前后空白、正负号、
 * 数字之间的单下划线。wbody_int 要求「整串就是数字」，比 Python 严 ——
 * 对 /api/gen 来说 `{"count":" 3 "}` 与 `{"count":"1_0"}` 在 Python 侧是
 * 合法请求（200），所以这里单独实现，别用共享 helper 的字符串分支。 */
/* ---------------------------------------------------------------- 字符串数组 */

typedef struct {
    char **v;
    size_t n, cap;
} strv;

static void sv_init(strv *s) { s->v = NULL; s->n = 0; s->cap = 0; }

static void sv_push(strv *s, char *own) {
    if (s->n == s->cap) {
        s->cap = s->cap ? s->cap * 2 : 8;
        s->v = xrealloc(s->v, s->cap * sizeof(*s->v));
    }
    s->v[s->n++] = own;
}

static void sv_pushs(strv *s, const char *t) { sv_push(s, xstrdup(t)); }

static void sv_free(strv *s) {
    for (size_t i = 0; i < s->n; i++) free(s->v[i]);
    free(s->v);
    sv_init(s);
}

static void sv_copy_range(strv *dst, const strv *src, size_t from, size_t to) {
    if (to > src->n) to = src->n;
    for (size_t i = from; i < to; i++) sv_pushs(dst, src->v[i]);
}

static int sv_cmp(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

/* `sorted(set(x))` */
static void sv_sort_unique(strv *s) {
    if (s->n < 2) return;
    qsort(s->v, s->n, sizeof(*s->v), sv_cmp);
    size_t w = 1;
    for (size_t i = 1; i < s->n; i++) {
        if (!strcmp(s->v[i], s->v[w - 1])) free(s->v[i]);
        else s->v[w++] = s->v[i];
    }
    s->n = w;
}

/* ---------------------------------------------------------------- 确定性随机 */

typedef struct { uint64_t s; } rng;

static uint64_t fnv1a64(const char *p) {
    uint64_t h = 1469598103934665603ULL;
    for (; *p; p++) {
        h ^= (unsigned char)*p;
        h *= 1099511628211ULL;
    }
    return h;
}

static void rng_seed(rng *r, const char *key) { r->s = fnv1a64(key); }

static uint64_t rng_u64(rng *r) {                     /* splitmix64 */
    uint64_t z = (r->s += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

static long long rng_below(rng *r, long long n) {
    if (n <= 0) return 0;
    return (long long)(rng_u64(r) % (uint64_t)n);
}

static long long rng_int(rng *r, long long a, long long b) {   /* randint 含两端 */
    if (b < a) return a;
    return a + rng_below(r, b - a + 1);
}

static double rng_double(rng *r) {                            /* [0,1) */
    return (double)(rng_u64(r) >> 11) * (1.0 / 9007199254740992.0);
}

/* ---------------------------------------------------------------- 内容词表 */

static const char *const P_NOUNS[] = {
    "cache", "session", "buffer", "packet", "record", "shard", "token",
    "cursor", "worker", "router", "config", "profile", "ledger", "vector",
    "matrix", "channel", "segment", "handle", "region", "bucket"
};
static const char *const P_VERBS[] = {
    "load", "store", "flush", "resolve", "commit", "rebuild", "probe",
    "compact", "encode", "decode", "scan", "merge", "purge", "rebalance"
};
static const char *const P_QUALS[] = {
    "primary", "shadow", "pending", "stale", "warm", "cold", "local",
    "remote", "legacy", "shared"
};
static const char *const P_ADJ[] = {
    "total", "current", "next", "last", "inner", "outer", "raw", "final"
};
static const char *const P_LEVELS[] = { "info", "warn", "debug" };

#define N_NOUNS (sizeof(P_NOUNS) / sizeof(P_NOUNS[0]))
#define N_VERBS (sizeof(P_VERBS) / sizeof(P_VERBS[0]))
#define N_QUALS (sizeof(P_QUALS) / sizeof(P_QUALS[0]))
#define N_ADJ   (sizeof(P_ADJ) / sizeof(P_ADJ[0]))
#define N_LEVELS (sizeof(P_LEVELS) / sizeof(P_LEVELS[0]))

/*: 每个生成文件的首尾样板（gen.py::_BOILERPLATE）。 */
static const char *const P_BOILER[] = {
    "# aigit generated fixture -- do not edit by hand",
    "# regenerate with: python gen.py --root <repo> --kind <kind>"
};

static void put_indent(buf *o, int depth) {
    for (int i = 0; i < depth; i++) buf_puts(o, "    ");
}

static void put_name(rng *r, buf *o) {
    buf_puts(o, P_NOUNS[rng_below(r, (long long)N_NOUNS)]);
    buf_putc(o, '_');
    buf_puts(o, P_VERBS[rng_below(r, (long long)N_VERBS)]);
}

static void put_phrase(rng *r, buf *o) {
    long long k = rng_int(r, 3, 7);
    long long pool = (long long)(N_ADJ + N_NOUNS + N_VERBS);
    for (long long i = 0; i < k; i++) {
        long long idx = rng_below(r, pool);
        const char *w;
        if (idx < (long long)N_ADJ) w = P_ADJ[idx];
        else if (idx < (long long)(N_ADJ + N_NOUNS)) w = P_NOUNS[idx - (long long)N_ADJ];
        else w = P_VERBS[idx - (long long)(N_ADJ + N_NOUNS)];
        if (i) buf_putc(o, ' ');
        buf_puts(o, w);
    }
}

/* 一行「看起来像代码」的语句。 */
static void put_stmt(rng *r, int depth, buf *o) {
    put_indent(o, depth);
    long long k = rng_below(r, 9);
    buf a, b;
    buf_init(&a);
    buf_init(&b);
    put_name(r, &a);
    put_name(r, &b);
    switch (k) {
    case 0: {
        long long t = rng_int(r, 1, 90);
        buf_printf(o, "%s = %s(%s, timeout=%lld)", buf_cstr(&a), buf_cstr(&b),
                   buf_cstr(&a), t);
        break;
    }
    case 1: {
        const char *q = P_QUALS[rng_below(r, (long long)N_QUALS)];
        long long n = rng_int(r, 0, 999);
        buf_printf(o, "if %s.%s > %lld:", buf_cstr(&a), q, n);
        break;
    }
    case 2: {
        long long n = rng_int(r, 0, 64);
        buf_printf(o, "%s = max(%s, %lld)  # ", buf_cstr(&b), buf_cstr(&a), n);
        put_phrase(r, o);
        break;
    }
    case 3: {
        const char *lv = P_LEVELS[rng_below(r, (long long)N_LEVELS)];
        buf_printf(o, "log.%s(\"", lv);
        put_phrase(r, o);
        buf_puts(o, "\")");
        break;
    }
    case 4: {
        long long n = rng_int(r, 2, 32);
        buf_printf(o, "for i in range(%lld):", n);
        break;
    }
    case 5:
        buf_printf(o, "assert isinstance(%s, dict), ", buf_cstr(&a));
        put_phrase(r, o);
        break;
    case 6: {
        long long idx = rng_int(r, 0, 8);
        long long num = rng_int(r, 0, 999);
        long long pick = rng_below(r, 4);      /* True/False/None/num */
        buf_printf(o, "%s[%lld] = ", buf_cstr(&a), idx);
        if (pick == 0) buf_puts(o, "True");
        else if (pick == 1) buf_puts(o, "False");
        else if (pick == 2) buf_puts(o, "None");
        else buf_printf(o, "%lld", num);
        break;
    }
    case 7:
        buf_printf(o, "yield %s", buf_cstr(&a));
        break;
    default:
        buf_printf(o, "return %s  # ", buf_cstr(&a));
        put_phrase(r, o);
        break;
    }
    buf_free(&a);
    buf_free(&b);
}

/* gen.py::_code_block —— 返回 n + 2*len(BOILER) 行。 */
static void code_block(rng *r, long long n, strv *out) {
    sv_pushs(out, P_BOILER[0]);
    sv_pushs(out, P_BOILER[1]);
    int depth = 0;
    for (long long i = 0; i < n; i++) {
        double rr = rng_double(r);
        if (out->n > 2 && rr < 0.08) {
            /* 逐字重复前面某一行（跳过样板区） */
            long long j = rng_below(r, (long long)(out->n - 2));
            sv_pushs(out, out->v[2 + (size_t)j]);
            continue;
        }
        double u = rng_double(r);
        if (u < 0.11 && depth < 3) depth++;
        else if (u < 0.25 && depth > 0) depth--;

        buf line;
        buf_init(&line);
        put_indent(&line, depth);
        if (rr < 0.15) {
            buf_puts(&line, "# ");
            put_phrase(r, &line);
        } else if (rr < 0.26) {
            buf nm;
            buf_init(&nm);
            put_name(r, &nm);
            const char *argn = P_NOUNS[rng_below(r, (long long)N_NOUNS)];
            const char *q = P_QUALS[rng_below(r, (long long)N_QUALS)];
            long long dv = rng_int(r, 0, 16);
            buf_printf(&line, "def %s(%s, *, %s=%lld):", buf_cstr(&nm), argn, q, dv);
            buf_free(&nm);
        } else if (rr < 0.37) {
            long long k = rng_int(r, 2, 32);
            buf_printf(&line, "for i in range(%lld):", k);
        } else if (rr < 0.49) {
            buf nm;
            buf_init(&nm);
            put_name(r, &nm);
            const char *q = P_QUALS[rng_below(r, (long long)N_QUALS)];
            long long k = rng_int(r, 0, 999);
            buf_printf(&line, "if %s.%s > %lld:", buf_cstr(&nm), q, k);
            buf_free(&nm);
        } else {
            put_stmt(r, depth, &line);
        }
        sv_push(out, xstrdup(buf_cstr(&line)));
        buf_free(&line);
    }
    sv_pushs(out, P_BOILER[0]);
    sv_pushs(out, P_BOILER[1]);
}

/* ---------------------------------------------------------------- SHA-256
 * 只为一件事：adddel 场景里 `aigit_gen_adddel_fresh_%d.txt` 的 %d 是
 * int(sha256(doomed_rel).hexdigest()[:4], 16)。用 FNV 也能确定，但这样
 * 文件名与 Python 版**逐字相同**，跨版本 diff 时噪音更小。 */

typedef struct {
    uint32_t h[8];
    uint64_t len;
    unsigned char blk[64];
    size_t n;
} sha256_ctx;

static uint32_t ror32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static const uint32_t SHA_K[64] = {
    0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,
    0x923f82a4u,0xab1c5ed5u,0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,
    0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,0xe49b69c1u,0xefbe4786u,
    0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
    0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,
    0x06ca6351u,0x14292967u,0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,
    0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,0xa2bfe8a1u,0xa81a664bu,
    0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
    0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,
    0x5b9cca4fu,0x682e6ff3u,0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,
    0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
};

static void sha256_block(sha256_ctx *c, const unsigned char *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ror32(w[i - 15], 7) ^ ror32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror32(w[i - 2], 17) ^ ror32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
    uint32_t e = c->h[4], f = c->h[5], g = c->h[6], hh = c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = hh + S1 + ch + SHA_K[i] + w[i];
        uint32_t S0 = ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22);
        uint32_t mj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + mj;
        hh = g; g = f; f = e; e = d + t1;
        d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += hh;
}

static void sha256(const void *data, size_t n, unsigned char out[32]) {
    sha256_ctx c;
    static const uint32_t iv[8] = {
        0x6a09e667u,0xbb67ae85u,0x3c6ef372u,0xa54ff53au,
        0x510e527fu,0x9b05688cu,0x1f83d9abu,0x5be0cd19u
    };
    memcpy(c.h, iv, sizeof(iv));
    c.len = 0;
    c.n = 0;
    const unsigned char *p = data;
    size_t left = n;
    c.len = (uint64_t)n * 8;
    while (left >= 64) { sha256_block(&c, p); p += 64; left -= 64; }
    unsigned char tail[128];
    memcpy(tail, p, left);
    tail[left++] = 0x80;
    size_t total = (left <= 56) ? 64 : 128;
    memset(tail + left, 0, total - left);
    for (int i = 0; i < 8; i++)
        tail[total - 1 - i] = (unsigned char)(c.len >> (8 * i));
    sha256_block(&c, tail);
    if (total == 128) sha256_block(&c, tail + 64);
    for (int i = 0; i < 8; i++) {
        out[i * 4] = (unsigned char)(c.h[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(c.h[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(c.h[i] >> 8);
        out[i * 4 + 3] = (unsigned char)c.h[i];
    }
}

/* `int(sha256hex(s)[:4], 16)` */
static long long fresh_number(const char *s) {
    unsigned char d[32];
    static const char hex[] = "0123456789abcdef";
    sha256(s, strlen(s), d);
    char q[5];
    q[0] = hex[d[0] >> 4];
    q[1] = hex[d[0] & 15];
    q[2] = hex[d[1] >> 4];
    q[3] = hex[d[1] & 15];
    q[4] = 0;
    long long v = 0;
    for (int i = 0; i < 4; i++) {
        char c = q[i];
        v = v * 16 + (c >= '0' && c <= '9' ? c - '0' : c - 'a' + 10);
    }
    return v;
}

/* ---------------------------------------------------------------- 路径 / 文件 */

/* 只接受带 GEN_PREFIX 的相对路径（gen.py::_assert_prefix 的同义护栏）。 */
static int rel_ok(const char *rel) {
    if (!rel || strncmp(rel, GEN_PREFIX, strlen(GEN_PREFIX)) != 0) return 0;
    if (strstr(rel, "..")) return 0;
    if (rel[0] == '/' || rel[0] == '\\') return 0;
    if (strchr(rel, ':')) return 0;
    return 1;
}

static wchar_t *repo_path_w(const char *rel) {
    wchar_t *w = u82w(rel);
    if (!w) return NULL;
    for (wchar_t *q = w; *q; q++) if (*q == L'/') *q = L'\\';
    const wchar_t *root = git_root();
    if (!root) { free(w); return NULL; }
    size_t n = wcslen(root) + wcslen(w) + 2;
    wchar_t *p = xmalloc(n * sizeof(wchar_t));
    _snwprintf(p, n, L"%ls\\%ls", root, w);
    free(w);
    return p;
}

/* 逐行写 "\n".join(lines) + "\n"（UTF-8，newline="\n"）。 */
static int write_lines(const char *rel, const strv *lines) {
    if (!rel_ok(rel)) return 0;
    wchar_t *p = repo_path_w(rel);
    if (!p) return 0;
    buf data;
    buf_init(&data);
    for (size_t i = 0; i < lines->n; i++) {
        buf_puts(&data, lines->v[i]);
        buf_putc(&data, '\n');
    }
    HANDLE h = CreateFileW(p, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    int ok = 0;
    if (h != INVALID_HANDLE_VALUE) {
        DWORD wrote = 0;
        size_t want = data.n;
        ok = WriteFile(h, data.p ? data.p : "", (DWORD)want, &wrote, NULL) &&
             wrote == (DWORD)want;
        CloseHandle(h);
    }
    free(p);
    buf_free(&data);
    return ok;
}

static int delete_rel(const char *rel) {
    if (!rel_ok(rel)) return 0;
    wchar_t *p = repo_path_w(rel);
    if (!p) return 0;
    int ok = DeleteFileW(p) ? 1 : 0;
    free(p);
    return ok;
}

/* ---------------------------------------------------------------- git 调用 */

/* gen.py::_GIT_CONFIG：身份 + 换行。`-c` 必须在子命令前，所以拼成前缀 argv。 */
static int gen_git(const char *const *args, buf *out, buf *errp) {
    static const char *pre[] = {
        "-c", "user.name=aigit",
        "-c", "user.email=aigit@localhost",
        "-c", "commit.gpgsign=false",
        "-c", "core.autocrlf=false",
        "-c", "core.safecrlf=false",
    };
    size_t np = sizeof(pre) / sizeof(pre[0]);
    size_t n = 0;
    while (args[n]) n++;
    const char **v = xmalloc((n + np + 1) * sizeof(*v));
    for (size_t i = 0; i < np; i++) v[i] = pre[i];
    for (size_t i = 0; i <= n; i++) v[np + i] = args[i];
    if (out) buf_reset(out);
    if (errp) buf_reset(errp);
    int rc = git_run(v, NULL, 0, out, errp);
    free(v);
    return rc;
}

/* `"git %s 失败(rc=%d): %s" % (" ".join(args[:3]), rc, (stderr or stdout).strip())` */
static char *git_fail_msg(const char *const *args, int rc, const buf *errp,
                          const buf *outp) {
    const buf *src = (errp && errp->n) ? errp : outp;
    char *msg = text_strip(src);
    buf m;
    buf_init(&m);
    buf_puts(&m, "git ");
    for (int i = 0; i < 3 && args[i]; i++) {
        if (i) buf_putc(&m, ' ');
        buf_puts(&m, args[i]);
    }
    buf_printf(&m, " 失败(rc=%d): ", rc);
    buf_puts(&m, msg);
    free(msg);
    char *r = xstrdup(buf_cstr(&m));
    buf_free(&m);
    return r;
}

/* ---------------------------------------------------------------- 仓状态 */

static int repo_is_git(void) {
    const wchar_t *root = git_root();
    if (!root) return 0;
    size_t n = wcslen(root) + 8;
    wchar_t *p = xmalloc(n * sizeof(wchar_t));
    _snwprintf(p, n, L"%ls\\.git", root);
    DWORD a = GetFileAttributesW(p);
    free(p);
    return a != INVALID_FILE_ATTRIBUTES;
}

static int git_has_head(void) {
    const char *a[] = { "rev-parse", "--verify", "HEAD", NULL };
    return gen_git(a, NULL, NULL) == 0;
}

static int branch_exists(const char *name) {
    char *ref = fmt_alloc("refs/heads/%s", name);
    const char *a[] = { "rev-parse", "--verify", "--quiet", ref, NULL };
    int rc = gen_git(a, NULL, NULL);
    free(ref);
    return rc == 0;
}

/* `git status --porcelain=v1 --untracked-files=all` 的 .strip() 结果。 */
static char *porcelain_text(void) {
    const char *a[] = { "status", "--porcelain=v1", "--untracked-files=all", NULL };
    buf o;
    buf_init(&o);
    gen_git(a, &o, NULL);
    char *t = text_strip(&o);
    buf_free(&o);
    return t;
}

static void unmerged_list(strv *out) {
    const char *a[] = { "diff", "--name-only", "--diff-filter=U", NULL };
    buf o;
    buf_init(&o);
    gen_git(a, &o, NULL);
    char *t = py_text(o.p ? o.p : "", o.n);
    char *line = t;
    while (line && *line) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = 0;
        char *s = py_strip(line);
        if (*s) sv_pushs(out, s);
        line = nl ? nl + 1 : NULL;
    }
    free(t);
    buf_free(&o);
}

/* 把 Python 行数统计拆出来（脏仓文案里要 len(lines)）。 */
static size_t count_lines(const char *s) {
    if (!s || !*s) return 0;
    size_t n = 0;
    for (const char *p = s; *p; p++) if (*p == '\n') n++;
    return n + 1;
}

/* ---------------------------------------------------------------- 场景数据结构 */

typedef struct {
    char *rel;
    strv lines;
    int deleted;
} edit;

typedef struct {
    strv files;
    char *a_edit;
    char *b_edit;
    edit *a;
    size_t na;
    edit *b;
    size_t nb;
    long long gap;
    int has_gap;
} plan;

typedef struct {
    char *id;
    char *kind;
    long long index;
    char *base;
    char *a_name;
    char *b_name;
    strv files;
    char *a_edit;
    char *b_edit;
    char *expect;
    int verified;
    int merged_ok;
    int first_merge_ok;
    int worktree_clean;          /* -1 → null */
    strv unmerged;
    int has_gap;
    long long gap;
    char *merge_output;
} scenario;

static void scenario_free(scenario *s) {
    free(s->id);
    free(s->kind);
    free(s->base);
    free(s->a_name);
    free(s->b_name);
    sv_free(&s->files);
    free(s->a_edit);
    free(s->b_edit);
    free(s->expect);
    sv_free(&s->unmerged);
    free(s->merge_output);
    memset(s, 0, sizeof(*s));
}

static void edit_free(edit *e) {
    free(e->rel);
    sv_free(&e->lines);
}

static void plan_free(plan *p) {
    sv_free(&p->files);
    free(p->a_edit);
    free(p->b_edit);
    for (size_t i = 0; i < p->na; i++) edit_free(&p->a[i]);
    free(p->a);
    for (size_t i = 0; i < p->nb; i++) edit_free(&p->b[i]);
    free(p->b);
    memset(p, 0, sizeof(*p));
}

static edit *edit_new(edit **arr, size_t *n, const char *rel) {
    *arr = xrealloc(*arr, (*n + 1) * sizeof(**arr));
    edit *e = &(*arr)[(*n)++];
    memset(e, 0, sizeof(*e));
    e->rel = xstrdup(rel);
    sv_init(&e->lines);
    return e;
}

/* ---------------------------------------------------------------- 四类场景 */

/* mergeable：同一文件的头部 / 尾部两块，中间留 >= 20 行不动。 */
static int apply_mergeable(rng *r, edit *base, size_t nb, plan *pl, char **err) {
    (void)nb;
    const char *rel = base[0].rel;
    long long n = (long long)base[0].lines.n;
    long long a_len = rng_int(r, 3, 7);
    long long b_len = rng_int(r, 3, 7);
    long long a_start = 0;
    long long b_start = n - b_len;
    long long gap = b_start - (a_start + a_len);
    if (gap < 20) {
        *err = fmt_alloc("内部错误:种子行数不足以留出安全间隔(n=%lld gap=%lld)",
                         n, gap);
        return 0;
    }

    strv ablk;
    sv_init(&ablk);
    code_block(r, a_len, &ablk);
    strv al;
    sv_init(&al);
    sv_copy_range(&al, &base[0].lines, 0, (size_t)a_start);
    for (size_t i = 0; i < ablk.n; i++) sv_pushs(&al, ablk.v[i]);
    sv_copy_range(&al, &base[0].lines, (size_t)(a_start + a_len), base[0].lines.n);
    sv_free(&ablk);

    strv bblk;
    sv_init(&bblk);
    code_block(r, b_len, &bblk);
    strv bl;
    sv_init(&bl);
    sv_copy_range(&bl, &base[0].lines, 0, (size_t)b_start);
    for (size_t i = 0; i < bblk.n; i++) sv_pushs(&bl, bblk.v[i]);
    sv_copy_range(&bl, &base[0].lines, (size_t)(b_start + b_len), base[0].lines.n);
    sv_free(&bblk);

    edit *ea = edit_new(&pl->a, &pl->na, rel);
    ea->lines = al;
    edit *eb = edit_new(&pl->b, &pl->nb, rel);
    eb->lines = bl;

    sv_pushs(&pl->files, rel);
    pl->a_edit = fmt_alloc("头部第 %lld 行起 %lld 行", a_start + 1, a_len);
    pl->b_edit = fmt_alloc("尾部第 %lld 行起 %lld 行", b_start + 1, b_len);
    pl->gap = gap;
    pl->has_gap = 1;
    return 1;
}

/* unrelated：两个分支改**完全不同的文件**。 */
static int apply_unrelated(rng *r, edit *base, size_t nb, plan *pl, char **err) {
    (void)err;
    if (nb < 2) return 0;
    const char *a_rel = base[0].rel;
    const char *b_rel = base[1].rel;

    long long an = rng_int(r, 6, 14);
    strv al;
    sv_init(&al);
    code_block(r, an, &al);

    long long bn = rng_int(r, 6, 14);
    strv bl;
    sv_init(&bl);
    code_block(r, bn, &bl);

    edit *ea = edit_new(&pl->a, &pl->na, a_rel);
    ea->lines = al;
    edit *eb = edit_new(&pl->b, &pl->nb, b_rel);
    eb->lines = bl;

    sv_pushs(&pl->files, a_rel);
    sv_pushs(&pl->files, b_rel);
    pl->a_edit = fmt_alloc("改写 %s", a_rel);
    pl->b_edit = fmt_alloc("改写 %s", b_rel);
    return 1;
}

/* adddel：A 新增一个文件，B 删除另一个文件（不同路径 → 必然干净）。 */
static int apply_adddel(rng *r, edit *base, size_t nb, plan *pl, char **err) {
    (void)err;
    if (nb < 2) return 0;
    const char *keep_rel = base[0].rel;
    const char *doomed_rel = base[1].rel;

    char *fresh_rel = fmt_alloc("%sadddel_fresh_%lld.txt", GEN_PREFIX,
                                fresh_number(doomed_rel));
    long long fn = rng_int(r, 10, 26);
    strv fresh;
    sv_init(&fresh);
    code_block(r, fn, &fresh);

    edit *ea = edit_new(&pl->a, &pl->na, fresh_rel);
    ea->lines = fresh;
    edit *eb = edit_new(&pl->b, &pl->nb, doomed_rel);
    eb->deleted = 1;

    sv_pushs(&pl->files, keep_rel);
    sv_pushs(&pl->files, doomed_rel);
    sv_pushs(&pl->files, fresh_rel);
    pl->a_edit = fmt_alloc("新增 %s", fresh_rel);
    pl->b_edit = fmt_alloc("删除 %s", doomed_rel);
    free(fresh_rel);
    return 1;
}

/* conflict：两个分支改**同一行**（对照组）。 */
static int apply_conflict(rng *r, edit *base, size_t nb, plan *pl, char **err) {
    (void)nb; (void)err;
    const char *rel = base[0].rel;
    long long n = (long long)base[0].lines.n;
    if (n < 3) return 0;
    long long target = 1 + rng_below(r, n - 2);      /* randrange(1, n-1) */

    strv al;
    sv_init(&al);
    sv_copy_range(&al, &base[0].lines, 0, base[0].lines.n);
    {
        buf t;
        buf_init(&t);
        buf_puts(&t, "    # A-EDIT ");
        put_phrase(r, &t);
        free(al.v[target]);
        al.v[target] = xstrdup(buf_cstr(&t));
        buf_free(&t);
    }

    strv bl;
    sv_init(&bl);
    sv_copy_range(&bl, &base[0].lines, 0, base[0].lines.n);
    {
        buf t;
        buf_init(&t);
        buf_puts(&t, "    # B-EDIT ");
        put_phrase(r, &t);
        free(bl.v[target]);
        bl.v[target] = xstrdup(buf_cstr(&t));
        buf_free(&t);
    }

    edit *ea = edit_new(&pl->a, &pl->na, rel);
    ea->lines = al;
    edit *eb = edit_new(&pl->b, &pl->nb, rel);
    eb->lines = bl;

    sv_pushs(&pl->files, rel);
    pl->a_edit = fmt_alloc("改第 %lld 行", target + 1);
    pl->b_edit = fmt_alloc("改第 %lld 行(同一行)", target + 1);
    return 1;
}

/* ---------------------------------------------------------------- 场景执行 */

static int materialize(const edit *e) {
    if (e->deleted) { delete_rel(e->rel); return 1; }
    return write_lines(e->rel, &e->lines);
}

static int commit_all(const char *msg, buf *out, char **err) {
    {
        const char *a[] = { "add", "-A", NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        int rc = gen_git(a, &o, &e);
        if (rc != 0) {
            *err = git_fail_msg(a, rc, &e, &o);
            buf_free(&o);
            buf_free(&e);
            return 0;
        }
        buf_free(&o);
        buf_free(&e);
    }
    {
        const char *a[] = { "commit", "-q", "-m", msg, NULL };
        buf o, e;
        buf_init(&o);
        buf_init(&e);
        int rc = gen_git(a, &o, &e);
        if (rc != 0) {
            *err = git_fail_msg(a, rc, &e, &o);
            buf_free(&o);
            buf_free(&e);
            return 0;
        }
        buf_free(&o);
        buf_free(&e);
    }
    if (out) {
        const char *a[] = { "rev-parse", "HEAD", NULL };
        buf o;
        buf_init(&o);
        int rc = gen_git(a, &o, NULL);
        if (rc != 0) {
            *err = fmt_alloc("git rev-parse HEAD 失败(rc=%d)", rc);
            buf_free(&o);
            return 0;
        }
        buf_reset(out);
        char *t = text_strip(&o);
        buf_puts(out, t);
        free(t);
        buf_free(&o);
    }
    return 1;
}

/* `git <args>` 失败即返回错误串（check=True 的那些调用）。 */
static int git_checked(const char *const *args, char **err) {
    buf o, e;
    buf_init(&o);
    buf_init(&e);
    int rc = gen_git(args, &o, &e);
    if (rc != 0) *err = git_fail_msg(args, rc, &e, &o);
    buf_free(&o);
    buf_free(&e);
    return rc == 0;
}

static void verify_scenario(scenario *sc, const char *base, int expect_clean) {
    const char *co_main[] = { "checkout", "-q", "-f", "main", NULL };
    const char *reset_hard[] = { "reset", "--hard", "-q", base, NULL };

    gen_git(co_main, NULL, NULL);
    gen_git(reset_hard, NULL, NULL);

    buf o1, e1, o2, e2;
    buf_init(&o1);
    buf_init(&e1);
    buf_init(&o2);
    buf_init(&e2);

    const char *m1[] = { "merge", "--no-edit", "-q", sc->a_name, NULL };
    int rc1 = gen_git(m1, &o1, &e1);
    const char *m2[] = { "merge", "--no-edit", "-q", sc->b_name, NULL };
    int rc2 = gen_git(m2, &o2, &e2);

    sc->first_merge_ok = (rc1 == 0);
    sc->merged_ok = (rc1 == 0 && rc2 == 0);

    if (expect_clean) {
        char *p = porcelain_text();
        sc->worktree_clean = (*p == 0) ? 1 : 0;
        free(p);
    } else {
        sc->worktree_clean = -1;                 /* null */
    }
    unmerged_list(&sc->unmerged);

    {
        buf all;
        buf_init(&all);
        buf_append(&all, o1.p ? o1.p : "", o1.n);
        buf_append(&all, e1.p ? e1.p : "", e1.n);
        buf_append(&all, o2.p ? o2.p : "", o2.n);
        buf_append(&all, e2.p ? e2.p : "", e2.n);
        sc->merge_output = text_strip(&all);
        buf_free(&all);
    }

    const char *abort_m[] = { "merge", "--abort", NULL };
    gen_git(abort_m, NULL, NULL);
    gen_git(co_main, NULL, NULL);
    gen_git(reset_hard, NULL, NULL);

    buf_free(&o1);
    buf_free(&e1);
    buf_free(&o2);
    buf_free(&e2);
}

static int verdict_of(const scenario *sc, int expect_clean) {
    if (expect_clean)
        return sc->merged_ok == 1 && sc->worktree_clean == 1 && sc->unmerged.n == 0;
    return sc->merged_ok == 0 && sc->unmerged.n > 0;
}

/* 造 base 文件（按 kind）。 */
static int seed_files(const char *kind, long long index, rng *r, edit **out,
                      size_t *n_out, char **err) {
    edit *arr = NULL;
    size_t n = 0;

    if (!strcmp(kind, "mergeable")) {
        char *rel = fmt_alloc("%smerge_%lld.txt", GEN_PREFIX, index);
        long long ln = rng_int(r, 56, 104);
        edit *e = edit_new(&arr, &n, rel);
        code_block(r, ln, &e->lines);
        free(rel);
    } else if (!strcmp(kind, "unrelated")) {
        char *ra = fmt_alloc("%sunrel_a_%lld.txt", GEN_PREFIX, index);
        long long la = rng_int(r, 18, 40);
        edit *ea = edit_new(&arr, &n, ra);
        code_block(r, la, &ea->lines);
        free(ra);
        char *rb = fmt_alloc("%sunrel_b_%lld.txt", GEN_PREFIX, index);
        long long lb = rng_int(r, 18, 40);
        edit *eb = edit_new(&arr, &n, rb);
        code_block(r, lb, &eb->lines);
        free(rb);
    } else if (!strcmp(kind, "adddel")) {
        char *rk = fmt_alloc("%sadddel_keep_%lld.txt", GEN_PREFIX, index);
        long long lk = rng_int(r, 14, 30);
        edit *ek = edit_new(&arr, &n, rk);
        code_block(r, lk, &ek->lines);
        free(rk);
        char *rd = fmt_alloc("%sadddel_doomed_%lld.txt", GEN_PREFIX, index);
        long long ld = rng_int(r, 14, 30);
        edit *ed = edit_new(&arr, &n, rd);
        code_block(r, ld, &ed->lines);
        free(rd);
    } else if (!strcmp(kind, "conflict")) {
        char *rel = fmt_alloc("%sclash_%lld.txt", GEN_PREFIX, index);
        long long ln = rng_int(r, 40, 80);
        edit *e = edit_new(&arr, &n, rel);
        code_block(r, ln, &e->lines);
        free(rel);
    } else {
        *err = fmt_alloc("未知 kind:'%s'", kind);
        return 0;
    }
    *out = arr;
    *n_out = n;
    return 1;
}

static int make_scenario(const char *kind, long long index, const char *seed_str,
                         const char *branch_prefix, scenario *sc, char **err) {
    memset(sc, 0, sizeof(*sc));
    sv_init(&sc->files);
    sv_init(&sc->unmerged);

    char *rngkey = fmt_alloc("%s|%s|%lld|", seed_str, kind, index);
    rng r;
    rng_seed(&r, rngkey);
    free(rngkey);

    sc->kind = xstrdup(kind);
    sc->index = index;
    sc->id = fmt_alloc("%s#%lld", kind, index);
    sc->a_name = fmt_alloc("%s/%s/%lld/a", branch_prefix, kind, index);
    sc->b_name = fmt_alloc("%s/%s/%lld/b", branch_prefix, kind, index);
    sc->expect = xstrdup(strcmp(kind, "conflict") ? GEN_EXPECT_CLEAN
                                                  : GEN_EXPECT_CONFLICT);

    edit *base = NULL;
    size_t nbase = 0;
    if (!seed_files(kind, index, &r, &base, &nbase, err)) return 0;

    int ok = 0;
    plan pl;
    memset(&pl, 0, sizeof(pl));
    sv_init(&pl.files);
    char *base_sha = NULL;

    /* 1) 落盘种子文件 + 提交（base） */
    for (size_t i = 0; i < nbase; i++) {
        if (!write_lines(base[i].rel, &base[i].lines)) {
            *err = fmt_alloc("写入失败:%s", base[i].rel);
            goto done;
        }
    }
    {
        char *msg = fmt_alloc("%s: seed %s #%lld (%zu file(s))", GEN_COMMIT_TAG,
                              kind, index, nbase);
        buf sha;
        buf_init(&sha);
        int rc = commit_all(msg, &sha, err);
        free(msg);
        if (!rc) { buf_free(&sha); goto done; }
        base_sha = xstrdup(buf_cstr(&sha));
        buf_free(&sha);
    }
    sc->base = xstrdup(base_sha);
    if (strlen(sc->base) > 10) sc->base[10] = 0;

    /* 2) 按 kind 算出两个分支各自的改动 */
    int applied;
    if (!strcmp(kind, "mergeable")) applied = apply_mergeable(&r, base, nbase, &pl, err);
    else if (!strcmp(kind, "unrelated")) applied = apply_unrelated(&r, base, nbase, &pl, err);
    else if (!strcmp(kind, "adddel")) applied = apply_adddel(&r, base, nbase, &pl, err);
    else applied = apply_conflict(&r, base, nbase, &pl, err);
    if (!applied) {
        if (!*err) *err = xstrdup("内部错误:场景构造失败");
        goto done;
    }

    /* 3) 分支 A：从 base 这个 sha 拉 */
    {
        const char *a[] = { "checkout", "-q", "-b", sc->a_name, base_sha, NULL };
        if (!git_checked(a, err)) goto done;
    }
    for (size_t i = 0; i < pl.na; i++)
        if (!materialize(&pl.a[i])) {
            *err = fmt_alloc("写入失败:%s", pl.a[i].rel);
            goto done;
        }
    {
        char *msg = fmt_alloc("%s: %s #%lld A: %s", GEN_COMMIT_TAG, kind, index,
                              pl.a_edit);
        int rc = commit_all(msg, NULL, err);
        free(msg);
        if (!rc) goto done;
    }

    /* 4) 分支 B：同样从 base 拉，内容独立重算 */
    {
        const char *co[] = { "checkout", "-q", "-f", base_sha, NULL };
        if (!git_checked(co, err)) goto done;
        const char *b[] = { "checkout", "-q", "-b", sc->b_name, base_sha, NULL };
        if (!git_checked(b, err)) goto done;
    }
    for (size_t i = 0; i < pl.nb; i++)
        if (!materialize(&pl.b[i])) {
            *err = fmt_alloc("写入失败:%s", pl.b[i].rel);
            goto done;
        }
    {
        char *msg = fmt_alloc("%s: %s #%lld B: %s", GEN_COMMIT_TAG, kind, index,
                              pl.b_edit);
        int rc = commit_all(msg, NULL, err);
        free(msg);
        if (!rc) goto done;
    }

    {
        const char *co[] = { "checkout", "-q", "-f", base_sha, NULL };
        if (!git_checked(co, err)) goto done;
    }

    /* 5) 真跑一遍 merge 复核 */
    verify_scenario(sc, base_sha, strcmp(kind, "conflict") != 0);
    sc->verified = verdict_of(sc, strcmp(kind, "conflict") != 0);

    /* 6) 结果字段 */
    sc->a_edit = xstrdup(pl.a_edit ? pl.a_edit : "");
    sc->b_edit = xstrdup(pl.b_edit ? pl.b_edit : "");
    sc->has_gap = pl.has_gap;
    sc->gap = pl.gap;
    for (size_t i = 0; i < pl.files.n; i++) sv_pushs(&sc->files, pl.files.v[i]);
    sv_sort_unique(&sc->files);
    ok = 1;

done:
    free(base_sha);
    for (size_t i = 0; i < nbase; i++) edit_free(&base[i]);
    free(base);
    plan_free(&pl);
    return ok;
}

/* ---------------------------------------------------------------- JSON 输出 */

static void json_float(buf *o, double d) {
    if (isnan(d)) { buf_puts(o, "NaN"); return; }
    if (isinf(d)) { buf_puts(o, d > 0 ? "Infinity" : "-Infinity"); return; }
    char t[64];
    for (int p = 1; p <= 17; p++) {
        snprintf(t, sizeof(t), "%.*g", p, d);
        if (strtod(t, NULL) == d) break;
    }
    if (!strchr(t, '.') && !strchr(t, 'e') && !strchr(t, 'E')) {
        size_t n = strlen(t);
        if (n + 3 < sizeof(t)) { t[n] = '.'; t[n + 1] = '0'; t[n + 2] = 0; }
    }
    buf_puts(o, t);
}

/* 把请求体里的 seed 原样写回响应（Python 侧是 json.dumps(seed)）。 */
static void emit_node(buf *o, const jnode *v) {
    switch (bjtype(v)) {
    case BJ_NULL: buf_puts(o, "null"); break;
    case BJ_BOOL: buf_puts(o, jbool(v) ? "true" : "false"); break;
    case BJ_INT: buf_printf(o, "%lld", jint(v)); break;
    case BJ_FLOAT: json_float(o, jfloat(v)); break;
    case BJ_STR: json_str(o, jstr(v)); break;
    case BJ_ARR:
        buf_putc(o, '[');
        for (size_t i = 0; i < jlen(v); i++) {
            if (i) buf_putc(o, ',');
            emit_node(o, jat(v, i));
        }
        buf_putc(o, ']');
        break;
    case BJ_OBJ:
        buf_putc(o, '{');
        for (size_t i = 0; i < jlen(v); i++) {
            if (i) buf_putc(o, ',');
            json_str(o, jkey(v, i));
            buf_putc(o, ':');
            emit_node(o, jget(v, jkey(v, i)));
        }
        buf_putc(o, '}');
        break;
    default: buf_puts(o, "null"); break;
    }
}

/* `"%s" % seed`（Python str()）——用于分支前缀与 _rnd_for 的键。 */
static char *seed_to_str(const jnode *v) {
    if (jis_str(v)) return xstrdup(jstr(v));
    buf o;
    buf_init(&o);
    w_repr(v, &o);
    char *r = xstrdup(buf_cstr(&o));
    buf_free(&o);
    return r;
}

static void emit_strv(buf *o, const strv *s) {
    buf_putc(o, '[');
    for (size_t i = 0; i < s->n; i++) {
        if (i) buf_putc(o, ',');
        json_str(o, s->v[i]);
    }
    buf_putc(o, ']');
}

static void emit_scenario(buf *o, const scenario *sc) {
    buf_puts(o, "{\"id\":");
    json_str(o, sc->id);
    buf_puts(o, ",\"kind\":");
    json_str(o, sc->kind);
    buf_printf(o, ",\"index\":%lld", sc->index);
    buf_puts(o, ",\"base\":");
    json_str(o, sc->base);
    buf_puts(o, ",\"branches\":[");
    json_str(o, sc->a_name);
    buf_putc(o, ',');
    json_str(o, sc->b_name);
    buf_putc(o, ']');
    buf_puts(o, ",\"files\":");
    emit_strv(o, &sc->files);
    buf_puts(o, ",\"aEdit\":");
    json_str(o, sc->a_edit);
    buf_puts(o, ",\"bEdit\":");
    json_str(o, sc->b_edit);
    buf_puts(o, ",\"expect\":");
    json_str(o, sc->expect);
    buf_printf(o, ",\"verified\":%s", sc->verified ? "true" : "false");
    buf_puts(o, ",\"verifyDetail\":{\"merged_ok\":");
    buf_puts(o, sc->merged_ok ? "true" : "false");
    buf_puts(o, ",\"first_merge_ok\":");
    buf_puts(o, sc->first_merge_ok ? "true" : "false");
    buf_puts(o, ",\"worktree_clean\":");
    if (sc->worktree_clean < 0) buf_puts(o, "null");
    else buf_puts(o, sc->worktree_clean ? "true" : "false");
    buf_puts(o, ",\"unmerged\":");
    emit_strv(o, &sc->unmerged);
    buf_putc(o, '}');
    if (sc->has_gap) buf_printf(o, ",\"gapLines\":%lld", sc->gap);
    if (!sc->verified) {
        buf_puts(o, ",\"mergeOutput\":");
        json_str(o, sc->merge_output ? sc->merge_output : "");
    }
    buf_putc(o, '}');
}

/* ---------------------------------------------------------------- 响应 */

static int send_bad(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
    return 1;
}

static int send_bad_free(SOCKET s, char *msg) {
    http_send_err(s, 400, msg, NULL);
    free(msg);
    return 1;
}

/* 非负随机 seed（Python: random.randrange(1 << 30)）。 */
static long long random_seed30(void) {
    LARGE_INTEGER q;
    QueryPerformanceCounter(&q);
    uint64_t x = (uint64_t)q.QuadPart ^ ((uint64_t)GetTickCount64() << 20) ^
                 ((uint64_t)GetCurrentProcessId() << 40);
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    return (long long)(x % 1073741824ULL);
}

static int do_generate(SOCKET s, const jnode *b) {
    /* ---- kind ---- */
    const jnode *kn = jget(b, "kind");
    const char *kind = "all";
    if (kn) {
        if (!jis_str(kn)) return send_bad(s, GEN_KIND_HINT);
        kind = jstr(kn);
    }
    int is_all = !strcmp(kind, "all");
    if (!is_all) {
        int known = 0;
        for (size_t i = 0; i < GEN_NKINDS; i++)
            if (!strcmp(kind, GEN_KINDS[i])) known = 1;
        if (!known) return send_bad(s, GEN_KIND_HINT);
    }

    /* ---- count（_bi 语义）---- */
    long long count = 3;
    {
        const jnode *cv = jget(b, "count");
        if (cv && bjtype(cv) == BJ_BOOL) {
            count = jbool(cv) ? 1 : 0;          /* Python: int(True) == 1 */
        } else if (cv && jis_str(cv)) {
            if (!py_int_parse(jstr(cv), &count)) {
                buf m;
                buf_init(&m);
                buf_puts(&m, "参数 count 必须是整数,收到 ");
                w_repr(cv, &m);
                char *msg = xstrdup(buf_cstr(&m));
                buf_free(&m);
                return send_bad_free(s, msg);
            }
        } else if (!wbody_int(s, b, "count", 3, &count)) {
            return 1;
        }
    }
    if (count < 1)
        return send_bad_free(s, fmt_alloc("count 至少为 1,收到 %lld", count));
    if (count > GEN_MAX_COUNT)
        return send_bad_free(s, fmt_alloc("count 过大(最多 %d 个场景),收到 %lld",
                                          GEN_MAX_COUNT, count));

    /* ---- seed ---- */
    const jnode *sn = jget(b, "seed");
    int seed_random = (!sn || bjtype(sn) == BJ_NULL);
    char *seed_str = NULL;
    long long seed_int = 0;
    if (seed_random) {
        seed_int = random_seed30();
        seed_str = fmt_alloc("%lld", seed_int);
    } else {
        seed_str = seed_to_str(sn);
    }

    char *branch_prefix = NULL;
    strv kinds;
    sv_init(&kinds);
    scenario *scs = NULL;
    size_t nsc = 0, capsc = 0;
    strv all_files, all_branches;

    if (is_all) {
        for (size_t i = 0; i < GEN_NKINDS; i++) sv_pushs(&kinds, GEN_KINDS[i]);
    } else {
        sv_pushs(&kinds, kind);
    }
    branch_prefix = fmt_alloc("%s/s%s", GEN_BRANCH_PREFIX, seed_str);

    /* ---- 仓护栏（顺序与 Python 一致）---- */
    if (!repo_is_git()) {
        char *root = w2u8(git_root());
        char *m = fmt_alloc("不是一个 git 仓:%s", root ? root : "");
        free(root);
        free(seed_str);
        free(branch_prefix);
        sv_free(&kinds);
        return send_bad_free(s, m);
    }
    if (!git_has_head()) {
        free(seed_str);
        free(branch_prefix);
        sv_free(&kinds);
        return send_bad(s, "仓里还没有任何提交(HEAD 不存在),先建一个初始提交");
    }
    {
        char *dirty = porcelain_text();
        if (*dirty) {
            size_t nl = count_lines(dirty);
            buf m;
            buf_init(&m);
            buf_puts(&m, "工作区不干净,拒绝生成(有 ");
            buf_printf(&m, "%zu", nl);
            buf_puts(&m, " 条未提交改动):\n      ");
            size_t shown = 0;
            char *line = dirty;
            while (line && *line && shown < 8) {
                char *nlt = strchr(line, '\n');
                if (nlt) *nlt = 0;
                if (shown) buf_puts(&m, "\n      ");
                buf_puts(&m, line);
                shown++;
                line = nlt ? nlt + 1 : NULL;
            }
            if (nl > 8) buf_printf(&m, "\n      ...(共 %zu 行)", nl);
            buf_puts(&m, "\n    先 commit / stash / checkout 干净,或者在演练仓上跑。");
            char *msg = xstrdup(buf_cstr(&m));
            buf_free(&m);
            free(dirty);
            free(seed_str);
            free(branch_prefix);
            sv_free(&kinds);
            return send_bad_free(s, msg);
        }
        free(dirty);
    }

    /* ---- 分支撞名（先查，再动手）---- */
    {
        strv want;
        sv_init(&want);
        for (size_t k = 0; k < kinds.n; k++) {
            for (long long i = 0; i < count; i++) {
                char *a = fmt_alloc("%s/%s/%lld/a", branch_prefix, kinds.v[k], i);
                char *bb = fmt_alloc("%s/%s/%lld/b", branch_prefix, kinds.v[k], i);
                sv_push(&want, a);
                sv_push(&want, bb);
            }
        }
        strv clash;
        sv_init(&clash);
        for (size_t i = 0; i < want.n; i++)
            if (branch_exists(want.v[i])) sv_pushs(&clash, want.v[i]);
        sv_free(&want);
        if (clash.n) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "以下分支已存在,拒绝生成(先跑 --clean,或换 seed):\n      ");
            for (size_t i = 0; i < clash.n && i < 10; i++) {
                if (i) buf_puts(&m, "\n      ");
                buf_puts(&m, clash.v[i]);
            }
            if (clash.n > 10) buf_printf(&m, "\n      ...(共 %zu 个)", clash.n);
            char *msg = xstrdup(buf_cstr(&m));
            buf_free(&m);
            sv_free(&clash);
            free(seed_str);
            free(branch_prefix);
            sv_free(&kinds);
            return send_bad_free(s, msg);
        }
        sv_free(&clash);
    }

    if (!branch_exists("main")) {
        free(seed_str);
        free(branch_prefix);
        sv_free(&kinds);
        return send_bad(s, "仓里没有 main 分支,无法作为场景基准点");
    }

    /* ---- 逐场景生成 ---- */
    sv_init(&all_files);
    sv_init(&all_branches);
    int all_ok = 1;
    for (size_t k = 0; k < kinds.n; k++) {
        for (long long i = 0; i < count; i++) {
            scenario sc;
            char *err = NULL;
            if (!make_scenario(kinds.v[k], i, seed_str, branch_prefix, &sc, &err)) {
                scenario_free(&sc);
                for (size_t q = 0; q < nsc; q++) scenario_free(&scs[q]);
                free(scs);
                sv_free(&all_files);
                sv_free(&all_branches);
                sv_free(&kinds);
                free(seed_str);
                free(branch_prefix);
                return send_bad_free(s, err ? err
                                            : xstrdup("生成失败:未知错误"));
            }
            if (nsc == capsc) {
                capsc = capsc ? capsc * 2 : 8;
                scs = xrealloc(scs, capsc * sizeof(*scs));
            }
            scs[nsc++] = sc;
            for (size_t q = 0; q < sc.files.n; q++) sv_pushs(&all_files, sc.files.v[q]);
            sv_pushs(&all_branches, sc.a_name);
            sv_pushs(&all_branches, sc.b_name);
            if (!sc.verified) all_ok = 0;
        }
    }

    /* ---- 响应 ---- */
    {
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":");
        buf_puts(&o, all_ok ? "true" : "false");
        buf_puts(&o, ",\"kind\":");
        json_str(&o, kind);
        buf_puts(&o, ",\"seed\":");
        if (seed_random) buf_printf(&o, "%lld", seed_int);
        else emit_node(&o, sn);
        buf_printf(&o, ",\"count\":%lld", count);
        buf_puts(&o, ",\"kinds\":");
        emit_strv(&o, &kinds);
        buf_puts(&o, ",\"scenarios\":[");
        for (size_t i = 0; i < nsc; i++) {
            if (i) buf_putc(&o, ',');
            emit_scenario(&o, &scs[i]);
        }
        buf_puts(&o, "],\"branches\":[");
        for (size_t i = 0; i < nsc; i++) {
            if (i) buf_putc(&o, ',');
            buf_puts(&o, "{\"file\":");
            /* ", ".join(sc["files"]) */
            buf fm;
            buf_init(&fm);
            for (size_t q = 0; q < scs[i].files.n; q++) {
                if (q) buf_puts(&fm, ", ");
                buf_puts(&fm, scs[i].files.v[q]);
            }
            json_str(&o, buf_cstr(&fm));
            buf_free(&fm);
            buf_puts(&o, ",\"branches\":[");
            json_str(&o, scs[i].a_name);
            buf_putc(&o, ',');
            json_str(&o, scs[i].b_name);
            buf_putc(&o, ']');
            buf_puts(&o, ",\"expect\":");
            json_str(&o, scs[i].expect);
            buf_puts(&o, ",\"id\":");
            json_str(&o, scs[i].id);
            buf_printf(&o, ",\"verified\":%s}", scs[i].verified ? "true" : "false");
        }
        buf_puts(&o, "],\"files\":");
        emit_strv(&o, &all_files);
        buf_puts(&o, ",\"prefix\":");
        json_str(&o, GEN_PREFIX);
        buf_puts(&o, ",\"branchPrefix\":");
        json_str(&o, branch_prefix);
        buf_printf(&o, ",\"allVerified\":%s", all_ok ? "true" : "false");

        buf sum;
        buf_init(&sum);
        buf_printf(&sum, "已生成 %zu 个场景(", nsc);
        for (size_t i = 0; i < kinds.n; i++) {
            if (i) buf_putc(&sum, '/');
            buf_puts(&sum, kinds.v[i]);
        }
        buf_printf(&sum, "),分支前缀 %s/,文件前缀 %s;真跑 merge 验证:%s。"
                         "合并完切回 main 再 reset。",
                   branch_prefix, GEN_PREFIX,
                   all_ok ? "全部符合预期" : "**有场景未达预期,详见 verified 字段**");
        buf_puts(&o, ",\"summary\":");
        json_str(&o, buf_cstr(&sum));
        buf_puts(&o, ",\"note\":");
        json_str(&o, buf_cstr(&sum));
        buf_putc(&o, '}');
        buf_free(&sum);

        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
    }

    for (size_t i = 0; i < nsc; i++) scenario_free(&scs[i]);
    free(scs);
    sv_free(&all_files);
    sv_free(&all_branches);
    sv_free(&kinds);
    free(seed_str);
    free(branch_prefix);
    return 1;
}

/* ---------------------------------------------------------------- 入口 */

int gen_post(const char *path, const char *query, const char *body,
             size_t body_len, SOCKET s) {
    (void)query;
    if (strcmp(path, "/api/gen") != 0) return 0;
    jnode *b = wbody_parse(s, body, body_len);
    if (!b) return 1;
    int rc = do_generate(s, b);
    jfree(b);
    return rc;
}
