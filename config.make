cat << END >> $NGX_MAKEFILE

modules: ngx_otel_module

ngx_otel_module:
	\$(MAKE) -C $NGX_OBJS/otel

.PHONY: ngx_otel_module

END
