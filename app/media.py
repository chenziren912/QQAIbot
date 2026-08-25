"""Safe, local storage for images received from QQ events.

The database deliberately does not live in this module.  Callers should keep a
``StoredMedia`` record in their own database; when a storage budget is reached
this module only removes the original binary and returns enough information for
the caller to mark that record as unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import mimetypes
import os
import socket
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlsplit

try:  # Keep importing the application possible before optional dependencies install.
    import httpx
except ImportError:  # pragma: no cover - exercised only in incomplete installs
    httpx = None  # type: ignore[assignment]


DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_BUDGET_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 20.0

SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    (
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    )
)

_MIME_EXTENSIONS = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}


class MediaError(Exception):
    """Base class for errors raised by :mod:`app.media`."""


class MediaDependencyError(MediaError):
    """The optional ``httpx`` dependency is unavailable."""


class UnsafeMediaURLError(MediaError):
    """The URL is not an acceptable remote image URL."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__("Unsafe media URL ({0}): {1}".format(reason, url))


class MediaDownloadError(MediaError):
    """A remote server could not provide an image."""

    def __init__(self, url: str, reason: str, status_code: Optional[int] = None) -> None:
        self.url = url
        self.reason = reason
        self.status_code = status_code
        detail = "Media download failed ({0}): {1}".format(reason, url)
        super().__init__(detail)


class MediaTooLargeError(MediaError):
    """An image exceeds the configured per-image limit."""

    def __init__(self, limit_bytes: int, actual_bytes: Optional[int] = None) -> None:
        self.limit_bytes = limit_bytes
        self.actual_bytes = actual_bytes
        if actual_bytes is None:
            message = "Image exceeds the {0}-byte limit".format(limit_bytes)
        else:
            message = "Image is {0} bytes; limit is {1} bytes".format(actual_bytes, limit_bytes)
        super().__init__(message)


class UnsupportedMediaTypeError(MediaError):
    """The downloaded body is not a supported raster image."""

    def __init__(self, content_type: Optional[str] = None, reason: str = "unsupported image") -> None:
        self.content_type = content_type
        self.reason = reason
        label = content_type or "unknown"
        super().__init__("Unsupported media type ({0}): {1}".format(label, reason))


class MediaStorageError(MediaError):
    """A local media file could not be safely read, written, or removed."""


class MediaBudgetExceededError(MediaStorageError):
    """A single image cannot fit within the configured media budget."""

    def __init__(self, image_bytes: int, budget_bytes: int) -> None:
        self.image_bytes = image_bytes
        self.budget_bytes = budget_bytes
        super().__init__(
            "Image is {0} bytes but the media budget is {1} bytes".format(
                image_bytes, budget_bytes
            )
        )


