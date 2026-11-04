#include "catfile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

#include "git.h"

#define WBUF 4096

/* ================================================================ cat-file 常驻
 *
 * 与 Python gitcat.py 对位：一个进程、一问一答、不计数。进程起不来或中途坏掉
 * 一律回退到 `git show`（那次**会**计数，与 Python 的回退同形）。
 */

#define CAT_MAX_BYTES (128u * 1024u * 1024u)

typedef struct {
    HANDLE proc, wr, rd;
    CRITICAL_SECTION cs;
    int state;                       /* 0 未启/起不来, 1 可用, 2 已死 */
    char buf[65536];
    size_t pos, len;
} catsess;

static catsess g_cat;
static INIT_ONCE g_cat_once = INIT_ONCE_STATIC_INIT;

static void cmdline_add(buf *b, const wchar_t *arg, int first) {
    int need = 0;
    for (const wchar_t *p = arg; *p; p++)
        if (*p == L' ' || *p == L'\t' || *p == L'"') { need = 1; break; }
    if (!first) buf_append(b, L" ", sizeof(wchar_t));
    if (!need) { buf_append(b, arg, wcslen(arg) * sizeof(wchar_t)); return; }
    buf_append(b, L"\"", sizeof(wchar_t));
    size_t bs = 0;
    for (const wchar_t *p = arg; *p; p++) {
        if (*p == L'\\') { bs++; continue; }
        if (*p == L'"') {
            for (size_t i = 0; i < bs * 2 + 1; i++) buf_append(b, L"\\", sizeof(wchar_t));
            buf_append(b, L"\"", sizeof(wchar_t));
            bs = 0;
            continue;
        }
        for (size_t i = 0; i < bs; i++) buf_append(b, L"\\", sizeof(wchar_t));
        bs = 0;
        buf_append(b, p, sizeof(wchar_t));
    }
    for (size_t i = 0; i < bs * 2; i++) buf_append(b, L"\\", sizeof(wchar_t));
    buf_append(b, L"\"", sizeof(wchar_t));
}

static void cat_shutdown(void) {
    if (g_cat.state == 1) {
        g_cat.state = 2;
        if (g_cat.wr != INVALID_HANDLE_VALUE) CloseHandle(g_cat.wr);
        WaitForSingleObject(g_cat.proc, 2000);
        TerminateProcess(g_cat.proc, 0);
        CloseHandle(g_cat.proc);
        if (g_cat.rd != INVALID_HANDLE_VALUE) CloseHandle(g_cat.rd);
    }
}

static BOOL CALLBACK cat_init_cb(PINIT_ONCE once, PVOID param, PVOID *ctx) {
    (void)once; (void)param; (void)ctx;
    g_cat.proc = g_cat.wr = g_cat.rd = INVALID_HANDLE_VALUE;
    g_cat.state = 0;
    InitializeCriticalSection(&g_cat.cs);

    buf cmd;
    buf_init(&cmd);
    cmdline_add(&cmd, L"git", 1);
    cmdline_add(&cmd, L"-C", 0);
    cmdline_add(&cmd, git_root(), 0);
    cmdline_add(&cmd, L"cat-file", 0);
    cmdline_add(&cmd, L"--batch", 0);
    wchar_t z = 0;
    buf_append(&cmd, &z, sizeof(wchar_t));

    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE in_r = INVALID_HANDLE_VALUE, in_w = INVALID_HANDLE_VALUE;
    HANDLE out_r = INVALID_HANDLE_VALUE, out_w = INVALID_HANDLE_VALUE;
    HANDLE nul = INVALID_HANDLE_VALUE;
    if (CreatePipe(&in_r, &in_w, &sa, 0) && CreatePipe(&out_r, &out_w, &sa, 0)) {
        SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
        nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
                          FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING,
                          FILE_ATTRIBUTE_NORMAL, NULL);
        STARTUPINFOW si;
        PROCESS_INFORMATION pi;
        memset(&si, 0, sizeof(si));
        memset(&pi, 0, sizeof(pi));
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = in_r;
        si.hStdOutput = out_w;
        si.hStdError = nul != INVALID_HANDLE_VALUE ? nul : out_w;
        /* 环境继承父进程（cat-file 只读对象，不受 guard 影响；与 Python 一样
         * 这次 Popen 不进计数）。 */
        if (CreateProcessW(NULL, (wchar_t *)cmd.p, NULL, NULL, TRUE,
                           0, NULL, git_root(), &si, &pi)) {
            CloseHandle(pi.hThread);
            g_cat.proc = pi.hProcess;
            g_cat.wr = in_w;
            g_cat.rd = out_r;
            g_cat.state = 1;
            atexit(cat_shutdown);
        }
    }
    if (g_cat.state != 1) {
        if (in_r != INVALID_HANDLE_VALUE) CloseHandle(in_r);
        if (in_w != INVALID_HANDLE_VALUE) CloseHandle(in_w);
        if (out_r != INVALID_HANDLE_VALUE) CloseHandle(out_r);
        if (out_w != INVALID_HANDLE_VALUE) CloseHandle(out_w);
    } else {
        CloseHandle(in_r);
        CloseHandle(out_w);
    }
    if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
    buf_free(&cmd);
    return TRUE;
}

