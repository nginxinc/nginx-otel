#include <ngx_config.h>
#include <ngx_core.h>

extern ngx_module_t gHttpModule;

ngx_module_t* ngx_modules[] = {
    &gHttpModule,
    NULL
};

char* ngx_module_names[] = {
    "ngx_http_otel_module",
    NULL
};
