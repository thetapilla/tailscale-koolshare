#!/bin/sh
exec "${TSKS_ROOT:-/koolshare}/scripts/tailscale_config" "${1:-start}"
