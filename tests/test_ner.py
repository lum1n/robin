import threading
import time

from robin.ner import GlinerNer


def test_load_failure_marks_ner_unavailable(monkeypatch) -> None:
    ner = GlinerNer()
    assert not ner.available()

    def boom() -> object:
        raise RuntimeError("weights missing")

    monkeypatch.setattr(ner, "_load", boom)
    ner._warming = False
    ner._warm()
    assert not ner.available()
    assert ner.detect("Jane Doe called") == ()


def test_detect_does_not_block_while_cold() -> None:
    ner = GlinerNer()
    assert not ner.available()
    assert ner.detect("hello") == ()


def test_warm_loads_in_background(monkeypatch) -> None:
    ner = GlinerNer()
    ready = threading.Event()

    class Fake:
        def predict_entities(self, text, labels, threshold=0.5):
            return [{"start": 0, "end": 4, "label": "person"}]

    def load() -> object:
        time.sleep(0.05)
        ner._model = Fake()
        ready.set()
        return ner._model

    monkeypatch.setattr(ner, "_load", load)
    ner.warm()
    assert ner.detect("Jane") == ()
    assert ready.wait(1)
    # Wait for warm thread to publish availability.
    for _ in range(20):
        if ner.available():
            break
        time.sleep(0.01)
    assert ner.available()
    assert ner.detect("Jane")[0].label == "PERSON"
