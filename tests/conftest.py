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


def pytest_configure():
    pytest.module_capsys = None


def pytest_addoption(parser):
    parser.addoption("--nginx", required=True)
    parser.addoption("--module", required=True)
    parser.addoption("--showlog", action="store_true")
    parser.addoption("--otelcol")
    parser.addoption("--globals", default="")


def self_signed_cert(test_dir, name):
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


@pytest.fixture(autouse=True)
def populate_capsys(capsys):
    pytest.module_capsys = capsys


@pytest.fixture(scope="module")
def testdir(tmp_path_factory):
    return tmp_path_factory.mktemp("nginx")


@pytest.fixture(scope="module")
def nginx_config(request, pytestconfig, testdir, logger):
    tmpl = jinja2.Environment().from_string(request.module.NGINX_CONFIG)
    params = request.param
    params["globals"] = (
        f"pid {testdir}/nginx.pid;\n"
        + f"error_log {testdir}/error.log notice;\n"
        + f"load_module {os.path.abspath(pytestconfig.option.module)};\n"
        + pytestconfig.option.globals
    )
    params["http_globals"] = f"root {testdir};\n" + "access_log off;\n"
    conf = tmpl.render(params)
    logger.debug(conf)
    return conf


@pytest.fixture(scope="module")
def nginx(testdir, pytestconfig, nginx_config, _certs, logger):
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
    if pytestconfig.option.showlog:
        with pytest.module_capsys.disabled():
            print((testdir / "error.log").read_text())
    (testdir / "error.log").unlink()


@pytest.fixture(scope="module")
def _certs(request, testdir):
    if getattr(request.module, "CERTS", None) is not None:
        for name in request.module.CERTS[1:]:
            request.module.CERTS[0](testdir, name)
