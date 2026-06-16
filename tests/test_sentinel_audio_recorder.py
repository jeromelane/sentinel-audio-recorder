import pytest
from pathlib import Path
from sentinel_audio_recorder import run_api
from sentinel_audio_recorder import api
from sentinel_audio_recorder.env import load_env_file
import requests
from sentinel_audio_recorder.recorder import AudioReadTimeout, Recorder
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig
import tempfile
import time
import os
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone


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
        monkeypatch.setattr(api, "UPLOADER", None)
        api.clear_list_recordings_cache()
        yield temp_path  # Let the test use the path
        api.clear_list_recordings_cache()

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


def test_list_recordings_uses_short_cache(temp_recordings_dir, monkeypatch):
    monkeypatch.setenv("SENTINEL_LIST_RECORDINGS_CACHE_SECONDS", "60")
    first = temp_recordings_dir / "test1.wav"
    second = temp_recordings_dir / "test2.wav"
    first.write_bytes(b"RIFF....WAVEfmt ")

    first_response = api.list_recordings()
    second.write_bytes(b"RIFF....WAVEfmt ")
    cached_response = api.list_recordings()

    assert first_response == cached_response
    assert [item["filename"] for item in cached_response["recordings"]] == ["test1.wav"]

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


def test_env_bool_parses_disabled_value(monkeypatch):
    recorder = Recorder.__new__(Recorder)
    monkeypatch.setenv("SENTINEL_AUDIO_DIAGNOSTICS", "0")

    assert recorder._env_bool("SENTINEL_AUDIO_DIAGNOSTICS", True) is False


def test_background_trigger_settings_from_env(monkeypatch):
    monkeypatch.setenv("SENTINEL_TRIGGER_THRESHOLD", "3000")
    monkeypatch.setenv("SENTINEL_TRIGGER_SILENCE_TIMEOUT", "30")

    assert run_api._trigger_threshold() == 3000
    assert run_api._trigger_silence_timeout() == 30


def test_capture_frames_raises_after_repeated_audio_timeouts():
    recorder = Recorder.__new__(Recorder)
    recorder.RATE = 48000
    recorder.CHUNK = 1024
    recorder.max_read_timeouts = 2
    recorder._stream_read_with_timeout = lambda stream: None

    with pytest.raises(AudioReadTimeout):
        recorder._capture_frames(object(), 1)


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
        upload_enabled=True,
        recordings_dir=temp_path,
        min_age_seconds=0,
        retry_base_seconds=1,
        retry_max_seconds=2,
        state_db=temp_path / ".upload_state.sqlite",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_upload_url_does_not_enable_automatic_upload_by_default(monkeypatch):
    monkeypatch.setenv("SENTINEL_UPLOAD_URL", "http://example.test/ingest-audio/")
    monkeypatch.delenv("SENTINEL_UPLOAD_ENABLED", raising=False)

    config = UploadConfig.from_env()

    assert config.configured is True
    assert config.upload_enabled is False
    assert config.enabled is False


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
    assert uploader.is_retained(wav)


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


def test_upload_stops_after_max_attempts(temp_recordings_dir):
    wav = temp_recordings_dir / "recording.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("server down")

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir, max_attempts=1),
        request_post=fake_post,
    )

    uploader.run_once()
    uploader.run_once()

    assert len(calls) == 1
    assert wav.exists()


def test_upload_pauses_scan_when_endpoint_times_out(temp_recordings_dir):
    first = temp_recordings_dir / "first.wav"
    second = temp_recordings_dir / "second.wav"
    first.write_bytes(b"RIFF....WAVEfmt ")
    second.write_bytes(b"RIFF....WAVEfmt ")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.exceptions.ConnectTimeout("server timed out")

    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir),
        request_post=fake_post,
    )

    result = uploader.run_once()

    assert result["uploaded"] == 0
    assert len(calls) == 1


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
            uploaded_retention_days=-1,
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
    assert cleanup["blocked"] is True
    assert wav.exists()


def test_cleanup_runs_while_upload_is_disabled(temp_recordings_dir):
    small = temp_recordings_dir / "small.wav"
    small.write_bytes(b"x")
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url="http://example.test/ingest-audio/",
            upload_enabled=False,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )

    result = uploader.run_once()

    assert result["uploaded"] == 0
    assert result["cleanup"]["triggered"] is True
    assert not small.exists()


def test_retained_uploaded_file_is_preserved_during_cleanup(temp_recordings_dir):
    wav = temp_recordings_dir / "retained.wav"
    wav.write_bytes(b"x")
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )
    uploader._record_success(wav, "abc")

    cleanup = uploader.cleanup_storage()

    assert wav.exists()
    assert cleanup["deleted"] == []
    assert cleanup["blocked"] is True


def test_retained_small_file_preserved_while_non_retained_is_deleted(temp_recordings_dir):
    retained = temp_recordings_dir / "retained.wav"
    removable = temp_recordings_dir / "removable.wav"
    retained.write_bytes(b"x")
    removable.write_bytes(b"x")
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )
    uploader.mark_served(retained)

    cleanup = uploader.cleanup_storage()

    assert retained.exists()
    assert not removable.exists()
    assert cleanup["deleted"][0]["filename"] == "removable.wav"


def test_download_marks_file_served_and_retained(temp_recordings_dir):
    wav = temp_recordings_dir / "served.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")
    uploader = RecordingUploader(
        config=uploader_config(temp_recordings_dir, upload_url=None)
    )
    api.set_uploader(uploader)

    response = api.download_file("served.wav")

    assert response.status_code == 200
    assert uploader.is_retained(wav)
    with sqlite3.connect(uploader.config.db_path) as conn:
        row = conn.execute(
            "SELECT last_served_at, retained_until FROM uploads WHERE filename = ?",
            (wav.name,),
        ).fetchone()
    assert row[0] is not None
    assert row[1] is not None


def test_expired_retention_allows_cleanup(temp_recordings_dir):
    wav = temp_recordings_dir / "expired.wav"
    wav.write_bytes(b"x")
    DiskUsage = namedtuple("usage", "total used free")

    uploader = RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            small_recording_bytes=10,
        ),
        disk_usage=lambda path: DiskUsage(total=100, used=90, free=10),
    )
    old_retention = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat().replace("+00:00", "Z")
    uploader.mark_served(wav)
    with sqlite3.connect(uploader.config.db_path) as conn:
        conn.execute(
            "UPDATE uploads SET retained_until = ? WHERE filename = ?",
            (old_retention, wav.name),
        )

    cleanup = uploader.cleanup_storage()

    assert not wav.exists()
    assert cleanup["deleted"][0]["filename"] == wav.name


def test_existing_upload_database_is_migrated_for_retention_columns(temp_recordings_dir):
    state_db = temp_recordings_dir / ".upload_state.sqlite"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            CREATE TABLE uploads (
                filename TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                sha256 TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                uploaded_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

    RecordingUploader(
        config=uploader_config(
            temp_recordings_dir,
            upload_url=None,
            state_db=state_db,
        )
    )

    with sqlite3.connect(state_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(uploads)")}

    assert "retained_until" in columns
    assert "last_served_at" in columns


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
