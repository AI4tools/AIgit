/* bodyjson.h —— 请求体 JSON 解析（只为写端点服务，不是通用库）。
 *
 * 与 Python `json.loads` 的对齐点（只对齐写端点用得到的）：
 *   * 合法 JSON 但不是对象 → 调用方按 "请求体必须是 JSON 对象,收到 <type>" 拒；
 *   * 数字区分 int / float（`5` vs `5.0`），因为 `_bi` 只收 int；
 *   * 字符串解码 \uXXXX（含代理对），非法转义 / 非法 UTF-8 一律报错；
 *   * 深度上限 64、体积上限由调用方控制（http.c 已限 64MB）。
 *
 * 失败时 jparse 返回 NULL 并写 err（形如 "Expecting value: line 1 column 1 (char 0)"，
 * 与 Python 的文案同形，供 `请求参数不合法:%s` 复用）。
 */
#ifndef AIGIT_BODYJSON_H
#define AIGIT_BODYJSON_H

#include <stddef.h>

enum {
    BJ_NULL = 0,
    BJ_BOOL,
    BJ_INT,
    BJ_FLOAT,
    BJ_STR,
    BJ_ARR,
    BJ_OBJ
};

typedef struct jnode jnode;

jnode *jparse(const char *s, size_t n, char *err, size_t errsz);
void   jfree(jnode *v);

int         bjtype(const jnode *v);
const char *jtype_name(const jnode *v);   /* NoneType/bool/int/float/str/list/dict */

int         jis_str(const jnode *v);
const char *jstr(const jnode *v);          /* 非 str 返回 NULL */
size_t      jstrlen(const jnode *v);

int         jis_bool(const jnode *v);
int         jbool(const jnode *v);

int         jis_int(const jnode *v);
int         jis_float(const jnode *v);
long long   jint(const jnode *v);
double      jfloat(const jnode *v);

size_t      jlen(const jnode *v);          /* arr/obj 的元素数；其它 0 */
const jnode *jat(const jnode *v, size_t i);/* arr */
const jnode *jget(const jnode *v, const char *key); /* obj；没有返回 NULL */
const char  *jkey(const jnode *v, size_t i);        /* obj 第 i 个键 */
int         jhas(const jnode *v, const char *key);

#endif
