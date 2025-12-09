import logging
import os
import wave
from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pyaudio

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        card_index: Optional[int] = 1,
        duration: int = 3600,
        output_dir: str = "recordings",
        loop: bool = False,
        trigger: bool = False,
        threshold: int = 1500,
        silence_timeout: int = 20,
        sample_rate: Optional[int] = None,
    ):
        """Create a recorder instance and validate device configuration.

        The constructor immediately performs device discovery and sample rate
        selection so that recording calls fail fast if the requested hardware
        or format is unavailable.
        """
        self.p = pyaudio.PyAudio()
        self.metrics: Dict[str, int] = {
            "frames_processed": 0,
            "events_detected": 0,
            "saves": 0,
            "errors": 0,
        }

        self.card_index = self._discover_card_index(card_index, sample_rate)
        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 2
        self.RATE = self._detect_sample_rate(self.card_index, sample_rate)

        self.duration = duration
        self.output_dir = output_dir
        self.loop = loop
        self.trigger = trigger
        self.threshold = threshold
        self.silence_timeout = silence_timeout

        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def preflight(device_index: Optional[int] = None, sample_rate: Optional[int] = None):
        """Enumerate input devices and verify the requested format is supported."""
        p = pyaudio.PyAudio()
        devices = []
        try:
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append((i, info))
                    logger.info(
                        "event=device_detected index=%s name=%s max_input_channels=%s",
                        i,
                        info.get("name"),
                        info.get("maxInputChannels"),
                    )

            if not devices:
                raise RuntimeError("No audio input devices available")

            target_index = device_index if device_index is not None else devices[0][0]
            rates = [sample_rate] if sample_rate else [48000, 44100, 32000, 16000, 8000]

            for rate in rates:
                try:
                    p.is_format_supported(
                        rate,
                        input_device=target_index,
                        input_channels=2,
                        input_format=pyaudio.paInt16,
                    )
                    logger.info(
                        "event=format_supported device_index=%s sample_rate=%s",
                        target_index,
                        rate,
                    )
                    return
                except ValueError:
                    continue

            raise RuntimeError(
                f"Sample rate(s) {rates} unsupported on device index {target_index}."
            )
        finally:
            p.terminate()

    def _detect_sample_rate(self, card_index: int, requested_rate: Optional[int]):
        rates = [requested_rate] if requested_rate else [48000, 44100, 32000, 16000, 8000]
        for rate in rates:
            if rate is None:
                continue
            try:
                self.p.is_format_supported(
                    rate,
                    input_device=card_index,
                    input_channels=self.CHANNELS,
                    input_format=self.FORMAT,
                )
                logger.info("event=sample_rate_selected rate=%s", rate)
                return rate
            except ValueError:
                logger.info(
                    "event=sample_rate_rejected rate=%s device_index=%s", rate, card_index
                )
                continue
        raise ValueError("No supported sample rate found for the device.")

    def _generate_filename(self):
        return os.path.join(
            self.output_dir, f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        )

    def _discover_card_index(self, user_card_index: Optional[int], sample_rate: Optional[int]):
        if user_card_index is not None:
            self.preflight(device_index=user_card_index, sample_rate=sample_rate)
            return user_card_index

        logger.info("event=device_discovery start=true")
        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                name = info["name"]
                logger.info(
                    "event=device_candidate index=%s name=%s max_channels=%s",
                    i,
                    name,
                    info["maxInputChannels"],
                )
                if "USB Audio CODEC" in name:
                    logger.info("event=device_selected index=%s name=%s", i, name)
                    self.preflight(device_index=i, sample_rate=sample_rate)
                    return i

        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                logger.warning(
                    "event=device_fallback index=%s name=%s", i, info["name"]
                )
                self.preflight(device_index=i, sample_rate=sample_rate)
                return i

        raise RuntimeError("No suitable audio input device found.")

    def _compute_rms(self, chunk):
        samples = np.frombuffer(chunk, dtype=np.int16)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

    def _save_wave(self, filename, frames):
        wf = wave.open(filename, "wb")
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(self.p.get_sample_size(self.FORMAT))
        wf.setframerate(self.RATE)
        wf.writeframes(b"".join(frames))
        wf.close()
        self.metrics["saves"] += 1
        logger.info("event=file_saved path=%s", filename)

    def _open_stream(self):
        return self.p.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            input_device_index=self.card_index,
            frames_per_buffer=self.CHUNK,
        )

    def record(self):
        """Start recording using the configured mode and log lifecycle events."""
        logger.info(
            "event=record_start mode=%s output_dir=%s device_index=%s rate=%s",
            "trigger" if self.trigger else "loop",
            self.output_dir,
            self.card_index,
            self.RATE,
        )
        try:
            if self.trigger:
                self._record_triggered()
            else:
                self._record_looped()
        except KeyboardInterrupt:
            logger.info("event=record_interrupted reason=user")
        except Exception as exc:
            self.metrics["errors"] += 1
            logger.exception("event=record_error error=%s", exc)
            raise
        finally:
            self.p.terminate()
            logger.info("event=record_complete metrics=%s", self.metrics)

    def _record_looped(self):
        """Continuously record fixed-length clips until looping is disabled."""
        while True:
            filename = self._generate_filename()
            logger.info("event=loop_recording_start path=%s duration=%s", filename, self.duration)

            stream = self._open_stream()
            frames = self._capture_frames(stream, self.duration)
            stream.stop_stream()
            stream.close()

            self._save_wave(filename, frames)

            if not self.loop:
                break

    def _record_triggered(self):
        """Record audio clips when input volume rises above the configured threshold."""
        logger.info(
            "event=trigger_wait threshold=%s silence_timeout=%s", self.threshold, self.silence_timeout
        )
        stream = self._open_stream()

        recording = False
        frames = []
        silence_counter = 0
        filename = None

        try:
            while True:
                chunk = stream.read(self.CHUNK, exception_on_overflow=False)
                self.metrics["frames_processed"] += self.CHUNK
                volume = self._compute_rms(chunk)

                if volume > self.threshold:
                    if not recording:
                        filename = self._generate_filename()
                        logger.info("event=triggered_recording_start path=%s volume=%s", filename, volume)
                        frames = []
                        silence_counter = 0
                        recording = True
                        self.metrics["events_detected"] += 1

                    frames.append(chunk)
                    silence_counter = 0

                elif recording:
                    frames.append(chunk)
                    silence_counter += self.CHUNK / self.RATE

                    if silence_counter >= self.silence_timeout:
                        logger.info("event=trigger_silence_timeout seconds=%s", self.silence_timeout)
                        self._save_wave(filename, frames)
                        recording = False
                        frames = []
                        filename = None

        except KeyboardInterrupt:
            logger.info("event=record_interrupted reason=user")
            if recording and frames:
                self._save_wave(filename, frames)
        finally:
            stream.stop_stream()
            stream.close()

    def _capture_frames(self, stream, duration):
        """Read a fixed-duration set of frames from the input stream."""
        frames = []
        for _ in range(0, int(self.RATE / self.CHUNK * duration)):
            frames.append(stream.read(self.CHUNK, exception_on_overflow=False))
            self.metrics["frames_processed"] += self.CHUNK
        return frames
