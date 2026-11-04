#include "guardcfg.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "util.h"

#define GC_MAX_NAMES 2000
#define GC_MAX_DEPTH 10

typedef struct { char *fam; char *name; } drv;

static drv   *g_drv = NULL;
static size_t g_drv_n = 0;

static char *trim_dup(const char *s, size_t n) {
    while (n && isspace((unsigned char)*s)) { s++; n--; }
    while (n && isspace((unsigned char)s[n - 1])) n--;
    char *o = xmalloc(n + 1);
    memcpy(o, s, n);
    o[n] = 0;
    return o;
}

static void lower_in_place(char *s) {
    for (; *s; s++) *s = (char)tolower((unsigned char)*s);
}

static int drv_seen(const char *fam, const char *name) {
    for (size_t i = 0; i < g_drv_n; i++)
        if (!strcmp(g_drv[i].fam, fam) && !strcmp(g_drv[i].name, name)) return 1;
    return 0;
}

static void drv_add(const char *fam, const char *name) {
    if (!*name || g_drv_n >= GC_MAX_NAMES) return;
    if (drv_seen(fam, name)) return;
    g_drv = xrealloc(g_drv, (g_drv_n + 1) * sizeof(*g_drv));
    g_drv[g_drv_n].fam = xstrdup(fam);
    g_drv[g_drv_n].name = xstrdup(name);
    g_drv_n++;
}

static int is_family(const char *f) {
    return !strcmp(f, "filter") || !strcmp(f, "diff") || !strcmp(f, "merge") ||
           !strcmp(f, "alias");
}

/* 把 include 的 path 解析成绝对宽路径（相对当前文件、~ 展开）。 */
static wchar_t *resolve_include(const wchar_t *cur_file, const char *val) {
    char *v = xstrdup(val);
    size_t n = strlen(v);
    while (n && (v[n - 1] == '\r' || v[n - 1] == '\n' || v[n - 1] == ' ')) v[--n] = 0;
    char *start = v;
    while (*start == ' ' || *start == '\t') start++;
    if (*start == '"') {
        start++;
        char *q = strrchr(start, '"');
        if (q) *q = 0;
    }
    wchar_t *out = NULL;
    if (!strncmp(start, "~/", 2) || !strcmp(start, "~")) {
        const char *home = getenv("HOME");
        if (!home || !*home) home = getenv("USERPROFILE");
        if (home && *home) {
            buf b;
            buf_init(&b);
            char *tail = start + 1;             /* 跳过 ~ */
            buf_printf(&b, "%s%s", home, tail);
            out = u82w(buf_cstr(&b));
            buf_free(&b);
        }
    } else if (start[0] == '\\' || (isalpha((unsigned char)start[0]) && start[1] == ':')) {
        out = u82w(start);
    } else {
        wchar_t *dir = _wcsdup(cur_file);
        wchar_t *slash = dir ? wcsrchr(dir, L'\\') : NULL;
        if (slash) *slash = 0;
        wchar_t *rel = u82w(start);
        if (dir && rel) {
            buf b;
            buf_init(&b);
            const wchar_t *sep = L"\\";
            buf_append(&b, dir, wcslen(dir) * sizeof(wchar_t));
            buf_append(&b, sep, sizeof(wchar_t));
            buf_append(&b, rel, wcslen(rel) * sizeof(wchar_t));
            wchar_t z = 0;
            buf_append(&b, &z, sizeof(wchar_t));
            out = _wcsdup((wchar_t *)b.p);
            buf_free(&b);
        }
        free(rel);
        free(dir);
    }
    free(v);
    return out;
}


/* 真实 gitdir：普通仓是 <root>\.git；worktree/submodule 的 .git 是文件，
 * 内容 "gitdir: <path>"。取不到返回 0。 */
