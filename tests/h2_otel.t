#!/usr/bin/perl

# (C) Nginx, Inc.

# Tests for opentelmetry metric exporter in case HTTP/2.

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

my $t = Test::Nginx->new()
	->has(qw/http_v2 http_ssl rewrite mirror grpc socket_ssl_alpn/)
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
        endpoint 127.0.0.1:8083;
        interval 1s;
        batch_size 10;
        batch_count 1;
    }

    otel_service_name test_server;
    otel_trace on;

    server {
        listen       127.0.0.1:8080 http2;
        listen       127.0.0.1:8081;
        listen       127.0.0.1:8082 http2 ssl;
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

    server {
        listen       127.0.0.1:8083 http2;
        server_name  localhost;
        otel_trace off;

        location / {
            mirror /mirror;
            grpc_pass 127.0.0.1:8084;
        }

        location /mirror {
            internal;
            grpc_pass 127.0.0.1:%%PORT_4317%%;
        }
    }

    server {
        listen       127.0.0.1:8084 http2;
        server_name  localhost;
        otel_trace off;

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

$t->try_run('no OTEL module')->plan(64);

###############################################################################

my $p = port(4317);
my $f = grpc();

#do requests
my $t_off_resp;
($_, $t_off_resp) = http2_get('/trace-off');

#batch0 (10 requests)
my ($t_headers_ignore, $tp_headers_ignore, $t_headers_extract,
	$tp_headers_extract, $t_headers_inject, $tp_headers_inject,
	$t_headers_propagate, $tp_headers_propagate);

my ($tp_headers, $tp_resp) = http2_get('/trace-on', trace_headers => 1);
my ($t_headers, $t_resp) = http2_get('/trace-on', ssl => 1);

($t_headers_ignore, $_) = http2_get('/context-ignore');
($tp_headers_ignore, $_) = http2_get('/context-ignore', trace_headers => 1);
($t_headers_extract, $_) = http2_get('/context-extract');
($tp_headers_extract, $_) = http2_get('/context-extract', trace_headers => 1);
($t_headers_inject, $_) = http2_get('/context-inject');
($tp_headers_inject, $_) = http2_get('/context-inject', trace_headers => 1);
($t_headers_propagate, $_) = http2_get('/context-propagate');
($tp_headers_propagate, $_) =
	http2_get('/context-propagate', trace_headers => 1);

my $frames = $f->{http_start}();
my ($frame) = grep { $_->{type} eq "DATA" } @$frames;
my $batch0 = to_hash(decode_protobuf(substr($frame->{data}, 8)));

my $spans = $$batch0{scope_spans};

#batch1 (5 reqeusts)
http2_get('/trace-on') for (1..5);

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
	$$batch0{resource}),
	'test_server', 'service.name - trace on');
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
is(get_attr("http.flavor", "string_value", $$spans{span0}), '2.0',
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
is(get_attr("http.flavor", "string_value", $$spans{span1}), '2.0',
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
is(get_attr("net.host.port", "int_value", $$spans{span1}), 8082,
	'net.host.port metric - trace on (https)');
is(get_attr("net.sock.peer.addr", "string_value", $$spans{span1}), '127.0.0.1',
	'net.sock.peer.addr metric - trace on (https)');
like(get_attr("net.sock.peer.port", "int_value", $$spans{span1}), qr/\d+/,
	'net.sock.peer.port metric - trace on (https)');

#validate custom http metrics
is(${get_attr("http.request.header.completion", "array_value", $$spans{span0})}
	{values}{string_value}, '"OK"',
	'http.request.header.completion metric - trace on');
is(${get_attr(
		"http.response.header.content.type", "array_value",$$spans{span0}
	)}{values}{string_value}, '"text/plain"',
	'http.response.header.content.type metric - trace on');
is(get_attr("http.request", "string_value", $$spans{span0}),
	'GET /trace-on HTTP/2.0', 'http.request metric - trace on');

#extract trace info
is($$spans{span0}{parent_span_id}, 'b9c7c989f97918e1', 'traceparent - trace on');
is($$spans{span0}{trace_state}, '"congo=ucfJifl5GOE,rojo=00f067aa0ba902b7"',
	'tracestate - trace on');
is($$spans{span1}{parent_span_id}, undef, 'no traceparent - trace on');
is($$spans{span1}{trace_state}, undef, 'no tracestate - trace on');

#variables
is($tp_headers->{'x-otel-trace-id'}, $$spans{span0}{trace_id},
	'$otel_trace_id variable - trace on');
is($tp_headers->{'x-otel-span-id'}, $$spans{span0}{span_id},
	'$otel_span_id variable - trace on');
is($tp_headers->{'x-otel-parent-id'}, $$spans{span0}{parent_span_id},
	'$otel_parent_id variable - trace on');
is($tp_headers->{'x-otel-parent-sampled'}, 1,
	'$otel_parent_sampled variable - trace on');
is($t_headers->{'x-otel-parent-sampled'}, 0,
	'$otel_parent_sampled variable - trace on (no traceparent header)');

#trace off
isnt(get_attr("http.target", "string_value", $$spans{span0}), '/trace-off',
	'no metric - trace off');

#trace context: ignore
is($t_headers_ignore->{'x-otel-traceparent'}, undef,
	'no traceparent - trace context ignore (no trace heders)');
is($t_headers_ignore->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context ignore (no trace heders)');

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

is($tp_headers_inject->{'x-otel-parent-id'}, undef,
	'no parent span id - trace context inject (trace headers)');

is($tp_headers_inject->{'x-otel-traceparent'},
	"00-$$spans{span7}{trace_id}-$$spans{span7}{span_id}-01",
	'traceparent - trace context inject (trace headers)');
is($tp_headers_inject->{'x-otel-tracestate'}, undef,
	'no tracestate - trace context inject (trace headers)');

#trace context: propagate
is($t_headers_propagate->{'x-otel-traceparent'},
	"00-$$spans{span8}{trace_id}-$$spans{span8}{span_id}-01",
	'traceparent - trace context propagate (no trace headers)');

is($tp_headers_propagate->{'x-otel-parent-id'}, 'b9c7c989f97918e1',
	'parent id - trace context propagate (trace headers)');
is($tp_headers_propagate->{'x-otel-traceparent'},
	"00-0af7651916cd43dd8448eb211c80319c-$$spans{span9}{span_id}-01",
	'traceparent - trace context propagate (trace headers)');
is($tp_headers_propagate->{'x-otel-tracestate'},
	'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
	'tracestate - trace context propagate (trace headers)');

###############################################################################

sub http2_get {
	my ($path, %extra) = @_;
	my ($frames, $frame);

	my $s = $extra{ssl}
		? Test::Nginx::HTTP2->new(
			undef, socket => get_ssl_socket(8082, ['h2']))
		: Test::Nginx::HTTP2->new();

	my $sid = $extra{trace_headers}
		? $s->new_stream({ headers => [
			{ name => ':method', value => 'GET' },
			{ name => ':scheme', value => 'http' },
			{ name => ':path', value => $path },
			{ name => ':authority', value => 'localhost' },
			{ name => 'user-agent', value => 'nginx-tests', mode => 2 },
			{ name => 'traceparent',
				value => '00-0af7651916cd43dd8448eb211c80319c-' .
					'b9c7c989f97918e1-01',
				mode => 2
			},
			{ name => 'tracestate',
				value => 'congo=ucfJifl5GOE,rojo=00f067aa0ba902b7',
				mode => 2
			}]})
		: $s->new_stream({ path => $path });
	$frames = $s->read(all => [{ sid => $sid, fin => 1 }]);

	($frame) = grep { $_->{type} eq "HEADERS" } @$frames;
	my $headers = $frame->{headers};

	($frame) = grep { $_->{type} eq "DATA" } @$frames;
	my $data = $frame->{data};

	return $headers, $data;
}

sub get_ssl_socket {
	my ($port, $alpn) = @_;

	return http(
		'', PeerAddr => '127.0.0.1:' . port($port), start => 1,
		SSL => 1,
		SSL_alpn_protocols => $alpn,
		SSL_error_trap => sub { die $_[1] }
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
