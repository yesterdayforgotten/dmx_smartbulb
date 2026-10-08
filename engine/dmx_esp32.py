"""ESP32 framed DMX input: "DMX#" followed by the 512 channel bytes, at
921600 baud 8N1 (see dmx_esp32/src/main.cpp).

A port of config/jobs.py's serial_receiver with a bounded resync buffer: the
old loop appended to its buffer until it found a header, without limit.
"""

HEADER = b"DMX#"
DATA_LEN = 512
FRAME_LEN = len(HEADER) + DATA_LEN
ESP32_BAUD = 921600


class Esp32Framer:
    def __init__(self):
        self.buf = bytearray()
        self.frames = 0
        self.resyncs = 0        # times bytes had to be skipped to find a header
        self.skipped = 0        # bytes skipped while resyncing

    def feed(self, data):
        """Consume bytes; return the list of complete 512-byte channel frames."""
        self.buf += data
        out = []
        while True:
            i = self.buf.find(HEADER)
            if i < 0:
                # Keep only a possible partial header at the end.
                drop = max(0, len(self.buf) - (len(HEADER) - 1))
                if drop:
                    self.resyncs += 1
                    self.skipped += drop
                    del self.buf[:drop]
                return out
            if i > 0:
                self.resyncs += 1
                self.skipped += i
                del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                return out
            # Taken as soon as it's complete, as before. If bytes were lost the
            # frame is misaligned and the next search resyncs on the header.
            out.append(bytes(self.buf[len(HEADER):FRAME_LEN]))
            self.frames += 1
            del self.buf[:FRAME_LEN]
