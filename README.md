# NGINX Native OpenTelemetry (OTel) Module

## What is OpenTelemetry
OpenTelemetry (OTel) is an observability framework for monitoring, tracing, troubleshooting, and optimizing applications. OTel enables the collection of telemetry data from a deployed application stack.

## What is the NGINX Native OTel Module
The `ngx_otel_module` dynamic module enables NGINX Open-Source or NGINX Plus to send telemetry data to an OTel collector. It provides support for [W3C trace context](https://www.w3.org/TR/trace-context/) propagation, OTLP/gRPC trace exports and offers several benefits over exiting OTel modules, including:

### Better Performance ###
3rd-party OTel implementations reduce performance of request processing by as much as 50% when tracing is enabled. The NGINX Native module limits this impact to approximately 10-15%.

### Easy Provisioning ###
Setup and configuration can be done right in NGINX configuration files.

### Dynamic, Variable-Based Control ###
The ability to control trace parameters dynamically using cookies, tokens, and variables. Please see our [Ratio-based Tracing](#ratio-based-tracing) example for more details.

Additionally, [NGINX Plus](https://www.nginx.com/products/nginx/), available as part of a [commercial subscription](https://www.nginx.com/products/), enables dynamic control of sampling parameters via the [NGINX Plus API](http://nginx.org/en/docs/http/ngx_http_api_module.html) and [key-value store](http://nginx.org/en/docs/http/ngx_http_keyval_module.html) modules.

## Building
Follow these steps to build the `ngx_otel_module` dynamic module on Ubuntu or Debian based systems:

Install build tools and dependencies.
```bash
sudo apt install cmake build-essential libssl-dev zlib1g-dev libpcre3-dev
sudo apt install pkg-config libc-ares-dev libre2-dev # for gRPC
```

For the next step, you will need the `configure` script that is packaged with the NGINX source code. There are several methods for obtaining NGINX sources. You may choose to [download](http://hg.nginx.org/nginx/archive/tip.tar.gz) them or clone them directly from the [NGINX Github repository](https://github.com/nginx/nginx).

**Important:** To ensure compatibility, the `ngx_otel_module` and the NGINX binary that it will be used with, will need to be built using the same NGINX source code and operating system. We will build and install NGINX from obtained sources in a later step. When obtaining NGINX sources from Github, please ensure that you switch to the branch that you intend to use with the module binary. For simplicity, we will assume that the `main` branch will be used for the remainder of this tutorial.

```bash
git clone https://github.com/nginx/nginx.git
```

Configure NGINX to generate files necessary for dynamic module compilation. These files will be placed into the `nginx/objs` directory. 

**Important:** If you did not obtain NGINX source code via the clone method in the previous step, you will need to adjust paths in the following commands to conform to your specific directory structure.
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
***Important:*** The built `ngx_otel_module.so` dynamic module binary will ONLY be compatible with the same version of NGINX source code that was used to build it. To guarantee proper operation, you will need to build and install NGINX from sources obtained in previous steps on the same operating system.

Follow [instructions](https://docs.nginx.com/nginx/admin-guide/installing-nginx/installing-nginx-open-source/#compiling-and-installing-from-source) related to compiling and installing NGINX. Skip procedures for downloading source code.

By default, this will install NGINX into `/usr/local/nginx`. The following steps assume this directory structure.

Copy the `ngx_otel_module.so` dynamic module binary to `/usr/local/nginx/modules`.

Load the module by adding the following line to the top of the main NGINX configuration file, located at: `/usr/local/nginx/conf/nginx.conf`.

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
    otel_exporter {
        endpoint localhost:4317;
    }

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
