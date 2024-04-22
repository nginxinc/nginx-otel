from binascii import hexlify
from conftest import self_signed_cert
import niquests
import pytest
import socket
import ssl
import subprocess
import time
from urllib.parse import urlparse
import urllib3


CERT_GEN = self_signed_cert

NGINX_CONFIG = """
{{ test_globals }}

daemon off;

events {
}

http {
    {{ test_globals_http }}

    ssl_certificate_key localhost.key;
    ssl_certificate localhost.crt;

    otel_exporter {
        endpoint 127.0.0.1:{{ otel_port }};
        interval 1s;
        batch_size 10;
        batch_count 2;
    }

    otel_service_name {{ otel_service }};
    otel_trace on;

    server {
        listen       127.0.0.1:8443 {{ mode }};
        listen       127.0.0.1:8080;
        server_name  localhost;

        location /trace-on {
            otel_trace_context extract;
            otel_span_name default_location;
            otel_span_attr http.request.completion
                $request_completion;
            otel_span_attr http.response.header.content.type
                $sent_http_content_type;
            otel_span_attr http.request $request;
            add_header "X-Otel-Trace-Id" $otel_trace_id;
            add_header "X-Otel-Span-Id" $otel_span_id;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            add_header "X-Otel-Parent-Sampled" $otel_parent_sampled;
            return 200 "TRACE-ON";
        }

        location /context-ignore {
            otel_trace_context ignore;
            otel_span_name context_ignore;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8080/trace-off;
        }

        location /context-extract {
            otel_trace_context extract;
            otel_span_name context_extract;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8080/trace-off;
        }

        location /context-inject {
            otel_trace_context inject;
            otel_span_name context_inject;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8080/trace-off;
        }

        location /context-propagate {
            otel_trace_context propagate;
            otel_span_name context_propagate;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8080/trace-off;
        }

        location /trace-off {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 200 "TRACE-OFF";
        }
    }
}

"""

trace_headers = {
    "Traceparent": "00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01",
    "Tracestate": "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
}

# Spans
_spans = []

# Headers from responses
_headers = {}


def span_attr(span, attr, atype):
    for value in (_.value for _ in span.attributes if _.key == attr):
        return getattr(value, atype)


def collect_headers(headers, conf):
    if conf not in _headers:
        _headers[conf] = []
    _headers[conf].append(headers)


def h_str(bstr):
    return hexlify(bstr).decode("utf-8")


@pytest.fixture(scope="class")
def _copy_spans(spans):
    yield
    time.sleep(3)  # wait for the last batch
    _spans.extend(spans)


@pytest.fixture()
def case_spans(http_ver, otel_mode):
    _ = 6 * http_ver + 3 * otel_mode
    return _spans[_ : _ + 3]


@pytest.fixture()
def span_list(case_spans):
    spans = []
    for batch in case_spans:
        spans.extend(batch[0].scope_spans[0].spans)
    return spans


@pytest.fixture()
def case_headers(http_ver, otel_mode):
    return _headers.get(f"{http_ver}{otel_mode}")


@pytest.fixture(scope="module")
def _otelcol(testdir, logger):
    (testdir / "otel-config.yaml").write_text(
        """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 127.0.0.1:8317

exporters:
  otlp/auth:
    endpoint: 127.0.0.1:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers:
        - otlp
      exporters:
        - otlp/auth"""
    )
    logger.info("Starting otelcol at 127.0.0.1:8317...")
    proc = subprocess.Popen(
        [
            "../otelcol",
            "--config",
            testdir / "otel-config.yaml",
        ]
    )
    if proc.poll() is not None:
        raise subprocess.SubprocessError("Can't start otelcol")
    yield
    logger.info("Stopping otelcol...")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def session(http_ver, url):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with niquests.Session(multiplexed=True) as s:
        if http_ver == 3:
            parsed = urlparse(url)
            assert parsed.scheme == "https", "Only https:// URLs are supported."
            port = parsed.port if parsed.port is not None else 8443
            s.quic_cache_layer.add_domain(parsed.hostname, port)
        yield s


@pytest.fixture()
def simple_client(url, logger):
    def do_get(sock, path):
        http_send = f"GET {path}\n".encode()
        logger.debug(f"{http_send=}")
        sock.sendall(http_send)
        http_recv = sock.recv(1024)
        logger.debug(f"{http_recv=}")
        return http_recv.decode("utf-8")

    parsed = urlparse(url)
    _ = 8443 if parsed.scheme == "https" else 8080
    port = parsed.port if parsed.port is not None else _
    with socket.create_connection((parsed.hostname, port)) as sock:
        if parsed.scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(
                sock, server_hostname=parsed.hostname
            ) as ssock:
                yield do_get(ssock, parsed.path)
        else:
            yield do_get(sock, parsed.path)


