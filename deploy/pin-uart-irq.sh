#!/bin/sh
# Pin the PL011 UARTs' shared interrupt (GIC SPI 153 on the Pi 4) to CPU 3, so DMX
# bytes are handled on a core the engine and web server don't compete for.
# Never fails the service: on other boards it just does nothing.
IRQ=$(awk '/uart-pl011|GICv2 153 Level/ {sub(":", "", $1); print $1; exit}' /proc/interrupts)
[ -n "$IRQ" ] && echo 8 > "/proc/irq/$IRQ/smp_affinity" 2>/dev/null
exit 0
