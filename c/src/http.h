/* http.h —— 极简 HTTP/1.1（只支持 Content-Length；每个连接一线程；响应后关闭）。 */
#ifndef AIGIT_HTTP_H
#define AIGIT_HTTP_H

#include <stddef.h>
#include <winsock2.h>
#include "util.h"

/* 发一个响应（自动带 Content-Length / Cache-Control / 计数头 / Connection: close） */
void http_send(SOCKET s, int code, const char *ctype, const char *body, size_t len);
void http_send_json(SOCKET s, int code, const char *body, size_t len);
void http_send_err(SOCKET s, int code, const char *msg, const char *err_code);

/* 由 main 设置：静态白名单的 web 目录（空 = 不服务静态页） */
void http_set_web(const wchar_t *dir);

/* 阻塞式服务循环 */
int http_serve(unsigned short port);

/* 启动后自动打开浏览器（--open）。 */
void http_set_open(int on);

/* 由 main 设置：请求是否带计数头 */
extern int g_count_on;

#endif
