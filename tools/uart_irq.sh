#!/bin/bash
# Show or pin the PL011 UART interrupt (all Pi 4 PL011s share one IRQ, 36).
#   sudo tools/uart_irq.sh          show its CPU affinity and per-CPU counts
#   sudo tools/uart_irq.sh pin [N]  pin it to CPU N (default 3)
#   sudo tools/uart_irq.sh unpin    allow all CPUs again
set -euo pipefail
# The PL011s' shared interrupt is GIC SPI 153 on the BCM2711; it may be listed without a name.
IRQ=$(awk '/uart-pl011|GICv2 153 Level/ {sub(":", "", $1); print $1; exit}' /proc/interrupts)
[ -n "$IRQ" ] || { echo "no uart-pl011 interrupt found" >&2; exit 1; }
case ${1:-show} in
    pin)   printf '%x' $((1 << ${2:-3})) > /proc/irq/$IRQ/smp_affinity ;;
    unpin) echo f > /proc/irq/$IRQ/smp_affinity ;;
    show)  ;;
    *)     sed -n '2,5p' "$0"; exit 1 ;;
esac
echo "IRQ $IRQ affinity: $(cat /proc/irq/$IRQ/smp_affinity_list)"
grep -E "^ *$IRQ:" /proc/interrupts
