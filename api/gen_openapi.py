#!/usr/bin/env python3
"""Write api/openapi.yaml from the gateway's FastAPI app.

    python api/gen_openapi.py           # rewrite api/openapi.yaml
    python api/gen_openapi.py --check   # exit 1 if the checked-in file is out of date

The routes, parameters, bodies, response shapes and error codes come from
gateway/loft_gateway/api.py (+ schemas.py). This script only adds what FastAPI can't
know: the overview text, the bearer security scheme (the handlers read the
Authorization header themselves), tag descriptions, and the FastAPI validation-error
shape next to our own error body. gateway/tests/test_openapi.py fails when the file
drifts from the app.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "openapi.yaml"
sys.path.insert(0, str(HERE.parent / "gateway"))

from loft_gateway.api import ERROR_STATUS, create_app  # noqa: E402

OVERVIEW = """\
The REST API between the Loft apps and a Loft gateway. The gateway sits in front of an SMB
server and signs in to it as each user, so the server's own permissions apply.

## Two listeners, one API

A gateway answers on two listeners with the same paths:

* **direct**: TLS with the gateway's own key. Clients pin it: they connect only if the
  SHA-256 of the server key's SubjectPublicKeyInfo matches a pin from the pairing code
  (`docs/pairing.md`). Password sign-in (`POST /v1/auth/login`) works only here. Used on the
  home network, over a VPN, and from outside in `direct` public mode (a port forward).
* **public**: plain HTTP on loopback, for a TLS-ending proxy such as Cloudflare Tunnel
  (`tunnel` public mode). Device tokens only; sign-in answers 403
  `password_login_direct_only`. A request on the direct listener carrying the proxy's
  `CF-Connecting-IP` header is refused (`edge_traffic_on_direct_listener`).

`GET /healthz` says which listener answered.

## Authentication

Sign in once on the direct listener with the SMB account; the answer holds a device token
(`nst_<device_id>.<secret>`). Send it as `Authorization: Bearer <token>`. Refresh it with
`POST /v1/auth/refresh` (sliding expiry); the previous token stays valid for a short grace
period. The gateway keeps the SMB password only wrapped under a key derived from the token,
so a revoked device (`DELETE /v1/auth/device/{id}`) leaves nothing that decrypts.

## Sync model

After sign-in a device picks a root (`PUT /v1/me/root`): a folder in a share, or `*` for
every share. Items have stable ids. `content_etag` changes with the content, `meta_etag`
with name or place. Clients enumerate with `/children`, then follow
`GET /v1/changes?anchor=` (long-poll with `wait`). 410 `anchor_expired` means: enumerate
again. Uploads are resumable sessions (create, PUT chunks at `received`, commit). A commit
whose `if_match` is stale never overwrites: it lands as a conflict copy
(`result: conflict`).

## Errors

Every error from the gateway is JSON `{"error": "<code>", ...}` with the HTTP status of
its code. Clients branch on `error`. Request bodies or query values the server can't parse
get FastAPI's 422 `{"detail": [...]}` instead.

| Status | Codes |
|---|---|
""" + "".join(f"| {st} | {', '.join(f'`{c}`' for c in codes.split())} |\n"
              for st, codes in ERROR_STATUS.items())

TAGS = [
    {"name": "meta", "description": "Unauthenticated: route probe and key pins."},
    {"name": "auth", "description": "Sign-in, token refresh, devices."},
    {"name": "roots", "description": "The account, its shares, and the folder a device syncs."},
    {"name": "items", "description": "Files and folders inside the root."},
    {"name": "changes", "description": "The change feed."},
    {"name": "uploads", "description": "Resumable, chunked uploads."},
]

SECURITY = {"deviceToken": {
    "type": "http", "scheme": "bearer", "bearerFormat": "nst_<device_id>.<secret>",
    "description": "Device token from sign-in or refresh."}}


def build() -> dict:
    spec = copy.deepcopy(create_app(None, "direct").openapi())
    spec["info"] = {
        "title": "Loft gateway API",
        "version": spec["info"]["version"],
        "summary": "Files on your own SMB server, through a Loft gateway.",
        "description": OVERVIEW,
        "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
    }
    spec["tags"] = TAGS
    spec.setdefault("components", {})["securitySchemes"] = SECURITY
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            params = [p for p in op.get("parameters", [])
                      if not (p["in"] == "header" and p["name"].lower() == "authorization")]
            if params:
                op["parameters"] = params
            else:
                op.pop("parameters", None)
            if params or "requestBody" in op:
                # FastAPI's own 422 next to ours (chunk_hash_mismatch etc.).
                resp = op["responses"].setdefault(
                    "422", {"description": "Request failed validation",
                            "content": {"application/json": {"schema": {}}}})
                sch = resp["content"]["application/json"]["schema"]
                fastapi_422 = {"$ref": "#/components/schemas/HTTPValidationError"}
                if sch.get("$ref", "").endswith("/Error"):
                    resp["content"]["application/json"]["schema"] = {
                        "anyOf": [sch, fastapi_422]}
                    resp["description"] += "; or a request that failed validation (`detail`)"
            if op.get("description") is None:
                op.pop("description", None)
    return spec


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, s: str):
    # Multi-line text (the overview) as a readable block, not an escaped one-liner.
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


_Dumper.add_representer(str, _str)


def render() -> str:
    head = ("# Generated by api/gen_openapi.py from gateway/loft_gateway/api.py. Do not edit;\n"
            "# change the app and run `python api/gen_openapi.py`.\n")
    return head + yaml.dump(build(), Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                            width=100)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("api/openapi.yaml is out of date: run `python api/gen_openapi.py`",
                  file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
