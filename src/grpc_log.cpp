#include "ngx.hpp"

#include "grpc_log.hpp"

#include <grpc/support/log.h>
#include <google/protobuf/stubs/common.h>

#if GOOGLE_PROTOBUF_VERSION < 4022000

#include <google/protobuf/stubs/logging.h>

class ProtobufLog {
public:
    ProtobufLog() { google::protobuf::SetLogHandler(protobufLogHandler); }
    ~ProtobufLog() { google::protobuf::SetLogHandler(NULL); }

private:
    static void protobufLogHandler(google::protobuf::LogLevel logLevel,
        const char* filename, int line, const std::string& msg)
    {
        using namespace google::protobuf;

        ngx_uint_t level = logLevel == LOGLEVEL_FATAL   ? NGX_LOG_EMERG :
                           logLevel == LOGLEVEL_ERROR   ? NGX_LOG_ERR :
                           logLevel == LOGLEVEL_WARNING ? NGX_LOG_WARN :
                                     /*LOGLEVEL_INFO*/    NGX_LOG_INFO;

        ngx_log_error(level, ngx_cycle->log, 0, "OTel/protobuf: %s",
            msg.c_str());
    }
};

#else

#include <absl/log/globals.h>
#include <absl/log/initialize.h>
#include <absl/log/log_sink_registry.h>

class ProtobufLog : absl::LogSink {
public:
    ProtobufLog()
    {
        absl::InitializeLog();
        absl::AddLogSink(this);
        // Disable logging to stderr
        absl::SetStderrThreshold(static_cast<absl::LogSeverity>(100));
    }

    ~ProtobufLog() override { absl::RemoveLogSink(this); }

    void Send(const absl::LogEntry& entry) override
    {
        auto severity = entry.log_severity();

        ngx_uint_t level =
            severity == absl::LogSeverity::kFatal   ? NGX_LOG_EMERG :
            severity == absl::LogSeverity::kError   ? NGX_LOG_ERR :
            severity == absl::LogSeverity::kWarning ? NGX_LOG_WARN :
                      /*absl::LogSeverity::kInfo*/    NGX_LOG_INFO;

        ngx_str_t message { entry.text_message().size(),
                            (u_char*)entry.text_message().data() };

        ngx_log_error(level, ngx_cycle->log, 0, "OTel/protobuf: %V", &message);
    }
};

#endif

class GrpcLog {
public:
    GrpcLog() { gpr_set_log_function(grpcLogHandler); }
    ~GrpcLog() { gpr_set_log_function(NULL); }

private:
    static void grpcLogHandler(gpr_log_func_args* args)
    {
        ngx_uint_t level =
            args->severity == GPR_LOG_SEVERITY_ERROR ? NGX_LOG_ERR :
            args->severity == GPR_LOG_SEVERITY_INFO  ? NGX_LOG_INFO :
                            /*GPR_LOG_SEVERITY_DEBUG*/ NGX_LOG_DEBUG;

        ngx_log_error(level, ngx_cycle->log, 0, "OTel/grpc: %s",
            args->message);
    }

    ProtobufLog protoLog;
};

void initGrpcLog()
{
    static GrpcLog init;
}
