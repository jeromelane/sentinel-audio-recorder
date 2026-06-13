import threading
import os
import logging
import time
import signal
import sys

import uvicorn
from fastapi import FastAPI

from sentinel_audio_recorder import api
from sentinel_audio_recorder.api import router
from sentinel_audio_recorder.env import load_env_file
from sentinel_audio_recorder.recorder import Recorder, _shutdown_event
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

app = FastAPI()
app.include_router(router)
upload_thread = None
uploader = None
recorder_thread = None
logger = logging.getLogger(__name__)
load_env_file()


def _recorder_retry_seconds():
    value = os.getenv("SENTINEL_RECORDER_RETRY_SECONDS", "30")
    try:
        return max(1, int(value))
    except ValueError:
        logger.warning(
            "Invalid SENTINEL_RECORDER_RETRY_SECONDS=%r; using 30 seconds",
            value,
        )
        return 30


def shutdown_handler(signum, frame):
    """Handle graceful shutdown on SIGTERM/SIGINT"""
    logger.info(f"🛑 Received signal {signum}, initiating graceful shutdown...")
    _shutdown_event.set()
    if uploader:
        uploader.stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

@app.on_event("startup")
def start_background_recording():
    global upload_thread, uploader, recorder_thread

    config = UploadConfig.from_env()
    uploader = RecordingUploader(config=config)
    api.set_uploader(uploader)

    upload_thread = threading.Thread(target=uploader.run_forever, daemon=True)
    upload_thread.start()

    if os.getenv("SENTINEL_DISABLE_BACKGROUND_RECORDER") == "1":
        return

    def background_trigger():
        retry_seconds = _recorder_retry_seconds()
        max_retries = 5
        retry_count = 0
        
        while not _shutdown_event.is_set() and retry_count < max_retries:
            try:
                logger.info(f"🎙️ Starting background recorder (attempt {retry_count + 1}/{max_retries})...")
                recorder = Recorder(trigger=True)
                recorder.record()
                retry_count = 0  # Reset on successful start
            except Exception as e:
                retry_count += 1
                logger.exception(
                    "Background recorder failed (attempt %d/%d); retrying in %ss",
                    retry_count,
                    max_retries,
                    retry_seconds,
                )
                if retry_count < max_retries:
                    _shutdown_event.wait(retry_seconds)
                else:
                    logger.error("❌ Background recorder exceeded max retries. Stopping.")
                    break
            else:
                break

    recorder_thread = threading.Thread(target=background_trigger, daemon=True)
    recorder_thread.start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
