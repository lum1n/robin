"""Optional local recall pass. The deterministic airlock stays the guarantee."""

from __future__ import annotations

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


class Ner(Protocol):
    def available(self) -> bool: ...

    def detect(self, text: str) -> tuple[Entity, ...]: ...


class UnavailableNer:
    def available(self) -> bool:
        return False

    def detect(self, text: str) -> tuple[Entity, ...]:
        return ()


class GlinerNer:
    """Loads weights only when detect() is called. A missing install means unavailable."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model: object | None = None
        try:
            import gliner  # noqa: F401
        except ImportError:
            self._installed = False
        else:
            self._installed = True

    def available(self) -> bool:
        return self._installed

    def detect(self, text: str) -> tuple[Entity, ...]:
        if not self._installed:
            return ()
        labels = list(_LABELS)
        found = self._load().predict_entities(text, labels, threshold=0.5)  # type: ignore[attr-defined]
        entities: list[Entity] = []
        for item in found:
            label = _LABELS.get(str(item.get("label", "")).lower())
            if label is None:
                continue
            entities.append(Entity(int(item["start"]), int(item["end"]), label))
        return tuple(entities)

    def _load(self) -> object:
        if self._model is None:
            from gliner import GLiNER

            self._model = GLiNER.from_pretrained(self.model_name)
        return self._model
