#!/usr/bin/env python3
"""
Maestro Grand 2 IDEX - Host serial multiplexer (direct-bus topology).

Two independent physical links:

  --main-port   /dev/ttyS1
      Point-to-point link to the ATmega1284p USART0.  The motherboard runs
      WITHOUT CONFIG_MAESTRO_ROUTER, so this is ordinary Klipper traffic and
      is passed through byte for byte.

  --periph-port /dev/ttyS0
      Local 3.3V CAN transceiver wired to the toolhead/bed multi-drop bus.
      Creates /tmp/klipper_mcu_{head0,head1,bed}, rewrites the DEST field and
      arbitrates the half-duplex bus.  The transceiver mirrors everything we
      transmit back onto its RXD output, so the first received frame that is
      byte-identical to our last request is dropped as local echo.

To Klippy each MCU still looks like a normal, independent serial device.
The old single-port/router topology is not handled here; use the previous
revision of this file if the router firmware is flashed back.
"""

import os
import sys
import time
import errno
import fcntl
import termios
import struct
import select
import signal
import logging
import argparse
import collections
from typing import Dict, Optional, Tuple

# Precompute CCITT CRC-16 table (poly 0x1021)
def _build_crc16_table():
    table = []
    for b in range(256):
        data = (b ^ (b << 4)) & 0xFF
        entry = ((data << 8) ^ (data >> 4) ^ (data << 3)) & 0xFFFF
        table.append(entry)
    return table

CRC_TABLE = _build_crc16_table()

def crc16_ccitt(buf: bytes) -> int:
    crc = 0xFFFF
    for b in buf:
        crc = (crc >> 8) ^ CRC_TABLE[b ^ (crc & 0xFF)]
    return crc

MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_SYNC = 0x7E
MESSAGE_SEQ_MASK = 0x0F
DEFAULT_DEST = 0x10

NODE_DEST_MAP = {
    'main': 0x10,
    'head0': 0x20,
    'head1': 0x30,
    'bed': 0x40,
}

DEST_TO_NAME = {v: k for k, v in NODE_DEST_MAP.items()}
VALID_DESTS = {0x10, 0x20, 0x30, 0x40}
PERIPH_DESTS = {0x20, 0x30, 0x40}


class KlipperPacketParser:
    """Stream parser for extracting framed Klipper messages.

    Frames have no leading sync: the length byte comes immediately after the
    previous frame's trailing 0x7E.  On any framing error we therefore jump
    past the next 0x7E (the end of the corrupt fragment) instead of deleting a
    single byte and re-interpreting the same residue over and over.  The line
    can also be cleared when it has been idle longer than a frame time, which
    recovers from a truncated frame without relying on a tap log.
    """
    def __init__(self, name: str = "", allowed_dests=None, tap=None, label=None,
                 idle_time=0.005):
        self.name = name
        self.allowed_dests = allowed_dests or VALID_DESTS
        self.buf = bytearray()
        self.bad_sync_count = 0
        self.tap = tap
        self.label = label or name
        self.last_feed_time: Optional[float] = None
        self.idle_time = idle_time

    def _report(self, reason):
        if self.tap is not None:
            self.tap.parse_error(self.label, reason, self.buf)

    def feed(self, data: bytes):
        if not data:
            return
        self.buf.extend(data)
        self.last_feed_time = time.monotonic()

    def _resync(self):
        """Drop everything through the next SYNC byte (the corrupt frame's
        trailer).  If no SYNC is present the whole residue is unusable."""
        idx = self.buf.find(MESSAGE_SYNC, 1)
        if idx < 0:
            self.buf.clear()
        else:
            del self.buf[:idx + 1]

    def idle_clear(self, now=None, idle_time=None):
        """Clear a partial frame after the line has been quiet long enough
        that no more bytes of it can still be in flight."""
        if not self.buf or self.last_feed_time is None:
            return False
        if now is None:
            now = time.monotonic()
        if idle_time is None:
            idle_time = self.idle_time
        if now - self.last_feed_time > idle_time:
            self.buf.clear()
            return True
        return False

    def next_packet(self) -> Optional[bytes]:
        while len(self.buf) >= MESSAGE_MIN:
            # Discard leading SYNC bytes (inter-message padding)
            if self.buf[0] == MESSAGE_SYNC:
                del self.buf[:1]
                continue

            # Check length byte
            msglen = self.buf[0]
            if msglen < MESSAGE_MIN or msglen > MESSAGE_MAX:
                self._report("bad-len")
                self.bad_sync_count += 1
                self._resync()
                continue

            # Check DEST field in SEQ byte
            msgseq = self.buf[1]
            dest = msgseq & ~MESSAGE_SEQ_MASK
            if dest not in self.allowed_dests:
                self._report("bad-dest")
                self.bad_sync_count += 1
                self._resync()
                continue

            if len(self.buf) < msglen:
                # Incomplete packet, await more bytes
                return None

            # Verify sync trailer byte
            if self.buf[msglen - 1] != MESSAGE_SYNC:
                self._report("bad-trailer")
                self.bad_sync_count += 1
                self._resync()
                continue

            # Verify CRC16
            msgcrc = (self.buf[msglen - 3] << 8) | self.buf[msglen - 2]
            crc = crc16_ccitt(self.buf[:msglen - 3])
            if crc != msgcrc:
                self._report("bad-crc")
                self.bad_sync_count += 1
                self._resync()
                continue

            # Complete, valid packet found
            pkt = bytes(self.buf[:msglen])
            del self.buf[:msglen]
            return pkt

        return None


