extern "C" {
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
}

#include "str_view.hpp"
#include "trace_context.hpp"
#include "batch_exporter.hpp"

extern ngx_module_t gHttpModule;

namespace {

struct MainConf {
    ngx_str_t endpoint;
    ngx_msec_t interval;
    size_t batchSize;
    size_t batchCount;

    ngx_str_t serviceName;
};

struct LocationConf {
    ngx_http_complex_value_t* trace;
};

char* setExporter(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);

ngx_command_t gCommands[] = {

    { ngx_string("otel_exporter"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_BLOCK|NGX_CONF_NOARGS,
      setExporter,
      NGX_HTTP_MAIN_CONF_OFFSET },

    { ngx_string("otel_service_name"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_str_slot,
      NGX_HTTP_MAIN_CONF_OFFSET,
      offsetof(MainConf, serviceName) },

    { ngx_string("otel_trace"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_http_set_complex_value_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, trace) },

      ngx_null_command
};

ngx_command_t gExporterCommands[] = {

    { ngx_string("endpoint"),
      NGX_CONF_TAKE1,
      ngx_conf_set_str_slot,
      0,
      offsetof(MainConf, endpoint) },

    { ngx_string("interval"),
      NGX_CONF_TAKE1,
      ngx_conf_set_msec_slot,
      0,
      offsetof(MainConf, interval) },

    { ngx_string("batch_size"),
      NGX_CONF_TAKE1,
      ngx_conf_set_size_slot,
      0,
      offsetof(MainConf, batchSize) },

    { ngx_string("batch_count"),
      NGX_CONF_TAKE1,
      ngx_conf_set_size_slot,
      0,
      offsetof(MainConf, batchCount) },

      ngx_null_command
};

std::unique_ptr<BatchExporter> gExporter;

StrView toStrView(ngx_str_t str)
{
    return StrView((char*)str.data, str.len);
}

ngx_int_t onRequestStart(ngx_http_request_t* r)
{
    // don't let internal redirects to override sampling decision
    if (r->internal) {
        return NGX_DECLINED;
    }

    bool sampled = false;

    auto lcf = (LocationConf*)ngx_http_get_module_loc_conf(r, gHttpModule);
    if (lcf->trace != NULL) {
        ngx_str_t trace;
        if (ngx_http_complex_value(r, lcf->trace, &trace) != NGX_OK) {
            return NGX_ERROR;
        }

        sampled = toStrView(trace) == "on";
    }

    if (sampled) {
        ngx_http_set_ctx(r, &gHttpModule, gHttpModule);
    }

    return NGX_DECLINED;
}

StrView getServerName(ngx_http_request_t* r)
{
    auto cscf = (ngx_http_core_srv_conf_t*)
        ngx_http_get_module_srv_conf(r, ngx_http_core_module);

    auto name = cscf->server_name;
    if (name.len == 0) {
        name = r->headers_in.server;
    }

    return toStrView(name);
}

void addDefaultAttrs(BatchExporter::Span& span, ngx_http_request_t* r)
{
    // based on trace semantic conventions for HTTP from 1.16.0 OTel spec

    span.add("http.method", toStrView(r->method_name));

    span.add("http.target", toStrView(r->unparsed_uri));

    auto clcf = (ngx_http_core_loc_conf_t*)
        ngx_http_get_module_loc_conf(r, ngx_http_core_module);
    if (clcf->name.len) {
        span.add("http.route", toStrView(clcf->name));
    }

    span.add("http.scheme", r->connection->ssl ? "https" : "http");

    auto protocol = toStrView(r->http_protocol);
    if (protocol.size() > 5) { // "HTTP/"
        span.add("http.flavor", protocol.substr(5));
    }

    if (r->headers_in.user_agent) {
        span.add("http.user_agent", toStrView(r->headers_in.user_agent->value));
    }

    auto received = r->headers_in.content_length_n;
    span.add("http.request_content_length", received > 0 ? received : 0);

    auto sent = r->connection->sent - (off_t)r->header_size;
    span.add("http.response_content_length", sent > 0 ? sent : 0);

    auto status = r->err_status ? r->err_status : r->headers_out.status;
    if (status) {
        span.add("http.status_code", status);

        if (status >= 500) {
            span.setError();
        }
    }

    span.add("net.host.name", getServerName(r));

    if (ngx_connection_local_sockaddr(r->connection, NULL, 0) == NGX_OK) {
        auto port = ngx_inet_get_port(r->connection->local_sockaddr);
        auto defaultPort = r->connection->ssl ? 443 : 80;

        if (port != defaultPort) {
            span.add("net.host.port", port);
        }
    }

    span.add("net.sock.peer.addr", toStrView(r->connection->addr_text));
    span.add("net.sock.peer.port", ngx_inet_get_port(r->connection->sockaddr));
}

ngx_int_t onRequestEnd(ngx_http_request_t* r)
{
    if (!ngx_http_get_module_ctx(r, gHttpModule)) {
        return NGX_DECLINED;
    }

    auto clcf = (ngx_http_core_loc_conf_t*)ngx_http_get_module_loc_conf(
        r, ngx_http_core_module);

    auto now = ngx_timeofday();

    auto toNanoSec = [](time_t sec, ngx_msec_t msec) -> uint64_t {
        return (sec * 1000 + msec) * 1000000;
    };

    try {
        BatchExporter::SpanInfo info{
            toStrView(clcf->name), TraceContext::generate(true), {},
            toNanoSec(r->start_sec, r->start_msec),
            toNanoSec(now->sec, now->msec)};

        bool ok = gExporter->add(info, [r](BatchExporter::Span& span) {
            addDefaultAttrs(span, r);
        });

        if (!ok) {
            static size_t dropped = 0;
            static time_t lastLog = 0;
            ++dropped;
            if (lastLog != ngx_time()) {
                lastLog = ngx_time();
                ngx_log_error(NGX_LOG_NOTICE, r->connection->log, 0,
                    "OTel dropped records: %uz", dropped);
            }
        }

    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_ERR, r->connection->log, 0,
            "OTel failed to add span: %s", e.what());
        return NGX_ERROR;
    }

    return NGX_DECLINED;
}

ngx_int_t initModule(ngx_conf_t* cf)
{
    auto cmcf = (ngx_http_core_main_conf_t*)ngx_http_conf_get_module_main_conf(
        cf, ngx_http_core_module);

    auto h = (ngx_http_handler_pt*)ngx_array_push(
        &cmcf->phases[NGX_HTTP_REWRITE_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    *h = onRequestStart;

    h = (ngx_http_handler_pt*)ngx_array_push(
        &cmcf->phases[NGX_HTTP_LOG_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    *h = onRequestEnd;

    return NGX_OK;
}

ngx_int_t initWorkerProcess(ngx_cycle_t* cycle)
{
    auto mcf = (MainConf*)ngx_http_cycle_get_module_main_conf(
        cycle, gHttpModule);

    try {
        gExporter.reset(new BatchExporter(
            toStrView(mcf->endpoint),
            mcf->batchSize,
            mcf->batchCount,
            toStrView(mcf->serviceName)));
    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_CRIT, cycle->log, 0,
            "OTel worker init error: %s", e.what());
        return NGX_ERROR;
    }

    static ngx_connection_t dummy;
    static ngx_event_t flushEvent;

    flushEvent.data = &dummy;
    flushEvent.log = cycle->log;
    flushEvent.cancelable = 1;
    flushEvent.handler = [](ngx_event_t* ev) {
        try {
            gExporter->flush();
        } catch (const std::exception& e) {
            ngx_log_error(NGX_LOG_CRIT, ev->log, 0,
                "OTel flush error: %s", e.what());
        }

        auto mcf = (MainConf*)ngx_http_cycle_get_module_main_conf(
            ngx_cycle, gHttpModule);

        ngx_add_timer(ev, mcf->interval);
    };

    ngx_add_timer(&flushEvent, mcf->interval);

    return NGX_OK;
}

void exitWorkerProcess(ngx_cycle_t* cycle)
{
    try {
        gExporter->flush();
    } catch (const std::exception& e) {
        ngx_log_error(NGX_LOG_CRIT, cycle->log, 0,
            "OTel flush error: %s", e.what());
    }

    gExporter.reset();
}

char* setExporter(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto mcf = (MainConf*)conf;

    if (mcf->endpoint.len) {
        return (char*)"is duplicate";
    }

    auto cfCopy = *cf;

    cfCopy.handler = [](ngx_conf_t* cf, ngx_command_t*, void*) {
        auto name = (ngx_str_t*)cf->args->elts;

        for (auto cmd = gExporterCommands; cmd->name.len; cmd++) {

            if (ngx_strcmp(name->data, cmd->name.data) != 0) {
                continue;
            }

            if (cf->args->nelts != 2) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                    "invalid number of arguments in \"%V\" "
                    "directive of \"otel_exporter\"", name);
                return (char*)NGX_CONF_ERROR;
            }

            auto rv = cmd->set(cf, cmd, cf->handler_conf);

            if (rv == NGX_CONF_OK) {
                return rv;
            }

            if (rv != NGX_CONF_ERROR) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                    "\"%V\" directive of \"otel_exporter\" %s", name, rv);
            }

            return (char*)NGX_CONF_ERROR;
        }

        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "unknown directive \"%V\" in \"otel_exporter\"", name);
        return (char*)NGX_CONF_ERROR;
    };

