"""HTTP API (api/openapi.yaml is generated from this). Two app instances share one Gateway:

* direct  — LAN/VPN listener, TLS with the pinned gateway key. Password login lives here only.
* public  — loopback listener behind cloudflared. Device tokens only; /v1/auth/login is 403.

The route is a property of the LISTENER the request arrived on, never of a
header, so no client can talk its way onto the direct route.
"""
from __future__ import annotations

import re

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__
from . import schemas as S
from .rootfs import Blocked, CrossShare, FixedItem
from .service import ApiError, Caller, Gateway
from .storage.base import AccessDenied, NotFound, StorageError


class LoginIn(BaseModel):
    user: str
    password: str
    device_name: str = "Mac"


class CodeIn(BaseModel):
    max_uses: int | None = None
    days: int | None = None


class CodeLoginIn(BaseModel):
    code: str
    device_name: str = "iPhone"


class RootIn(BaseModel):
    share: str
    path: str = ""


class UploadIn(BaseModel):
    size: int
    sha256: str
    near_id: str | None = None      # parent (create) or item (modify): picks the share


class CommitIn(BaseModel):
    parent_id: str | None = None
    name: str | None = None
    item_id: str | None = None      # modify existing content (modifyItem)
    if_match: str | None = None     # content etag the client based its edit on
    replaces: str | None = None     # safe-save: the item this create supersedes


class FolderIn(BaseModel):
    parent_id: str
    name: str


