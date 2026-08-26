"""Deploy the embedded WebUI bundle to Home Assistant's config directory."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile

from ._webui_bundle import ASSET_NAMES, get_asset_bytes

_LOGGER = logging.getLogger(__name__)
_MARKER_FILENAME = ".version"


def prepare_webui_directory(destination: Path) -> None:
    """Create only the runtime WebUI directory if it does not exist."""
    destination.mkdir(parents=True, exist_ok=True)


def _read_marker(destination: Path) -> str | None:
    """Read the deployed WebUI version without touching any other data."""
    marker = destination / _MARKER_FILENAME
    try:
        return marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None


def webui_needs_deploy(destination: Path, version: str) -> bool:
    """Return True only when version changed or a required runtime file is missing."""
    if _read_marker(destination) != version:
        return True
    return any(not (destination / name).is_file() for name in ASSET_NAMES)


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace one generated WebUI file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def ensure_webui_assets(destination: Path, version: str) -> bool:
    """Deploy WebUI assets when required, without deleting user data.

    Normal startup performs only one tiny marker read plus file-existence checks.
    Asset decompression/writes happen only after an integration version change or
    when a required runtime WebUI file has been removed. The version marker is
    written last so an interrupted deployment is automatically retried later.
    """
    prepare_webui_directory(destination)
    if not webui_needs_deploy(destination, version):
        return False

    previous_version = _read_marker(destination)
    for name in ASSET_NAMES:
        _atomic_write(destination / name, get_asset_bytes(name))

    _atomic_write(destination / _MARKER_FILENAME, f"{version}\n".encode("utf-8"))
    if previous_version == version:
        _LOGGER.info("Restored missing EVN CSKH Monitor WebUI runtime files")
    else:
        _LOGGER.info(
            "Deployed EVN CSKH Monitor WebUI runtime version %s%s",
            version,
            f" (previous {previous_version})" if previous_version else "",
        )
    return True
