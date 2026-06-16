# sentinel-audio-recorder 🎙️

**sentinel-audio-recorder** is a Raspberry Pi-based audio recording system that captures audio from a USB device on boot. It provides a simple CLI to start recordings, exposes a small API, and can continuously upload completed recordings to a remote analyser backend for backup and processing.

---

## 🚀 Features

- 🎤 Record audio from a USB interface (e.g., UCA222)
- 🐍 Python-based with modern `pyproject.toml` project structure
- 📦 Automatic virtual environment and dependency setup
- 🔁 Records on boot using `systemd`
- 🧪 Easy-to-use CLI for starting/stopping recordings
- 🌐 Optionally uploads completed recordings to a remote HTTP analyser backend
- 💾 Tracks upload state in a small local SQLite ledger
- 🧹 Cleans local storage when disk usage reaches the configured limit

---

## 🔧 Setup Instructions

Run this on your Raspberry Pi or any other suitable device:

```bash
git clone https://github.com/jeromelane/sentinel-audio-recorder.git
cd sentinel-audio-recorder
./setup.sh
```

To test api for remote services:

```bash
curl http://localhost:8000/
```

To test command line recording:

```bash
sentinel-audio-recorder start --duration 60

```

To manually sync recordings to a remote analyser backend:

```bash
export SENTINEL_UPLOAD_URL="http://SERVER:8080/ingest-audio/"
sentinel-audio-recorder sync --once --url "$SENTINEL_UPLOAD_URL"
```

Use continuous manual sync to keep uploading and cleaning storage:

```bash
sentinel-audio-recorder sync --watch --url "$SENTINEL_UPLOAD_URL"
```

---

## 🖥️ CLI Usage

```bash
sentinel-audio-recorder start

# Record in a 10-min rolling loop
sentinel-audio-recorder start --duration 600 --loop

# Noise-activated recording
sentinel-audio-recorder start --trigger

# Triggered recording with custom silence timeout
sentinel-audio-recorder start --trigger --threshold 500 --silence-timeout 10

# Run one cleanup-only maintenance pass
sentinel-audio-recorder sync --once

# Continuously upload and clean local cache
sentinel-audio-recorder sync --watch --url http://SERVER:8080/ingest-audio/

```

Recordings are saved in `recordings/` and can be downloaded remotely while they are still present locally.

---

## 🔁 Upload Sync

Maintenance scans `recordings/*.wav` from the filesystem, then uses a local SQLite ledger at `recordings/.upload_state.sqlite` to track upload, retry, retention, and cleanup state.

Each sync pass:

1. Finds completed `.wav` files in `recordings/`.
2. Uploads eligible files only when automatic upload is explicitly enabled.
3. Marks successful uploads as `uploaded`.
4. Records failed uploads and retries them later with backoff.
5. Checks local disk usage.
6. Cleans local files only if storage is at or above the configured high-watermark.

Automatic upload is **off by default**. Set both `SENTINEL_UPLOAD_ENABLED=1` and `SENTINEL_UPLOAD_URL` to let the background maintenance loop upload recordings. The command-line `sync --url ...` path is treated as an explicit manual upload request.

Uploaded or API-served files are **not deleted immediately**. They receive a local retention window, default `30` days, and remain protected from cleanup until that window expires.

### Storage Cleanup

Cleanup starts when disk usage reaches `SENTINEL_STORAGE_HIGH_WATERMARK`, default `80`.

When cleanup runs:

1. Delete local WAV files smaller than `SENTINEL_SMALL_RECORDING_BYTES`, default `3984588` bytes, about `3.8 MiB`.
2. Skip any file still inside its retention window.
3. If disk usage is still above the high-watermark, delete the oldest local WAV files that have already been uploaded and are no longer retained.
4. Larger unuploaded recordings are preserved.

Deleted file history is kept in the SQLite ledger for `SENTINEL_DELETED_RETENTION_DAYS`, default `30`.

### Configuration

`setup.sh` creates a local `.env` file from `.env.default` if `.env` does not already exist. Edit `.env` with real values for your deployment. The installed systemd service loads this file automatically.

```bash
# URL alone does not enable automatic background upload
SENTINEL_UPLOAD_URL=http://SERVER:8080/ingest-audio/
SENTINEL_UPLOAD_ENABLED=0

# Optional bearer token
SENTINEL_UPLOAD_TOKEN=token

# Optional tuning
SENTINEL_RECORDINGS_DIR=recordings
SENTINEL_AUDIO_CARD_INDEX=
SENTINEL_AUDIO_SAMPLE_RATE=
SENTINEL_AUDIO_CHANNELS=
SENTINEL_AUDIO_READ_TIMEOUT=5
SENTINEL_AUDIO_MAX_READ_TIMEOUTS=3
SENTINEL_RECORDER_RETRY_SECONDS=30
SENTINEL_RECORDER_MAX_RETRIES=5
SENTINEL_UPLOAD_INTERVAL=30
SENTINEL_UPLOAD_TIMEOUT=60
SENTINEL_UPLOAD_MAX_ATTEMPTS=5
SENTINEL_UPLOAD_MIN_AGE=10
SENTINEL_UPLOAD_RETRY_BASE=30
SENTINEL_UPLOAD_MAX_BACKOFF=3600
SENTINEL_STORAGE_HIGH_WATERMARK=80
SENTINEL_SMALL_RECORDING_BYTES=3984588
SENTINEL_DELETED_RETENTION_DAYS=30
SENTINEL_UPLOADED_RETENTION_DAYS=30
```

---

## 🌐 API

```bash
curl http://localhost:8000/
curl http://localhost:8000/list-recordings
curl http://localhost:8000/download-last
curl http://localhost:8000/sync-status
```

`/sync-status` reports upload configuration, filesystem WAV counts, retained files, ledger counts, disk usage, last upload error, and recent cleanup actions.
