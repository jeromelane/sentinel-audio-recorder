import threading
import os
import logging
import time

import uvicorn
from fastapi import FastAPI

from sentinel_audio_recorder import api
from sentinel_audio_recorder.api import router
from sentinel_audio_recorder.env import load_env_file
from sentinel_audio_recorder.recorder import Recorder
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

app = FastAPI()
app.include_router(router)
upload_thread = None
uploader = None
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

@app.on_event("startup")
def start_background_recording():
    global upload_thread, uploader

    config = UploadConfig.from_env()
    uploader = RecordingUploader(config=config)
    api.set_uploader(uploader)

    if config.enabled:
        upload_thread = threading.Thread(target=uploader.run_forever, daemon=True)
        upload_thread.start()

    if os.getenv("SENTINEL_DISABLE_BACKGROUND_RECORDER") == "1":
        return

    def background_trigger():
        retry_seconds = _recorder_retry_seconds()
        while True:
            try:
                recorder = Recorder(trigger=True)
                recorder.record()
            except Exception:
                logger.exception(
                    "Background recorder failed to start; retrying in %ss",
                    retry_seconds,
                )
                time.sleep(retry_seconds)
            else:
                break

    thread = threading.Thread(target=background_trigger, daemon=True)
    thread.start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
