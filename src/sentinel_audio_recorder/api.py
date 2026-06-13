import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

router = APIRouter()

RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"
UPLOADER = None


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
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=os.path.getmtime, reverse=True)
    return {"recordings": [recording_metadata(path) for path in files]}


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
