#include "http.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
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
    case 405: return "Method Not Allowed";
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

static void route(SOCKET s, const char *method, const char *path, const char *query,
                  const char *body, size_t body_len) {
    if (!strcmp(method, "GET")) {
        if (path_eq(path, "/api/health")) {
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
            return;
        }
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
        /* 写端点 */
        if (write_basic_post(path, query, body, body_len, s)) return;
        if (write_more_post(path, query, body, body_len, s)) return;
        if (write_conflict_post(path, query, body, body_len, s)) return;
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

static void handle_conn(SOCKET s) {
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

    /* 先把请求体读进内存再响应。写端点要解析 body；只读端点也要读走，
     * 否则端点回 404 后直接 closesocket，对端还有未读数据，Windows 会发 RST ——
     * 客户端看到的是 ConnectionResetError，而不是那个 404（实测确实踩到）。 */
    buf body;
    buf_init(&body);
    {
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
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        fprintf(stderr, "aigit: bind 127.0.0.1:%hu failed (%d)\n", port, WSAGetLastError());
        return 1;
    }
    if (listen(srv, 64) == SOCKET_ERROR) {
        fputs("aigit: listen failed\n", stderr);
        return 1;
    }
    printf("aigit-c listening on http://127.0.0.1:%hu\n", port);
    fflush(stdout);
    for (;;) {
        SOCKET c = accept(srv, NULL, NULL);
        if (c == INVALID_SOCKET) continue;
        HANDLE th = CreateThread(NULL, 0, conn_thread, (LPVOID)(uintptr_t)c, 0, NULL);
        if (th) CloseHandle(th);
        else closesocket(c);
    }
}
