from pathlib import Path

from robin.capabilities.browser import Browser
from robin.capabilities.install import files_root, install
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


def test_files_sit_beside_the_household_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROBIN_FILES", raising=False)
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(store=store)
    assert files_root(assistant) == tmp_path / "files"
    install(assistant)
    assert (tmp_path / "files").is_dir()
    browser = next(cap for cap in assistant.registry._capabilities if isinstance(cap, Browser))
    assert browser.desk is not None
    assert browser.desk.profiles == tmp_path / "files"


def test_robin_files_env_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROBIN_FILES", str(tmp_path / "custom"))
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(store=store)
    assert files_root(assistant) == tmp_path / "custom"
