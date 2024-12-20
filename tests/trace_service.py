import concurrent
import grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
import pytest
import subprocess
import time


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    batches = []

    def Export(self, request, context):
        self.batches.append(request.resource_spans)
        return trace_service_pb2.ExportTracePartialSuccess()

    def get_batch(self):
        for _ in range(10):
            if len(self.batches):
                break
            time.sleep(0.001)
        assert len(self.batches) == 1
        assert len(self.batches[0]) == 1
        return self.batches.pop()[0]

    def get_span(self):
        batch = self.get_batch()
        assert len(batch.scope_spans) == 1
        assert len(batch.scope_spans[0].spans) == 1
        return batch.scope_spans[0].spans.pop()


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


@pytest.fixture(scope="module")
def otelcol(pytestconfig, testdir, logger, trace_service):
    if pytestconfig.option.otelcol is None:
        yield
        return

    (testdir / "otel-config.yaml").write_text(
        """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 127.0.0.1:14317

exporters:
  otlp:
    endpoint: 127.0.0.1:24317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlp]
  telemetry:
    metrics:
      # prevent otelcol from opening 8888 port
      level: none"""
    )
    logger.info("Starting otelcol at 127.0.0.1:14317...")
    proc = subprocess.Popen(
        [pytestconfig.option.otelcol, "--config", testdir / "otel-config.yaml"]
    )
    time.sleep(1)  # give some time to get ready
    assert proc.poll() is None, "Can't start otelcol"
    yield
    logger.info("Stopping otelcol...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
