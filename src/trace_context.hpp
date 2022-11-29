#pragma once

#include <opentelemetry/trace/trace_id.h>
#include <opentelemetry/trace/span_id.h>
#include <opentelemetry/sdk/trace/random_id_generator.h>

#include "str_view.hpp"

struct TraceContext {
    opentelemetry::trace::TraceId traceId;
    opentelemetry::trace::SpanId spanId;
    bool sampled;
    StrView state;

    static TraceContext generate(bool sampled, TraceContext parent = {})
    {
        opentelemetry::sdk::trace::RandomIdGenerator idGen;

        return {parent.traceId.IsValid() ?
                    parent.traceId : idGen.GenerateTraceId(),
                idGen.GenerateSpanId(),
                sampled,
                parent.state};
    }
};
