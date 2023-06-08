#!/usr/bin/perl

# (C) Nginx, Inc.

# Tests for opentelmetry metric exporter in case HTTP.

###############################################################################

use warnings;
use strict;

use Test::More;

BEGIN { use FindBin; chdir($FindBin::Bin); }

use Test::Nginx;
use Test::Nginx::HTTP2;
use MIME::Base64;

###############################################################################

select STDERR; $| = 1;
select STDOUT; $| = 1;

my $t = Test::Nginx->new()->has(qw/http http_ssl http_v2 mirror rewrite/)
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

    otel_exporter {
        endpoint 127.0.0.1:8082;
        interval 1s;
        batch_size 10;
        batch_count 2;
    }

    otel_service_name test_server;
    otel_trace on;

    server {
        listen       127.0.0.1:8080;
        listen       127.0.0.1:8081 ssl;
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
            proxy_pass http://localhost:8080/trace-off;
        }

        location /context-extract {
            otel_trace_context extract;
            otel_span_name context_extract;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://localhost:8080/trace-off;
        }

        location /context-inject {
            otel_trace_context inject;
            otel_span_name context_inject;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://localhost:8080/trace-off;
        }

        location /context-propagate {
            otel_trace_context propagate;
            otel_span_name context_propogate;
            add_header "X-Otel-Parent-Id" $otel_parent_id;
            proxy_pass http://localhost:8080/trace-off;
        }

        location /trace-off {
            otel_trace off;
            add_header "X-Otel-Traceparent" $http_traceparent;
            add_header "X-Otel-Tracestate" $http_tracestate;

            return 200 "TRACE-OFF";
        }
    }

    server {
        listen       127.0.0.1:8082 http2;
        server_name  localhost;
        otel_trace off;

        location / {
            mirror /mirror;
            grpc_pass 127.0.0.1:8083;
        }

        location /mirror {
            internal;
            grpc_pass 127.0.0.1:%%PORT_4317%%;
        }
    }

    server {
        otel_trace off;

        listen       127.0.0.1:8082 http2;
        server_name  localhost;

        location / {
            return 200;
        }
    }
}

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

$t->try_run('no OTEL module')->plan(65);

###############################################################################

my $p = port(4317);
my $f = grpc();

#do requests
my $t_off_resp = http_get('/trace-off');

#batch0 (10 requests)
my $tp_resp = http_get_traceparent('/trace-on');
my $t_resp = http_get_ssl('/trace-on');

my $t_resp_ignore = http_get('/context-ignore');
my $tp_resp_ignore = http_get_traceparent('/context-ignore');
my $t_resp_extract = http_get('/context-extract');
my $tp_resp_extract = http_get_traceparent('/context-extract');
my $t_resp_inject = http_get('/context-inject');
my $tp_resp_inject = http_get_traceparent('/context-inject');
my $t_resp_propagate = http_get('/context-propagate');
my $tp_resp_propagate = http_get_traceparent('/context-propagate');

my $frames = $f->{http_start}();
my ($frame) = grep { $_->{type} eq "DATA" } @$frames;
my $batch0 = to_hash(decode_protobuf(substr($frame->{data}, 8)));

my $spans = $$batch0{scope_spans};

#batch1 (5 reqeusts)
http_get('/trace-on') for (1..5);

$frames = $f->{http_start}();
($frame) = grep { $_->{type} eq "DATA" } @$frames;
my $batch1 = to_hash(decode_protobuf(substr($frame->{data}, 8)));

#validate responses
like($tp_resp, qr/TRACE-ON/, 'http request1 - trace on');
like($t_resp, qr/TRACE-ON/, 'http request2 - trace on');
like($t_off_resp, qr/TRACE-OFF/, 'http request - trace off');

#validate batch size
is(scalar(keys %{$spans}) - 1, 10, 'batch0 size - trace on');
is(scalar(keys %{$$batch1{scope_spans}}) - 1, 5, 'batch1 size - trace on');

#validate general attributes
is(get_attr("service.name", "string_value",
	$$batch0{resource}), 'test_server', 'service.name - trace on');
is($$spans{span0}{name}, '"default_location"', 'span.name - trace on');

#validate http metrics
is(get_attr("http.method", "string_value", $$spans{span0}), 'GET',
	'http.method metric - trace on');
