#!/usr/bin/env python3
"""
Standalone EID computation helper.

Computes the Ephemeral Identifier (EID) for a DIY FHN tracker given its
Ephemeral Identity Key (EIK), pair date, and an explicit current timestamp.

Usage:
    python3 compute_eid.py <eik_hex_64chars> <pair_date_unix> [<current_time_unix>]

    If <current_time_unix> is omitted, the value is read from the
    .last_known_time file in the same directory as this script.

Output (two lines):
    Line 1 — 40-character hex EID for the current 1024-second window.
    Line 2 — Unix timestamp used for the computation (for clock persistence).

This script is intentionally self-contained (no project imports)
so it can be called from fmd_tracker.sh without PYTHONPATH setup.
Requires: pycryptodome, ecdsa  (already in requirements.txt)
"""

import sys
import os

from Cryptodome.Cipher import AES
from ecdsa import SECP160r1

K = 10
ROTATION_PERIOD = 1024  # 2^K seconds


def generate_eid(identity_key: bytes, time_offset: int) -> bytes:
    """Compute EID from EIK and time offset (seconds since pair_date, K low bits cleared)."""
    mask = ~((1 << K) - 1)
    time_offset &= mask
    ts_bytes = time_offset.to_bytes(4, byteorder='big')

    data = bytearray(32)
    data[0:11] = b'\xFF' * 11
    data[11] = K
    data[12:16] = ts_bytes
    data[16:27] = b'\x00' * 11
    data[27] = K
    data[28:32] = ts_bytes

    cipher = AES.new(identity_key, AES.MODE_ECB)
    r_dash = cipher.encrypt(bytes(data))

    r_dash_int = int.from_bytes(r_dash, byteorder='big', signed=False)
    curve = SECP160r1
    r = r_dash_int % curve.order
    R = r * curve.generator
    return R.x().to_bytes(20, 'big')


def compute_current_eid(eik_hex: str, pair_date: int, current_time: int) -> tuple[str, int]:
    """Return (eid_hex, current_time) for the given explicit timestamp.

    Never reads the system clock; the caller is responsible for supplying
    a monotonically advancing current_time (e.g. loaded from .last_known_time).
    """
    eik = bytes.fromhex(eik_hex)
    offset = current_time - pair_date
    if offset < 0:
        print(f"Warning: current_time is behind pair_date by {-offset}s "
              "(stale save file?). Using offset=0.",
              file=sys.stderr)
        offset = 0
    aligned_offset = (offset // ROTATION_PERIOD) * ROTATION_PERIOD
    eid = generate_eid(eik, aligned_offset)
    return eid.hex(), current_time


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        print("Usage: compute_eid.py <eik_hex_64chars> <pair_date_unix> [<current_time_unix>]",
              file=sys.stderr)
        sys.exit(1)

    eik_hex = sys.argv[1]
    pair_date = int(sys.argv[2])

    if len(eik_hex) != 64:
        print("Error: EIK must be 64 hex characters (32 bytes)", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) == 4:
        current_time = int(sys.argv[3])
    else:
        clock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.last_known_time')
        try:
            with open(clock_file) as _f:
                current_time = int(_f.read().strip())
        except Exception:
            print(f"Error: no current_time given and {clock_file} not found or unreadable.",
                  file=sys.stderr)
            sys.exit(1)

    eid_hex, t = compute_current_eid(eik_hex, pair_date, current_time)
    print(eid_hex)
    print(t)
