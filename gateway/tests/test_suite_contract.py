"""Contracts other products rely on (the Incremental suite's suite.git).

Set SUITE_CONTRACTS to a checkout of the suite contracts repo; without it these
tests skip. They fail when a gateway change breaks:
- what an assistant client (Jeevo) sends to and reads from /mcp
  (contracts/mcp/<client>-expects-loft.json), checked on the real wire;
- the published REST API: api/openapi.yaml must stay backwards compatible with
  the version recorded in contracts/loft/openapi.yaml.
A deliberate break is made there first (new major + decision record), then here.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from loft_gateway import mcp

from test_mcp import A, call, connect, ok, rpc

SUITE = os.environ.get("SUITE_CONTRACTS", "")
REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (SUITE and Path(SUITE, "tools", "check_contract.py").is_file()),
    reason="SUITE_CONTRACTS is not set to a suite contracts checkout")


def _checker():
    spec = importlib.util.spec_from_file_location("check_contract", Path(SUITE, "tools", "check_contract.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _expectations():
    return sorted(Path(SUITE, "contracts", "mcp").glob("*-expects-loft.json"))


def test_there_is_something_to_check():
    assert _expectations(), "no *-expects-loft.json in the suite contracts"


@pytest.mark.parametrize("path", _expectations() if SUITE else [], ids=lambda p: p.name)
def test_tools_and_server_name_on_the_wire(env, path):
    cc, exp = _checker(), json.loads(path.read_text())
    h, _ = connect(env)
    init = rpc(env.public, h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": exp["consumer"]}})["result"]
    tools = rpc(env.public, h, "tools/list")["result"]["tools"]
    assert cc.mcp_provider_errors(exp, tools, init["serverInfo"]["name"]) == []
    assert mcp.CONTEXT_KEY in exp.get("meta_keys", [mcp.CONTEXT_KEY])


@pytest.mark.parametrize("path", _expectations() if SUITE else [], ids=lambda p: p.name)
def test_results_carry_what_the_client_reads(env, path):
    cc, exp = _checker(), json.loads(path.read_text())
    h, _ = connect(env)
    errs = []

    def tool(name, **args):
        res = ok(call(env.public, h, name, ctx="conv-1", **args))
        if name in exp.get("tools", {}):
            errs.extend(cc.structured_errors(exp, "tool", name, res.get("structuredContent") or {}))
        return res["structuredContent"]

    def method(name, params):
        res = rpc(env.public, h, name, params)
        assert "result" in res, res
        if name in exp.get("methods", {}):
            errs.extend(cc.structured_errors(exp, "method", name, res["result"]))
        return res["result"]

    small = b"contract check\n"
    tool("write_file", path=f"{A}/Contract/small.txt", content_base64=base64.b64encode(small).decode(),
         if_exists="rename", create_folders=True)
    data = hashlib.sha256(b"c").digest() * 2000
    st = tool("upload_start", path=f"{A}/Contract/big.bin", size=len(data),
              sha256=hashlib.sha256(data).hexdigest(), create_folders=True)
    step = min(int(st["chunk_max"]), 30_000)
    for off in range(0, len(data), step):
        tool("upload_chunk", upload_id=st["upload_id"], offset=off,
             content_base64=base64.b64encode(data[off:off + step]).decode())
    tool("upload_finish", upload_id=st["upload_id"], path=f"{A}/Contract/big.bin", if_exists="replace")
    tool("download_file", path=f"{A}/Contract/big.bin", offset=0, length=2 * 1024 * 1024)
    method("loft/folders", {"path": ""})
    method("loft/access/get", {"context": "conv-1"})
    method("loft/access/set", {"context": "conv-1", "working_folder": A})
    assert errs == []


def test_rest_api_stays_backwards_compatible():
    yaml = pytest.importorskip("yaml")
    contract = Path(SUITE, "contracts", "loft", "openapi.yaml")
    if not contract.is_file():
        pytest.skip("no contracts/loft/openapi.yaml in the suite contracts")
    cc = _checker()
    old = yaml.safe_load(contract.read_text())
    new = yaml.safe_load((REPO / "api" / "openapi.yaml").read_text())
    assert cc.openapi_errors(old, new) == []
