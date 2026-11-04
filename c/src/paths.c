#include "paths.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "git.h"
#include "util.h"

static void set_err(char **err, const char *fmt, const char *arg) {
    if (!err) return;
    buf b;
    buf_init(&b);
    buf_printf(&b, fmt, arg ? arg : "");
    *err = xstrdup(buf_cstr(&b));
    buf_free(&b);
}

static int has_prefix_ci(const wchar_t *s, const wchar_t *prefix) {
    size_t n = wcslen(prefix);
    return _wcsnicmp(s, prefix, n) == 0;
}

/* 去掉 \\?\ / \\?\UNC\ 前缀，便于统一比较 */
static const wchar_t *strip_ns_prefix(const wchar_t *p) {
    if (p && p[0] == L'\\' && p[1] == L'\\' && p[2] == L'?' && p[3] == L'\\') return p + 4;
    return p;
}

/* 取一个已存在路径的真实路径（句柄级，能穿过符号链接/联接）。失败返回 NULL。 */
static wchar_t *real_path_of(const wchar_t *path) {
    HANDLE h = CreateFileW(path, 0,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING,
                           FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (h == INVALID_HANDLE_VALUE) h = CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                                                   OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED);
    wchar_t *out = NULL;
    if (need) {
        out = xmalloc((size_t)need * sizeof(wchar_t) + sizeof(wchar_t));
        DWORD got = GetFinalPathNameByHandleW(h, out, need, FILE_NAME_NORMALIZED);
        if (!got) { free(out); out = NULL; }
    }
    CloseHandle(h);
    return out;
}

/* 把 full 拆成「最长的已存在祖先 + 剩余后缀」，返回真实路径（后缀拼回去）。 */
static wchar_t *resolve_with_existing_prefix(const wchar_t *full, const wchar_t *root_real) {
    wchar_t *cur = _wcsdup(full);
    wchar_t *suffix = _wcsdup(L"");
    for (int depth = 0; depth < 256; depth++) {
        wchar_t *rp = real_path_of(cur);
        if (rp) {
            buf joined;
            buf_init(&joined);
            const wchar_t *p = strip_ns_prefix(rp);
            buf_append(&joined, p, wcslen(p) * sizeof(wchar_t));
            if (suffix[0]) {
                buf_append(&joined, L"\\", sizeof(wchar_t));
                buf_append(&joined, suffix, wcslen(suffix) * sizeof(wchar_t));
            }
            wchar_t z = 0;
            buf_append(&joined, &z, sizeof(wchar_t));
            free(rp);
            free(cur);
            free(suffix);
            (void)root_real;
            return (wchar_t *)joined.p;
        }
        wchar_t *slash = wcsrchr(cur, L'\\');
        if (!slash || slash == cur) break;
        if (suffix[0]) {
            wchar_t *ns = xmalloc((wcslen(slash + 1) + 1 + wcslen(suffix) + 1) * sizeof(wchar_t));
            wcscpy(ns, slash + 1);
            wcscat(ns, L"\\");
            wcscat(ns, suffix);
            free(suffix);
            suffix = ns;
        } else {
            suffix = _wcsdup(slash + 1);
        }
        *slash = 0;
    }
    free(cur);
    free(suffix);
    return NULL;
}

char *path_canon_rel(const char *rel) {
    if (!rel) return xstrdup("");
    const wchar_t *root = git_root();
    if (!root) return xstrdup(rel);
    wchar_t *rw = u82w(rel);
    if (!rw) return xstrdup(rel);

    buf full;
    buf_init(&full);
    buf_append(&full, root, wcslen(root) * sizeof(wchar_t));
    buf_append(&full, L"\\", sizeof(wchar_t));
    for (wchar_t *q = rw; *q; q++) {
        wchar_t c = (*q == L'/') ? L'\\' : *q;
        buf_append(&full, &c, sizeof(wchar_t));
    }
    wchar_t z = 0;
    buf_append(&full, &z, sizeof(wchar_t));
    free(rw);

    wchar_t *resolved = resolve_with_existing_prefix((wchar_t *)full.p, root);
    buf_free(&full);
    if (!resolved) return xstrdup(rel);

    const wchar_t *p = strip_ns_prefix(resolved);
    size_t rl = wcslen(root);
    if (_wcsnicmp(p, root, rl) != 0 || p[rl] != L'\\' || !p[rl + 1]) {
        free(resolved);
        return xstrdup(rel);
    }
    wchar_t *tmp = _wcsdup(p + rl + 1);
    for (wchar_t *q = tmp; *q; q++)
        if (*q == L'\\') *q = L'/';
    char *out = w2u8(tmp);
    free(tmp);
    free(resolved);
    return out ? out : xstrdup(rel);
}

