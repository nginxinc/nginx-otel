# NGINX Native OpenTelemetry (OTel) Module

## What is OpenTelemetry
OpenTelemetry (OTel) is an observability framework for monitoring, tracing, troubleshooting, and optimizing applications. OTel enables the collection of telemetry data from a deployed application stack.

## What is the Native NGINX Otel Module
The `ngx_otel_module` dynamic module enables NGINX Open-Source or NGINX Plus to send telemetry data to an OTel collector. It provides support for [W3C trace context](https://www.w3.org/TR/trace-context/) propagation, OTLP/gRPC trace exports and offers several benefits over exiting OTel modules, including:

### Better Performance ###
3rd-party OTel implementations reduce performance of request processing by as much as 50% when tracing is enabled. The NGINX Native module limits this impact to approximately 10-15%.

### Easy Provisioning ###
Setup and configuration can be done right in NGINX configuration files.

### Fully Dynamic Variable-Based Sampling ###
The module provides the ability to trace a particular session by cookie/token. [NGINX Plus](https://www.nginx.com/products/nginx/), available as part of a [commercial subscription](https://www.nginx.com/products/), enables dynamic module control via the [NGINX Plus API](http://nginx.org/en/docs/http/ngx_http_api_module.html) and [key-value store](http://nginx.org/en/docs/http/ngx_http_keyval_module.html) modules.

## Building
Follow these steps to build the `ngx_otel_module` dynamic module on Ubuntu or Debian based systems:

Install build tools and dependencies.
```bash
sudo apt install cmake build-essential libssl-dev zlib1g-dev libpcre3-dev
sudo apt install pkg-config libc-ares-dev libre2-dev # for gRPC
```

Clone the NGINX repository.
```bash
git clone https://github.com/nginx/nginx.git
```

Configure NGINX to generate files necessary for dynamic module compilation. These files will be placed into the `nginx/objs` directory.
```bash
cd nginx
auto/configure --with-compat
```

Exit the NGINX directory and clone the `ngx_otel_module` repository.
```bash
cd ..
git clone https://github.com/nginxinc/nginx-otel.git
```

Configure and build the NGINX OTel module.

**Important**: replace the path in the `cmake` command with the path to the `nginx/objs` directory from above.
```bash
cd nginx-otel
mkdir build
cd build
cmake -DNGX_OTEL_NGINX_BUILD_DIR=/path/to/configured/nginx/objs ..
make
```

Compilation will produce a binary named `ngx_otel_module.so`.

## Installation
If necessary, follow [instructions](https://nginx.org/en/docs/install.html) to install NGINX. Alternatively, you may choose to [download binaries](https://nginx.org/en/download.html).

Copy the `ngx_otel_module.so` dynamic module binary to the NGINX configuration directory, typically located at: `/etc/nginx/modules`.

Load the module by adding the following line to the top of the main NGINX configuration file, typically located at: `/etc/nginx/nginx.conf`.

```nginx
load_module modules/ngx_otel_module.so;
```

## Configuring the Module
For a complete list of directives, embedded variables, default span attributes and sample configurations, please refer to the [`ngx_otel_module` documentation](https://nginx.org/en/docs/ngx_otel_module.html).

## Examples
Use these examples to configure some common use-cases for OTel tracing.

### Simple Tracing
This example sends telemetry data for all http requests.

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
In this example, we inherit trace contexts from incoming requests and record spans only if a parent span is sampled. We also propagate trace contexts and sampling decisions to upstream servers.

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
In this ratio-based example, tracing is configured for a percentage of traffic (in this case 10%):

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

## Collecting and Viewing Traces
There are several methods and available software packages for viewing traces. For a quick start, [Jaeger](https://www.jaegertracing.io/) provides an all-in-one container to collect, process and view OTel trace data. Follow [these steps](https://www.jaegertracing.io/docs/next-release/deployment/#all-in-one) to download, install, launch and use Jaeger's OTel services.

# Community
- Our Slack channel [#nginx-opentelemetry-module](https://nginxcommunity.slack.com/archives/C05NMNAQDU6), is the go-to place to start asking questions and sharing your thoughts.

- Our [GitHub issues page](https://github.com/nginxinc/nginx-otel/issues) offers space for a more technical discussion at your own pace.

# Contributing
Get involved with the project by contributing! Please see our [contributing guide](CONTRIBUTING.md) for details.

# Change Log
See our [release page](https://github.com/nginxinc/nginx-otel/releases) to keep track of updates.

# License
[Apache License, Version 2.0](https://github.com/nginxinc/nginx-otel/blob/main/LICENSE)

&copy; [F5, Inc.](https://www.f5.com/) 2023