@pytest.mark.usefixtures("_otelcollector", "_otelcol", "nginx")
@pytest.mark.order(1)
@pytest.mark.parametrize(
    "nginx_config",
    [
        {"otel_port": 4317, "otel_service": "test_http0", "mode": "ssl"},
        {"otel_port": 8317, "otel_service": "test_http0", "mode": "ssl"},
    ],
    indirect=True,
    ids=["https 0.9-to mock", "https 0.9-to otelcol"],
)
class TestOTelGenerateSpansSimpleClient:
    @pytest.mark.parametrize(
        ("url", "response"),
        [
            pytest.param(
                "https://127.0.0.1:8443/trace-off",
                "TRACE-OFF",
                id="trace-off",
            ),
            pytest.param(
                "https://127.0.0.1:8443/trace-on",
                "TRACE-ON",
                id="trace-on",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-ignore",
                "TRACE-OFF",
                id="context-ignore",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-extract",
                "TRACE-OFF",
                id="context-extract",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-inject",
                "TRACE-OFF",
                id="context-inject",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-propagate",
                "TRACE-OFF",
                id="context-propagate",
            ),
        ]
        + [
            pytest.param(
                "https://127.0.0.1:8443/trace-on",
                "TRACE-ON",
                id=f"bulk request {_}",
            )
            for _ in range(1, 26)
        ],
    )
    def test_do_request(self, simple_client, url, response):
        assert response == simple_client


@pytest.mark.usefixtures("_otelcollector", "_otelcol", "nginx", "_copy_spans")
@pytest.mark.order(2)
@pytest.mark.parametrize(
    ("nginx_config", "http_ver", "otel_mode"),
    [
        (
            {"otel_port": 4317, "otel_service": "test_http1", "mode": "ssl"},
            1,
            0,
        ),
        (
            {"otel_port": 8317, "otel_service": "test_http1", "mode": "ssl"},
            1,
            1,
        ),
        (
            {
                "otel_port": 4317,
                "otel_service": "test_http2",
                "mode": "ssl http2",
            },
            2,
            0,
        ),
        (
            {
                "otel_port": 8317,
                "otel_service": "test_http2",
                "mode": "ssl http2",
            },
            2,
            1,
        ),
        (
            {"otel_port": 4317, "otel_service": "test_http3", "mode": "quic"},
            3,
            0,
        ),
        (
            {"otel_port": 8317, "otel_service": "test_http3", "mode": "quic"},
            3,
            1,
        ),
    ],
    indirect=["nginx_config"],
    ids=[
        "https-to mock",
        "https-to otelcol",
        "http2-to mock",
        "http2-to otelcol",
        "quic-to mock",
        "quic-to otelcol",
    ],
    scope="module",
)
class TestOTelGenerateSpans:
    @pytest.mark.parametrize(
        ("url", "headers", "response"),
        [
            pytest.param(
                "https://127.0.0.1:8443/trace-off",
                None,
                "TRACE-OFF",
                id="trace-off",
            ),
            pytest.param(
                "https://127.0.0.1:8443/trace-on",
                trace_headers,
                "TRACE-ON",
                id="trace-on with trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/trace-on",
                None,
                "TRACE-ON",
                id="trace-on no trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-ignore",
                None,
                "TRACE-OFF",
                id="context-ignore no trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-ignore",
                trace_headers,
                "TRACE-OFF",
                id="context-ignore with trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-extract",
                None,
                "TRACE-OFF",
                id="context-extract no trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-extract",
                trace_headers,
                "TRACE-OFF",
                id="context-extract with trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-inject",
                None,
                "TRACE-OFF",
                id="context-inject no trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-inject",
                trace_headers,
                "TRACE-OFF",
                id="context-inject with trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-propagate",
                None,
                "TRACE-OFF",
                id="context-propagate no trace headers",
            ),
            pytest.param(
                "https://127.0.0.1:8443/context-propagate",
                trace_headers,
                "TRACE-OFF",
                id="context-propagate with trace headers",
            ),
        ]
        + [
            pytest.param(
                "https://127.0.0.1:8443/trace-on",
                None,
                "TRACE-ON",
                id=f"bulk request {_}",
            )
            for _ in range(1, 21)
        ],
    )
    def test_do_request(
        self, session, http_ver, otel_mode, url, headers, response, spans
    ):
        r = session.get(url, headers=headers, verify=False)
        collect_headers(r.headers, f"{http_ver}{otel_mode}")
        assert r.status_code == 200
        assert r.text == response


