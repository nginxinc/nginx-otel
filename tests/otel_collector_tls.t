#!/usr/bin/perl

# (C) Nginx, Inc.

# Tests for OTel exporter in case HTTP using otelcol (with TLS).

###############################################################################

use warnings;
use strict;

use Test::More;

BEGIN { use FindBin; chdir($FindBin::Bin); }

use Test::Nginx;
use File::Copy;

###############################################################################

select STDERR; $| = 1;
select STDOUT; $| = 1;

plan(skip_all => "depends on logs content") unless $ENV{TEST_NGINX_UNSAFE};

eval { require JSON::PP; };
plan(skip_all => "JSON::PP not installed") if $@;

my $t = Test::Nginx->new()->has(qw/http http_ssl rewrite/);

my $nginx_conf = <<'EOF';
%%TEST_GLOBALS%%

daemon off;

events {
}

http {
    %%TEST_GLOBALS_HTTP%%

    otel_exporter {
        endpoint localhost:%%PORT_4317%%;
        interval 1s;
        batch_size 1;
        batch_count 1;
        pem_root_certs {{PEM_ROOT_CERT}};
    }

    otel_service_name test_server;
    otel_trace on;

    server {
        listen       127.0.0.1:8080;
        server_name  localhost;

        location /trace-on {
            otel_trace_context extract;
            otel_span_name default_location;
            otel_span_attr http.request.header.completion
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
    }
}

EOF

$nginx_conf =~ s/\{\{PEM_ROOT_CERT\}\}/${\ $t->testdir() }\/ca.crt/g;

$t->write_file_expand('nginx.conf', $nginx_conf);

$t->write_file_expand('otel-config.yaml', <<EOF);

receivers:
  otlp:
    protocols:
      grpc:
          endpoint: 127.0.0.1:%%PORT_4317%%
          tls:
              cert_file: ${\ $t->testdir() }/server.crt
              key_file:  ${\ $t->testdir() }/server.key
              ca_file:   ${\ $t->testdir() }/ca.crt

exporters:
  logging:
    loglevel: debug
  file:
    path: ${\ $t->testdir() }/otel.json

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [logging, file]
    metrics:
      receivers: [otlp]
      exporters: [logging, file]

EOF

my $d = $t->testdir();

generate_certificates($t);

#suppress otel collector output
open OLDERR, ">&", \*STDERR;
open STDERR, ">>" , $^O eq 'MSWin32' ? 'nul' : '/dev/null';
$t->run_daemon('../otelcol', '--config', $t->testdir().'/otel-config.yaml');
open STDERR, ">&", \*OLDERR;
$t->waitforsocket('127.0.0.1:' . port(4317)) or
	die 'No otel collector open socket';

$t->try_run('OTel module with TLS')->plan(7);

###############################################################################

my $tp_resp = http1_get('/trace-on', trace_headers => 1);
#make sure telemetry was exported
sleep(2);

#validate responses
like($tp_resp, qr/TRACE-ON/, 'http request1 - trace on');

my @batches = split /\n/, $t->read_file('otel.json');
my $batch_json = JSON::PP::decode_json($batches[0]);
my $spans = $$batch_json{"resourceSpans"}[0]{"scopeSpans"}[0]{"spans"};

#variables
like($tp_resp, qr/X-Otel-Trace-Id: $$spans[0]{traceId}/,
	'$otel_trace_id variable - trace on');
like($tp_resp, qr/X-Otel-Span-Id: $$spans[0]{spanId}/,
	'$otel_span_id variable - trace on');
like($tp_resp, qr/X-Otel-Parent-Id: $$spans[0]{parentSpanId}/,
	'$otel_parent_id variable - trace on');
like($tp_resp, qr/X-Otel-Parent-Sampled: 1/,
	'$otel_parent_sampled variable - trace on');

$t->stop();
my $log = $t->read_file("error.log");

unlike($log, qr/OTel\/grpc: Error parsing metadata: error=invalid value/,
	'log: no error parsing metadata');
unlike($log, qr/OTel export failure: No status received/,
	'log: no export failure');

###############################################################################

sub http1_get {
	my ($path, %extra) = @_;

	my $port = $extra{port} || 8080;

	my $r = <<EOF;
GET $path HTTP/1.0
Host: localhost
User-agent: nginx-tests
EOF

	$r .= <<EOF if $extra{trace_headers};
Traceparent: 00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01
Tracestate: congo=ucfJifl5GOE,rojo=00f067aa0ba902b7
EOF

	return http($r . "\n", PeerAddr => '127.0.0.1:' . port($port),
		SSL => $extra{ssl});
}

sub get_attr {
	my($attr, $type, $obj) = @_;

	my ($res) = grep { $$_{"key"} eq $attr } @{$$obj{"attributes"}};

	return defined $res ? $res->{"value"}{$type} : undef;
}

sub generate_certificates {
    my ($t) = @_;
    my $d = $t->testdir();
    my $CA_KEY_PATH      = "$d/ca.key";
    my $CA_CERT_PATH     = "$d/ca.crt";
    my $SERVER_KEY_PATH  = "$d/server.key";
    my $SERVER_CSR_PATH  = "$d/server.csr";
    my $SERVER_CERT_PATH = "$d/server.crt";
    my $COUNTRY          = "US";
    my $STATE            = "State";
    my $LOCALITY         = "City";
    my $ORGANIZATION     = "My Organization";
    my $ROOT_CA_NAME     = "Root CA";
    my $SERVER_CN        = "localhost";

    # Generate the CA private key and certificate
    system("openssl genrsa -out $CA_KEY_PATH 4096 > /dev/null 2>&1") == 0
        or die "Failed to generate CA key: $!";
    system("openssl req -new -x509 -key $CA_KEY_PATH -sha256 -days 3650 -out $CA_CERT_PATH " .
           "-subj '/C=$COUNTRY/ST=$STATE/L=$LOCALITY/O=$ORGANIZATION/CN=$ROOT_CA_NAME' > /dev/null 2>&1") == 0
        or die "Failed to generate CA certificate: $!";

    # Generate the Server private key and CSR
    system("openssl genrsa -out $SERVER_KEY_PATH 4096 > /dev/null 2>&1") == 0
        or die "Failed to generate server key: $!";
    system("openssl req -new -key $SERVER_KEY_PATH -out $SERVER_CSR_PATH " .
           "-subj '/C=$COUNTRY/ST=$STATE/L=$LOCALITY/O=$ORGANIZATION/CN=$SERVER_CN' > /dev/null 2>&1") == 0
        or die "Failed to generate server CSR: $!";

    # Sign the Server certificate with the CA
    system("openssl x509 -req -in $SERVER_CSR_PATH -CA $CA_CERT_PATH -CAkey $CA_KEY_PATH -CAcreateserial " .
           "-out $SERVER_CERT_PATH -days 365 -sha256 > /dev/null 2>&1") == 0
        or die "Failed to sign server certificate: $!";
}

###############################################################################
