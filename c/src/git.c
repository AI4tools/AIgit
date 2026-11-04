#include "git.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "guardcfg.h"

static wchar_t *g_root = NULL;
static HANDLE g_nul = INVALID_HANDLE_VALUE;
static wchar_t *g_hooks = NULL;
static wchar_t *g_attrs = NULL;

/* 按配置枚举出来的 driver 覆盖（UTF-8 "key=value"，不含 -c） */
static char **g_guards = NULL;
static size_t g_guards_n = 0;
static unsigned long long g_guard_stamp = 0;

/* 语义配置（用户视角） */
typedef struct { wchar_t *key; wchar_t *val; } kv;
static kv    *g_sem = NULL;
static size_t g_sem_n = 0;

/* 每请求的计数必须线程本地：服务是每连接一线程，全局计数在并发下会串。 */
static __thread int  g_calls = 0;
static __thread buf  g_cmds;
static __thread int  g_cmds_init = 0;

static int dbg(void) { const char *e = getenv("AIGIT_C_DEBUG"); return e && *e == 0x31; }

const wchar_t *git_root(void) { return g_root; }

void git_count_reset(void) {
    g_calls = 0;
    if (!g_cmds_init) { buf_init(&g_cmds); g_cmds_init = 1; }
    buf_reset(&g_cmds);
}

int git_calls(void) { return g_calls; }

const char *git_cmds(void) {
    if (!g_cmds_init) { buf_init(&g_cmds); g_cmds_init = 1; }
    return buf_cstr(&g_cmds);
}

/* ---------------------------------------------------------------- 环境块 */

/* 需要从父环境里删掉的键（能注入执行面的那一族） */
static int env_drop(const wchar_t *entry, size_t keylen) {
    static const wchar_t *drop[] = {
        L"GIT_CONFIG_COUNT", L"GIT_CONFIG_PARAMETERS",
        NULL
    };
    for (int i = 0; drop[i]; i++)
        if (keylen == wcslen(drop[i]) && !_wcsnicmp(entry, drop[i], keylen)) return 1;
    static const wchar_t *prefix[] = { L"GIT_CONFIG_KEY_", L"GIT_CONFIG_VALUE_", NULL };
    for (int i = 0; prefix[i]; i++) {
        size_t n = wcslen(prefix[i]);
        if (keylen >= n && !_wcsnicmp(entry, prefix[i], n)) return 1;
    }
    return 0;
}

/* guarded 模式下我们要自己设的键：父环境里的同名项必须先删掉。
 * Windows 环境块里同名键并存时子进程取哪个不保证（实测取到父环境那个），
 * 那会让 GIT_CONFIG_GLOBAL 隔离整个失效。 */
static int env_overridden(const wchar_t *entry, size_t keylen, int net) {
    static const wchar_t *own[] = {
        L"GIT_CONFIG_NOSYSTEM", L"GIT_CONFIG_GLOBAL",
        L"GIT_TERMINAL_PROMPT", L"GIT_PAGER", L"GIT_EDITOR",
        L"GIT_SEQUENCE_EDITOR", L"GIT_MERGE_AUTOEDIT",
        NULL
    };
    for (int i = 0; own[i]; i++) {
        /* 联网动词（net=1）要保留用户自己的 GIT_CONFIG_GLOBAL —— 凭据/代理
         * 都靠它；其余键仍然由我们统一设置。 */
        if (net && !wcscmp(own[i], L"GIT_CONFIG_GLOBAL")) continue;
        if (keylen == wcslen(own[i]) && !_wcsnicmp(entry, own[i], keylen)) return 1;
    }
    return 0;
}

/* 构造环境块：父环境（过滤）+ 我们的固定项 + 语义配置。
 * net=1 表示本次是远程动词：不碰用户级配置（凭据/代理要能用）。 */