@pytest.mark.parametrize("otel_mode", [0, 1], ids=["to mock", "to otelcol"])
@pytest.mark.parametrize(
    "http_ver", [0, 1, 2, 3], ids=["https 0.9", "https", "http2", "quic"]
)
class TestOTelSpans:
    @pytest.mark.parametrize(
        ("batch", "size"),
        [(_, 10) for _ in range(3)],
        ids=[f"batch {_}" for _ in range(3)],
    )
    def test_batch_size(self, http_ver, case_spans, batch, size, otel_mode):
        assert size == len(case_spans[batch][0].scope_spans[0].spans)

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        "batch",
        [_ for _ in range(3)],
        ids=[f"batch {_}" for _ in range(3)],
    )
    def test_service_name(self, http_ver, case_spans, batch, otel_mode):
        assert f"test_http{http_ver}" == span_attr(
            case_spans[batch][0].resource,
            "service.name",
            "string_value",
        )

    @pytest.mark.depends(on=["test_batch_size"])
    def test_trace_off(self, http_ver, span_list, otel_mode):
        assert "/trace-off" not in [
            span_attr(_, "http.target", "string_value") for _ in span_list
        ]

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        ("location", "span_name", "idx"),
        [
            ("/trace-on", "default_location", 0),
            ("/context-ignore", "context_ignore", 2),
            ("/context-extract", "context_extract", 4),
            ("/context-inject", "context_inject", 6),
            ("/context-propagate", "context_propagate", 8),
        ],
        ids=[
            "default_location",
            "context_ignore",
            "context_extract",
            "context_inject",
            "context_propagate",
        ],
    )
    def test_span_name(
        self, http_ver, span_list, location, span_name, idx, logger, otel_mode
    ):
        span = span_list[idx if http_ver else idx // 2]
        assert span_name == span.name
        assert location == span_attr(span, "http.target", "string_value")

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        ("attr_name", "attr_value", "attr_type"),
        [
            ("http.method", "GET", "string_value"),
            ("http.target", "/trace-on", "string_value"),
            ("http.route", "/trace-on", "string_value"),
            ("http.scheme", "https", "string_value"),
            ("http.flavor", [None, "1.1", "2.0", "3.0"], "string_value"),
            (
                "http.user_agent",
                [None] + [f"niquests/{niquests.__version__}"] * 3,
                "string_value",
            ),
            ("http.request_content_length", 0, "int_value"),
            ("http.response_content_length", 8, "int_value"),
            ("http.status_code", 200, "int_value"),
            ("net.host.name", "localhost", "string_value"),
            ("net.host.port", 8443, "int_value"),
            ("net.sock.peer.addr", "127.0.0.1", "string_value"),
            ("net.sock.peer.port", range(1024, 65536), "int_value"),
        ],
        ids=[
            "http.method",
            "http.target",
            "http.route",
            "http.scheme",
            "http.flavor",
            "http.user_agent",
            "http.request_content_length",
            "http.response_content_length",
            "http.status_code",
            "net.host.name",
            "net.host.port",
            "net.sock.peer.addr",
            "net.sock.peer.port",
        ],
    )
    def test_metrics(
        self, http_ver, span_list, attr_name, attr_value, attr_type, otel_mode
    ):
        value = span_attr(span_list[0], attr_name, attr_type)
        if attr_name in ["http.flavor", "http.user_agent"]:
            attr_value = attr_value[http_ver]
        if attr_name == "net.sock.peer.port":
            assert value in attr_value
        else:
            assert value == attr_value

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        ("attr_name", "attr_value", "attr_type"),
        [
            ("http.request.completion", "OK", "string_value"),
            ("http.response.header.content.type", "text/plain", "array_value"),
            (
                "http.request",
                [
                    "GET /trace-on",
                    "GET /trace-on HTTP/1.1",
                    "GET /trace-on HTTP/2.0",
                    "GET /trace-on HTTP/3.0",
                ],
                "string_value",
            ),
        ],
        ids=[
            "http.request.completion",
            "http.response.header.content.type",
            "http.request",
        ],
    )
    def test_custom_metrics(
        self, http_ver, span_list, attr_name, attr_value, attr_type, otel_mode
    ):
        value = span_attr(span_list[0], attr_name, attr_type)
        if attr_type == "array_value":
            value = value.values[0].string_value
        if type(attr_value) is list:
            attr_value = attr_value[http_ver]
        assert attr_value == value

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        ("name", "value", "idx"),
        [
            ("X-Otel-Trace-Id", "trace_id", 1),
            ("X-Otel-Span-Id", "span_id", 1),
            ("X-Otel-Parent-Id", "parent_span_id", 1),
            ("X-Otel-Parent-Sampled", "1", 1),
            ("X-Otel-Parent-Sampled", "0", 2),
        ],
        ids=[
            "otel_trace_id",
            "otel_span_id",
            "otel_parent_id",
            "otel_parent_sampled - 1",
            "otel_parent_sampled - 0",
        ],
    )
    def test_variables(
        self, http_ver, span_list, case_headers, name, value, idx, otel_mode
    ):
        if value.endswith("_id"):
            value = h_str(getattr(span_list[idx - 1], value))
        if http_ver == 0:
            if "Parent" in name:
                pytest.skip("no headers support")
            assert len(value) == (16 if "Span-Id" in name else 32)
        else:
            assert case_headers[idx].get(name) == value

    @pytest.mark.depends(on=["test_batch_size"])
    @pytest.mark.parametrize(
        ("name", "value", "idx"),
        [
            ("X-Otel-Traceparent", None, 3),
            ("X-Otel-Tracestate", None, 3),
            ("X-Otel-Parent-Id", None, 4),
            (
                "X-Otel-Traceparent",
                "00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01",
                4,
            ),
            ("X-Otel-Tracestate", "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7", 4),
        ]
        + [
            ("X-Otel-Traceparent", None, 5),
            ("X-Otel-Tracestate", None, 5),
            ("X-Otel-Parent-Id", "b9c7c989f97918e1", 6),
            (
                "X-Otel-Traceparent",
                "00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01",
                6,
            ),
            ("X-Otel-Tracestate", "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7", 6),
        ]
        + [
            (
                "X-Otel-Traceparent",
                ["00-", "trace_id", "-", "span_id", "-01"],
                7,
            ),
            ("X-Otel-Tracestate", None, 7),
            ("X-Otel-Parent-Id", None, 8),
            (
                "X-Otel-Traceparent",
                ["00-", "trace_id", "-", "span_id", "-01"],
                8,
            ),
            ("X-Otel-Tracestate", None, 8),
        ]
        + [
            (
                "X-Otel-Traceparent",
                ["00-", "trace_id", "-", "span_id", "-01"],
                9,
            ),
            ("X-Otel-Tracestate", None, 9),
            ("X-Otel-Parent-Id", "b9c7c989f97918e1", 10),
            (
                "X-Otel-Traceparent",
                ["00-0af7651916cd43dd8448eb211c80319c-", "span_id", "-01"],
                10,
            ),
            (
                "X-Otel-Tracestate",
                "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
                10,
            ),
        ],
        ids=[
            "ignore-no traceparent-no headers",
            "ignore-no tracestate-no headers",
            "ignore-no parent id-trace headers",
            "ignore-traceparent-trace headers",
            "ignore-tracestate-trace headers",
        ]
        + [
            "extract-no traceparent-no headers",
            "extract-no tracestate-no headers",
            "extract-parent id-trace headers",
            "extract-traceparent-trace headers",
            "extract-tracestate-trace headers",
        ]
        + [
            "inject-traceparent-no headers",
            "inject-no tracestate-no headers",
            "inject-no parent id-trace headers",
            "inject-traceparent-trace headers",
            "inject-tracestate-trace headers",
        ]
        + [
            "propagate-traceparent-no headers",
            "propagate-no tracestate-no headers",
            "propagate-parent id-trace headers",
            "propagate-traceparent-trace headers",
            "propagate-tracestate-trace headers",
        ],
    )
    def test_trace_context(
        self, http_ver, span_list, case_headers, name, value, idx, otel_mode
    ):
        if http_ver == 0:
            pytest.skip("no headers support")
        if type(value) is list:
            value = "".join(
                h_str(getattr(span_list[idx - 1], _))
                if _.endswith("_id")
                else _
                for _ in value
            )
        assert case_headers[idx].get(name) == value
