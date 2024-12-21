import jinja2
import logging
from OpenSSL import crypto
import os
import pytest
import subprocess
import time


pytest_plugins = [
    "trace_service",
]


def pytest_addoption(parser):
    parser.addoption("--nginx", required=True)
    parser.addoption("--module", required=True)
    parser.addoption("--otelcol")
    parser.addoption("--globals", default="")


def self_signed_cert(name):
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)
    cert = crypto.X509()
    cert.get_subject().CN = name
    cert.set_issuer(cert.get_subject())
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365 * 86400)  # 365 days
    cert.set_pubkey(k)
    cert.sign(k, "sha512")
    return (
        crypto.dump_privatekey(crypto.FILETYPE_PEM, k),
        crypto.dump_certificate(crypto.FILETYPE_PEM, cert),
    )


@pytest.fixture(scope="session")
def logger():
    logging.basicConfig(level=logging.INFO)
    return logging.getLogger(__name__)


@pytest.fixture(scope="module")
def testdir(tmp_path_factory):
    return tmp_path_factory.mktemp("nginx")


@pytest.fixture(scope="module")
def nginx_config(request, pytestconfig, testdir, logger):
    tmpl = jinja2.Environment().from_string(request.module.NGINX_CONFIG)
    params = getattr(request, "param", {})
    params["globals"] = (
        f"pid {testdir}/nginx.pid;\n"
        + "error_log stderr info;\n"
        + f"error_log {testdir}/error.log info;\n"
        + f"load_module {os.path.abspath(pytestconfig.option.module)};\n"
        + pytestconfig.option.globals
    )
    params["http_globals"] = f"root {testdir};\n" + "access_log off;\n"
    conf = tmpl.render(params)
    logger.debug(conf)
    return conf


@pytest.fixture(scope="module")
def nginx(testdir, pytestconfig, nginx_config, cert, logger, otelcol):
    (testdir / "nginx.conf").write_text(nginx_config)
    logger.info("Starting nginx...")
    proc = subprocess.Popen(
        [
            pytestconfig.option.nginx,
            "-p",
            str(testdir),
            "-c",
            "nginx.conf",
            "-e",
            "error.log",
        ]
    )
    logger.debug(f"args={' '.join(proc.args)}")
    logger.debug(f"pid={proc.pid}")
    while not (testdir / "nginx.pid").exists():
        time.sleep(0.1)
        assert proc.poll() is None, "Can't start nginx"
    yield proc
    logger.info("Stopping nginx...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    assert "[alert]" not in (testdir / "error.log").read_text()


@pytest.fixture(scope="module")
def cert(testdir):
    key, cert = self_signed_cert("localhost")
    (testdir / "localhost.key").write_text(key.decode("utf-8"))
    (testdir / "localhost.crt").write_text(cert.decode("utf-8"))
    yield (key, cert)
