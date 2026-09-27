#!/usr/bin/env python3
"""
Maestro Grand 2 IDEX - Host Serial Proxy Multiplexer
Part of Klipper port for Maestro Grand 2 3D printer.

This daemon runs on the host (Orange Pi Zero) and creates 4 virtual pseudo-terminals (PTY):
  - /tmp/klipper_mcu_main  (Main MCU: ATmega1284p, DEST = 0x10)
  - /tmp/klipper_mcu_head0 (Toolhead 0 MCU: ATmega16A, DEST = 0x20)
  - /tmp/klipper_mcu_head1 (Toolhead 1 MCU: ATmega16A, DEST = 0x30)
  - /tmp/klipper_mcu_bed   (Heated Bed MCU: ATmega16A, DEST = 0x40)

All 4 virtual streams are multiplexed onto a single physical serial port (/dev/ttyS0)
connected to the Motherboard ATmega1284p gateway router.

Downstream (Klippy -> Physical UART):
  - Packets from /tmp/klipper_mcu_head0 have their SEQ DEST field rewritten to 0x20,
    CRC16 is recalculated, and the frame is transmitted to physical UART.
  - Packets from /tmp/klipper_mcu_head1 are rewritten to 0x30 with CRC recalculated.
  - Packets from /tmp/klipper_mcu_bed are rewritten to 0x40 with CRC recalculated.
  - Packets from /tmp/klipper_mcu_main (0x10) pass through without rewriting.

Upstream (Physical UART -> Klippy):
  - Packets from physical UART are demultiplexed by DEST ID in the SEQ byte:
    - 0x10 -> forwarded to /tmp/klipper_mcu_main
    - 0x20 -> DEST rewritten back to 0x10 (with CRC recalculated) -> /tmp/klipper_mcu_head0
    - 0x30 -> DEST rewritten back to 0x10 (with CRC recalculated) -> /tmp/klipper_mcu_head1
    - 0x40 -> DEST rewritten back to 0x10 (with CRC recalculated) -> /tmp/klipper_mcu_bed

To Klippy, each MCU looks like a completely independent, standard point-to-point serial device.
"""

import os
import sys
import time
import errno
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

class KlipperPacketParser:
    """Stream parser for extracting framed Klipper messages using a resilient sliding window."""
    def __init__(self, name: str = "", allowed_dests=None):
        self.name = name
        self.allowed_dests = allowed_dests or VALID_DESTS
        self.buf = bytearray()
        self.bad_sync_count = 0

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
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            # Check DEST field in SEQ byte
            msgseq = self.buf[1]
            dest = msgseq & ~MESSAGE_SEQ_MASK
            if dest not in self.allowed_dests:
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            if len(self.buf) < msglen:
                # Incomplete packet, await more bytes
                return None

            # Verify sync trailer byte
            if self.buf[msglen - 1] != MESSAGE_SYNC:
                del self.buf[:1]
                self.bad_sync_count += 1
                continue

            # Verify CRC16
            msgcrc = (self.buf[msglen - 3] << 8) | self.buf[msglen - 2]
            crc = crc16_ccitt(self.buf[:msglen - 3])
            if crc != msgcrc:
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
    def __init__(self, name: str, node_id: int, symlink_path: str):
        self.name = name
        self.node_id = node_id
        self.symlink_path = symlink_path
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.slave_name: str = ""
        self.parser = KlipperPacketParser(name=name)
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_packets = 0
        self.rx_packets = 0

    def open(self):
        import pty
        import tty
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_name = os.ttyname(self.slave_fd)
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)

        # Set non-blocking on master_fd
        flags = os.fcntl(self.master_fd, os.F_GETFL)
        os.fcntl(self.master_fd, os.F_SETFL, flags | os.O_NONBLOCK)

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
        self.parser = KlipperPacketParser(name=self.name)
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


