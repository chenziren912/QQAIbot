from __future__ import annotations

import httpx
import pytest

from app.media import MediaStore, MediaTooLargeError, image_file_to_data_uri


PNG = b"\x89PNG\r\n\x1a\n" + b"not-a-real-png-payload"


def test_store_retains_newest_binary_and_returns_data_uri(tmp_path):
    store = MediaStore(tmp_path, max_image_bytes=1024, budget_bytes=len(PNG) + 1)

    first = store.store_bytes(PNG, source_url="https://cdn.example/one.png")
    second = store.store_bytes(PNG, source_url="https://cdn.example/two.png")

    assert not first.path.exists()
    assert second.path.exists()
    assert store.storage_usage_bytes() == len(PNG)
    assert image_file_to_data_uri(second.path).startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_async_download_uses_supplied_client_and_persists_original(tmp_path):
    def handler(request):
        assert str(request.url) == "https://cdn.example/image"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    store = MediaStore(tmp_path, allow_private_hosts=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        record = await store.download_image("https://cdn.example/image", client=client)

    assert record.path.read_bytes() == PNG
    assert record.mime_type == "image/png"


def test_download_rejects_declared_oversized_body_without_writing(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "999"},
            content=PNG,
        )

    store = MediaStore(tmp_path, max_image_bytes=100, allow_private_hosts=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaTooLargeError):
            store.download_image_sync("https://cdn.example/image", client=client)
    assert store.storage_usage_bytes() == 0
