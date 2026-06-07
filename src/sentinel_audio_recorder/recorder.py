import pyaudio
import wave
import os
import logging
import numpy as np
from datetime import datetime

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

class Recorder:
    def __init__(
        self,
        card_index=None,
        duration=3600,
        output_dir=None,
        loop=False,
        trigger=False,
        threshold=1500,
        silence_timeout=20,
        sample_rate=None,
        channels=None
    ):
        self.p = pyaudio.PyAudio()
        card_index = self._env_int("SENTINEL_AUDIO_CARD_INDEX", card_index)
        self.card_index = self._discover_card_index(card_index)
        self.duration = duration
        self.output_dir = output_dir or os.getenv("SENTINEL_RECORDINGS_DIR", "recordings")
        self.loop = loop
        self.trigger = trigger
        self.threshold = threshold
        self.silence_timeout = silence_timeout

        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16
        requested_rate = self._env_int("SENTINEL_AUDIO_SAMPLE_RATE", sample_rate)
        requested_channels = self._env_int("SENTINEL_AUDIO_CHANNELS", channels)
        self.RATE, self.CHANNELS = self._detect_audio_settings(
            self.card_index,
            requested_rate=requested_rate,
            requested_channels=requested_channels,
        )

        os.makedirs(self.output_dir, exist_ok=True)

    def _env_int(self, name, default=None):
        value = os.getenv(name)
        if value in (None, ""):
            return default

        try:
            return int(value)
        except ValueError:
            logger.warning(f"⚠️ Ignoring invalid integer for {name}: {value!r}")
            return default

    def _detect_audio_settings(self, card_index, requested_rate=None, requested_channels=None):
        device_info = self.p.get_device_info_by_index(card_index)
        max_channels = int(device_info.get("maxInputChannels", 0))
        default_rate = int(round(device_info.get("defaultSampleRate", 0) or 0))

        if requested_channels is not None:
            channel_candidates = [requested_channels]
        elif max_channels >= 2:
            channel_candidates = [2, 1]
        else:
            channel_candidates = [1]

        rate_candidates = []
        for rate in [requested_rate, default_rate, 48000, 44100, 32000, 16000, 8000]:
            if rate and rate not in rate_candidates:
                rate_candidates.append(rate)

        for channels in channel_candidates:
            if channels < 1 or channels > max_channels:
                continue
            for rate in rate_candidates:
                try:
                    self.p.is_format_supported(
                        rate,
                        input_device=card_index,
                        input_channels=channels,
                        input_format=pyaudio.paInt16
                    )
                    logger.info(
                        f"✅ Detected audio settings: {rate} Hz, {channels} channel(s)"
                    )
                    return rate, channels
                except ValueError:
                    continue

        raise ValueError(
            f"❌ No supported audio format found for device {card_index} "
            f"({max_channels} input channel(s))."
        )

    def _generate_filename(self):
        return os.path.join(
            self.output_dir,
            f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        )

    def _discover_card_index(self, user_card_index):
        if user_card_index is not None:
            return user_card_index

        logger.info("🔎 Discovering input devices...")
        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                name = info['name']
                logger.info(f"   Found [{i}]: {name} ({info['maxInputChannels']} channels)")
                if "USB Audio CODEC" in name:
                    logger.info(f"✅ Selected USB Audio CODEC at index {i}")
                    return i

        # fallback to first with input
        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                logger.warning(f"⚠️ Using fallback input device: {info['name']} at index {i}")
                return i

        raise RuntimeError("❌ No suitable audio input device found.")

    def _compute_rms(self, chunk):
        samples = np.frombuffer(chunk, dtype=np.int16)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float32)**2)))


    def _save_wave(self, filename, frames):
        wf = wave.open(filename, 'wb')
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(self.p.get_sample_size(self.FORMAT))
        wf.setframerate(self.RATE)
        wf.writeframes(b''.join(frames))
        wf.close()
        logger.info(f"✅ Saved: {filename}")

    def _open_stream(self):
        return self.p.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            input_device_index=self.card_index,
            frames_per_buffer=self.CHUNK
        )

    def record(self):
        """Main entry point. Delegates to triggered or looped recording."""
        try:
            if self.trigger:
                self._record_triggered()
            else:
                self._record_looped()
        except KeyboardInterrupt:
            logger.info("🛑 Interrupted by user.")
        finally:
            self.p.terminate()
            logger.info("🎧 Audio interface closed.")

    def _record_looped(self):
        while True:
            filename = self._generate_filename()
            logger.info(f"🎙️ Recording to {filename}...")

            stream = self._open_stream()
            frames = self._capture_frames(stream, self.duration)
            stream.stop_stream()
            stream.close()

            self._save_wave(filename, frames)

            if not self.loop:
                break

    def _record_triggered(self):
        logger.info("🕵️ Waiting for sound to trigger recording...")
        stream = self._open_stream()

        recording = False
        frames = []
        silence_counter = 0
        filename = None

        try:
            while True:
                chunk = stream.read(self.CHUNK, exception_on_overflow=False)
                volume = self._compute_rms(chunk)

                if volume > self.threshold:
                    if not recording:
                        filename = self._generate_filename()
                        logger.info(f"🎤 Triggered! Started recording to {filename}")
                        frames = []
                        silence_counter = 0
                        recording = True

                    frames.append(chunk)
                    silence_counter = 0

                elif recording:
                    frames.append(chunk)
                    silence_counter += self.CHUNK / self.RATE

                    if silence_counter >= self.silence_timeout:
                        logger.info(f"📁 Saving after {self.silence_timeout}s of silence.")
                        self._save_wave(filename, frames)
                        recording = False
                        frames = []
                        filename = None  # reset for next trigger

        except KeyboardInterrupt:
            logger.info("🛑 Interrupted by user.")
            if recording and frames:
                self._save_wave(filename, frames)
        finally:
            stream.stop_stream()
            stream.close()


    def _capture_frames(self, stream, duration):
        frames = []
        for _ in range(0, int(self.RATE / self.CHUNK * duration)):
            frames.append(stream.read(self.CHUNK, exception_on_overflow=False))
        return frames
