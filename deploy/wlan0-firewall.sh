#!/bin/sh
# Block new inbound connections on the WiFi uplink so the web UI and SSH are reachable
# only on the wired show network (eth0), never on the venue WiFi. The Pi's own outbound
# (relay to the cloud, DNS, NTP) and its return traffic stay allowed via connection tracking.
# Idempotent: add-then-flush avoids errors on a fresh boot (no table to delete yet).
nft add table inet dmx_relay 2>/dev/null
nft flush table inet dmx_relay
nft -f - <<'NFT'
table inet dmx_relay {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "wlan0" ct state { established, related } accept
    iifname "wlan0" ip protocol icmp accept
    iifname "wlan0" ct state new drop
    iifname "wlan0" ct state invalid drop
  }
}
NFT
