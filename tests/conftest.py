from jinja2 import Environment
from logging import basicConfig, getLogger, INFO
from OpenSSL import crypto
from os import path
import pytest
from subprocess import (
    check_output,
    Popen,
    STDOUT,
    SubprocessError,
    TimeoutExpired,
)
from time import sleep


pytest_plugins = [
    "mock_fixtures",
]


def pytest_addoption(parser):
    parser.addoption("--nginx-binary", dest="NGX", default="nginx/objs/nginx")
    parser.addoption("--nginx-catlog", dest="CATLOG", default="0")
    parser.addoption(
        "--module-path",
        dest="MODULE_PATH",
        default="build/ngx_otel_module.so",
    )
    parser.addoption("--otelcol-binary", dest="OTELCOL", default="./otelcol")


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
    basicConfig(level=INFO)
    return getLogger(__name__)


@pytest.fixture(scope="module")
def testdir(tmp_path_factory):
    return tmp_path_factory.mktemp("nginx")


@pytest.fixture(scope="module", autouse=True)
def _errorlog(request, logger, testdir):
    yield
    if request.session.testsfailed and (testdir / "error.log").exists():
        logger.debug((testdir / "error.log").read_text())


@pytest.fixture(scope="module")
def nginx_config(request, pytestconfig, testdir, logger):
    tmpl = Environment().from_string(request.module.NGINX_CONFIG)
    params = request.param
    params["globals"] = (
        f"pid {testdir}/nginx.pid;\nerror_log {testdir}/error.log debug;\n"
        + "load_module "
        + (
            f"{pytestconfig.option.MODULE_PATH};\n"
            if path.isabs(pytestconfig.option.MODULE_PATH)
            else f"{pytestconfig.rootdir}/{pytestconfig.option.MODULE_PATH};\n"
        )
    )
    params["globals_http"] = (
        f"root {testdir};\naccess_log {testdir}/access.log;\n"
    )
    conf = tmpl.render(params)
    logger.debug(conf)
    return conf


@pytest.fixture(scope="module")
def nginx(testdir, pytestconfig, nginx_config, _certs, logger):
    assert path.exists(pytestconfig.option.NGX), "No nginx binary found"
    logger.debug(
        check_output([pytestconfig.option.NGX, "-V"], stderr=STDOUT).decode(
            "utf-8"
        )
    )
    (testdir / "nginx.conf").write_text(nginx_config)
    logger.info("Starting nginx...")
    proc = Popen(
        [
            pytestconfig.option.NGX,
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
        sleep(0.1)
        if proc.poll() is not None:
            raise SubprocessError("Can't start nginx")
    yield proc
    logger.info("Stopping nginx...")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except TimeoutExpired:
        proc.kill()
    assert (testdir / "error.log").exists(), "no error.log"
    log = (testdir / "error.log").read_text()
    assert "[alert]" not in log, "[alert] in error.log"
    if pytestconfig.option.CATLOG in ["1", "true"]:
        logger.debug(log)


@pytest.fixture(scope="module")
def _certs(request, testdir):
    if getattr(request.module, "CERTS", None) is not None:
        for name in request.module.CERTS[1:]:
            request.module.CERTS[0](testdir, name)
