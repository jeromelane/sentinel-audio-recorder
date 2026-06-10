import pytest
from pathlib import Path
from sentinel_audio_recorder import api
from sentinel_audio_recorder.env import load_env_file
from sentinel_audio_recorder.recorder import Recorder
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig
import tempfile
import time
import os
import sqlite3
from collections import namedtuple


class FakePyAudio:
    def __init__(self, device_info, supported_formats):
        self.device_info = device_info
        self.supported_formats = supported_formats

    def get_device_info_by_index(self, index):
        return self.device_info

    def is_format_supported(self, rate, input_device, input_channels, input_format):
        if (rate, input_channels) not in self.supported_formats:
            raise ValueError("unsupported")
        return True


@pytest.fixture
def temp_recordings_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        monkeypatch.setattr(api, "RECORDINGS_DIR", temp_path)
        yield temp_path  # Let the test use the path

def test_root():
    response = api.root()
    assert "message" in response

def test_list_recordings():
    response = api.list_recordings()
    assert "recordings" in response

def test_list_recordings_with_fake_files(temp_recordings_dir):
    # Create fake .wav files
    first = temp_recordings_dir / "test1.wav"
    second = temp_recordings_dir / "test2.wav"
    first.write_bytes(b"RIFF....WAVEfmt ")
    second.write_bytes(b"RIFF....WAVEfmt more-data")

    data = api.list_recordings()
    recordings = sorted(data["recordings"], key=lambda item: item["filename"])
    assert [item["filename"] for item in recordings] == ["test1.wav", "test2.wav"]
    assert recordings[0]["size"] == first.stat().st_size
    assert recordings[0]["size_bytes"] == first.stat().st_size
    assert recordings[0]["created_at"].endswith("Z")
    assert recordings[0]["modified_at"].endswith("Z")

def test_download_last_with_fake_file(temp_recordings_dir):
    # Create one fake .wav file
    test_file = temp_recordings_dir / "latest.wav"
    test_file.write_bytes(b"RIFF....WAVEfmt ")

    response = api.download_last()
    assert response.status_code == 200
    assert response.media_type == "audio/wav"


def test_detect_audio_settings_prefers_device_default_rate():
    recorder = Recorder.__new__(Recorder)
    recorder.p = FakePyAudio(
        {"maxInputChannels": 2, "defaultSampleRate": 96000.0},
        {(96000, 2)},
    )

    rate, channels = recorder._detect_audio_settings(1)

    assert rate == 96000
    assert channels == 2


def test_detect_audio_settings_falls_back_to_mono():
    recorder = Recorder.__new__(Recorder)
    recorder.p = FakePyAudio(
        {"maxInputChannels": 2, "defaultSampleRate": 48000.0},
        {(48000, 1)},
    )

    rate, channels = recorder._detect_audio_settings(1)

    assert rate == 48000
    assert channels == 1


def test_env_int_ignores_invalid_values(monkeypatch):
    recorder = Recorder.__new__(Recorder)
    monkeypatch.setenv("SENTINEL_AUDIO_CHANNELS", "nope")

    assert recorder._env_int("SENTINEL_AUDIO_CHANNELS", 2) == 2


def test_load_env_file_sets_missing_values_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SENTINEL_UPLOAD_URL=http://example.test/ingest-audio/\n"
        "EXISTING=value-from-file\n"
        "# ignored comment\n"
    )
    monkeypatch.delenv("SENTINEL_UPLOAD_URL", raising=False)
    monkeypatch.setenv("EXISTING", "already-set")

    load_env_file(env_file)

    assert os.environ["SENTINEL_UPLOAD_URL"] == "http://example.test/ingest-audio/"
    assert os.environ["EXISTING"] == "already-set"


class FakeResponse:
    status_code = 201
    text = ""


def uploader_config(temp_path, **overrides):
    config = UploadConfig(
        upload_url="http://example.test/ingest-audio/",
        recordings_dir=temp_path,
        min_age_seconds=0,
        retry_base_seconds=1,
        retry_max_seconds=2,
        state_db=temp_path / ".upload_state.sqlite",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_successful_upload_marks_uploaded_without_deleting(temp_recordings_dir):
    wav = temp_recordings_dir / "recording.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir),
        request_post=fake_post,
    )

    result = uploader.run_once()

    assert result["uploaded"] == 1
    assert wav.exists()
    assert uploader.is_uploaded(wav)
    assert calls[0][0][0] == "http://example.test/ingest-audio/"


