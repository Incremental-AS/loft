"""Dev server: both listeners, real TLS on direct, LocalBackend instead of SMB.

    python scripts/dev_server.py <nas-dir> <work-dir> [direct_port public_port]

Users: loft-dev / dev-password, share "home" = <nas-dir>/home. For client
development and tests without a NAS (apple/scripts/core-it.sh, the conformance suite).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loft_gateway.config import Listener, Settings  # noqa: E402
from loft_gateway.db import DB  # noqa: E402
from loft_gateway.initcmd import make_tls_key  # noqa: E402
from loft_gateway.main import serve  # noqa: E402
from loft_gateway.service import Gateway  # noqa: E402
from loft_gateway.storage.local import LocalBackend  # noqa: E402

nas, work = Path(sys.argv[1]), Path(sys.argv[2])
dport, pport = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (18443, 18080)
(nas / "home").mkdir(parents=True, exist_ok=True)
work.mkdir(parents=True, exist_ok=True)
if not (work / "direct.crt").exists():
    # Same key + self-signed cert as `loft-gateway init` makes; the pin goes to direct.pin.
    (work / "direct.pin").write_text(make_tls_key(work / "direct.key", work / "direct.crt",
                                                  ["127.0.0.1", "localhost"]) + "\n")
pin = (work / "direct.pin").read_text().strip()
s = Settings(thumb_socket="", db_path=str(work / "index.db"), shares=["home"], direct_spki_pins=[pin],
             direct=Listener("127.0.0.1", dport, str(work / "direct.crt"), str(work / "direct.key")),
             public=Listener("127.0.0.1", pport))
gw = Gateway(s, DB(s.db_path), LocalBackend(nas, {"loft-dev": {"password": "dev-password",
                                                          "shares": ["home"]}}), os.urandom(32))
asyncio.run(serve(gw))
