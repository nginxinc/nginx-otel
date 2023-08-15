#!/usr/bin/perl

# (C) Nginx, Inc.

# Tests for OTel exporter in case HTTP/3 using otelcol.

###############################################################################

use warnings;
use strict;

use Test::More;

BEGIN { use FindBin; chdir($FindBin::Bin); }

use Test::Nginx;
use Test::Nginx::HTTP3;

###############################################################################

select STDERR; $| = 1;
select STDOUT; $| = 1;

plan(skip_all => "depends on logs content") unless $ENV{TEST_NGINX_UNSAFE};

eval { require JSON::PP; };
plan(skip_all => "JSON::PP not installed") if $@;

my $t = Test::Nginx->new()->has(qw/http_v2 http_v3 rewrite cryptx/)
	->has_daemon(qw/openssl/)
	->write_file_expand('nginx.conf', <<'EOF');

%%TEST_GLOBALS%%

daemon off;

events {
}

http {
    %%TEST_GLOBALS_HTTP%%

    ssl_certificate_key localhost.key;
    ssl_certificate localhost.crt;
    ssl_protocols TLSv1.3;

    otel_exporter {
        endpoint 127.0.0.1:%%PORT_4317%%;
        interval 1s;
        batch_size 10;
        batch_count 2;
    }

    otel_service_name test_server;
    otel_trace on;

    server {
        listen       127.0.0.1:%%PORT_8980_UDP%% quic;
        listen       127.0.0.1:8081;
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

        location /context-ignore {
            otel_trace_context ignore;
            otel_span_name context_ignore;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8081/trace-off;
        }

        location /context-extract {
            otel_trace_context extract;
            otel_span_name context_extract;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8081/trace-off;
        }

        location /context-inject {
            otel_trace_context inject;
            otel_span_name context_inject;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8081/trace-off;
        }

        location /context-propagate {
            otel_trace_context propagate;
            otel_span_name context_propogate;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://127.0.0.1:8081/trace-off;
        }

        location /trace-off {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;
            return 200 "TRACE-OFF";
        }
    }
}

EOF

$t->write_file_expand('otel-config.yaml', <<EOF);

receivers:
  otlp:
    protocols:
      grpc:
          endpoint: 127.0.0.1:%%PORT_4317%%

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

$t->write_file('openssl.conf', <<'EOF');
[ req ]
default_bits = 2048
encrypt_key = no
distinguished_name = req_distinguished_name
[ req_distinguished_name ]

EOF

my $d = $t->testdir();

foreach my $name ('localhost') {
	system('openssl req -x509 -new '
		. "-config $d/openssl.conf -subj /CN=$name/ "
		. "-out $d/$name.crt -keyout $d/$name.key "
		. ">>$d/openssl.out 2>&1") == 0
		or die "Can't create certificate for $name: $!\n";
}

#suppress otel collector output
open OLDERR, ">&", \*STDERR;
open STDERR, ">>" , $^O eq 'MSWin32' ? 'nul' : '/dev/null';
$t->run_daemon('../otelcol', '--config', $t->testdir().'/otel-config.yaml');
open STDERR, ">&", \*OLDERR;
$t->waitforsocket('127.0.0.1:' . port(4317)) or
	die 'No otel collector open socket';

$t->try_run('no OTel module')->plan(56);

###############################################################################

#do requests
(undef, my $t_off_resp) = http3_get('/trace-off');

#batch0 (10 requests)
my ($tp_headers, $tp_resp) = http3_get('/trace-on', trace_headers => 1);
my ($t_headers, $t_resp) = http3_get('/trace-on');

(my $t_headers_ignore, undef) = http3_get('/context-ignore');
(my $tp_headers_ignore, undef) = http3_get('/context-ignore',
	trace_headers => 1);
(my $t_headers_extract, undef) = http3_get('/context-extract');
(my $tp_headers_extract, undef) = http3_get('/context-extract',
	trace_headers => 1);
(my $t_headers_inject, undef) = http3_get('/context-inject');
(my $tp_headers_inject, undef) = http3_get('/context-inject',
	trace_headers => 1);
(my $t_headers_propagate, undef) = http3_get('/context-propagate');
(my $tp_headers_propagate, undef) =
	http3_get('/context-propagate', trace_headers => 1);

#batch1 (5 reqeusts)
http3_get('/trace-on') for (1..5);

