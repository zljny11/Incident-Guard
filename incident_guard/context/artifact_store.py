from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class StoredToolResult:
    context_content: str
    sha256: str
    byte_size: int
    reference: str | None = None

    @property
    def externalized(self) -> bool:
        return self.reference is not None


class FileToolResultStore:
    """Content-addressed storage for tool results too large for model context."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        threshold_bytes: int = 16_384,
        preview_chars: int = 2_000,
    ) -> None:
        if type(threshold_bytes) is not int or threshold_bytes < 1:
            raise ValueError("threshold_bytes must be a positive int")
        if type(preview_chars) is not int or preview_chars < 1:
            raise ValueError("preview_chars must be a positive int")
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.threshold_bytes = threshold_bytes
        self.preview_chars = preview_chars

    def store(self, content: str) -> StoredToolResult:
        if not isinstance(content, str):
            raise ValueError("tool result content must be a string")
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) <= self.threshold_bytes:
            return StoredToolResult(content, digest, len(encoded))

        reference = f"sha256/{digest}.txt"
        target = self._resolve(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{digest}.", suffix=".tmp"
            )
            try:
                with os.fdopen(file_descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise

        preview = content[: self.preview_chars]
        context_content = (
            f"{preview}\n\n[tool result truncated; bytes={len(encoded)}; "
            f"sha256={digest}; ref={reference}]"
        )
        return StoredToolResult(
            context_content=context_content,
            sha256=digest,
            byte_size=len(encoded),
            reference=reference,
        )

    def load(self, reference: str, *, expected_sha256: str | None = None) -> str:
        path = self._resolve(reference)
        try:
            encoded = path.read_bytes()
        except FileNotFoundError as error:
            raise FileNotFoundError(f"tool result artifact not found: {reference}") from error
        digest = hashlib.sha256(encoded).hexdigest()
        reference_digest = path.stem
        if digest != reference_digest:
            raise ValueError("tool result artifact hash does not match its reference")
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("tool result artifact hash verification failed")
        return encoded.decode("utf-8")

    def _resolve(self, reference: str) -> Path:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("artifact reference must be non-empty")
        pure = PurePosixPath(reference)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("artifact reference must be relative and contained")
        path = self.base_dir.joinpath(*pure.parts)
        resolved_base = self.base_dir.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_base):
            raise ValueError("artifact reference escapes base directory")
        return resolved_path
