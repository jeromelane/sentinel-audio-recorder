import threading
import os

import uvicorn
from fastapi import FastAPI

from sentinel_audio_recorder import api
from sentinel_audio_recorder.api import router
from sentinel_audio_recorder.recorder import Recorder
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

app = FastAPI()
app.include_router(router)
upload_thread = None
uploader = None

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
        recorder = Recorder(trigger=True)
        recorder.record()

    thread = threading.Thread(target=background_trigger, daemon=True)
    thread.start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
