#!/bin/bash
# Make the cloud relay permanent: survive reboots, auto-connect WiFi, firewall the uplink.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
echo "== stopping any ad-hoc relay"
pkill -f 'faketest/relay.py' 2>/dev/null || true
pkill -f 'sntp_test.py' 2>/dev/null || true
echo "== letting NetworkManager manage wlan0 (remove the unmanaged rule)"
rm -f /usr/lib/NetworkManager/conf.d/90-dmx_smartbulb.conf /etc/NetworkManager/conf.d/90-dmx_smartbulb.conf
rm -f /run/NetworkManager/conf.d/95-dmx_smartbulb-venue.conf
systemctl reload NetworkManager; sleep 2
rfkill unblock wifi || true
nmcli device set wlan0 managed yes || true
echo "== WiFi uplink autoconnect"
SSID="${1:-}"
nmcli connection delete dmx_smartbulb-venue 2>/dev/null || true
if [ -n "$SSID" ]; then
  if ! nmcli -t -f NAME connection show | grep -qxF "$SSID"; then
    nmcli connection add type wifi con-name "$SSID" ifname wlan0 ssid "$SSID" \
      connection.autoconnect yes ipv4.route-metric 50 || echo "  (add $SSID failed; is it an open network?)"
  fi
  nmcli connection modify "$SSID" connection.autoconnect yes ipv4.route-metric 50 2>/dev/null || true
  nmcli connection up "$SSID" 2>/dev/null || true
else
  echo "  (no SSID given; set the WiFi uplink in the web UI: Setup -> Cloud relay)"
fi
echo "== installing the relay service"
sed "s#@REPO@#$REPO#g" "$REPO/deploy/dmx_smartbulb-relay.service.in" > /etc/systemd/system/dmx_smartbulb-relay.service
systemctl daemon-reload
systemctl enable --now dmx_smartbulb-relay.service
sleep 3
echo "== status"
systemctl is-active dmx_smartbulb-relay.service
ss -ltn | grep -E ':(443|123) ' | head
nft list table inet dmx_relay >/dev/null 2>&1 && echo "firewall: active" || echo "firewall: NOT active"
echo "Done. Enable in config: relay.enabled, relay.ssid, relay.allow (see README)."
