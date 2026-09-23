from pathlib import Path

from robin.provision import PINNED_IMAGE, setup_script

ROOT = Path(__file__).resolve().parents[1]


def test_pinned_image_contains_robin_and_does_not_start_until_enroll() -> None:
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text()
    unit = (ROOT / "deploy" / "robin.service").read_text()
    ignored = (ROOT / ".dockerignore").read_text()
    script = setup_script("abc123token", "https://house.example")

    assert "\nFROM ghcr.io/boldsoftware/exeuntu:" in dockerfile
    assert "exe.dev/install-shelley=false" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "--extra ner" not in dockerfile
    assert "systemctl enable" not in dockerfile
    assert "shelley.socket" in dockerfile
    assert "ssh.service" in dockerfile
    assert "robin.service" in dockerfile
    assert "enroll.token" not in dockerfile
    assert "COPY src" in dockerfile

    assert "EXPOSE 8000" in dockerfile
    assert "ExecStart=/usr/local/bin/robin boot --host 127.0.0.1 --port 8000" in unit
    assert "After=network-online.target exe-setup.service" in unit

    timer = (ROOT / "deploy" / "robin-tick.service").read_text()
    house = (ROOT / "deploy" / "robin-house.service").read_text()
    assert "robin tick" in timer
    assert "/var/lib/robin/house.sqlite" in timer
    assert "/var/lib/robin/house.sqlite" in house
    assert "/etc/robin/store.key" in house
    assert "--host 0.0.0.0" in house
    assert "--port 8000" not in house
    assert "--advertise-file /etc/robin/advertise.url" in house
    assert "0.0.0.0" not in unit
    assert "enroll.token" not in timer
    assert "OnUnitActiveSec=15min" in (ROOT / "deploy" / "robin-tick.timer").read_text()
    assert "robin-tick.timer" in dockerfile
    assert "sudo systemctl enable --now robin robin-tick.timer" in script
    assert "/etc/robin/enroll.token" in script
    assert PINNED_IMAGE == "nimul/robin:pinned"
    assert ".env" in ignored
    assert "tests" in ignored
