import os
import pwd
import subprocess

from robin.capabilities import identity
from robin.capabilities.files import Files, Workspace
from robin.capabilities.identity import claim, login_name
from robin.capabilities.shell import ShellBox, Terminal
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.policy import Task
from robin.session import Assistant

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_a_browser_profile_is_not_listed_with_the_account_files(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    cache = tmp_path / "ada" / "browser" / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "data_0").write_text("pixels")
    note = tmp_path / "ada" / "notes"
    note.mkdir()
    (note / "today.txt").write_text("hi")
    assert workspace.names("ada") == ["notes/today.txt"]


def test_files_stay_in_the_account_and_delete_waits(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    assistant = Assistant()
    assistant.add(Files(workspace))
    wrote = assistant.invoke("ada", "files", "files_write", {"path": "notes/today.txt", "text": f"buy milk {SECRET}"})
    assert wrote["status"] == "done"
    assert (tmp_path / "ada" / "notes" / "today.txt").read_text().startswith("buy milk")
    ada = assistant.decide(Task("ada", "files", "look"))
    bea = assistant.decide(Task("bea", "files", "look"))
    assert "notes/today.txt" in ada.local_text
    assert SECRET not in ada.local_text
    assert "notes/today.txt" not in bea.local_text
    assert workspace.read("ada", "notes/today.txt").startswith("buy milk")
    try:
        workspace.write("ada", "../bea/secret.txt", "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("a path left the account")
    assert not (tmp_path / "bea" / "secret.txt").exists()
    held = assistant.invoke("ada", "files", "files_delete", {"path": "notes/today.txt"})
    assert held["status"] == "confirm"
    assert (tmp_path / "ada" / "notes" / "today.txt").exists()
    done = assistant.invoke("ada", "files", "files_delete", {"path": "notes/today.txt"}, confirmed=True)
    assert done["result"] == "deleted"
    assert not (tmp_path / "ada" / "notes" / "today.txt").exists()


def test_a_command_waits_and_the_prompt_shows_it_without_the_secret(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def runner(command: str, work, login: str) -> str:
        calls.append((command, str(work), login))
        return f"ran {command}"

    assistant = Assistant()
    assistant.add(Terminal(ShellBox(tmp_path, runner)))

    class Scripted:
        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            return ModelTurn("", (ToolCall("shell_run", {"command": f"echo {SECRET}"}),))

    reply = converse(assistant, Task("ada", "desk", "run it"), Scripted())
    assert reply.status == "confirm"
    assert reply.tool == "shell_run"
    assert SECRET not in reply.text
    assert "echo" in reply.text
    assert calls == []
    done = assistant.invoke("ada", "shell", "shell_run", {"command": "echo hello"}, confirmed=True)
    assert done["status"] == "done"
    assert calls == [("echo hello", str((tmp_path / "ada").resolve()), "robin-ada")]
    log = assistant.activity.read("ada")
    assert SECRET not in str(log)


def test_a_command_runs_as_that_account_and_keeps_the_process_environment(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def fake_spawn(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(identity, "spawn", fake_spawn)
    monkeypatch.setenv("ROBIN_STORE_KEY", SECRET)
    assert ShellBox(tmp_path).run("ada", "echo hello") == "ok"
    work = str((tmp_path / "ada").resolve())
    assert captured["argv"] == [
        "runuser",
        "--preserve-environment",
        "-u",
        "robin-ada",
        "--",
        "sh",
        "-c",
        "echo hello",
    ]
    assert captured["cwd"] == (tmp_path / "ada").resolve()
    assert captured["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": work,
        "TMPDIR": work,
        "USER": "robin-ada",
        "LOGNAME": "robin-ada",
    }
    assert (tmp_path / "ada").stat().st_mode & 0o777 == 0o700
    assert login_name("bea") == "robin-bea"
    long_id = "a" * 27
    assert login_name(long_id) != f"robin-{long_id}"
    assert len(login_name(long_id)) == 32


def test_root_gives_the_directory_to_that_login(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    calls: list[list[str]] = []

    def fake_spawn(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(identity, "spawn", fake_spawn)

    class Owner:
        pw_uid = 2001
        pw_gid = 2001

    lookups = {"n": 0}

    def getpwnam(name: str):
        lookups["n"] += 1
        if lookups["n"] == 1:
            raise KeyError(name)
        return Owner()

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    chowned: list[tuple[str, int]] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((str(path), uid)))
    root = tmp_path / "files"
    work = root / "ada"
    assert claim("ada", root, work) == "robin-ada"
    assert calls[0][0] == "useradd"
    assert "/usr/sbin/nologin" in calls[0]
    assert calls[0][-1] == "robin-ada"
    assert str(work.resolve()) in calls[0]
    assert chowned[-1] == (str(work.resolve()), 2001)
    assert work.stat().st_mode & 0o777 == 0o700
    assert root.stat().st_mode & 0o777 == 0o711
    failed = subprocess.CompletedProcess(["useradd"], 1, stdout="", stderr=SECRET)

    def reject(argv, **kwargs):
        return failed

    monkeypatch.setattr(identity, "spawn", reject)

    def missing(name: str) -> pwd.struct_passwd:
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", missing)
    try:
        claim("bea", root, root / "bea")
    except RuntimeError as exc:
        assert SECRET not in str(exc)
    else:
        raise AssertionError("a failed login was ignored")


def test_this_user_can_read_their_files_and_not_another_users(tmp_path) -> None:
    me = 1000
    other = 1001
    robin_uid = 2000
    volume = tmp_path / "machine"
    root = volume / "files"
    home = volume / "home" / "vegard"
    theirs = volume / "home" / "other"
    owners: dict[Path, int] = {}

    def place(path: Path, uid: int, text: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is not None:
            path.write_text(text)
        else:
            path.mkdir(parents=True, exist_ok=True)
        owners[path.resolve()] = uid

    for path in (volume, volume / "home", volume / "etc", root):
        place(path, 0)
    place(home, me)
    place(theirs, other)
    place(home / "dog.jpg", me, "pixels")
    place(home / "notes.txt", me, "buy milk")
    place(home / "robin.key", me, SECRET)
    place(home / "link.txt", me, "nope")
    (home / "link.txt").unlink()
    (home / "link.txt").symlink_to(theirs / "secret.txt")
    place(theirs / "secret.txt", other, SECRET)
    place(theirs / "planted.jpg", me, "hidden pixels")
    place(volume / "etc" / "passwd", 0, "root:x:0:0")
    place(root / "ada" / "kept.txt", robin_uid, "kept")
    place(root / "bea" / "hidden.txt", me, SECRET)

    def owner(path: Path) -> int:
        return owners.get(path.resolve(), 0)

    workspace = Workspace(root, volume=volume, owner=owner, user=me)
    found = {path.name for path in workspace.owned_files("ada")}
    assert found == {"dog.jpg", "notes.txt", "kept.txt"}
    assert workspace.read("ada", str(home / "notes.txt")) == "buy milk"
    assert workspace.read("ada", str(theirs / "secret.txt")) == "path belongs to another user"
    assert workspace.read("ada", str(theirs / "planted.jpg")) == "path belongs to another user"
    assert workspace.read("ada", str(volume / "etc" / "passwd")) == "path belongs to another user"
    assert workspace.read("ada", str(home / "robin.key")) == "path belongs to another user"
    assert workspace.read("ada", str(home / "dog.jpg")).startswith("photo ")
    assert SECRET not in workspace.read("ada", str(home / "dog.jpg"))
    try:
        workspace.write("ada", "../bea/secret.txt", "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("a path left the account")
    assert not (root / "bea" / "secret.txt").exists()


def test_root_without_a_person_does_not_read_personal_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("ROBIN_USER", raising=False)
    volume = tmp_path / "machine"
    root = volume / "files"
    (volume / "home").mkdir(parents=True)
    (volume / "home" / "note.txt").write_text(SECRET)
    (root / "ada").mkdir(parents=True)
    (root / "ada" / "mine.txt").write_text("mine")
    workspace = Workspace(root, volume=volume)
    assert [path.name for path in workspace.owned_files("ada")] == ["mine.txt"]
    assert workspace.read("ada", str(volume / "home" / "note.txt")) == "path belongs to another user"


def test_a_file_question_answers_from_this_users_files(tmp_path) -> None:
    me = 1000
    other = 1001
    volume = tmp_path / "machine"
    root = volume / "files"
    home = volume / "home" / "vegard"
    theirs = volume / "home" / "other"
    owners: dict[Path, int] = {}

    def place(path: Path, uid: int, text: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text(text)
        owners[path.resolve()] = uid

    for path in (volume, volume / "home", root):
        place(path, 0)
    place(home, me)
    place(theirs, other)
    place(home / "dog.jpg", me, "pixels")
    place(theirs / "secret.txt", other, SECRET)

    workspace = Workspace(root, volume=volume, owner=lambda path: owners.get(path.resolve(), 0), user=me)
    assistant = Assistant()
    assistant.add(Files(workspace))

    class Scripted:
        def __init__(self) -> None:
            self.user = ""
            self.tools: list[str] = []

        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            self.user = str(messages)
            self.tools = [tool["name"] for tool in tools]
            return ModelTurn("Here are the files.")

    model = Scripted()
    reply = converse(assistant, Task("ada", "desk", "what files do I have"), model)
    assert reply.text == "Here are the files."
    assert "files_list" in model.tools
    assert SECRET not in model.user
    read = converse(
        assistant,
        Task("ada", "desk", f"read {home / 'dog.jpg'}"),
        Scripted(),
    )
    assert read.text == "Here are the files."
    assert SECRET not in read.text