is(get_attr("http.target", "string_value", $$spans{span0}), '/trace-on',
	'http.target metric - trace on');
is(get_attr("http.route", "string_value", $$spans{span0}), '/trace-on',
	'http.route metric - trace on');
is(get_attr("http.scheme", "string_value", $$spans{span0}), 'http',
	'http.scheme metric - trace on');
is(get_attr("http.flavor", "string_value", $$spans{span0}), '1.0',
	'http.flavor metric - trace on');
is(get_attr("http.user_agent", "string_value", $$spans{span0}), 'nginx-tests',
	'http.user_agent metric - trace on');
is(get_attr("http.request_content_length", "int_value", $$spans{span0}), 0,
	'http.request_content_length metric - trace on');
is(get_attr("http.response_content_length", "int_value", $$spans{span0}), 8,
	'http.response_content_length metric - trace on');
is(get_attr("http.status_code", "int_value", $$spans{span0}), 200,
	'http.status_code metric - trace on');
is(get_attr("net.host.name", "string_value", $$spans{span0}), 'localhost',
	'net.host.name metric - trace on');
is(get_attr("net.host.port", "int_value", $$spans{span0}), 8080,
	'net.host.port metric - trace on');
is(get_attr("net.sock.peer.addr", "string_value", $$spans{span0}), '127.0.0.1',
	'net.sock.peer.addr metric - trace on');
like(get_attr("net.sock.peer.port", "int_value", $$spans{span0}), qr/\d+/,
	'net.sock.peer.port metric - trace on');

#validate https metrics
is(get_attr("http.method", "string_value", $$spans{span1}), 'GET',
	'http.method metric - trace on (https)');
is(get_attr("http.target", "string_value", $$spans{span1}), '/trace-on',
	'http.target metric - trace on (https)');
is(get_attr("http.route", "string_value", $$spans{span1}), '/trace-on',
	'http.route metric - trace on (https)');
is(get_attr("http.scheme", "string_value", $$spans{span1}), 'https',
	'http.scheme metric - trace on (https)');
is(get_attr("http.flavor", "string_value", $$spans{span1}), '1.0',
	'http.flavor metric - trace on (https)');
isnt(get_attr("http.user_agent", "string_value", $$spans{span1}),
	'nginx-tests', 'http.user_agent metric - trace on (https)');
is(get_attr("http.request_content_length", "int_value", $$spans{span1}), 0,
	'http.request_content_length metric - trace on (https)');
is(get_attr("http.response_content_length", "int_value", $$spans{span1}), 8,
	'http.response_content_length metric - trace on (https)');
is(get_attr("http.status_code", "int_value", $$spans{span1}), 200,
	'http.status_code metric - trace on (https)');
is(get_attr("net.host.name", "string_value", $$spans{span1}), 'localhost',
	'net.host.name metric - trace on (https)');
is(get_attr("net.host.port", "int_value", $$spans{span1}), 8081,
	'net.host.port metric - trace on (https)');
is(get_attr("net.sock.peer.addr", "string_value", $$spans{span1}), '127.0.0.1',
	'net.sock.peer.addr metric - trace on (https)');
like(get_attr("net.sock.peer.port", "int_value", $$spans{span1}), qr/\d+/,
	'net.sock.peer.port metric - trace on (https)');

#validate custom http metrics
is(${get_attr("http.request.header.completion", "array_value", $$spans{span0})}
	{values}{string_value}, '"OK"',
	'http.request.header.completion metric - trace on');
is(${get_attr("http.response.header.content.type",
		"array_value",$$spans{span0})}{values}{string_value}, '"text/plain"',
	'http.response.header.content.type metric - trace on');
is(get_attr("http.request", "string_value", $$spans{span0}),
	'GET /trace-on HTTP/1.0', 'http.request metric - trace on');

#extract trace info
is($$spans{span0}{parent_span_id}, 'b9c7c989f97918e1',
	'traceparent - trace on');
is($$spans{span0}{trace_state}, '"congo=ucfJifl5GOE,rojo=00f067aa0ba902b7"',
	'tracestate - trace on');
is($$spans{span1}{parent_span_id}, undef, 'no traceparent - trace on');
is($$spans{span1}{trace_state}, undef, 'no tracestate - trace on');