    cfCopy.handler_conf = mcf;

    auto rv = ngx_conf_parse(&cfCopy, NULL);
    if (rv != NGX_CONF_OK) {
        return rv;
    }

    if (mcf->endpoint.len == 0) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "\"otel_exporter\" requires \"endpoint\"");
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

void* createMainConf(ngx_conf_t* cf)
{
    auto mcf = (MainConf*)ngx_pcalloc(cf->pool, sizeof(MainConf));
    if (mcf == NULL) {
        return NULL;
    }

    mcf->interval = NGX_CONF_UNSET_MSEC;
    mcf->batchSize = NGX_CONF_UNSET_SIZE;
    mcf->batchCount = NGX_CONF_UNSET_SIZE;

    return mcf;
}

char* initMainConf(ngx_conf_t* cf, void* conf)
{
    auto mcf = (MainConf*)conf;

    if (mcf->endpoint.len == 0) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "\"otel_exporter\" block is missing");
        return (char*)NGX_CONF_ERROR;
    }

    ngx_conf_init_msec_value(mcf->interval, 5000);
    ngx_conf_init_size_value(mcf->batchSize, 512);
    ngx_conf_init_size_value(mcf->batchCount, 4);

    if (mcf->serviceName.data == NULL) {
        mcf->serviceName = ngx_string("unknown_service:nginx");
    }

    return NGX_CONF_OK;
}

