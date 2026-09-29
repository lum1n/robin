from pathlib import Path

from robin.capabilities.files import Workspace
from robin.capabilities.photos import Photos, _local_embedder
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
_NEEDS = "Photo search needs a local model."


class Names:
    def __init__(self) -> None:
        self.images = 0

    def embed_text(self, text: str) -> list[float]:
        if "dog" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]

    def embed_image(self, path: Path) -> list[float]:
        self.images += 1
        if "dog" in path.name.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


class Scripted:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.tools: list[str] = []

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.messages = messages
        self.tools = [tool["name"] for tool in tools]
        if any(tool["name"] == "photos_search" for tool in tools) and not any(
            message.get("role") == "tool" for message in messages
        ):
            return ModelTurn("", (ToolCall("photos_search", {"query": "dogs"}),))
        return ModelTurn("Here they are.")


def _photo(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(SECRET.encode())


def test_a_sentence_finds_photos_of_dogs_and_hides_the_other_account(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    _photo(tmp_path / "ada", "park-dog.jpg")
    _photo(tmp_path / "ada", "cat.png")
    _photo(tmp_path / "ada", "notes.txt")
    _photo(tmp_path / "bea", "bea-dog.jpg")
    photos = Photos(workspace, Names(), threshold=0.5)
    assistant = Assistant(ner=StubNer())
    assistant.add(photos)
    model = Scripted()
    reply = converse(assistant, Task("ada", "home", "find all photos with dogs in them"), model)
    assert reply.text == "Here they are."
    assert "photos_search" in model.tools
    blob = str(model.messages)
    assert "bea-dog.jpg" not in blob
    assert SECRET not in blob
    assert "photos_search" in {tool["name"] for tool in assistant.tools("ada")}
    assert photos.records("bea") == []


def test_a_second_search_reuses_the_saved_vectors(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    workspace = Workspace(tmp_path / "files")
    _photo(tmp_path / "files" / "ada", "dog.jpg")
    embedder = Names()
    first = Photos(workspace, embedder, store=store, threshold=0.5)
    assert "dog.jpg" in first.invoke("ada", "photos_search", {"query": "dogs"})
    assert embedder.images == 1
    store.close()
    revived = Photos(workspace, Names(), store=HouseholdStore(path, key), threshold=0.5)
    assert "dog.jpg" in revived.invoke("ada", "photos_search", {"query": "dog"})
    assert revived.embedder.images == 0


def test_photo_search_without_a_local_model_says_so(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("robin.capabilities.photos._local_embedder", lambda: None)
    workspace = Workspace(tmp_path)
    _photo(tmp_path / "ada", "dog.jpg")
    photos = Photos(workspace)
    assert _NEEDS in photos.invoke("ada", "photos_search", {"query": "dogs"})
    listed = photos.invoke("ada", "photos_search", {"query": ""})
    assert listed.startswith("Photos owned by this user:\n")
    assert listed.endswith("dog.jpg")


def test_photo_search_uses_this_users_pictures_and_skips_another_user(tmp_path) -> None:
    me = 1000
    other = 1001
    volume = tmp_path / "machine"
    root = volume / "files"
    home = volume / "home" / "vegard"
    theirs = volume / "home" / "other"
    owners: dict[Path, int] = {}

    def place(path: Path, uid: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix:
            path.write_bytes(SECRET.encode())
        else:
            path.mkdir(parents=True, exist_ok=True)
        owners[path.resolve()] = uid

    for path in (volume, volume / "home", root):
        place(path, 0)
    place(home, me)
    place(theirs, other)
    place(home / "park-dog.jpg", me)
    place(theirs / "other-dog.jpg", other)
    place(root / "bea" / "bea-dog.jpg", me)
    workspace = Workspace(root, volume=volume, owner=lambda path: owners.get(path.resolve(), 0), user=me)
    photos = Photos(workspace, Names(), threshold=0.5)
    found = photos.invoke("ada", "photos_search", {"query": "dogs"})
    assert "park-dog.jpg" in found
    assert "other-dog.jpg" not in found
    assert "bea-dog.jpg" not in found
    assert SECRET not in found


def test_the_local_embedder_is_absent_until_the_extra_is_installed() -> None:
    try:
        import open_clip  # noqa: F401
    except ImportError:
        assert _local_embedder() is None
