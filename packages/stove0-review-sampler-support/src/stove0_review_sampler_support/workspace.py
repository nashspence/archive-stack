"""Strict sampler view over a review-target-owned shared workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path

from stove0_review_sampler_protocol import SamplerInput, SamplerRequest


class SamplerWorkspace:
    def __init__(self, root: Path, request: SamplerRequest) -> None:
        base = root.resolve()
        if root.is_symlink() or not base.is_dir():
            raise ValueError("sampler workspace root must be a real directory")
        self.root = base
        self.request = request
        self.job_root = self._resolve_job_root(request.workspace_id)

    def _resolve_job_root(self, workspace_id: str) -> Path:
        path = self.root / workspace_id
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.root:
            raise ValueError("sampler job workspace is not an assigned real directory")
        return path.resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = self.job_root.joinpath(*relative_path.split("/"))
        current = self.job_root
        for part in relative_path.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError("sampler workspace path traverses a symlink")
            if not current.exists():
                break
        parent = candidate.parent.resolve()
        if parent != self.job_root and self.job_root not in parent.parents:
            raise ValueError("sampler workspace path escapes the assigned job")
        return candidate

    def verify_input(self, declared: SamplerInput) -> Path:
        path = self.resolve(declared.path)
        if not path.is_file() or path.stat().st_size != declared.bytes:
            raise ValueError(f"sampler input identity differs: {declared.id}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024**2):
                digest.update(chunk)
        if digest.hexdigest() != declared.sha256:
            raise ValueError(f"sampler input digest differs: {declared.id}")
        return path

    def output(self, relative_path: str) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def canceled(self) -> bool:
        return self.resolve(self.request.cancellation_path).exists()


__all__ = ["SamplerWorkspace"]
