/* main.c —— aigit C 版入口（M0：health + status）。
 *
 *   aigit.exe --root <仓库> [--port 8899]
 *
 * 环境变量 AIGIT_GIT_COUNT=1 时，响应带 X-aigit-git-calls / X-aigit-git-cmds
 * （与 Python 版同名同义）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "http.h"
#include <windows.h>

#include "git.h"
#include "http.h"
#include "util.h"

static void usage(void) {
    fputs("usage: aigit.exe --root <repo> [--port <n>]\n", stderr);
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *root = NULL;
    unsigned short port = 8899;
    for (int i = 1; i < argc; i++) {
        if (!wcscmp(argv[i], L"--root") && i + 1 < argc) {
            root = argv[++i];
        } else if (!wcscmp(argv[i], L"--port") && i + 1 < argc) {
            port = (unsigned short)_wtoi(argv[++i]);
        } else if (!wcscmp(argv[i], L"-h") || !wcscmp(argv[i], L"--help")) {
            usage();
            return 0;
        } else {
            usage();
            return 2;
        }
    }
    if (!root || !*root) { usage(); return 2; }

    wchar_t full[32768];
    DWORD n = GetFullPathNameW(root, 32768, full, NULL);
    if (!n || n >= 32768) {
        fputs("aigit: bad --root\n", stderr);
        return 2;
    }
    if (full[0] >= L'a' && full[0] <= L'z') full[0] = (wchar_t)(full[0] - 32);
    size_t len = wcslen(full);
    while (len > 3 && (full[len - 1] == L'\\' || full[len - 1] == L'/')) full[--len] = 0;

    DWORD attr = GetFileAttributesW(full);
    if (attr == INVALID_FILE_ATTRIBUTES || !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
        fwprintf(stderr, L"aigit: --root 不是目录: %ls\n", full);
        return 2;
    }

    if (getenv("AIGIT_C_DEBUG")) fputs("[c] before git_init\n", stderr);
    git_init(full);
    if (getenv("AIGIT_C_DEBUG")) fputs("[c] after git_init\n", stderr);
    g_count_on = (getenv("AIGIT_GIT_COUNT") && !strcmp(getenv("AIGIT_GIT_COUNT"), "1")) ? 1 : 0;
    return http_serve(port);
}
