#include "indexfile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "git.h"
#include "util.h"

#define IW 4096

static unsigned be32(const unsigned char *p) {
    return ((unsigned)p[0] << 24) | ((unsigned)p[1] << 16) |
           ((unsigned)p[2] << 8) | p[3];
}
static unsigned be16(const unsigned char *p) {
    return ((unsigned)p[0] << 8) | p[1];
}

static int utf8_ok(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) { i++; continue; }
        int need;
        unsigned cp;
        if (c >= 0xc2 && c <= 0xdf) { need = 1; cp = c & 0x1f; }
        else if (c >= 0xe0 && c <= 0xef) { need = 2; cp = c & 0x0f; }
        else if (c >= 0xf0 && c <= 0xf4) { need = 3; cp = c & 0x07; }
        else return 0;
        if (i + (size_t)need >= n) return 0;
        for (int k = 1; k <= need; k++) {
            unsigned char cc = (unsigned char)s[i + (size_t)k];
            if ((cc & 0xc0) != 0x80) return 0;
            cp = (cp << 6) | (cc & 0x3f);
        }
        if ((need == 2 && cp < 0x800) || (need == 3 && cp < 0x10000) ||
            (cp >= 0xd800 && cp <= 0xdfff) || cp > 0x10ffff) return 0;
        i += (size_t)need + 1;
    }
    return 1;
}

/* root/.git 是目录、且不是 reftable / GIT_DIR 改写 —— 只有这时才直读。 */
static int gitdir_w(wchar_t *out, size_t outsz) {
    if (getenv("GIT_DIR") || getenv("GIT_COMMON_DIR")) return 0;
    const wchar_t *root = git_root();
    if (!root) return 0;
    _snwprintf(out, outsz, L"%ls\\.git", root);
    DWORD a = GetFileAttributesW(out);
    if (a == INVALID_FILE_ATTRIBUTES || !(a & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    wchar_t rt[IW];
    _snwprintf(rt, IW, L"%ls\\reftable", out);
    DWORD a2 = GetFileAttributesW(rt);
    if (a2 != INVALID_FILE_ATTRIBUTES && (a2 & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    return 1;
}

static int read_bytes(const wchar_t *path, buf *out) {
    FILE *f = _wfopen(path, L"rb");
    if (!f) return 0;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return 0; }
    long sz = ftell(f);
    if (sz < 0 || sz > (long)(256 * 1024 * 1024)) { fclose(f); return 0; }
    rewind(f);
    char *tmp = xmalloc((size_t)sz + 1);
    size_t got = fread(tmp, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) { free(tmp); return 0; }
    buf_append(out, tmp, (size_t)sz);
    free(tmp);
    return 1;
}

static void push_name(idx_names *out, const char *s, size_t n) {
    out->paths = xrealloc(out->paths, (out->n + 1) * sizeof(*out->paths));
    out->paths[out->n] = xmalloc(n + 1);
    memcpy(out->paths[out->n], s, n);
    out->paths[out->n][n] = 0;
    out->n++;
}

int idx_read(idx_names *out) {
    memset(out, 0, sizeof(*out));
    if (getenv("GIT_INDEX_FILE")) return 0;
    wchar_t gd[IW];
    if (!gitdir_w(gd, IW)) return 0;
    wchar_t ip[IW];
    _snwprintf(ip, IW, L"%ls\\index", gd);
    DWORD a = GetFileAttributesW(ip);
    if (a == INVALID_FILE_ATTRIBUTES) return 1;          /* 空索引 */
    buf raw;
    buf_init(&raw);
    if (!read_bytes(ip, &raw)) { buf_free(&raw); return 0; }
    const unsigned char *r = (const unsigned char *)(raw.p ? raw.p : "");
    size_t n = raw.n;
    int ok = 1;
    if (n < 12 || memcmp(r, "DIRC", 4)) {
        ok = 0;
    } else {
        unsigned ver = be32(r + 4);
        unsigned count = be32(r + 8);
        if (ver != 2 && ver != 3) {
            ok = 0;
        } else {
            size_t off = 12;
            for (unsigned i = 0; i < count && ok; i++) {
                if (off + 62 > n) { ok = 0; break; }
                unsigned flags = be16(r + off + 60);
                unsigned stage = (flags >> 12) & 3u;
                if (stage) out->has_unmerged = 1;
                size_t name_len = flags & 0x0FFF;
                size_t p = off + 62;
                if (ver >= 3 && (flags & 0x4000)) p += 2;
                if (p > n) { ok = 0; break; }
                size_t nstart = p, nlen = 0, entry_end = 0;
                if (name_len < 0xFFF) {
                    size_t end = p + name_len;
                    if (end >= n || r[end] != 0) { ok = 0; break; }
                    nlen = name_len;
                    entry_end = end + 1;
                } else {
                    size_t end = p;
                    while (end < n && r[end] != 0) end++;
                    if (end >= n) { ok = 0; break; }
                    nlen = end - p;
                    entry_end = end + 1;
                }
                if (!nlen || !utf8_ok((const char *)r + nstart, nlen)) { ok = 0; break; }
                push_name(out, (const char *)r + nstart, nlen);
                off = off + ((entry_end - off + 7) & ~(size_t)7);
            }
            while (ok && off + 8 <= n) {
                const unsigned char *sig = r + off;
                unsigned size = be32(r + off + 4);
                if (!memcmp(sig, "link", 4) || !memcmp(sig, "sdir", 4)) { ok = 0; break; }
                off += 8 + size;
            }
        }
    }
    buf_free(&raw);
    if (!ok) { idx_free(out); return 0; }
    return 1;
}

void idx_free(idx_names *out) {
    if (!out) return;
    for (size_t i = 0; i < out->n; i++) free(out->paths[i]);
    free(out->paths);
    out->paths = NULL;
    out->n = 0;
    out->has_unmerged = 0;
}

int idx_has(const idx_names *ix, const char *path) {
    if (!ix || !path) return 0;
    for (size_t i = 0; i < ix->n; i++)
        if (!strcmp(ix->paths[i], path)) return 1;
    return 0;
}

/* 40 位十六进制才认（短 sha / 符号引用一律回退）。 */
static int looks_like_sha(const char *s, size_t n) {
    if (n != 40) return 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
              (c >= 'A' && c <= 'F')))
            return 0;
    }
    return 1;
}

