import threading
import time

from robin.ner import GlinerNer, spurious_person


def test_spurious_person_drops_identity_asks_and_pronouns() -> None:
    assert spurious_person("who are you?", 0, 12)
    assert spurious_person("Who are you?", 0, 12)
    assert spurious_person("hvem er du?", 0, 11)
    assert spurious_person("No, I was asking", 4, 5)
    assert spurious_person("ask you later", 4, 7)
    assert spurious_person("Hello, Robin", 7, 12)
    assert not spurious_person("Jane Doe called", 0, 8)
    assert not spurious_person("Vegard was here", 0, 6)


def test_detect_filters_identity_question_as_person(monkeypatch) -> None:
    ner = GlinerNer()

    class Fake:
        def predict_entities(self, text, labels, threshold=0.5):
            return [{"start": 0, "end": len(text.rstrip("?")), "label": "person"}]

    ner._model = Fake()
    ner._failed = False
    assert ner.detect("who are you?") == ()
    assert ner.detect("Jane Doe")[0].label == "PERSON"


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