class VirtualChannel:
    """Manages one pseudo-terminal (PTY) pair connected to Klippy."""
    def __init__(self, name: str, node_id: int, symlink_path: str,
                 tap=None):
        self.name = name
        self.node_id = node_id
        self.symlink_path = symlink_path
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.slave_name: str = ""
        self.tap = tap
        self.parser = KlipperPacketParser(name=name, tap=tap,
                                          label="PTY:" + name)
        self.tx_queue = bytearray()
        self.rx_bytes = 0
        self.rx_packets = 0

    def open(self):
        import pty
        import tty
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_name = os.ttyname(self.slave_fd)
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)

        # Set non-blocking on master_fd
        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Allow non-root users (pi, orangepi, klipper) to access the PTY slave
        try:
            os.chmod(self.slave_name, 0o666)
        except OSError:
            pass

        # Create symlink for Klipper
        try:
            if os.path.islink(self.symlink_path) or os.path.exists(self.symlink_path):
                os.unlink(self.symlink_path)
        except OSError:
            pass
        os.symlink(self.slave_name, self.symlink_path)
        logging.info("Created PTY for %s: %s -> %s (master_fd=%d)",
                     self.name, self.symlink_path, self.slave_name, self.master_fd)

    def close(self):
        try:
            if os.path.islink(self.symlink_path):
                os.unlink(self.symlink_path)
        except OSError:
            pass
        if self.slave_fd is not None:
            try:
                os.close(self.slave_fd)
            except OSError:
                pass
            self.slave_fd = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def reopen(self, poll: Optional[select.poll] = None):
        logging.info("Reopening PTY channel for %s...", self.name)
        if poll is not None and self.master_fd is not None:
            try:
                poll.unregister(self.master_fd)
            except Exception:
                pass
        self.close()
        self.tx_queue.clear()
        self.parser = KlipperPacketParser(name=self.name, tap=self.tap,
                                          label="PTY:" + self.name)
        self.open()
        if poll is not None and self.master_fd is not None:
            poll.register(self.master_fd, select.POLLIN | select.POLLERR | select.POLLHUP)


def rewrite_packet_dest(pkt: bytes, new_dest: int) -> bytes:
    """Rewrites the destination in byte 1 (SEQ) and updates CRC16."""
    msglen = pkt[0]
    seq_num = pkt[1] & MESSAGE_SEQ_MASK
    new_seq = (new_dest & 0xF0) | seq_num

    m = bytearray(pkt)
    m[1] = new_seq
    # Compute CRC over bytes 0 to msglen - 4 (length, seq, payload)
    new_crc = crc16_ccitt(m[:msglen - 3])
    m[msglen - 3] = (new_crc >> 8) & 0xFF
    m[msglen - 2] = new_crc & 0xFF
    return bytes(m)


