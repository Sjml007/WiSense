#!/usr/bin/env python3
"""Raw serial diagnostic reader for rx_csi_collector (WiSense-Twin Section 14).

idf_monitor is NOT suitable for viewing this firmware's output once it starts
streaming binary CSI frames -- it tries to decode every byte as an ESP-IDF log
message and crashes on raw binary data. This script talks to the serial port
directly and understands the actual wire protocol documented in
docs/hardware/receiver-firmware.md:

    [4-byte magic 0xAA,0x55,0x57,0x53] [24 more header bytes = 28 total]
    [payload_len bytes of raw CSI data]
    [2-byte CRC-16-CCITT][2-byte trailer 0x55,0xAA]

Any bytes NOT part of a valid frame (ESP-IDF boot banner, [RX_STAT] lines,
disconnected logging) are plain 7-bit ASCII text and get printed as-is -- the
0xAA magic byte can never appear inside real ASCII text, so resyncing on it
is always safe.

Usage:
    pip install pyserial
    python raw_csi_monitor.py --port COM4 --baud 921600 --duration 60
"""

from __future__ import annotations

import argparse
import binascii
import struct
import sys
import time

MAGIC = bytes([0xAA, 0x55, 0x57, 0x53])
TRAILER = bytes([0x55, 0xAA])
HEADER_LEN = 28
TRAILER_LEN = 4

# Header layout after the 4-byte magic (24 bytes, little-endian):
#   header_len:u16, payload_len:u16, seq_num:u32, rx_timestamp_us:u32,
#   sender_ts_ms:u32, rssi:i8, channel:u8, noise_floor:i8,
#   first_word_invalid:u8, sig_mode:i8, mcs:i8, reserved:2x u8
HEADER_STRUCT = struct.Struct("<HHIIIbBbBbb2s")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="Serial port, e.g. COM4")
    p.add_argument("--baud", type=int, default=921600, help="Baud rate (default 921600)")
    p.add_argument("--duration", type=float, default=60.0, help="Seconds to run before printing summary (default 60)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import serial  # type: ignore
    except ImportError:
        sys.stderr.write("pyserial not installed. Run: pip install pyserial\n")
        return 1

    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    sys.stdout.write(f"Connected to {args.port} @ {args.baud} baud. Reading for {args.duration:.0f}s... (Ctrl+C to stop early)\n\n")

    buf = bytearray()
    valid_frames = 0
    corrupt_frames = 0
    seq_numbers: list[int] = []
    rssi_values: list[int] = []
    first_ts: float | None = None
    last_ts: float | None = None
    text_line_buf = bytearray()

    def flush_text() -> None:
        if text_line_buf:
            try:
                line = text_line_buf.decode("ascii", errors="replace")
            except Exception:
                line = repr(bytes(text_line_buf))
            sys.stdout.write(line)
            text_line_buf.clear()

    start = time.monotonic()
    try:
        while time.monotonic() - start < args.duration:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)

            while True:
                idx = buf.find(MAGIC)
                if idx == -1:
                    # No magic yet -- everything so far is plain text/log output.
                    # Keep the last 3 bytes in case a magic sequence is split
                    # across this read and the next one.
                    if len(buf) > 3:
                        text_line_buf.extend(buf[:-3])
                        del buf[:-3]
                    flush_text()
                    break

                # Bytes before the magic are plain text (log lines, [RX_STAT], etc.)
                if idx > 0:
                    text_line_buf.extend(buf[:idx])
                    del buf[:idx]
                flush_text()

                if len(buf) < HEADER_LEN:
                    break  # wait for the rest of the header

                header_body = buf[4:HEADER_LEN]
                (header_len, payload_len, seq_num, rx_ts_us, sender_ts_ms,
                 rssi, channel, noise_floor, first_word_invalid,
                 sig_mode, mcs, _reserved) = HEADER_STRUCT.unpack(header_body)

                if header_len != HEADER_LEN or payload_len > 384:
                    # Not a real frame (magic bytes appeared coincidentally in
                    # a binary payload) -- drop just the magic and resync.
                    del buf[:4]
                    corrupt_frames += 1
                    continue

                total_len = HEADER_LEN + payload_len + TRAILER_LEN
                if len(buf) < total_len:
                    break  # wait for the rest of the frame

                frame = bytes(buf[:total_len])
                header_and_payload = frame[:HEADER_LEN + payload_len]
                trailer_bytes = frame[HEADER_LEN + payload_len:total_len]
                crc_received = struct.unpack("<H", trailer_bytes[0:2])[0]
                end_marker = trailer_bytes[2:4]

                crc_computed = binascii.crc_hqx(header_and_payload, 0xFFFF)

                now = time.monotonic()
                if end_marker == TRAILER and crc_computed == crc_received:
                    valid_frames += 1
                    seq_numbers.append(seq_num)
                    rssi_values.append(rssi)
                    if first_ts is None:
                        first_ts = now
                    last_ts = now
                else:
                    corrupt_frames += 1

                del buf[:total_len]
    except KeyboardInterrupt:
        sys.stdout.write("\nStopped early by user.\n")
    finally:
        ser.close()

    flush_text()

    sys.stdout.write("\n" + "=" * 60 + "\n")
    sys.stdout.write("Raw CSI capture summary\n")
    sys.stdout.write("=" * 60 + "\n")
    sys.stdout.write(f"Valid frames:      {valid_frames}\n")
    sys.stdout.write(f"Corrupt frames:    {corrupt_frames}\n")

    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        elapsed = last_ts - first_ts
        rate = valid_frames / elapsed if elapsed > 0 else 0.0
        sys.stdout.write(f"Elapsed (valid window): {elapsed:.2f} s\n")
        sys.stdout.write(f"Achieved rate:     {rate:.1f} frames/sec\n")

    if seq_numbers:
        gaps = [b - a for a, b in zip(seq_numbers, seq_numbers[1:]) if b - a != 1]
        missed = sum(g - 1 for g in gaps)
        sys.stdout.write(f"Sequence gaps:     {len(gaps)} (estimated {missed} dropped frame(s))\n")
        sys.stdout.write(f"RSSI range:        min={min(rssi_values)} max={max(rssi_values)} dBm\n")

    sys.stdout.write("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
