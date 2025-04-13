# sentinel-audio-recorder 🎙️

**sentinel-audio-recorder** is a Raspberry Pi-based audio recording system that captures audio from a USB device on boot. It provides a simple CLI to start and stop recordings and is designed for future integration with remote services for transcription, annotation, and smart search.

---

## 🚀 Features

- 🎤 Record audio from a USB interface (e.g., UCA222)
- 🐍 Python-based with modern `pyproject.toml` project structure
- 📦 Automatic virtual environment and dependency setup
- 🔁 Records on boot using `systemd`
- 🧪 Easy-to-use CLI for starting/stopping recordings

---

## 🔧 Setup Instructions

Run this on your Raspberry Pi or any other suitable device:

```bash
git clone https://github.com/jeromelane/sentinel-audio-recorder.git
cd sentinel-audio-recorder
./setup.sh
```

To test api for remote download of recordings:

```bash
curl http://localhost:8000/
```

To test recordings:

```bash
sentinel-audio-recorder start --duration 60

```

---

## 🖥️ CLI Usage

```bash
# Record for 5 minutes
sentinel-audio-recorder start

# Record in a 10-min rolling loop
sentinel-audio-recorder start --duration 600 --loop

# Noise-activated recording
sentinel-audio-recorder start --trigger

# Triggered recording with custom silence timeout
sentinel-audio-recorder start --trigger --threshold 500 --silence-timeout 10

```

Recordings are saved in `recordings/` and can be downloaded remotely.
