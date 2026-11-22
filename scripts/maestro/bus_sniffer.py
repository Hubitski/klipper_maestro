#!/usr/bin/env python3
"""Read-only Klipper bus sniffer for the Maestro Grand 2 IDEX.

Run this on a laptop connected to the printer's FT232 (rear USB).
The FT232 TXD pin must stay disconnected; this script never writes to the
port, so it cannot disturb the bus.

Wiring:
  To see what the Orange Pi sends (node A = MCP2561 TXD):
      FT232 RXD -> MCP2561 TXD / level-shifter 5V output.
  To see the actual bus (echo + head0/head1/bed replies):
      FT232 RXD -> MCP2561 RXD / level-shifter 5V input (node B).
  Keep FT232 TXD lifted in both cases.

Usage:
  python bus_sniffer.py --port COM7 --baud 250000 --log bus.log
  python bus_sniffer.py --port /dev/ttyUSB0 --raw
"""
import argparse
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_SYNC = 0x7E
MESSAGE_SEQ_MASK = 0x0F
VALID_DESTS = {0x10, 0x20, 0x30, 0x40}
DEST_NAMES = {0x10: "main", 0x20: "head0", 0x30: "head1", 0x40: "bed"}


def _crc_table():
    table = []
    for b in range(256):
        data = (b ^ (b << 4)) & 0xFF
        entry = ((data << 8) ^ (data >> 4) ^ (data << 3)) & 0xFFFF
        table.append(entry)
    return table


CRC_TABLE = _crc_table()


def crc16_ccitt(buf):
    crc = 0xFFFF
    for b in buf:
        crc = (crc >> 8) ^ CRC_TABLE[b ^ (crc & 0xFF)]
    return crc


class Sniffer:
    def __init__(self, log_path=None, raw=False):
        self.buf = bytearray()
        self.log_path = log_path
        self.raw = raw
        self.last_change = time.monotonic()
        self.last_len = 0
        self.stall_logged = False
        self.fh = open(log_path, "a", buffering=1) if log_path else None

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None

    def _emit(self, msg):
        now = time.time()
        stamp = (time.strftime("%H:%M:%S", time.localtime(now))
                 + ".%03d" % (int((now % 1) * 1000),))
        line = "%s %s" % (stamp, msg)
        print(line, flush=True)
        if self.fh is not None:
            self.fh.write(line + "\n")

    @staticmethod
    def _hex(buf, limit=32):
        h = bytes(buf[:limit]).hex()
        return h + ("..." if len(buf) > limit else "")

    def feed(self, data):
        if self.raw:
            self._emit("RAW %s" % (data.hex(),))
        self.buf.extend(data)
        if len(self.buf) != self.last_len:
            self.last_len = len(self.buf)
            self.last_change = time.monotonic()
            self.stall_logged = False
        self._parse()

    def _parse(self):
        while len(self.buf) >= MESSAGE_MIN:
            if self.buf[0] == MESSAGE_SYNC:
                del self.buf[:1]
                continue
            msglen = self.buf[0]
            if msglen < MESSAGE_MIN or msglen > MESSAGE_MAX:
                self._emit("BAD len=%d have=%d hex=%s"
                           % (msglen, len(self.buf), self._hex(self.buf)))
                del self.buf[:1]
                continue
            seq = self.buf[1]
            dest = seq & ~MESSAGE_SEQ_MASK
            if dest not in VALID_DESTS:
                self._emit("BAD dest=0x%02X seq=%d have=%d hex=%s"
                           % (dest, seq & MESSAGE_SEQ_MASK, len(self.buf),
                              self._hex(self.buf)))
                del self.buf[:1]
                continue
            if len(self.buf) < msglen:
                return
            if self.buf[msglen - 1] != MESSAGE_SYNC:
                self._emit("BAD trailer dest=0x%02X len=%d have=%d hex=%s"
                           % (dest, msglen, len(self.buf),
                              self._hex(self.buf)))
                del self.buf[:1]
                continue
            msgcrc = (self.buf[msglen - 3] << 8) | self.buf[msglen - 2]
            crc = crc16_ccitt(self.buf[:msglen - 3])
            if crc != msgcrc:
                self._emit("BAD crc dest=0x%02X len=%d got=0x%04X want=0x%04X"
                           " hex=%s"
                           % (dest, msglen, msgcrc, crc,
                              self._hex(self.buf)))
                del self.buf[:1]
                continue
            pkt = bytes(self.buf[:msglen])
            del self.buf[:msglen]
            name = DEST_NAMES.get(dest, "?")
            kind = "ACK" if msglen == MESSAGE_MIN else "DATA"
            self._emit("RX  %s %s seq=%d len=%d payload=%s"
                       % (name, kind, seq & MESSAGE_SEQ_MASK, msglen,
                          self._hex(pkt[2:msglen - 3])))

    def check_stall(self):
        now = time.monotonic()
        n = len(self.buf)
        if n == 0:
            self.last_len = 0
            self.stall_logged = False
            return
        if now - self.last_change < 0.010:
            return
        if not self.stall_logged:
            self.stall_logged = True
            want = (self.buf[0] if MESSAGE_MIN <= self.buf[0] <= MESSAGE_MAX
                    else -1)
            self._emit("STALL want=%d have=%d hex=%s"
                       % (want, n, self._hex(self.buf)))
        if now - self.last_change > 0.050:
            self._emit("RESYNC dropped=%d hex=%s"
                       % (n, self._hex(self.buf)))
            self.buf.clear()
            self.last_len = 0
            self.stall_logged = False


def main():
    p = argparse.ArgumentParser(description="Maestro Klipper bus sniffer")
    p.add_argument("--port", required=True, help="COMx or /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=250000)
    p.add_argument("--log", default=None, help="append output to this file")
    p.add_argument("--raw", action="store_true", help="also print raw bytes")
    args = p.parse_args()

    sniffer = Sniffer(args.log, args.raw)
    try:
        ser = serial.Serial(args.port, baudrate=args.baud, timeout=0)
    except Exception as e:
        sys.exit("Cannot open %s: %s" % (args.port, e))
    print("Sniffing %s at %d baud (read-only). Ctrl+C to stop."
          % (args.port, args.baud), flush=True)
    try:
        while True:
            data = ser.read(4096)
            if data:
                sniffer.feed(data)
            sniffer.check_stall()
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        sniffer.close()


if __name__ == "__main__":
    main()
