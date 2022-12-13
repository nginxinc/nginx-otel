# nginx_otel

This project provides support for OpenTelemetry distributed tracing in Nginx, offering:

- Lightweight and high-performance incoming HTTP request tracing
- [W3C trace context](https://www.w3.org/TR/trace-context/) propagation
- OTLP/gRPC trace export
- Fully Dynamic Variable-Based Sampling

## Building

Install build tools and dependencies:
```bash
  $ sudo apt install cmake build-essential libssl-dev zlib1g-dev libpcre3-dev
  $ sudo apt install pkg-config libc-ares-dev libre2-dev # for gRPC
```

Configure Nginx:
```bash
  $ ./configure --with-compat
```

Configure and build Nginx OTel module:
```bash
  $ mkdir build
  $ cd build
  $ cmake -DNGX_OTEL_NGINX_BUILD_DIR=/path/to/configured/nginx/objs ..
  $ make
```

## Getting Started

### Simple Tracing

Dumping all the requests could be useful even in non-distributed environment.

```nginx
  http {
      otel_trace on;
      server {
          location / {
              proxy_pass http://backend;
          }
      }
  }
```

### Parent-based Tracing

```nginx
http {
    server {
        location / {
            otel_trace $otel_parent_sampled;
            otel_trace_context propagate;

            proxy_pass http://backend;
        }
    }
}
```

### Ratio-based Tracing

```nginx
http {
    # trace 10% of requests
    split_clients $otel_trace_id $ratio_sampler {
        10%     on;
        *       off;
    }

    # or we can trace 10% of user sessions
    split_clients $cookie_sessionid $session_sampler {
        10%     on;
        *       off;
    }

    server {
        location / {
            otel_trace $ratio_sampler;
            otel_trace_context inject;

            proxy_pass http://backend;
        }
    }
}
```

## How to Use

### Directives

#### Available in `http/server/location` contexts

**`otel_trace`** `on | off | “$var“;`

The argument is a “complex value”, which should result in `on`/`off` or `1`/`0`. Default is `off`.

**`otel_trace_context`** `ignore | extract | inject | propagate;`

Defines how to propagate traceparent/tracestate headers. `extract` uses existing trace context from request. `inject` adds new context to request, rewriting existing headers if any. `propagate` updates existing context (i.e. combines `extract` and `inject`). `ignore` skips context headers processing. Default is `ignore`.

#### Available in `http` context

**`otel_exporter`**`;`

Defines how to export tracing data. There can only be one `otel_exporter` directive in a given `http` context.

```nginx
otel_exporter {
    endpoint “host:port“;
    interval 5s;         # max interval between two exports
    batch_size 512;      # max number of spans to be sent in one batch per worker
    batch_count 4;       # max number of pending batches per worker, over the limit spans are dropped
}
```

**`otel_service_name`** `name;`

Sets `service.name` attribute of OTel resource. By default, it is set to `unknown_service:nginx`.

### Available in `otel_exporter` context

**`endpoint`** `"host:post";`

Defines exporter endpoint `host` and `port`. Only one endpoint per `otel_exporter` can be specified.

**`interval`** `5s;`

Maximum interval between two exports. Default is `5s`.

**`batch_size`** `512;`

Maximum number of spans to be sent in one batch per worker. Detault is 512.

**`batch_count`** `4;`

Maximum number of pending batches per worker, over the limit spans are dropped. Default is 4.

### Variables

`$otel_trace_id` - trace id.

`$otel_span_id` - current span id.

`$otel_parent_id` - parent span id.

`$otel_parent_sampled` - `sampled` flag of parent span, `1`/`0`.

### Default span [attributes](https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/trace/semantic_conventions/http.md)

`http.method`

`http.target`

`http.route`

`http.scheme`

`http.flavor`

`http.user_agent`

`http.request_content_length`

`http.response_content_length`

`http.status_code`

`net.host.name`

`net.host.port`

`net.sock.peer.addr`

`net.sock.peer.port`

## License

[Apache License, Version 2.0](https://github.com/nginxinc/nginx-otel/blob/main/LICENSE)

&copy; [F5, Inc.](https://www.f5.com/) 2023
