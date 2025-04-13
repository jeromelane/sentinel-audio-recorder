import uvicorn
from fastapi import FastAPI
import threading

from sentinel_audio_recorder.api import router
from sentinel_audio_recorder.recorder import Recorder

app = FastAPI()
app.include_router(router)

@app.on_event("startup")
def start_background_recording():
    def background_trigger():
        recorder = Recorder(trigger=True)
        recorder.record()

    thread = threading.Thread(target=background_trigger, daemon=True)
    thread.start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
