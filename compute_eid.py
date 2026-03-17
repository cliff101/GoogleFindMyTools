#!/usr/bin/env python3
"""
Standalone EID computation helper.

Computes the current Ephemeral Identifier (EID) for a DIY FHN tracker
given its Ephemeral Identity Key (EIK) and pair date.

Usage:
    python3 compute_eid.py <eik_hex_64chars> <pair_date_unix>

Output:
    40-character hex EID for the current 1024-second window.

This script is intentionally self-contained (no project imports)
so it can be called from fmd_tracker.sh without PYTHONPATH setup.
Requires: pycryptodome, ecdsa  (already in requirements.txt)
"""

import sys
import time

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


def compute_current_eid(eik_hex: str, pair_date: int) -> str:
    eik = bytes.fromhex(eik_hex)
    current_time = int(time.time())
    offset = current_time - pair_date
    aligned_offset = (offset // ROTATION_PERIOD) * ROTATION_PERIOD
    eid = generate_eid(eik, aligned_offset)
    return eid.hex()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: compute_eid.py <eik_hex_64chars> <pair_date_unix>", file=sys.stderr)
        sys.exit(1)

    eik_hex = sys.argv[1]
    pair_date = int(sys.argv[2])

    if len(eik_hex) != 64:
        print("Error: EIK must be 64 hex characters (32 bytes)", file=sys.stderr)
        sys.exit(1)

    print(compute_current_eid(eik_hex, pair_date))
