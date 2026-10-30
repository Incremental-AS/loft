"""A small, spec-driven Loft API client for the conformance suite.

* Pinned TLS: the direct listener is trusted only if the SHA-256 of its key's
  SubjectPublicKeyInfo matches a pin. The check runs right after the TLS handshake,
  before a single request byte is written, on every new connection. This is the
  behaviour every Loft client must have (docs/pairing.md).
* Every call names an OpenAPI operation id; the path, method and the shape of the
  answer come from api/openapi.yaml. JSON answers are validated against the schema
  of that operation and status, and an error status or code the operation doesn't
  document fails the test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpcore
import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(os.environ.get("LOFT_OPENAPI")
                 or Path(__file__).resolve().parents[1] / "api" / "openapi.yaml")


class PinMismatch(ssl.SSLError):
    pass


def spki_pin(der_cert: bytes) -> str:
    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


class _PinnedStream(httpcore.NetworkStream):
    def __init__(self, inner, pins: list[str]):
        self._s, self._pins = inner, pins

    def read(self, max_bytes, timeout=None):
        return self._s.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self._s.write(buffer, timeout)

    def close(self):
        self._s.close()

    def get_extra_info(self, info):
        return self._s.get_extra_info(info)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        tls = self._s.start_tls(ssl_context, server_hostname, timeout)
        der = tls.get_extra_info("ssl_object").getpeercert(True)
        got = spki_pin(der)
        if got not in self._pins:
            tls.close()
            raise PinMismatch(f"server key {got} matches none of the pins")
        return tls


class _PinningBackend(httpcore.SyncBackend):
    def __init__(self, pins: list[str]):
        self.pins = pins

    def connect_tcp(self, *a, **k):
        return _PinnedStream(super().connect_tcp(*a, **k), self.pins)


def pinned_transport(pins: list[str]) -> httpx.HTTPTransport:
    """TLS without CA or host-name checks; the pin is the trust (self-signed key)."""
    if not pins:
        raise ValueError("no pins: refusing to talk TLS to an unpinned server")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t = httpx.HTTPTransport(verify=ctx)
    t._pool = httpcore.ConnectionPool(ssl_context=ctx, network_backend=_PinningBackend(pins))
    return t


class Spec:
    def __init__(self, path: Path = SPEC_PATH):
        self.doc = yaml.safe_load(path.read_text())
        self.registry = Registry().with_resource(
            "urn:loft-openapi", Resource.from_contents(self.doc, default_specification=DRAFT202012))
        self.ops: dict[str, tuple[str, str, dict]] = {}
        for p, ops in self.doc["paths"].items():
            for m, op in ops.items():
                self.ops[op["operationId"]] = (m.upper(), p, op)

    def validator(self, op_id: str, status: int) -> Draft202012Validator | None:
        method, path, op = self.ops[op_id]
        resp = op["responses"].get(str(status))
        if resp is None or "application/json" not in resp.get("content", {}):
            return None
        ptr = "/".join(x.replace("~", "~0").replace("/", "~1") for x in
                       ("paths", path, method.lower(), "responses", str(status), "content",
                        "application/json", "schema"))
        return Draft202012Validator({"$ref": f"urn:loft-openapi#/{ptr}"}, registry=self.registry)

    def codes(self, op_id: str, status: int) -> set[str]:
        resp = self.ops[op_id][2]["responses"].get(str(status), {})
        return set(re.findall(r"`([a-z_]+)`", resp.get("description", "")))


class SpecViolation(AssertionError):
    pass


class Loft:
    """One base URL (one listener), optionally pinned, with an optional device token."""

    def __init__(self, base: str, spec: Spec, pins: list[str] | None = None,
                 token: str | None = None, timeout: float = 60):
        self.base, self.spec, self.token = base.rstrip("/"), spec, token
        transport = pinned_transport(pins) if urlsplit(base).scheme == "https" and pins else None
        self.http = httpx.Client(base_url=self.base, transport=transport, timeout=timeout)

    def with_token(self, token: str | None) -> "Loft":
        c = Loft.__new__(Loft)
        c.base, c.spec, c.http, c.token = self.base, self.spec, self.http, token
        return c

    def call(self, op_id: str, *, path: dict | None = None, params: dict | None = None,
             json_body=None, content: bytes | None = None, headers: dict | None = None,
             token: str | None | bool = True, check: bool = True) -> httpx.Response:
        method, tmpl, op = self.spec.ops[op_id]
        url = tmpl.format(**(path or {}))
        h = dict(headers or {})
        tok = self.token if token is True else (token or None)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        r = self.http.request(method, url, params=params, json=json_body, content=content,
                              headers=h)
        if check:
            self.check(op_id, r)
        return r

    def check(self, op_id: str, r: httpx.Response):
        _, tmpl, op = self.spec.ops[op_id]
        st = r.status_code
        if str(st) not in op["responses"]:
            raise SpecViolation(f"{op_id}: status {st} not documented ({r.text[:200]})")
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            body = r.json()
            v = self.spec.validator(op_id, st)
            if v is not None:
                errs = sorted(v.iter_errors(body), key=str)
                if errs:
                    raise SpecViolation(f"{op_id} {st}: {errs[0].message} in "
                                        f"{json.dumps(body)[:300]}")
            if st >= 400 and isinstance(body, dict) and "error" in body:
                codes = self.spec.codes(op_id, st)
                if body["error"] not in codes:
                    raise SpecViolation(f"{op_id}: error code {body['error']!r} not documented "
                                        f"for status {st} (documented: {sorted(codes)})")

    def close(self):
        self.http.close()


def decode_pairing(text: str) -> dict:
    """The payload of `loft-gateway pair` (a link or the bare base64url payload)."""
    text = text.strip().split("#", 1)[-1]
    if text.startswith("loft://pair?"):
        text = text.split("?", 1)[1]
    p = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    if p.get("v") != 1 or not p.get("pins") or not str(p.get("lan", "")).startswith("https://"):
        raise ValueError("not a valid v1 pairing payload")
    return p
