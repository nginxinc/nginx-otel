cmake_minimum_required(VERSION 3.16.3)
project(nginx-otel)

set(NGX_OTEL_NGINX_BUILD_DIR ""
    CACHE PATH "Nginx build (objs) dir")
set(NGX_OTEL_NGINX_DIR "${NGX_OTEL_NGINX_BUILD_DIR}/.."
    CACHE PATH "Nginx source dir")

set(NGX_OTEL_FETCH_DEPS ON CACHE BOOL "Download dependencies")
set(NGX_OTEL_PROTO_DIR  "" CACHE PATH "OTel proto files root")

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

set(CMAKE_CXX_VISIBILITY_PRESET hidden)

if(NGX_OTEL_FETCH_DEPS)
    include(FetchContent)

    FetchContent_Declare(
        grpc
        GIT_REPOSITORY https://github.com/grpc/grpc
        GIT_TAG        b521d4a3177fa556df749365cc31232d06ba437a # v1.48.4
        GIT_SUBMODULES third_party/protobuf third_party/abseil-cpp
        GIT_SHALLOW    ON)

    set(gRPC_USE_PROTO_LITE ON  CACHE INTERNAL "")
    set(gRPC_INSTALL        OFF CACHE INTERNAL "")
    set(gRPC_CARES_PROVIDER package CACHE INTERNAL "")
    set(gRPC_RE2_PROVIDER   package CACHE INTERNAL "")
    set(gRPC_SSL_PROVIDER   package CACHE INTERNAL "")
    set(gRPC_ZLIB_PROVIDER  package CACHE INTERNAL "")

    FetchContent_Declare(
        otelcpp
        GIT_REPOSITORY https://github.com/open-telemetry/opentelemetry-cpp
        GIT_TAG        11d5d9e0d8fd8ba876c8994714cc2647479b6574 # v1.11.0
        GIT_SUBMODULES third_party/opentelemetry-proto
        GIT_SHALLOW    ON)

    set(BUILD_TESTING OFF CACHE INTERNAL "")
    set(WITH_EXAMPLES OFF CACHE INTERNAL "")

    set(CMAKE_POSITION_INDEPENDENT_CODE ON)
    set(CMAKE_POLICY_DEFAULT_CMP0063 NEW)

    FetchContent_MakeAvailable(grpc otelcpp)

    set_property(DIRECTORY ${grpc_SOURCE_DIR}
                 PROPERTY EXCLUDE_FROM_ALL YES)
    set_property(DIRECTORY ${otelcpp_SOURCE_DIR}
                 PROPERTY EXCLUDE_FROM_ALL YES)

    if(NOT NGX_OTEL_PROTO_DIR)
        set(NGX_OTEL_PROTO_DIR
            "${otelcpp_SOURCE_DIR}/third_party/opentelemetry-proto")
    endif()

    add_library(opentelemetry-cpp::trace ALIAS opentelemetry_trace)
    add_library(gRPC::grpc++ ALIAS grpc++)
    add_executable(gRPC::grpc_cpp_plugin ALIAS grpc_cpp_plugin)
else()
    find_package(opentelemetry-cpp REQUIRED)
    find_package(protobuf REQUIRED)
    find_package(gRPC REQUIRED)
endif()

set(PROTO_DIR ${NGX_OTEL_PROTO_DIR})
set(PROTOS
    "${PROTO_DIR}/opentelemetry/proto/common/v1/common.proto"
    "${PROTO_DIR}/opentelemetry/proto/resource/v1/resource.proto"
    "${PROTO_DIR}/opentelemetry/proto/trace/v1/trace.proto"
    "${PROTO_DIR}/opentelemetry/proto/collector/trace/v1/trace_service.proto")

set(PROTO_OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}")
set(PROTO_SOURCES
    "${PROTO_OUT_DIR}/opentelemetry/proto/common/v1/common.pb.cc"
    "${PROTO_OUT_DIR}/opentelemetry/proto/resource/v1/resource.pb.cc"
    "${PROTO_OUT_DIR}/opentelemetry/proto/trace/v1/trace.pb.cc"
    "${PROTO_OUT_DIR}/opentelemetry/proto/collector/trace/v1/trace_service.pb.cc"
    "${PROTO_OUT_DIR}/opentelemetry/proto/collector/trace/v1/trace_service.grpc.pb.cc")

# generate protobuf code for lite runtime
add_custom_command(
    OUTPUT ${PROTO_SOURCES}
    COMMAND protobuf::protoc
        --proto_path ${PROTO_DIR}
        --cpp_out lite:${PROTO_OUT_DIR}
        --grpc_out ${PROTO_OUT_DIR}
        --plugin protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>
        ${PROTOS}
    # remove inconsequential UTF8 check during serialization to aid performance
    COMMAND sed -i.bak
        -e [[/    ::PROTOBUF_NAMESPACE_ID::internal::WireFormatLite::VerifyUtf8String(/,/);/d]]
        ${PROTO_SOURCES}
    DEPENDS ${PROTOS} protobuf::protoc gRPC::grpc_cpp_plugin
    VERBATIM)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_EXTENSIONS OFF)

add_compile_options(-Wall -Wtype-limits -Werror)

add_library(ngx_otel_module MODULE
    src/http_module.cpp
    src/modules.c
    ${PROTO_SOURCES})

# avoid 'lib' prefix in binary name
set_target_properties(ngx_otel_module PROPERTIES PREFIX "")

# can't use OTel's WITH_ABSEIL until cmake 3.24, as it triggers find_package()
target_compile_definitions(ngx_otel_module PRIVATE HAVE_ABSEIL)

target_include_directories(ngx_otel_module PRIVATE
    ${NGX_OTEL_NGINX_BUILD_DIR}
    ${NGX_OTEL_NGINX_DIR}/src/core
    ${NGX_OTEL_NGINX_DIR}/src/event
    ${NGX_OTEL_NGINX_DIR}/src/event/modules
    ${NGX_OTEL_NGINX_DIR}/src/event/quic
    ${NGX_OTEL_NGINX_DIR}/src/os/unix
    ${NGX_OTEL_NGINX_DIR}/src/http
    ${NGX_OTEL_NGINX_DIR}/src/http/modules
    ${NGX_OTEL_NGINX_DIR}/src/http/v2
    ${NGX_OTEL_NGINX_DIR}/src/http/v3
    ${PROTO_OUT_DIR})

target_link_libraries(ngx_otel_module
    opentelemetry-cpp::trace
    gRPC::grpc++)
