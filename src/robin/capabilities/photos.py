"""Photo search of pictures this user owns on this machine. The picture stays here."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Protocol

from robin.capabilities.files import IMAGE_SUFFIXES, Workspace
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool
from robin.store import HouseholdStore

_PHOTO_WORD = re.compile(r"\b(?:photos?|pictures?|images?|photographs?)\b", re.IGNORECASE)
_SUBJECT = re.compile(
    r"\b(?:with|of|containing|showing)\s+(?:(?:a|an|the|all|some)\s+)?(.+)",
    re.IGNORECASE,
)
_STRIP = re.compile(
    r"\b(?:find|search|show|list|all|my|the|some|me|photos?|pictures?|images?|photographs?|please|for)\b",
    re.IGNORECASE,
)
_NEEDS_MODEL = "Photo search needs a local model. Run uv sync --extra photos in the Robin environment, then try again."
_MISSING = object()


class Embedder(Protocol):
    def embed_text(self, text: str) -> list[float]: ...

    def embed_image(self, path: Path) -> list[float]: ...


class Photos(Capability):
    id = "photos"
    tools = [
        Tool(
            name="search_photos",
            description="Find photos this user owns on this machine that match a description, such as dogs.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
    ]
    fields = [FieldSpec("name", FieldClass.ORDINARY)]

    def __init__(
        self,
        workspace: Workspace,
        embedder: Embedder | None = None,
        *,
        store: HouseholdStore | None = None,
        threshold: float = 0.24,
    ) -> None:
        self.workspace = workspace
        self.embedder = embedder
        self.store = store
        self.threshold = threshold
        self._lock = threading.Lock()
        self._loaded: Embedder | None | object = _MISSING
        self._index: dict[str, dict[str, dict]] = {}
        if store is not None:
            for account_id, rows in store.load_photo_index().items():
                self._index[account_id] = {str(row["path"]): {"stamp": row["stamp"], "vector": row["vector"]} for row in rows}

    def offered_tools(self, account_id: str, task: str) -> list[Tool]:
        if _PHOTO_WORD.search(task):
            return list(self.tools)
        return []

    def prepare(self, account_id: str, task: str) -> str:
        query = _photo_query(task)
        if query is None:
            return ""
        if not query:
            return self._list(account_id)
        return self._search(account_id, query)

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "search_photos":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return self._list(account_id)
            return self._search(account_id, query)
        raise NotImplementedError(tool_name)

    def _list(self, account_id: str) -> str:
        names = [name for name, _path in self._images(account_id)]
        if not names:
            return "No photos owned by this user."
        return "Photos owned by this user:\n" + "\n".join(names)

    def _search(self, account_id: str, query: str) -> str:
        images = self._images(account_id)
        if not images:
            return "No photos owned by this user."
        embedder = self._embedder()
        if embedder is None:
            return _NEEDS_MODEL
        try:
            wanted = embedder.embed_text(f"a photo of {query}")
        except Exception as exc:
            return _vision_failure(exc)
        scored: list[tuple[float, str]] = []
        for name, path in images:
            vector = self._remember(account_id, name, path, embedder)
            if not vector:
                continue
            score = _cosine(wanted, vector)
            if score >= self.threshold:
                scored.append((score, name))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return f"No photos matched {query}."
        shown = scored[:30]
        lines = "\n".join(name for _score, name in shown)
        if len(scored) > len(shown):
            return f"Photos matching {query}:\n{lines}\n{len(shown)} of {len(scored)}"
        return f"Photos matching {query}:\n{lines}"

    def _images(self, account_id: str) -> list[tuple[str, Path]]:
        return [(str(path), path) for path in self.workspace.owned_files(account_id, suffixes=IMAGE_SUFFIXES)]

    def _embedder(self) -> Embedder | None:
        if self.embedder is not None:
            return self.embedder
        if self._loaded is _MISSING:
            self._loaded = _local_embedder()
        if self._loaded is None or self._loaded is _MISSING:
            return None
        return self._loaded  # type: ignore[return-value]

    def _remember(self, account_id: str, name: str, path: Path, embedder: Embedder) -> list[float] | None:
        stamp = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
        with self._lock:
            cached = self._index.get(account_id, {}).get(name)
            if cached and cached["stamp"] == stamp:
                return list(cached["vector"])
        try:
            vector = [float(value) for value in embedder.embed_image(path)]
        except Exception:
            return None
        if not vector:
            return None
        with self._lock:
            self._index.setdefault(account_id, {})[name] = {"stamp": stamp, "vector": vector}
            self._drop_missing(account_id)
            self._save(account_id)
        return vector

    def _drop_missing(self, account_id: str) -> None:
        present = {name for name, _path in self._images(account_id)}
        kept = {name: row for name, row in self._index.get(account_id, {}).items() if name in present}
        self._index[account_id] = kept

    def _save(self, account_id: str) -> None:
        if self.store is None:
            return
        rows = [
            {"path": name, "stamp": row["stamp"], "vector": row["vector"]}
            for name, row in sorted(self._index.get(account_id, {}).items())
        ]
        self.store.save_photo_index(account_id, rows)


class ClipEmbedder:
    """Local CLIP. Image bytes stay in this process."""

    def __init__(self, model: Any, preprocess: Any, tokenizer: Any) -> None:
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer

    @classmethod
    def load(cls) -> ClipEmbedder:
        import open_clip

        model, _preprocess_train, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
        model.eval()
        return cls(model, preprocess, open_clip.get_tokenizer("ViT-B-32"))

    def embed_image(self, path: Path) -> list[float]:
        import torch
        from PIL import Image

        image = self._preprocess(Image.open(path).convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            features = self._model.encode_image(image)
            features = features / features.norm(dim=-1, keepdim=True)
        return [float(value) for value in features[0].tolist()]

    def embed_text(self, text: str) -> list[float]:
        import torch

        tokens = self._tokenizer([text])
        with torch.no_grad():
            features = self._model.encode_text(tokens)
            features = features / features.norm(dim=-1, keepdim=True)
        return [float(value) for value in features[0].tolist()]


def _local_embedder() -> Embedder | None:
    try:
        return ClipEmbedder.load()
    except ImportError:
        return None


def _photo_query(task: str) -> str | None:
    if not _PHOTO_WORD.search(task):
        return None
    match = _SUBJECT.search(task)
    if match:
        query = re.sub(r"\b(?:in them|in it|please|for me)\b", " ", match.group(1), flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", query).strip(" .?")
    return re.sub(r"\s+", " ", _STRIP.sub(" ", task)).strip(" .?")


def _cosine(left: list[float], right: list[float]) -> float:
    pairs = list(zip(left, right))
    if not pairs:
        return 0.0
    dot = sum(x * y for x, y in pairs)
    left_norm = sum(x * x for x, _y in pairs) ** 0.5
    right_norm = sum(y * y for _x, y in pairs) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _vision_failure(exc: Exception) -> str:
    line = str(exc).splitlines()[0].strip()
    return f"could not search photos. {line[:180]}" if line else "could not search photos."
