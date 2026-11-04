#include "http.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <windows.h>
#include <ws2tcpip.h>

#include "bodyjson.h"
#include "git.h"
#include "json.h"
#include "routes.h"
#include "status.h"

int g_count_on = 0;

static const char *reason_of(int code) {
    switch (code) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 401: return "Unauthorized";
    case 403: return "Forbidden";
    case 405: return "Method Not Allowed";
    case 409: return "Conflict";
    case 413: return "Payload Too Large";
    case 500: return "Internal Server Error";
    default:  return "Error";
    }
}

static void send_all(SOCKET s, const char *p, size_t n) {
    while (n) {
        int chunk = (int)(n > 65536 ? 65536 : n);
        int sent = send(s, p, chunk, 0);
        if (sent <= 0) return;
        p += sent;
        n -= (size_t)sent;
    }
}

void http_send(SOCKET s, int code, const char *ctype, const char *body, size_t len) {
    buf h;
    buf_init(&h);
    buf_printf(&h, "HTTP/1.1 %d %s\r\n", code, reason_of(code));
    buf_printf(&h, "Content-Type: %s\r\n", ctype);
    buf_printf(&h, "Content-Length: %zu\r\n", len);
    buf_puts(&h, "Cache-Control: no-store\r\n");
    buf_puts(&h, "X-Content-Type-Options: nosniff\r\n");
    if (g_count_on) {
        char tmp[64];
        snprintf(tmp, sizeof(tmp), "X-aigit-git-calls: %d\r\n", git_calls());
        buf_puts(&h, tmp);
        const char *cmds = git_cmds();
        if (cmds && *cmds) {
            buf_puts(&h, "X-aigit-git-cmds: ");
            buf_puts(&h, cmds);              /* 截断口径与 Python 版一致：>200 截到 197+... */
            if (strlen(cmds) > 200) {
                h.n -= strlen(cmds);
                h.p[h.n] = 0;
                buf_append(&h, cmds, 197);
                buf_puts(&h, "...");
            }
            buf_puts(&h, "\r\n");
        }
    }
    buf_puts(&h, "Connection: close\r\n\r\n");
    send_all(s, h.p, h.n);
    if (len) send_all(s, body, len);
    buf_free(&h);
}

void http_send_json(SOCKET s, int code, const char *body, size_t len) {
    http_send(s, code, "application/json; charset=utf-8", body, len);
}

/* ---------------------------------------------------------------- 静态白名单
 * 与 Python 版一致：只发 index.html / conflicts-demo.html / 三个 js，其余 404。
 * 不拼路径、不扫目录（白名单本身就是防穿越的那道闸）。 */
static wchar_t g_web[32768];

typedef struct { char *url, *ctype, *file; } sentry;
static sentry *g_static = NULL;
static size_t g_static_n = 0;

static void static_add(const char *url, const char *ctype, const char *file) {
    if (!url || !ctype || !file) return;
    if (url[0] != '/' || strstr(url, "..") || strchr(url, '\\')) return;
    if (!*file || strchr(file, '/') || strchr(file, '\\') ||
        strstr(file, "..") || file[0] == '.') return;
    if (strchr(ctype, '\r') || strchr(ctype, '\n')) return;
    g_static = xrealloc(g_static, (g_static_n + 1) * sizeof(*g_static));
    g_static[g_static_n].url = xstrdup(url);
    g_static[g_static_n].ctype = xstrdup(ctype);
    g_static[g_static_n].file = xstrdup(file);
    g_static_n++;
}

static void static_builtin(void) {
    static_add("/", "text/html; charset=utf-8", "index.html");
    static_add("/index.html", "text/html; charset=utf-8", "index.html");
    static_add("/conflicts-demo.html", "text/html; charset=utf-8", "conflicts-demo.html");
    static_add("/virtuallist.js", "application/javascript; charset=utf-8", "virtuallist.js");
    static_add("/history-panel.js", "application/javascript; charset=utf-8", "history-panel.js");
    static_add("/conflicts-panel.js", "application/javascript; charset=utf-8", "conflicts-panel.js");
}

