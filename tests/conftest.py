from jinja2 import Environment
import logging
from OpenSSL import crypto
import os
import pytest
import subprocess
import time


pytest_plugins = [
    "otelcol_fixtures",
]

NGINX_BINARY = os.getenv("TEST_NGINX_BINARY", "../nginx/objs/nginx")
CAPABILITIES = subprocess.check_output(
    [NGINX_BINARY, "-V"], stderr=subprocess.STDOUT
).decode("utf-8")


def self_signed_cert(test_dir):
    name = "localhost"
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)
    cert = crypto.X509()
    cert.get_subject().CN = name
    cert.set_issuer(cert.get_subject())
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365 * 86400)  # 365 days
    cert.set_pubkey(k)
    cert.sign(k, "sha512")
    (test_dir / f"{name}.key").write_text(
        crypto.dump_privatekey(crypto.FILETYPE_PEM, k).decode("utf-8")
    )
    (test_dir / f"{name}.crt").write_text(
        crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode("utf-8")
    )


@pytest.fixture(scope="session")
def logger():
    logging.basicConfig(level=logging.INFO)
    return logging.getLogger(__name__)


@pytest.fixture(scope="module")
def testdir(tmp_path_factory):
    return tmp_path_factory.mktemp("nginx")


@pytest.fixture(scope="module")
def nginx_config(request, testdir, logger):
    tmpl = Environment().from_string(request.module.NGINX_CONFIG)
    params = request.param
    params["test_globals"] = (
        f"pid {testdir}/nginx.pid;\nerror_log {testdir}/error.log debug;\n"
        + os.getenv("TEST_NGINX_GLOBALS", "")
    )
    params[
        "test_globals_http"
    ] = f"root {testdir};\naccess_log {testdir}/access.log;\n" + os.getenv(
        "TEST_NGINX_GLOBALS_HTTP", ""
    )
    params["test_globals_stream"] = os.getenv("TEST_NGINX_GLOBALS_STREAM", "")
    conf = tmpl.render(params)
    logger.debug(conf)
    return conf


@pytest.fixture(scope="module")
def nginx(testdir, nginx_config, certs, logger):
    logger.debug(CAPABILITIES)
    (testdir / "nginx.conf").write_text(nginx_config)
    args = [
        NGINX_BINARY,
        "-p",
        f"{testdir}",
        "-c",
        "nginx.conf",
        "-e",
        "error.log",
    ]
    logger.info("Starting nginx...")
    proc = subprocess.Popen(args)
    logger.debug(f"path={NGINX_BINARY}")
    logger.debug(f"args={' '.join(proc.args[1:])}")
    logger.debug(f"pid={proc.pid}")
    while not (testdir / "nginx.pid").exists():
        time.sleep(0.1)
        if proc.poll() is not None:
            raise subprocess.SubprocessError("Can't start nginx")
    yield proc
    logger.info("Stopping nginx...")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log = (testdir / "error.log").read_text()
    assert "[alert]" not in log
    if os.getenv("TEST_NGINX_CATLOG", "0") in ["1", "true"]:
        logger.debug(log)


@pytest.fixture(scope="module")
def certs(request, testdir):
    if getattr(request.module, "CERT_GEN", None) is not None:
        return eval(request.module.CERT_GEN)(testdir)
