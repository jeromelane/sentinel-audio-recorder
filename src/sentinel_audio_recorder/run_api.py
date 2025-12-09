import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from sentinel_audio_recorder.api import router
from sentinel_audio_recorder.config import load_config, setup_logging
from sentinel_audio_recorder.recorder import Recorder

setup_logging()
logger = logging.getLogger(__name__)
app = FastAPI()
app.include_router(router)
config = load_config()
app.state.config = config
recordings_dir = Path(config.recording_dir).resolve()
recordings_dir.mkdir(parents=True, exist_ok=True)
app.state.recordings_dir = recordings_dir


@app.on_event("startup")
def start_background_recording():
    if not config.background_enabled:
        logger.info("event=background_recording status=disabled")
        return

    def background_trigger():
        trigger_mode = True if config.trigger is None else config.trigger
        recorder = Recorder(
            card_index=config.device_index,
            output_dir=config.recording_dir,
            duration=config.duration,
            loop=config.loop,
            trigger=trigger_mode,
            threshold=config.threshold,
            silence_timeout=config.silence_timeout,
            sample_rate=config.sample_rate,
        )
        recorder.record()

    logger.info("event=background_recording status=starting")
    thread = threading.Thread(target=background_trigger, daemon=True)
    thread.start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
