#pragma once

#include <opentelemetry/nostd/string_view.h>

typedef opentelemetry::nostd::string_view StrView;

inline bool startsWith(StrView str, StrView prefix)
{
    return str.substr(0, prefix.size()) == prefix;
}
