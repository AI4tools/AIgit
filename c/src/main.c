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
#include "version.h"
#include <windows.h>

#include "git.h"
#include "http.h"
#include "util.h"

static void usage(void) {
    fputs("usage: aigit.exe --root <repo> [--port <n>] [--sandbox] [--web <dir>]\n"
          "                 [--open] [--version]\n", stderr);
}

static int is_dir_w(const wchar_t *p) {
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

/* 找 web 目录：--web > exe 所在目录的 ../../web > 当前目录/web。
 * 只认存在的目录；找不到就只服务 API（与「前端缺失」时的 404 一致）。 */
static int resolve_web(const wchar_t *explicit_dir, wchar_t *out, size_t outsz) {
    if (explicit_dir && *explicit_dir) {
        if (!is_dir_w(explicit_dir)) return 0;
        _snwprintf(out, outsz, L"%ls", explicit_dir);
        return 1;
    }
    wchar_t exe[32768];
    DWORD n = GetModuleFileNameW(NULL, exe, 32768);
    if (n && n < 32768) {
        wchar_t *slash = wcsrchr(exe, L'\\');
        if (slash) {
            *slash = 0;
            wchar_t cand[32768];
            _snwprintf(cand, 32768, L"%ls\\..\\..\\web", exe);
            if (is_dir_w(cand)) {
                _snwprintf(out, outsz, L"%ls", cand);
                return 1;
            }
        }
    }
    wchar_t cand[32768];
    DWORD c = GetCurrentDirectoryW(32768, cand);
    if (c && c < 32768) {
        wchar_t w[32768];
        _snwprintf(w, 32768, L"%ls\\web", cand);
        if (is_dir_w(w)) {
            _snwprintf(out, outsz, L"%ls", w);
            return 1;
        }
    }
    return 0;
}

/* 递归删除（不跟随 reparse point：联接/符号链接本身当文件删）。 */
/* 超过 240 字符加 \\?\ 前缀（关掉 MAX_PATH 限制）。输入必须是绝对路径。 */
static const wchar_t *long_path(const wchar_t *p, wchar_t *buf, size_t n) {
    if (wcslen(p) < 240 || !wcsncmp(p, L"\\\\?\\", 4)) return p;
    _snwprintf(buf, n, L"\\\\?\\%ls", p);
    return buf;
}

static int delete_tree(const wchar_t *path) {
    DWORD a = GetFileAttributesW(path);
    if (a == INVALID_FILE_ATTRIBUTES) return 1;
    wchar_t lb[32768];
    const wchar_t *lp = long_path(path, lb, 32768);
    if (!(a & FILE_ATTRIBUTE_DIRECTORY) || (a & FILE_ATTRIBUTE_REPARSE_POINT)) {
        if (DeleteFileW(lp)) return 1;
        SetFileAttributesW(lp, FILE_ATTRIBUTE_NORMAL);
        return DeleteFileW(lp) ? 1 : 0;
    }
    wchar_t pat[32768];
    _snwprintf(pat, 32768, L"%ls\\*", path);
    wchar_t pb[32768];
    const wchar_t *pp = long_path(pat, pb, 32768);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pp, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..")) continue;
            wchar_t child[32768];
            _snwprintf(child, 32768, L"%ls\\%ls", path, fd.cFileName);
            wchar_t cb[32768];
            const wchar_t *cp = long_path(child, cb, 32768);
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
                    if (!RemoveDirectoryW(cp)) {   /* 只删联接本身 */
                        SetFileAttributesW(cp, FILE_ATTRIBUTE_NORMAL);
                        RemoveDirectoryW(cp);
                    }
                } else if (!delete_tree(child)) {
                    FindClose(h);
                    return 0;
                }
            } else if (!DeleteFileW(cp)) {
                /* 只读文件先改属性再试一次 */
                SetFileAttributesW(cp, FILE_ATTRIBUTE_NORMAL);
                if (!DeleteFileW(cp)) { FindClose(h); return 0; }
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    if (RemoveDirectoryW(lp)) return 1;
    SetFileAttributesW(lp, FILE_ATTRIBUTE_NORMAL);
    return RemoveDirectoryW(lp) ? 1 : 0;
}