class BusTap:
    """Optional protocol/physical-layer log for the peripheral bus.

    The mux is the only process that owns ttyS0, so the tap lives here rather
    than in a separate sniffer.  It logs valid requests/responses, dropped
    local echoes, parser errors, incomplete frames and UART error counters.
    """
    ICOUNT_FIELDS = ("cts", "dsr", "rng", "dcd", "rx", "tx",
                     "frame", "overrun", "parity", "brk", "buf_overrun")
    # Linux struct serial_icounter_struct is 11 named counters plus
    # int reserved[9] = 20 ints = 80 bytes.  The kernel copies the whole
    # struct, so the ioctl buffer must be that large.
    ICOUNT_STRUCT_INTS = 20
    TIOCGICOUNT = 0x545D

    # Large userspace buffer: the 1 ms arbiter loop must never block on a
    # synchronous line flush.  maybe_flush() drains it on a timer / size.
    FLUSH_INTERVAL = 0.5
    FLUSH_BYTES = 32768

    def __init__(self, path):
        self.fh = None
        self.last_error_time = 0.0
        self.last_error_key = None
        self.stall_len = 0
        self.stall_time = 0.0
        self.stall_logged = False
        self.resync_logged = False
        self.icount = None
        self.icount_time = 0.0
        self._pending_bytes = 0
        self._last_flush = time.monotonic()
        if not path:
            return
        try:
            self.fh = open(path, "a", buffering=65536)
            self._write("TAP started")
            self.maybe_flush(force=True)
        except OSError as e:
            logging.warning("Tap log %s disabled: %s", path, e)

    def close(self):
        if self.fh is not None:
            self.maybe_flush(force=True)
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None

    def _write(self, msg):
        if self.fh is None:
            return
        try:
            now = time.time()
            stamp = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                     + ".%03d" % (int((now % 1) * 1000),))
            line = "%s %s\n" % (stamp, msg)
            self.fh.write(line)
            self._pending_bytes += len(line)
            if self._pending_bytes >= self.FLUSH_BYTES:
                self.maybe_flush(force=True)
        except Exception:
            pass

    def maybe_flush(self, force=False):
        if self.fh is None:
            return
        now = time.monotonic()
        if (not force and self._pending_bytes < self.FLUSH_BYTES
                and now - self._last_flush < self.FLUSH_INTERVAL):
            return
        try:
            self.fh.flush()
        except Exception:
            pass
        self._pending_bytes = 0
        self._last_flush = now

    @staticmethod
    def _hex(buf, limit=24):
        h = bytes(buf[:limit]).hex()
        return h + ("..." if len(buf) > limit else "")

    def packet(self, direction, pkt, note=""):
        msglen = pkt[0]
        dest = pkt[1] & 0xF0
        seq = pkt[1] & MESSAGE_SEQ_MASK
        payload = pkt[2:msglen - 3]
        kind = "ACK" if msglen == MESSAGE_MIN else "DATA"
        self._write("%s %s dest=0x%02X seq=%d len=%d payload=%s %s"
                    % (direction, kind, dest, seq, msglen,
                       self._hex(payload), note))

    def echo(self, pkt):
        self._write("ECHO  <- dest=0x%02X seq=%d len=%d (dropped)"
                    % (pkt[1] & 0xF0, pkt[1] & MESSAGE_SEQ_MASK, pkt[0]))

    def event(self, msg):
        self._write("BUS   " + msg)

    def parse_error(self, label, reason, buf):
        now = time.monotonic()
        key = (label, reason)
        if key == self.last_error_key and now - self.last_error_time < 0.1:
            return
        self.last_error_key = key
        self.last_error_time = now
        self._write("PARSE %s %s have=%d hex=%s"
                    % (label, reason, len(buf), self._hex(buf)))

    def stall(self, label, parser):
        """Diagnose a frame that stopped mid-way.

        Purely observational: the parser performs its own idle resync so
        recovery does not depend on the tap being enabled.
        """
        now = time.monotonic()
        n = len(parser.buf)
        if n == 0:
            self.stall_len = 0
            self.stall_logged = False
            self.resync_logged = False
            return
        if n != self.stall_len:
            self.stall_len = n
            self.stall_time = now
            self.stall_logged = False
            self.resync_logged = False
            return
        if now - self.stall_time < 0.010:
            return
        buf = parser.buf
        want = buf[0] if (len(buf) >= 1
                          and MESSAGE_MIN <= buf[0] <= MESSAGE_MAX) else -1
        if not self.stall_logged:
            self.stall_logged = True
            self._write("STALL %s want=%d have=%d hex=%s"
                        % (label, want, n, self._hex(buf, 32)))
        if now - self.stall_time > 0.050 and not self.resync_logged:
            self.resync_logged = True
            self._write("RESYNC %s dropped=%d hex=%s"
                        % (label, n, self._hex(buf, 32)))

    def poll_icount(self, fd, label="BUS"):
        """Log deltas of the kernel UART error counters (framing/overrun/...)."""
        now = time.monotonic()
        if now - self.icount_time < 1.0:
            return
        self.icount_time = now
        fmt = "I" * self.ICOUNT_STRUCT_INTS
        try:
            buf = bytearray(struct.pack(fmt,
                             *([0] * self.ICOUNT_STRUCT_INTS)))
            res = fcntl.ioctl(fd, self.TIOCGICOUNT, buf)
            vals = struct.unpack(fmt, res)[:len(self.ICOUNT_FIELDS)]
        except Exception:
            self.icount = None
            return
        cur = dict(zip(self.ICOUNT_FIELDS, vals))
        if self.icount is not None:
            delta = {k: cur[k] - self.icount[k] for k in self.ICOUNT_FIELDS}
            if any(delta[k] for k in ("frame", "overrun", "parity", "brk",
                                      "buf_overrun")):
                self._write("PHY   %s frame+%d overrun+%d parity+%d brk+%d "
                            "buf_overrun+%d"
                            % (label, delta["frame"], delta["overrun"],
                               delta["parity"], delta["brk"],
                               delta["buf_overrun"]))
        self.icount = cur