#waiting batch1 is sent to collector for 1s
select undef, undef, undef, 1;

my @batches = split /\n/, $t->read_file('otel.json');
my $batch_json = JSON::PP::decode_json($batches[0]);
my $spans = $$batch_json{"resourceSpans"}[0]{"scopeSpans"}[0]{"spans"};

#validate responses
like($tp_resp, qr/TRACE-ON/, 'http request1 - trace on');
like($t_resp, qr/TRACE-ON/, 'http request2 - trace on');
like($t_off_resp, qr/TRACE-OFF/, 'http request - trace off');

#validate amount of batches
is(scalar @batches, 2, 'amount of batches - trace on');

#validate batch size
is(scalar @{$spans}, 10, 'batch0 size - trace on');
is(scalar @{${JSON::PP::decode_json($batches[1])}{"resourceSpans"}[0]
	{"scopeSpans"}[0]{"spans"}}, 5, 'batch1 size - trace on');

#validate general attributes
is(get_attr("service.name", "stringValue",
	$$batch_json{resourceSpans}[0]{resource}),
	'test_server', 'service.name - trace on');
is($$spans[0]{name}, 'default_location', 'span.name - trace on');

#validate metrics
is(get_attr("http.method", "stringValue", $$spans[0]), 'GET',
	'http.method metric - trace on');
is(get_attr("http.target", "stringValue", $$spans[0]), '/trace-on',
	'http.target metric - trace on');
is(get_attr("http.route", "stringValue", $$spans[0]), '/trace-on',
	'http.route metric - trace on');
is(get_attr("http.scheme", "stringValue", $$spans[0]), 'https',
	'http.scheme metric - trace on');
is(get_attr("http.flavor", "stringValue", $$spans[0]), '3.0',
	'http.flavor metric - trace on');
is(get_attr("http.user_agent", "stringValue", $$spans[0]), 'nginx-tests',
	'http.user_agent metric - trace on');
is(get_attr("http.request_content_length", "intValue", $$spans[0]), 0,
	'http.request_content_length metric - trace on');
is(get_attr("http.response_content_length", "intValue", $$spans[0]), 8,
	'http.response_content_length metric - trace on');
is(get_attr("http.status_code", "intValue", $$spans[0]), 200,
	'http.status_code metric - trace on');
is(get_attr("net.host.name", "stringValue", $$spans[0]), 'localhost',
	'net.host.name metric - trace on');
is(get_attr("net.host.port", "intValue", $$spans[0]), 8980,
	'net.host.port metric - trace on');
is(get_attr("net.sock.peer.addr", "stringValue", $$spans[0]), '127.0.0.1',
	'net.sock.peer.addr metric - trace on');
like(get_attr("net.sock.peer.port", "intValue", $$spans[0]), qr/\d+/,
	'net.sock.peer.port metric - trace on');

#validate custom http metrics
is(${get_attr("http.request.header.completion", "arrayValue", $$spans[0])}
	{values}[0]{stringValue}, 'OK',
	'http.request.header.completion metric - trace on');
is(${get_attr("http.response.header.content.type", "arrayValue", $$spans[0])}
	{values}[0]{stringValue}, 'text/plain',
	'http.response.header.content.type metric - trace on');
is(get_attr("http.request", "stringValue", $$spans[0]),
	'GET /trace-on HTTP/3.0', 'http.request metric - trace on');

#extract trace info
is($$spans[0]{parentSpanId}, 'b9c7c989f97918e1', 'traceparent - trace on');
is($$spans[0]{traceState}, 'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
	'tracestate - trace on');
is($$spans[1]{parentSpanId}, '', 'no traceparent - trace on');
is($$spans[1]{traceState}, undef, 'no tracestate - trace on');

#variables
is($tp_headers->{'x-otel-trace-id'}, $$spans[0]{traceId},
	'$otel_trace_id variable - trace on');
is($tp_headers->{'x-otel-span-id'}, $$spans[0]{spanId},
	'$otel_span_id variable - trace on');
is($tp_headers->{'x-otel-parent-id'}, $$spans[0]{parentSpanId},
	'$otel_parent_id variable - trace on');
is($tp_headers->{'x-otel-parent-sampled'}, 1,
	'$otel_parent_sampled variable - trace on');
is($t_headers->{'x-otel-parent-sampled'}, 0,
	'$otel_parent_sampled variable - trace on (no traceparent header)');

