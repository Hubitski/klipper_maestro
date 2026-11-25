#!/usr/bin/env python3
# Align a simulated slave's message sequence with Klippy's first message.
#
# The firmware starts with next_sequence = MESSAGE_DEST (sequence 0), while
# the host's chelper starts sending with sequence 1.  On a fresh MCU the
# first host packet therefore mismatches and the firmware answers with a
# NAK; Klippy's "connection init" path then drops the pending command and
# waits forever.  Sending one valid get_config packet with sequence 0 before
# Klippy connects advances the firmware to sequence 1 and drains the queued
# "starting" report, so the real handshake is in order.
#
#   prime_node.py head0=/tmp/sim_node_head0 head1=... bed=...
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import select
import sys
import time
import tty

NODE_DEST = {"head0": 0x20, "head1": 0x30, "bed": 0x40}


def _build_crc16_table():
    table = []
    for b in range(256):
        data = (b ^ (b << 4)) & 0xFF
        entry = ((data << 8) ^ (data >> 4) ^ (data << 3)) & 0xFFFF
        table.append(entry)
    return table


CRC_TABLE = _build_crc16_table()


def crc16_ccitt(buf):
    crc = 0xFFFF
    for b in buf:
        crc = (crc >> 8) ^ CRC_TABLE[b ^ (crc & 0xFF)]
    return crc


def make_frame(dest, payload=b"\x01\x00\x28"):
    # get_config, the same first query Klippy sends.
    body = bytes((len(payload) + 5, dest)) + payload
    crc = crc16_ccitt(body)
    return body + bytes((crc >> 8, crc & 0xFF, 0x7E))


def prime(name, path):
    dest = NODE_DEST[name]
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        tty.setraw(fd)
        os.write(fd, make_frame(dest))
        got = bytearray()
        deadline = time.monotonic() + 1.0
        last = time.monotonic()
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data = os.read(fd, 4096)
                if data:
                    got.extend(data)
                    last = time.monotonic()
            elif time.monotonic() - last > 0.3:
                break
        print("%s: primed, drained %d bytes" % (name, len(got)))
        return len(got)
    finally:
        os.close(fd)


def main(argv):
    if not argv:
        print("usage: prime_node.py name=path [...]", file=sys.stderr)
        return 2
    rc = 0
    for arg in argv:
        name, sep, path = arg.partition("=")
        if not sep or name not in NODE_DEST:
            print("bad argument %r" % arg, file=sys.stderr)
            rc = 2
            continue
        try:
            if prime(name, path) == 0:
                print("%s: no response" % name, file=sys.stderr)
                rc = 1
        except OSError as e:
            print("%s: %s" % (name, e), file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