static void static_free(void) {
    for (size_t i = 0; i < g_static_n; i++) {
        free(g_static[i].url); free(g_static[i].ctype); free(g_static[i].file);
    }
    free(g_static);
    g_static = NULL;
    g_static_n = 0;
}

/* 读 web/.aigit-static：每行 `url<TAB>content-type<TAB>filename`。
 * 文件不存在/全部非法时回退内置表。加静态资源只改清单，不改 C。 */
static void static_load(void) {
    static_free();
    if (!g_web[0]) { static_builtin(); return; }
    wchar_t p[32768];
    _snwprintf(p, 32768, L"%ls\\.aigit-static", g_web);
    FILE *f = _wfopen(p, L"rb");
    if (!f) { static_builtin(); return; }
    char line[2048];
    while (fgets(line, sizeof(line), f)) {
        size_t n = strlen(line);
        while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
        if (!n || line[0] == '#') continue;
        char *t1 = strchr(line, '\t');
        if (!t1) continue;
        *t1 = 0;
        char *t2 = strchr(t1 + 1, '\t');
        if (!t2) continue;
        *t2 = 0;
        static_add(line, t1 + 1, t2 + 1);
    }
    fclose(f);
    if (!g_static_n) static_builtin();
}

void http_set_web(const wchar_t *dir) {
    g_web[0] = 0;
    if (dir && *dir) {
        _snwprintf(g_web, 32768, L"%ls", dir);
    }
    static_load();
}

static void send_file(SOCKET s, const wchar_t *path, const char *ctype) {
    FILE *f = _wfopen(path, L"rb");
    if (!f) {
        if (getenv("AIGIT_C_DEBUG")) fwprintf(stderr, L"[c] fopen fail: %ls errno=%d\n", path, errno);
        http_send_err(s, 500, "服务端内部错误,详情见服务端日志", "internal_error");
        return;
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        if (getenv("AIGIT_C_DEBUG")) fwprintf(stderr, L"[c] fseek fail: %ls\n", path);
        fclose(f); http_send_err(s, 500, "服务端内部错误,详情见服务端日志", "internal_error"); return;
    }
    long sz = ftell(f);
    if (sz < 0 || sz > (long)(64 * 1024 * 1024)) {
        if (getenv("AIGIT_C_DEBUG")) fwprintf(stderr, L"[c] ftell=%ld: %ls\n", sz, path);
        fclose(f); http_send_err(s, 500, "服务端内部错误,详情见服务端日志", "internal_error"); return;
    }
    rewind(f);
    char *buf = xmalloc((size_t)sz + 1);
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) {
        free(buf);
        http_send_err(s, 500, "服务端内部错误,详情见服务端日志", "internal_error");
        return;
    }
    http_send(s, 200, ctype, buf, (size_t)sz);
    free(buf);
}

static int serve_static(SOCKET s, const char *path) {
    if (!g_web[0]) return 0;
    for (size_t i = 0; i < g_static_n; i++) {
        if (strcmp(g_static[i].url, path)) continue;
        wchar_t full[32768];
        wchar_t *fw = u82w(g_static[i].file);
        if (!fw) {
            http_send_err(s, 500, "服务端内部错误,详情见服务端日志", "internal_error");
            return 1;
        }
        _snwprintf(full, 32768, L"%ls\\%ls", g_web, fw);
        free(fw);
        send_file(s, full, g_static[i].ctype);
        return 1;
    }
    return 0;
}

void http_send_err(SOCKET s, int code, const char *msg, const char *err_code) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":false,\"error\":");
    json_str(&o, msg ? msg : "");
    if (err_code && *err_code) {
        buf_puts(&o, ",\"code\":");
        json_str(&o, err_code);
    }
    buf_putc(&o, '}');
    http_send_json(s, code, buf_cstr(&o), o.n);
    buf_free(&o);
}

