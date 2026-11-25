#!/usr/bin/env python3
"""
Unit tests for the Maestro Grand 2 IDEX host multiplexer.

The bus tests drive the real MaestroMultiplexer methods (_service_bus,
_note_bus_packet, _release_bus, _dispatch_next_bus_packet, _is_echo,
_enqueue_bus_packet) rather than re-implementing the arbiter.
"""

import unittest
import sys
import os
import time

# Add parent directory to path to import host_multiplexer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from host_multiplexer import (
    crc16_ccitt,
    rewrite_packet_dest,
    KlipperPacketParser,
    MaestroMultiplexer,
    VirtualChannel,
    MESSAGE_SYNC,
    MESSAGE_MIN,
    DEFAULT_DEST,
    NODE_DEST_MAP,
    PERIPH_DESTS,
)


def safe_close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


class TestMaestroMultiplexer(unittest.TestCase):
    def make_packet(self, seq_byte: int, payload: bytes = b"") -> bytes:
        msglen = len(payload) + 5
        hdr = bytes([msglen, seq_byte]) + payload
        crc = crc16_ccitt(hdr)
        return hdr + bytes([crc >> 8, crc & 0xFF, MESSAGE_SYNC])

    def make_request(self, dest: int, seq: int = 5,
                     payload: bytes = b"CMD") -> bytes:
        """A Klippy-originated request rewritten for `dest`."""
        return rewrite_packet_dest(self.make_packet(DEFAULT_DEST | (seq & 0xF),
                                                    payload), dest)

    def make_ack(self, dest: int, seq: int) -> bytes:
        return self.make_packet(dest | (seq & 0xF), b"")

    def make_mux(self):
        """Mux with a writable periph fd so _start_bus_packet drains its TX."""
        mux = MaestroMultiplexer(channel_configs={})
        fd = os.open(os.devnull, os.O_WRONLY | os.O_NONBLOCK)
        mux.periph_fd = fd
        self.addCleanup(safe_close, fd)
        return mux

    # ------------------------------------------------------------------ CRC
    def test_crc16(self):
        self.assertEqual(crc16_ccitt(b""), 0xFFFF)
        self.assertEqual(crc16_ccitt(b"\x05\x10"), 0x9E81)

    # --------------------------------------------------------------- parser
    def test_parser_complete_packet(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x11, b"TEST")
        parser.feed(pkt)
        self.assertEqual(parser.next_packet(), pkt)
        self.assertIsNone(parser.next_packet())

    def test_parser_fragmented_packet(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x12, b"ABCDEF")
        for i in range(len(pkt) - 1):
            parser.feed(pkt[i:i + 1])
            self.assertIsNone(parser.next_packet())
        parser.feed(pkt[-1:])
        self.assertEqual(parser.next_packet(), pkt)

    def test_parser_back_to_back(self):
        parser = KlipperPacketParser()
        pkt1 = self.make_packet(0x10, b"MSG1")
        pkt2 = self.make_packet(0x11, b"MSG2")
        parser.feed(pkt1 + pkt2)
        self.assertEqual(parser.next_packet(), pkt1)
        self.assertEqual(parser.next_packet(), pkt2)
        self.assertIsNone(parser.next_packet())

    def test_parser_noise_recovery(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x10, b"VALID")
        # Garbage terminated by a frame trailer, as seen on a mid-stream sync.
        noise = b"\x00\xFF\xAA\x7E"
        parser.feed(noise + pkt)
        self.assertEqual(parser.next_packet(), pkt)

    def test_parser_boundary_sizes(self):
        parser = KlipperPacketParser()
        min_pkt = self.make_packet(0x10, b"")
        self.assertEqual(len(min_pkt), 5)
        max_pkt = self.make_packet(0x10, b"X" * (64 - 5))
        self.assertEqual(len(max_pkt), 64)
        parser.feed(min_pkt + max_pkt)
        self.assertEqual(parser.next_packet(), min_pkt)
        self.assertEqual(parser.next_packet(), max_pkt)
        self.assertIsNone(parser.next_packet())

    def test_parser_embedded_sync_in_payload(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x10, b"ABC\x7EDEF")
        parser.feed(pkt)
        self.assertEqual(parser.next_packet(), pkt)

    def test_parser_corrupt_crc(self):
        parser = KlipperPacketParser()
        good_pkt = self.make_packet(0x10, b"GOOD")
        bad_pkt = bytearray(self.make_packet(0x10, b"BAD_DATA"))
        bad_pkt[-2] ^= 0xFF
        parser.feed(bytes(bad_pkt) + good_pkt)
        self.assertEqual(parser.next_packet(), good_pkt)

    def test_crc_with_embedded_sync_bytes(self):
        parser = KlipperPacketParser()
        found = False
        for i in range(1000):
            pkt = self.make_packet(0x10, f"CRC_TEST_{i}".encode())
            if pkt[-3] == MESSAGE_SYNC or pkt[-2] == MESSAGE_SYNC:
                parser.feed(pkt)
                self.assertEqual(parser.next_packet(), pkt)
                found = True
                break
        self.assertTrue(found)

    def test_parser_idle_clear(self):
        parser = KlipperPacketParser(idle_time=0.005)
        parser.feed(b"\x05\x20")
        base = parser.last_feed_time
        self.assertFalse(parser.idle_clear(now=base + 0.0001,
                                           idle_time=0.005))
        self.assertEqual(len(parser.buf), 2)
        self.assertTrue(parser.idle_clear(now=base + 0.010,
                                          idle_time=0.005))
        self.assertEqual(len(parser.buf), 0)

    def test_parser_resync_from_observed_residue(self):
        """Recover from the bad-len / bad-dest residue in the incident tap,
        with no tap configured, and parse the whole following burst."""
        reports = [self.make_packet(0x40 | 6, b"ANALOG_%02d" % i)
                   for i in range(4)]
        ack = self.make_ack(0x40, 6)
        burst = b"".join(reports + [ack])
        residues = [
            bytes.fromhex("02d8f3c210023d03a10f7e"),   # bad-len residue
            bytes.fromhex("10023d03a10f7e"),           # bad-dest residue
        ]
        for residue in residues:
            parser = KlipperPacketParser(name="BUS",
                                         allowed_dests=PERIPH_DESTS,
                                         tap=None)
            parser.feed(residue + burst)
            got = []
            while True:
                pkt = parser.next_packet()
                if pkt is None:
                    break
                got.append(pkt)
            self.assertEqual(got, reports + [ack],
                             "residue %s did not resync" % residue.hex())

    # ------------------------------------------------------- rewrite helpers
    def test_downstream_and_upstream_rewrite(self):
        for _node_name, target_dest in [('head0', 0x20), ('head1', 0x30),
                                        ('bed', 0x40)]:
            original_pkt = self.make_packet(DEFAULT_DEST | 0x05, b"HEATER_SET")
            downstream_pkt = rewrite_packet_dest(original_pkt, target_dest)
            self.assertEqual(downstream_pkt[1], target_dest | 0x05)
            self.assertEqual(downstream_pkt[-1], MESSAGE_SYNC)
            crc = (downstream_pkt[-3] << 8) | downstream_pkt[-2]
            self.assertEqual(crc, crc16_ccitt(downstream_pkt[:-3]))
            self.assertEqual(rewrite_packet_dest(downstream_pkt,
                                                 DEFAULT_DEST),
                             original_pkt)

    # ------------------------------------------------------------- arbiter
    def test_four_report_burst_and_ack_parses_and_releases(self):
        mux = self.make_mux()
        req = self.make_request(0x40, seq=5)
        mux._enqueue_bus_packet(0x40, req)
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x40)
        self.assertEqual(mux.bus_request_seq, 5)

        # Four analog_in_state reports plus the terminating ACK.
        reports = [self.make_packet(0x40 | 6, b"ANALOG_%02d" % i)
                   for i in range(4)]
        ack = self.make_ack(0x40, 6)
        mux.uart_parser.feed(b"".join(reports + [ack]))
        parsed = []
        while True:
            pkt = mux.uart_parser.next_packet()
            if pkt is None:
                break
            parsed.append(pkt)
        self.assertEqual(parsed, reports + [ack])

        for pkt in parsed:
            self.assertFalse(mux._is_echo(pkt))
            mux._note_bus_packet(pkt[1] & 0xF0, pkt)

        self.assertIsNotNone(mux.bus_release_time)

        # A partially received next frame must NOT block the ACK release.
        mux.uart_parser.buf.extend(b"\x05")
        mux._note_bus_packet(0x40, ack)
        self.assertIsNotNone(mux.bus_release_time)
        mux.uart_parser.buf.clear()

        release = mux.bus_release_time
        mux._service_bus(now=release + 1e-6)
        self.assertFalse(mux.bus_busy)
        self.assertEqual(mux.bus_last_release, "ack")

    def test_post_ack_data_extends_release(self):
        mux = self.make_mux()
        mux._enqueue_bus_packet(0x20, self.make_request(0x20, seq=5))

        report = self.make_packet(0x20 | 6, b"REPORT")
        mux._note_bus_packet(0x20, report)
        self.assertIsNone(mux.bus_release_time)  # pre-ACK data not terminal

        mux._note_bus_packet(0x20, self.make_ack(0x20, 6))
        t1 = mux.bus_release_time
        self.assertIsNotNone(t1)

        mux._note_bus_packet(0x20, self.make_packet(0x20 | 6, b"LATE"))
        t2 = mux.bus_release_time
        self.assertIsNotNone(t2, "post-ACK data must extend, not cancel")
        self.assertGreaterEqual(t2, t1)

    def test_timeout_release_respects_tx_queue_and_recovery_guard(self):
        mux = self.make_mux()
        mux._enqueue_bus_packet(0x20, self.make_request(0x20, seq=5))
        mux._enqueue_bus_packet(0x40, self.make_request(0x40, seq=6))
        self.assertEqual(len(mux.bus_pending_queue), 1)

        mux.uart_parser.buf.extend(b"\x05\x20")
        mux.bus_tx_time = time.monotonic() - 1.0
        mux.uart_tx_queue.extend(b"\xAA")  # simulate an undrained write
        mux._service_bus()

        self.assertFalse(mux.bus_busy)
        self.assertEqual(mux.bus_last_release, "timeout")
        self.assertEqual(len(mux.uart_parser.buf), 0,
                         "timeout must clear the parser buffer")
        self.assertGreater(mux.bus_recovery_until, time.monotonic())

        # Must not start the next packet while the TX queue is still non-empty.
        self.assertFalse(mux.bus_busy)
        mux.uart_tx_queue.clear()

        # Must not dispatch inside the recovery guard either.
        mux._dispatch_next_bus_packet()
        self.assertFalse(mux.bus_busy)

        # Once the guard expires the other node proceeds.
        mux.bus_recovery_until = 0.0
        mux._dispatch_next_bus_packet()
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x40)

    def _force_timeout(self, mux, dest):
        mux.bus_busy = True
        mux.bus_active_dest = dest
        mux.bus_request_seq = 0
        mux.bus_busy_since = time.monotonic()
        mux.uart_tx_queue.clear()
        mux._release_bus("timeout from 0x%02X" % dest, timed_out=True)

    def test_three_timeouts_quarantine_node_others_proceed(self):
        mux = self.make_mux()
        for _ in range(3):
            self._force_timeout(mux, 0x20)

        self.assertGreater(mux.quarantine_until.get(0x20, 0.0),
                           time.monotonic())
        self.assertEqual(mux.consecutive_timeouts.get(0x20), 0)

        mux.bus_recovery_until = 0.0
        # Leave a bed packet in the queue, then a head1 packet behind it.
        mux.bus_pending_queue.append((0x20, self.make_request(0x20, seq=1)))
        mux._enqueue_bus_packet(0x30, self.make_request(0x30, seq=2))

        self.assertTrue(mux.bus_busy, "healthy node must proceed immediately")
        self.assertEqual(mux.bus_active_dest, 0x30)
        # Quarantine delays, it does not drop: the 0x20 packet stays queued
        # so Klippy's reset can still reach the MCU after the quarantine.
        self.assertTrue(any(d == 0x20 for d, _ in mux.bus_pending_queue))
        mux.quarantine_until[0x20] = 0.0
        mux.bus_busy = False
        mux.uart_tx_queue.clear()
        mux._dispatch_next_bus_packet()
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x20)

    def test_ack_resets_timeout_counter(self):
        mux = self.make_mux()
        for _ in range(2):
            self._force_timeout(mux, 0x40)
        self.assertEqual(mux.consecutive_timeouts.get(0x40), 2)
        self.assertNotIn(0x40, mux.quarantine_until)

        mux.bus_busy = True
        mux.bus_active_dest = 0x40
        mux.bus_request_seq = 5
        mux.uart_tx_queue.clear()
        mux._note_bus_packet(0x40, self.make_ack(0x40, 6))
        self.assertEqual(mux.consecutive_timeouts.get(0x40), 0)
        self.assertIsNotNone(mux.bus_release_time)

    def test_wrong_seq_frame_forwarded_but_does_not_release(self):
        mux = self.make_mux()  # periph_fd is writable /dev/null
        mux._enqueue_bus_packet(0x20, self.make_request(0x20, seq=5))
        self.assertTrue(mux.bus_busy)

        # Capture upstream output via a pipe on the channel's master fd.
        up_r, up_w = os.pipe()
        os.set_blocking(up_r, False)
        os.set_blocking(up_w, False)
        chan = VirtualChannel('head0', 0x20, '/tmp/test_mcu_head0')
        chan.master_fd = up_w
        mux.dest_to_channel[0x20] = chan

        # Feed bus RX through a pipe instead of the serial port.
        rx_r, rx_w = os.pipe()
        os.set_blocking(rx_r, False)
        mux.periph_fd = rx_r
        self.addCleanup(safe_close, up_r)
        self.addCleanup(safe_close, up_w)
        self.addCleanup(safe_close, rx_r)
        self.addCleanup(safe_close, rx_w)

        stale = self.make_ack(0x20, 7)  # wrong: expected 6
        os.write(rx_w, stale)
        mux.handle_serial_read('periph')

        forwarded = os.read(up_r, 4096)
        self.assertEqual(forwarded,
                         rewrite_packet_dest(stale, DEFAULT_DEST),
                         "stale frame must still go upstream")
        self.assertIsNone(mux.bus_release_time,
                          "wrong-seq terminator must not release")

        os.write(rx_w, self.make_ack(0x20, 6))
        mux.handle_serial_read('periph')
        self.assertIsNotNone(mux.bus_release_time)

    def test_delayed_echo_dropped_after_transaction_boundary(self):
        mux = self.make_mux()
        req = self.make_request(0x20, seq=5)
        mux._enqueue_bus_packet(0x20, req)
        self.assertIn(req, [p for p, _t in mux.echo_fifo])

        # Transaction ends; the transceiver echo arrives afterwards.
        mux._release_bus("ack from 0x20")
        self.assertFalse(mux.bus_busy)
        self.assertTrue(mux._is_echo(req),
                        "delayed echo must survive the release")
        self.assertFalse(mux._is_echo(req), "an echo is consumed once")
        self.assertFalse(mux._is_echo(self.make_packet(0x26, b"RESP")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
