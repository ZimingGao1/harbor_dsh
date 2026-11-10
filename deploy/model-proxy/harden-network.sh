#!/usr/bin/env bash
# Run on the Linux Docker host BEFORE starting this single trial.
# Does not flush existing firewall rules. Requires Docker's iptables backend.
set -euo pipefail
[[ "$(uname -s)" = Linux && "$EUID" = 0 ]] || { echo 'Linux root required' >&2; exit 1; }
task_subnet="${DSH_TASK_SUBNET:-172.30.90.0/24}"
proxy_ip="${DSH_PROXY_IP:-172.30.90.2}"
python3 - "$task_subnet" "$proxy_ip" <<'PY'
import ipaddress, sys
net = ipaddress.ip_network(sys.argv[1], strict=True)
ip = ipaddress.ip_address(sys.argv[2])
assert net.version == 4 and net.prefixlen == 24 and net.is_private and ip in net
assert ip not in (net.network_address, net.broadcast_address)
PY
iptables -w -nL DOCKER-USER >/dev/null

ensure_rule() {
  local chain="$1"; shift
  if ! iptables -w -C "$chain" "$@" 2>/dev/null; then
    iptables -w -I "$chain" 1 "$@"
  fi
}
# Insert reject first, then the two exceptions above it. Reply streams are
# allowed only for already-established connections to this proxy's port 8080.
ensure_rule DOCKER-USER -s "$task_subnet" -j REJECT
ensure_rule DOCKER-USER -s "$proxy_ip" -d "$task_subnet" -p tcp --sport 8080 -m conntrack --ctstate ESTABLISHED -j ACCEPT
ensure_rule DOCKER-USER -s "$task_subnet" -d "$proxy_ip" -p tcp --dport 8080 -j ACCEPT
# An internal bridge alone does NOT block services on the Docker host.
ensure_rule INPUT -s "$task_subnet" -j REJECT
echo 'Trial network rules installed. Run the isolation acceptance check before using a real key.'