static int cfg_gitdir(const wchar_t *root, wchar_t *out, size_t outsz) {
    wchar_t gd[32768];
    _snwprintf(gd, 32768, L"%ls\\.git", root);
    DWORD a = GetFileAttributesW(gd);
    if (a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY)) {
        _snwprintf(out, outsz, L"%ls", gd);
        return 1;
    }
    if (a == INVALID_FILE_ATTRIBUTES) return 0;
    FILE *f = _wfopen(gd, L"rb");
    if (!f) return 0;
    char b[4096];
    size_t n = fread(b, 1, sizeof(b) - 1, f);
    fclose(f);
    b[n] = 0;
    char *q = strstr(b, "gitdir:");
    if (!q) return 0;
    q += 7;
    while (*q == ' ' || *q == '\t') q++;
    size_t len = strlen(q);
    while (len && (q[len - 1] == '\r' || q[len - 1] == '\n' || q[len - 1] == ' '))
        q[--len] = 0;
    if (!len) return 0;
    wchar_t *rel = u82w(q);
    if (!rel) return 0;
    wchar_t full[32768];
    if (rel[0] == L'\\' || (iswalpha(rel[0]) && rel[1] == L':'))
        _snwprintf(full, 32768, L"%ls", rel);
    else
        _snwprintf(full, 32768, L"%ls\\%ls", root, rel);
    free(rel);
    wchar_t canon[32768];
    if (!GetFullPathNameW(full, 32768, canon, NULL)) return 0;
    _snwprintf(out, outsz, L"%ls", canon);
    return 1;
}

/* 配置指纹（mtime + size）。0 表示读不到任何配置。 */
unsigned long long guard_config_stamp(const wchar_t *root) {
    wchar_t gd[32768];
    if (!root || !cfg_gitdir(root, gd, 32768)) return 0;
    unsigned long long acc = 0;
    const wchar_t *names[] = { L"config", L"config.worktree" };
    for (int i = 0; i < 2; i++) {
        wchar_t p[32768];
        _snwprintf(p, 32768, L"%ls\\%ls", gd, names[i]);
        WIN32_FILE_ATTRIBUTE_DATA fa;
        if (!GetFileAttributesExW(p, GetFileExInfoStandard, &fa)) continue;
        unsigned long long t =
            ((unsigned long long)fa.ftLastWriteTime.dwHighDateTime << 32) |
            fa.ftLastWriteTime.dwLowDateTime;
        acc += t ^ ((unsigned long long)fa.nFileSizeLow << 16) ^ fa.nFileSizeHigh;
    }
    return acc ? acc : 1;
}

static void parse_file(const wchar_t *path, int depth);

static void parse_bytes(const wchar_t *path, char *data, size_t n, int depth) {
    char *cur_fam = NULL;
    char *cur_name = NULL;
    char *p = data;
    char *end = data + n;
    while (p < end) {
        char *nl = memchr(p, '\n', (size_t)(end - p));
        size_t ll = nl ? (size_t)(nl - p) : (size_t)(end - p);
        char *line = trim_dup(p, ll);
        p = nl ? nl + 1 : end;
        if (!*line || line[0] == '#' || line[0] == ';') { free(line); continue; }

        if (line[0] == '[') {
            char *rb = strchr(line, ']');
            if (!rb) { free(line); continue; }
            *rb = 0;
            char *inner = line + 1;
            char *fam = trim_dup(inner, strlen(inner));
            /* family 取到第一个空白/点之前 */
            char *cut = fam;
            while (*cut && *cut != ' ' && *cut != '\t' && *cut != '.') cut++;
            char saved = *cut;
            *cut = 0;
            lower_in_place(fam);
            free(cur_fam);
            cur_fam = xstrdup(fam);
            free(cur_name);
            cur_name = NULL;
            /* 引号里的子段名：按 git 的转义规则（\\ 与 \"）解析到未转义的收尾引号 */
            char *q1 = strchr(inner, '"');
            if (q1) {
                q1++;
                buf nm;
                buf_init(&nm);
                for (char *q = q1; *q && *q != '"'; q++) {
                    if (*q == '\\' && q[1]) {
                        q++;
                        buf_putc(&nm, *q);
                    } else {
                        buf_putc(&nm, *q);
                    }
                }
                cur_name = xstrdup(buf_cstr(&nm));
                buf_free(&nm);
            } else if (saved == '.') {
                char *dot = strchr(inner, '.');
                if (dot) cur_name = xstrdup(dot + 1);
            }
            free(fam);
            free(line);
            continue;
        }

        char *eq = strchr(line, '=');
        if (!eq) { free(line); continue; }
        *eq = 0;
        char *key = trim_dup(line, strlen(line));
        /* 行尾反斜杠续行：只用于 include.path 这类值 */
        buf vb;
        buf_init(&vb);
        buf_puts(&vb, eq + 1);
        while (vb.n && vb.p[vb.n - 1] == '\\' && p < end) {
            vb.n--;
            char *nl2 = memchr(p, '\n', (size_t)(end - p));
            size_t l2 = nl2 ? (size_t)(nl2 - p) : (size_t)(end - p);
            buf_append(&vb, p, l2);
            p = nl2 ? nl2 + 1 : end;
        }
        char *val = trim_dup(buf_cstr(&vb), vb.n);
        buf_free(&vb);
        lower_in_place(key);

        if (cur_fam && (!strcmp(cur_fam, "include") || !strncmp(cur_fam, "includeif", 9))) {
            if (!strcmp(key, "path")) {
                wchar_t *inc = resolve_include(path, val);
                if (inc && depth < GC_MAX_DEPTH) parse_file(inc, depth + 1);
                free(inc);
            }
        } else if (cur_fam && !strcmp(cur_fam, "alias")) {
            drv_add("alias", key);
        } else if (cur_fam && is_family(cur_fam) && cur_name && *cur_name) {
            drv_add(cur_fam, cur_name);
        }
        free(key);
        free(val);
        free(line);
    }
    free(cur_fam);
    free(cur_name);
}

