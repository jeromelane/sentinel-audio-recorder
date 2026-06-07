import os
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

@router.get("/")
def root():
    return {"message": "Sentinel audio recorder API is running"}

@router.get("/list-recordings")
def list_recordings():
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=os.path.getmtime, reverse=True)
    return {"recordings": [f.name for f in files]}


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

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="audio/wav"
    )


@router.get("/sync-status")
def sync_status():
    uploader = UPLOADER
    if uploader is None:
        config = UploadConfig.from_env()
        config.recordings_dir = RECORDINGS_DIR
        uploader = RecordingUploader(config=config)
    return uploader.status()
