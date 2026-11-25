"""Image loading for vision-capable members.

Accepts local paths (absolute or relative to the MCP server's working
directory), ``http(s)`` URLs, and ``data:image/...;base64,...`` URIs. Everything
is normalised to a base64 data URL because local OpenAI-compatible servers
(Ollama, LM Studio, llama.cpp) are not required to fetch remote URLs themselves.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sub_family.errors import ImageError

MAX_IMAGES_PER_CALL = 8
_REMOTE_TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True)
class ImageBlob:
    """An image ready to be embedded into a chat message."""

    source: str
    mime: str
    size_bytes: int
    data_url: str


def sniff_mime(data: bytes) -> str | None:
    """Detect a supported image MIME type from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def load_images(
    sources: list[str],
    *,
    http: httpx.AsyncClient,
    max_bytes: int,
    max_images: int = MAX_IMAGES_PER_CALL,
) -> list[ImageBlob]:
    """Load and validate all images for one call.

    Raises :class:`ImageError` with a precise message for missing files,
    unsupported formats, oversized payloads, or download failures.
    """
    if not sources:
        return []
    if len(sources) > max_images:
        raise ImageError(
            f"too many images ({len(sources)}); at most {max_images} per call are supported"
        )
    blobs: list[ImageBlob] = []
    for source in sources:
        blobs.append(await load_image(source, http=http, max_bytes=max_bytes))
    return blobs


async def load_image(source: str, *, http: httpx.AsyncClient, max_bytes: int) -> ImageBlob:
    """Load one image from a path, URL, or data URI."""
    value = source.strip()
    if not value:
        raise ImageError("image reference must not be empty")
    if value.startswith("data:"):
        data, declared = _decode_data_uri(value, max_bytes=max_bytes)
        return _finish(value, data, max_bytes=max_bytes, declared=declared)
    if value.lower().startswith(("http://", "https://")):
        data = await _download(value, http=http, max_bytes=max_bytes)
        return _finish(value, data, max_bytes=max_bytes)
    return _load_path(value, max_bytes=max_bytes)


def _load_path(value: str, *, max_bytes: int) -> ImageBlob:
    path = Path(value).expanduser()
    if not path.exists():
        raise ImageError(f"image not found: {value}")
    if not path.is_file():
        raise ImageError(f"image path is not a regular file: {value}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageError(f"cannot stat image {value}: {exc}") from exc
    _check_size(size, max_bytes, label=value)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read image {value}: {exc}") from exc
    return _finish(value, data, max_bytes=max_bytes)


async def _download(url: str, *, http: httpx.AsyncClient, max_bytes: int) -> bytes:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ImageError(f"unsupported image URL: {url}")
    chunks: list[bytes] = []
    total = 0
    try:
        async with http.stream(
            "GET", url, timeout=_REMOTE_TIMEOUT_S, follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                raise ImageError(f"failed to download image {url}: HTTP {response.status_code}")
            declared_length = response.headers.get("content-length")
            if declared_length and declared_length.isdigit():
                _check_size(int(declared_length), max_bytes, label=url)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    _check_size(total, max_bytes, label=url)
                chunks.append(chunk)
    except ImageError:
        raise
    except httpx.HTTPError as exc:
        raise ImageError(f"failed to download image {url}: {exc}") from exc
    data = b"".join(chunks)
    if not data:
        raise ImageError(f"downloaded image is empty: {url}")
    return data


def _finish(source: str, data: bytes, *, max_bytes: int, declared: str | None = None) -> ImageBlob:
    _check_size(len(data), max_bytes, label=source)
    mime = sniff_mime(data)
    if mime is None:
        hint = f" (server declared {declared})" if declared else ""
        raise ImageError(
            f"unsupported image format for {source}{hint}; supported: PNG, JPEG, GIF, WebP"
        )
    if declared and declared.startswith("image/") and declared != mime:
        # Trust the bytes, but do not fail: some exporters mislabel screenshots.
        pass
    encoded = base64.b64encode(data).decode("ascii")
    return ImageBlob(
        source=source,
        mime=mime,
        size_bytes=len(data),
        data_url=f"data:{mime};base64,{encoded}",
    )


def _decode_data_uri(value: str, *, max_bytes: int) -> tuple[bytes, str | None]:
    header, separator, payload = value.partition(",")
    if not separator or not header.startswith("data:"):
        raise ImageError("malformed data URI for image")
    meta = header[5:]
    declared = meta.split(";", 1)[0] or None
    if ";base64" not in meta:
        raise ImageError("only base64 data URIs are supported for images")
    # Reject obviously oversized payloads before allocating the decoded bytes.
    if len(payload) > (max_bytes * 4 // 3) + 8:
        _check_size(len(payload) * 3 // 4, max_bytes, label="data URI")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("image data URI contains invalid base64") from exc
    if not data:
        raise ImageError("image data URI is empty")
    return data, declared


def _check_size(size: int, max_bytes: int, *, label: str) -> None:
    if size > max_bytes:
        raise ImageError(
            f"image {label} is {size / (1024 * 1024):.1f} MB, "
            f"above the configured limit of {max_bytes / (1024 * 1024):.1f} MB "
            "(security.max_image_mb)"
        )
