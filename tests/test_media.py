"""Image loading and normalisation."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from sub_family.errors import ImageError
from sub_family.providers.media import load_image, load_images, sniff_mime

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
JPEG = b"\xff\xd8\xff" + b"payload"
GIF = b"GIF89a" + b"payload"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"payload"


def test_sniff_known_formats() -> None:
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(GIF) == "image/gif"
    assert sniff_mime(WEBP) == "image/webp"
    assert sniff_mime(b"not an image") is None


async def test_load_local_path(tmp_path: Path) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(PNG)
    async with httpx.AsyncClient() as client:
        blob = await load_image(str(path), http=client, max_bytes=1024)
    assert blob.mime == "image/png"
    assert blob.data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(blob.data_url.split(",", 1)[1]) == PNG


async def test_load_data_uri() -> None:
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    async with httpx.AsyncClient() as client:
        blob = await load_image(uri, http=client, max_bytes=1024)
    assert blob.mime == "image/png"


async def test_download_remote_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        blob = await load_image("https://example.com/shot.png", http=client, max_bytes=1024)
    assert blob.mime == "image/png"
    assert blob.size_bytes == len(PNG)


async def test_download_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageError) as error:
            await load_image("https://example.com/missing.png", http=client, max_bytes=1024)
    assert "404" in str(error.value)


async def test_missing_file() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError) as error:
            await load_image("/definitely/not/here.png", http=client, max_bytes=1024)
    assert "not found" in str(error.value)


async def test_directory_is_rejected(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError):
            await load_image(str(tmp_path), http=client, max_bytes=1024)


async def test_oversized_image(tmp_path: Path) -> None:
    path = tmp_path / "big.png"
    path.write_bytes(PNG + b"x" * 100)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError) as error:
            await load_image(str(path), http=client, max_bytes=10)
    assert "above the configured limit" in str(error.value)


async def test_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / "vector.svg"
    path.write_text("<svg/>", encoding="utf-8")
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError) as error:
            await load_image(str(path), http=client, max_bytes=1024)
    assert "unsupported image format" in str(error.value)


async def test_invalid_data_uri() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError):
            await load_image("data:image/png,plain", http=client, max_bytes=1024)


async def test_oversized_data_uri_is_rejected_before_decoding() -> None:
    payload = base64.b64encode(PNG + b"x" * 10_000).decode()
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError) as error:
            await load_image(f"data:image/png;base64,{payload}", http=client, max_bytes=100)
    assert "above the configured limit" in str(error.value)


async def test_too_many_images(tmp_path: Path) -> None:
    path = tmp_path / "one.png"
    path.write_bytes(PNG)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ImageError) as error:
            await load_images([str(path)] * 9, http=client, max_bytes=1024)
    assert "too many images" in str(error.value)