/* ---------------------------------------------------------------- 路由 */

static int path_eq(const char *path, const char *want) {
    /* path 已去掉 query；精确匹配 */
    return !strcmp(path, want);
}

/* 仓操作全局串行：Python 的 Repo 有 per-repo RLock，status/log 等读方法也拿锁
 * （git status 会写索引扩展），所以并发读 + 写一样会撞 index.lock。
 * C 按请求粒度串行化；HTTP 解析、JSON 渲染仍在锁外。 */
static CRITICAL_SECTION g_post_cs;
static INIT_ONCE g_post_once = INIT_ONCE_STATIC_INIT;

static BOOL CALLBACK post_init_cb(PINIT_ONCE once, PVOID p, PVOID *c) {
    (void)once; (void)p; (void)c;
    InitializeCriticalSection(&g_post_cs);
    return TRUE;
}

static void route_locked(SOCKET s, const char *method, const char *path,
                         const char *query, const char *body, size_t body_len);

/* /api/health 不碰 git（只读启动时确定的 root 与 .git 是否存在），
 * 所以不进全局锁 —— 否则健康检查会和一张静态页一起被串行化。 */
static void send_health(SOCKET s) {
    buf o;
    buf_init(&o);
    buf_puts(&o, "{\"ok\":true,\"root\":");
    char *root = w2u8(git_root());
    json_str(&o, root ? root : "");
    free(root);
    DWORD attr = GetFileAttributesW(git_root());
    (void)attr;
    wchar_t dotgit[MAX_PATH];
    _snwprintf(dotgit, MAX_PATH, L"%ls\\.git", git_root());
    DWORD a2 = GetFileAttributesW(dotgit);
    int is_repo = (a2 != INVALID_FILE_ATTRIBUTES);
    buf_printf(&o, ",\"isRepo\":%s}", is_repo ? "true" : "false");
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
}

static void route(SOCKET s, const char *method, const char *path,
                  const char *query, const char *body, size_t body_len) {
    InitOnceExecuteOnce(&g_post_once, post_init_cb, NULL, NULL);
    /* 静态资源与 health 完全不碰 git：让它们走锁外快路径。
     * 之前连一张 100KB 的 index.html 都要排队，16 并发下吞吐被全局锁压死。 */
    if (!strcmp(method, "GET")) {
        if (serve_static(s, path)) return;
        if (path_eq(path, "/api/health")) { send_health(s); return; }
    }
    EnterCriticalSection(&g_post_cs);
    route_locked(s, method, path, query, body, body_len);
    LeaveCriticalSection(&g_post_cs);
}

static void route_post(SOCKET s, const char *path, const char *query,
                       const char *body, size_t body_len) {
    /* 读端点里的 POST（C 的超集）：批量 diff */
    if (read_basic_post(path, body, body_len, s)) return;
    /* 写端点 */
    if (write_basic_post(path, query, body, body_len, s)) return;
    if (write_more_post(path, query, body, body_len, s)) return;
    if (write_conflict_post(path, query, body, body_len, s)) return;
    if (write_history_post(path, query, body, body_len, s)) return;
    if (write_refs_post(path, query, body, body_len, s)) return;
    if (write_stash_post(path, query, body, body_len, s)) return;
    if (write_remote_post(path, query, body, body_len, s)) return;
    if (gen_post(path, query, body, body_len, s)) return;
    /* Python 的 do_POST 先 _body() 再分派：未知路径 + 坏 JSON 也是 400，
     * 不是 404。这里照同序补上（空 body 视作 {}）。 */
    {
        char jerr[256];
        jnode *b = jparse(body_len ? body : "{}", body_len ? body_len : 2,
                          jerr, sizeof(jerr));
        if (!b) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "请求参数不合法:");
            buf_puts(&m, jerr);
            http_send_err(s, 400, buf_cstr(&m), NULL);
            buf_free(&m);
            return;
        }
        if (bjtype(b) != BJ_OBJ) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "请求体必须是 JSON 对象,收到 ");
            buf_puts(&m, jtype_name(b));
            http_send_err(s, 400, buf_cstr(&m), NULL);
            buf_free(&m);
            jfree(b);
            return;
        }
        jfree(b);
    }
    /* 读端点只认 GET（与 Python 版一致：POST 到只读路径 → 404，不是 405） */
    http_send_err(s, 404, "没有这个接口", NULL);
}

