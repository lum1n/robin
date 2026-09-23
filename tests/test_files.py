from robin.capabilities.files import Files, Workspace
from robin.capabilities.shell import ShellBox, Terminal
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.policy import Task
from robin.session import Assistant

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_files_stay_in_the_account_and_delete_waits(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    assistant = Assistant()
    assistant.add(Files(workspace))
    wrote = assistant.invoke("ada", "files", "write_file", {"path": "notes/today.txt", "text": f"buy milk {SECRET}"})
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
    held = assistant.invoke("ada", "files", "delete_file", {"path": "notes/today.txt"})
    assert held["status"] == "confirm"
    assert (tmp_path / "ada" / "notes" / "today.txt").exists()
    done = assistant.invoke("ada", "files", "delete_file", {"path": "notes/today.txt"}, confirmed=True)
    assert done["result"] == "deleted"
    assert not (tmp_path / "ada" / "notes" / "today.txt").exists()


def test_a_command_waits_and_the_prompt_shows_it_without_the_secret(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def runner(command: str, work) -> str:
        calls.append((command, str(work)))
        return f"ran {command}"

    assistant = Assistant()
    assistant.add(Terminal(ShellBox(tmp_path, runner)))

    class Scripted:
        def complete(self, *, system: str, user: str, tools: list[dict]) -> ModelTurn:
            return ModelTurn("", (ToolCall("run_command", {"command": f"echo {SECRET}"}),))

    reply = converse(assistant, Task("ada", "desk", "run it"), Scripted())
    assert reply.status == "confirm"
    assert reply.tool == "run_command"
    assert SECRET not in reply.text
    assert "echo" in reply.text
    assert calls == []
    done = assistant.invoke("ada", "shell", "run_command", {"command": "echo hello"}, confirmed=True)
    assert done["status"] == "done"
    assert calls == [("echo hello", str((tmp_path / "ada").resolve()))]
    log = assistant.activity.read("ada")
    assert SECRET not in str(log)
