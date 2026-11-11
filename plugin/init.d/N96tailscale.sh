#!/bin/sh
# NAT rebuilding only refreshes plugin firewall rules. It never restarts the daemon;
# the minute task restores the rules the daemon installed for itself.
exec "${TSKS_ROOT:-/koolshare}/scripts/tailscale_config" start_nat