static wchar_t *build_env(int guarded, int net) {
    buf out;                       /* 存 wchar_t，末尾双 NUL */
    buf_init(&out);
    wchar_t *parent = GetEnvironmentStringsW();
    for (wchar_t *p = parent; p && *p; p += wcslen(p) + 1) {
        const wchar_t *eq = wcschr(p, L'=');
        if (!eq) continue;
        size_t keylen = (size_t)(eq - p);
        if (keylen == 0) continue;                 /* =C:=... 这种伪条目 */
        if (env_drop(p, keylen)) continue;
        if (guarded && env_overridden(p, keylen, net)) continue;
        buf_append(&out, p, (wcslen(p) + 1) * sizeof(wchar_t));
    }
    if (parent) FreeEnvironmentStringsW(parent);

    if (!guarded) {
        buf_reserve(&out, sizeof(wchar_t));
        wchar_t zz = 0;
        buf_append(&out, &zz, sizeof(wchar_t));
        return (wchar_t *)out.p;
    }

    static const wchar_t *fixed[] = {
        L"GIT_CONFIG_NOSYSTEM=1",
        /* 指向一个不存在的路径：用户级配置整个不可达。
         * 远程动词（net=1）不设，否则 https/ssh 的凭据与 url.* 改写都失效。 */
        L"GIT_CONFIG_GLOBAL=__AIGIT_NO_USER_CONFIG__",
        L"GIT_TERMINAL_PROMPT=0",
        L"GIT_PAGER=cat",
        L"GIT_EDITOR=true",
        L"GIT_SEQUENCE_EDITOR=true",
        L"GIT_MERGE_AUTOEDIT=no",
        NULL
    };
    for (int i = 0; fixed[i]; i++) {
        if (net && !wcsncmp(fixed[i], L"GIT_CONFIG_GLOBAL=", 19)) continue;
        buf_append(&out, fixed[i], (wcslen(fixed[i]) + 1) * sizeof(wchar_t));
    }

    if (g_sem_n) {
        wchar_t tmp[64];
        _snwprintf(tmp, 64, L"GIT_CONFIG_COUNT=%zu", g_sem_n);
        buf_append(&out, tmp, (wcslen(tmp) + 1) * sizeof(wchar_t));
        for (size_t i = 0; i < g_sem_n; i++) {
            wchar_t line[1024];
            _snwprintf(line, 1024, L"GIT_CONFIG_KEY_%zu=%ls", i, g_sem[i].key);
            buf_append(&out, line, (wcslen(line) + 1) * sizeof(wchar_t));
            _snwprintf(line, 1024, L"GIT_CONFIG_VALUE_%zu=%ls", i, g_sem[i].val);
            buf_append(&out, line, (wcslen(line) + 1) * sizeof(wchar_t));
        }
    }
    buf_reserve(&out, sizeof(wchar_t));
    wchar_t z = 0;
    buf_append(&out, &z, sizeof(wchar_t));         /* 双 NUL 结尾 */
    return (wchar_t *)out.p;
}

/* ---------------------------------------------------------------- 命令行 */

