"""Linux host inspection. Usage: python tools/check-proxy-deployment.py MAIN_ID PROXY_ID.

Does not print docker inspect environment values or credential file contents.
Requires Docker access and permission to read the iptables rules.
"""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys


def docker_json(*args):
    return json.loads(subprocess.check_output(["docker", *args], text=True))


def require(condition, message):
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        raise SystemExit(1)


main, proxy = (docker_json("inspect", cid)[0] for cid in sys.argv[1:3])
main_config, proxy_config = main["HostConfig"], proxy["HostConfig"]
main_nets, proxy_nets = main["NetworkSettings"]["Networks"], proxy["NetworkSettings"]["Networks"]
require(len(main_nets) == 1, "task has exactly one network")
network = docker_json("network", "inspect", next(iter(main_nets)))[0]
require(network["Internal"] and not network["EnableIPv6"], "task network internal, IPv6 disabled")
require(len(proxy_nets) == 2 and set(main_nets) <= set(proxy_nets), "only proxy has second network")
for container in (main, proxy):
    hc = container["HostConfig"]
    require(not hc["Privileged"] and not hc.get("PidMode")
            and hc.get("NetworkMode") != "host", "no privileged/host/shared PID mode")
    require("no-new-privileges:true" in hc.get("SecurityOpt", []), "no-new-privileges enabled")
require(not main_config.get("CapAdd"), "task has no added capabilities")
require({"NET_ADMIN", "NET_RAW", "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE"}
        <= set(main_config.get("CapDrop", [])), "task network/ptrace capabilities dropped")
require(not proxy_config.get("PortBindings"), "proxy has no published port")
require(main_config.get("Dns") == ["127.0.0.1"], "task DNS forwarding override present")
secret_mounts = [m for m in proxy["Mounts"] if m["Destination"] == "/run/secrets/qianfan_key"]
require(len(secret_mounts) == 1 and not secret_mounts[0]["RW"], "upstream key mounted only read-only in proxy")
secret_source = Path(secret_mounts[0]["Source"]).resolve()
for mount in main["Mounts"]:
    source = Path(mount["Source"]).resolve()
    require(source not in secret_source.parents and source != secret_source,
            "task mount cannot contain upstream secret source")
    require(str(source) not in ("/", "/proc", "/sys", "/var/run", "/run", "/var/run/docker.sock", "/run/docker.sock")
            and mount["Destination"] not in ("/proc", "/sys", "/host", "/run/secrets"),
            "task has no broad host/process/socket mount")
# These checks require host permission, not inspection from inside the task.
for name in ("pid", "mnt"):
    main_ns = os.stat(f"/proc/{main['State']['Pid']}/ns/{name}").st_ino
    proxy_ns = os.stat(f"/proc/{proxy['State']['Pid']}/ns/{name}").st_ino
    require(main_ns != proxy_ns, f"task and proxy have different {name} namespaces")
net_name = next(iter(main_nets))
proxy_ip = proxy_nets[net_name]["IPAddress"]
subnet = network["IPAM"]["Config"][0]["Subnet"]
require(ipaddress.ip_address(proxy_ip) in ipaddress.ip_network(subnet), "proxy internal IP matches firewall network")
for chain in ("DOCKER-USER", "INPUT"):
    require(subprocess.run(["iptables", "-w", "-C", chain, "-s", subnet, "-j", "REJECT"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0,
            f"{chain} rejects task subnet outside explicit proxy exceptions")
print('Host checks passed; run /checks/check-task-isolation.py inside the task next.')
