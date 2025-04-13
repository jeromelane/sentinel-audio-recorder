from fastapi import FastAPI
from fastapi import APIRouter

from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
import os

router = APIRouter()

RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"

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
