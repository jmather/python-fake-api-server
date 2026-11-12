"""The fake-api-server console script."""

import socket
import subprocess
import sys
import threading
import time

import httpx

from fake_api_server.cli import resolve_port


def test_resolve_port_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert resolve_port([]) == 3000
    assert resolve_port([], {"PORT": "4242"}) == 4242
    assert resolve_port(["--port", "5151"], {"PORT": "4242"}) == 5151


def test_cli_writes_json_and_xml_when_uvicorn_configures_logging(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    xml_path = tmp_path / "requests.xml"
    env = {key: value for key, value in __import__("os").environ.items()}
    env["PORT"] = str(port)
    env["FAKEAPI_LOG_CAPTURE"] = "all"
    env["FAKEAPI_LOG_XML_PATH"] = str(xml_path)
    env.pop("FAKEAPI_LOG_HOOK", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "fake_api_server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout: list[str] = []
    stderr: list[str] = []

    def _read(pipe, sink):
        for line in pipe:
            sink.append(line)

    threading.Thread(target=_read, args=(proc.stdout, stdout), daemon=True).start()
    threading.Thread(target=_read, args=(proc.stderr, stderr), daemon=True).start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not any("Listening at" in line for line in stdout):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert any("Listening at" in line for line in stdout), "".join(stdout + stderr)
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/",
                headers={"X-FakeApi-Action": "register"},
                json={"external_id": "cli-log"},
            )
            assert response.status_code == 201, response.text
        deadline = time.time() + 5
        while time.time() < deadline and not xml_path.exists():
            time.sleep(0.05)
        text = xml_path.read_text(encoding="utf-8")
        assert "<request-log>" in text
        assert "<server>python</server>" in text
        assert any(line.startswith("{") and '"server":"python"' in line for line in stdout)
        assert not any("Logging error" in line for line in stderr)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_cli_registers_records_and_plays_back():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = dict(**{key: value for key, value in __import__("os").environ.items()})
    env["PORT"] = str(port)
    env["FAKEAPI_LOG_CAPTURE"] = "none"
    env.pop("FAKEAPI_LOG_XML_PATH", None)
    env.pop("FAKEAPI_LOG_HOOK", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "fake_api_server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout: list[str] = []
    stderr: list[str] = []

    def _read(pipe, sink):
        for line in pipe:
            sink.append(line)

    threading.Thread(target=_read, args=(proc.stdout, stdout), daemon=True).start()
    threading.Thread(target=_read, args=(proc.stderr, stderr), daemon=True).start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not any("Listening at" in line for line in stdout):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert any("Listening at" in line for line in stdout), "".join(stdout + stderr)

        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=5, trust_env=False) as client:
            registered = client.post(
                base + "/",
                headers={"X-FakeApi-Action": "register"},
                json={"external_id": "cli"},
            )
            assert registered.status_code == 201, registered.text
            token = registered.json()["auth_token"]
            auth = {"Authorization": f"Basic {token}"}
            recorded = client.put(
                base + "/",
                headers={**auth, "X-FakeApi-Action": "record"},
                json={
                    "method": "get",
                    "path": "/cli-hello",
                    "responses": [
                        {"status": 200, "content": "from the cli", "content_type": "text/plain"}
                    ],
                },
            )
            assert recorded.status_code == 201, recorded.text
            played = client.get(base + "/cli-hello", headers=auth)
            assert played.status_code == 200
            assert played.text == "from the cli"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
