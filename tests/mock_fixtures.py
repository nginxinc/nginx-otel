from concurrent import futures
from grpc import server
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
import pytest


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    spans = []

    def Export(self, request, context):
        self.spans.append(request.resource_spans)
        return trace_service_pb2.ExportTracePartialSuccess()


@pytest.fixture(scope="module")
def trace_service_mock(logger):
    mock = server(futures.ThreadPoolExecutor())
    trace_service = TraceService()
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        trace_service, mock
    )
    listen_addr = "localhost:4317"
    mock.add_insecure_port(listen_addr)
    mock.start()
    logger.info(f"Starting otelcol mock at {listen_addr}...")
    yield trace_service
    logger.info("Stopping otelcol mock...")
    mock.stop(grace=None)
