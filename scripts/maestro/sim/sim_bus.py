#!/usr/bin/env python3
# Half-duplex PTY bus hub for the Maestro simulator.
#
# The real peripheral bus is one half-duplex wire: the host transceiver
# echoes its own bytes, every slave hears them, and exactly one slave may
# answer.  If two transmitters drive the line at the same time the bytes
# are wire-ANDed (the CAN transceiver pulls dominant) and both sides see
# garbage - that is the failure this hub emulates.
#
# host_multiplexer.py opens the upstream PTY; each simulated MCU exposes a
# node PTY.  Every byte is scheduled on a shared wire timeline at the
# configured baud rate.  Overlapping bytes from different sources are
# ANDed together and counted as a collision.
#
#   sim_bus.py --upstream /tmp/sim_bus_upstream --baud 250000 \
#              --node head0=/tmp/sim_node_head0 \
#              --node head1=/tmp/sim_node_head1 \
#              --node bed=/tmp/sim_node_bed \
#              --delay bed=0.03
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import os
import pty
import random
import select
import signal
import sys
import time
import tty

NODE_ALIASES = {
    "0x20": "head0", "0x30": "head1", "0x40": "bed",
    "head0": "head0", "head1": "head1", "bed": "bed",
}


def log(msg):
    sys.stderr.write("sim_bus: %s\n" % msg)
    sys.stderr.flush()


def parse_target(value):
    name, sep, number = value.partition("=")
    if not sep:
        raise ValueError("expected NAME=VALUE, got %r" % value)
    norm = NODE_ALIASES.get(name.lower())
    if norm is None:
        raise ValueError("unknown node %r" % name)
    return norm, float(number)


class Node:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.fd = -1
        self.delay = 0.0
        self.jitter = 0.0
        self.drop = 0.0
        self.pending = []       # [due_monotonic, bytes] to this node
        self.last_open = 0.0
        self.rx = 0
        self.tx = 0

    def open(self, now):
        self.last_open = now
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            return False
        tty.setraw(fd)
        self.fd = fd
        log("%s: connected %s" % (self.name, self.path))
        return True

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def write(self, data):
        while data and self.fd >= 0:
            try:
                n = os.write(self.fd, data)
                self.tx += n
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.0002)
            except OSError:
                self.close()

    def deliver(self, data, now):
        if self.drop and random.random() < self.drop:
            return
        delay = self.delay
        if self.jitter:
            delay += random.uniform(-self.jitter, self.jitter)
        due = now + max(0.0, delay)
        if due <= now:
            self.write(data)
        else:
            self.pending.append([due, data])

    def flush(self, now):
        if not self.pending:
            return
        keep = []
        for due, data in self.pending:
            if due <= now:
                self.write(data)
            else:
                keep.append([due, data])
        self.pending = keep

    def next_due(self):
        if not self.pending:
            return None
        return min(item[0] for item in self.pending)


class Slot:
    """One byte time on the shared wire."""
    __slots__ = ("start", "end", "sources", "byte")

    def __init__(self, start, end, source, byte):
        self.start = start
        self.end = end
        self.sources = {source}
        self.byte = byte


class Wire:
    """Byte-granular half-duplex wire with wire-AND collisions."""

    def __init__(self, baud):
        self.byte_time = 10.0 / baud     # 8N1
        self.slots = []
        self.collisions = 0

    def submit(self, source, data, start):
        t = start
        for byte in data:
            self._submit_byte(source, byte, t)
            t += self.byte_time
        return t

    def _submit_byte(self, source, byte, t):
        for slot in self.slots:
            if slot.start < t + self.byte_time and t < slot.end \
                    and source not in slot.sources:
                # Two transmitters drive the line at once: dominant wins.
                slot.byte &= byte
                slot.sources.add(source)
                self.collisions += 1
                if self.collisions <= 200 or self.collisions % 1000 == 0:
                    log("COLLISION %s at %.6f -> %02x"
                        % ("+".join(sorted(slot.sources)), t, slot.byte))
                return
        self.slots.append(Slot(t, t + self.byte_time, source, byte))

    def pop_ready(self, now):
        ready = []
        keep = []
        for slot in self.slots:
            if slot.end <= now:
                ready.append(slot)
            else:
                keep.append(slot)
        self.slots = keep
        return ready

    def next_due(self):
        if not self.slots:
            return None
        return min(s.end for s in self.slots)