#variables
like($tp_resp, qr/X-Otel-Trace-Id: $$spans{span0}{trace_id}/,
	'$otel_trace_id variable - trace on');
like($tp_resp, qr/X-Otel-Span-Id: $$spans{span0}{span_id}/,
	'$otel_span_id variable - trace on');
like($tp_resp, qr/X-Otel-Parent-Id: $$spans{span0}{parent_span_id}/,
	'$otel_parent_id variable - trace on');
like($tp_resp, qr/X-Otel-Parent-Sampled: 1/,
	'$otel_parent_sampled variable - trace on');
like($t_resp, qr/X-Otel-Parent-Sampled: 0/,
	'$otel_parent_sampled variable - trace on (no traceparent header)');

#trace off
isnt(get_attr("http.target", "string_value", $$spans{span0}), '/trace-off',
	'no metric in batch0 - trace off');
isnt(get_attr("http.target", "string_value", $$batch1{scope_spans}{span1}),
	'/trace-off', 'no metric in batch1 - trace off');

#trace context: ignore
unlike($t_resp_ignore, qr/X-Otel-Traceparent/,
	'no traceparent - trace context ignore (no trace heders)');
unlike($t_resp_ignore, qr/X-Otel-Tracestate/,
	'no tracestate - trace context ignore (no trace heders)');

unlike($tp_resp_ignore, qr/X-Otel-Parent-Id/,
	'no parent span id - trace context ignore (trace headers)');
like($tp_resp_ignore,
	qr/Traceparent: 00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01/,
	'traceparent - trace context ignore (trace headers)');
like($tp_resp_ignore,
	qr/Tracestate: congo=ucfJifl5GOE,rojo=00f067aa0ba902b7/,
	'tracestate - trace context ignore (trace headers)');

#trace context: extract
unlike($t_resp_extract, qr/X-Otel-Traceparent/,
	'no traceparent - trace context extract (no trace headers)');
unlike($t_resp_extract, qr/X-Otel-Tracestate/,
	'no tracestate - trace context extract (no trace headers)');

like($tp_resp_extract, qr/X-Otel-Parent-Id: b9c7c989f97918e1/,
	'parent span id - trace context extract (trace headers)');
like($tp_resp_extract,
	qr/Traceparent: 00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01/,
	'traceparent - trace context extract (trace headers)');
like($tp_resp_extract,
	qr/Tracestate: congo=ucfJifl5GOE,rojo=00f067aa0ba902b7/,
	'tracestate - trace context extract (trace headers)');

#trace context: inject
like($t_resp_inject, qr/X-Otel-Traceparent/,
	'traceparent - trace context inject (no trace headers)');

unlike($tp_resp_inject, qr/X-Otel-Parent-Id/,
	'no parent span id - trace context inject (trace headers)');

like($tp_resp_inject,
	qr/Traceparent: 00-$$spans{span7}{trace_id}-$$spans{span7}{span_id}-01/,
	'traceparent - trace context inject (trace headers)');
unlike($tp_resp_inject, qr/Tracestate:/,
	'no tracestate - trace context inject (trace headers)');

#trace context: propagate
like($t_resp_propagate,
	qr/Traceparent: 00-$$spans{span8}{trace_id}-$$spans{span8}{span_id}-01/,
	'traceparent - trace context propagate (no trace headers)');

like($tp_resp_propagate, qr/X-Otel-Parent-Id: b9c7c989f97918e1/,
	'parent id - trace context propagate (trace headers)');
like($tp_resp_propagate,
	qr/parent: 00-0af7651916cd43dd8448eb211c80319c-$$spans{span9}{span_id}-01/,
	'traceparent - trace context propagate (trace headers)');
like($tp_resp_propagate,
	qr/Tracestate: congo=ucfJifl5GOE,rojo=00f067aa0ba902b7/,
	'tracestate - trace context propagate (trace headers)');

###############################################################################

sub http_get_traceparent {
	my ($path) = @_;

	return http(<<EOF);
GET $path HTTP/1.0
Host: localhost
User-agent: nginx-tests
Traceparent: 00-0af7651916cd43dd8448eb211c80319c-b9c7c989f97918e1-01
Tracestate: congo=ucfJifl5GOE,rojo=00f067aa0ba902b7

EOF
}