static void route_locked(SOCKET s, const char *method, const char *path, const char *query,
                  const char *body, size_t body_len) {
    if (!strcmp(method, "GET")) {
        if (serve_static(s, path)) return;
        if (path_eq(path, "/api/health")) { send_health(s); return; }

        if (path_eq(path, "/api/status")) {
            status_t st;
            char *err = NULL;
            if (status_compute(&st, &err) != 0) {
                http_send_err(s, 500, err ? err : "git status 失败", "internal_error");
                free(err);
                return;
            }
            buf o;
            buf_init(&o);
            char *root = w2u8(git_root());
            status_json(&o, &st, root ? root : "");
            free(root);
            status_free(&st);
            http_send_json(s, 200, buf_cstr(&o), o.n);
            buf_free(&o);
            return;
        }
        if (read_basic_get(path, query, s)) return;
        if (read_history_get(path, query, s)) return;
        if (read_history_list_get(path, query, s)) return;
        if (read_conflict_get(path, query, s)) return;
        if (read_plan_get(path, query, s)) return;
        if (read_histplan_get(path, query, s)) return;
        if (read_remoteplan_get(path, query, s)) return;
        http_send_err(s, 404, "没有这个接口", NULL);
        return;
    }
    if (!strcmp(method, "POST")) {
        route_post(s, path, query, body, body_len);
        return;
    }
    /* PUT / DELETE / PATCH / 其它：405 + Allow（与 Python 版契约一致） */
    {
        buf o;
        buf_init(&o);
        buf_puts(&o, "{\"ok\":false,\"error\":");
        buf_printf(&o, "\"不支持的 HTTP 方法:%s（本服务只有 GET / POST）\"", method);
        buf_puts(&o, ",\"code\":\"method_not_allowed\"}");
        buf h;
        buf_init(&h);
        buf_printf(&h, "HTTP/1.1 405 Method Not Allowed\r\n");
        buf_puts(&h, "Content-Type: application/json; charset=utf-8\r\n");
        buf_printf(&h, "Content-Length: %zu\r\n", o.n);
        buf_puts(&h, "Cache-Control: no-store\r\n");
        buf_puts(&h, "Allow: GET, POST\r\n");
        buf_puts(&h, "Connection: close\r\n\r\n");
        send_all(s, h.p, h.n);
        send_all(s, o.p, o.n);
        buf_free(&h);
        buf_free(&o);
    }
}

/* 在请求头里找 Content-Length（不区分大小写）。找不到返回 -1。 */
static long long header_content_length(const char *h, size_t n) {
    static const char key[] = "content-length:";
    for (size_t i = 0; i + sizeof(key) - 1 < n; i++) {
        size_t k = 0;
        while (k < sizeof(key) - 1 && tolower((unsigned char)h[i + k]) == key[k]) k++;
        if (k == sizeof(key) - 1) {
            const char *p = h + i + k;
            while (*p == ' ' || *p == '\t') p++;
            char *end = NULL;
            long long v = strtoll(p, &end, 10);
            return end == p ? -1 : v;
        }
    }
    return -1;
}


