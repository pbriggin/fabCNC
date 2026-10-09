#!/usr/bin/env bash
set -euo pipefail

# Delete every saved WiFi profile before rebooting so wifi-provision starts its
# setup hotspot on the next boot. This helper is installed root-owned and is
# invoked through a narrowly scoped sudoers rule.
connections="$(nmcli -t -f UUID,TYPE connection show)"
wifi_uuids=()
while IFS=: read -r uuid connection_type; do
    if [[ "$connection_type" == "802-11-wireless" && -n "$uuid" ]]; then
        wifi_uuids+=("$uuid")
    fi
done <<< "$connections"

for uuid in "${wifi_uuids[@]}"; do
    nmcli connection delete uuid "$uuid" >/dev/null
done

exec /sbin/reboot