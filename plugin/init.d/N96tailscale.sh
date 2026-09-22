#!/bin/sh
# NAT rebuilding only refreshes plugin firewall rules. It never restarts the daemon.
exec "${TSKS_ROOT:-/koolshare}/scripts/tailscale_config" start_nat