class PatchIn(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    if_match: str | None = None     # meta etag


# ── OpenAPI metadata (api/openapi.yaml is generated from this; see api/gen_openapi.py) ──

# HTTP status of every error code. A code always comes with the same status.
ERROR_STATUS = {
    400: "bad_anchor bad_path bad_upload cannot_delete_root cannot_move_root is_a_folder "
         "missing_credentials move_into_self near_id_required not_a_folder "
         "parent_and_name_required",
    401: "code_expired device_revoked invalid_code invalid_credentials invalid_token "
         "missing_token nas_credentials_invalid token_expired token_superseded",
    403: "access_denied account_not_allowed codes_direct_only edge_traffic_on_direct_listener "
         "password_login_direct_only share_blocked share_is_fixed",
    404: "no_such_code no_such_device no_such_share no_such_upload no_such_version not_found "
         "no_thumbnail root_missing",
    409: "cross_share_move name_exists name_exists_on_nas no_root_selected upload_incomplete "
         "wrong_offset",
    410: "anchor_expired",
    412: "content_etag_mismatch meta_etag_mismatch",
    413: "chunk_too_large",
    416: "bad_range range_not_satisfiable",
    422: "chunk_hash_mismatch file_hash_mismatch invalid_or_excluded_name",
    429: "too_many_attempts",
    502: "nas_error",
    503: "busy nas_unavailable",
}
CODE_STATUS = {c: st for st, codes in ERROR_STATUS.items() for c in codes.split()}

# Every call with a device token can fail these ways.
_TOKEN = ("missing_token invalid_token token_expired device_revoked nas_credentials_invalid "
          "edge_traffic_on_direct_listener nas_unavailable")
# ... and every call inside the chosen root these.
_ROOTED = _TOKEN + " no_root_selected not_found access_denied share_blocked nas_error"


def _doc(op: str, summary: str, tag: str, ok=None, errors: str = "", *, auth: bool = True,
         ok_status: int = 200, description: str | None = None, **extra) -> dict:
    """Route decorator kwargs: operation id, summary, tag, success schema and the error
    codes this operation can answer with (grouped by status for the OpenAPI file)."""
    responses: dict = {}
    if ok is not None:
        responses[ok_status] = {"model": ok, "description": "OK"}
    by_status: dict[int, list[str]] = {}
    for code in dict.fromkeys(errors.split()):
        by_status.setdefault(CODE_STATUS[code], []).append(code)
    for st, codes in sorted(by_status.items()):
        responses[st] = {"model": S.Error,
                         "description": ", ".join(f"`{c}`" for c in codes)}
    oe = dict(extra.pop("openapi_extra", {}))
    oe["security"] = [{"deviceToken": []}] if auth else []
    return dict(operation_id=op, summary=summary, tags=[tag], responses=responses,
                description=description, openapi_extra=oe, **extra)


def create_app(gw: Gateway, route: str) -> FastAPI:
    assert route in ("direct", "public")
    # openapi_url=None: the schema is not served; api/openapi.yaml is the published copy.
    app = FastAPI(title="Loft gateway API", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_err(_req, e: ApiError):
        headers = {"Retry-After": str(e.extra["retry_after"])} if "retry_after" in e.extra else None
        return JSONResponse({"error": e.code, **e.extra}, status_code=e.status, headers=headers)

    @app.exception_handler(NotFound)
    async def _nf(_req, _e):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(AccessDenied)
    async def _ad(_req, _e):
        return JSONResponse({"error": "access_denied"}, status_code=403)

    @app.exception_handler(FixedItem)
    async def _fixed(_req, _e):
        return JSONResponse({"error": "share_is_fixed"}, status_code=403)

    @app.exception_handler(Blocked)
    async def _blocked(_req, _e):
        return JSONResponse({"error": "share_blocked"}, status_code=403)

    @app.exception_handler(CrossShare)
    async def _cross(_req, _e):
        return JSONResponse({"error": "cross_share_move"}, status_code=409)

    @app.exception_handler(StorageError)
    async def _se(_req, e):
        return JSONResponse({"error": "nas_error", "detail": str(e)}, status_code=502)

    def ip_of(req: Request) -> str | None:
        if route == "public":
            # Only cloudflared reaches the loopback listener, so its header is
            # the client address. On direct it is never trusted.
            return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else None)
        return req.client.host if req.client else None

    def caller(req: Request, authorization: str | None) -> Caller:
        if route == "direct" and "cf-connecting-ip" in req.headers:
            raise ApiError(403, "edge_traffic_on_direct_listener")
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "missing_token")
        c = gw.authenticate(authorization[7:].strip(), ip_of(req), route)
        return c

    # ── unauthenticated ───────────────────────────────────────────────────

    @app.get("/healthz", **_doc(
        "healthz", "Liveness and route probe", "meta", S.Health, auth=False,
        description="Which listener answered (`direct` or `public`). Clients use it to pick a route."))
    def healthz():
        # Minimal on purpose: the route probe and monitoring need nothing else.
        return {"ok": True, "service": "loft", "route": route, "version": __version__}

    @app.get("/v1/identity", **_doc(
        "identity", "Pins of the direct listener's key", "meta", S.Identity, auth=False,
        description="Live and standby SPKI pins. A client refreshes its stored pins from here "
                    "over a route it already trusts (pin rotation)."))
    def identity():
        # Pins for the direct route, delivered over the WebPKI-protected public
        # route. Same answer on both listeners.
        return {"direct_spki_sha256": gw.s.direct_spki_pins}

    # ── auth ──────────────────────────────────────────────────────────────

    @app.post("/v1/auth/login", **_doc(
        "login", "Sign in with the SMB account", "auth", S.LoginResult,
        "password_login_direct_only missing_credentials account_not_allowed too_many_attempts "
        "invalid_credentials nas_unavailable", auth=False,
        description="Only on the direct listener (TLS with the pinned key). Refused with 403 "
                    "`password_login_direct_only` on the public listener (tunnel). Failed "
                    "attempts are limited per account and per address (429 with Retry-After)."))
    def login(body: LoginIn, req: Request):
        if route != "direct" or "cf-connecting-ip" in req.headers:
            raise ApiError(403, "password_login_direct_only")
        return gw.login(body.user, body.password, body.device_name, ip_of(req), route)

    @app.post("/v1/auth/codes", **_doc(
        "mint_code", "Make a sign-in code for this account", "auth", S.CodeMinted,
        "codes_direct_only missing_token invalid_token token_superseded token_expired "
        "device_revoked account_not_allowed",
        description="Direct listener only, and only for accounts matching `code_users_re` "
                    "(empty by default: nobody)."))
    def mint_code(body: CodeIn, req: Request, authorization: str | None = Header(None)):
        if route != "direct" or "cf-connecting-ip" in req.headers:
            raise ApiError(403, "codes_direct_only")
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "missing_token")
        return gw.mint_code(authorization[7:].strip(), ip_of(req), route, body.max_uses, body.days)

    @app.post("/v1/auth/code", **_doc(
        "code_login", "Sign in with a sign-in code", "auth", S.LoginResult,
        "edge_traffic_on_direct_listener invalid_code too_many_attempts code_expired "
        "account_not_allowed nas_credentials_invalid nas_unavailable", auth=False))
    def code_login(body: CodeLoginIn, req: Request):
        if route == "direct" and "cf-connecting-ip" in req.headers:
            raise ApiError(403, "edge_traffic_on_direct_listener")
        return gw.redeem_code(body.code, body.device_name, ip_of(req), route)

    @app.post("/v1/auth/refresh", **_doc(
        "refresh", "Rotate the device token", "auth", S.Token,
        "missing_token invalid_token token_superseded token_expired device_revoked",
        description="Returns a new token and moves the expiry. The previous token keeps "
                    "working for a short grace period; only the current token may rotate."))
    def refresh(req: Request, authorization: str | None = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "missing_token")
        return gw.refresh(authorization[7:].strip(), ip_of(req), route)

    @app.get("/v1/auth/devices", **_doc(
        "list_devices", "Devices signed in to this account", "auth", S.Devices, _TOKEN))
    def devices(req: Request, authorization: str | None = Header(None)):
        c = caller(req, authorization)
        return {"devices": gw.list_devices(c.user), "current": c.device_id}

    @app.delete("/v1/auth/device/{device_id}", **_doc(
        "revoke_device", "Sign a device out", "auth", S.Ok, _TOKEN + " no_such_device",
        description="Wipes the device's stored credential. Its token answers 401 "
                    "`device_revoked` from then on."))
    def revoke(device_id: str, req: Request, authorization: str | None = Header(None)):
        c = caller(req, authorization)
        gw.revoke(device_id, by_user=c.user, ip=c.ip, route=route)
        return {"ok": True}

    # ── roots ─────────────────────────────────────────────────────────────

    @app.get("/v1/me", **_doc(
        "get_me", "This device, its account and root", "roots", S.Me, _TOKEN))
    def me(req: Request, authorization: str | None = Header(None)):
        return gw.me(caller(req, authorization))

    @app.get("/v1/roots", **_doc(
        "browse_roots", "Shares, or folders inside a share", "roots", S.Shares | S.Folders,
        _TOKEN + " bad_path no_such_share not_found access_denied",
        description="Without `share`: the shares this account may use. With `share` (and "
                    "`path`): the folders there, to pick a root."))
    def roots(req: Request, share: str | None = None, path: str = "",
              authorization: str | None = Header(None)):
        return gw.browse(caller(req, authorization), share, path)

    @app.put("/v1/me/root", **_doc(
        "set_root", "Choose the folder this device syncs", "roots", S.Me,
        _TOKEN + " bad_path no_such_share not_found not_a_folder access_denied",
        description="`share` = `*` gives every share the account can open, each a top-level "
                    "folder (`fixed`)."))
    def set_root(body: RootIn, req: Request, authorization: str | None = Header(None)):
        return gw.set_root(caller(req, authorization), body.share, body.path)

    # ── items ─────────────────────────────────────────────────────────────

    @app.get("/v1/items/{item_id}", **_doc(
        "get_item", "One item", "items", S.Item, _ROOTED))
    def item(item_id: str, req: Request, authorization: str | None = Header(None)):
        return gw.get_item(caller(req, authorization), item_id)

    @app.get("/v1/items/{item_id}/children", **_doc(
        "list_children", "List a folder", "items", S.Children, _ROOTED + " not_a_folder",
        description="Paged by name. The first page re-reads the folder on the NAS."))
    def children(item_id: str, req: Request, cursor: str | None = None,
                 limit: int = Query(500, ge=1, le=2000),
                 authorization: str | None = Header(None)):
        c = caller(req, authorization)
        gw.maybe_rescan(c)
        return gw.children(c, item_id, cursor, limit)

    @app.get("/v1/changes", **_doc(
        "list_changes", "Changes since an anchor (long-poll)", "changes", S.Changes,
        _ROOTED + " bad_anchor anchor_expired root_missing",
        description="`wait` > 0 holds the request until something changes or the wait ends "
                    "(capped by the gateway). 410 `anchor_expired`: enumerate again from "
                    "the root and take a new anchor from /v1/changes/latest."))
    def changes(req: Request, anchor: str, limit: int = Query(500, ge=1, le=2000),
                wait: int = Query(0, ge=0, le=60), authorization: str | None = Header(None)):
        c = caller(req, authorization)
        gw.maybe_rescan(c)
        return gw.changes(c, anchor, limit, wait)

    @app.get("/v1/changes/latest", **_doc(
        "latest_anchor", "The current anchor", "changes", S.Anchor, _TOKEN + " no_root_selected"))
    def latest(req: Request, authorization: str | None = Header(None)):
        c = caller(req, authorization)
        return {"anchor": gw.latest_anchor(gw._root(c))}

    @app.get("/v1/items/{item_id}/content", **_doc(
        "get_content", "Download a file (Range supported)", "items", None,
        _ROOTED + " is_a_folder bad_range range_not_satisfiable",
        description="One `bytes=` range: `a-b`, `a-` or the suffix form `-n`. 206 with "
                    "Content-Range for a range, 200 for the whole file. ETag = content_etag.",
        openapi_extra={"responses": {
            "200": {"description": "Whole file", "content": {"application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}}}},
            "206": {"description": "The requested range", "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
        }}))
    def content(item_id: str, req: Request, range: str | None = Header(None),
                authorization: str | None = Header(None)):
        c = caller(req, authorization)
        it, share, path = gw.open_content(c, item_id)
        size = it["size"]
        headers = {"ETag": f'"c{it["content_version"]}"', "Accept-Ranges": "bytes",
                   "Cache-Control": "no-store"}
        start, end, status = 0, size - 1, 200
        if range:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range.strip())
            if not m or (not m[1] and not m[2]):
                raise ApiError(416, "bad_range")
            if m[1]:
                start = int(m[1])
                end = min(int(m[2]), size - 1) if m[2] else size - 1
            else:                                # suffix range: last N bytes
                start = max(size - int(m[2]), 0)
            if start >= size or start > end:
                return JSONResponse({"error": "range_not_satisfiable"}, status_code=416,
                                    headers={"Content-Range": f"bytes */{size}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(end - start + 1, 0)
        headers["Content-Length"] = str(length)
        body = c.session.read(share, path, start, length) if length else iter(())
        return StreamingResponse(body, status_code=status, headers=headers,
                                 media_type="application/octet-stream")

    @app.get("/v1/items/{item_id}/thumbnail", **_doc(
        "get_thumbnail", "A thumbnail of an image, PDF, video or document", "items", None,
        _ROOTED + " is_a_folder no_thumbnail busy",
        description="JPEG or PNG, at most `px` on the long side. 404 `no_thumbnail` when the "
                    "file has none (unknown kind, too big, undecodable).",
        openapi_extra={"responses": {"200": {"description": "The thumbnail", "content": {
            "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            "image/png": {"schema": {"type": "string", "format": "binary"}}}}}}))
    def thumbnail(item_id: str, req: Request, px: int = Query(256, ge=16, le=1024),
                  authorization: str | None = Header(None)):
        fmt, data, etag = gw.thumbnail(caller(req, authorization), item_id, px)
        return Response(data, media_type=f"image/{fmt}",
                        headers={"ETag": f'"{etag}"', "Cache-Control": "private, no-store"})

    @app.post("/v1/folders", **_doc(
        "create_folder", "Make a folder", "items", S.Item,
        _ROOTED + " not_a_folder share_is_fixed invalid_or_excluded_name name_exists"))
    def mkdir(body: FolderIn, req: Request, authorization: str | None = Header(None)):
        return gw.mkdir(caller(req, authorization), body.parent_id, body.name)

    @app.patch("/v1/items/{item_id}", **_doc(
        "update_item", "Rename and/or move", "items", S.Item,
        _ROOTED + " cannot_move_root meta_etag_mismatch not_a_folder move_into_self "
        "invalid_or_excluded_name name_exists name_exists_on_nas share_is_fixed",
        description="`if_match` = the item's meta_etag; 412 with `current` if it moved on."))
    def patch(item_id: str, body: PatchIn, req: Request, authorization: str | None = Header(None)):
        return gw.patch(caller(req, authorization), item_id, body.name, body.parent_id,
                        body.if_match)

    @app.delete("/v1/items/{item_id}", **_doc(
        "delete_item", "Move to the root's trash", "items", S.Deleted,
        _ROOTED + " cannot_delete_root content_etag_mismatch share_is_fixed",
        description="Idempotent. `if_match` (query) = content_etag for files; 412 with "
                    "`current` if the file changed. Trash is kept `trash_days`."))
    def delete(item_id: str, req: Request, if_match: str | None = None,
               authorization: str | None = Header(None)):
        return gw.delete(caller(req, authorization), item_id, if_match)

    @app.get("/v1/items/{item_id}/versions", **_doc(
        "list_versions", "Earlier versions of a file", "items", S.Versions, _ROOTED))
    def versions(item_id: str, req: Request, authorization: str | None = Header(None)):
        return {"versions": gw.versions(caller(req, authorization), item_id)}

    @app.post("/v1/items/{item_id}/versions/{version_id}/restore", **_doc(
        "restore_version", "Make an earlier version current", "items", S.Item,
        _ROOTED + " no_such_version",
        description="The current content becomes a version itself; nothing is lost."))
    def restore(item_id: str, version_id: str, req: Request,
                authorization: str | None = Header(None)):
        return gw.restore_version(caller(req, authorization), item_id, version_id)

    # ── uploads ───────────────────────────────────────────────────────────

    @app.post("/v1/uploads", **_doc(
        "upload_create", "Start an upload session", "uploads", S.Upload,
        _ROOTED + " bad_upload near_id_required",
        description="Declare size and SHA-256 up front. `near_id` (the parent for a new "
                    "file, the item for a modify) is required in an all-shares root."))
    def upload_create(body: UploadIn, req: Request, authorization: str | None = Header(None)):
        return gw.upload_create(caller(req, authorization), body.size, body.sha256.lower(),
                                body.near_id)

    @app.get("/v1/uploads/{upload_id}", **_doc(
        "upload_status", "How much of an upload the gateway holds", "uploads", S.Upload,
        _ROOTED + " no_such_upload", description="Resume from `received`."))
    def upload_status(upload_id: str, req: Request, authorization: str | None = Header(None)):
        return gw.upload_status(caller(req, authorization), upload_id)

    @app.put("/v1/uploads/{upload_id}", **_doc(
        "upload_chunk", "Send the next chunk", "uploads", S.Upload,
        _ROOTED + " no_such_upload wrong_offset chunk_too_large chunk_hash_mismatch",
        description="Raw bytes at `offset`, which must equal `received` (409 `wrong_offset` "
                    "carries the right one). At most `chunk_max` bytes. `X-Chunk-SHA256` "
                    "(hex) is checked when sent.",
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}}))
    async def upload_chunk(upload_id: str, req: Request, offset: int = Query(..., ge=0),
                           x_chunk_sha256: str | None = Header(None),
                           authorization: str | None = Header(None)):
        from starlette.concurrency import run_in_threadpool
        declared = int(req.headers.get("content-length") or 0)
        if declared > gw.s.upload_chunk_max:
            raise ApiError(413, "chunk_too_large")
        data = bytearray()
        async for part in req.stream():
            data += part
            if len(data) > gw.s.upload_chunk_max:
                raise ApiError(413, "chunk_too_large")
        c = await run_in_threadpool(caller, req, authorization)
        return await run_in_threadpool(gw.upload_chunk, c, upload_id, offset, bytes(data),
                                       x_chunk_sha256)

    @app.post("/v1/uploads/{upload_id}/commit", **_doc(
        "upload_commit", "Place a finished upload", "uploads", S.Commit,
        _ROOTED + " no_such_upload upload_incomplete file_hash_mismatch is_a_folder "
        "parent_and_name_required invalid_or_excluded_name not_a_folder share_is_fixed "
        "name_exists_on_nas",
        description="New file: `parent_id` + `name`. Modify: `item_id` + `if_match` "
                    "(content_etag). Safe-save: `replaces`. A stale `if_match` or a name "
                    "taken by another file never overwrites: the upload lands as a conflict "
                    "copy and `result` is `conflict`."))
    def upload_commit(upload_id: str, body: CommitIn, req: Request,
                      authorization: str | None = Header(None)):
        return gw.commit(caller(req, authorization), upload_id, body.parent_id, body.name,
                         body.item_id, body.if_match, body.replaces)

    return app
