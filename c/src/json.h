/* json.h —— 自写 JSON：写（含转义）与严格的读（只服务 POST body）。
 *
 * 为什么不引第三方：与 Python 版同一个约束（零依赖，单文件分发）。
 * 解析器只接受标准 JSON；重复键后者覆盖前者；深度上限 64；多余尾字符 -> 错。
 */
#ifndef AIGIT_JSON_H
#define AIGIT_JSON_H

#include <stddef.h>
#include "util.h"

void json_str(buf *o, const char *s);          /* 写一个带引号的字符串 */
void json_kv_str(buf *o, const char *k, const char *v, int comma);
void json_kv_int(buf *o, const char *k, long long v, int comma);
void json_kv_bool(buf *o, const char *k, int v, int comma);

typedef enum { J_NULL = 0, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ } jtype;

typedef struct jval {
    jtype t;
    int   b;            /* J_BOOL */
    double num;         /* J_NUM */
    char *str;          /* J_STR / J_NULL(未用) */
    struct jval **items;/* J_ARR / J_OBJ 的值 */
    char **keys;        /* J_OBJ 的键 */
    size_t n;           /* J_ARR / J_OBJ 的元素数 */
} jval;

/* 成功返回树，失败返回 NULL 并（可选）写 *err（静态字符串）。 */
jval *json_parse(const char *s, size_t len, const char **err);
void  jval_free(jval *v);
jval *json_get(const jval *obj, const char *key);
/* 便捷取值：类型不符返回默认值 */
const char *json_str_of(const jval *obj, const char *key, const char *dflt);
long long   json_int_of(const jval *obj, const char *key, long long dflt);
int         json_bool_of(const jval *obj, const char *key, int dflt);

#endif
