/* writeutil.h —— 写端点共用：请求体取值 + 成功响应（带/不带 status）。
 *
 * 与 Python server.py 的对位：
 *   * `_body()`：空 body 当 {}；非法 JSON → 400 `请求参数不合法:<json 错误>`；
 *     合法但不是对象 → 400 `请求体必须是 JSON 对象,收到 <type>`。
 *   * `_bi()` / `_bb()` / `as_file_list()`：类型错误一律 400，文案逐字对齐。
 *   * `_ok_with_status()`：`{"ok":true, <extra>, <status 字段>}`；
 *     body 里显式 `"status": false` 时改成 `{"ok":true, <extra>, "statusOmitted":true}`；
 *     status 取不到时走 E3 兜底（仍是 200，带 statusError）。
 */
#ifndef AIGIT_WRITEUTIL_H
#define AIGIT_WRITEUTIL_H

#include <stddef.h>
#include <winsock2.h>

#include "bodyjson.h"
#include "util.h"

/* 解析请求体。成功返回对象节点（调用方 jfree）；失败已回 400 并返回 NULL。 */
jnode *wbody_parse(SOCKET s, const char *body, size_t len);

/* Python repr 的近似（错误文案里的 %r 用）。写进 out（追加）。 */
void w_repr(const jnode *v, buf *out);

/* _bi：缺省用 dflt；非整数（含 bool）回 400。成功返回 1，失败返回 0（已响应）。 */
int wbody_int(SOCKET s, const jnode *b, const char *key, long long dflt, long long *out);

/* _bb：只认真 bool；缺省用 dflt。成功返回 1，失败返回 0（已响应）。 */
int wbody_bool(SOCKET s, const jnode *b, const char *key, int dflt, int *out);

/* as_file_list：缺省/null → 空数组；不是数组或元素不是字符串 → 400。
 * 成功返回 1（*out 为 malloc 的 char**，*n 为长度，调用方 free_strv）；失败 0。 */
int wbody_files(SOCKET s, const jnode *b, char ***out, size_t *n);
void wbody_free_files(char **v, size_t n);

/* 成功的写响应。extra_json 是已经渲染好的 JSON 片段（不含前后逗号），可为 NULL。
 * 例如 commit 传 "\"output\":\"...\""。 */
void wsend_ok(SOCKET s, const jnode *b, const char *extra_json);

/* body 里 `"status": false`（只认字面 false） */
int wbody_status_omitted(const jnode *b);

#endif
