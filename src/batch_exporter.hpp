#pragma once

#include <nginx.h>

#include <thread>
#include <mutex>
#include <vector>

#include "str_view.hpp"
#include "trace_context.hpp"
#include "trace_service_client.hpp"

class BatchExporter {
public:
    typedef TraceServiceClient::Request Request;
    typedef TraceServiceClient::Response Response;

    struct SpanInfo {
        StrView name;
        TraceContext trace;
        opentelemetry::trace::SpanId parent;
        uint64_t start;
        uint64_t end;
    };

    class Span {
    public:
        Span(const Span&) = delete;
        Span& operator=(const Span&) = delete;

        Span(const SpanInfo& info, opentelemetry::proto::trace::v1::Span* span)
            : span(span)
        {
            span->set_kind(
                opentelemetry::proto::trace::v1::Span::SPAN_KIND_SERVER);

            // Short setters, like set_name(), use additional std::string as an
            // intermediary at least up to v21.5 of protobuf.
            set(span->mutable_name(), info.name);

            set(span->mutable_trace_id(), info.trace.traceId.Id());
            set(span->mutable_span_id(), info.trace.spanId.Id());
            set(span->mutable_trace_state(), info.trace.state);

            if (info.parent.IsValid()) {
                set(span->mutable_parent_span_id(), info.parent.Id());
            } else {
                span->mutable_parent_span_id()->clear();
            }

            span->set_start_time_unix_nano(info.start);
            span->set_end_time_unix_nano(info.end);

            span->mutable_status()->clear_code();
        }

        ~Span()
        {
            truncate(span->mutable_attributes(), attrSize);
        }

        void add(StrView key, StrView value)
        {
            add(key)->mutable_value()->mutable_string_value()->assign(
                value.data(), value.size());
        }

        void add(StrView key, int value)
        {
            add(key)->mutable_value()->set_int_value(value);
        }

        void addArray(StrView key, StrView value)
        {
            auto elems = add(key)->mutable_value()->mutable_array_value()->
                mutable_values();

            auto elem = elems->size() > 0 ? elems->Mutable(0) : elems->Add();

            elem->mutable_string_value()->assign(value.data(), value.size());
        }

        void setError()
        {
            span->mutable_status()->set_code(
                opentelemetry::proto::trace::v1::Status::STATUS_CODE_ERROR);
        }

    private:
        template <class ByteRange>
        static void set(std::string* str, const ByteRange& range)
        {
            str->assign((const char*)range.data(), range.size());
        }

        opentelemetry::proto::common::v1::KeyValue* add(StrView key)
        {
            auto attrs = span->mutable_attributes();

            auto newAttr = attrs->size() > attrSize ?
                attrs->Mutable(attrSize) : attrs->Add();

            newAttr->mutable_key()->assign(key.data(), key.size());

            ++attrSize;

            return newAttr;
        }

        opentelemetry::proto::trace::v1::Span* span;
        int attrSize{0};
    };

    BatchExporter(StrView target, const std::shared_ptr<grpc::ChannelCredentials> &creds,
            size_t batchSize, size_t batchCount, StrView serviceName) :
        batchSize(batchSize), client(std::string(target), creds)
    {
        free.reserve(batchCount);
        while (batchCount-- > 0) {
            free.emplace_back();
            auto resourceSpans = free.back().add_resource_spans();

            auto attr = resourceSpans->mutable_resource()->add_attributes();
            attr->set_key("service.name");
            attr->mutable_value()->set_string_value(std::string(serviceName));

            auto scopeSpans = resourceSpans->add_scope_spans();
            scopeSpans->mutable_scope()->set_name("nginx");
            scopeSpans->mutable_scope()->set_version(NGINX_VERSION);

            scopeSpans->mutable_spans()->Reserve(batchSize);
        }

        worker = std::thread(&TraceServiceClient::run, &client);
    }

    ~BatchExporter()
    {
        client.stop();
        worker.join();
    }

    template <class F>
    bool add(const SpanInfo& info, F fillSpan)
    {
        if (currentSize == (int)batchSize) {
            sendBatch(current);
            currentSize = -1;
        }

        if (currentSize == -1) {
            std::unique_lock<std::mutex> lock(mutex);
            if (free.empty()) {
                return false;
            }
            current = std::move(free.back());
            free.pop_back();
            currentSize = 0;
        }

        auto spans = getSpans(current);

        Span span(info, spans->size() > currentSize ?
            spans->Mutable(currentSize) : spans->Add());

        fillSpan(span);

        ++currentSize;

        return true;
    }

    void flush()
    {
        if (currentSize <= 0) {
            return;
        }

        truncate(getSpans(current), currentSize);

        sendBatch(current);
        currentSize = -1;
    }

private:
    const size_t batchSize;

    TraceServiceClient client;

    std::mutex mutex;
    std::vector<Request> free;

    Request current;
    int currentSize{-1};

    std::thread worker;

    static auto getSpans(Request& req) -> decltype(
        req.mutable_resource_spans(0)->mutable_scope_spans(0)->mutable_spans())
    {
        return req.mutable_resource_spans(0)->mutable_scope_spans(0)->
            mutable_spans();
    }

    template <class T>
    static void truncate(T* items, int newSize)
    {
        // unlike DeleteSubrange(), this doesn't destruct removed items
        int tailSize = items->size() - newSize;
        while (tailSize-- > 0) {
            items->RemoveLast();
        }
    }

    void sendBatch(Request& request)
    {
        client.send(request,
            [this](Request req, Response, grpc::Status status) {
                std::unique_lock<std::mutex> lock(mutex);
                free.push_back(std::move(req));
                lock.unlock();

                if (!status.ok()) {
                    ngx_log_error(NGX_LOG_ERR, ngx_cycle->log, 0,
                        "OTel export failure: %s",
                        status.error_message().c_str());
                }
            });
    }
};
