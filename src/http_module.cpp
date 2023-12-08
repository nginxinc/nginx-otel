extern "C" {
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
}

#include <grpc/support/log.h>
#include <google/protobuf/stubs/logging.h>

#include "str_view.hpp"
#include "trace_context.hpp"
#include "batch_exporter.hpp"

extern ngx_module_t gHttpModule;

namespace {

struct OtelCtx {
    TraceContext parent;
    TraceContext current;
};

struct MainConf {
    ngx_str_t endpoint;
    ngx_msec_t interval;
    size_t batchSize;
    size_t batchCount;

    ngx_str_t serviceName;
};

struct SpanAttr {
    ngx_str_t name;
    ngx_http_complex_value_t value;
};

struct LocationConf {
    ngx_http_complex_value_t* trace;
    ngx_uint_t traceContext;

    ngx_http_complex_value_t* spanName;
    ngx_array_t spanAttrs;
};

char* setExporter(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);
char* addSpanAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf);

namespace Propagation {

const ngx_uint_t Extract = 1;
const ngx_uint_t Inject = 2;

/*const*/ ngx_conf_enum_t Types[] = {
    { ngx_string("ignore"), 0 },
    { ngx_string("extract"), Extract },
    { ngx_string("inject"), Inject },
    { ngx_string("propagate"), Extract | Inject },
    { ngx_null_string, 0 }
};

}

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

    { ngx_string("otel_trace_context"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_enum_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, traceContext),
      &Propagation::Types },

    { ngx_string("otel_span_name"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_http_set_complex_value_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(LocationConf, spanName) },

    { ngx_string("otel_span_attr"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE2,
      addSpanAttr,
      NGX_HTTP_LOC_CONF_OFFSET },

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

ngx_str_t toNgxStr(StrView str)
{
    return ngx_str_t{str.size(), (u_char*)str.data()};
}

LocationConf* getLocationConf(ngx_http_request_t* r)
{
    return (LocationConf*)ngx_http_get_module_loc_conf(r, gHttpModule);
}

void cleanupOtelCtx(void* data)
{
}

OtelCtx* getOtelCtx(ngx_http_request_t* r)
{
    auto ctx = (OtelCtx*)ngx_http_get_module_ctx(r, gHttpModule);

    // restore module context if it was reset by e.g. internal redirect
    if (ctx == NULL && (r->internal || r->filter_finalize)) {

        for (auto cln = r->pool->cleanup; cln; cln = cln->next) {
            if (cln->handler == cleanupOtelCtx) {
                ctx = (OtelCtx*)cln->data;
                ngx_http_set_ctx(r, ctx, gHttpModule);
                break;
            }
        }
    }

    return ctx;
}

OtelCtx* createOtelCtx(ngx_http_request_t* r)
{
    static_assert(std::is_trivially_destructible<OtelCtx>::value, "");

    auto storage = ngx_pool_cleanup_add(r->pool, sizeof(OtelCtx));
    if (storage == NULL) {
        return NULL;
    }

    storage->handler = cleanupOtelCtx;

    auto ctx = new (storage->data) OtelCtx{};
    ngx_http_set_ctx(r, ctx, gHttpModule);

    return ctx;
}

ngx_table_elt_t* findHeader(ngx_list_t* list, ngx_uint_t hash, StrView key)
{
    auto part = &list->part;
    auto elts = (ngx_table_elt_t*)part->elts;

    for (ngx_uint_t i = 0; /* void */; i++) {

        if (i >= part->nelts) {
            if (part->next == NULL) {
                break;
            }

            part = part->next;
            elts = (ngx_table_elt_t*)part->elts;
            i = 0;
        }

        if (elts[i].hash != hash || elts[i].key.len != key.size() ||
                ngx_memcmp(elts[i].lowcase_key, key.data(), key.size()) != 0) {
            continue;
        }

        return &elts[i];
    }

    return NULL;
}

StrView getHeader(ngx_http_request_t* r, StrView name)
{
    auto hash = ngx_hash_key((u_char*)name.data(), name.size());
    auto header = findHeader(&r->headers_in.headers, hash, name);

    return header ? toStrView(header->value) : StrView{};
}

ngx_int_t updateRequestHeader(ngx_http_request_t* r, ngx_table_elt_t* header)
{
    auto cmcf = (ngx_http_core_main_conf_t*)
        ngx_http_get_module_main_conf(r, ngx_http_core_module);

    auto hh = (ngx_http_header_t*)ngx_hash_find(&cmcf->headers_in_hash,
        header->hash, header->lowcase_key, header->key.len);

    return hh ? hh->handler(r, header, hh->offset) : NGX_OK;
}

ngx_int_t setHeader(ngx_http_request_t* r, StrView name, StrView value)
{
    auto hash = ngx_hash_key((u_char*)name.data(), name.size());
    auto header = findHeader(&r->headers_in.headers, hash, name);

    if (header == NULL) {
        if (value.empty()) {
            return NGX_OK;
        }

        auto headers = &r->headers_in.headers;
        if (!headers->pool && ngx_list_init(headers, r->pool, 2,
                sizeof(ngx_table_elt_t)) != NGX_OK) {
            return NGX_ERROR;
        }

        header = (ngx_table_elt_t*)ngx_list_push(headers);
        if (header == NULL) {
            return NGX_ERROR;
        }

        header->hash = hash;
        header->key = toNgxStr(name);
        header->lowcase_key = header->key.data;
        header->next = NULL;
    }

    header->value = toNgxStr(value);
    return updateRequestHeader(r, header);
}

TraceContext extract(ngx_http_request_t* r)
{
    auto parent = getHeader(r, "traceparent");
    auto state = getHeader(r, "tracestate");

    return TraceContext::parse(parent, state);
}

ngx_int_t inject(ngx_http_request_t* r, const TraceContext& tc)
{
    auto buf = (char*)ngx_pnalloc(r->pool, TraceContext::Size);
    if (buf == NULL) {
        return NGX_ERROR;
    }

    TraceContext::serialize(tc, buf);

    auto rc = setHeader(r, "traceparent", {buf, TraceContext::Size});
    if (rc != NGX_OK) {
        return rc;
    }

    return setHeader(r, "tracestate", tc.state);
}

OtelCtx* ensureOtelCtx(ngx_http_request_t* r)
{
    auto ctx = getOtelCtx(r);
    if (ctx) {
        return ctx;
    }

    ctx = createOtelCtx(r);
    if (!ctx) {
        return NULL;
    }

    auto lcf = getLocationConf(r);
    if (lcf->traceContext & Propagation::Extract) {
        ctx->parent = extract(r);
    }

    ctx->current = TraceContext::generate(false, ctx->parent);

    return ctx;
}

ngx_int_t onRequestStart(ngx_http_request_t* r)
{
    // don't let internal redirects to override sampling decision
    if (r->internal) {
        return NGX_DECLINED;
    }

    bool sampled = false;

    auto lcf = getLocationConf(r);
    if (lcf->trace != NULL) {
        ngx_str_t trace;
        if (ngx_http_complex_value(r, lcf->trace, &trace) != NGX_OK) {
            return NGX_ERROR;
        }

        sampled = toStrView(trace) == "on" || toStrView(trace) == "1";
    }

    if (!lcf->traceContext && !sampled) {
        return NGX_DECLINED;
    }

    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    ctx->current.sampled = sampled;

    ngx_int_t rc = NGX_OK;

    if (lcf->traceContext & Propagation::Inject) {
        rc = inject(r, ctx->current);
    }

    return rc == NGX_OK ? NGX_DECLINED : rc;
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

StrView getSpanName(ngx_http_request_t* r)
{
    auto lcf = getLocationConf(r);

    if (lcf->spanName) {
        ngx_str_t result;
        if (ngx_http_complex_value(r, lcf->spanName, &result) != NGX_OK) {
            throw std::runtime_error("failed to compute complex value");
        }

        return toStrView(result);
    } else {
        auto clcf = (ngx_http_core_loc_conf_t*)
            ngx_http_get_module_loc_conf(r, ngx_http_core_module);

        return toStrView(clcf->name);
    }
}

void addCustomAttrs(BatchExporter::Span& span, ngx_http_request_t* r)
{
    auto lcf = getLocationConf(r);
    auto attrs = (SpanAttr*)lcf->spanAttrs.elts;

    for (ngx_uint_t i = 0; i < lcf->spanAttrs.nelts; i++) {
        ngx_str_t value;
        if (ngx_http_complex_value(r, &attrs[i].value, &value) != NGX_OK) {
            throw std::runtime_error("failed to compute complex value");
        }

        StrView name = toStrView(attrs[i].name);
        if (startsWith(name, "http.request.header.") ||
            startsWith(name, "http.response.header."))
        {
            //TODO: remove this once headers are supported natively
            span.addArray(name, toStrView(value));
        } else {
            span.add(name, toStrView(value));
        }
    }
}

ngx_int_t onRequestEnd(ngx_http_request_t* r)
{
    auto ctx = getOtelCtx(r);
    if (!ctx || !ctx->current.sampled) {
        return NGX_DECLINED;
    }

    auto now = ngx_timeofday();

    auto toNanoSec = [](time_t sec, ngx_msec_t msec) -> uint64_t {
        return (sec * 1000 + msec) * 1000000;
    };

    try {
        BatchExporter::SpanInfo info{
            getSpanName(r), ctx->current, ctx->parent.spanId,
            toNanoSec(r->start_sec, r->start_msec),
            toNanoSec(now->sec, now->msec)};

        bool ok = gExporter->add(info, [r](BatchExporter::Span& span) {
            addDefaultAttrs(span, r);
            addCustomAttrs(span, r);
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

void grpcLogHandler(gpr_log_func_args* args)
{
    ngx_uint_t level = args->severity == GPR_LOG_SEVERITY_ERROR ? NGX_LOG_ERR :
                       args->severity == GPR_LOG_SEVERITY_INFO  ? NGX_LOG_INFO :
                                       /*GPR_LOG_SEVERITY_DEBUG*/ NGX_LOG_DEBUG;

    ngx_log_error(level, ngx_cycle->log, 0, "OTel/grpc: %s", args->message);
}

void protobufLogHandler(google::protobuf::LogLevel logLevel,
    const char* filename, int line, const std::string& msg)
{
    using namespace google::protobuf;

    ngx_uint_t level = logLevel == LOGLEVEL_FATAL   ? NGX_LOG_EMERG :
                       logLevel == LOGLEVEL_ERROR   ? NGX_LOG_ERR :
                       logLevel == LOGLEVEL_WARNING ? NGX_LOG_WARN :
                                 /*LOGLEVEL_INFO*/    NGX_LOG_INFO;

    ngx_log_error(level, ngx_cycle->log, 0, "OTel/protobuf: %s", msg.c_str());
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

    gpr_set_log_function(grpcLogHandler);
    google::protobuf::SetLogHandler(protobufLogHandler);

    return NGX_OK;
}

ngx_int_t initWorkerProcess(ngx_cycle_t* cycle)
{
    auto mcf = (MainConf*)ngx_http_cycle_get_module_main_conf(
        cycle, gHttpModule);

    // no 'http' or 'otel_exporter' blocks
    if (mcf == NULL || mcf->endpoint.len == 0) {
        return NGX_OK;
    }

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
    if (!gExporter) {
        return;
    }

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


    ngx_conf_init_msec_value(mcf->interval, 5000);
    ngx_conf_init_size_value(mcf->batchSize, 512);
    ngx_conf_init_size_value(mcf->batchCount, 4);

    if (mcf->serviceName.data == NULL) {
        mcf->serviceName = ngx_string("unknown_service:nginx");
    }

    return NGX_CONF_OK;
}

char* addSpanAttr(ngx_conf_t* cf, ngx_command_t* cmd, void* conf)
{
    auto lcf = (LocationConf*)conf;

    if (lcf->spanAttrs.elts == NULL && ngx_array_init(&lcf->spanAttrs,
            cf->pool, 4, sizeof(SpanAttr)) != NGX_OK) {
        return (char*)NGX_CONF_ERROR;
    }

    auto attr = (SpanAttr*)ngx_array_push(&lcf->spanAttrs);
    if (attr == NULL) {
        return (char*)NGX_CONF_ERROR;
    }

    auto args = (ngx_str_t*)cf->args->elts;

    attr->name = args[1];

    ngx_http_compile_complex_value_t ccv = { cf, &args[2], &attr->value };
    if (ngx_http_compile_complex_value(&ccv) != NGX_OK) {
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

template <class Id>
ngx_int_t hexIdVar(ngx_http_request_t* r, ngx_http_variable_value_t* v,
    uintptr_t data)
{
    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    auto id = (Id*)((char*)ctx + data);

    if (id->IsValid()) {
        auto size = id->Id().size() * 2;
        auto buf = (char*)ngx_pnalloc(r->pool, size);
        if (buf == NULL) {
            return NGX_ERROR;
        }

        id->ToLowerBase16({buf, size});

        v->len = size;
        v->valid = 1;
        v->no_cacheable = 0;
        v->not_found = 0;
        v->data = (u_char*)buf;

    } else {
        v->not_found = 1;
    }

    return NGX_OK;
}

ngx_int_t parentSampledVar(ngx_http_request_t* r, ngx_http_variable_value_t* v,
    uintptr_t data)
{
    auto ctx = ensureOtelCtx(r);
    if (!ctx) {
        return NGX_ERROR;
    }

    v->len = 1;
    v->valid = 1;
    v->no_cacheable = 0;
    v->not_found = 0;
    v->data = (u_char*)(ctx->parent.sampled ? "1" : "0");

    return NGX_OK;
}

ngx_int_t addVariables(ngx_conf_t* cf)
{
    using namespace opentelemetry::trace;

    ngx_http_variable_t vars[] = {
        { ngx_string("otel_trace_id"), NULL, hexIdVar<TraceId>,
            offsetof(OtelCtx, current.traceId) },

        { ngx_string("otel_span_id"), NULL, hexIdVar<SpanId>,
            offsetof(OtelCtx, current.spanId) },

        { ngx_string("otel_parent_id"), NULL, hexIdVar<SpanId>,
            offsetof(OtelCtx, parent.spanId) },

        { ngx_string("otel_parent_sampled"), NULL, parentSampledVar }
    };

    for (auto& v : vars) {
        auto var = ngx_http_add_variable(cf, &v.name, 0);
        if (var == NULL) {
            return NGX_ERROR;
        }
        var->get_handler = v.get_handler;
        var->data = v.data;
    }

    return NGX_OK;
}

void* createLocationConf(ngx_conf_t* cf)
{
    auto conf = (LocationConf*)ngx_pcalloc(cf->pool, sizeof(LocationConf));
    if (conf == NULL) {
        return NULL;
    }

    conf->trace = (ngx_http_complex_value_t*)NGX_CONF_UNSET_PTR;
    conf->traceContext = NGX_CONF_UNSET_UINT;
    conf->spanName = (ngx_http_complex_value_t*)NGX_CONF_UNSET_PTR;

    return conf;
}

char* mergeLocationConf(ngx_conf_t* cf, void* parent, void* child)
{
    auto prev = (LocationConf*)parent;
    auto conf = (LocationConf*)child;

    ngx_conf_merge_ptr_value(conf->trace, prev->trace, NULL);
    ngx_conf_merge_uint_value(conf->traceContext, prev->traceContext, 0);
    ngx_conf_merge_ptr_value(conf->spanName, prev->spanName, NULL);

    if (conf->spanAttrs.elts == NULL) {
        conf->spanAttrs = prev->spanAttrs;
    }

    auto mcf = (MainConf*)ngx_http_conf_get_module_main_conf(cf, gHttpModule);

    if (mcf->endpoint.len == 0 && conf->trace) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
            "\"otel_exporter\" block is missing");
        return (char*)NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}

ngx_http_module_t gHttpModuleCtx = {
    addVariables,                       /* preconfiguration */
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