/* 请求头里是否有 Transfer-Encoding: chunked（大小写不敏感）。 */
static int header_has_chunked(const char *h, size_t n) {
    static const char key[] = "transfer-encoding:";
    for (size_t i = 0; i + sizeof(key) - 1 < n; i++) {
        size_t k = 0;
        while (k < sizeof(key) - 1 && tolower((unsigned char)h[i + k]) == key[k]) k++;
        if (k != sizeof(key) - 1) continue;
        const char *p = h + i + k;
        const char *end = h + n;
        while (p < end && *p != '\r') {
            if ((end - p) >= 7 && !_strnicmp(p, "chunked", 7)) return 1;
            p++;
        }
    }
    return 0;
}

/* 保证 req 里从 pos 起有一整行；返回行的结束 \n 位置（指向 \n）。 */
static char *chunk_line(SOCKET s, buf *req, size_t pos) {
    char tmp[8192];
    for (;;) {
        char *nl = memchr(req->p + pos, '\n', req->n - pos);
        if (nl) return nl;
        if (req->n > (64u << 20)) return NULL;
        int got = recv(s, tmp, sizeof(tmp), 0);
        if (got <= 0) return NULL;
        buf_append(req, tmp, (size_t)got);
    }
}

/* 解码 Transfer-Encoding: chunked。成功 1，失败 0。
 * 数据可能已经有一部分在 req[header_end..] 里，缺的再 recv。 */
static int read_chunked(SOCKET s, buf *req, size_t start, buf *body) {
    size_t pos = start;
    for (;;) {
        char *nl = chunk_line(s, req, pos);
        if (!nl) return 0;
        size_t llen = (size_t)(nl - (req->p + pos));
        if (llen && req->p[pos + llen - 1] == '\r') llen--;
        unsigned long long sz = 0;
        int digits = 0;
        for (size_t i = 0; i < llen; i++) {
            char c = req->p[pos + i];
            if (c == ';' || c == ' ' || c == '\t') break;
            int v = (c >= '0' && c <= '9') ? c - '0'
                  : (c >= 'a' && c <= 'f') ? c - 'a' + 10
                  : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
            if (v < 0) return 0;
            sz = sz * 16 + (unsigned)v;
            digits = 1;
        }
        if (!digits) return 0;
        pos = (size_t)(nl - req->p) + 1;

        if (sz == 0) {
            /* 吃掉 trailer，直到空行 */
            for (;;) {
                char *t = chunk_line(s, req, pos);
                if (!t) return 0;
                size_t tl = (size_t)(t - (req->p + pos));
                if (tl && req->p[pos + tl - 1] == '\r') tl--;
                pos = (size_t)(t - req->p) + 1;
                if (tl == 0) return 1;
            }
        }

        if (body->n + sz > (64u << 20)) return 0;
        while (req->n - pos < (size_t)sz + 2) {
            char tmp[8192];
            int got = recv(s, tmp, sizeof(tmp), 0);
            if (got <= 0) return 0;
            buf_append(req, tmp, (size_t)got);
        }
        buf_append(body, req->p + pos, (size_t)sz);
        pos += (size_t)sz;
        if (req->p[pos] != '\r' || req->p[pos + 1] != '\n') return 0;
        pos += 2;
    }
}


/* ---------------------------------------------------------------- 本机同源闸 */

/* 取一个请求头的值（大小写不敏感，只认行首）。1 = 找到。 */
static int header_value(const char *h, size_t n, const char *key, char *out,
                        size_t outsz) {
    size_t klen = strlen(key);
    for (size_t i = 0; i + klen < n; i++) {
        if (i && h[i - 1] != '\n') continue;
        size_t k = 0;
        while (k < klen && tolower((unsigned char)h[i + k]) == key[k]) k++;
        if (k != klen) continue;
        size_t j = i + klen;
        while (j < n && (h[j] == ' ' || h[j] == '\t')) j++;
        size_t e = j;
        while (e < n && h[e] != '\r' && h[e] != '\n') e++;
        size_t len = e - j;
        if (len >= outsz) len = outsz - 1;
        memcpy(out, h + j, len);
        out[len] = 0;
        return 1;
    }
    return 0;
}

