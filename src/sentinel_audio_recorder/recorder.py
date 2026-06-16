import pyaudio
import wave
import os
import logging
import numpy as np
import subprocess
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from queue import Queue, Empty

logger = logging.getLogger(__name__)
_shutdown_event = Event()
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
        self.read_timeout = self._env_float("SENTINEL_AUDIO_READ_TIMEOUT", 5.0)
        self.max_read_timeouts = self._env_int("SENTINEL_AUDIO_MAX_READ_TIMEOUTS", 3)
        self.diagnostics_enabled = self._env_bool("SENTINEL_AUDIO_DIAGNOSTICS", True)
        self.level_log_interval = self._env_int("SENTINEL_AUDIO_LEVEL_LOG_INTERVAL", 60)
        self._last_rms = None

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
        self._log_audio_diagnostics("recorder initialized")

    def _env_int(self, name, default=None):
        value = os.getenv(name)
        if value in (None, ""):
            return default

        try:
            return int(value)
        except ValueError:
            logger.warning(f"⚠️ Ignoring invalid integer for {name}: {value!r}")
            return default

    def _env_float(self, name, default=None):
        value = os.getenv(name)
        if value in (None, ""):
            return default

        try:
            return float(value)
        except ValueError:
            logger.warning(f"⚠️ Ignoring invalid float for {name}: {value!r}")
            return default

    def _env_bool(self, name, default=False):
        value = os.getenv(name)
        if value in (None, ""):
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

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
        # Set a timeout of 5 seconds for stream read operations to prevent indefinite hangs
        stream = self.p.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            input_device_index=self.card_index,
            frames_per_buffer=self.CHUNK
        )
        return stream

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
        while not _shutdown_event.is_set():
            filename = self._generate_filename()
            logger.info(f"🎙️ Recording to {filename}...")

            stream = self._open_stream()
            try:
                frames = self._capture_frames(stream, self.duration)
                if frames:
                    self._save_wave(filename, frames)
                else:
                    logger.warning("No audio frames captured; skipping empty recording.")
            except AudioReadTimeout:
                logger.exception("Audio device stopped responding during recording.")
                raise
            except Exception as e:
                logger.error(f"❌ Error during recording: {e}")
                raise
            finally:
                self._close_stream(stream)

            if not self.loop:
                break

    def _raise_audio_timeout(self):
        self._log_audio_diagnostics("audio read timeouts exceeded")
        raise AudioReadTimeout("Audio device stopped responding.")

    def _stream_read_with_timeout(self, stream, timeout_seconds=None):
        """
        Read from audio stream with timeout protection.
        Prevents indefinite blocking if USB device becomes unresponsive.
        
        Args:
            stream: PyAudio stream object
            timeout_seconds: Max time to wait for audio data (default 5s)
            
        Returns:
            Audio chunk or None if timeout occurs
        """
        result_queue = Queue()
        exception_queue = Queue()
        
        def read_thread():
            try:
                chunk = stream.read(self.CHUNK, exception_on_overflow=False)
                result_queue.put(chunk)
            except Exception as e:
                exception_queue.put(e)
        
        timeout_seconds = timeout_seconds or self.read_timeout
        thread = Thread(target=read_thread, daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)
        
        # Check if thread finished
        if thread.is_alive():
            logger.error(
                "❌ Audio stream read timed out after %ss "
                "(device may be unresponsive; card_index=%s, rate=%s, channels=%s)",
                timeout_seconds,
                self.card_index,
                self.RATE,
                self.CHANNELS,
            )
            return None
        
        # Check for exceptions
        try:
            exc = exception_queue.get_nowait()
            raise exc
        except Empty:
            pass
        
        # Get the result
        try:
            return result_queue.get_nowait()
        except Empty:
            return None

    def _record_triggered(self):
        logger.info("🕵️ Waiting for sound to trigger recording...")
        stream = self._open_stream()

        recording = False
        frames = []
        silence_counter = 0
        timeout_count = 0
        filename = None
        recording_started_at = None
        last_level_log_at = time.monotonic()
        min_volume = None
        peak_volume = 0.0

        try:
            while not _shutdown_event.is_set():
                chunk = self._stream_read_with_timeout(stream)
                
                if chunk is None:
                    timeout_count += 1
                    logger.warning(
                        "Audio read timeout %d/%d%s",
                        timeout_count,
                        self.max_read_timeouts,
                        self._active_recording_summary(
                            recording,
                            filename,
                            recording_started_at,
                            silence_counter,
                            min_volume,
                            peak_volume,
                        ),
                    )
                    if timeout_count >= self.max_read_timeouts:
                        self._raise_audio_timeout()
                    continue

                timeout_count = 0
                    
                volume = self._compute_rms(chunk)
                self._last_rms = volume

                if volume > self.threshold:
                    if not recording:
                        filename = self._generate_filename()
                        recording_started_at = time.monotonic()
                        min_volume = volume
                        peak_volume = volume
                        last_level_log_at = recording_started_at
                        logger.info(
                            "🎤 Triggered! Started recording to %s "
                            "(rms=%.1f, threshold=%s)",
                            filename,
                            volume,
                            self.threshold,
                        )
                        frames = []
                        silence_counter = 0
                        recording = True
                    else:
                        min_volume = min(min_volume, volume)
                        peak_volume = max(peak_volume, volume)

                    frames.append(chunk)
                    silence_counter = 0

                elif recording:
                    min_volume = min(min_volume, volume)
                    peak_volume = max(peak_volume, volume)
                    frames.append(chunk)
                    silence_counter += self.CHUNK / self.RATE

                    if silence_counter >= self.silence_timeout:
                        logger.info(
                            "📁 Saving after %ss of silence "
                            "(duration=%.1fs, last_rms=%.1f, min_rms=%.1f, "
                            "peak_rms=%.1f, threshold=%s).",
                            self.silence_timeout,
                            time.monotonic() - recording_started_at,
                            volume,
                            min_volume,
                            peak_volume,
                            self.threshold,
                        )
                        self._save_wave(filename, frames)
                        recording = False
                        frames = []
                        filename = None  # reset for next trigger
                        recording_started_at = None
                        min_volume = None
                        peak_volume = 0.0

                if recording:
                    last_level_log_at = self._maybe_log_recording_levels(
                        filename,
                        recording_started_at,
                        last_level_log_at,
                        volume,
                        silence_counter,
                        min_volume,
                        peak_volume,
                    )

        except KeyboardInterrupt:
            logger.info("🛑 Interrupted by user.")
            if recording and frames:
                self._save_wave(filename, frames)
        finally:
            self._close_stream(stream)

    def _active_recording_summary(
        self,
        recording,
        filename,
        recording_started_at,
        silence_counter,
        min_volume,
        peak_volume,
    ):
        if not recording:
            return ""
        duration = time.monotonic() - recording_started_at
        return (
            f" during active recording filename={filename}, duration={duration:.1f}s, "
            f"last_rms={self._last_rms}, min_rms={min_volume}, "
            f"peak_rms={peak_volume}, silence={silence_counter:.1f}s, "
            f"threshold={self.threshold}"
        )

    def _maybe_log_recording_levels(
        self,
        filename,
        recording_started_at,
        last_level_log_at,
        volume,
        silence_counter,
        min_volume,
        peak_volume,
    ):
        if not self.level_log_interval or self.level_log_interval < 1:
            return last_level_log_at

        now = time.monotonic()
        if now - last_level_log_at < self.level_log_interval:
            return last_level_log_at

        logger.info(
            "Recording levels: filename=%s, duration=%.1fs, last_rms=%.1f, "
            "min_rms=%.1f, peak_rms=%.1f, silence=%.1fs/%ss, threshold=%s",
            filename,
            now - recording_started_at,
            volume,
            min_volume,
            peak_volume,
            silence_counter,
            self.silence_timeout,
            self.threshold,
        )
        return now


    def _capture_frames(self, stream, duration):
        frames = []
        max_frames = int(self.RATE / self.CHUNK * duration)
        timeout_count = 0
        
        for i in range(max_frames):
            chunk = self._stream_read_with_timeout(stream)
            
            if chunk is None:
                timeout_count += 1
                logger.warning(
                    "Audio read timeout %d/%d",
                    timeout_count,
                    self.max_read_timeouts,
                )
                
                if timeout_count >= self.max_read_timeouts:
                    self._raise_audio_timeout()
                continue
            
            timeout_count = 0  # Reset on successful read
            frames.append(chunk)
            
            # Check for shutdown
            if _shutdown_event.is_set():
                logger.info("🛑 Shutdown requested during frame capture")
                break
        
        return frames

    def _close_stream(self, stream):
        try:
            stream.stop_stream()
        except Exception:
            logger.debug("Audio stream stop failed during cleanup.", exc_info=True)
        try:
            stream.close()
        except Exception:
            logger.debug("Audio stream close failed during cleanup.", exc_info=True)

    def _log_audio_diagnostics(self, reason):
        if not getattr(self, "diagnostics_enabled", False):
            return

        logger.info(
            "Audio diagnostics (%s): card_index=%s, rate=%s, channels=%s, "
            "chunk=%s, read_timeout=%s, max_read_timeouts=%s",
            reason,
            self.card_index,
            self.RATE,
            self.CHANNELS,
            self.CHUNK,
            self.read_timeout,
            self.max_read_timeouts,
        )
        self._log_selected_device()
        self._log_input_devices()
        self._log_text_file("/proc/asound/cards", "ALSA cards")
        self._log_text_file("/proc/asound/devices", "ALSA devices")
        self._log_text_file("/proc/asound/pcm", "ALSA PCM devices")
        self._log_text_file("/proc/device-tree/model", "Pi model")
        self._log_command(["vcgencmd", "get_throttled"], "Pi throttling")
        self._log_command(["lsusb"], "USB devices")

    def _log_selected_device(self):
        try:
            info = self.p.get_device_info_by_index(self.card_index)
        except Exception as exc:
            logger.warning("Audio diagnostics: selected device lookup failed: %s", exc)
            return
        logger.info("Audio diagnostics: selected PyAudio device: %s", info)

    def _log_input_devices(self):
        try:
            devices = []
            for index in range(self.p.get_device_count()):
                info = self.p.get_device_info_by_index(index)
                if int(info.get("maxInputChannels", 0)) > 0:
                    devices.append(
                        {
                            "index": index,
                            "name": info.get("name"),
                            "maxInputChannels": info.get("maxInputChannels"),
                            "defaultSampleRate": info.get("defaultSampleRate"),
                        }
                    )
        except Exception as exc:
            logger.warning("Audio diagnostics: input device listing failed: %s", exc)
            return
        logger.info("Audio diagnostics: input devices: %s", devices)

    def _log_text_file(self, path, label):
        try:
            text = Path(path).read_text(errors="replace").replace("\x00", "").strip()
        except FileNotFoundError:
            logger.debug("Audio diagnostics: %s unavailable at %s", label, path)
            return
        except Exception as exc:
            logger.warning("Audio diagnostics: failed reading %s: %s", path, exc)
            return

        if len(text) > 4000:
            text = text[:4000] + "...<truncated>"
        logger.info("Audio diagnostics: %s:\n%s", label, text or "<empty>")

    def _log_command(self, command, label):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except FileNotFoundError:
            logger.debug("Audio diagnostics: %s command not found: %s", label, command[0])
            return
        except Exception as exc:
            logger.warning("Audio diagnostics: %s command failed: %s", label, exc)
            return

        output = (result.stdout + result.stderr).strip()
        if len(output) > 4000:
            output = output[:4000] + "...<truncated>"
        logger.info(
            "Audio diagnostics: %s exit=%s:\n%s",
            label,
            result.returncode,
            output or "<empty>",
        )


class AudioReadTimeout(RuntimeError):
    """Raised when the audio input stops returning data."""