static int write_readme(const wchar_t *root) {
    wchar_t p[32768];
    _snwprintf(p, 32768, L"%ls\\README.md", root);
    HANDLE h = CreateFileW(p, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    const char *txt = "# aigit sandbox\n\n这是一个演练仓,可以随便删。\n";
    DWORD wrote = 0;
    BOOL ok = WriteFile(h, txt, (DWORD)strlen(txt), &wrote, NULL) &&
              wrote == (DWORD)strlen(txt);
    CloseHandle(h);
    return ok ? 1 : 0;
}

/* 与 Python `main()` 同义：root 已建好后，若不是仓就 init + README + 首个提交。
 * （--sandbox 只是多了一步「先清空」；非 sandbox 下同样会 init。） */
static int ensure_repo(const wchar_t *root) {
    wchar_t dg[32768];
    _snwprintf(dg, 32768, L"%ls\\.git", root);
    if (GetFileAttributesW(dg) != INVALID_FILE_ATTRIBUTES) return 1;

    buf o, e;
    buf_init(&o);
    buf_init(&e);
    const char *a1[] = { "init", "-b", "main", NULL };
    int rc = git_run(a1, NULL, 0, &o, &e);
    buf_reset(&o);
    buf_reset(&e);
    /* 与 Python `Repo.init()` 逐条对齐：默认分支 main + 仓级身份 +
     * 关掉 gpg 签名（否则 guard 把用户配置挡掉后 commit 会失败）。 */
    if (rc == 0) {
        const char *k1[] = { "config", "user.email", "aigit@localhost", NULL };
        git_run(k1, NULL, 0, &o, &e);
        buf_reset(&o);
        buf_reset(&e);
        const char *k2[] = { "config", "user.name", "aigit", NULL };
        git_run(k2, NULL, 0, &o, &e);
        buf_reset(&o);
        buf_reset(&e);
        const char *k3[] = { "config", "commit.gpgsign", "false", NULL };
        git_run(k3, NULL, 0, &o, &e);
        buf_reset(&o);
        buf_reset(&e);
        write_readme(root);
        const char *a2[] = { "add", "--", "README.md", NULL };
        git_run(a2, NULL, 0, &o, &e);
        buf_reset(&o);
        buf_reset(&e);
        const char *a3[] = { "commit", "-m", "init: sandbox", NULL };
        git_run(a3, NULL, 0, &o, &e);
    }
    if (rc != 0)
        fputs("aigit: git init 失败\n", stderr);
    buf_free(&o);
    buf_free(&e);
    return rc == 0;
}


/* 把根路径规范成「真实长路径 + 真实大小写」（Python 的 Path.resolve() 同义）。
 *
 * 为什么必须有：TEMP 在 GitHub runner 上是 8.3 短名（C:\Users\RUNNER~1\...），
 * 而 path_rel_in_root 用 GetFinalPathNameByHandleW 拿到的**真实**路径去比较。
 * 根还是短名时，仓内每个文件都会被判成「路径越界」—— 实测 CI 上 /api/file、
 * /api/diff 全 400。junction / symlink / 大小写不一致同理。
 */
static void canon_root_w(wchar_t *p) {
    HANDLE h = CreateFileW(p, 0,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED);
    if (need) {
        wchar_t *b = (wchar_t *)xmalloc(((size_t)need + 1) * sizeof(wchar_t));
        DWORD got = GetFinalPathNameByHandleW(h, b, need + 1, FILE_NAME_NORMALIZED);
        if (got && got <= need) {
            const wchar_t *q = b;
            if (!wcsncmp(q, L"\\\\?\\UNC\\", 8)) {
                _snwprintf(p, 32768, L"\\\\%ls", q + 8);
            } else if (!wcsncmp(q, L"\\\\?\\", 4)) {
                _snwprintf(p, 32768, L"%ls", q + 4);
            } else {
                _snwprintf(p, 32768, L"%ls", q);
            }
        }
        free(b);
    }
    CloseHandle(h);
}

static HANDLE g_repolock = INVALID_HANDLE_VALUE;

/* 取真实 gitdir：普通仓是 <root>\.git；worktree/submodule 的 .git 是文件，
 * 内容形如 "gitdir: <path>"（可能是相对路径）。取不到返回 0。 */
static int resolve_gitdir_w(const wchar_t *root, wchar_t *out, size_t outsz) {
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
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    char *p = strstr(buf, "gitdir:");
    if (!p) return 0;
    p += 7;
    while (*p == ' ' || *p == '\t') p++;
    size_t len = strlen(p);
    while (len && (p[len - 1] == '\r' || p[len - 1] == '\n' || p[len - 1] == ' '))
        p[--len] = 0;
    if (!len) return 0;
    wchar_t *rel = u82w(p);
    if (!rel) return 0;
    wchar_t full[32768];
    if (rel[0] == L'\\' || (iswalpha(rel[0]) && rel[1] == L':')) {
        _snwprintf(full, 32768, L"%ls", rel);
    } else {
        _snwprintf(full, 32768, L"%ls\\%ls", root, rel);
    }
    free(rel);
    wchar_t canon[32768];
    if (!GetFullPathNameW(full, 32768, canon, NULL)) return 0;
    _snwprintf(out, outsz, L"%ls", canon);
    return 1;
}

/* 锁文件放到 %TEMP%，不要放进 .git：
 * 1) 放 .git 里会让「重命名/删除 .git」被占用拒绝（测试与真实运维都会碰到）；
 * 2) 按真实 gitdir 的哈希命名，同一仓的不同 worktree 也落在同一把锁上。 */
static int lock_path_for(const wchar_t *gd, wchar_t *out, size_t outsz) {
    wchar_t tmp[MAX_PATH];
    DWORD n = GetTempPathW(MAX_PATH, tmp);
    if (!n) return 0;
    unsigned long long h = 1469598103934665603ULL;
    for (const wchar_t *p = gd; *p; p++) {
        unsigned v = (unsigned)*p;
        h ^= (unsigned long long)(v & 0xff);
        h *= 1099511628211ULL;
        h ^= (unsigned long long)((v >> 8) & 0xff);
        h *= 1099511628211ULL;
    }
    _snwprintf(out, outsz, L"%lsaigit-lock-%016llx.lock", tmp, h);
    return 1;
}

/* 进程级仓锁：两个 aigit 同时服务同一个仓会互相抢 .git/index.lock。
 * 用独占打开的锁文件（OS 在进程退出时自动释放），第二个实例明确拒绝。
 * worktree 等解析不到 gitdir 时跳过锁，不阻止启动。 */
static int acquire_repo_lock(const wchar_t *root) {
    wchar_t gd[32768];
    if (!resolve_gitdir_w(root, gd, 32768)) return 1;
    wchar_t p[32768];
    if (!lock_path_for(gd, p, 32768)) return 1;
    for (int i = 0; i < 40; i++) {
        g_repolock = CreateFileW(p, GENERIC_READ | GENERIC_WRITE, 0, NULL,
                                 OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (g_repolock != INVALID_HANDLE_VALUE) return 1;
        Sleep(50);
    }
    werr(L"aigit: 这个仓已经有另一个 aigit 实例在服务（锁文件 %ls）\n", p);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *root = NULL;
    unsigned short port = 8899;
    int sandbox = 0;
    int open_browser = 0;
    const wchar_t *webdir_arg = NULL;
    for (int i = 1; i < argc; i++) {
        if (!wcscmp(argv[i], L"--root") && i + 1 < argc) {
            root = argv[++i];
        } else if (!wcscmp(argv[i], L"--port") && i + 1 < argc) {
            port = (unsigned short)_wtoi(argv[++i]);
        } else if (!wcscmp(argv[i], L"--sandbox")) {
            sandbox = 1;
        } else if (!wcscmp(argv[i], L"--web") && i + 1 < argc) {
            webdir_arg = argv[++i];
        } else if (!wcscmp(argv[i], L"-h") || !wcscmp(argv[i], L"--help")) {
            usage();
            return 0;
        } else if (!wcscmp(argv[i], L"-v") || !wcscmp(argv[i], L"--version")) {
            printf("aigit %s\n", AIGIT_VERSION);
            return 0;
        } else if (!wcscmp(argv[i], L"--open")) {
            /* 测试/无人值守环境用 AIGIT_NO_BROWSER=1 压掉，避免弹出浏览器 */
            open_browser = getenv("AIGIT_NO_BROWSER") ? 0 : 1;
        } else {
            usage();
            return 2;
        }
    }
    if (!root || !*root) { usage(); return 2; }

    wchar_t full[32768];
    DWORD n = GetFullPathNameW(root, 32768, full, NULL);
    canon_root_w(full);
    if (!n || n >= 32768) {
        fputs("aigit: bad --root\n", stderr);
        return 2;
    }
    if (full[0] >= L'a' && full[0] <= L'z') full[0] = (wchar_t)(full[0] - 32);
    size_t len = wcslen(full);
    while (len > 3 && (full[len - 1] == L'\\' || full[len - 1] == L'/')) full[--len] = 0;

    if (sandbox) {
        if (is_dir_w(full)) {
            if (!delete_tree(full)) {
                werr(L"aigit: 无法清空 --root: %ls\n", full);
                return 2;
            }
        }
        if (!CreateDirectoryW(full, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) {
            werr(L"aigit: 无法创建 --root: %ls\n", full);
            return 2;
        }
        git_init(full);
        if (!ensure_repo(full)) return 1;
    } else {
        /* Python 的 main()：root 不存在就 init；存在但不是仓也 init（+README
         * + "init: sandbox" 提交）。只有 --sandbox 才会先清空目录。 */
        if (!is_dir_w(full)) {
            if (GetFileAttributesW(full) != INVALID_FILE_ATTRIBUTES) {
                werr(L"aigit: --root 不是目录: %ls\n", full);
                return 2;
            }
            if (!CreateDirectoryW(full, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) {
                werr(L"aigit: 无法创建 --root: %ls\n", full);
                return 2;
            }
        }
        git_init(full);
        if (!ensure_repo(full)) return 1;
    }

    if (!acquire_repo_lock(full)) return 1;

    {
        wchar_t webdir[32768];
        if (resolve_web(webdir_arg, webdir, 32768)) http_set_web(webdir);
        else http_set_web(NULL);
    }
    if (getenv("AIGIT_C_DEBUG")) fputs("[c] after git_init\n", stderr);
    g_count_on = (getenv("AIGIT_GIT_COUNT") && !strcmp(getenv("AIGIT_GIT_COUNT"), "1")) ? 1 : 0;
    http_set_open(open_browser);
    return http_serve(port);
}
