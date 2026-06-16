import threading
import os
import logging
import signal

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
    return _env_int("SENTINEL_RECORDER_RETRY_SECONDS", 30, minimum=1)


def _recorder_max_retries():
    return _env_int("SENTINEL_RECORDER_MAX_RETRIES", 5, minimum=1)


def _trigger_threshold():
    return _env_int("SENTINEL_TRIGGER_THRESHOLD", 1500, minimum=1)


def _trigger_silence_timeout():
    return _env_int("SENTINEL_TRIGGER_SILENCE_TIMEOUT", 20, minimum=1)


def _env_int(name, default, minimum=None):
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using %s",
            name,
            value,
            default,
        )
        return default
    if minimum is not None and parsed < minimum:
        logger.warning("%s=%s is too low; using %s", name, parsed, minimum)
        return minimum
    return parsed


def shutdown_handler(signum, frame):
    """Handle graceful shutdown on SIGTERM/SIGINT"""
    logger.info(f"🛑 Received signal {signum}, initiating graceful shutdown...")
    _shutdown_event.set()
    if uploader:
        uploader.stop()


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

@app.on_event("startup")
def start_background_recording():
    global upload_thread, uploader, recorder_thread

    _shutdown_event.clear()

    config = UploadConfig.from_env()
    uploader = RecordingUploader(config=config)
    api.set_uploader(uploader)

    upload_thread = threading.Thread(target=uploader.run_forever, daemon=True)
    upload_thread.start()

    if os.getenv("SENTINEL_DISABLE_BACKGROUND_RECORDER") == "1":
        return

    def background_trigger():
        retry_seconds = _recorder_retry_seconds()
        max_retries = _recorder_max_retries()
        threshold = _trigger_threshold()
        silence_timeout = _trigger_silence_timeout()
        retry_count = 0
        
        while not _shutdown_event.is_set() and retry_count < max_retries:
            try:
                logger.info(
                    "🎙️ Starting background recorder "
                    "(attempt %d/%d, threshold=%d, silence_timeout=%ds)...",
                    retry_count + 1,
                    max_retries,
                    threshold,
                    silence_timeout,
                )
                recorder = Recorder(
                    trigger=True,
                    threshold=threshold,
                    silence_timeout=silence_timeout,
                )
                recorder.record()
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


@app.on_event("shutdown")
def stop_background_workers():
    _shutdown_event.set()
    if uploader:
        uploader.stop()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