static void cmdline_add(buf *b, const wchar_t *arg, int first) {
    /* Windows 命令行引用规则：含空格/制表/引号才加引号；反斜杠在引号前要翻倍 */
    int need = 0;
    for (const wchar_t *p = arg; *p; p++)
        if (*p == L' ' || *p == L'\t' || *p == L'"') { need = 1; break; }
    if (!first) buf_append(b, L" ", sizeof(wchar_t));
    if (!arg[0]) {
        /* 空参数必须写成 ""；什么都不写等于把参数吞掉（实测 `-m ""` 会变成裸
         * `-m`，git 报 "switch 'm' requires a value"）。 */
        buf_append(b, L"\"\"", 2 * sizeof(wchar_t));
        return;
    }
    if (!need) {
        buf_append(b, arg, wcslen(arg) * sizeof(wchar_t));
        return;
    }
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

/* 宽字符缓冲区收尾：buf_cstr 只写 1 字节 0，宽字符命令行需要 2 字节 0 */
static wchar_t *cmdline_finish(buf *b) {
    wchar_t z = 0;
    buf_append(b, &z, sizeof(wchar_t));
    return (wchar_t *)b->p;
}

/* ---------------------------------------------------------------- 读管道 */

typedef struct { HANDLE h; buf *out; } reader_ctx;

static DWORD WINAPI reader_thread(LPVOID param) {
    reader_ctx *c = param;
    char tmp[4096];
    DWORD got = 0;
    while (ReadFile(c->h, tmp, sizeof(tmp), &got, NULL) && got > 0) {
        if (c->out) buf_append(c->out, tmp, got);
    }
    return 0;
}

/* ---------------------------------------------------------------- 初始化 */

static void make_empty_dir(wchar_t *dst, size_t n) {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    _snwprintf(dst, n, L"%lsaigit-c-guard-%lu\\hooks", tmp, (unsigned long)GetCurrentProcessId());
    wchar_t parent[MAX_PATH];
    _snwprintf(parent, MAX_PATH, L"%lsaigit-c-guard-%lu", tmp, (unsigned long)GetCurrentProcessId());
    CreateDirectoryW(parent, NULL);
    CreateDirectoryW(dst, NULL);
}

static void make_empty_file(wchar_t *dst, size_t n) {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    _snwprintf(dst, n, L"%lsaigit-c-guard-%lu\\attributes", tmp, (unsigned long)GetCurrentProcessId());
    HANDLE h = CreateFileW(dst, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
}

/* 用**用户环境**问一次 core.* 语义键（per-root 缓存） */
static void load_semantic(void) {
    const char *rx = "^core\\.(autocrlf|eol|safecrlf|symlinks|ignorecase|filemode|"
                     "quotepath|precomposeunicode|excludesfile|trustctime|checkstat)$";
    buf out;
    buf_init(&out);
    int rc = 0;
    /* 递归调用会加 guard，这里要的是用户视角：直接跑一次裸 git。 */
    {
        buf cmd;
        buf_init(&cmd);
        cmdline_add(&cmd, L"git", 1);
        cmdline_add(&cmd, L"-C", 0);
        cmdline_add(&cmd, g_root, 0);
        cmdline_add(&cmd, L"config", 0);
        cmdline_add(&cmd, L"--get-regexp", 0);
        wchar_t *rxw = u82w(rx);
        cmdline_add(&cmd, rxw, 0);
        free(rxw);

        SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
        HANDLE rd = NULL, wr = NULL;
        CreatePipe(&rd, &wr, &sa, 0);
        SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);
        STARTUPINFOW si;
        PROCESS_INFORMATION pi;
        memset(&si, 0, sizeof(si));
        memset(&pi, 0, sizeof(pi));
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdOutput = wr;
        si.hStdError = wr;
        si.hStdInput = g_nul;
        wchar_t *env = build_env(0, 0);     /* 语义配置必须用**用户环境**读 */
        wchar_t *cmdline = cmdline_finish(&cmd);
        if (CreateProcessW(NULL, cmdline, NULL, NULL, TRUE,
                           CREATE_UNICODE_ENVIRONMENT, env, g_root,
                           &si, &pi)) {
            CloseHandle(wr);
            /* git 输出的是 **UTF-8 字节流**，不是 UTF-16。原实现把 got 字节当
             * wchar_t 数（got/sizeof(wchar_t)）再转 UTF-8，等于把 UTF-8 当 UTF-16，
             * 结果 semantic keys 永远为 0 —— global/system 里的 core.autocrlf 会丢。
             * 这里直接按字节累积（下游解析只认 ASCII 的 "key value" 行）。 */
            char cbuf[4096];
            DWORD got = 0;
            while (ReadFile(rd, cbuf, sizeof(cbuf), &got, NULL) && got > 0)
                buf_append(&out, cbuf, got);
            WaitForSingleObject(pi.hProcess, 30000);
            DWORD code = 0;
            GetExitCodeProcess(pi.hProcess, &code);
            rc = (int)code;
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
        } else {
            CloseHandle(wr);
        }
        CloseHandle(rd);
        free(env);
        free(cmd.p);
    }
    /* 解析 "key value" 行 */
    if (rc == 0) {
        char *s = buf_cstr(&out);
        char *line = s;
        while (line && *line) {
            char *nl = strchr(line, '\n');
            if (nl) *nl = 0;
            char *sp = strchr(line, ' ');
            if (sp) {
                *sp = 0;
                g_sem = xrealloc(g_sem, (g_sem_n + 1) * sizeof(kv));
                g_sem[g_sem_n].key = u82w(line);
                g_sem[g_sem_n].val = u82w(sp + 1);
                if (g_sem[g_sem_n].key && g_sem[g_sem_n].val) g_sem_n++;
                else { free(g_sem[g_sem_n].key); free(g_sem[g_sem_n].val); }
            }
            line = nl ? nl + 1 : NULL;
        }
    }
    buf_free(&out);
}

void git_init(const wchar_t *root) {
    if (dbg()) fputs("[c] git_init: start\n", stderr);
    g_root = _wcsdup(root);
    g_hooks = xmalloc(MAX_PATH * sizeof(wchar_t));
    g_attrs = xmalloc(MAX_PATH * sizeof(wchar_t));
    /* stdin 的占位句柄必须是**可继承**的：STARTF_USESTDHANDLES 要求三个句柄都可继承,
       否则 CreateProcessW 直接 ERROR_INVALID_PARAMETER(87)。 */
    SECURITY_ATTRIBUTES nsa = { sizeof(nsa), NULL, TRUE };
    g_nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
                       FILE_SHARE_READ | FILE_SHARE_WRITE, &nsa, OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (dbg()) fprintf(stderr, "[c] nul handle=%p err=%lu\n", (void *)g_nul, (unsigned long)GetLastError());
    make_empty_dir(g_hooks, MAX_PATH);
    make_empty_file(g_attrs, MAX_PATH);
    if (dbg()) fputs("[c] git_init: guard dirs ok\n", stderr);
    load_semantic();
    g_guards = guard_driver_pairs(root, &g_guards_n);
    g_guard_stamp = guard_config_stamp(root);
    if (dbg()) {
        fprintf(stderr, "[c] guard driver pairs=%zu\n", g_guards_n);
        for (size_t i = 0; i < g_guards_n; i++) fprintf(stderr, "[c]   %s\n", g_guards[i]);
    }
    if (dbg()) fprintf(stderr, "[c] git_init: semantic keys=%zu\n", g_sem_n);
}

/* 只记一笔调用、不 spawn —— 与 Python 在能力闸**之前**就记 COUNT_HOOK 的行为对齐。 */
void git_count_note(const char *cmd) {
    if (cmd && cmd[0] != '-') {
        if (!g_cmds_init) { buf_init(&g_cmds); g_cmds_init = 1; }
        if (g_cmds.n) buf_putc(&g_cmds, ',');
        buf_puts(&g_cmds, cmd);
    }
    g_calls++;
}

/* ---------------------------------------------------------------- 运行 */

static int is_diff_verb(const char *v) {
    return v && (!strcmp(v, "diff") || !strcmp(v, "show") || !strcmp(v, "log") ||
                 !strcmp(v, "whatchanged") || !strcmp(v, "format-patch"));
}

/* Python gitops.REMOTE_VERBS（完全同表） */
static int is_remote_op(const char *op) {
    static const char *set[] = {
        "ls-remote", "fetch", "fetch-pack", "remote-ext", "remote-fd",
        "push", "pull", "clone", "send-pack", "upload-pack", "receive-pack",
        "daemon", "http-backend", "submodule", "credential", "credential-cache",
        "credential-store", "request-pull", NULL
    };
    if (!op) return 0;
    for (int i = 0; set[i]; i++) if (!strcmp(op, set[i])) return 1;
    return 0;
}

int git_run(const char *const args[], const char *stdin_data, size_t stdin_len,
            buf *out, buf *errp) {
    /* 逻辑参数（UTF-8）→ 宽字符 */
    for (int i = 0; args[i]; i++) {
        if (args[i][0] != '-') {
            if (g_cmds_init && g_cmds.n) buf_putc(&g_cmds, ',');
            if (!g_cmds_init) { buf_init(&g_cmds); g_cmds_init = 1; }
            buf_puts(&g_cmds, args[i]);
            break;
        }
    }
    g_calls++;

    /* 配置在运行中被改（加一个 filter/diff driver）必须重新枚举，
     * 否则启动后改配置就能绕过 guard（TOCTOU）。 */
    {
        unsigned long long st = guard_config_stamp(g_root);
        if (st != g_guard_stamp) {
            if (g_guards) { guard_free_pairs(g_guards, g_guards_n); g_guards = NULL; }
            g_guards = guard_driver_pairs(g_root, &g_guards_n);
            g_guard_stamp = st;
        }
    }

    /* 找子命令（第一个不以 '-' 开头的参数） */
    int vi = 0;
    while (args[vi] && args[vi][0] == '-') vi++;
    const char *op = args[vi];
    int net = is_remote_op(op);
    if (!net && op && !strcmp(op, "remote")) {
        const char *rest = args[vi + 1];
        if (rest && (!strcmp(rest, "show") || !strcmp(rest, "update"))) net = 1;
    }
    if (!net && op && !strcmp(op, "archive")) {
        for (int j = vi + 1; args[j]; j++)
            if (!strncmp(args[j], "--remote", 8) || !strncmp(args[j], "--exec", 6)) { net = 1; break; }
    }

    buf cmd;
    buf_init(&cmd);
    cmdline_add(&cmd, L"git", 1);
    cmdline_add(&cmd, L"-C", 0);
    cmdline_add(&cmd, g_root, 0);
    /* 13 条固定覆盖（与 Python gitguard._scalar_overrides 对齐） */
    static const wchar_t *scalar[][2] = {
        { L"core.hooksPath", NULL },        /* NULL -> g_hooks */
        { L"core.fsmonitor", L"" },
        { L"commit.gpgsign", L"false" },
        { L"gpg.program", L"" },
        { L"tag.gpgSign", L"false" },
        { L"core.editor", L"" },
        { L"sequence.editor", L"" },
        { L"core.pager", L"cat" },
        { L"core.attributesFile", NULL },   /* NULL -> g_attrs */
        { L"credential.helper", L"" },
        { L"core.sshCommand", L"" },
        { L"uploadpack.packObjectsHook", L"" },
        { L"core.gitProxy", L"" },
    };
    for (size_t i = 0; i < sizeof(scalar) / sizeof(scalar[0]); i++) {
        if (net && (!wcscmp(scalar[i][0], L"credential.helper") ||
                    !wcscmp(scalar[i][0], L"core.sshCommand") ||
                    !wcscmp(scalar[i][0], L"uploadpack.packObjectsHook") ||
                    !wcscmp(scalar[i][0], L"core.gitProxy"))) continue;
        wchar_t pair[1024];
        const wchar_t *val = scalar[i][1] ? scalar[i][1]
                            : (i == 0 ? g_hooks : g_attrs);
        _snwprintf(pair, 1024, L"%ls=%ls", scalar[i][0], val);
        cmdline_add(&cmd, L"-c", 0);
        cmdline_add(&cmd, pair, 0);
    }
    for (size_t i = 0; i < g_guards_n; i++) {
        wchar_t *w = u82w(g_guards[i]);
        if (!w) continue;
        cmdline_add(&cmd, L"-c", 0);
        cmdline_add(&cmd, w, 0);
        free(w);
    }
    for (int i = 0; args[i]; i++) {
        wchar_t *w = u82w(args[i]);
        if (!w) { buf_free(&cmd); return -1; }
        cmdline_add(&cmd, w, 0);
        free(w);
        /* diff/show/log 这类动词必须在**子命令后面**补两个 flag：
         * textconv/命令执行面靠它们压住（-c 置空不够）。 */
        if (i == vi && is_diff_verb(op)) {
            cmdline_add(&cmd, L"--no-ext-diff", 0);
            cmdline_add(&cmd, L"--no-textconv", 0);
        }
    }

    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE out_rd = NULL, out_wr = NULL, err_rd = NULL, err_wr = NULL;
    HANDLE in_rd = NULL, in_wr = NULL;
    CreatePipe(&out_rd, &out_wr, &sa, 0);
    CreatePipe(&err_rd, &err_wr, &sa, 0);
    SetHandleInformation(out_rd, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_rd, HANDLE_FLAG_INHERIT, 0);
    if (stdin_data) {
        CreatePipe(&in_rd, &in_wr, &sa, 0);
        SetHandleInformation(in_wr, HANDLE_FLAG_INHERIT, 0);
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = out_wr;
    si.hStdError = err_wr;
    si.hStdInput = stdin_data ? in_rd : g_nul;

    wchar_t *env = build_env(1, net);
    wchar_t *cmdline = cmdline_finish(&cmd);
    if (dbg()) { fwprintf(stderr, L"[c] cmdline: %.240ls\n", cmdline);
                 fprintf(stderr, "[c] env=%p cwd=%ls hIn=%p hOut=%p hErr=%p\n",
                         (void *)env, g_root, (void *)si.hStdInput,
                         (void *)si.hStdOutput, (void *)si.hStdError); }
    /* 不要加 CREATE_NO_WINDOW：实测每次 spawn 多 ~12ms（51.5 vs 38.6ms）。
     * aigit 是控制台程序，子进程继承同一个控制台，不会弹窗。 */
    BOOL ok = CreateProcessW(NULL, cmdline, NULL, NULL, TRUE, CREATE_UNICODE_ENVIRONMENT,
                             env, g_root, &si, &pi);
    free(env);
    buf_free(&cmd);
    CloseHandle(out_wr);
    CloseHandle(err_wr);
    if (stdin_data) CloseHandle(in_rd);
    if (!ok) {
        if (dbg()) fprintf(stderr, "[c] CreateProcess failed, GetLastError=%lu\n",
                           (unsigned long)GetLastError());
        CloseHandle(out_rd);
        CloseHandle(err_rd);
        if (stdin_data) CloseHandle(in_wr);
        return -1;
    }

    reader_ctx oc = { out_rd, out }, ec = { err_rd, errp };
    HANDLE th_out = CreateThread(NULL, 0, reader_thread, &oc, 0, NULL);
    HANDLE th_err = CreateThread(NULL, 0, reader_thread, &ec, 0, NULL);

    if (stdin_data) {
        DWORD wrote = 0;
        if (stdin_len) WriteFile(in_wr, stdin_data, (DWORD)stdin_len, &wrote, NULL);
        CloseHandle(in_wr);
    }

    DWORD waitrc = WaitForSingleObject(pi.hProcess, 120000);
    if (waitrc == WAIT_TIMEOUT) TerminateProcess(pi.hProcess, 124);
    WaitForSingleObject(th_out, INFINITE);
    WaitForSingleObject(th_err, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);

    CloseHandle(th_out);
    CloseHandle(th_err);
    CloseHandle(out_rd);
    CloseHandle(err_rd);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    if (out) buf_cstr(out);
    if (errp) buf_cstr(errp);
    return (int)code;
}
