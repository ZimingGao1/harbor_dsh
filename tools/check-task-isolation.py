"""Run inside the task container with a DUMMY proxy deployment, before paid use.

This is an acceptance probe, not a network enforcement mechanism. It never
prints credentials or reads the secret file value. Docker host inspection and
firewall checks are additionally required; see docs/model-proxy.md.
"""
import http.client
import json
from pathlib import Path
import socket
import sys

failures = []


def check(condition, name):
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failures.append(name)


check(not Path("/run/secrets/qianfan_key").exists(), "upstream secret is not mounted")
check(not Path("/var/run/docker.sock").exists(), "Docker socket is not mounted")
# Inspect all visible process environments for a dummy sentinel. No values
# printed, even if the test finds a leak. DUMMY ONLY: never pass a real key here.
sentinel = b"dummy-upstream-isolation-sentinel"
leaked = False
for path in Path("/proc").glob("[0-9]*/environ"):
    try:
        leaked |= sentinel in path.read_bytes()
    except (PermissionError, ProcessLookupError, FileNotFoundError):
        pass
check(not leaked, "upstream dummy key absent from visible process environments")
for host, port in [("1.1.1.1", 443), ("8.8.8.8", 53), ("169.254.169.254", 80),
                   ("100.100.100.200", 80), ("172.30.90.1", 22)]:
    try:
        with socket.create_connection((host, port), timeout=2):
            connected = True
    except OSError:
        connected = False
    check(not connected, f"direct TCP blocked: {host}:{port}")
try:
    socket.getaddrinfo("example.com", 443)
    dns_worked = True
except OSError:
    dns_worked = False
check(not dns_worked, "public DNS resolution blocked")
try:
    socket.create_connection(("2606:4700:4700::1111", 443), timeout=2).close()
    ipv6_worked = True
except OSError:
    ipv6_worked = False
check(not ipv6_worked, "direct IPv6 blocked")
conn = http.client.HTTPConnection("dsh-model-proxy", 8080, timeout=5)
conn.request("POST", "/v2/chat/completions", body=b"{}", headers={"Content-Type": "application/json"})
check(conn.getresponse().status == 401, "restricted proxy reachable; anonymous requests denied")
conn.close()
print(json.dumps({"passed": not failures, "failed_checks": failures}))
sys.exit(bool(failures))
