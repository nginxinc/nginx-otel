from binascii import hexlify
from conftest import self_signed_cert
from niquests import __version__ as VERSION
from niquests import Session
from os import path
import pytest
from socket import create_connection
from ssl import CERT_NONE, PROTOCOL_TLS_CLIENT, SSLContext
from subprocess import Popen, SubprocessError, TimeoutExpired
from time import sleep
from urllib.parse import urlparse
from urllib3 import disable_warnings, exceptions


CERTS = [self_signed_cert, "localhost"]

NGINX_CONFIG = """
{{ globals }}

daemon off;

events {
}

http {
    {{ globals_http }}

    ssl_certificate localhost.crt;
    ssl_certificate_key localhost.key;

    otel_exporter {
        endpoint 127.0.0.1:14317;
        interval 1s;
        batch_size 10;
        batch_count 2;
    }

    otel_trace on;
    otel_service_name {{ name }};

    add_header "X-Otel-Trace-Id" $otel_trace_id;
    add_header "X-Otel-Span-Id" $otel_span_id;
    add_header "X-Otel-Parent-Id" $otel_parent_id;
    add_header "X-Otel-Parent-Sampled" $otel_parent_sampled;

    server {
        listen       127.0.0.1:8443 {{ mode }};
        listen       127.0.0.1:8080;
        server_name  localhost;

        location / {
            otel_trace_context extract;
            otel_span_name default_location;
            otel_span_attr http.request.completion
                $request_completion;
            otel_span_attr http.response.header.content.type
                $sent_http_content_type;
            otel_span_attr http.request $request;
            return 200 "TRACE-ON";
        }

        location /context-ignore {
            otel_span_name context_ignore;
            proxy_pass http://127.0.0.1:8080/204;
        }

        location /context-extract {
            otel_trace_context extract;
            otel_span_name context_extract;
            proxy_pass http://127.0.0.1:8080/204;
        }

        location /context-inject {
            otel_trace_context inject;
            otel_span_name context_inject;
            proxy_pass http://127.0.0.1:8080/204;
        }

        location /context-propagate {
            otel_trace_context propagate;
            otel_span_name context_propagate;
            proxy_pass http://127.0.0.1:8080/204;
        }

        location /204 {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 204;
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


def decode_id(span, value):
    if value in ["trace_id", "span_id"]:
        return hexlify(getattr(span, value)).decode("utf-8")
    return value


def span_attr(span, attr, atype):
    for value in (atrb.value for atrb in span.attributes if atrb.key == attr):
        return getattr(value, atype)


def collect_headers(headers, http_ver):
    if f"http{http_ver}" not in _headers:
        _headers[f"http{http_ver}"] = []
    _headers[f"http{http_ver}"].append(headers)


def simple_client(url, logger):
    def do_get(sock, path):
        http_send = f"GET {path}\n".encode()
        logger.debug(f"{http_send=}")
        sock.sendall(http_send)
        http_recv = sock.recv(1024)
        logger.debug(f"{http_recv=}")
        return http_recv.decode("utf-8")

    p_url = urlparse(url)
    port = 8443 if p_url.scheme == "https" else 8080
    port = p_url.port if p_url.port is not None else port
    with create_connection((p_url.hostname, port)) as sock:
        if p_url.scheme == "https":
            ctx = SSLContext(PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=p_url.hostname) as ssock:
                recv = do_get(ssock, p_url.path)
        else:
            recv = do_get(sock, p_url.path)
    return recv


@pytest.fixture
def batches(trace_service_mock, http_ver):
    return trace_service_mock.spans[http_ver * 3 : http_ver * 3 + 3]


@pytest.fixture
def span(batches, idx):
    return batches[idx // 10][0].scope_spans[0].spans[idx % 10]


@pytest.fixture
def headers(http_ver, idx):
    if http_ver:
        return _headers.get(f"http{http_ver}")[idx]


@pytest.fixture(scope="module")
def _otelcol(pytestconfig, testdir, logger):
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
  otlp/auth:
    endpoint: 127.0.0.1:24317
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
    logger.info("Starting otelcol at 127.0.0.1:14317...")
    assert path.exists(pytestconfig.option.otelcol), "No otelcol binary found"
    proc = Popen(
        [pytestconfig.option.otelcol, "--config", testdir / "otel-config.yaml"]
    )
    if proc.poll() is not None:
        raise SubprocessError("Can't start otelcol")
    yield
    logger.info("Stopping otelcol...")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except TimeoutExpired:
        proc.kill()


@pytest.fixture
def response(logger, http_ver, url, headers):
    if http_ver == 0:
        return simple_client(url, logger)
    disable_warnings(exceptions.InsecureRequestWarning)
    with Session(multiplexed=True) as s:
        if http_ver == 3:
            p_url = urlparse(url)
            assert p_url.scheme == "https", "Only https:// URLs are supported."
            port = p_url.port if p_url.port is not None else 8443
            s.quic_cache_layer.add_domain(p_url.hostname, port)
        resp = s.get(url, headers=headers, verify=False)
    collect_headers(resp.headers, http_ver)
    return resp.text


@pytest.mark.usefixtures("trace_service_mock", "_otelcol", "nginx")
@pytest.mark.parametrize(
    ("nginx_config", "http_ver"),
    [
        ({"name": "test_http0", "mode": "ssl"}, 0),
        ({"name": "test_http1", "mode": "ssl"}, 1),
        ({"name": "test_http2", "mode": "ssl http2"}, 2),
        ({"name": "test_http3", "mode": "quic"}, 3),
    ],
    indirect=["nginx_config"],
    ids=["https 0.9", "https", "http2", "quic"],
    scope="module",
)
class TestOTelGenerateSpans:
    @classmethod
    def teardown_class(cls):
        sleep(4)  # wait for sending the last batch to collector

    @pytest.mark.parametrize(
        "headers", [None, context], ids=["no context", "context"]
    )
    @pytest.mark.parametrize(
        ("url", "value"),
        [
            ("https://127.0.0.1:8443/", "TRACE-ON"),
            ("https://127.0.0.1:8443/context-ignore", ""),
            ("https://127.0.0.1:8443/context-extract", ""),
            ("https://127.0.0.1:8443/context-inject", ""),
            ("https://127.0.0.1:8443/context-propagate", ""),
        ],
        ids=[
            "trace on",
            "context ignore",
            "context extract",
            "context inject",
            "context propagate",
        ],
    )
    def test_make_batch0(self, logger, response, http_ver, url, headers, value):
        assert response == value

    @pytest.mark.parametrize(
        ("url", "value", "headers"),
        [("https://127.0.0.1:8443/", "TRACE-ON", None)] * 10,
        ids=["trace on"] * 10,
    )
    def test_make_batch1(self, logger, response, http_ver, url, headers, value):
        assert response == value

    @pytest.mark.parametrize(
        ("url", "value", "headers"),
        [("https://127.0.0.1:8443/", "TRACE-ON", None)] * 10,
        ids=["trace on"] * 10,
    )
    def test_make_batch2(self, logger, response, http_ver, url, headers, value):
        assert response == value

    @pytest.mark.parametrize(
        ("url", "value", "headers"),
        [("https://127.0.0.1:8443/204", "", None)],
        ids=["trace off"],
    )
    def test_do_request(self, logger, response, http_ver, url, headers, value):
        assert response == value


@pytest.mark.parametrize(
    "http_ver", [0, 1, 2, 3], ids=["https 0.9", "https", "http2", "quic"]
)
class TestOTelSpans:
    @pytest.mark.parametrize(
        ("idx", "value"), enumerate([10] * 3), ids=["batch"] * 3
    )
    def test_batch_size(self, http_ver, batches, idx, value):
        assert len(batches[idx][0].scope_spans[0].spans) == value

    @pytest.mark.parametrize("idx", [0, 1, 2], ids=["batch"] * 3)
    def test_service_name(self, http_ver, batches, idx):
        assert (
            span_attr(batches[idx][0].resource, "service.name", "string_value")
        ) == f"test_http{http_ver}"

    @pytest.mark.parametrize(
        ("idx", "value"),
        enumerate(
            ["default_location"] * 2
            + ["context_ignore"] * 2
            + ["context_extract"] * 2
            + ["context_inject"] * 2
            + ["context_propagate"] * 2
            + ["default_location"] * 20
        ),
        ids=["span"] * 30,
    )
    def test_span_name(self, http_ver, span, value, idx):
        assert span.name == value

    @pytest.mark.parametrize("idx", range(30), ids=["span"] * 30)
    @pytest.mark.parametrize(
        ("name", "atype", "value"),
        [
            ("http.method", "string_value", "GET"),
            (
                "http.target",
                "string_value",
                ["/"] * 2
                + ["/context-ignore"] * 2
                + ["/context-extract"] * 2
                + ["/context-inject"] * 2
                + ["/context-propagate"] * 2
                + ["/"] * 20,
            ),
            (
                "http.route",
                "string_value",
                ["/"] * 2
                + ["/context-ignore"] * 2
                + ["/context-extract"] * 2
                + ["/context-inject"] * 2
                + ["/context-propagate"] * 2
                + ["/"] * 20,
            ),
            ("http.scheme", "string_value", "https"),
            ("http.flavor", "string_value", [None, "1.1", "2.0", "3.0"]),
            (
                "http.user_agent",
                "string_value",
                [None] + [f"niquests/{VERSION}"] * 3,
            ),
            ("http.request_content_length", "int_value", 0),
            (
                "http.response_content_length",
                "int_value",
                [8] * 2 + [0] * 8 + [8] * 20,
            ),
            (
                "http.status_code",
                "int_value",
                [200] * 2 + [204] * 8 + [200] * 20,
            ),
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
    def test_metrics(self, http_ver, span, idx, name, atype, value):
        if name == "net.sock.peer.port":
            assert span_attr(span, name, atype) in value
        else:
            if name in ["http.flavor", "http.user_agent"]:
                value = value[http_ver]
            value = value[idx] if type(value) is list else value
            assert span_attr(span, name, atype) == value

    @pytest.mark.parametrize(
        "idx",
        [0, 1] + list(range(10, 30)),
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
                ["GET /", "GET / HTTP/1.1", "GET / HTTP/2.0", "GET / HTTP/3.0"],
            ),
        ],
        ids=[
            "http.request.completion",
            "http.response.header.content.type",
            "http.request",
        ],
    )
    def test_custom_metrics(self, http_ver, span, idx, name, atype, value):
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
    def test_no_custom_metrics(self, http_ver, span, idx, name, atype, value):
        assert span_attr(span, name, atype) == value

    @pytest.mark.parametrize("idx", [0, 1], ids=["no context", "context"])
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("X-Otel-Trace-Id", ["trace_id", trace_id]),
            ("X-Otel-Span-Id", ["span_id"] * 2),
            ("X-Otel-Parent-Id", [None, span_id]),
            ("X-Otel-Parent-Sampled", ["0", "1"]),
        ],
        ids=[
            "otel_trace_id",
            "otel_span_id",
            "otel_parent_id",
            "otel_parent_sampled",
        ],
    )
    def test_variables(self, http_ver, span, headers, name, value, idx):
        if http_ver == 0:
            pytest.skip("no headers support")
        assert headers.get(name) == decode_id(span, value[idx])

    @pytest.mark.xfail(reason="otel variables are present when trace is off")
    @pytest.mark.parametrize("idx", [10, 11], ids=["no context", "context"])
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("X-Otel-Trace-Id", None),
            ("X-Otel-Span-Id", None),
            ("X-Otel-Parent-Id", None),
            ("X-Otel-Parent-Sampled", None),
        ],
        ids=[
            "otel_trace_id",
            "otel_span_id",
            "otel_parent_id",
            "otel_parent_sampled",
        ],
    )
    def test_no_variables(self, http_ver, headers, name, value, idx):
        if http_ver == 0:
            pytest.skip("no headers support")
        assert headers.get(name) == value

    @pytest.mark.parametrize(
        "idx",
        range(2, 10),
        ids=["ignore-no context", "ignore-context"]
        + ["extract-no context", "extract-context"]
        + ["inject-no context", "inject-context"]
        + ["propagate-no context", "propagate-context"],
    )
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            (
                "X-Otel-Parent-Id",
                ([None] * 3 + [span_id]) * 2,
            ),
            (
                "X-Otel-Traceparent",
                [None, context["Traceparent"]] * 2
                + ["00-trace_id-span_id-01"] * 3
                + [f"00-{trace_id}-span_id-01"],
            ),
            (
                "X-Otel-Tracestate",
                [None, context["Tracestate"]] * 2
                + [None] * 3
                + [context["Tracestate"]],
            ),
        ],
        ids=["parent id", "traceparent", "tracestate"],
    )
    def test_trace_context(self, http_ver, span, headers, name, value, idx):
        if http_ver == 0:
            pytest.skip("no headers support")
        value = value[idx - 2]  # because idx starts from 2
        if name == "X-Otel-Traceparent" and value is not None:
            value = "-".join(decode_id(span, val) for val in value.split("-"))
        assert headers.get(name) == value
