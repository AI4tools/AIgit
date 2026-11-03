#include "routes.h"

/* M2 占位：未实现前一律不认领。 */
int write_basic_post(const char *path, const char *query, const char *body,
               size_t body_len, SOCKET s) {
    (void)path; (void)query; (void)body; (void)body_len; (void)s;
    return 0;
}
