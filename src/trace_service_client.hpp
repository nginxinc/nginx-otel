#pragma once

#include <functional>

#include <grpcpp/grpcpp.h>
#include <grpcpp/alarm.h>
#include <opentelemetry/proto/collector/trace/v1/trace_service.grpc.pb.h>

namespace otel_proto_trace = opentelemetry::proto::collector::trace::v1;

class TraceServiceClient {
public:
    typedef otel_proto_trace::ExportTraceServiceRequest Request;
    typedef otel_proto_trace::ExportTraceServiceResponse Response;
    typedef otel_proto_trace::TraceService TraceService;

    typedef std::function<void (Request, Response, grpc::Status)>
        ResponseCb;

    TraceServiceClient(const std::string& target, const std::shared_ptr<grpc::ChannelCredentials> &creds)
    {
        auto channel = grpc::CreateChannel(target, creds);
        channel->GetState(true); // trigger 'connecting' state

        stub = TraceService::NewStub(channel);
    }

    void send(Request& req, ResponseCb cb)
    {
        std::unique_ptr<ActiveCall> call{new ActiveCall{}};

        call->request = std::move(req);
        call->cb = std::move(cb);

        ++pending;

        // post actual RPC to worker thread to minimize load on caller
        gpr_timespec past{};
        call->sendAlarm.Set(&queue, past, call.release());
    }

    void run()
    {
        void* tag = NULL;
        bool ok = false;

        while (queue.Next(&tag, &ok)) {
            assert(ok);

            if (tag == &shutdownAlarm) {
                shutdown = true;
            } else {
                std::unique_ptr<ActiveCall> call{(ActiveCall*)tag};

                if (!call->sent) {
                    --pending;

                    call->responseReader = stub->AsyncExport(
                        &call->context, call->request, &queue);
                    call->sent = true;

                    call->responseReader->Finish(
                        &call->response, &call->status, call.get());
                    call.release();
                } else {
                    call->cb(std::move(call->request),
                        std::move(call->response), std::move(call->status));
                }
            }

            // It's not clear if gRPC guarantees any order for expired alarms,
            // so we use 'pending' counter to ensure CQ shutdown happens last.
            // https://github.com/grpc/grpc/issues/31398
            if (shutdown && pending == 0) {
                queue.Shutdown();
            }
        }
    }

    void stop()
    {
        gpr_timespec past{};
        shutdownAlarm.Set(&queue, past, &shutdownAlarm);
    }

private:
    struct ActiveCall {
        grpc::Alarm sendAlarm;
        bool sent;

        grpc::ClientContext context;
        Request request;
        Response response;
        grpc::Status status;
        std::unique_ptr<grpc::ClientAsyncResponseReader<Response>>
            responseReader;

        ResponseCb cb;
    };

    std::unique_ptr<TraceService::Stub> stub;
    grpc::CompletionQueue queue;

    grpc::Alarm shutdownAlarm;
    std::atomic<int> pending{0};
    bool shutdown{false};
};
