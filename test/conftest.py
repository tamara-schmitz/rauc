import json
import os
import shutil
import signal
import subprocess
import time
from functools import cache
from configparser import ConfigParser
from random import Random

import pytest
from pydbus import SessionBus

from helper import run

# Avoid fastcopy via sendfile() which seems to cause troubles with the 9pfs and
# the current kernel used in our qemu-test:
#
# | WARNING: CPU: 1 PID: 6623 at lib/iov_iter.c:1201 iov_iter_pipe+0x31/0x40
#
# Manually override _USE_CP_SENDFILE as suggested here:
# https://github.com/python/cpython/issues/87909#issuecomment-1093909363
# and originally defined here:
# https://github.com/python/cpython/blob/3.12/Lib/shutil.py#L51
shutil._USE_CP_SENDFILE = False


meson_build = os.environ.get("MESON_BUILD_DIR")
if not meson_build:
    raise Exception("Please set MESON_BUILD_DIR to point to the meson build directory.")
if not os.path.isabs(meson_build):
    meson_build = os.path.abspath(meson_build)


@pytest.fixture(scope="session")
def monkeysession():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(scope="session", autouse=True)
def env_setup(monkeysession):
    monkeysession.setenv("PATH", meson_build, prepend=os.pathsep)
    monkeysession.setenv("LC_ALL", "C")
    monkeysession.setenv("TZ", "UTC")
    monkeysession.setenv("DBUS_STARTER_BUS_TYPE", "session")

    os.chdir(f"{os.path.dirname(os.path.abspath(__file__))}")


@cache
def meson_buildoptions():
    with open(os.path.join(meson_build, "meson-info/intro-buildoptions.json")) as f:
        data = json.loads(f.read())

    return {o["name"]: o for o in data}


@cache
def string_in_config_h(findstring):
    with open(os.path.join(meson_build, "config.h")) as f:
        if findstring in f.read():
            return True
    return False


have_json = pytest.mark.skipif(not string_in_config_h("ENABLE_JSON 1"), reason="No json")


root = pytest.mark.skipif(os.geteuid() != 0, reason="Not root")


def _have_grub():
    out, err, exitcode = run("grub-editenv -V")
    return exitcode == 0


have_grub = pytest.mark.skipif(not _have_grub(), reason="Have no grub-editenv")


def _have_openssl():
    out, err, exitcode = run("openssl version")
    return exitcode == 0


have_openssl = pytest.mark.skipif(not _have_openssl(), reason="Have no OPENSSL")


def _have_casync():
    try:
        out, err, exitcode = run("casync --version")
        return exitcode == 0
    except Exception:
        return False


have_casync = pytest.mark.skipif(not _have_casync(), reason="Have no casync")


def _have_desync():
    try:
        out, err, exitcode = run("desync --help")
        return exitcode == 0
    except Exception:
        return False


have_desync = pytest.mark.skipif(not _have_desync(), reason="Have no desync")


def _have_faketime():
    # faketime is not compatible with sanitizers:
    # https://github.com/wolfcw/libfaketime/issues/412#issuecomment-1293686539
    b_sanitize = meson_buildoptions().get("b_sanitize", {})
    if b_sanitize.get("value", "none") != "none":
        print("faketime not compatible with sanitizers")
        return False

    try:
        out, err, exitcode = run('faketime "2018-01-01" date')
        if exitcode != 0:
            return False

        # On some platforms faketime is broken, see e.g. https://github.com/wolfcw/libfaketime/issues/418
        out, err, exitcode = run('faketime "2018-01-01" date -R')
        if "Jan 2018" not in out:
            return False

        return exitcode == 0
    except Exception:
        return False


have_faketime = pytest.mark.skipif(not _have_faketime(), reason="Have no faketime")


have_http = pytest.mark.skipif("RAUC_TEST_HTTP_SERVER" not in os.environ, reason="Have no HTTP")


have_streaming = pytest.mark.skipif(
    "RAUC_TEST_HTTP_SERVER" not in os.environ or not string_in_config_h("ENABLE_STREAMING 1"),
    reason="Have no streaming",
)


no_service = pytest.mark.skipif(string_in_config_h("ENABLE_SERVICE 1"), reason="Have service")


def have_service():
    return string_in_config_h("ENABLE_SERVICE 1")


ca_dev = "openssl-ca/dev"
ca_rel = "openssl-ca/rel"