static void parse_file(const wchar_t *path, int depth) {
    if (depth > GC_MAX_DEPTH) return;
    FILE *f = _wfopen(path, L"rb");
    if (!f) return;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    if (sz < 0 || sz > 16 * 1024 * 1024) { fclose(f); return; }
    rewind(f);
    char *data = xmalloc((size_t)sz + 1);
    size_t got = fread(data, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) { free(data); return; }
    data[sz] = 0;
    parse_bytes(path, data, (size_t)sz, depth);
    free(data);
}

static void add_pair(char ***arr, size_t *n, const char *fmt, const char *name) {
    buf b;
    buf_init(&b);
    buf_printf(&b, fmt, name);
    *arr = xrealloc(*arr, (*n + 1) * sizeof(**arr));
    (*arr)[*n] = xstrdup(buf_cstr(&b));
    (*n)++;
    buf_free(&b);
}

char **guard_driver_pairs(const wchar_t *root, size_t *n) {
    *n = 0;
    if (!root) return NULL;

    /* 仓级配置（worktree 要顺着 .git 文件找到公共 gitdir） */
    wchar_t cfg[32768];
    wchar_t gd[32768];
    if (cfg_gitdir(root, gd, 32768)) {
        _snwprintf(cfg, 32768, L"%ls\\config", gd);
        parse_file(cfg, 0);
        _snwprintf(cfg, 32768, L"%ls\\config.worktree", gd);
        parse_file(cfg, 0);
    }

    /* 用户级（净网关闭时其实读不到，但多覆盖几条无害；净网开启时必须覆盖） */
    const char *home = getenv("HOME");
    if (!home || !*home) home = getenv("USERPROFILE");
    if (home && *home) {
        wchar_t *h = u82w(home);
        if (h) {
            _snwprintf(cfg, 32768, L"%ls\\.gitconfig", h);
            parse_file(cfg, 0);
            const char *xdg = getenv("XDG_CONFIG_HOME");
            if (xdg && *xdg) {
                wchar_t *x = u82w(xdg);
                if (x) {
                    _snwprintf(cfg, 32768, L"%ls\\git\\config", x);
                    parse_file(cfg, 0);
                    free(x);
                }
            } else {
                _snwprintf(cfg, 32768, L"%ls\\.config\\git\\config", h);
                parse_file(cfg, 0);
            }
            free(h);
        }
    }

    char **out = NULL;
    size_t on = 0;
    for (size_t i = 0; i < g_drv_n; i++) {
        const char *fam = g_drv[i].fam, *nm = g_drv[i].name;
        if (!strcmp(fam, "filter")) {
            add_pair(&out, &on, "filter.%s.clean=", nm);
            add_pair(&out, &on, "filter.%s.smudge=", nm);
            add_pair(&out, &on, "filter.%s.process=", nm);
            add_pair(&out, &on, "filter.%s.required=false", nm);
        } else if (!strcmp(fam, "diff")) {
            add_pair(&out, &on, "diff.%s.textconv=", nm);
            add_pair(&out, &on, "diff.%s.command=", nm);
            add_pair(&out, &on, "diff.%s.cachetextconv=false", nm);
        } else if (!strcmp(fam, "merge")) {
            add_pair(&out, &on, "merge.%s.driver=", nm);
        } else if (!strcmp(fam, "alias")) {
            add_pair(&out, &on, "alias.%s=", nm);
        }
    }
    for (size_t i = 0; i < g_drv_n; i++) {
        free(g_drv[i].fam);
        free(g_drv[i].name);
    }
    free(g_drv);
    g_drv = NULL;
    g_drv_n = 0;
    *n = on;
    return out;
}

void guard_free_pairs(char **pairs, size_t n) {
    for (size_t i = 0; i < n; i++) free(pairs[i]);
    free(pairs);
}
