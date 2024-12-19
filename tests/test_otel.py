from collections import namedtuple
import niquests
import pytest
import socket
import time
import urllib3


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
        endpoint {{ scheme }}127.0.0.1:14317;
        interval {{ interval or "1ms" }};
        batch_size 3;
        batch_count 3;
    }

    otel_trace on;
    {{ resource_attrs }}

    server {
        listen       127.0.0.1:18443 ssl;
        listen       127.0.0.1:18443 quic;
        listen       127.0.0.1:18080;

        http2 on;

        server_name  localhost;

        location /ok {
            return 200 "OK";
        }

        location /err {
            return 500 "ERR";
        }

        location /custom {
            otel_span_name custom_location;
            otel_span_attr http.request.completion
                $request_completion;
            otel_span_attr http.response.header.content.type
                $sent_http_content_type;
            otel_span_attr http.request $request;
            return 200 "OK";
        }

        location /vars {
            otel_trace_context extract;
            add_header "X-Otel-Trace-Id" $otel_trace_id;
            add_header "X-Otel-Span-Id" $otel_span_id;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            add_header "X-Otel-Parent-Sampled" $otel_parent_sampled;
            return 204;
        }

        location /ignore {
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /extract {
            otel_trace_context extract;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /inject {
            otel_trace_context inject;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /propagate {
            otel_trace_context propagate;
            proxy_pass http://127.0.0.1:18080/notrace;
        }

        location /notrace {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 204;
        }
    }
}

"""

TraceContext = namedtuple("TraceContext", ["trace_id", "span_id", "state"])

parent_ctx = TraceContext(
    trace_id="0af7651916cd43dd8448eb211c80319c",
    span_id="b9c7c989f97918e1",
    state="congo=ucfJifl5GOE,rojo=00f067aa0ba902b7",
)


def trace_headers(ctx):
    return (
        {
            "Traceparent": f"00-{ctx.trace_id}-{ctx.span_id}-01",
            "Tracestate": ctx.state,
        }
        if ctx
        else {"Traceparent": None, "Tracestate": None}
    )


def get_attr(span, name):
    for value in (a.value for a in span.attributes if a.key == name):
        return getattr(value, value.WhichOneof("value"))


@pytest.fixture
def client(nginx):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with niquests.Session(multiplexed=True) as s:
        yield s


def test_http09(trace_service, nginx):

    def get_http09(host, port, path):
        with socket.create_connection((host, port)) as sock:
            sock.sendall(f"GET {path}\n".encode())
            resp = sock.recv(1024).decode("utf-8")
        return resp

    assert get_http09("127.0.0.1", 18080, "/ok") == "OK"

    span = trace_service.get_span()
    assert span.name == "/ok"


@pytest.mark.parametrize("http_ver", ["1.1", "2.0", "3.0"])
@pytest.mark.parametrize(
    ("path", "status"),
    [("/ok", 200), ("/err", 500)],
)
def test_default_attributes(client, trace_service, http_ver, path, status):
    scheme, port = ("http", 18080) if http_ver == "1.1" else ("https", 18443)
    if http_ver == "3.0":
        client.quic_cache_layer.add_domain("127.0.0.1", port)
    r = client.get(f"{scheme}://127.0.0.1:{port}{path}", verify=False)

    span = trace_service.get_span()
    assert span.name == path

    assert get_attr(span, "http.method") == "GET"
    assert get_attr(span, "http.target") == path
    assert get_attr(span, "http.route") == path
    assert get_attr(span, "http.scheme") == scheme
    assert get_attr(span, "http.flavor") == http_ver
    assert get_attr(span, "http.user_agent") == (
        f"niquests/{niquests.__version__}"
    )
    assert get_attr(span, "http.request_content_length") == 0
    assert get_attr(span, "http.response_content_length") == len(r.text)
    assert get_attr(span, "http.status_code") == status
    assert get_attr(span, "net.host.name") == "localhost"
    assert get_attr(span, "net.host.port") == port
    assert get_attr(span, "net.sock.peer.addr") == "127.0.0.1"
    assert get_attr(span, "net.sock.peer.port") in range(1024, 65536)


def test_custom_attributes(client, trace_service):
    assert client.get("http://127.0.0.1:18080/custom").status_code == 200

    span = trace_service.get_span()
    assert span.name == "custom_location"

    assert get_attr(span, "http.request.completion") == "OK"
    value = get_attr(span, "http.response.header.content.type")
    assert value.values[0].string_value == "text/plain"
    assert get_attr(span, "http.request") == "GET /custom HTTP/1.1"


def test_trace_off(client, trace_service):
    assert client.get("http://127.0.0.1:18080/notrace").status_code == 204

    time.sleep(0.01)  # wait for spans
    assert len(trace_service.batches) == 0


@pytest.mark.parametrize("parent", [None, parent_ctx])
def test_variables(client, trace_service, parent):
    r = client.get("http://127.0.0.1:18080/vars", headers=trace_headers(parent))

    span = trace_service.get_span()

    if parent:
        assert span.trace_id.hex() == parent.trace_id
        assert span.parent_span_id.hex() == parent.span_id
        assert span.trace_state == parent.state

    assert r.headers.get("X-Otel-Trace-Id") == span.trace_id.hex()
    assert r.headers.get("X-Otel-Span-Id") == span.span_id.hex()
    assert r.headers.get("X-Otel-Parent-Id") or "" == span.parent_span_id.hex()
    assert r.headers.get("X-Otel-Parent-Sampled") == ("1" if parent else "0")


@pytest.mark.parametrize("parent", [None, parent_ctx])
@pytest.mark.parametrize(
    "path", ["/ignore", "/extract", "/inject", "/propagate"]
)
def test_context(client, trace_service, parent, path):
    headers = trace_headers(parent)

    r = client.get(f"http://127.0.0.1:18080{path}", headers=headers)

    span = trace_service.get_span()

    if path in ["/extract", "/propagate"] and parent:
        assert span.trace_id.hex() == parent.trace_id
        assert span.parent_span_id.hex() == parent.span_id
        assert span.trace_state == parent.state

    if path in ["/inject", "/propagate"]:
        headers = trace_headers(
            TraceContext(
                span.trace_id.hex(),
                span.span_id.hex(),
                span.trace_state or None,
            )
        )

    assert r.headers.get("X-Otel-Traceparent") == headers["Traceparent"]
    assert r.headers.get("X-Otel-Tracestate") == headers["Tracestate"]


@pytest.mark.parametrize(
    "nginx_config",
    [{"interval": "200ms", "scheme": "http://"}],
    indirect=True,
)
@pytest.mark.parametrize("batch_count", [1, 3])
def test_batches(client, trace_service, batch_count):
    batch_size = 3

    for _ in range(
        batch_count * batch_size + 1
    ):  # +1 request to trigger batch sending
        assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    time.sleep(0.01)

    assert len(trace_service.batches) == batch_count

    for batch in trace_service.batches:
        assert (
            get_attr(batch[0].resource, "service.name")
            == "unknown_service:nginx"
        )
        assert len(batch[0].scope_spans[0].spans) == batch_size

    time.sleep(0.3)  # wait for +1 request to be flushed
    trace_service.batches.clear()


@pytest.mark.parametrize(
    "nginx_config",
    [
        {
            "resource_attrs": """
                otel_service_name "test_service";
                otel_resource_attr my.name "my name";
                otel_resource_attr my.service "my service";
            """,
        }
    ],
    indirect=True,
)
def test_custom_resource_attributes(client, trace_service):
    assert client.get("http://127.0.0.1:18080/ok").status_code == 200

    batch = trace_service.get_batch()

    assert get_attr(batch[0].resource, "service.name") == "test_service"
    assert get_attr(batch[0].resource, "my.name") == "my name"
    assert get_attr(batch[0].resource, "my.service") == "my service"