@dataclass(frozen=True)
class StoredMedia:
    """Metadata for one locally retained source image.

    ``path`` is deliberately an absolute :class:`~pathlib.Path`, while
    ``relative_path`` is safe to persist and reconstruct below the store root.
    ``metadata`` is never written by this module; it is provided for the
    caller's database record.
    """

    media_id: str
    path: Path
    relative_path: str
    source_url: str
    downloaded_url: str
    mime_type: str
    byte_size: int
    downloaded_at: datetime
    declared_content_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """Return JSON-friendly metadata suitable for a caller-owned database."""

        return {
            "media_id": self.media_id,
            "path": str(self.path),
            "relative_path": self.relative_path,
            "source_url": self.source_url,
            "downloaded_url": self.downloaded_url,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "downloaded_at": self.downloaded_at.isoformat(),
            "declared_content_type": self.declared_content_type,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvictedMedia:
    """A binary removed by :meth:`MediaStore.evict_to_budget`.

    The caller should retain the database metadata and mark this relative path
    as no longer present, rather than deleting the associated event record.
    """

    media_id: str
    path: Path
    relative_path: str
    byte_size: int
    modified_at: datetime


def _normalise_mime_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.split(";", 1)[0].strip().lower()
    aliases = {
        "image/jpg": "image/jpeg",
        "image/x-png": "image/png",
        "image/x-ms-bmp": "image/bmp",
        "image/x-tiff": "image/tiff",
    }
    return aliases.get(value, value)


def detect_image_mime(data: Union[bytes, bytearray, memoryview]) -> Optional[str]:
    """Return a MIME type when ``data`` has a recognised raster-image signature.

    SVG is intentionally not accepted.  It is XML rather than a binary image
    and accepting arbitrary SVG fetched from the network makes the media cache
    substantially harder to reason about.
    """

    header = bytes(data[:32])
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 3 and header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    # ISO Base Media files use a size + ftyp + brand header.  AVIF/HEIF may
    # have compatible brands after the major brand, so inspect the first box.
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
    return None


def detect_image_file_mime(path: Union[str, Path]) -> Optional[str]:
    """Detect an image MIME type from a local file's magic bytes."""

    try:
        with Path(path).open("rb") as source:
            return detect_image_mime(source.read(32))
    except OSError as exc:
        raise MediaStorageError("Could not read media file: {0}".format(path)) from exc


def image_file_to_data_uri(
    path: Union[str, Path],
    mime_type: Optional[str] = None,
    max_bytes: Optional[int] = DEFAULT_MAX_IMAGE_BYTES,
) -> str:
    """Encode a stored image as a Chat Completions-compatible ``data:`` URI.

    The file is signature-checked even when the caller supplies ``mime_type``;
    the supplied type is used only when it agrees with the detected content.
    This avoids labelling an arbitrary local file as an image in a model input.
    """

    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise MediaStorageError("Could not stat media file: {0}".format(candidate)) from exc
    if max_bytes is not None and size > max_bytes:
        raise MediaTooLargeError(max_bytes, size)
    try:
        body = candidate.read_bytes()
    except OSError as exc:
        raise MediaStorageError("Could not read media file: {0}".format(candidate)) from exc

    detected = detect_image_mime(body)
    declared = _normalise_mime_type(mime_type)
    if detected is None:
        # A filename is a useful last hint for a caller migrating an old cache,
        # but it is never enough to accept unrecognised content.
        guessed = _normalise_mime_type(mimetypes.guess_type(str(candidate))[0])
        raise UnsupportedMediaTypeError(declared or guessed, "unrecognised image signature")
    if declared is not None and declared not in SUPPORTED_IMAGE_MIME_TYPES:
        raise UnsupportedMediaTypeError(declared)

    encoded = base64.b64encode(body).decode("ascii")
    return "data:{0};base64,{1}".format(detected, encoded)


# A clear alias for code building multimodal Chat Completions content blocks.
encode_image_as_data_uri = image_file_to_data_uri


class MediaStore:
    """Download and retain original images in a dedicated local directory.

    Network URLs are limited to HTTP(S) and, by default, hosts resolving only
    to globally routable addresses.  Set ``allow_private_hosts=True`` solely
    for a controlled development proxy or a test server; production QQ image
    URLs should keep the safe default.
    """

    def __init__(
        self,
        root: Union[str, Path],
        *,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        budget_bytes: int = DEFAULT_MEDIA_BUDGET_BYTES,
        timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        max_redirects: int = 3,
        allow_private_hosts: bool = False,
    ) -> None:
        if max_image_bytes <= 0:
            raise ValueError("max_image_bytes must be positive")
        if budget_bytes < 0:
            raise ValueError("budget_bytes must not be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")

        self.root = Path(root).expanduser().resolve()
        self.originals_dir = self.root / "originals"
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.max_image_bytes = int(max_image_bytes)
        self.budget_bytes = int(budget_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self.max_redirects = int(max_redirects)
        self.allow_private_hosts = bool(allow_private_hosts)
        self._lock = threading.RLock()

    def set_budget_bytes(self, budget_bytes: int) -> List[EvictedMedia]:
        """Set a new persistent-binary budget and eagerly enforce it."""

        if budget_bytes < 0:
            raise ValueError("budget_bytes must not be negative")
        with self._lock:
            self.budget_bytes = int(budget_bytes)
            return self.evict_to_budget()

    async def download_image(
        self,
        url: str,
        *,
        client: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoredMedia:
        """Safely fetch ``url`` and persist its original image bytes.

        A caller-provided ``httpx.AsyncClient`` is not closed.  It is useful for
        application-wide connection pooling and deterministic tests.
        """

        self._require_httpx()
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
        try:
            if owns_client:
                async with client:
                    body, content_type, downloaded_url = await self._download_async(client, url)
            else:
                body, content_type, downloaded_url = await self._download_async(client, url)
        except MediaError:
            raise
        except httpx.HTTPError as exc:
            raise MediaDownloadError(url, str(exc)) from exc

        return await asyncio.to_thread(
            self.store_bytes,
            body,
            source_url=url,
            downloaded_url=downloaded_url,
            content_type=content_type,
            metadata=metadata,
        )

    async def fetch_image(self, url: str, **kwargs: Any) -> StoredMedia:
        """Alias for :meth:`download_image`."""

        return await self.download_image(url, **kwargs)

    def download_image_sync(
        self,
        url: str,
        *,
        client: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoredMedia:
        """Synchronous counterpart to :meth:`download_image`."""

        self._require_httpx()
        owns_client = client is None
        if owns_client:
            client = httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
        try:
            if owns_client:
                with client:
                    body, content_type, downloaded_url = self._download_sync(client, url)
            else:
                body, content_type, downloaded_url = self._download_sync(client, url)
        except MediaError:
            raise
        except httpx.HTTPError as exc:
            raise MediaDownloadError(url, str(exc)) from exc
        return self.store_bytes(
            body,
            source_url=url,
            downloaded_url=downloaded_url,
            content_type=content_type,
            metadata=metadata,
        )

    def fetch_image_sync(self, url: str, **kwargs: Any) -> StoredMedia:
        """Alias for :meth:`download_image_sync`."""

        return self.download_image_sync(url, **kwargs)

    def store_bytes(
        self,
        data: Union[bytes, bytearray, memoryview],
        *,
        source_url: str = "",
        downloaded_url: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoredMedia:
        """Persist already-downloaded image bytes after validating their type."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        body = bytes(data)
        if len(body) > self.max_image_bytes:
            raise MediaTooLargeError(self.max_image_bytes, len(body))

        detected_mime = detect_image_mime(body)
        declared_mime = _normalise_mime_type(content_type)
        if detected_mime is None:
            raise UnsupportedMediaTypeError(declared_mime, "unrecognised image signature")
        if declared_mime is not None and declared_mime.startswith("image/"):
            # A bad content-type should not determine the locally stored type;
            # QQ CDNs occasionally use image/jpg or application/octet-stream.
            # The magic-byte type remains authoritative.
            if declared_mime not in SUPPORTED_IMAGE_MIME_TYPES:
                raise UnsupportedMediaTypeError(declared_mime)

        now = datetime.now(timezone.utc)
        media_id = uuid.uuid4().hex
        target_dir = self.originals_dir / now.strftime("%Y") / now.strftime("%m")
        target = target_dir / "{0}{1}".format(media_id, _MIME_EXTENSIONS[detected_mime])

        with self._lock:
            if len(body) > self.budget_bytes:
                raise MediaBudgetExceededError(len(body), self.budget_bytes)
            self._evict_until_free_space(len(body))
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                self._write_atomically(target, body)
            except OSError as exc:
                raise MediaStorageError("Could not store media file: {0}".format(target)) from exc

        return StoredMedia(
            media_id=media_id,
            path=target,
            relative_path=target.relative_to(self.root).as_posix(),
            source_url=source_url,
            downloaded_url=downloaded_url or source_url,
            mime_type=detected_mime,
            byte_size=len(body),
            downloaded_at=now,
            declared_content_type=declared_mime,
            metadata=dict(metadata or {}),
        )

    def storage_usage_bytes(self) -> int:
        """Return the current size of managed original binaries in bytes."""

        with self._lock:
            return sum(size for _, size, _ in self._managed_files())

    def evict_to_budget(self, budget_bytes: Optional[int] = None) -> List[EvictedMedia]:
        """Remove oldest managed binaries until their usage fits the budget.

        No database rows or caller-provided metadata are removed.  The return
        values allow the caller to retain an audit trail and update file
        availability in its own database.
        """

        if budget_bytes is None:
            budget_bytes = self.budget_bytes
        if budget_bytes < 0:
            raise ValueError("budget_bytes must not be negative")
        with self._lock:
            usage = self.storage_usage_bytes()
            evicted: List[EvictedMedia] = []
            for path, size, modified_at in self._managed_files():
                if usage <= budget_bytes:
                    break
                try:
                    path.unlink()
                except FileNotFoundError:
                    usage -= size
                    continue
                except OSError as exc:
                    raise MediaStorageError("Could not evict media file: {0}".format(path)) from exc
                usage -= size
                evicted.append(
                    EvictedMedia(
                        media_id=path.stem,
                        path=path,
                        relative_path=path.relative_to(self.root).as_posix(),
                        byte_size=size,
                        modified_at=datetime.fromtimestamp(modified_at, timezone.utc),
                    )
                )
                self._remove_empty_parents(path.parent)
            return evicted

    def _evict_until_free_space(self, incoming_bytes: int) -> None:
        """Make room for a new file without ever evicting that new file."""

        allowed_existing = self.budget_bytes - incoming_bytes
        if allowed_existing < 0:
            raise MediaBudgetExceededError(incoming_bytes, self.budget_bytes)
        self.evict_to_budget(allowed_existing)

    def _managed_files(self) -> List[Tuple[Path, int, float]]:
        """Return managed regular files in deterministic oldest-first order."""

        if not self.originals_dir.exists():
            return []
        files: List[Tuple[Path, int, float]] = []
        try:
            candidates: Iterable[Path] = self.originals_dir.rglob("*")
            for path in candidates:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    files.append((path, info.st_size, info.st_mtime))
        except OSError as exc:
            raise MediaStorageError("Could not inspect media directory: {0}".format(self.originals_dir)) from exc
        files.sort(key=lambda item: (item[2], item[0].as_posix()))
        return files

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self.originals_dir:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @staticmethod
    def _write_atomically(destination: Path, body: bytes) -> None:
        temporary_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=str(destination.parent), prefix=".media-", suffix=".part"
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(body)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, str(destination))
            temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _require_httpx() -> None:
        if httpx is None:
            raise MediaDependencyError("httpx is required to download remote media")

    @staticmethod
    def _url_host_and_port(url: str) -> Tuple[str, int]:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise UnsafeMediaURLError(url, "malformed URL") from exc
        if parsed.scheme.lower() not in ("http", "https"):
            raise UnsafeMediaURLError(url, "only http and https URLs are allowed")
        if not parsed.hostname:
            raise UnsafeMediaURLError(url, "a host is required")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeMediaURLError(url, "URLs with credentials are not allowed")
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return parsed.hostname, port

    @staticmethod
    def _is_safe_address(address: str) -> bool:
        try:
            return ipaddress.ip_address(address).is_global
        except ValueError:
            return False

    def _validate_url_sync(self, url: str) -> None:
        host, port = self._url_host_and_port(url)
        if self.allow_private_hosts:
            return
        try:
            direct_address = ipaddress.ip_address(host)
        except ValueError:
            direct_address = None
        if direct_address is not None:
            if not direct_address.is_global:
                raise UnsafeMediaURLError(url, "private, loopback, or reserved address")
            return
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise MediaDownloadError(url, "could not resolve host") from exc
        addresses = [answer[4][0] for answer in answers]
        if not addresses:
            raise MediaDownloadError(url, "host resolved to no addresses")
        if any(not self._is_safe_address(address) for address in addresses):
            raise UnsafeMediaURLError(url, "host resolves to a non-public address")

    async def _validate_url_async(self, url: str) -> None:
        host, port = self._url_host_and_port(url)
        if self.allow_private_hosts:
            return
        try:
            direct_address = ipaddress.ip_address(host)
        except ValueError:
            direct_address = None
        if direct_address is not None:
            if not direct_address.is_global:
                raise UnsafeMediaURLError(url, "private, loopback, or reserved address")
            return
        try:
            loop = asyncio.get_running_loop()
            answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise MediaDownloadError(url, "could not resolve host") from exc
        addresses = [answer[4][0] for answer in answers]
        if not addresses:
            raise MediaDownloadError(url, "host resolved to no addresses")
        if any(not self._is_safe_address(address) for address in addresses):
            raise UnsafeMediaURLError(url, "host resolves to a non-public address")

    async def _download_async(self, client: Any, source_url: str) -> Tuple[bytes, Optional[str], str]:
        current_url = source_url
        for redirect_count in range(self.max_redirects + 1):
            await self._validate_url_async(current_url)
            try:
                async with client.stream(
                    "GET",
                    current_url,
                    headers={"Accept": "image/avif,image/webp,image/*;q=0.8,*/*;q=0.1"},
                    follow_redirects=False,
                ) as response:
                    redirect = self._redirect_target(current_url, response.status_code, response.headers)
                    if redirect is not None:
                        if redirect_count >= self.max_redirects:
                            raise MediaDownloadError(source_url, "too many redirects")
                        current_url = redirect
                        continue
                    self._raise_for_bad_status(current_url, response.status_code)
                    self._check_declared_size(response.headers.get("content-length"))
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_image_bytes:
                            raise MediaTooLargeError(self.max_image_bytes, len(body))
                    return bytes(body), response.headers.get("content-type"), current_url
            except MediaError:
                raise
            except httpx.HTTPError as exc:
                raise MediaDownloadError(current_url, str(exc)) from exc
        raise MediaDownloadError(source_url, "too many redirects")

    def _download_sync(self, client: Any, source_url: str) -> Tuple[bytes, Optional[str], str]:
        current_url = source_url
        for redirect_count in range(self.max_redirects + 1):
            self._validate_url_sync(current_url)
            try:
                with client.stream(
                    "GET",
                    current_url,
                    headers={"Accept": "image/avif,image/webp,image/*;q=0.8,*/*;q=0.1"},
                    follow_redirects=False,
                ) as response:
                    redirect = self._redirect_target(current_url, response.status_code, response.headers)
                    if redirect is not None:
                        if redirect_count >= self.max_redirects:
                            raise MediaDownloadError(source_url, "too many redirects")
                        current_url = redirect
                        continue
                    self._raise_for_bad_status(current_url, response.status_code)
                    self._check_declared_size(response.headers.get("content-length"))
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_image_bytes:
                            raise MediaTooLargeError(self.max_image_bytes, len(body))
                    return bytes(body), response.headers.get("content-type"), current_url
            except MediaError:
                raise
            except httpx.HTTPError as exc:
                raise MediaDownloadError(current_url, str(exc)) from exc
        raise MediaDownloadError(source_url, "too many redirects")

    @staticmethod
    def _redirect_target(current_url: str, status_code: int, headers: Any) -> Optional[str]:
        if status_code not in (301, 302, 303, 307, 308):
            return None
        location = headers.get("location")
        if not location:
            raise MediaDownloadError(current_url, "redirect without a Location header", status_code)
        return urljoin(current_url, location)

    @staticmethod
    def _raise_for_bad_status(url: str, status_code: int) -> None:
        if status_code < 200 or status_code >= 300:
            raise MediaDownloadError(url, "HTTP {0}".format(status_code), status_code)

    def _check_declared_size(self, content_length: Optional[str]) -> None:
        if not content_length:
            return
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            return
        if declared_size > self.max_image_bytes:
            raise MediaTooLargeError(self.max_image_bytes, declared_size)


__all__ = [
    "DEFAULT_DOWNLOAD_TIMEOUT_SECONDS",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_MEDIA_BUDGET_BYTES",
    "EvictedMedia",
    "MediaBudgetExceededError",
    "MediaDependencyError",
    "MediaDownloadError",
    "MediaError",
    "MediaStorageError",
    "MediaStore",
    "MediaTooLargeError",
    "SUPPORTED_IMAGE_MIME_TYPES",
    "StoredMedia",
    "UnsafeMediaURLError",
    "UnsupportedMediaTypeError",
    "detect_image_file_mime",
    "detect_image_mime",
    "encode_image_as_data_uri",
    "image_file_to_data_uri",
]
