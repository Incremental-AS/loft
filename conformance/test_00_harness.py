"""The suite checks itself first: the spec checks really reject bad answers."""
import httpx
import pytest

from loftclient import Spec, SpecViolation, decode_pairing


def _loft(spec):
    from loftclient import Loft
    c = Loft.__new__(Loft)
    c.spec = spec
    return c


def _resp(status, body):
    return httpx.Response(status, json=body)


def test_spec_rejects_wrong_shapes_and_codes():
    spec = Spec()
    c = _loft(spec)
    good = {"ok": True, "service": "loft", "route": "direct", "version": "0.2.0"}
    c.check("healthz", _resp(200, good))
    with pytest.raises(SpecViolation):
        c.check("healthz", _resp(200, {**good, "route": "sideways"}))
    with pytest.raises(SpecViolation):                      # status not documented
        c.check("healthz", _resp(500, {"error": "boom"}))
    c.check("get_item", _resp(404, {"error": "not_found"}))
    with pytest.raises(SpecViolation):                      # code not documented for 404
        c.check("get_item", _resp(404, {"error": "no_such_upload"}))
    with pytest.raises(SpecViolation):                      # item missing a field
        c.check("get_item", _resp(200, {"id": "x", "name": "n"}))


def test_pairing_decode():
    import base64
    import json
    p = {"v": 1, "name": "Home", "lan": "https://10.0.0.5:8443", "pins": ["a="]}
    raw = base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
    assert decode_pairing("https://loft.incremental.no/pair#" + raw) == p
    assert decode_pairing(raw) == p
    with pytest.raises(ValueError):
        decode_pairing(base64.urlsafe_b64encode(json.dumps({**p, "pins": []}).encode()).decode())
