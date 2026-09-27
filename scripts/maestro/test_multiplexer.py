#!/usr/bin/env python3
"""
Unit tests for Maestro Grand 2 IDEX Host Multiplexer.
Validates CRC16, packet framing, parsing, downstream rewriting,
and upstream demultiplexing.
"""

import unittest
import sys
import os

# Add parent directory to path to import host_multiplexer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from host_multiplexer import (
    crc16_ccitt,
    rewrite_packet_dest,
    KlipperPacketParser,
    MESSAGE_SYNC,
    DEFAULT_DEST,
    NODE_DEST_MAP,
)


class TestMaestroMultiplexer(unittest.TestCase):
    def make_packet(self, seq_byte: int, payload: bytes = b"") -> bytes:
        msglen = len(payload) + 5
        hdr = bytes([msglen, seq_byte]) + payload
        crc = crc16_ccitt(hdr)
        return hdr + bytes([crc >> 8, crc & 0xFF, MESSAGE_SYNC])

    def test_crc16(self):
        # Known test vectors
        self.assertEqual(crc16_ccitt(b""), 0xFFFF)
        crc = crc16_ccitt(b"\x05\x10")
        self.assertEqual(crc, 0x9E81)

    def test_parser_complete_packet(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x11, b"TEST")
        parser.feed(pkt)
        res = parser.next_packet()
        self.assertEqual(res, pkt)
        self.assertIsNone(parser.next_packet())

    def test_parser_fragmented_packet(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x12, b"ABCDEF")
        # Feed 1 byte at a time
        for i in range(len(pkt) - 1):
            parser.feed(pkt[i:i+1])
            self.assertIsNone(parser.next_packet())
        # Feed last byte
        parser.feed(pkt[-1:])
        res = parser.next_packet()
        self.assertEqual(res, pkt)

    def test_parser_back_to_back(self):
        parser = KlipperPacketParser()
        pkt1 = self.make_packet(0x10, b"MSG1")
        pkt2 = self.make_packet(0x11, b"MSG2")
        parser.feed(pkt1 + pkt2)
        res1 = parser.next_packet()
        res2 = parser.next_packet()
        self.assertEqual(res1, pkt1)
        self.assertEqual(res2, pkt2)
        self.assertIsNone(parser.next_packet())

    def test_parser_noise_recovery(self):
        parser = KlipperPacketParser()
        pkt = self.make_packet(0x10, b"VALID")
        # Corrupted bytes before valid packet
        noise = b"\x00\xFF\xAA\x7E\x12"
        parser.feed(noise + pkt)
        res = parser.next_packet()
        self.assertEqual(res, pkt)

    def test_downstream_and_upstream_rewrite(self):
        for node_name, target_dest in [('head0', 0x20), ('head1', 0x30), ('bed', 0x40)]:
            # Klippy sends packet with DEST=0x10 and SEQ_NUM=5
            seq_in = DEFAULT_DEST | 0x05
            original_pkt = self.make_packet(seq_in, b"HEATER_SET")

            # Downstream rewrite for peripheral node
            downstream_pkt = rewrite_packet_dest(original_pkt, target_dest)
            self.assertEqual(downstream_pkt[0], original_pkt[0])  # Length preserved
            self.assertEqual(downstream_pkt[1], target_dest | 0x05) # Dest updated
            self.assertEqual(downstream_pkt[-1], MESSAGE_SYNC)

            # Verify downstream packet CRC validity
            downstream_crc = (downstream_pkt[-3] << 8) | downstream_pkt[-2]
            computed_downstream_crc = crc16_ccitt(downstream_pkt[:-3])
            self.assertEqual(downstream_crc, computed_downstream_crc)

            # Upstream rewrite: slave responds with target_dest
            upstream_pkt = rewrite_packet_dest(downstream_pkt, DEFAULT_DEST)
            self.assertEqual(upstream_pkt, original_pkt)


    def test_parser_boundary_sizes(self):
        parser = KlipperPacketParser()
        # Min packet: 5 bytes (ack/nak)
        min_pkt = self.make_packet(0x10, b"")
        self.assertEqual(len(min_pkt), 5)
        # Max packet: 64 bytes
        max_payload = b"X" * (64 - 5)
        max_pkt = self.make_packet(0x10, max_payload)
        self.assertEqual(len(max_pkt), 64)

        parser.feed(min_pkt + max_pkt)
        self.assertEqual(parser.next_packet(), min_pkt)
        self.assertEqual(parser.next_packet(), max_pkt)
        self.assertIsNone(parser.next_packet())

    def test_parser_embedded_sync_in_payload(self):
        parser = KlipperPacketParser()
        # Payload containing byte 0x7E
        pkt = self.make_packet(0x10, b"ABC\x7EDEF")
        parser.feed(pkt)
        res = parser.next_packet()
        self.assertEqual(res, pkt)

    def test_parser_corrupt_crc(self):
        parser = KlipperPacketParser()
        good_pkt = self.make_packet(0x10, b"GOOD")
        bad_pkt = bytearray(self.make_packet(0x10, b"BAD_DATA"))
        bad_pkt[-2] ^= 0xFF  # Corrupt CRC

        parser.feed(bytes(bad_pkt) + good_pkt)
        res = parser.next_packet()
        self.assertEqual(res, good_pkt)

    def test_crc_with_embedded_sync_bytes(self):
        # Test packets where CRC16 bytes contain 0x7E
        parser = KlipperPacketParser()
        found = False
        # Search for a payload that produces 0x7E in CRC
        for i in range(1000):
            payload = f"CRC_TEST_{i}".encode()
            pkt = self.make_packet(0x10, payload)
            crc_high = pkt[-3]
            crc_low = pkt[-2]
            if crc_high == MESSAGE_SYNC or crc_low == MESSAGE_SYNC:
                parser.feed(pkt)
                res = parser.next_packet()
                self.assertEqual(res, pkt)
                found = True
                break
        self.assertTrue(found, "Should find at least one packet with 0x7E in CRC within 1000 iterations")

    def test_bus_arbiter_queuing_and_release(self):
        from host_multiplexer import MaestroMultiplexer, VirtualChannel
        channel_configs = {
            'main': (0x10, '/tmp/test_mcu_main'),
            'head0': (0x20, '/tmp/test_mcu_head0'),
            'head1': (0x30, '/tmp/test_mcu_head1'),
            'bed': (0x40, '/tmp/test_mcu_bed'),
        }
        mux = MaestroMultiplexer(serial_port="/dev/null", baud_rate=250000, channel_configs=channel_configs)

        pkt_main = self.make_packet(0x10, b"MAIN_CMD")
        pkt_head0 = self.make_packet(0x10, b"HEAD0_CMD")
        pkt_head1 = self.make_packet(0x10, b"HEAD1_CMD")

        # Create dummy channel objects
        chan_main = VirtualChannel('main', 0x10, '/tmp/test_mcu_main')
        chan_head0 = VirtualChannel('head0', 0x20, '/tmp/test_mcu_head0')
        chan_head1 = VirtualChannel('head1', 0x30, '/tmp/test_mcu_head1')

        # 1. Send packet for Head 0 (0x20): should acquire bus
        out_head0 = rewrite_packet_dest(pkt_head0, 0x20)
        mux.bus_busy = True
        mux.bus_active_dest = 0x20
        mux.bus_tx_time = 1000.0
        mux.uart_tx_queue.extend(out_head0)
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x20)
        self.assertIn(out_head0, bytes(mux.uart_tx_queue))

        # 2. Send packet for Head 1 (0x30) while bus is busy: should queue in FIFO
        out_head1 = rewrite_packet_dest(pkt_head1, 0x30)
        mux.bus_pending_queue.append((0x30, out_head1))
        self.assertEqual(len(mux.bus_pending_queue), 1)

        # 3. Send packet for Main MCU (0x10): should BYPASS the bus arbiter and send immediately
        mux.uart_tx_queue.extend(pkt_main)
        self.assertIn(pkt_main, bytes(mux.uart_tx_queue))

        # 4. Simulate response from Head 0 (0x20): should free bus and dispatch Head 1
        # Clear TX queue to verify dispatch
        mux.uart_tx_queue.clear()
        mux.bus_busy = False
        mux.bus_active_dest = 0
        mux._dispatch_next_bus_packet()

        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x30)
        self.assertEqual(len(mux.bus_pending_queue), 0)
        self.assertEqual(bytes(mux.uart_tx_queue), out_head1)

    def test_bus_arbiter_timeout(self):
        from host_multiplexer import MaestroMultiplexer
        mux = MaestroMultiplexer(serial_port="/dev/null", baud_rate=250000, channel_configs={})
        pkt_bed = self.make_packet(0x40, b"BED_CMD")

        # Simulate busy bus waiting for Head 0
        mux.bus_busy = True
        mux.bus_active_dest = 0x20
        mux.bus_tx_time = 100.0 # Old timestamp
        mux.bus_pending_queue.append((0x40, pkt_bed))

        # Simulate timeout check at time 101.0 (difference 1.0s > 0.020s timeout)
        now = 101.0
        if mux.bus_busy and (now - mux.bus_tx_time > mux.bus_timeout):
            mux.bus_busy = False
            mux.bus_active_dest = 0
            mux._dispatch_next_bus_packet()

        # Bus should have released Head 0 and dispatched Bed
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x40)
        self.assertEqual(bytes(mux.uart_tx_queue), pkt_bed)

    def test_multi_packet_response_keeps_bus_busy_until_ack(self):
        from host_multiplexer import MaestroMultiplexer, VirtualChannel, MESSAGE_MIN
        channel_configs = {
            'main': (0x10, '/tmp/test_mcu_main'),
            'head0': (0x20, '/tmp/test_mcu_head0'),
            'head1': (0x30, '/tmp/test_mcu_head1'),
        }
        mux = MaestroMultiplexer(serial_port="/dev/null", baud_rate=250000, channel_configs=channel_configs)

        # Queue a pending command for Head 1 (0x30)
        pkt_head1_cmd = self.make_packet(0x30, b"HEAD1_CMD")
        mux.bus_pending_queue.append((0x30, pkt_head1_cmd))

        # Bus is busy waiting for Head 0 (0x20)
        mux.bus_busy = True
        mux.bus_active_dest = 0x20
        mux.bus_tx_time = 100.0

        # Simulate Head 0 responding with Packet 1: Temperature Report (len > 5)
        pkt_temp_report = self.make_packet(0x20, b"TEMPERATURE_NTC_100K")
        self.assertGreater(len(pkt_temp_report), MESSAGE_MIN)

        # Process packet 1 via arbiter logic:
        dest = pkt_temp_report[1] & 0xF0
        self.assertEqual(dest, mux.bus_active_dest)
        if mux.bus_busy and dest == mux.bus_active_dest:
            if len(pkt_temp_report) == MESSAGE_MIN:
                mux.bus_busy = False
                mux.bus_active_dest = 0
                mux._dispatch_next_bus_packet()
            else:
                mux.bus_tx_time = 100.5 # Updated timer

        # BUS MUST REMAIN BUSY! Head 1 command must NOT be dispatched yet!
        self.assertTrue(mux.bus_busy, "Bus must stay busy during multi-packet response")
        self.assertEqual(mux.bus_active_dest, 0x20)
        self.assertEqual(len(mux.bus_pending_queue), 1)
        self.assertEqual(len(mux.uart_tx_queue), 0)

        # Simulate Head 0 sending terminating Packet 2: ACK (len == 5)
        pkt_ack = self.make_packet(0x20, b"")
        self.assertEqual(len(pkt_ack), MESSAGE_MIN)

        if mux.bus_busy and dest == mux.bus_active_dest:
            if len(pkt_ack) == MESSAGE_MIN:
                mux.bus_busy = False
                mux.bus_active_dest = 0
                mux._dispatch_next_bus_packet()

        # NOW bus released Head 0 and dispatched Head 1 command!
        self.assertTrue(mux.bus_busy)
        self.assertEqual(mux.bus_active_dest, 0x30)
        self.assertEqual(len(mux.bus_pending_queue), 0)
        self.assertEqual(bytes(mux.uart_tx_queue), pkt_head1_cmd)


if __name__ == "__main__":
    unittest.main()
