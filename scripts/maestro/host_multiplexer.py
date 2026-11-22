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
    """Stream parser for extracting framed Klipper messages using a resilient sliding window."""
    def __init__(self, name: str = "", allowed_dests=None, tap=None, label=None):
        self.name = name
        self.allowed_dests = allowed_dests or VALID_DESTS
        self.buf = bytearray()
        self.bad_sync_count = 0
        self.tap = tap
        self.label = label or name

    def _report(self, reason):
        if self.tap is not None:
            self.tap.parse_error(self.label, reason, self.buf)

    def feed(self, data: bytes):
        self.buf.extend(data)

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
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            # Check DEST field in SEQ byte
            msgseq = self.buf[1]
            dest = msgseq & ~MESSAGE_SEQ_MASK
            if dest not in self.allowed_dests:
                self._report("bad-dest")
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            if len(self.buf) < msglen:
                # Incomplete packet, await more bytes
                return None

            # Verify sync trailer byte
            if self.buf[msglen - 1] != MESSAGE_SYNC:
                self._report("bad-trailer")
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            # Verify CRC16
            msgcrc = (self.buf[msglen - 3] << 8) | self.buf[msglen - 2]
            crc = crc16_ccitt(self.buf[:msglen - 3])
            if crc != msgcrc:
                self._report("bad-crc")
                del self.buf[:1]
                self.bad_sync_count += 1
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
    TIOCGICOUNT = 0x545D

    def __init__(self, path):
        self.fh = None
        self.last_error_time = 0.0
        self.last_error_key = None
        self.stall_len = 0
        self.stall_time = 0.0
        self.stall_logged = False
        self.icount = None
        self.icount_time = 0.0
        if not path:
            return
        try:
            self.fh = open(path, "a", buffering=1)
            self._write("TAP started")
        except OSError as e:
            logging.warning("Tap log %s disabled: %s", path, e)

    def close(self):
        if self.fh is not None:
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
            self.fh.write("%s %s\n" % (stamp, msg))
        except Exception:
            pass

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
        """Detect a frame that started with a valid header but stopped."""
        now = time.monotonic()
        n = len(parser.buf)
        if n == 0:
            self.stall_len = 0
            self.stall_logged = False
            return
        if n != self.stall_len:
            self.stall_len = n
            self.stall_time = now
            self.stall_logged = False
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
        if now - self.stall_time > 0.050:
            self._write("RESYNC %s dropped=%d hex=%s"
                        % (label, n, self._hex(buf, 32)))
            buf.clear()
            self.stall_len = 0
            self.stall_logged = False

    def poll_icount(self, fd, label="BUS"):
        """Log deltas of the kernel UART error counters (framing/overrun/...)."""
        now = time.monotonic()
        if now - self.icount_time < 1.0:
            return
        self.icount_time = now
        fmt = "I" * len(self.ICOUNT_FIELDS)
        try:
            buf = bytearray(struct.pack(fmt,
                             *([0] * len(self.ICOUNT_FIELDS))))
            res = fcntl.ioctl(fd, self.TIOCGICOUNT, buf)
            vals = struct.unpack(fmt, res)
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
                 bus_guard=0.003, bus_timeout=0.050):
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

        # Peripheral link.
        self.uart_parser = KlipperPacketParser(name="periph",
                                               allowed_dests=PERIPH_DESTS,
                                               tap=tap, label="BUS")
        self.uart_tx_queue = bytearray()

        # Half-Duplex Multi-Drop Bus Arbiter for the peripheral transceiver.
        self.bus_busy = False
        self.bus_active_dest = 0
        self.bus_tx_time: Optional[float] = None
        self.bus_release_time: Optional[float] = None
        self.bus_timeout = bus_timeout  # response timeout after the request
        self.bus_guard = bus_guard      # silence required after the last packet
        self.bus_pending_queue = collections.deque()

        # Local transceiver echo of our own transmitted frame.
        self.pending_echo: Optional[bytes] = None
        self.pending_echo_time = 0.0
        self.echo_window = 0.050

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
            # Peripheral bus arbitration.  Release only after BUS_GUARD seconds
            # of silence following the last packet, or on a response timeout.
            now = time.monotonic()
            if self.periph_fd is not None and self.bus_busy:
                if self.bus_release_time is not None:
                    if now >= self.bus_release_time and not self.uart_tx_queue:
                        logging.debug("Bus idle after response from 0x%02X",
                                      self.bus_active_dest)
                        self._release_bus("idle after 0x%02X"
                                          % (self.bus_active_dest,))
                elif (self.bus_tx_time is not None
                      and now - self.bus_tx_time > self.bus_timeout):
                    logging.debug("Bus timeout from 0x%02X, releasing",
                                  self.bus_active_dest)
                    self._release_bus("timeout from 0x%02X"
                                      % (self.bus_active_dest,))

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

            try:
                events = poll.poll(5)  # 5ms for responsive half-duplex handling
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

            if self.tap is not None:
                self.tap.stall("BUS", self.uart_parser)
                if self.periph_fd is not None:
                    self.tap.poll_icount(self.periph_fd)

            now = time.time()
            if now - last_stat_time > 30.0:
                last_stat_time = now
                self.log_stats()

    def _release_bus(self, reason="idle"):
        if self.tap is not None:
            self.tap.event("release: " + reason)
        self.bus_busy = False
        self.bus_active_dest = 0
        self.bus_release_time = None
        self.bus_tx_time = None
        self._dispatch_next_bus_packet()

    def _start_bus_packet(self, dest: int, pkt: bytes):
        self.bus_busy = True
        self.bus_active_dest = dest
        self.bus_tx_time = None
        self.bus_release_time = None
        # Arm the echo filter with the exact frame we are about to put on the
        # bus.  The transceiver mirrors it back; the node's real response can
        # only arrive later and is a different frame.
        self.pending_echo = pkt
        self.pending_echo_time = time.monotonic()
        if self.tap is not None:
            self.tap.packet("REQ  ->", pkt)
        self.uart_tx_queue.extend(pkt)
        if self.periph_fd is not None:
            self.handle_serial_write('periph')

    def _dispatch_next_bus_packet(self):
        if not self.bus_busy and self.bus_pending_queue:
            dest, pkt = self.bus_pending_queue.popleft()
            self._start_bus_packet(dest, pkt)

    def _is_echo(self, pkt: bytes) -> bool:
        if self.pending_echo is None:
            return False
        if time.monotonic() - self.pending_echo_time > self.echo_window:
            self.pending_echo = None
            return False
        if pkt == self.pending_echo:
            self.pending_echo = None
            return True
        return False

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

        if link == 'main':
            # Direct motherboard link: pass bytes through untouched.
            chan = self.dest_to_channel.get(DEFAULT_DEST)
            if chan is None:
                return
            try:
                os.write(chan.master_fd, data)
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    logging.warning("Failed to write to main PTY: %s", e)
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

            # Keep the bus busy until a short silence follows the last packet.
            if self.bus_busy and dest == self.bus_active_dest:
                self.bus_tx_time = time.monotonic()
                self.bus_release_time = self.bus_tx_time + self.bus_guard

            # Upstream rewrite: Klippy expects DEST = 0x10.
            out_pkt = rewrite_packet_dest(pkt, new_dest=DEFAULT_DEST)
            try:
                os.write(target_chan.master_fd, out_pkt)
                target_chan.rx_packets += 1
                target_chan.rx_bytes += len(out_pkt)
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    logging.warning("Failed to write to channel %s: %s",
                                    target_chan.name, e)

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
        if (link == 'periph' and not queue and self.bus_busy
                and self.bus_tx_time is None and self.bus_release_time is None):
            self.bus_tx_time = time.monotonic()

    def handle_channel_hup(self, chan: VirtualChannel, poll: select.poll):
        old_fd = chan.master_fd
        if old_fd in self.fd_to_channel:
            del self.fd_to_channel[old_fd]
        chan.reopen(poll)
        self.fd_to_channel[chan.master_fd] = chan
        logging.info("Channel %s PTY reset (new fd=%d)",
                     chan.name, chan.master_fd)

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
        while True:
            pkt = chan.parser.next_packet()
            if pkt is None:
                break
            chan.rx_packets += 1
            out_pkt = rewrite_packet_dest(pkt, new_dest=chan.node_id)
            if not self.bus_busy:
                self._start_bus_packet(chan.node_id, out_pkt)
            else:
                self.bus_pending_queue.append((chan.node_id, out_pkt))

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
    parser.add_argument("--bus-timeout", type=float, default=0.050,
                        help="Response timeout (s) after a request")
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