char *path_rel_in_root(const char *input, char **err) {
    if (err) *err = NULL;
    if (!input || !*input) { set_err(err, "%s", "路径为空"); return NULL; }
    /* 绝对路径：盘符、UNC、前导斜杠一律拒 */
    if ((isalpha((unsigned char)input[0]) && input[1] == ':') ||
        input[0] == '\\' || input[0] == '/') {
        set_err(err, "路径越界,已拒绝:%s", input);
        return NULL;
    }
    wchar_t *win = u82w(input);
    if (!win) { set_err(err, "%s", "路径不是合法 UTF-8"); return NULL; }

    /* 拼成绝对路径（'/' → '\\'），再用 GetFullPathNameW 消掉 . 与 .. */
    buf full;
    buf_init(&full);
    const wchar_t *root = git_root();
    buf_append(&full, root, wcslen(root) * sizeof(wchar_t));
    buf_append(&full, L"\\", sizeof(wchar_t));
    for (wchar_t *p = win; *p; p++) {
        wchar_t c = (*p == L'/') ? L'\\' : *p;
        buf_append(&full, &c, sizeof(wchar_t));
    }
    wchar_t z = 0;
    buf_append(&full, &z, sizeof(wchar_t));

    wchar_t canon[32768];
    DWORD n = GetFullPathNameW((wchar_t *)full.p, 32768, canon, NULL);
    buf_free(&full);
    free(win);
    if (!n || n >= 32768) { set_err(err, "路径不合法,已拒绝:%s", input); return NULL; }

    /* 词法越界检查 */
    if (!has_prefix_ci(canon, root)) { set_err(err, "路径越界,已拒绝:%s", input); return NULL; }
    const wchar_t *relw = canon + wcslen(root);
    if (*relw != L'\\') { set_err(err, "路径越界,已拒绝:%s", input); return NULL; }
    relw++;
    if (!*relw) { set_err(err, "路径指向仓库根目录本身,已拒绝:%s", input); return NULL; }

    /* .git 拦截（第一段，大小写不敏感） */
    {
        const wchar_t *seg_end = wcschr(relw, L'\\');
        size_t seg_len = seg_end ? (size_t)(seg_end - relw) : wcslen(relw);
        if (seg_len == 4 && _wcsnicmp(relw, L".git", 4) == 0) {
            set_err(err, "不允许操作 .git,已拒绝:%s", input);
            return NULL;
        }
    }

    /* 真实路径复查：顺着链接走出去的要拦住（存在祖先也要查） */
    wchar_t *resolved = resolve_with_existing_prefix(canon, root);
    if (resolved) {
        const wchar_t *rp = strip_ns_prefix(resolved);
        if (!has_prefix_ci(rp, root)) {
            free(resolved);
            set_err(err, "路径越界,已拒绝:%s", input);
            return NULL;
        }
        free(resolved);
    }

    /* 相对路径 → UTF-8，'\' → '/' */
    size_t rlen = wcslen(relw);
    wchar_t *tmp = xmalloc((rlen + 1) * sizeof(wchar_t));
    for (size_t i = 0; i <= rlen; i++) tmp[i] = (relw[i] == L'\\') ? L'/' : relw[i];
    char *out = w2u8(tmp);
    free(tmp);
    if (!out) { set_err(err, "%s", "路径不是合法 UTF-8"); return NULL; }
    return out;
}
