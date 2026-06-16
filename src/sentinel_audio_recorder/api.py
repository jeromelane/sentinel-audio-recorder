import os
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

router = APIRouter()
logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"
UPLOADER = None
_LIST_RECORDINGS_CACHE = {"expires_at": 0.0, "data": None}
_LIST_RECORDINGS_LOCK = threading.Lock()


def set_uploader(uploader):
    global UPLOADER
    UPLOADER = uploader


def get_uploader():
    uploader = UPLOADER
    if uploader is None:
        config = UploadConfig.from_env()
        config.recordings_dir = RECORDINGS_DIR
        uploader = RecordingUploader(config=config)
    return uploader


def mark_recording_served(path: Path):
    get_uploader().mark_served(path)


def _env_float(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using %s", name, value, default)
        return default


def _list_recordings_cache_seconds():
    return max(0.0, _env_float("SENTINEL_LIST_RECORDINGS_CACHE_SECONDS", 2.0))


def clear_list_recordings_cache():
    with _LIST_RECORDINGS_LOCK:
        _LIST_RECORDINGS_CACHE["expires_at"] = 0.0
        _LIST_RECORDINGS_CACHE["data"] = None


@router.get("/")
def root():
    return {"message": "Sentinel audio recorder API is running"}

def recording_metadata(path: Path):
    stat = path.stat()
    return {
        "filename": path.name,
        "size": stat.st_size,
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@router.get("/list-recordings")
def list_recordings():
    cache_seconds = _list_recordings_cache_seconds()
    now = time.monotonic()

    with _LIST_RECORDINGS_LOCK:
        cached = _LIST_RECORDINGS_CACHE["data"]
        if cached is not None and now < _LIST_RECORDINGS_CACHE["expires_at"]:
            return cached

        started_at = time.monotonic()
        files = sorted(RECORDINGS_DIR.glob("*.wav"), key=os.path.getmtime, reverse=True)
        response = {"recordings": [recording_metadata(path) for path in files]}
        elapsed = time.monotonic() - started_at

        logger.info(
            "Listed %d recordings in %.3fs (cache_ttl=%ss)",
            len(response["recordings"]),
            elapsed,
            cache_seconds,
        )
        if elapsed >= 1.0:
            logger.warning(
                "Slow /list-recordings response: %.3fs for %d files",
                elapsed,
                len(response["recordings"]),
            )

        _LIST_RECORDINGS_CACHE["data"] = response
        _LIST_RECORDINGS_CACHE["expires_at"] = now + cache_seconds
        return response


@router.get("/download-last")
def download_last():
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=os.path.getmtime, reverse=True)
    if not files:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "message": "No recordings found"
            }
        )
    
    latest = files[0]
    mark_recording_served(latest)
    return FileResponse(
        path=latest,
        filename=latest.name,
        media_type="audio/wav"
    )


@router.get("/download/{filename}")
def download_file(filename: str):
    file_path = RECORDINGS_DIR / filename

    # Ensure the requested file is a WAV file in the recordings directory
    if not file_path.is_file() or not file_path.suffix == ".wav":
        raise HTTPException(
            status_code=404,
            detail="Recording not found or invalid file type"
        )

    mark_recording_served(file_path)
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="audio/wav"
    )


@router.get("/sync-status")
def sync_status():
    return get_uploader().status()