sub http_get_ssl {
	my ($path) = @_;

	my $s = get_ssl_socket(8081) or return;

	return http_get($path, socket => $s);
}

sub get_ssl_socket {
	my ($port) = @_;

	return http(
		'', PeerAddr => '127.0.0.1:' . port($port), start => 1, SSL => 1
	);
}

sub get_attr {
	my($attr, $type, $obj) = @_;

	my ($res) = grep {
			$_ =~ /^attribute\d+/ && $$obj{$_}{key} eq '"' . $attr . '"'
		} keys %{$obj};

	$$obj{$res}{value}{$type} =~ s/(^\")|(\"$)//g
		if $res && $type eq 'string_value';

	return $$obj{$res}{value}{$type};
}

sub decode_protobuf {
	my ($protobuf) = @_;

	$protobuf = encode_base64($protobuf);

	open my $cmd => "echo '$protobuf' | base64 -d | " .
		'$PWD/../build/_deps/grpc-build/third_party/protobuf/protoc '.
		'--decode opentelemetry.proto.trace.v1.ResourceSpans -I ' .
		'$PWD/../build/_deps/otelcpp-src/third_party/opentelemetry-proto ' .
		'opentelemetry/proto/collector/trace/v1/trace_service.proto |'
		or die $!;

	my $out = do { local $/; <$cmd> };

	close $cmd or die $!;

	return $out;
}

sub decode_bytes {
	my ($bytes) = @_;

	my ($res, $acc) = ('', '');
	for	my $c (split //, $bytes) {
		if ($c eq "\\") {
			$res .= $acc;
			$acc = $c;
		} elsif ($acc ne '') {
			$acc .= $c;
		}

		if ($acc =~ /\\(\d{3})/) {
			$res .= chr(oct($1));
			$acc = '';
			next;
		}
		$res .= $c if ($acc eq '');
	}
	$res .= $acc;

	$res =~ s/(^\")|(\"$)//g;
	$res =~ s/\\\\/\\/g;
	$res =~ s/\\r/\r/g;
	$res =~ s/\\n/\n/g;
	$res =~ s/\\t/\t/g;
	$res =~ s/\\"/\"/g;
	$res =~ s/\\'/\'/g;

	return unpack("H*", unpack("a*", $res));
}

sub to_hash {
	my ($textdata) = @_;
	my $out;

	%{$out} = ();
	my @stack = ($out);
	my ($attr_count, $span_count) = (0, 0);
	for my $line (split /\n/, $textdata) {
		chomp $line;
		$line =~ s/^\s+//;
		if ($line =~ /\:/) {
			my ($k, $v) = split /\: /, $line;
			$v = decode_bytes($v) if ($k =~ /trace_id|span_id|parent_span_id/);
			$stack[scalar(@stack)-1]{$k} = $v;
		} elsif ($line =~ /\{/) {
			$line =~ s/\s\{//;
			$line = 'attribute' . $attr_count++ if ($line eq 'attributes');
			if ($line eq 'spans') {
				$line = 'span' . $span_count++;
				$attr_count = 0;
			}
			my $new;
			%{$new} = ();
			$stack[scalar(@stack)-1]{$line} = $new;
			push @stack, $new;
		} elsif ($line =~ /\}/) {
			pop @stack;
		}
	}

	return $out;
}

sub grpc {
	my ($server, $client, $f, $s, $c, $sid, $csid, $uri);

	$server = IO::Socket::INET->new(
		Proto => 'tcp',
		LocalHost => '127.0.0.1',
		LocalPort => $p,
		Listen => 5,
		Reuse => 1
	) or die "Can't create listening socket: $!\n";

	$f->{http_start} = sub {
		if (IO::Select->new($server)->can_read(5)) {
			$client = $server->accept();
		} else {
			# connection could be unexpectedly reused
			goto reused if $client;
			return undef;
		}

		$client->sysread(my $buf, 24) == 24 or return; # preface

		$c = Test::Nginx::HTTP2->new(1, socket => $client,
			pure => 1, preface => "") or return;

reused:
		my $frames = $c->read(all => [{ fin => 1 }]);

		return $frames;
	};

	return $f;
}

###############################################################################
