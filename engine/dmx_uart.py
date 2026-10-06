"""Direct DMX512 input on a Pi UART (250 kbaud, 8N2).

The tty is put in raw mode with PARMRK, so the kernel marks line events in-band:

    \\377 \\377     a literal 0xFF data byte
    \\377 \\0 \\0   a BREAK (start of a DMX packet)
    \\377 \\0 <c>  byte <c> received with a framing error

DmxParser is a pure, incremental state machine over those bytes; it keeps its
state across reads, so escapes split between two reads are handled. Packets are
delimited by BREAKs, so any length from 1 to 512 slots works. A full 513-byte
packet (start code + 512 slots) is emitted as soon as its last byte arrives
instead of waiting for the next BREAK.
"""

import fcntl
import os
import struct
import termios
from collections import Counter
from dataclasses import dataclass, field

DMX_BAUD = 250000
MAX_PACKET = 513  # start code + 512 slots

# Linux termios2 (asm-generic, used by arm64 and x86): 4 flags, c_line, c_cc[19], 2 speeds.
_TERMIOS2 = struct.Struct("4IB19s2I")
_TCGETS2 = 0x802C542A
_TCSETS2 = 0x402C542B
_BOTHER = 0o010000
_CBAUD = 0o010017

# Parser states
_DATA, _ESC, _ESC0 = 0, 1, 2


@dataclass
class Frame:
    start_code: int
    slots: bytes


@dataclass
class ParserStats:
    frames: int = 0            # well-formed packets with start code 0x00
    other_start_codes: Counter = field(default_factory=Counter)
    malformed: int = 0         # packets dropped: error byte inside, or longer than 513
    error_bytes: int = 0       # \377\0<c> framing-error markers
    overlong: int = 0          # packets longer than 513 bytes (also counted as malformed)
    bad_escapes: int = 0       # \377 followed by something other than \377 or \0
    breaks: int = 0
    empty: int = 0             # BREAK followed directly by another BREAK
    sync_bytes: int = 0        # bytes discarded before the first BREAK


class DmxParser:
    def __init__(self):
        self.stats = ParserStats()
        self._state = _DATA
        self._buf = bytearray()
        self._synced = False   # seen a BREAK yet
        self._bad = False      # current packet had an error byte
        self._overflow = False  # current packet ran past 513 bytes
        self._emitted = False  # current packet already emitted at 513 bytes

    def feed(self, data):
        """Consume raw bytes from the tty; return the well-formed Frames completed."""
        out = []
        for b in data:
            st = self._state
            if st == _DATA:
                if b == 0xFF:
                    self._state = _ESC
                else:
                    self._byte(b, out)
            elif st == _ESC:
                if b == 0xFF:
                    self._state = _DATA
                    self._byte(0xFF, out)
                elif b == 0x00:
                    self._state = _ESC0
                else:
                    # Shouldn't happen with PARMRK; keep the 0xFF as data.
                    self.stats.bad_escapes += 1
                    self._state = _DATA
                    self._byte(0xFF, out)
                    self._byte(b, out)
            else:  # _ESC0
                self._state = _DATA
                if b == 0x00:
                    self._break(out)
                else:
                    self.stats.error_bytes += 1
                    if self._synced:
                        self._bad = True
        return out

    def _byte(self, b, out):
        if not self._synced:
            self.stats.sync_bytes += 1
            return
        buf = self._buf
        if len(buf) < MAX_PACKET:
            buf.append(b)
            if len(buf) == MAX_PACKET and not self._bad:
                self._emit(out)
                self._emitted = True
        elif not self._overflow:
            self._overflow = True
            self.stats.overlong += 1

    def _break(self, out):
        self.stats.breaks += 1
        if self._synced:
            if self._bad or self._overflow:
                # A 513-byte packet that overflowed was already emitted; it is
                # still counted here so the probe reports it.
                self.stats.malformed += 1
            elif not self._buf:
                self.stats.empty += 1
            elif not self._emitted:
                self._emit(out)
        self._synced = True
        self._buf = bytearray()
        self._bad = False
        self._overflow = False
        self._emitted = False

    def _emit(self, out):
        buf = self._buf
        sc = buf[0]
        if sc == 0:
            self.stats.frames += 1
        else:
            self.stats.other_start_codes[sc] += 1
        out.append(Frame(sc, bytes(buf[1:])))


def open_dmx_port(path):
    """Open a UART for DMX input: 250000 8N2, raw, PARMRK, no flow control.

    Returns a non-blocking file descriptor.
    """
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        buf = bytearray(_TERMIOS2.size)
        fcntl.ioctl(fd, _TCGETS2, buf)
        iflag, oflag, cflag, lflag, line, cc, ispeed, ospeed = _TERMIOS2.unpack(buf)

        # Only mark breaks and framing errors; everything else off (no IXON/IXOFF,
        # no ISTRIP, no IGNBRK/BRKINT/IGNPAR). INPCK is needed so the kernel
        # marks framing errors instead of passing the bad byte through.
        iflag = termios.PARMRK | termios.INPCK
        oflag = 0
        lflag = 0
        cflag &= ~(_CBAUD | termios.CSIZE | termios.PARENB | termios.CRTSCTS)
        cflag |= _BOTHER | termios.CS8 | termios.CSTOPB | termios.CREAD | termios.CLOCAL
        cc = bytearray(cc)
        cc[termios.VMIN] = 1
        cc[termios.VTIME] = 0
        fcntl.ioctl(fd, _TCSETS2, _TERMIOS2.pack(
            iflag, oflag, cflag, lflag, line, bytes(cc), DMX_BAUD, DMX_BAUD))

        fcntl.ioctl(fd, _TCGETS2, buf)
        got = _TERMIOS2.unpack(buf)
        if got[6] != DMX_BAUD or got[0] != iflag:
            raise OSError(f"{path}: port settings did not stick "
                          f"(iflag={got[0]:#o}, speed={got[6]})")
        termios.tcflush(fd, termios.TCIFLUSH)  # drop anything from before setup
        return fd
    except BaseException:
        os.close(fd)
        raise
