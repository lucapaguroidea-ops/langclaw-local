"""
``Bucket`` — a small async wrapper over an S3-compatible bucket.

boto3 is synchronous, so each call runs in a worker thread. Errors surface as
:class:`BucketError` with a readable message; tools turn those into
``{"error": ...}`` dicts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langclaw.config.schema import BucketConfig


class BucketError(RuntimeError):
    """A bucket operation failed (missing object, bad credentials, unreachable...)."""


@dataclass(slots=True)
class BucketObject:
    key: str
    size: int
    modified: str  # ISO 8601


class Bucket:
    """Async access to one bucket — or, via :meth:`with_prefix`, to one folder of
    it that the caller can't step out of.

    Keys given to and returned by every method are relative to that folder.

    Args:
        config: Endpoint, name, and credentials.
        client: An existing boto3 S3 client (tests inject one); built from
            *config* when omitted.
    """

    def __init__(self, config: BucketConfig, *, client: Any | None = None) -> None:
        if not config.configured and client is None:
            raise BucketError(
                "No bucket configured: set LANGCLAW__DOCUMENTS__BUCKET__NAME / "
                "ACCESS_KEY / SECRET_KEY (or Railway's BUCKET_* variables)."
            )
        self.name = config.name
        self._client = client or _make_client(config)
        self.root = ""

    def with_prefix(self, prefix: str) -> Bucket:
        """A view of this bucket confined to *prefix* (e.g. ``tenants/acme/``)."""
        view = object.__new__(Bucket)
        view.name, view._client = self.name, self._client
        view.root = self.root + _check_key(prefix.rstrip("/")) + "/"
        return view

    def _full(self, key: str) -> str:
        return self.root + _check_key(key)

    async def list(self, prefix: str = "", *, limit: int = 100) -> list[BucketObject]:
        """Objects under *prefix*, newest first (at most *limit*)."""
        full_prefix = self.root + (_check_key(prefix) if prefix else "")

        def _list() -> list[BucketObject]:
            out: list[BucketObject] = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.name, Prefix=full_prefix):
                for item in page.get("Contents", []):
                    out.append(
                        BucketObject(
                            key=item["Key"][len(self.root) :],
                            size=int(item.get("Size", 0)),
                            modified=item["LastModified"].isoformat(),
                        )
                    )
            out.sort(key=lambda o: o.modified, reverse=True)
            return out[:limit]

        return await self._run(_list, f"list {prefix!r}")

    async def get(self, key: str) -> tuple[bytes, str]:
        """``(content, content_type)`` of *key*."""

        full = self._full(key)

        def _get() -> tuple[bytes, str]:
            resp = self._client.get_object(Bucket=self.name, Key=full)
            return resp["Body"].read(), resp.get("ContentType") or ""

        return await self._run(_get, f"read {key!r}")

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        """Upload *data* to *key* (overwrites)."""

        full = self._full(key)

        def _put() -> None:
            extra = {"ContentType": content_type} if content_type else {}
            self._client.put_object(Bucket=self.name, Key=full, Body=data, **extra)

        await self._run(_put, f"upload {key!r}")

    async def link(self, key: str, *, expires_s: int = 3600) -> str:
        """A time-limited download URL for *key*."""

        full = self._full(key)

        def _link() -> str:
            self._client.head_object(Bucket=self.name, Key=full)  # 404 early, not a dead link
            return self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self.name, "Key": full}, ExpiresIn=expires_s
            )

        return await self._run(_link, f"link {key!r}")

    async def _run(self, fn: Any, what: str) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001 — normalised for tools
            raise BucketError(f"Bucket {what} failed: {_reason(exc)}") from exc


def _check_key(key: str) -> str:
    """A key a caller (or a model) passed: relative, no ``..`` segments."""
    parts = (key or "").split("/")
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(p in ("", ".", "..") for p in parts[:-1])
        or parts[-1] in (".", "..")
    ):
        raise BucketError(
            f"Invalid file key {key!r}: use a relative path like 'inbox/2026-09-27/invoice.pdf'."
        )
    return key


def _reason(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error", {})
        code, msg = err.get("Code", ""), err.get("Message", "")
        if code in ("NoSuchKey", "404"):
            return "no such object"
        return f"{code} {msg}".strip() or str(exc)
    return str(exc)


def _make_client(config: BucketConfig) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise BucketError(
            "The bucket tools need the documents extra: uv add 'langclaw[documents]'"
        ) from exc
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint or None,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region or "auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