class MaestroMultiplexer:
    """Main multiplexer engine coordinating UART and PTYs."""
    def __init__(self, serial_port: str, baud_rate: int, channel_configs: Dict[str, Tuple[int, str]]):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.channel_configs = channel_configs
        self.channels: Dict[str, VirtualChannel] = {}
        self.fd_to_channel: Dict[int, VirtualChannel] = {}
        self.dest_to_channel: Dict[int, VirtualChannel] = {}
        self.ser_dev = None
        self.serial_fd: Optional[int] = None
        self.uart_parser = KlipperPacketParser(name="UART")
        self.running = False
        self.uart_tx_queue = bytearray()

        # Half-Duplex Multi-Drop Bus Arbiter for MCP2561 peripherals (0x20, 0x30, 0x40)
        self.bus_busy: bool = False
        self.bus_active_dest: int = 0
        self.bus_tx_time: float = 0.0
        self.bus_timeout: float = 0.020  # 20ms timeout
        self.bus_pending_queue = collections.deque()

    def open_serial(self):
        logging.info("Opening physical serial port %s at %d baud...", self.serial_port, self.baud_rate)
        # Using pyserial guarantees accurate configuration of 250000 baud via Linux termios2
        try:
            import serial
            self.ser_dev = serial.Serial(self.serial_port, baudrate=self.baud_rate, timeout=0)
            self.ser_dev.reset_input_buffer()
            self.ser_dev.reset_output_buffer()
            self.serial_fd = self.ser_dev.fileno()
        except ImportError:
            logging.critical(
                "pyserial is required for reliable 250000 baud support. "
                "Install with: pip3 install pyserial"
            )
            raise SystemExit(1)

        # Ensure non-blocking
        flags = os.fcntl(self.serial_fd, os.F_GETFL)
        os.fcntl(self.serial_fd, os.F_SETFL, flags | os.O_NONBLOCK)
        logging.info("Physical serial port %s opened successfully (fd=%d)", self.serial_port, self.serial_fd)

    def setup(self):
        self.open_serial()
        for name, (node_id, symlink_path) in self.channel_configs.items():
            chan = VirtualChannel(name=name, node_id=node_id, symlink_path=symlink_path)
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
        if self.ser_dev is not None:
            try:
                self.ser_dev.close()
            except Exception:
                pass
            self.ser_dev = None
            self.serial_fd = None
        elif self.serial_fd is not None:
            try:
                os.close(self.serial_fd)
            except OSError:
                pass
            self.serial_fd = None

    def handle_channel_hup(self, chan: VirtualChannel, poll: select.poll):
        old_fd = chan.master_fd
        if old_fd in self.fd_to_channel:
            del self.fd_to_channel[old_fd]
        chan.reopen(poll)
        self.fd_to_channel[chan.master_fd] = chan
        logging.info("Channel %s PTY reset and ready for new connection (new fd=%d)", chan.name, chan.master_fd)

    def run(self):
        self.running = True
        poll = select.poll()

        # Register physical UART
        poll.register(self.serial_fd, select.POLLIN | select.POLLERR | select.POLLHUP)

        # Register channel master FDs
        for chan in self.channels.values():
            poll.register(chan.master_fd, select.POLLIN | select.POLLERR | select.POLLHUP)

        logging.info("Maestro Multiplexer active. Listening on all channels.")

        last_stat_time = time.time()

        while self.running:
            # Check for bus timeout on multi-drop peripheral bus (20ms)
            now = time.time()
            if self.bus_busy and (now - self.bus_tx_time > self.bus_timeout):
                logging.debug("Bus timeout waiting for response from 0x%02X, releasing bus", self.bus_active_dest)
                self.bus_busy = False
                self.bus_active_dest = 0
                self._dispatch_next_bus_packet()

            # Adjust poll events for UART write if there is pending data
            uart_events = select.POLLIN | select.POLLERR | select.POLLHUP
            if self.uart_tx_queue:
                uart_events |= select.POLLOUT
            poll.register(self.serial_fd, uart_events)

            try:
                events = poll.poll(5) # 5ms timeout for responsive half-duplex arbitration
            except select.error as e:
                if e.args[0] == errno.EINTR:
                    continue
                raise

            for fd, event in events:
                # 1. Event on Physical Serial Port
                if fd == self.serial_fd:
                    if event & select.POLLIN:
                        self.handle_serial_read()
                    if event & select.POLLOUT:
                        self.handle_serial_write()
                    if event & (select.POLLERR | select.POLLHUP):
                        logging.error("Error/HUP on physical serial port!")
                        self.running = False
                        break

                # 2. Event on Virtual PTY Master (from Klippy)
                elif fd in self.fd_to_channel:
                    chan = self.fd_to_channel[fd]
                    if event & (select.POLLERR | select.POLLHUP):
                        self.handle_channel_hup(chan, poll)
                        continue
                    if event & select.POLLIN:
                        self.handle_channel_read(chan, poll)

            # Periodic stats logging every 30 seconds
            now = time.time()
            if now - last_stat_time > 30.0:
                last_stat_time = now
                self.log_stats()

    def _dispatch_next_bus_packet(self):
        """Dispatches the next queued packet to the MCP2561 multi-drop bus."""
        if not self.bus_busy and self.bus_pending_queue:
            dest, pkt = self.bus_pending_queue.popleft()
            self.bus_busy = True
            self.bus_active_dest = dest
            self.bus_tx_time = time.time()
            self.uart_tx_queue.extend(pkt)
            self.handle_serial_write()

    def handle_serial_read(self):
        try:
            data = os.read(self.serial_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            logging.error("UART read error: %s", e)
            return

        if not data:
            return

        self.uart_parser.feed(data)
        while True:
            pkt = self.uart_parser.next_packet()
            if pkt is None:
                break

            # Demultiplex packet by DEST ID in SEQ byte
            dest = pkt[1] & 0xF0
            target_chan = self.dest_to_channel.get(dest)

            if target_chan is None:
                # Unknown DEST, log warning
                logging.warning("UART received packet with unknown DEST=0x%02X, len=%d", dest, len(pkt))
                continue

            # If response came from the currently polled peripheral node,
            # verify if this is the terminating ACK/NAK packet (len == MESSAGE_MIN, 5 bytes).
            # In Klipper protocol, command responses may include multi-packet data
            # (e.g. ADC/temperature reports) which are always terminated by an ACK/NAK (5 bytes).
            if self.bus_busy and dest == self.bus_active_dest:
                if len(pkt) == MESSAGE_MIN:
                    # Final ACK/NAK packet: peripheral node has finished its response burst
                    self.bus_busy = False
                    self.bus_active_dest = 0
                    self._dispatch_next_bus_packet()
                else:
                    # Pre-ACK data packet: peripheral is still transmitting,
                    # refresh bus activity timer to allow ACK to follow without timeout
                    self.bus_tx_time = time.time()

            # Upstream rewrite: Klippy expects DEST = 0x10
            if dest == DEFAULT_DEST:
                out_pkt = pkt
            else:
                out_pkt = rewrite_packet_dest(pkt, new_dest=DEFAULT_DEST)

            try:
                os.write(target_chan.master_fd, out_pkt)
                target_chan.tx_packets += 1
                target_chan.tx_bytes += len(out_pkt)
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    logging.warning("Failed to write to channel %s: %s", target_chan.name, e)

    def handle_serial_write(self):
        if not self.uart_tx_queue or self.serial_fd is None:
            return
        try:
            written = os.write(self.serial_fd, self.uart_tx_queue)
            del self.uart_tx_queue[:written]
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                logging.error("UART write error: %s", e)

    def handle_channel_read(self, chan: VirtualChannel, poll: select.poll):
        try:
            data = os.read(chan.master_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno == errno.EIO: # PTY slave disconnected by client
                self.handle_channel_hup(chan, poll)
                return
            logging.warning("Channel %s read error: %s", chan.name, e)
            return

        if not data:
            self.handle_channel_hup(chan, poll)
            return

        chan.rx_bytes += len(data)
        chan.parser.feed(data)

        while True:
            pkt = chan.parser.next_packet()
            if pkt is None:
                break

            chan.rx_packets += 1

            # Downstream rewrite: Set node DEST ID
            if chan.node_id == DEFAULT_DEST:
                # Main MCU (0x10): connected locally on USART0, bypasses MCP2561 bus arbiter
                self.uart_tx_queue.extend(pkt)
            else:
                out_pkt = rewrite_packet_dest(pkt, new_dest=chan.node_id)
                # Peripheral node (0x20, 0x30, 0x40): route through half-duplex bus arbiter
                if not self.bus_busy:
                    self.bus_busy = True
                    self.bus_active_dest = chan.node_id
                    self.bus_tx_time = time.time()
                    self.uart_tx_queue.extend(out_pkt)
                else:
                    self.bus_pending_queue.append((chan.node_id, out_pkt))

        # Trigger immediate flush if possible
        if self.uart_tx_queue:
            self.handle_serial_write()

    def log_stats(self):
        stats = []
        for name, chan in self.channels.items():
            stats.append(f"{name}[rx_pkt={chan.rx_packets}, tx_pkt={chan.tx_packets}]")
        logging.info("STATS: %s | UART_TX_PENDING=%d", " ".join(stats), len(self.uart_tx_queue))


def main():
    parser = argparse.ArgumentParser(description="Maestro Grand 2 IDEX Klipper Serial Multiplexer")
    parser.add_argument("--port", default="/dev/ttyS0", help="Physical serial port (default: /dev/ttyS0)")
    parser.add_argument("--baud", type=int, default=250000, help="Physical baud rate (default: 250000)")
    parser.add_argument("--main-path", default="/tmp/klipper_mcu_main", help="Symlink path for main MCU PTY")
    parser.add_argument("--head0-path", default="/tmp/klipper_mcu_head0", help="Symlink path for head0 MCU PTY")
    parser.add_argument("--head1-path", default="/tmp/klipper_mcu_head1", help="Symlink path for head1 MCU PTY")
    parser.add_argument("--bed-path", default="/tmp/klipper_mcu_bed", help="Symlink path for bed MCU PTY")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    channel_configs = {
        'main': (NODE_DEST_MAP['main'], args.main_path),
        'head0': (NODE_DEST_MAP['head0'], args.head0_path),
        'head1': (NODE_DEST_MAP['head1'], args.head1_path),
        'bed': (NODE_DEST_MAP['bed'], args.bed_path),
    }

    mux = MaestroMultiplexer(serial_port=args.port, baud_rate=args.baud, channel_configs=channel_configs)

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
    finally:
        mux.cleanup()
        logging.info("Multiplexer stopped.")


if __name__ == "__main__":
    main()
