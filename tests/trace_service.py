import concurrent
import grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
import pytest


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    spans = []

    def Export(self, request, context):
        self.spans.append(request.resource_spans)
        return trace_service_pb2.ExportTracePartialSuccess()


@pytest.fixture(scope="module")
def trace_service(pytestconfig, logger):
    server = grpc.server(concurrent.futures.ThreadPoolExecutor())
    trace_service = TraceService()
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        trace_service, server
    )
    listen_addr = f"127.0.0.1:{24317 if pytestconfig.option.otelcol else 14317}"
    server.add_insecure_port(listen_addr)
    logger.info(f"Starting trace service at {listen_addr}...")
    server.start()
    yield trace_service
    logger.info("Stopping trace service...")
    server.stop(grace=None)
