from base64 import b64decode
import os
from socket import gethostbyname, gethostname, gaierror
from subprocess import (
    CalledProcessError,
    check_call,
    check_output
)
import netifaces
import time
import platform
import json

from charmhelpers.contrib.network.ip import get_address_in_network
from charmhelpers.core.hookenv import (
    config,
    status_set,
    log,
    ERROR,
    application_version_set,
)
from charmhelpers.core.host import file_hash, write_file
from charmhelpers.core.templating import render

from docker_utils import (
    is_container_launched,
    is_container_present,
    apply_config_in_container,
    get_docker_image_id,
    load_docker_image,
    launch_docker_image,
    dpkg_version,
    docker_exec,
)

config = config()


def get_ip():
    network = config.get("control-network")
    ip = get_address_in_network(network) if network else None
    if not ip:
        ip = _get_default_ip()
    return ip


def _get_default_ip():
    if hasattr(netifaces, "gateways"):
        iface = netifaces.gateways()["default"][netifaces.AF_INET][1]
    else:
        data = check_output("ip route | grep ^default", shell=True).split()
        iface = data[data.index("dev") + 1]
    return netifaces.ifaddresses(iface)[netifaces.AF_INET][0]["addr"]


def fix_hostname():
    hostname = gethostname()
    try:
        gethostbyname(hostname)
    except gaierror:
        ip = get_ip()
        check_call(["sed", "-E", "-i", "-e",
            ("/127.0.0.1[[:blank:]]+/a \\\n" + ip + " " + hostname),
            "/etc/hosts"])


def decode_cert(key):
    val = config.get(key)
    if not val:
        return None
    try:
        return b64decode(val)
    except Exception as e:
        log("Couldn't decode certificate from config['{}']: {}".format(
            key, str(e)), level=ERROR)
    return None


def save_file(path, data, perms=0o400):
    if data:
        fdir = os.path.dirname(path)
        if not os.path.exists(fdir):
            os.makedirs(fdir)
        write_file(path, data, perms=perms)
    elif os.path.exists(path):
        os.remove(path)


def update_services_status(name, services):
    try:
        output = docker_exec(name, "contrail-status")
    except CalledProcessError as e:
        log("Container is not ready to get contrail-status: " + str(e))
        status_set("waiting", "Waiting services to run in container")
        return

    statuses = dict()
    for line in output.splitlines()[1:]:
        if len(line) == 0 or line.startswith("=="):
            continue
        lst = line.split()
        if len(lst) < 2:
            continue
        srv = lst[0].split(":")[0]
        statuses[srv] = (lst[1], " ".join(lst[2:]))
    for srv in services:
        if srv not in statuses:
            status_set("waiting", srv + " is absent in the contrail-status")
            return
        status, desc = statuses.get(srv)
        if status != "active":
            workload = "waiting" if status == "initializing" else "blocked"
            status_set(workload, "{} is not ready. Reason: {}"
                       .format(srv, desc))
            return

    status_set("active", "Unit is ready")


def check_run_prerequisites(name, config_name, update_config_func, services):
    if is_container_launched(name):
        # already launched. just sync config if needed.
        check = True
        if update_config_func and update_config_func():
            check = apply_config_in_container(name, config_name)
        if check:
            update_services_status(name, services)
        return False

    if is_container_present(name):
        status_set(
            "blocked",
            "Container is present but is not running. Run or remove it.")
        return False

    image_id = get_docker_image_id(name)
    if not image_id:
        image_id = load_docker_image(name)
        if not image_id:
            status_set("waiting", "Awaiting for container resource")
            return False

    return True


def run_container(name, pkg_to_check):
    args = []
    if platform.linux_distribution()[2].strip() == "trusty":
        args.append("--pid=host")
    launch_docker_image(name, args)

    time.sleep(5)
    version = dpkg_version(name, pkg_to_check)
    application_version_set(version)
    status_set("waiting", "Waiting services to run in container")


def json_loads(data, default=None):
    return json.loads(data) if data else default


def render_and_check(ctx, template, conf_file, do_check):
    """Returns True if configuration has been changed."""

    log("Render and store new configuration: " + conf_file)
    if do_check:
        try:
            with open(conf_file) as f:
                old_lines = set(f.readlines())
        except Exception:
            old_lines = set()

    ks_ca_path = "/etc/contrailctl/keystone-ca-cert.pem"
    ks_ca_hash = file_hash(ks_ca_path) if do_check else None
    ks_ca = ctx.get("keystone_ssl_ca")
    save_file(ks_ca_path, ks_ca, 0o444)
    ks_ca_hash_new = file_hash(ks_ca_path)
    if ks_ca:
        ctx["keystone_ssl_ca_path"] = ks_ca_path
    ca_changed = (ks_ca_hash != ks_ca_hash_new) if do_check else False
    if ca_changed:
        log("Keystone CA cert has been changed: {h1} != {h2}"
            .format(h1=ks_ca_hash, h2=ks_ca_hash_new))

    render(template, conf_file, ctx)
    if not do_check:
        return True
    with open(conf_file) as f:
        new_lines = set(f.readlines())
    new_set = new_lines.difference(old_lines)
    old_set = old_lines.difference(new_lines)
    if new_set or old_set:
        log("New lines:\n{new}".format(new="".join(new_set)))
        log("Old lines:\n{old}".format(old="".join(old_set)))
        log("Configuration file has been changed.")
    else:
        log("Configuration file has not been changed.")
    return ca_changed or new_set or old_set


def update_certificates(cert, key, ca):
    # NOTE: store files in default paths cause no way to pass this path to
    # some of components (sandesh)
    files = {"/etc/contrailctl/ssl/server.pem": cert,
             "/etc/contrailctl/ssl/server-privkey.pem": key,
             "/etc/contrailctl/ssl/ca-cert.pem": ca}
    changed = False
    for cfile in files:
        data = files[cfile]
        old_hash = file_hash(cfile)
        save_file(cfile, data)
        changed |= (old_hash != file_hash(cfile))

    return changed