def test_run_once_discovers_wav_without_existing_db_row(temp_recordings_dir):
    wav = temp_recordings_dir / "new-recording.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir, upload_url=None),
    )

    result = uploader.run_once()
    status = uploader.status()

    assert result["uploaded"] == 0
    assert wav.exists()
    assert status["pending"] == 1
    assert status["filesystem_wav_count"] == 1
    assert status["filesystem_unuploaded_wav_count"] == 1


def test_failed_upload_retries_without_deleting(temp_recordings_dir):
    wav = temp_recordings_dir / "recording.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")

    def fake_post(*args, **kwargs):
        raise RuntimeError("network down")

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir),
        request_post=fake_post,
    )

    result = uploader.run_once()
    status = uploader.status()

    assert result["uploaded"] == 0
    assert wav.exists()
    assert status["failed"] == 1
    assert status["last_error"]["message"] == "network down"


def test_cleanup_below_watermark_deletes_nothing(temp_recordings_dir):
    wav = temp_recordings_dir / "small.wav"
    wav.write_bytes(b"x")
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir, upload_url=None),
        disk_usage=lambda path: DiskUsage(total=100, used=79, free=21),
    )

    cleanup = uploader.cleanup_storage()

    assert cleanup["triggered"] is False
    assert wav.exists()


def test_cleanup_deletes_small_recordings_first(temp_recordings_dir):
    small = temp_recordings_dir / "small.wav"
    large = temp_recordings_dir / "large.wav"
    small.write_bytes(b"x")
    large.write_bytes(b"x" * 20)
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )

    cleanup = uploader.cleanup_storage()

    assert cleanup["triggered"] is True
    assert not small.exists()
    assert large.exists()
    assert cleanup["deleted"][0]["reason"] == "small_recording"


def test_cleanup_deletes_oldest_uploaded_after_small_files(temp_recordings_dir):
    first = temp_recordings_dir / "first.wav"
    second = temp_recordings_dir / "second.wav"
    first.write_bytes(b"x" * 20)
    second.write_bytes(b"x" * 20)
    now = time.time()
    first.touch()
    second.touch()
    first_mtime = now - 20
    second_mtime = now - 10
    os.utime(first, (first_mtime, first_mtime))
    os.utime(second, (second_mtime, second_mtime))

    DiskUsage = namedtuple("usage", "total used free")
    usage_values = iter([
        DiskUsage(total=100, used=90, free=10),
        DiskUsage(total=100, used=90, free=10),
        DiskUsage(total=100, used=70, free=30),
        DiskUsage(total=100, used=70, free=30),
    ])

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: next(usage_values),
    )
    uploader._record_success(first, "abc")
    uploader._record_success(second, "def")

    cleanup = uploader.cleanup_storage()

    assert cleanup["deleted"][0]["filename"] == "first.wav"
    assert cleanup["deleted"][0]["reason"] == "uploaded_cache"
    assert not first.exists()
    assert second.exists()


def test_cleanup_preserves_larger_unuploaded_recordings(temp_recordings_dir):
    wav = temp_recordings_dir / "important.wav"
    wav.write_bytes(b"x" * 20)
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )

    cleanup = uploader.run_once()["cleanup"]

    assert cleanup["triggered"] is True
    assert cleanup["deleted"] == []
    assert wav.exists()


def test_prune_deleted_history_after_retention_window(temp_recordings_dir):
    state_db = temp_recordings_dir / ".upload_state.sqlite"
    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            state_db=state_db,
            deleted_retention_days=30,
        ),
    )
    old_timestamp = "2026-01-01T00:00:00Z"
    recent_timestamp = uploader._now()

    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            INSERT INTO uploads (filename, size, mtime, status, updated_at)
            VALUES ('old.wav', 1, 1, 'deleted', ?)
            """,
            (old_timestamp,),
        )
        conn.execute(
            """
            INSERT INTO uploads (filename, size, mtime, status, updated_at)
            VALUES ('recent.wav', 1, 1, 'deleted', ?)
            """,
            (recent_timestamp,),
        )

    pruned = uploader.prune_deleted_history()

    with sqlite3.connect(state_db) as conn:
        rows = conn.execute(
            "SELECT filename FROM uploads WHERE status = 'deleted'"
        ).fetchall()

    assert pruned == 1
    assert rows == [("recent.wav",)]
