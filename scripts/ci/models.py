"""Immutable public model inputs for the maintained regression gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Model:
    name: str
    repository: str
    revision: str


MODELS = (
    Model("embedding", "BAAI/bge-small-en-v1.5", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"),
    Model("reranker", "cross-encoder/ms-marco-MiniLM-L6-v2", "233902d25c440f23af6f7d6e94d2946bac0bee0a"),
)


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare(root: Path) -> None:
    from huggingface_hub import snapshot_download

    for model in MODELS:
        destination = root / model.name / model.revision
        snapshot_download(
            repo_id=model.repository,
            revision=model.revision,
            local_dir=destination,
            allow_patterns=["*.json", "*.txt", "*.safetensors"],
            ignore_patterns=["onnx/*", "openvino/*"],
            token=False,
        )
        files = {
            str(path.relative_to(destination)): file_hash(path)
            for path in sorted(destination.rglob("*"))
            if path.is_file() and ".cache" not in path.relative_to(destination).parts
        }
        (root / f"{model.name}.json").write_text(
            json.dumps({"repository": model.repository, "revision": model.revision, "files": files}) + "\n"
        )
    verify(root)


def verify(root: Path) -> None:
    for model in MODELS:
        destination = root / model.name / model.revision
        manifest = json.loads((root / f"{model.name}.json").read_text())
        if manifest["repository"] != model.repository or manifest["revision"] != model.revision:
            raise RuntimeError(f"Wrong prepared {model.name} revision; run ./bin/ci setup")
        required = {"config.json", "model.safetensors", "tokenizer_config.json"}
        if model.name == "embedding":
            required.update({"modules.json", "1_Pooling/config.json"})
        if not required.issubset(manifest["files"]):
            raise RuntimeError(f"Incomplete {model.name} snapshot; run ./bin/ci setup")
        for name, expected in manifest["files"].items():
            path = destination / name
            if not path.resolve().is_relative_to(destination.resolve()) or file_hash(path) != expected:
                raise RuntimeError(f"Modified {model.name} model file: {name}")
