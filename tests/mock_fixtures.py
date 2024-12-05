from concurrent import futures
from grpc import server
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
import pytest


# Spans
_spans = []


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):
        collect(request.resource_spans)
        return trace_service_pb2.ExportTracePartialSuccess()


def collect(spans):
    _spans.append(spans)


def clear():
    _spans.clear()


@pytest.fixture(scope="module")
def _mock_otelcol(logger):
    mock = server(futures.ThreadPoolExecutor())
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        TraceService(), mock
    )
    listen_addr = "localhost:4317"
    mock.add_insecure_port(listen_addr)
    mock.start()
    logger.info(f"Starting otelcol mock at {listen_addr}...")
    yield
    logger.info("Stopping otelcol mock...")
    mock.stop(grace=None)
    clear()


@pytest.fixture(scope="module")
def spans():
    return _spans
