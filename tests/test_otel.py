import binascii
from conftest import self_signed_cert
import niquests
import pytest
import socket
import ssl
import time
import urllib3


CERTS = [self_signed_cert, "localhost"]

NGINX_CONFIG = """
{{ globals }}

daemon off;

events {
}

http {
    {{ http_globals }}

    ssl_certificate localhost.crt;
    ssl_certificate_key localhost.key;

    otel_exporter {
        endpoint 127.0.0.1:14317;
        interval 1s;
        batch_size {{ batch_size or 1 }};
        batch_count {{ batch_count or 1 }};
    }

    otel_trace on;
    otel_service_name test_service;

    add_header "X-Otel-Trace-Id" $otel_trace_id;
    add_header "X-Otel-Span-Id" $otel_span_id;
    add_header "X-Otel-Parent-Id" $otel_parent_id;
    add_header "X-Otel-Parent-Sampled" $otel_parent_sampled;

    server {
        listen       127.0.0.1:18443 ssl;
        listen       127.0.0.1:18443 quic;
        listen       127.0.0.1:18080;

        http2 on;

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
            proxy_pass http://127.0.0.1:18080/204;
        }

        location /context-extract {
            otel_trace_context extract;
            proxy_pass http://127.0.0.1:18080/204;
        }

        location /context-inject {
            otel_trace_context inject;
            proxy_pass http://127.0.0.1:18080/204;
        }

        location /context-propagate {
            otel_trace_context propagate;
            proxy_pass http://127.0.0.1:18080/204;
        }

        location /204 {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 204;
        }

        location /404 {
            return 404;
        }
    }
}

"""

(trace_id, span_id) = ("0af7651916cd43dd8448eb211c80319c", "b9c7c989f97918e1")
context = {
    "Traceparent": f"00-{trace_id}-{span_id}-01",
    "Tracestate": "congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
}


def decode_id(span, value):
    if value in ["trace_id", "span_id"]:
        return binascii.hexlify(getattr(span, value)).decode("utf-8")
    return value


def get_attr(span, attr, atype):
    for value in (a.value for a in span.attributes if a.key == attr):
        return getattr(value, atype)


def get_http09(scheme, port, path, logger):
    def do_get(sock, path):
        http_send = f"GET {path}\n".encode()
        logger.debug(f"{http_send=}")
        sock.sendall(http_send)
        http_recv = sock.recv(1024)
        logger.debug(f"{http_recv=}")
        return http_recv.decode("utf-8")

    with socket.create_connection(("127.0.0.1", port)) as sock:
        if scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname="127.0.0.1") as ssock:
                recv = do_get(ssock, path)
        else:
            recv = do_get(sock, path)
    return recv


def get_response(logger, http_ver, scheme, path, headers):
    port = 18443 if scheme == "https" else 18080
    if http_ver == 0:
        return get_http09(scheme, port, path, logger)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with niquests.Session(multiplexed=True) as s:
        if http_ver == 3:
            assert scheme == "https", "Only https:// URLs are supported."
            s.quic_cache_layer.add_domain("127.0.0.1", port)
        resp = s.get(
            f"{scheme}://127.0.0.1:{port}{path}", headers=headers, verify=False
        )
    return resp


@pytest.fixture
def batches(trace_service):
    for _ in range(20):
        if len(trace_service.spans):
            break
        time.sleep(0.1)
    else:
        assert len(trace_service.spans) > 0, "No spans received"
    return trace_service.spans


@pytest.fixture
def span(batches):
    return (batches.pop())[0].scope_spans[0].spans[0]


@pytest.fixture
def response(logger, http_ver, scheme, path, headers):
    return get_response(logger, http_ver, scheme, path, headers)


@pytest.fixture
def nresponses(logger, http_ver, scheme, path, headers, n):
    out = []
    for _ in range(n):
        out.append(get_response(logger, http_ver, scheme, path, headers))
    return out


@pytest.mark.usefixtures("trace_service", "otelcol", "nginx")
@pytest.mark.parametrize(
    ("http_ver", "scheme"),
    [
        (0, "http"),
        (1, "http"),
        (2, "https"),
        (3, "https"),
    ],
    ids=["http 0.9", "http 1.1", "http 2.0 ssl", "http 3.0 quic"],
)
@pytest.mark.parametrize(
    ("path", "headers", "text", "status", "idx"),
    [
        ("/", None, "TRACE-ON", 200, 0),
        ("/404", None, "404 Not Found", 404, 1),
    ],
    ids=["ok", "error"],
)
def test_response_and_span_attributes(
    http_ver, scheme, path, text, status, idx, response, span
):
    assert text in (response.text if http_ver else response)
    if http_ver:
        assert response.status_code == status

    assert span.name == ["default_location", "/404"][idx]

    # Default span attributes
    assert get_attr(span, "http.method", "string_value") == "GET"
    assert get_attr(span, "http.target", "string_value") == path
    assert get_attr(span, "http.route", "string_value") == path
    assert get_attr(span, "http.scheme", "string_value") == scheme
    assert get_attr(span, "http.flavor", "string_value") == (
        [None, "1.1", "2.0", "3.0"][http_ver]
    )
    assert get_attr(span, "http.user_agent", "string_value") == (
        ([None] + [f"niquests/{niquests.__version__}"] * 3)[http_ver]
    )
    assert get_attr(span, "http.request_content_length", "int_value") == 0
    assert get_attr(span, "http.response_content_length", "int_value") == (
        len(response.text if http_ver else response)
    )
    assert get_attr(span, "http.status_code", "int_value") == status
    assert get_attr(span, "net.host.name", "string_value") == "localhost"
    assert get_attr(span, "net.host.port", "int_value") == (
        [18080, 18080, 18443, 18443][http_ver]
    )
    assert get_attr(span, "net.sock.peer.addr", "string_value") == "127.0.0.1"
    assert get_attr(span, "net.sock.peer.port", "int_value") in range(
        1024, 65536
    )

    # Custom span attributes
    assert get_attr(span, "http.request.completion", "string_value") == (
        ["OK", None][idx]
    )
    assert (
        get_attr(span, "http.response.header.content.type", "array_value")
        if idx
        else get_attr(span, "http.response.header.content.type", "array_value")
        .values[0]
        .string_value
    ) == ["text/plain", None][idx]
    assert get_attr(span, "http.request", "string_value") == (
        [
            "GET /" + ["", " HTTP/1.1", " HTTP/2.0", " HTTP/3.0"][http_ver],
            None,
        ][idx]
    )