static int cat_fill(void) {
    DWORD got = 0;
    if (!ReadFile(g_cat.rd, g_cat.buf, (DWORD)sizeof(g_cat.buf), &got, NULL) || got == 0) return 0;
    g_cat.len = got;
    g_cat.pos = 0;
    return 1;
}

static int cat_getc(void) {
    if (g_cat.pos >= g_cat.len && !cat_fill()) return -1;
    return (unsigned char)g_cat.buf[g_cat.pos++];
}

static int cat_read_bytes(buf *o, size_t n) {
    while (n) {
        if (g_cat.pos >= g_cat.len && !cat_fill()) return 0;
        size_t avail = g_cat.len - g_cat.pos;
        size_t take = n < avail ? n : avail;
        if (o) buf_append(o, g_cat.buf + g_cat.pos, take);
        g_cat.pos += take;
        n -= take;
    }
    return 1;
}

/* 带状态的版本：status 1=ok、0=missing、-1=ambiguous、-2=进程坏/不可用。
 * ok 时 got 收 cat-file 回显的完整 sha（可为 NULL）。 */
int cat_read_ex(const char *spec, char *got, size_t gotsz, buf *data,
                char *type, size_t typesz, int *status) {
    if (status) *status = -2;
    if (!spec || !*spec || strchr(spec, '\n')) return 0;
    /* 与 Python gitcat.read 一致：`.git` 不是目录（worktree / submodule）就不用常驻。 */
    {
        wchar_t gd[WBUF];
        _snwprintf(gd, WBUF, L"%ls\\.git", git_root());
        DWORD a = GetFileAttributesW(gd);
        if (a == INVALID_FILE_ATTRIBUTES || !(a & FILE_ATTRIBUTE_DIRECTORY)) return 0;
    }
    InitOnceExecuteOnce(&g_cat_once, cat_init_cb, NULL, NULL);
    if (g_cat.state != 1) return 0;

    int ok = 0;
    EnterCriticalSection(&g_cat.cs);
    if (g_cat.state == 1) {
        size_t sl = strlen(spec);
        DWORD wrote = 0;
        int good = WriteFile(g_cat.wr, spec, (DWORD)sl, &wrote, NULL) && wrote == (DWORD)sl &&
                   WriteFile(g_cat.wr, "\n", 1, &wrote, NULL) && wrote == 1;
        if (!good) {
            g_cat.state = 2;
        } else {
            char line[512];
            size_t ln = 0;
            int bad = 0;
            for (;;) {
                int c = cat_getc();
                if (c < 0) { bad = 1; break; }
                if (c == '\n') break;
                if (ln + 1 < sizeof(line)) line[ln++] = (char)c;
                else bad = 1;
            }
            line[ln] = 0;
            if (!bad) {
                /* `git cat-file --batch` 对没有对象的 spec 回的是
                 * `<原样输入> missing`（输入里有空格就整行带空格）。
                 * 必须先按后缀判定，否则会被当成 `<sha> <type> <size>`，
                 * 读一个不存在的 payload 而永久阻塞（实测卡死）。 */
                size_t L = strlen(line);
                if (L >= 8 && !strcmp(line + L - 8, " missing")) {
                    if (status) *status = 0;
                } else if (L >= 10 && !strcmp(line + L - 10, " ambiguous")) {
                    if (status) *status = -1;
                } else {
                char *sp1 = strchr(line, ' ');
                if (!sp1) {
                    bad = 1;
                } else {
                    *sp1 = 0;
                    char *rest = sp1 + 1;
                    char *sp2 = strchr(rest, ' ');
                    if (!sp2) {
                        if (!strcmp(rest, "missing")) {
                            if (status) *status = 0;
                        } else if (!strcmp(rest, "ambiguous")) {
                            if (status) *status = -1;
                        } else {
                            bad = 1;
                        }
                    } else {
                        *sp2 = 0;
                        size_t size = (size_t)strtoull(sp2 + 1, NULL, 10);
                        if (size > CAT_MAX_BYTES) {
                            if (!cat_read_bytes(NULL, size) || cat_getc() < 0) bad = 1;
                        } else if (!cat_read_bytes(data, size) || cat_getc() < 0) {
                            bad = 1;
                        } else {
                            if (type && typesz) snprintf(type, typesz, "%s", rest);
                            if (got && gotsz) snprintf(got, gotsz, "%s", line);
                            if (status) *status = 1;
                            ok = 1;
                        }
                    }
                }
                }
            }
            if (bad) g_cat.state = 2;
        }
    }
    LeaveCriticalSection(&g_cat.cs);
    return ok;
}


/* 兼容旧调用：只要「对象存在且读到了」。 */
int cat_read(const char *spec, buf *data, char *type, size_t typesz) {
    int st = -2;
    int ok = cat_read_ex(spec, NULL, 0, data, type, typesz, &st);
    return ok && st == 1;
}