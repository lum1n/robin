"""Optional local recall pass. The deterministic airlock stays the guarantee."""

from __future__ import annotations

import threading
import warnings
from typing import Protocol

from robin.airlock import Entity

DEFAULT_MODEL = "urchade/gliner_multi_pii-v1"
NORWEGIAN_MODEL = "SovereignSystems-cc/sosa-pii-ner-no-v1.0.0"

_LABELS = {
    "person": "PERSON",
    "organization": "ORG",
    "address": "ADDRESS",
    "email": "EMAIL",
    "email address": "EMAIL",
    "phone number": "PHONE",
    "mobile phone number": "PHONE",
}

_CACHE_LIMIT = 64


class Ner(Protocol):
    def available(self) -> bool: ...

    def detect(self, text: str) -> tuple[Entity, ...]: ...


class UnavailableNer:
    def available(self) -> bool:
        return False

    def detect(self, text: str) -> tuple[Entity, ...]:
        return ()


class GlinerNer:
    """Loads weights in the background. detect() never blocks on that load."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model: object | None = None
        self._failed = False
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[Entity, ...]] = {}
        self._warming = False
        try:
            import importlib.util

            self._installed = importlib.util.find_spec("gliner") is not None
        except Exception:
            self._installed = False

    def available(self) -> bool:
        """True only when weights are loaded. Installed-but-cold is not available."""
        return self._installed and not self._failed and self._model is not None

    def warm(self) -> None:
        """Start loading weights on a daemon thread. Safe to call more than once."""
        if not self._installed or self._failed or self._model is not None:
            return
        with self._lock:
            if self._warming or self._model is not None or self._failed:
                return
            self._warming = True
        thread = threading.Thread(target=self._warm, name="robin-ner", daemon=True)
        thread.start()

    def detect(self, text: str) -> tuple[Entity, ...]:
        if not text or not self._installed or self._failed:
            return ()
        if self._model is None:
            self.warm()
            return ()
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        try:
            model = self._model
            labels = list(_LABELS)
            found = model.predict_entities(text, labels, threshold=0.5)  # type: ignore[attr-defined]
        except Exception:
            self._failed = True
            return ()
        entities: list[Entity] = []
        for item in found:
            label = _LABELS.get(str(item.get("label", "")).lower())
            if label is None:
                continue
            entities.append(Entity(int(item["start"]), int(item["end"]), label))
        result = tuple(entities)
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[text] = result
        return result

    def _warm(self) -> None:
        try:
            self._load()
        except Exception:
            self._failed = True
        finally:
            self._warming = False

    def _load(self) -> object:
        with self._lock:
            if self._model is not None:
                return self._model
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*torch\.jit\.script is not supported.*",
                )
                warnings.filterwarnings(
                    "ignore",
                    message=r".*resume_download.*",
                )
                from gliner import GLiNER

                self._model = GLiNER.from_pretrained(self.model_name)
            return self._model