void* createLocationConf(ngx_conf_t* cf)
{
    auto conf = (LocationConf*)ngx_pcalloc(cf->pool, sizeof(LocationConf));
    if (conf == NULL) {
        return NULL;
    }

    conf->trace = (ngx_http_complex_value_t*)NGX_CONF_UNSET_PTR;

    return conf;
}

char* mergeLocationConf(ngx_conf_t* cf, void* parent, void* child)
{
    auto prev = (LocationConf*)parent;
    auto conf = (LocationConf*)child;

    ngx_conf_merge_ptr_value(conf->trace, prev->trace, NULL);

    return NGX_CONF_OK;
}

ngx_http_module_t gHttpModuleCtx = {
    NULL,                               /* preconfiguration */
    initModule,                         /* postconfiguration */

    createMainConf,                     /* create main configuration */
    initMainConf,                       /* init main configuration */

    NULL,                               /* create server configuration */
    NULL,                               /* merge server configuration */

    createLocationConf,                 /* create location configuration */
    mergeLocationConf                   /* merge location configuration */
};

}

ngx_module_t gHttpModule = {
    NGX_MODULE_V1,
    &gHttpModuleCtx,                    /* module context */
    gCommands,                          /* module directives */
    NGX_HTTP_MODULE,                    /* module type */
    NULL,                               /* init master */
    NULL,                               /* init module */
    initWorkerProcess,                  /* init process */
    NULL,                               /* init thread */
    NULL,                               /* exit thread */
    exitWorkerProcess,                  /* exit process */
    NULL,                               /* exit master */
    NGX_MODULE_V1_PADDING
};
