if [ ! "`which protoc 2>/dev/null`" ]; then
    echo "Need to install protoc."
    exit 2
else
    PROTOC=`which protoc`
fi

if [ ! "`which grpc_cpp_plugin 2>/dev/null`" ]; then
    echo "Need to install grpc tools."
    exit 2
else
    GRPC_CPP=`which grpc_cpp_plugin`
fi

mkdir -p objs

if [ -z $NGX_OTEL_PROTO_DIR ]; then
    echo "Need to set \$NGX_OTEL_PROTO_DIR variable."
    exit 2
fi

if [ ! -d $NGX_OTEL_PROTO_DIR ]; then
    echo "\$NGX_OTEL_PROTO_DIR is set to unavailable directory."
    exit 2
fi

find $NGX_OTEL_PROTO_DIR/opentelemetry/proto -type f -name '*.proto' | \
    xargs $PROTOC \
        --proto_path $NGX_OTEL_PROTO_DIR \
        --cpp_out=objs \
        --grpc_out=objs \
        --plugin=protoc-gen-grpc=$GRPC_CPP

find objs -name '*.pb.cc' | \
    xargs sed -i.bak -e "/    ::PROTOBUF_NAMESPACE_ID::internal::WireFormatLite::VerifyUtf8String(/,/);/d"

for src_file in $OTEL_NGX_SRCS; do
    obj_file="$NGX_OBJS/addon/src/`basename $src_file .cpp`.o"
    echo "$obj_file : CFLAGS += $CXXFLAGS -Wno-missing-field-initializers -Wno-conditional-uninitialized -fPIC -fvisibility=hidden -DHAVE_ABSEIL -Dngx_otel_module_EXPORTS" >> $NGX_MAKEFILE
done

for src_file in $OTEL_NGX_SRCS; do
    obj_file="$NGX_OBJS/addon/v1/`basename $src_file .cc`.o"
    echo "$obj_file : CFLAGS += $CXXFLAGS -Wno-missing-field-initializers -Wno-conditional-uninitialized -fPIC -fvisibility=hidden -DHAVE_ABSEIL -Dngx_otel_module_EXPORTS" >> $NGX_MAKEFILE
done
