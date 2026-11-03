/* params.h —— query 串解析（percent-decode 到 UTF-8）。
 *
 * 与 Python 版的对齐点：
 *   * 缺参数 / 参数非整数 → 400（由调用方决定文案），**不静默取默认**；
 *   * `+` 在 query 里是空格（urllib 同语义）；
 *   * %XX 非法 → 保留原字符而不是崩。
 */
#ifndef AIGIT_PARAMS_H
#define AIGIT_PARAMS_H

#include <stddef.h>

/* 取 query 里 key 的值，percent-decode 后写进 out（UTF-8，NUL 结尾）。
 * 返回 1=找到，0=没有，-1=超出 outsz，-2=值里含内嵌 NUL（拒绝，绝不截断）。 */
int q_get(const char *query, const char *key, char *out, size_t outsz);

/* 长度感知版：解码结果可以含内嵌 NUL；*out 为 malloc（调用方 free）。
 * 返回 1=找到，0=没有。 */
int q_get_n(const char *query, const char *key, char **out, size_t *outlen);

/* 取整数参数。返回 1=找到且合法；0=没有（*out 保持 dflt）；
 * -1=找到了但不是整数（调用方应回 400）。 */
int q_int(const char *query, const char *key, long long dflt, long long *out);

/* 取布尔参数：只认 "1"/"true"/"yes"（与 Python 版同表）。缺省返回 dflt。 */
int q_bool(const char *query, const char *key, int dflt);

#endif
