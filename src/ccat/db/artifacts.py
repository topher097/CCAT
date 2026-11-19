"""Private artifact storage (plan §6.3, §5.2).

Generated files (match CSVs, PDFs, exports) live under one
server-derived artifact root — ``cfg.storage.artifact_root`` — and are
registered in the ``artifacts`` table by server-relative path.  The
future download route resolves an artifact id back to a physical file
only when the resolved path stays inside the canonical root (no
symlink escape) and serves a safe content type.
"""

import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ccat.db.errors import DbStoreError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ccat.config import Config
    from ccat.db.repositories import SeasonStoreRepository

#: Filename suffixes served with an explicit content type; everything
#: else downloads as ``application/octet-stream`` (§6.3).
SAFE_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".html": "text/html",
}


class ArtifactError(DbStoreError):
    """An artifact path escapes the canonical root or is unusable."""


def artifact_root(cfg: "Config") -> Path:
    """Return the configured private artifact root directory.

    Args:
        cfg: Pipeline configuration (``storage.artifact_root``).

    Returns:
        The root path; not created on disk by this function.
    """
    return Path(cfg.storage.artifact_root)


def content_type_for(filename: str) -> str:
    """Map one filename to its safe download content type.

    Args:
        filename: The artifact's download filename.

    Returns:
        The MIME type; unknown suffixes fall back to
        ``application/octet-stream``.
    """
    return SAFE_CONTENT_TYPES.get(
        Path(filename).suffix.lower(), "application/octet-stream"
    )


def _sha256(path: Path) -> str:
    """Hash one file's content.

    Args:
        path: The file to hash.

    Returns:
        The hex sha256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def register_file(
    repo: "SeasonStoreRepository",
    kind: str,
    path: Path,
    actor: str | None = None,
    meta: "Mapping[str, Any] | None" = None,
    *,
    root: Path | None = None,
) -> int:
    """Register one generated file as an artifact row (§6.3).

    The file is hashed, and when ``root`` is given a file stored
    outside it is moved into ``<root>/<kind>/`` first; paths already
    inside the root are accepted in place.  The registered ``path``
    column holds the root-relative location (or the verbatim path when
    no root is supplied).

    Args:
        repo: Season repository used to insert the artifact row.
        kind: Artifact kind (csv_export / child_sheet / ...).
        path: The physical file, which must exist.
        actor: Creating user/job identity.
        meta: Optional JSON metadata (e.g. the owning run id).
        root: Optional artifact root to relocate/link the file into.

    Returns:
        The new artifact id.

    Raises:
        ArtifactError: When the file does not exist.
    """
    source = Path(path)
    if not source.is_file():
        raise ArtifactError(f"artifact file does not exist: {source}")
    stored = source.resolve()
    if root is not None:
        canonical = Path(root).resolve()
        if not stored.is_relative_to(canonical):
            target_dir = canonical / kind
            target_dir.mkdir(parents=True, exist_ok=True)
            moved = target_dir / source.name
            shutil.move(str(stored), str(moved))
            stored = moved.resolve()
        relative = Path(stored).relative_to(canonical)
    else:
        relative = stored
    filename = source.name
    return await repo.register_artifact(
        kind,
        str(relative),
        filename,
        content_type_for(filename),
        stored.stat().st_size,
        _sha256(stored),
        actor,
        meta=dict(meta) if meta else None,
    )


async def resolve_download(
    repo: "SeasonStoreRepository", artifact_id: int, cfg: "Config"
) -> tuple[Path, str, str]:
    """Resolve one artifact id to a contained physical file (§5.2).

    Args:
        repo: Season repository to read the artifact metadata from.
        artifact_id: The artifact id.
        cfg: Pipeline configuration supplying the artifact root.

    Returns:
        A tuple of (physical path, content type, download filename).

    Raises:
        ArtifactError: When the artifact is unknown, its file is
            missing, or the resolved path escapes the canonical root
            (symlink escape, §6.3).
    """
    row = await repo.artifact(artifact_id)
    if row is None:
        raise ArtifactError(f"unknown artifact {artifact_id}")
    root = artifact_root(cfg).resolve()
    stored = row.get("path") or ""
    target = (root / stored).resolve()
    if not target.is_relative_to(root):
        raise ArtifactError(
            f"artifact {artifact_id} path escapes the artifact root"
        )
    if not target.is_file():
        raise ArtifactError(f"artifact {artifact_id} file is missing")
    filename = str(row.get("filename") or target.name)
    return target, content_type_for(filename), filename


__all__ = [
    "SAFE_CONTENT_TYPES",
    "ArtifactError",
    "artifact_root",
    "content_type_for",
    "register_file",
    "resolve_download",
]
