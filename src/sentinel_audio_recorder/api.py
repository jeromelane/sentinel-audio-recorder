import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from sentinel_audio_recorder.config import load_config

router = APIRouter()
RECORDINGS_DIR: Path | None = None


def _get_recordings_dir(request: Request) -> Path:
    """Return the recordings directory, ensuring it exists and matches app config.

    The module-level ``RECORDINGS_DIR`` can be monkeypatched for tests while the
    FastAPI app state remains the single source of truth during runtime.
    """

    recordings_dir = getattr(request.app.state, "recordings_dir", None)
    if recordings_dir is not None:
        return recordings_dir

    config = getattr(request.app.state, "config", None)
    base_dir = RECORDINGS_DIR

    if base_dir is None:
        config = config or load_config()
        base_dir = Path(config.recording_dir)

    recordings_dir = Path(base_dir).resolve()
    recordings_dir.mkdir(parents=True, exist_ok=True)

    request.app.state.recordings_dir = recordings_dir
    if config is not None:
        request.app.state.config = config

    return recordings_dir


@router.get("/")
def root():
    return {"message": "Sentinel audio recorder API is running"}


@router.get("/list-recordings")
def list_recordings(request: Request):
    recordings_dir = _get_recordings_dir(request)
    files = sorted(recordings_dir.glob("*.wav"), key=os.path.getmtime, reverse=True)
    return {"recordings": [f.name for f in files]}


@router.get("/download-last")
def download_last(request: Request):
    recordings_dir = _get_recordings_dir(request)
    files = sorted(recordings_dir.glob("*.wav"), key=os.path.getmtime, reverse=True)
    if not files:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": "No recordings found"},
        )

    latest = files[0]
    return FileResponse(path=latest, filename=latest.name, media_type="audio/wav")


@router.get("/download/{filename}")
def download_file(filename: str, request: Request):
    recordings_dir = _get_recordings_dir(request)
    file_path = recordings_dir / filename

    if not file_path.is_file() or not file_path.suffix == ".wav":
        raise HTTPException(status_code=404, detail="Recording not found or invalid file type")

    return FileResponse(path=file_path, filename=file_path.name, media_type="audio/wav")