def main():
    ap = argparse.ArgumentParser(description="Maestro half-duplex bus hub")
    ap.add_argument("--upstream", default="/tmp/sim_bus_upstream")
    ap.add_argument("--baud", type=float, default=250000)
    ap.add_argument("--node", action="append", default=[],
                    metavar="NAME=PATH")
    ap.add_argument("--delay", action="append", default=[], metavar="NAME=SEC")
    ap.add_argument("--jitter", action="append", default=[], metavar="NAME=SEC")
    ap.add_argument("--drop", action="append", default=[], metavar="NAME=P")
    ap.add_argument("--stats-interval", type=float, default=10.0)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    node_paths = {}
    for item in args.node:
        name, _, path = item.partition("=")
        node_paths[NODE_ALIASES[name.lower()]] = path
    if not node_paths:
        node_paths = {
            "head0": "/tmp/sim_node_head0",
            "head1": "/tmp/sim_node_head1",
            "bed": "/tmp/sim_node_bed",
        }

    nodes = [Node(name, path) for name, path in node_paths.items()]
    by_name = {n.name: n for n in nodes}
    for value in args.delay:
        name, sec = parse_target(value)
        by_name[name].delay = sec
    for value in args.jitter:
        name, sec = parse_target(value)
        by_name[name].jitter = sec
    for value in args.drop:
        name, p = parse_target(value)
        by_name[name].drop = p

    wire = Wire(args.baud)

    # Upstream side: the mux opens the slave path, we keep the master.
    master, slave = pty.openpty()
    tty.setraw(slave)
    try:
        os.unlink(args.upstream)
    except FileNotFoundError:
        pass
    os.symlink(os.ttyname(slave), args.upstream)
    os.set_blocking(master, False)
    log("upstream %s -> %s, %d baud, byte %.1f us"
        % (args.upstream, os.ttyname(slave), args.baud,
           wire.byte_time * 1e6))

    stopping = [False]

    def stop(sig, frame):
        stopping[0] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    stats_last = time.monotonic()
    echo_bytes = 0
    up_bytes = 0

    while not stopping[0]:
        now = time.monotonic()
        for node in nodes:
            if node.fd < 0 and now - node.last_open > 1.0:
                node.open(now)

        rfds = [master] + [n.fd for n in nodes if n.fd >= 0]
        timeout = 0.02
        due = wire.next_due()
        if due is not None:
            timeout = min(timeout, max(0.0, due - now))
        for node in nodes:
            d = node.next_due()
            if d is not None:
                timeout = min(timeout, max(0.0, d - now))
        try:
            rlist, _, _ = select.select(rfds, [], [], timeout)
        except InterruptedError:
            continue
        now = time.monotonic()

        if master in rlist:
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            if data:
                echo_bytes += len(data)
                up_bytes += len(data)
                wire.submit("upstream", data, now)

        for node in nodes:
            if node.fd < 0 or node.fd not in rlist:
                continue
            try:
                data = os.read(node.fd, 4096)
            except OSError:
                data = b""
            if not data:
                log("%s: disconnected" % node.name)
                node.close()
                continue
            node.rx += len(data)
            wire.submit(node.name, data, now)

        # Resolve bytes that finished their time on the wire.
        for slot in wire.pop_ready(now):
            byte = bytes((slot.byte,))
            if "upstream" not in slot.sources:
                try:
                    os.write(master, byte)      # node -> host
                except (BlockingIOError, OSError):
                    pass
            else:
                try:
                    os.write(master, byte)      # transceiver echo
                except (BlockingIOError, OSError):
                    pass
            for node in nodes:
                if node.name not in slot.sources:
                    node.deliver(byte, now)

        for node in nodes:
            node.flush(now)

        if not args.quiet and args.stats_interval > 0:
            if now - stats_last >= args.stats_interval:
                stats_last = now
                log("echo=%d up=%d collisions=%d "
                    % (echo_bytes, up_bytes, wire.collisions)
                    + " ".join("%s rx=%d tx=%d q=%d"
                               % (n.name, n.rx, n.tx, len(n.pending))
                               for n in nodes))

    for node in nodes:
        node.close()
    try:
        os.unlink(args.upstream)
    except OSError:
        pass
    os.close(master)
    os.close(slave)
    log("stopped, collisions=%d" % wire.collisions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