/* host[:port] 是否是回环地址。 */
static int host_is_loopback(const char *hp) {
    if (hp[0] == '[') return !_strnicmp(hp, "[::1]", 5);
    if (!_strnicmp(hp, "127.0.0.1", 9)) return hp[9] == 0 || hp[9] == ':';
    if (!_strnicmp(hp, "localhost", 9)) return hp[9] == 0 || hp[9] == ':';
    if (!strncmp(hp, "::1", 3)) return hp[3] == 0 || hp[3] == ':';
    return 0;
}

/* 只接受本机同源请求。
 *
 * 不上这道闸，任何网页都能对 127.0.0.1 发 `Content-Type: text/plain` 的
 * POST（简单请求不触发预检），body 里直接写 JSON + `"confirmed":true`，
 * 就能触发 discard / reset --hard —— 浏览器同源策略在这里帮不上忙。
 * `Host` 还挡住 DNS rebinding（攻击者域名解析到 127.0.0.1 后 Host 仍是它）。
 *
 * 非浏览器客户端（curl、判据脚本）不带 Origin，只看 Host。 */
static int request_is_local(const char *h, size_t n) {
    char host[256];
    if (!header_value(h, n, "host:", host, sizeof(host))) return 0;
    if (!host_is_loopback(host)) return 0;

    char org[512];
    if (header_value(h, n, "origin:", org, sizeof(org))) {
        const char *q = org;
        if (!strncmp(q, "http://", 7)) q += 7;
        else if (!strncmp(q, "https://", 8)) q += 8;
        else return 0;              /* "null"（file:// 页面）等一律拒 */
        if (!host_is_loopback(q)) return 0;
    }
    return 1;
}

static void handle_conn(SOCKET s) {
    /* 半包/慢速连接不能永久占住一个线程：头部 10s 读不到就放弃。 */
    DWORD rcv_to = 10000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&rcv_to, sizeof(rcv_to));
    buf req;
    buf_init(&req);
    char tmp[8192];
    size_t header_end = 0;
    while (req.n < (1u << 20)) {
        int got = recv(s, tmp, sizeof(tmp), 0);
        if (got <= 0) break;
        buf_append(&req, tmp, (size_t)got);
        /* 找 \r\n\r\n */
        if (req.n >= 4) {
            for (size_t i = 3; i < req.n; i++) {
                if (req.p[i - 3] == '\r' && req.p[i - 2] == '\n' &&
                    req.p[i - 1] == '\r' && req.p[i] == '\n') {
                    header_end = i + 1;
                    break;
                }
            }
        }
        if (header_end) break;
    }
    if (!header_end) {
        http_send_err(s, 400, "请求头不完整", "bad_request");
        buf_free(&req);
        return;
    }
    /* 第一行：METHOD SP TARGET SP VERSION
     * 目标串不再拷进固定 2048 缓冲（那会把长路径静默截断，Python 版没有这个上限）。 */
    char method[16] = {0};
    char *target = NULL;
    char *line_end = strstr(req.p, "\r\n");
    if (line_end) {
        *line_end = 0;
        char *sp1 = strchr(req.p, ' ');
        if (sp1) {
            size_t mlen = (size_t)(sp1 - req.p);
            if (mlen >= sizeof(method)) mlen = sizeof(method) - 1;
            memcpy(method, req.p, mlen);
            method[mlen] = 0;
            target = sp1 + 1;
            while (*target == ' ') target++;
            char *sp2 = strchr(target, ' ');
            if (sp2) *sp2 = 0;
        }
    }
    if (!method[0] || !target || !*target) {
        http_send_err(s, 400, "请求行不完整", "bad_request");
        buf_free(&req);
        return;
    }

    /* 本机同源闸（见 request_is_local 的注释）。 */
    if (!request_is_local(req.p, header_end)) {
        http_send_err(s, 403, "只接受本机同源请求(Host/Origin 校验失败)",
                      "forbidden");
        buf_free(&req);
        return;
    }

    /* 先把请求体读进内存再响应。写端点要解析 body；只读端点也要读走，
     * 否则端点回 404 后直接 closesocket，对端还有未读数据，Windows 会发 RST ——
     * 客户端看到的是 ConnectionResetError，而不是那个 404（实测确实踩到）。 */
    buf body;
    buf_init(&body);
    if (header_has_chunked(req.p, header_end)) {
        if (!read_chunked(s, &req, header_end, &body)) {
            http_send_err(s, 400, "请求体不合法", "bad_request");
            buf_free(&body);
            buf_free(&req);
            return;
        }
    } else {
        long long clen = header_content_length(req.p, header_end);
        if (clen > 0) {
            size_t need = (size_t)clen;
            if (need > (size_t)64 * 1024 * 1024) {
                http_send_err(s, 400, "请求体过大", "bad_request");
                buf_free(&body);
                buf_free(&req);
                return;
            }
            size_t have = req.n > header_end ? req.n - header_end : 0;
            if (have > need) have = need;
            if (have) buf_append(&body, req.p + header_end, have);
            DWORD tv = 2000;
            setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));
            while (body.n < need) {
                int got = recv(s, tmp, sizeof(tmp), 0);
                if (got <= 0) break;
                size_t take = (size_t)got;
                if (take > need - body.n) take = need - body.n;
                buf_append(&body, tmp, take);
            }
        }
    }

    /* 去掉 query */
    char *qm = strchr(target, '?');
    const char *query = "";
    if (qm) { *qm = 0; query = qm + 1; }

    git_count_reset();
    route(s, method, target, query, body.p ? body.p : "", body.n);
    buf_free(&body);
    buf_free(&req);
}