def prepare_softhsm2(tmp_path, softhsm2_mod):
    softhsm2_conf = tmp_path / "softhsm2.conf"
    softhsm2_dir = tmp_path / "softhsm2.tokens"

    os.environ["SOFTHSM2_CONF"] = str(softhsm2_conf)
    os.environ["SOFTHSM2_DIR"] = str(softhsm2_dir)

    with open(softhsm2_conf, mode="w") as f:
        f.write(f"directories.tokendir = {softhsm2_dir}")

    os.mkdir(softhsm2_dir)

    out, err, exitcode = run(f"pkcs11-tool --module {softhsm2_mod} --init-token --label rauc --so-pin 0000")
    assert exitcode == 0
    out, err, exitcode = run(f"pkcs11-tool --module {softhsm2_mod} -l --so-pin 0000 --new-pin 1111 --init-pin")
    assert exitcode == 0

    out, err, exitcode = run("p11-kit list-modules")
    assert exitcode == 0

    out, err, exitcode = run("openssl engine pkcs11 -tt -vvvv")
    assert exitcode == 0

    proc = subprocess.run(
        f"openssl x509 -in {ca_dev}/autobuilder-1.cert.pem -inform pem -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y cert -w /proc/self/fd/0 "
        "--label autobuilder-1 --id 01",
        shell=True,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        f"openssl rsa -in {ca_dev}/private/autobuilder-1.pem -inform pem -pubout -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y pubkey -w /proc/self/fd/0 "
        "--label autobuilder-1 --id 01",
        shell=True,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        f"openssl rsa -in {ca_dev}/private/autobuilder-1.pem -inform pem -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y privkey -w /proc/self/fd/0 "
        "--label autobuilder-1 --id 01",
        shell=True,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        f"openssl x509 -in {ca_dev}/autobuilder-2.cert.pem -inform pem -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y cert -w /proc/self/fd/0 "
        "--label autobuilder-2 --id 02",
        shell=True,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        f"openssl rsa -in {ca_dev}/private/autobuilder-2.pem -inform pem -pubout -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y pubkey -w /proc/self/fd/0 "
        "--label autobuilder-2 --id 02",
        shell=True,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        f"openssl rsa -in {ca_dev}/private/autobuilder-2.pem -inform pem -outform der "
        f"| pkcs11-tool --module {softhsm2_mod} -l --pin 1111 -y privkey -w /proc/self/fd/0 "
        "--label autobuilder-2 --id 02",
        shell=True,
    )
    assert proc.returncode == 0

    out, err, exitcode = run(f"pkcs11-tool --module {softhsm2_mod} -l --pin 1111 --list-objects")
    assert exitcode == 0

    os.environ["RAUC_PKCS11_PIN"] = "1111"
    # setting the module is needed only if p11-kit doesn't work
    os.environ["RAUC_PKCS11_MODULE"] = softhsm2_mod


@pytest.fixture(scope="session")
def pkcs11(tmp_path_factory):
    if os.path.exists("/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so"):
        softhsm2_mod = "/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so"
    else:
        softhsm2_mod = "/usr/lib/softhsm/libsofthsm2.so"
    if not os.path.exists(softhsm2_mod):
        pytest.skip("libsofthsm2.so not available on system")

    prepare_softhsm2(tmp_path_factory.mktemp("blub"), softhsm2_mod)


@pytest.fixture(scope="session")
def dbus_session_bus():
    if not have_service():
        pytest.skip("No service")

    # Run the dbus-launch command and capture its output
    output = subprocess.check_output(["dbus-launch", "--sh-syntax"], universal_newlines=True, shell=True)

    # Parse the output to extract environment variable assignments
    env_vars = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env_vars[key] = value

    # Set the environment variables in the current process's environment
    for key, value in env_vars.items():
        os.environ[key] = value

    dbus_session_bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert dbus_session_bus_address
    print(f"DBUS_SESSION_BUS_ADDRESS: {dbus_session_bus_address}")

    yield

    pid = os.environ["DBUS_SESSION_BUS_PID"]
    print(f"Killing PID {pid}")
    os.kill(int(pid), signal.SIGTERM)


@pytest.fixture
def create_system_files(env_setup, tmp_path):
    os.mkdir(tmp_path / "images")
    open(tmp_path / "images/rootfs-0", mode="w").close()
    open(tmp_path / "images/rootfs-1", mode="w").close()
    open(tmp_path / "images/appfs-0", mode="w").close()
    open(tmp_path / "images/appfs-1", mode="w").close()
    os.symlink(os.path.abspath("bin"), tmp_path / "bin")
    os.symlink(os.path.abspath("openssl-ca"), tmp_path / "openssl-ca")
    os.symlink(os.path.abspath("openssl-enc"), tmp_path / "openssl-enc")


