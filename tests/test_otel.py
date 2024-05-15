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


CERTS = [self_signed_cert, "localhost"]

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
        endpoint 127.0.0.1:{{ port }};
        interval 1s;
        batch_size 10;
        batch_count 2;
    }

    otel_service_name {{ name }};
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

(trace_id, span_id) = ("0af7651916cd43dd8448eb211c80319c", "b9c7c989f97918e1")
context = {
    "Traceparent": f"00-{trace_id}-{span_id}-01",
    "Tracestate": "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
}

# Headers from responses
_headers = {}


def span_attr(span, attr, atype):
    for value in (atrb.value for atrb in span.attributes if atrb.key == attr):
        return getattr(value, atype)


def collect_headers(headers, conf):
    if conf not in _headers:
        _headers[conf] = []
    _headers[conf].append(headers)


def simple_client(url, logger):
    def do_get(sock, path):
        http_send = f"GET {path}\n".encode()
        logger.debug(f"{http_send=}")
        sock.sendall(http_send)
        http_recv = sock.recv(1024)
        logger.debug(f"{http_recv=}")
        return http_recv.decode("utf-8")

    parsed = urlparse(url)
    port = 8443 if parsed.scheme == "https" else 8080
    port = parsed.port if parsed.port is not None else port
    with socket.create_connection((parsed.hostname, port)) as sock:
        if parsed.scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(
                sock, server_hostname=parsed.hostname
            ) as ssock:
                recv = do_get(ssock, parsed.path)
        else:
            recv = do_get(sock, parsed.path)
    return recv


@pytest.fixture()
def batches(spans, http_ver, otel_mode):
    pos = 6 * http_ver + 3 * otel_mode
    return spans[pos : pos + 3]


@pytest.fixture()
def batch(batches, idx):
    return batches[idx][0]


@pytest.fixture()
def scope_spans(batch):
    return batch.scope_spans[0].spans


@pytest.fixture()
def span(batches, idx):
    spans = []
    for batch in batches:
        spans.extend(batch[0].scope_spans[0].spans)
    return spans[idx]


@pytest.fixture()
def headers(http_ver, otel_mode, idx):
    if http_ver:
        return _headers.get(f"{http_ver}{otel_mode}")[idx]


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
        ["../otelcol", "--config", testdir / "otel-config.yaml"]
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