static DWORD WINAPI conn_thread(LPVOID p) {
    SOCKET s = (SOCKET)(uintptr_t)p;
    handle_conn(s);
    closesocket(s);
    return 0;
}

static int g_open_browser = 0;

void http_set_open(int on) { g_open_browser = on ? 1 : 0; }

int http_serve(unsigned short port) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        fputs("aigit: WSAStartup failed\n", stderr);
        return 1;
    }
    SOCKET srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == INVALID_SOCKET) {
        fputs("aigit: socket failed\n", stderr);
        return 1;
    }
    BOOL one = TRUE;
    setsockopt(srv, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *)&one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    /* 端口被占用时自动往后试 20 个：用户双击启动不该因为 8899 被别的程序
     * 占着就整个失败。显式 --port 也一样（打印出来的就是真实端口）。 */
    unsigned short chosen = port;
    for (int attempt = 0; ; attempt++) {
        addr.sin_port = htons(chosen);
        if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) == 0) break;
        int err = WSAGetLastError();
        if (err != WSAEADDRINUSE || attempt >= 19 || chosen >= 65535) {
            fprintf(stderr, "aigit: bind 127.0.0.1:%hu failed (%d)\n", chosen, err);
            closesocket(srv);
            return 1;
        }
        fprintf(stderr, "aigit: 端口 %hu 被占用,改用 %hu\n", chosen,
                (unsigned short)(chosen + 1));
        chosen++;
    }
    if (listen(srv, 64) == SOCKET_ERROR) {
        fputs("aigit: listen failed\n", stderr);
        closesocket(srv);
        return 1;
    }
    printf("aigit-c listening on http://127.0.0.1:%hu\n", chosen);
    fflush(stdout);
    if (g_open_browser) {
        wchar_t url[160];
        _snwprintf(url, 160, L"http://127.0.0.1:%hu/", chosen);
        ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
    }
    for (;;) {
        SOCKET c = accept(srv, NULL, NULL);
        if (c == INVALID_SOCKET) continue;
        HANDLE th = CreateThread(NULL, 0, conn_thread, (LPVOID)(uintptr_t)c, 0, NULL);
        if (th) CloseHandle(th);
        else closesocket(c);
    }
}