@pytest.mark.usefixtures("trace_service", "otelcol", "nginx")
@pytest.mark.parametrize(
    ("http_ver", "scheme"), [(1, "http")], ids=["http 1.1"]
)
class TestOtel:
    @pytest.mark.parametrize(("path", "text"), [("/", "TRACE-ON")], ids=["/"])
    @pytest.mark.parametrize(
        ("headers", "tid", "sid", "pid", "ps"),
        [
            (None, "trace_id", "span_id", None, "0"),
            (context, trace_id, "span_id", span_id, "1"),
        ],
        ids=["no context", "context"],
    )
    def test_variables(self, tid, sid, pid, ps, text, response, span):
        assert text in response.text
        assert response.status_code == 200

        assert response.headers["X-Otel-Trace-Id"] == decode_id(span, tid)
        assert response.headers["X-Otel-Span-Id"] == decode_id(span, sid)
        assert response.headers.get("X-Otel-Parent-Id") == pid
        assert response.headers["X-Otel-Parent-Sampled"] == ps

    @pytest.mark.parametrize(("path", "text"), [("/204", "")], ids=["/204"])
    @pytest.mark.parametrize(
        "headers", [None, context], ids=["no context", "context"]
    )
    def test_no_variables(self, text, response):
        assert text in response.text
        assert response.status_code == 204

        assert response.headers.get("X-Otel-Trace-Id") is None
        assert response.headers.get("X-Otel-Span-Id") is None
        assert response.headers.get("X-Otel-Parent-Id") is None
        assert response.headers.get("X-Otel-Parent-Sampled") is None

    @pytest.mark.parametrize(
        ("path", "text", "pid", "tparent", "tstate"),
        [
            (
                "/context-ignore",
                "",
                [None, None],
                [None, context["Traceparent"]],
                [None, context["Tracestate"]],
            ),
            (
                "/context-extract",
                "",
                [None, span_id],
                [None, context["Traceparent"]],
                [None, context["Tracestate"]],
            ),
            (
                "/context-inject",
                "",
                [None, None],
                ["00-trace_id-span_id-01", "00-trace_id-span_id-01"],
                [None, None],
            ),
            (
                "/context-propagate",
                "",
                [None, span_id],
                ["00-trace_id-span_id-01", f"00-{trace_id}-span_id-01"],
                [None, context["Tracestate"]],
            ),
        ],
        ids=["ignore", "extract", "inject", "propagate"],
    )
    @pytest.mark.parametrize(
        ("headers", "idx"),
        [(None, 0), (context, 1)],
        ids=["no context", "context"],
    )
    def test_trace_context(
        self, idx, text, pid, tparent, tstate, response, span
    ):
        assert text in response.text
        assert response.status_code == 204

        # Validate headers
        assert response.headers.get("X-Otel-Parent-Id") == pid[idx]
        assert response.headers.get("X-Otel-Traceparent") == (
            "-".join(decode_id(span, val) for val in tparent[idx].split("-"))
            if tparent[idx] is not None
            else None
        )
        assert response.headers.get("X-Otel-Tracestate") == tstate[idx]

    @pytest.mark.parametrize(
        ("path", "headers", "text", "status", "n"),
        [("/", None, "TRACE-ON", 200, 3)],
        ids=["3 requests"],
    )
    @pytest.mark.parametrize(
        ("nginx_config", "nbatch", "nspan"),
        [
            ({"batch_size": 2, "batch_count": 2}, 2, [2, 1]),
            ({"batch_size": 3, "batch_count": 2}, 1, [3]),
        ],
        indirect=["nginx_config"],
        ids=["batch_size 2", "batch_size 3"],
        scope="module",
    )
    def test_batches(self, text, status, nresponses, batches, nbatch, nspan):
        for r in nresponses:
            assert text in r.text
            assert r.status_code == status

        time.sleep(1)  # wait for the rest batches
        assert len(batches) == nbatch
        for i, b in enumerate(batches):
            assert (
                get_attr(b[0].resource, "service.name", "string_value")
                == "test_service"
            )
            assert len(b[0].scope_spans[0].spans) == nspan[i]
        batches.clear()  # clean up spans
