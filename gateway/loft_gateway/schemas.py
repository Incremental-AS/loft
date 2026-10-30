"""Response shapes of the HTTP API, for the OpenAPI file (api/openapi.yaml) only.

The handlers return plain dicts built in service.py; these models are attached as
`responses=` metadata, never as `response_model=`, so they document the wire format
without filtering or re-validating it at runtime. The conformance suite
(conformance/) validates real responses against the generated schemas, so a field
that drifts from service.py fails there.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    # Clients must ignore keys they don't know: the gateway may add fields.
    model_config = ConfigDict(extra="allow")


class Error(_Open):
    """Every gateway error. `error` is a stable machine code; clients branch on it, never
    on the HTTP status alone. Some codes carry extra keys (listed below)."""
    error: str = Field(description="Stable error code, e.g. `invalid_token`, `name_exists`, "
                                   "`anchor_expired`. See the API description for the full list.")
    detail: str | None = Field(None, description="Human-readable detail (`nas_unavailable`, "
                                                 "`nas_error`). Not stable; for logs only.")
    retry_after: int | None = Field(None, description="`too_many_attempts`: seconds; also "
                                                      "sent as the `Retry-After` header.")
    received: int | None = Field(None, description="`wrong_offset`, `upload_incomplete`: bytes "
                                                   "the gateway holds; resume from here.")
    existing_id: str | None = Field(None, description="`name_exists`: id of the item that "
                                                      "already has the name.")
    current: Item | None = Field(None, description="`*_etag_mismatch`: the item as the "
                                                   "gateway has it now.")


class Health(BaseModel):
    ok: bool
    service: Literal["loft"]
    route: Literal["direct", "public"]
    version: str


class Identity(BaseModel):
    direct_spki_sha256: list[str] = Field(description="base64 SHA-256 of the direct "
                                                      "listener key's SubjectPublicKeyInfo: "
                                                      "live, then standby.")


class Token(_Open):
    device_id: str
    token: str = Field(description="Device token, `nst_<device_id>.<secret>`. Send as "
                                   "`Authorization: Bearer <token>`. Store it in the "
                                   "platform's secure store.")
    expires_at: float = Field(description="Unix time. Sliding: every refresh moves it.")


class LoginResult(Token):
    nas_user: str


class CodeMinted(BaseModel):
    code: str = Field(description="Shown once, never stored in clear: `XXXX-XXXX-...`")
    code_id: str
    nas_user: str
    max_uses: int
    expires_at: float


class Device(_Open):
    id: str
    device_name: str
    created_at: float
    last_used_at: float | None
    last_ip: str | None
    expires_at: float
    revoked_at: float | None


class Devices(BaseModel):
    devices: list[Device]
    current: str = Field(description="The calling device's id.")


class Ok(BaseModel):
    ok: Literal[True]


class Root(BaseModel):
    id: str
    share: str = Field(description="Share name, or `*` for every share the account can open.")
    path: str
    root_item_id: str = Field(description="Item id of the root folder (same as `id`).")
    anchor: str = Field(description="Current change-feed anchor for this root.")


class Me(BaseModel):
    nas_user: str
    device_id: str
    device_name: str
    route: Literal["direct", "public"]
    root: Root | None = Field(description="null until the device picks a root with "
                                          "PUT /v1/me/root.")


class Shares(BaseModel):
    shares: list[str]


class Folders(BaseModel):
    share: str
    path: str
    folders: list[str]


class Item(_Open):
    id: str
    parent_id: str | None = Field(description="null for the root.")
    name: str
    type: Literal["file", "dir"]
    size: int
    mtime_ns: int
    content_etag: str = Field(description="Changes when the content changes. `If-Match` for "
                                          "modify and delete.")
    meta_etag: str = Field(description="Changes on rename/move. `if_match` for PATCH.")
    sha256: str | None = Field(description="Known when the file was uploaded through Loft.")
    fixed: bool = Field(description="All-shares root: the root and each share. No rename, "
                                    "move or delete.")


class Children(BaseModel):
    items: list[Item]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on "
                                                "the last page.")


class Changes(BaseModel):
    updated: list[Item]
    deleted: list[str] = Field(description="Item ids gone since the anchor.")
    anchor: str = Field(description="Opaque. Store it and send it next time.")
    more: bool = Field(description="true: call again at once with the new anchor.")


class Anchor(BaseModel):
    anchor: str


class Version(BaseModel):
    id: str
    content_version: int
    size: int
    mtime_ns: int
    created_at: float


class Versions(BaseModel):
    versions: list[Version]


class Upload(BaseModel):
    upload_id: str
    received: int = Field(description="Bytes the gateway holds. The next chunk starts here.")
    size: int
    chunk_max: int | None = Field(None, description="Largest chunk accepted (create only).")


class Commit(BaseModel):
    result: Literal["created", "modified", "replaced", "conflict"] = Field(
        description="`conflict`: the upload was kept as a conflict copy beside the original; "
                    "nothing was overwritten.")
    item: Item
    replaced_id: str | None = Field(description="`replaced`: the item this one superseded.")


class Deleted(BaseModel):
    result: Literal["trashed", "superseded", "already_deleted"] = Field(
        description="All three are success (delete is idempotent).")
    trash_id: str | None = None
    superseded_by: str | None = None


Error.model_rebuild()
