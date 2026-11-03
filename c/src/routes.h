/* routes.h —— 读端点的分派约定。
 *
 * http.c 先处理 health/status，再把剩下的 GET 依次交给这三个模块；
 * 每个函数返回 1 表示「我处理了并且已经发完响应」，0 表示「不认识这个路径」。
 * 这样三个模块可以各自一个 .c 文件并行开发，互不改对方的文件。
 */
#ifndef AIGIT_ROUTES_H
#define AIGIT_ROUTES_H

#include <winsock2.h>
#include <stddef.h>

/* 基础读：branches / log / file / diff */
int read_basic_get(const char *path, const char *query, SOCKET s);

/* 历史读：history/{file,blame,tags,reflog,stashes} */
int read_history_get(const char *path, const char *query, SOCKET s);

/* 历史清单读：history/{tags,reflog,stashes} */
int read_history_list_get(const char *path, const char *query, SOCKET s);

/* 计划面：reset/plan、discard/plan */
int read_plan_get(const char *path, const char *query, SOCKET s);

/* 历史计划：history/stash、history/plan/<mode> */
int read_histplan_get(const char *path, const char *query, SOCKET s);

/* 远程计划：remote/heads、remote/plan/<mode> */
int read_remoteplan_get(const char *path, const char *query, SOCKET s);
/* 冲突/远程读：conflict/{state,list,inspect} / remote/{capability,list,ahead-behind} */
int read_conflict_get(const char *path, const char *query, SOCKET s);

/* 写面：stage/unstage/discard/commit */
int write_basic_post(const char *path, const char *query, const char *body,
                     size_t body_len, SOCKET s);

/* 写面：checkout/branch/merge/merge-abort/reset */
int write_more_post(const char *path, const char *query, const char *body,
                    size_t body_len, SOCKET s);

/* 写面：conflict/{resolve,choose,delete,commit,abort} */
int write_conflict_post(const char *path, const char *query, const char *body,
                        size_t body_len, SOCKET s);

#endif
