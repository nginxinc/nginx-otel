from concurrent import futures
import grpc
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
def _otelcollector(logger):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        TraceService(), server
    )
    listen_addr = "localhost:4317"
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info(f"Starting otelcol mock at {listen_addr}...")
    yield
    logger.info("Stopping otelcol mock...")
    server.stop(grace=None)
    clear()


@pytest.fixture(scope="module")
def spans():
    return _spans
