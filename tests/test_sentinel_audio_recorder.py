import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from sentinel_audio_recorder.run_api import app
from sentinel_audio_recorder import api
import tempfile

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def temp_recordings_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr(api, "RECORDINGS_DIR", temp_path)
        yield temp_path  # Let the test use the path

def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()

def test_list_recordings(client):
    response = client.get("/list-recordings")
    assert response.status_code == 200
    assert "recordings" in response.json()

def test_list_recordings_with_fake_files(client, temp_recordings_dir):
    # Create fake .wav files
    (temp_recordings_dir / "test1.wav").write_bytes(b"RIFF....WAVEfmt ")
    (temp_recordings_dir / "test2.wav").write_bytes(b"RIFF....WAVEfmt ")

    response = client.get("/list-recordings")
    assert response.status_code == 200
    data = response.json()
    assert sorted(data["recordings"]) == ["test1.wav", "test2.wav"]

def test_download_last_with_fake_file(client, temp_recordings_dir):
    # Create one fake .wav file
    test_file = temp_recordings_dir / "latest.wav"
    test_file.write_bytes(b"RIFF....WAVEfmt ")

    response = client.get("/download-last")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")



