"""Print a pairing link (docs/pairing.md) made with the gateway's own encoder, for tests
against gateway/scripts/dev_server.py (core-it.sh, the iPhone UI tests).

    PYTHONPATH=gateway python apple/scripts/pair-link.py <lan-url> <pin>[,<pin>…] [name]
"""
import sys

from loft_gateway import admin

lan, pins = sys.argv[1], [p for p in sys.argv[2].split(",") if p]
name = sys.argv[3] if len(sys.argv) > 3 else "Dev gateway"
print(admin.PAIR_BASE + admin.encode_payload({"v": 1, "name": name, "lan": lan, "pins": pins}))