class MaestroMultiplexer:
    """Main multiplexer engine coordinating the two UARTs and the PTYs."""
    def __init__(self, serial_port=None, baud_rate=250000, channel_configs=None,
                 main_port=None, periph_port=None, tap=None,
                 bus_guard=0.003, bus_timeout=0.012):
        self.baud_rate = baud_rate
        self.channel_configs = channel_configs or {}
        self.tap = tap
        self.main_port = main_port
        # serial_port is kept as an alias for periph_port for the unit tests.
        self.periph_port = periph_port if periph_port is not None else serial_port

        self.channels: Dict[str, VirtualChannel] = {}
        self.fd_to_channel: Dict[int, VirtualChannel] = {}
        self.dest_to_channel: Dict[int, VirtualChannel] = {}
        self.fd_to_link: Dict[int, str] = {}

        self.main_ser = None
        self.main_fd: Optional[int] = None
        self.periph_ser = None
        self.periph_fd: Optional[int] = None

        # Main link is raw passthrough, no frame parsing.
        self.main_tx_queue = bytearray()

        # Idle threshold used to clear a half-received frame.  Two maximal
        # frames' worth of wire time, with a 5 ms floor.
        frame_time = (MESSAGE_MAX * 10.0) / float(baud_rate or 250000)
        self.parser_idle_time = max(0.005, frame_time * 2.0)

        # Peripheral link.
        self.uart_parser = KlipperPacketParser(name="periph",
                                               allowed_dests=PERIPH_DESTS,
                                               tap=tap, label="BUS",
                                               idle_time=self.parser_idle_time)
        self.uart_tx_queue = bytearray()

        # Half-Duplex Multi-Drop Bus Arbiter for the peripheral transceiver.
        self.bus_busy = False
        self.bus_active_dest = 0
        self.bus_request_seq: Optional[int] = None
        self.bus_tx_time: Optional[float] = None
        self.bus_release_time: Optional[float] = None
        self.bus_timeout = bus_timeout  # inactivity timeout for a transaction
        self.bus_guard = bus_guard      # delay after the terminating ACK
        self.bus_hard_timeout = 0.200   # absolute wedge protection
        self.bus_busy_since = None
        self.bus_last_byte_time = 0.0
        self.bus_last_activity = 0.0
        self.bus_pending_queue = collections.deque()
        self.bus_last_release = None

        # Never dispatch immediately after a timeout: give the bus a short
        # quiet gap so a late slave frame cannot collide with the next request.
        self.bus_recovery_guard = 0.003
        self.bus_recovery_until = 0.0

        # Per-node fairness / isolation.
        self.bus_queue_per_node_max = 2
        self.consecutive_timeouts: Dict[int, int] = {}
        self.quarantine_until: Dict[int, float] = {}
        self.quarantine_threshold = 3
        self.quarantine_time = 1.0

        # Local transceiver echo of our own transmitted frames.  A FIFO (not a
        # single slot) so an echo delayed past a transaction boundary is still
        # recognised and dropped instead of reaching Klippy as a fake response.
        self.echo_fifo = collections.deque()
        self.echo_fifo_max = 8
        self.echo_window = 0.100

        # New-Klippy-session heuristic: a request arriving after the bus has
        # been idle for this long is treated as the start of a fresh session,
        # so stale queued/PTY data from the previous client is discarded.
        # Must be longer than Klippy's idle poll interval (~5s in shutdown),
        # otherwise every poll is mistaken for a new session and the queues
        # (including Klippy's reset) are cleared on every cycle.
        self.session_idle_time = 15.0

        self.running = False

    # ------------------------------------------------------------------ setup
    def _open_serial(self, port: str):
        logging.info("Opening %s at %d baud...", port, self.baud_rate)
        # pyserial guarantees accurate 250000 baud via Linux termios2.
        try:
            import serial
        except ImportError:
            logging.critical(
                "pyserial is required for reliable 250000 baud support. "
                "Install with: pip3 install pyserial"
            )
            raise SystemExit(1)
        try:
            ser = serial.Serial(port, baudrate=self.baud_rate, timeout=0,
                                exclusive=True)
        except TypeError:
            # Older pyserial without the exclusive argument.
            ser = serial.Serial(port, baudrate=self.baud_rate, timeout=0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        fd = ser.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return ser, fd

    def setup(self):
        if self.main_port:
            self.main_ser, self.main_fd = self._open_serial(self.main_port)
            self.fd_to_link[self.main_fd] = 'main'
        if self.periph_port:
            self.periph_ser, self.periph_fd = self._open_serial(self.periph_port)
            self.fd_to_link[self.periph_fd] = 'periph'
        if self.main_fd is None and self.periph_fd is None:
            logging.critical("No serial ports configured")
            raise SystemExit(1)

        for name, (node_id, symlink_path) in self.channel_configs.items():
            if node_id == DEFAULT_DEST and self.main_fd is None:
                logging.warning("Skipping channel %s: no --main-port", name)
                continue
            if node_id != DEFAULT_DEST and self.periph_fd is None:
                logging.warning("Skipping channel %s: no --periph-port", name)
                continue
            chan = VirtualChannel(name=name, node_id=node_id,
                                  symlink_path=symlink_path, tap=self.tap)
            chan.open()
            self.channels[name] = chan
            self.fd_to_channel[chan.master_fd] = chan
            self.dest_to_channel[node_id] = chan

    def cleanup(self):
        logging.info("Cleaning up resources...")
        for chan in self.channels.values():
            chan.close()
        self.channels.clear()
        self.fd_to_channel.clear()
        self.dest_to_channel.clear()
        for ser_attr in ('main_ser', 'periph_ser'):
            ser = getattr(self, ser_attr)
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                setattr(self, ser_attr, None)
        self.main_fd = None
        self.periph_fd = None
        self.fd_to_link.clear()
        if self.tap is not None:
            self.tap.close()

    # ------------------------------------------------------------- main loop
    def run(self):
        self.running = True
        poll = select.poll()

        for fd in self.fd_to_link:
            poll.register(fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        for chan in self.channels.values():
            poll.register(chan.master_fd,
                          select.POLLIN | select.POLLERR | select.POLLHUP)

        logging.info("Maestro multiplexer active (main=%s periph=%s).",
                     self.main_port, self.periph_port)
        last_stat_time = time.time()

        while self.running:
            # Peripheral bus arbitration (also performs the idle resync and
            # retries a guard-delayed dispatch without blocking the loop).
            self._service_bus()

            if self.main_fd is not None:
                events = select.POLLIN | select.POLLERR | select.POLLHUP
                if self.main_tx_queue:
                    events |= select.POLLOUT
                poll.register(self.main_fd, events)
            if self.periph_fd is not None:
                events = select.POLLIN | select.POLLERR | select.POLLHUP
                if self.uart_tx_queue:
                    events |= select.POLLOUT
                poll.register(self.periph_fd, events)
            for chan in self.channels.values():
                events = select.POLLIN | select.POLLERR | select.POLLHUP
                if chan.tx_queue:
                    events |= select.POLLOUT
                poll.register(chan.master_fd, events)

            try:
                events = poll.poll(1)  # 1ms keeps polled-slave latency low
            except OSError as e:
                if e.args[0] == errno.EINTR:
                    continue
                raise

            for fd, event in events:
                link = self.fd_to_link.get(fd)
                if link is not None:
                    if event & select.POLLIN:
                        self.handle_serial_read(link)
                    if event & select.POLLOUT:
                        self.handle_serial_write(link)
                    if event & (select.POLLERR | select.POLLHUP):
                        logging.error("Error/HUP on %s serial port!", link)
                        self.running = False
                        break
                elif fd in self.fd_to_channel:
                    chan = self.fd_to_channel[fd]
                    if event & (select.POLLERR | select.POLLHUP):
                        self.handle_channel_hup(chan, poll)
                        continue
                    if event & select.POLLIN:
                        self.handle_channel_read(chan, poll)
                    if event & select.POLLOUT:
                        self._flush_pty(chan)

            if self.tap is not None:
                self.tap.stall("BUS", self.uart_parser)
                self.tap.maybe_flush()
                if self.periph_fd is not None:
                    self.tap.poll_icount(self.periph_fd)

            now = time.time()
            if now - last_stat_time > 30.0:
                last_stat_time = now
                self.log_stats()

    def _service_bus(self, now=None):
        """One arbiter tick.  Split out from run() so tests can drive it."""
        if self.periph_fd is None:
            return
        if now is None:
            now = time.monotonic()
        # Independently of any tap, drop a half-frame once the line has been
        # quiet long enough that the rest cannot still be arriving.
        self.uart_parser.idle_clear(now=now, idle_time=self.parser_idle_time)
        if self.bus_busy:
            if (self.bus_busy_since is not None
                    and now - self.bus_busy_since > self.bus_hard_timeout):
                logging.warning("Bus hard watchdog from 0x%02X",
                                self.bus_active_dest)
                self._release_bus("hard watchdog from 0x%02X"
                                  % (self.bus_active_dest,), timed_out=True)
            elif (self.bus_release_time is not None
                  and now >= self.bus_release_time
                  and not self.uart_tx_queue):
                logging.debug("Bus idle after response from 0x%02X",
                              self.bus_active_dest)
                self._release_bus("ack from 0x%02X"
                                  % (self.bus_active_dest,))
            else:
                last_activity = self.bus_tx_time
                if (self.bus_last_byte_time
                        and (last_activity is None
                             or self.bus_last_byte_time > last_activity)):
                    last_activity = self.bus_last_byte_time
                if (last_activity is not None
                        and now - last_activity > self.bus_timeout):
                    logging.debug("Bus timeout from 0x%02X, releasing",
                                  self.bus_active_dest)
                    self._release_bus("timeout from 0x%02X"
                                      % (self.bus_active_dest,),
                                      timed_out=True)
        if not self.bus_busy:
            self._dispatch_next_bus_packet()

    def _release_bus(self, reason="idle", timed_out=False, kind=None):
        dest = self.bus_active_dest
        now = time.monotonic()
        if self.tap is not None:
            self.tap.event("release: " + reason)
        self.bus_busy = False
        self.bus_active_dest = 0
        self.bus_request_seq = None
        self.bus_release_time = None
        self.bus_tx_time = None
        self.bus_busy_since = None
        self.bus_last_byte_time = 0.0
        # NOTE: the echo FIFO is deliberately NOT cleared here.  The
        # transceiver can mirror our request after the transaction has ended.
        self.bus_last_release = kind or ("timeout" if timed_out else "ack")
        if timed_out:
            # Discard any half-parsed frame from the failed transaction and
            # hold the bus quiet briefly before the next dispatch.
            self.uart_parser.buf.clear()
            self.bus_recovery_until = now + self.bus_recovery_guard
            if dest:
                self._register_timeout(dest, now)
        self._dispatch_next_bus_packet()

    def _register_timeout(self, dest: int, now: float):
        count = self.consecutive_timeouts.get(dest, 0) + 1
        if count < self.quarantine_threshold:
            self.consecutive_timeouts[dest] = count
            return
        self.consecutive_timeouts[dest] = 0
        self.quarantine_until[dest] = now + self.quarantine_time
        # Do not drop the node's queue: the quarantine delays dispatch, so
        # Klippy's reset/identify packets still reach the MCU after 1s.
        logging.warning("Quarantining 0x%02X for %.1fs after %d timeouts",
                        dest, self.quarantine_time, count)
        if self.tap is not None:
            self.tap.event("quarantine 0x%02X (%d timeouts)"
                           % (dest, count))

    def _drop_queued_for(self, dest: int):
        if not self.bus_pending_queue:
            return
        self.bus_pending_queue = collections.deque(
            item for item in self.bus_pending_queue if item[0] != dest)

    def _enqueue_bus_packet(self, dest: int, pkt: bytes):
        """All downstream traffic goes through here (no direct-start bypass)."""
        now = time.monotonic()
        if now < self.quarantine_until.get(dest, 0.0):
            # Delay, do not drop: the node may be recovering and Klippy's
            # reset/identify must still get through once the quarantine ends.
            if self.tap is not None:
                self.tap.event("quarantine delay 0x%02X" % dest)
        # Bound the per-node backlog so a retransmitting dead node cannot make
        # the queue grow without limit.  Drop its oldest queued packet.
        queued = [i for i, (d, _) in enumerate(self.bus_pending_queue)
                  if d == dest]
        if len(queued) >= self.bus_queue_per_node_max:
            del self.bus_pending_queue[queued[0]]
            logging.warning("Node 0x%02X queue full; dropped oldest packet",
                            dest)
        self.bus_pending_queue.append((dest, pkt))
        self._dispatch_next_bus_packet()

    def _dispatch_next_bus_packet(self):
        if self.bus_busy:
            return
        # Never start a new transaction while the previous frame is still in
        # the transmit FIFO, and honour the post-timeout recovery guard.
        if self.uart_tx_queue:
            return
        now = time.monotonic()
        if now < self.bus_recovery_until:
            return
        # Rotate quarantined nodes to the back instead of dropping them, so a
        # healthy node proceeds immediately and the quarantined node's queue
        # (including Klippy's reset) is dispatched after the quarantine ends.
        for _ in range(len(self.bus_pending_queue)):
            dest, pkt = self.bus_pending_queue.popleft()
            if now < self.quarantine_until.get(dest, 0.0):
                self.bus_pending_queue.append((dest, pkt))
                continue
            self._start_bus_packet(dest, pkt)
            return

    def _start_bus_packet(self, dest: int, pkt: bytes):
        self.bus_busy = True
        self.bus_active_dest = dest
        self.bus_request_seq = pkt[1] & MESSAGE_SEQ_MASK
        self.bus_tx_time = None
        self.bus_release_time = None
        self.bus_busy_since = time.monotonic()
        self.bus_last_byte_time = 0.0
        self.bus_last_activity = self.bus_busy_since
        # Record the exact frame we are about to put on the bus.  The
        # transceiver mirrors it back; the node's real response can only
        # arrive later and is a different frame.
        self.echo_fifo.append((pkt, self.bus_busy_since))
        while len(self.echo_fifo) > self.echo_fifo_max:
            self.echo_fifo.popleft()
        if self.tap is not None:
            self.tap.packet("REQ  ->", pkt)
        self.uart_tx_queue.extend(pkt)
        if self.periph_fd is not None:
            self.handle_serial_write('periph')

    def _note_bus_packet(self, dest: int, pkt: bytes):
        """Update the transaction state from a packet received on the bus."""
        if not (self.bus_busy and dest == self.bus_active_dest):
            return
        now = time.monotonic()
        self.bus_tx_time = now
        self.bus_last_byte_time = now
        if len(pkt) == MESSAGE_MIN:
            seq = pkt[1] & MESSAGE_SEQ_MASK
            expected = ((self.bus_request_seq + 1) & MESSAGE_SEQ_MASK
                        if self.bus_request_seq is not None else None)
            if expected is not None and seq == expected:
                # The slave's terminating ACK/NAK, correlated by sequence.
                self.consecutive_timeouts[dest] = 0
                self.bus_release_time = now + self.bus_guard
                return
            # A stale 5-byte frame for another request: it is still forwarded
            # upstream by the caller, but it must not end this transaction.
        # Data that arrives after the ACK extends the release deadline; data
        # before any ACK keeps it unset (wait for ACK or inactivity timeout).
        if self.bus_release_time is not None:
            self.bus_release_time = now + self.bus_guard

    def _purge_echoes(self, now):
        # Only age out expected echoes once the line itself has been idle for
        # a safe interval; an echo is a near-instant mirror of our own TX.
        if (self.bus_last_activity
                and now - self.bus_last_activity <= self.echo_window):
            return
        while (self.echo_fifo
               and now - self.echo_fifo[0][1] > self.echo_window):
            self.echo_fifo.popleft()

    def _is_echo(self, pkt: bytes) -> bool:
        now = time.monotonic()
        self._purge_echoes(now)
        for i, (epkt, _etime) in enumerate(self.echo_fifo):
            if pkt == epkt:
                del self.echo_fifo[i]
                return True
        return False

    def _flush_slave_input(self, chan: "VirtualChannel"):
        if chan.slave_fd is None:
            return
        try:
            termios.tcflush(chan.slave_fd, termios.TCIFLUSH)
        except OSError:
            pass

    def _reset_session_state(self):
        """Discard buffered traffic from a previous Klippy session.

        Heuristic: the first request after the bus has been idle for
        session_idle_time is treated as a new client.  (The PTY pair itself is
        kept open: closing the slave would HUP-loop the master.)
        """
        logging.info("New Klippy session suspected; clearing stale bus/PTY "
                     "state")
        if self.tap is not None:
            self.tap.event("new session: clear stale state")
        self.bus_pending_queue.clear()
        for chan in self.channels.values():
            chan.tx_queue.clear()
            self._flush_slave_input(chan)

    # ------------------------------------------------------------ serial I/O
    def handle_serial_read(self, link: str):
        fd = self.main_fd if link == 'main' else self.periph_fd
        try:
            data = os.read(fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            logging.error("%s UART read error: %s", link, e)
            return
        if not data:
            return
        if link == 'periph':
            now = time.monotonic()
            self.bus_last_byte_time = now
            self.bus_last_activity = now

        if link == 'main':
            # Direct motherboard link: pass bytes through untouched.
            chan = self.dest_to_channel.get(DEFAULT_DEST)
            if chan is None:
                return
            self._write_pty(chan, data)
            return

        self.uart_parser.feed(data)
        while True:
            pkt = self.uart_parser.next_packet()
            if pkt is None:
                break

            # Drop our own transceiver echo before treating the frame as a
            # response from a peripheral node.
            if self._is_echo(pkt):
                logging.debug("Dropped local transceiver echo len=%d", len(pkt))
                if self.tap is not None:
                    self.tap.echo(pkt)
                continue

            dest = pkt[1] & 0xF0
            if self.tap is not None:
                self.tap.packet("RESP <-", pkt)
            target_chan = self.dest_to_channel.get(dest)
            if target_chan is None:
                logging.warning("Peripheral packet with unknown DEST=0x%02X",
                                dest)
                continue

            # Only the terminating ACK may end a polled transaction.
            self._note_bus_packet(dest, pkt)

            # Upstream rewrite: Klippy expects DEST = 0x10.
            out_pkt = rewrite_packet_dest(pkt, new_dest=DEFAULT_DEST)
            self._write_pty(target_chan, out_pkt)
            target_chan.rx_packets += 1
            target_chan.rx_bytes += len(out_pkt)

    def handle_serial_write(self, link: str):
        if link == 'main':
            fd, queue = self.main_fd, self.main_tx_queue
        else:
            fd, queue = self.periph_fd, self.uart_tx_queue
        if not queue or fd is None:
            return
        try:
            written = os.write(fd, queue)
            del queue[:written]
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                logging.error("%s UART write error: %s", link, e)
                queue.clear()
                if link == 'periph':
                    # Never leave the arbiter wedged on a broken write, but
                    # still go through the normal release/dispatch path.
                    self.uart_parser.buf.clear()
                    self.bus_recovery_until = max(
                        self.bus_recovery_until,
                        time.monotonic() + self.bus_recovery_guard)
                    self._release_bus("write error on 0x%02X"
                                      % (self.bus_active_dest,),
                                      kind="write-error")
        if (link == 'periph' and not queue and self.bus_busy
                and self.bus_tx_time is None and self.bus_release_time is None):
            self.bus_tx_time = time.monotonic()

    def _flush_pty(self, chan: VirtualChannel):
        if not chan.tx_queue or chan.master_fd is None:
            return
        try:
            written = os.write(chan.master_fd, chan.tx_queue)
            del chan.tx_queue[:written]
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            logging.warning("Failed to write to channel %s: %s",
                            chan.name, e)
            chan.tx_queue.clear()

    def _write_pty(self, chan: VirtualChannel, data: bytes):
        chan.tx_queue.extend(data)
        self._flush_pty(chan)

    def handle_channel_hup(self, chan: VirtualChannel,
                           poll: Optional[select.poll] = None):
        """The PTY client went away.

        Keep the PTY pair (and its stable symlink) open.  Closing the slave
        would make the master HUP-loop before the next client connects, so we
        only discard buffered state; the next Klippy session opens the same
        device node.
        """
        logging.info("Channel %s client disconnected; keeping PTY %s",
                     chan.name, chan.symlink_path)
        chan.tx_queue.clear()
        chan.parser.buf.clear()

    def handle_channel_read(self, chan: VirtualChannel, poll: select.poll):
        try:
            data = os.read(chan.master_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno == errno.EIO:  # PTY slave disconnected by client
                self.handle_channel_hup(chan, poll)
                return
            logging.warning("Channel %s read error: %s", chan.name, e)
            return
        if not data:
            self.handle_channel_hup(chan, poll)
            return

        if chan.node_id == DEFAULT_DEST:
            # Main MCU: raw passthrough, no DEST rewriting.
            self.main_tx_queue.extend(data)
            self.handle_serial_write('main')
            return

        chan.rx_bytes += len(data)
        chan.parser.feed(data)

        # New-session heuristic: a request arriving after a long quiet period
        # is the first command of a (re)started Klippy.  Drop buffered state
        # from the previous session before queueing it.
        now = time.monotonic()
        if (self.bus_last_activity
                and now - self.bus_last_activity > self.session_idle_time):
            self._reset_session_state()
        self.bus_last_activity = now

        while True:
            pkt = chan.parser.next_packet()
            if pkt is None:
                break
            chan.rx_packets += 1
            out_pkt = rewrite_packet_dest(pkt, new_dest=chan.node_id)
            # Every request goes through the bounded per-node queue; the
            # arbiter decides when the bus is free.
            self._enqueue_bus_packet(chan.node_id, out_pkt)

    def log_stats(self):
        stats = []
        for name, chan in self.channels.items():
            stats.append(f"{name}[rx_pkt={chan.rx_packets}]")
        logging.info("STATS: %s | main_q=%d periph_q=%d",
                     " ".join(stats), len(self.main_tx_queue),
                     len(self.uart_tx_queue))


def main():
    parser = argparse.ArgumentParser(
        description="Maestro Grand 2 IDEX Klipper serial multiplexer")
    parser.add_argument("--main-port", default=None,
                        help="Motherboard UART, e.g. /dev/ttyS1")
    parser.add_argument("--periph-port", default=None,
                        help="Toolhead/bed bus UART, e.g. /dev/ttyS0")
    parser.add_argument("--baud", type=int, default=250000)
    parser.add_argument("--main-path", default="/tmp/klipper_mcu_main")
    parser.add_argument("--head0-path", default="/tmp/klipper_mcu_head0")
    parser.add_argument("--head1-path", default="/tmp/klipper_mcu_head1")
    parser.add_argument("--bed-path", default="/tmp/klipper_mcu_bed")
    parser.add_argument("--tap-log", default=None,
                        help="Write a bus protocol/error tap log to this file")
    parser.add_argument("--bus-guard", type=float, default=0.003,
                        help="Silence (s) required before releasing the bus")
    parser.add_argument("--bus-timeout", type=float, default=0.012,
                        help="Inactivity timeout (s) for a transaction")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    if not args.main_port and not args.periph_port:
        parser.error("at least one of --main-port / --periph-port is required")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    channel_configs = {
        'main': (NODE_DEST_MAP['main'], args.main_path),
        'head0': (NODE_DEST_MAP['head0'], args.head0_path),
        'head1': (NODE_DEST_MAP['head1'], args.head1_path),
        'bed': (NODE_DEST_MAP['bed'], args.bed_path),
    }

    tap = BusTap(args.tap_log) if args.tap_log else None
    mux = MaestroMultiplexer(channel_configs=channel_configs,
                             main_port=args.main_port,
                             periph_port=args.periph_port,
                             baud_rate=args.baud, tap=tap,
                             bus_guard=args.bus_guard,
                             bus_timeout=args.bus_timeout)

    def sig_handler(signum, frame):
        logging.info("Caught signal %d, shutting down...", signum)
        mux.running = False

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        mux.setup()
        mux.run()
    except Exception as e:
        logging.exception("Multiplexer fatal error: %s", e)
        sys.exit(1)
    finally:
        mux.cleanup()
        logging.info("Multiplexer stopped.")


if __name__ == "__main__":
    main()
