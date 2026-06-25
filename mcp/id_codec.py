"""
registry_service/app/id_codec.py

Helpers for converting external canonical IDs (such as did:web values) into
SpiceDB-safe object IDs and back again.

SpiceDB object IDs reject characters like ':' and '.'. To keep a stateless,
reversible transform, we use base64url encoding and tag encoded values with a
prefix so decode logic is deterministic.
"""
from __future__ import annotations

import base64
import re

# SpiceDB object ID allowed character set (documented behavior):
# [a-zA-Z0-9/_|=+-], max length 1024.
_SPICEDB_SAFE_RE = re.compile(r"^[a-zA-Z0-9/_|=+\-]{1,1024}$")
_ENC_PREFIX = "b64_"


def to_storage_id(external_id: str) -> str:
    """
    Convert an external ID to a SpiceDB-safe storage ID.

    If the ID is already SpiceDB-safe, keep it as-is.
    Otherwise encode with base64url and prefix with b64_.
    """
    if _SPICEDB_SAFE_RE.fullmatch(external_id):
        return external_id

    encoded = base64.urlsafe_b64encode(external_id.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_ENC_PREFIX}{encoded}"


def to_external_id(storage_id: str) -> str:
    """
    Convert a storage ID back to the caller-facing canonical ID.

    Non-encoded IDs pass through unchanged.
    """
    if not storage_id.startswith(_ENC_PREFIX):
        return storage_id

    payload = storage_id[len(_ENC_PREFIX):]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    return decoded