@pytest.fixture
def rauc_no_service(create_system_files, tmp_path):
    tmp_conf_file = tmp_path / "system.conf"
    shutil.copy("test.conf", tmp_conf_file)

    return f"rauc -c {tmp_conf_file}"


def _rauc_dbus_service(tmp_path, conf_file, bootslot):
    tmp_conf_file = tmp_path / "system.conf"
    shutil.copy(conf_file, tmp_conf_file)

    env = os.environ.copy()
    env["RAUC_PYTEST_TMP"] = str(tmp_path)

    service = subprocess.Popen(
        f"rauc service --conf={tmp_conf_file} --mount={tmp_path}/mnt --override-boot-slot={bootslot}".split(),
        env=env,
    )

    bus = SessionBus()
    proxy = None

    # Wait for de.pengutronix.rauc to appear on the bus
    timeout = time.monotonic() + 5.0
    while True:
        time.sleep(0.1)
        try:
            proxy = bus.get("de.pengutronix.rauc", "/")
            break
        except Exception:
            if time.monotonic() > timeout:
                raise

    return service, proxy


@pytest.fixture
def rauc_dbus_service(tmp_path, dbus_session_bus, create_system_files):
    service, bus = _rauc_dbus_service(tmp_path, "minimal-test.conf", "system0")

    yield bus

    service.kill()
    service.wait()


@pytest.fixture
def rauc_dbus_service_with_system(tmp_path, dbus_session_bus, create_system_files):
    service, bus = _rauc_dbus_service(tmp_path, "minimal-test.conf", "system0")

    yield bus

    service.kill()
    service.wait()


@pytest.fixture
def rauc_dbus_service_with_system_crypt(tmp_path, dbus_session_bus, create_system_files):
    service, bus = _rauc_dbus_service(tmp_path, "crypt-test.conf", "system0")

    yield bus

    service.kill()
    service.wait()


@pytest.fixture
def rauc_dbus_service_with_system_external(tmp_path, dbus_session_bus, create_system_files):
    service, bus = _rauc_dbus_service(tmp_path, "crypt-test.conf", "_external_")

    yield bus

    service.kill()
    service.wait()


@pytest.fixture
def rauc_dbus_service_with_system_adaptive(tmp_path, dbus_session_bus, create_system_files):
    service, bus = _rauc_dbus_service(tmp_path, "adaptive-test.conf", "system0")

    yield bus

    service.kill()
    service.wait()


class Bundle:
    def __init__(self, tmp_path, name):
        self.tmp_path = tmp_path
        self.output = tmp_path / name
        self.content = tmp_path / "install-content"

        assert not self.output.exists()
        assert not self.content.exists()
        self.content.mkdir()

        # default manifest
        self.manifest = ConfigParser()
        self.manifest["update"] = {
            "compatible": "Test Config",
            "version": "2011.03-2",
        }
        self.manifest["bundle"] = {
            "format": "verity",
        }

        # some padding
        self._make_random_file(self.content / "padding", 4096, "fixed padding")

    def _make_random_file(self, path, size, seed):
        rand = Random(seed)

        with open(path, "wb") as f:
            f.write(bytes(rand.getrandbits(8) for _ in range(size)))

    def make_random_image(self, image_name, size, seed):
        path = self.content / self.manifest[f"image.{image_name}"]["filename"]
        self._make_random_file(path, size, seed)

    def add_hook_script(self, hook_script):
        path = self.content / "hook.sh"
        with open(path, "w") as f:
            f.write(hook_script)
        path.chmod(0o755)

        self.manifest["hooks"] = {
            "filename": "hook.sh",
        }

    def build(self):
        with open(self.content / "manifest.raucm", "w") as f:
            self.manifest.write(f, space_around_delimiters=False)

        out, err, exitcode = run(
            "rauc bundle "
            "--cert openssl-ca/dev/autobuilder-1.cert.pem "
            "--key openssl-ca/dev/private/autobuilder-1.pem "
            f"{self.content} {self.output}"
        )
        assert exitcode == 0
        assert "Creating 'verity' format bundle" in out
        assert self.output.is_file()


@pytest.fixture
def bundle(tmp_path):
    bundle = Bundle(tmp_path, "test.raucb")

    yield bundle