#trace off
unlike($batches[0],
	qr/\Q{"key":"http.target","value":{"stringValue":"\/trace-off"}}\E/,
	'no metrics - trace off');

#trace context: ignore
is($t_headers_ignore->{'x-otel-traceparent'}, undef,
	'no traceparent - trace context ignore (no trace headers)');
is($t_headers_ignore->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context ignore (no trace headers)');

is($tp_headers_ignore->{'x-otel-parent-id'}, undef,
	'no parent span id - trace context ignore (trace headers)');
is($tp_headers_ignore->{'x-otel-traceparent'},
	'00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01',
	'traceparent - trace context ignore (trace headers)');
is($tp_headers_ignore->{'x-otel-tracestate'},
	'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
	'tracestate - trace context ignore (trace headers)');

#trace context: extract
is($t_headers_extract->{'x-otel-traceparent'}, undef,
	'no traceparent - trace context extract (no trace headers)');
is($t_headers_extract->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context extract (no trace headers)');

is($tp_headers_extract->{'x-otel-parent-id'}, 'b9c7c989f97918e1',
	'parent span id - trace context extract (trace headers)');
is($tp_headers_extract->{'x-otel-traceparent'},
	'00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01',
	'traceparent - trace context extract (trace headers)');
is($tp_headers_extract->{'x-otel-tracestate'},
	'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
	'tracestate - trace context extract (trace headers)');

#trace context: inject
isnt($t_headers_inject->{'x-otel-traceparent'}, undef,
	'traceparent - trace context inject (no trace headers)');
is($t_headers_inject->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context inject (no trace headers)');

is($tp_headers_inject->{'x-otel-parent-id'}, undef,
	'no parent span id - trace context inject (trace headers)');
is($tp_headers_inject->{'x-otel-traceparent'},
	"00-$$spans[7]{traceId}-$$spans[7]{spanId}-01",
	'traceparent - trace context inject (trace headers)');
is($tp_headers_inject->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context inject (trace headers)');

#trace context: propagate
is($t_headers_propagate->{'x-otel-traceparent'},
	"00-$$spans[8]{traceId}-$$spans[8]{spanId}-01",
	'traceparent - trace context propagate (no trace headers)');
is($t_headers_propagate->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context propagate (no trace headers)');

is($tp_headers_propagate->{'x-otel-parent-id'}, 'b9c7c989f97918e1',
	'parent id - trace context propagate (trace headers)');
is($tp_headers_propagate->{'x-otel-traceparent'},
	"00-0af7651916cd43dd8448eb211c80319c-$$spans[9]{spanId}-01",
	'traceparent - trace context propagate (trace headers)');
is($tp_headers_propagate->{'x-otel-tracestate'},
	'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
	'tracestate - trace context propagate (trace headers)');

$t->stop();
my $log = $t->read_file("error.log");

unlike($log, qr/OTel\/grpc: Error parsing metadata: error=invalid value/,
	'log: no error parsing metadata');
unlike($log, qr/OTel export failure: No status received/,
	'log: no export failure');

###############################################################################

sub http3_get {
	my ($path, %extra) = @_;
	my ($frames, $frame);

	my $s = Test::Nginx::HTTP3->new();

	my $sid = $extra{trace_headers}
		? $s->new_stream({ headers => [
			{ name => ':method', value => 'GET' },
			{ name => ':scheme', value => 'http' },
			{ name => ':path', value => $path },
			{ name => ':authority', value => 'localhost' },
			{ name => 'user-agent', value => 'nginx-tests' },
			{ name => 'traceparent',
				value => '00-0af7651916cd43dd8448eb211c80319c-' .
					'b9c7c989f97918e1-01'
			},
			{ name => 'tracestate',
				value => 'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7'
			}]})
		: $s->new_stream({ path => $path });

	$frames = $s->read(all => [{ sid => $sid, fin => 1 }]);

	($frame) = grep { $_->{type} eq "HEADERS" } @$frames;
	my $headers = $frame->{headers};

	($frame) = grep { $_->{type} eq "DATA" } @$frames;
	my $data = $frame->{data};

	return $headers, $data;
}

sub get_attr {
	my($attr, $type, $obj) = @_;

	my ($res) = grep { $$_{"key"} eq $attr } @{$$obj{"attributes"}};

	return defined $res ? $res->{"value"}{$type} : undef;
}

###############################################################################