char *head_sha_direct(void) {
    wchar_t gd[IW];
    if (!gitdir_w(gd, IW)) return NULL;

    wchar_t p[IW];
    _snwprintf(p, IW, L"%ls\\HEAD", gd);
    buf b;
    buf_init(&b);
    if (!read_bytes(p, &b)) { buf_free(&b); return NULL; }
    buf_cstr(&b);

    /* 去掉尾部空白 */
    size_t n = b.n;
    while (n && (b.p[n - 1] == '\n' || b.p[n - 1] == '\r' ||
                 b.p[n - 1] == ' ' || b.p[n - 1] == '\t'))
        n--;
    b.p[n] = 0;

    if (n >= 5 && !strncmp(b.p, "ref: ", 5)) {
        char *ref = b.p + 5;
        while (*ref == ' ' || *ref == '\t') ref++;
        /* 只接受 refs/... 这种正常路径，别让 ../ 之类跑出 gitdir */
        if (strncmp(ref, "refs/", 5) != 0 || strstr(ref, "..") ||
            strchr(ref, '\\')) {
            buf_free(&b);
            return NULL;
        }
        wchar_t *rw = u82w(ref);
        if (!rw) { buf_free(&b); return NULL; }
        _snwprintf(p, IW, L"%ls\\%ls", gd, rw);
        free(rw);
        buf r;
        buf_init(&r);
        if (!read_bytes(p, &r)) { buf_free(&r); buf_free(&b); return NULL; }
        buf_cstr(&r);
        size_t rn = r.n;
        while (rn && (r.p[rn - 1] == '\n' || r.p[rn - 1] == '\r' ||
                      r.p[rn - 1] == ' ' || r.p[rn - 1] == '\t'))
            rn--;
        char *out = looks_like_sha(r.p, rn) ? xstrndup(r.p, rn) : NULL;
        buf_free(&r);
        buf_free(&b);
        return out;
    }

    char *out = looks_like_sha(b.p, n) ? xstrndup(b.p, n) : NULL;
    buf_free(&b);
    return out;
}