@pytest.mark.usefixtures("_otelcollector", "_otelcol", "nginx")
@pytest.mark.parametrize(
    ("nginx_config", "http_ver", "otel_mode"),
    [
        ({"port": 4317, "name": "test_http0", "mode": "ssl"}, 0, 0),
        ({"port": 8317, "name": "test_http0", "mode": "ssl"}, 0, 1),
        ({"port": 4317, "name": "test_http1", "mode": "ssl"}, 1, 0),
        ({"port": 8317, "name": "test_http1", "mode": "ssl"}, 1, 1),
        ({"port": 4317, "name": "test_http2", "mode": "ssl http2"}, 2, 0),
        ({"port": 8317, "name": "test_http2", "mode": "ssl http2"}, 2, 1),
        ({"port": 4317, "name": "test_http3", "mode": "quic"}, 3, 0),
        ({"port": 8317, "name": "test_http3", "mode": "quic"}, 3, 1),
    ],
    indirect=["nginx_config"],
    ids=[
        "https 0.9-to mock",
        "https 0.9-to otelcol",
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
    @classmethod
    def teardown_class(cls):
        time.sleep(3)  # wait for sending the last batch to collector

    @pytest.mark.parametrize(
        "headers", [None, context], ids=["no context", "with context"]
    )
    @pytest.mark.parametrize(
        ("url", "response"),
        [
            ("https://127.0.0.1:8443/trace-on", "TRACE-ON"),
            ("https://127.0.0.1:8443/context-ignore", "TRACE-OFF"),
            ("https://127.0.0.1:8443/context-extract", "TRACE-OFF"),
            ("https://127.0.0.1:8443/context-inject", "TRACE-OFF"),
            ("https://127.0.0.1:8443/context-propagate", "TRACE-OFF"),
            ("https://127.0.0.1:8443/trace-off", "TRACE-OFF"),
        ]
        + [("https://127.0.0.1:8443/trace-on", "TRACE-ON")] * 10,
        ids=[
            "trace-on",
            "context-ignore",
            "context-extract",
            "context-inject",
            "context-propagate",
            "trace-off",
        ]
        + ["trace-on bulk request"] * 10,
    )
    def test_do_request(
        self, logger, session, http_ver, otel_mode, url, headers, response
    ):
        if http_ver == 0:
            assert response == simple_client(url, logger)
        else:
            resp = session.get(url, headers=headers, verify=False)
            collect_headers(resp.headers, f"{http_ver}{otel_mode}")
            assert resp.status_code == 200
            assert resp.text == response


@pytest.mark.parametrize(
    "http_ver", [0, 1, 2, 3], ids=["https 0.9", "https", "http2", "quic"]
)
@pytest.mark.parametrize("otel_mode", [0, 1], ids=["to mock", "to otelcol"])
class TestOTelSpans:
    @pytest.mark.parametrize(
        ("idx", "size"), [(0, 10), (1, 10), (2, 10)], ids=["batch"] * 3
    )
    def test_batch_size(self, http_ver, scope_spans, idx, size, otel_mode):
        assert size == len(scope_spans)

    @pytest.mark.parametrize("idx", [0, 1, 2], ids=["batch"] * 3)
    def test_service_name(self, http_ver, batch, idx, otel_mode):
        assert f"test_http{http_ver}" == span_attr(
            batch.resource, "service.name", "string_value"
        )

    @pytest.mark.parametrize("idx", range(30), ids=["span"] * 30)
    def test_trace_off(self, http_ver, span, idx, otel_mode):
        assert "/trace-off" != span_attr(span, "http.target", "string_value")

    @pytest.mark.parametrize("idx", range(30), ids=["span"] * 30)
    @pytest.mark.parametrize(
        ("name", "size"),
        [("trace_id", 32), ("span_id", 16)],
        ids=["trace_id", "span_id"],
    )
    def test_id_size(self, http_ver, span, idx, name, size, otel_mode):
        assert size == len(hexlify(getattr(span, name)).decode("utf-8"))

    @pytest.mark.parametrize(
        ("path", "span_name", "idx"),
        [
            ("/trace-on", "default_location", 0),
            ("/trace-on", "default_location", 1),
            ("/context-ignore", "context_ignore", 2),
            ("/context-ignore", "context_ignore", 3),
            ("/context-extract", "context_extract", 4),
            ("/context-extract", "context_extract", 5),
            ("/context-inject", "context_inject", 6),
            ("/context-inject", "context_inject", 7),
            ("/context-propagate", "context_propagate", 8),
            ("/context-propagate", "context_propagate", 9),
        ]
        + [("/trace-on", "default_location", i) for i in range(10, 30)],
        ids=[
            "default_location-span0",
            "default_location-span1",
            "context_ignore-span2",
            "context_ignore-span3",
            "context_extract-span4",
            "context_extract-span5",
            "context_inject-span6",
            "context_inject-span7",
            "context_propagate-span8",
            "context_propagate-span9",
        ]
        + [f"trace-on-span{i}" for i in range(10, 30)],
    )
    def test_span_name(
        self, http_ver, span, path, span_name, idx, logger, otel_mode
    ):
        assert span_name == span.name
        assert path == span_attr(span, "http.target", "string_value")

    @pytest.mark.parametrize("idx", range(30), ids=["span"] * 30)
    @pytest.mark.parametrize(
        ("name", "atype", "value"),
        [
            ("http.method", "string_value", "GET"),
            (
                "http.target",
                "string_value",
                ["/trace-on"] * 2
                + ["/context-ignore"] * 2
                + ["/context-extract"] * 2
                + ["/context-inject"] * 2
                + ["/context-propagate"] * 2
                + ["/trace-on"] * 20,
            ),
            (
                "http.route",
                "string_value",
                ["/trace-on"] * 2
                + ["/context-ignore"] * 2
                + ["/context-extract"] * 2
                + ["/context-inject"] * 2
                + ["/context-propagate"] * 2
                + ["/trace-on"] * 20,
            ),
            ("http.scheme", "string_value", "https"),
            ("http.flavor", "string_value", [None, "1.1", "2.0", "3.0"]),
            (
                "http.user_agent",
                "string_value",
                [None] + [f"niquests/{niquests.__version__}"] * 3,
            ),
            ("http.request_content_length", "int_value", 0),
            (
                "http.response_content_length",
                "int_value",
                [8] * 2 + [9] * 8 + [8] * 20,
            ),
            ("http.status_code", "int_value", 200),
            ("net.host.name", "string_value", "localhost"),
            ("net.host.port", "int_value", 8443),
            ("net.sock.peer.addr", "string_value", "127.0.0.1"),
            ("net.sock.peer.port", "int_value", range(1024, 65536)),
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
    def test_metrics(self, http_ver, span, idx, name, atype, value, otel_mode):
        if name in ["http.flavor", "http.user_agent"]:
            value = value[http_ver]
        if type(value) is list:
            value = value[idx]
        if name == "net.sock.peer.port":
            assert span_attr(span, name, atype) in value
        else:
            assert span_attr(span, name, atype) == value

    @pytest.mark.parametrize(
        "idx",
        [0, 1] + [i for i in range(10, 30)],
        ids=["span"] * 2 + [f"span{i}" for i in range(10, 30)],
    )
    @pytest.mark.parametrize(
        ("name", "atype", "value"),
        [
            ("http.request.completion", "string_value", "OK"),
            ("http.response.header.content.type", "array_value", "text/plain"),
            (
                "http.request",
                "string_value",
                [
                    "GET /trace-on",
                    "GET /trace-on HTTP/1.1",
                    "GET /trace-on HTTP/2.0",
                    "GET /trace-on HTTP/3.0",
                ],
            ),
        ],
        ids=[
            "http.request.completion",
            "http.response.header.content.type",
            "http.request",
        ],
    )
    def test_custom_metrics(
        self, http_ver, span, idx, name, atype, value, otel_mode
    ):
        assert (
            span_attr(span, name, atype).values[0].string_value
            if atype == "array_value"
            else span_attr(span, name, atype)
        ) == (value[http_ver] if type(value) is list else value)

    @pytest.mark.parametrize(
        "idx", range(2, 10), ids=[f"span{i}" for i in range(2, 10)]
    )
    @pytest.mark.parametrize(
        ("name", "atype", "value"),
        [
            ("http.request.completion", "string_value", None),
            ("http.response.header.content.type", "array_value", None),
            ("http.request", "string_value", None),
        ],
        ids=[
            "http.request.completion",
            "http.response.header.content.type",
            "http.request",
        ],
    )
    def test_no_custom_metrics(
        self, http_ver, span, idx, name, atype, value, otel_mode
    ):
        assert span_attr(span, name, atype) == value

    @pytest.mark.parametrize(
        ("name", "value", "idx"),
        [
            ("X-Otel-Trace-Id", "trace_id", 0),
            ("X-Otel-Span-Id", "span_id", 0),
            ("X-Otel-Parent-Id", None, 0),
            ("X-Otel-Parent-Sampled", "0", 0),
            ("X-Otel-Trace-Id", trace_id, 1),
            ("X-Otel-Span-Id", "span_id", 1),
            ("X-Otel-Parent-Id", span_id, 1),
            ("X-Otel-Parent-Sampled", "1", 1),
        ],
        ids=[
            "otel_trace_id-no context-span0",
            "otel_span_id-no context-span0",
            "no otel_parent_id-no context-span0",
            "otel_parent_sampled is 0-no context-span0",
            "otel_trace_id-with context-span1",
            "otel_span_id-with context-span1",
            "otel_parent_id-with context-span1",
            "otel_parent_sampled is 1-with context-span1",
        ],
    )
    def test_variables(
        self, http_ver, span, headers, name, value, idx, otel_mode
    ):
        if http_ver == 0:
            pytest.skip("no headers support")
        if type(value) is str and value in ["trace_id", "span_id"]:
            value = hexlify(getattr(span, value)).decode("utf-8")
        assert headers.get(name) == value

    @pytest.mark.parametrize(
        ("name", "value", "idx"),
        [
            ("X-Otel-Traceparent", None, 2),
            ("X-Otel-Tracestate", None, 2),
            ("X-Otel-Parent-Id", None, 3),
            ("X-Otel-Traceparent", context["Traceparent"], 3),
            ("X-Otel-Tracestate", context["Tracestate"], 3),
        ]
        + [
            ("X-Otel-Traceparent", None, 4),
            ("X-Otel-Tracestate", None, 4),
            ("X-Otel-Parent-Id", span_id, 5),
            ("X-Otel-Traceparent", context["Traceparent"], 5),
            ("X-Otel-Tracestate", context["Tracestate"], 5),
        ]
        + [
            ("X-Otel-Traceparent", "00-trace_id-span_id-01", 6),
            ("X-Otel-Tracestate", None, 6),
            ("X-Otel-Parent-Id", None, 7),
            ("X-Otel-Traceparent", "00-trace_id-span_id-01", 7),
            ("X-Otel-Tracestate", None, 7),
        ]
        + [
            ("X-Otel-Traceparent", "00-trace_id-span_id-01", 8),
            ("X-Otel-Tracestate", None, 8),
            ("X-Otel-Parent-Id", span_id, 9),
            ("X-Otel-Traceparent", f"00-{trace_id}-span_id-01", 9),
            ("X-Otel-Tracestate", context["Tracestate"], 9),
        ],
        ids=[
            "ignore-no traceparent-no context-span2",
            "ignore-no tracestate-no context-span2",
            "ignore-no parent id-with context-span3",
            "ignore-old traceparent-with context-span3",
            "ignore-old tracestate-with context-span3",
        ]
        + [
            "extract-no traceparent-no context-span4",
            "extract-no tracestate-no context-span4",
            "extract-old parent id-with context-span5",
            "extract-old traceparent-with context-span5",
            "extract-old tracestate-with context-span5",
        ]
        + [
            "inject-new traceparent-no context-span6",
            "inject-no tracestate-no context-span6",
            "inject-no parent id-with context-span7",
            "inject-new traceparent-with context-span7",
            "inject-no tracestate-with context-span7",
        ]
        + [
            "propagate-new traceparent-no context-span8",
            "propagate-no tracestate-no context-span8",
            "propagate-old parent id-with context-span9",
            "propagate-updated traceparent(new span id)-with context-span9",
            "propagate-old tracestate-with context-span9",
        ],
    )
    def test_trace_context(
        self, http_ver, span, headers, name, value, idx, otel_mode
    ):
        if http_ver == 0:
            pytest.skip("no headers support")
        if type(value) is str:
            value = "-".join(
                (
                    hexlify(getattr(span, val)).decode("utf-8")
                    if val in ["trace_id", "span_id"]
                    else val
                )
                for val in value.split("-")
            )
        assert headers.get(name) == value
