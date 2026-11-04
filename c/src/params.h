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
#include "util.h"

/* 取 query 里 key 的值，percent-decode 后写进 out（UTF-8，NUL 结尾）。
 * 返回 1=找到，0=没有，-1=超出 outsz，-2=值里含内嵌 NUL（拒绝，绝不截断）。 */
int q_get(const char *query, const char *key, char *out, size_t outsz);

/* 长度感知版：解码结果可以含内嵌 NUL；*out 为 malloc（调用方 free）。
 * 返回 1=找到，0=没有。 */
int q_get_n(const char *query, const char *key, char **out, size_t *outlen);

/* 与 q_get_n 相同，但**空值视为没找到**（parse_qs keep_blank_values=False）。 */
int q_get_nonempty(const char *query, const char *key, char **out, size_t *outlen);

/* 取整数参数。返回 1=找到且合法；0=没有（*out 保持 dflt）；
 * -1=找到了但不是整数（调用方应回 400）。 */
int q_int(const char *query, const char *key, long long dflt, long long *out);

/* 取布尔参数：只认 "1"/"true"/"yes"（与 Python 版同表）。缺省返回 dflt。 */
int q_bool(const char *query, const char *key, int dflt);

/* Python int(str) 的 ASCII 子集：前后 ASCII 空白、可选 +/-
 * 数字间允许单个下划线；超过 2^126 饱和。返回 1=成功。 */
int py_int128_parse(const char *s, __int128 *out);
int py_int_parse(const char *s, long long *out);

/* Python `str(int(s))`：把任意长度的十进制文字规范化（去前导零、-0 -> 0、
 * 支持 Unicode Nd 与数字间下划线）。解析失败返回 NULL。
 * C 内部只算到 2^126，需要**原样回显**用户给的大整数时用它。 */
char *py_int_canon_n(const char *s, size_t n);
char *py_int_canon(const char *s);

/* ---- Unicode 语义的共享基础件（各模块不再各写一套）---- */

/* 解一个 UTF-8 码点。成功返回消耗的字节数（1..4），非法序列返回 0。 */
int py_utf8_one(const unsigned char *s, size_t n, unsigned *cp);

/* Python `str.isspace()`：	
、-、空格、U+0085、U+00A0、
 * U+1680、U+2000-200A、U+2028、U+2029、U+202F、U+205F、U+3000。 */
int py_is_space_cp(unsigned cp);

/* Python re 的 `\w`（str 模式）：str.isalnum() 或 `_`。733 段区间表。 */
int py_is_word(unsigned cp);

/* Python `str.strip()`：按码点去前后 Unicode 空白。返回 malloc 的串。 */
char *py_strip_dup(const char *s);
/* 长度感知版（内容可含内嵌 NUL）；*outn 回传长度。 */
char *py_strip_dup_n(const char *s, size_t n, size_t *outn);
/* 就地版：把尾部空白截断，返回跳过前导空白后的指针（返回值可能 != s）。 */
char *py_strip(char *s);

/* 串里是否有空白码点或 <0x20 的控制字符（ref 名校验用）。 */
int py_has_space_or_ctrl(const char *s, size_t n);

/* C 的 ASCII 空白（git 输出与 CLI 参数用；不是 Python 的 isspace）。 */
int py_is_ascii_space(int c);

/* UTF-8 步进：非法字节前进 1 并按 0 返回（旧的 next_cp 语义）。 */
int py_utf8_step(const unsigned char *s, size_t n, size_t *i, unsigned *cp);

/* Python `str.isprintable()`。不可打印的用 701 段区间表表示补集。 */
int py_is_printable(unsigned cp);

/* Python `str(v)` 里的 repr：引号选择（含 ' 且不含 " -> 用 "）、
 * 转义 \\ \' \n \r \t，不可打印码点按 \xNN / \uNNNN / \UNNNNNNNN。
 * 结果写进 out（不追加结尾 NUL 以外的任何东西）。 */
void py_repr_str(buf *out, const char *s, size_t n);

/* 上面那版的 malloc 包装（错误文案 `%r` 用），支持内嵌 NUL。 */
char *py_repr_n(const char *s, size_t n);

#endif
