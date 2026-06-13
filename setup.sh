#!/bin/bash

set -euo pipefail

echo -e "\033[1;34m🔄 Updating system...\033[0m"
sudo apt update

echo -e "\033[1;34m📦 Installing required system packages...\033[0m"
sudo apt install -y python3-pyaudio sox portaudio19-dev python3-pip python3-venv

echo -e "\033[1;34m🐍 Creating virtual environment...\033[0m"
python3 -m venv .venv

echo -e "\033[1;34m📦 Installing Python dependencies...\033[0m"
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# Ensure recordings directory exists
mkdir -p recordings

if [ ! -f .env ]; then
    echo -e "\033[1;34m🧾 Creating .env from .env.default...\033[0m"
    cp .env.default .env
    echo -e "\033[1;33m⚠️  Edit .env and set SENTINEL_UPLOAD_URL to enable upload sync.\033[0m"
fi

echo -e "\033[1;34m🛠️  Installing systemd service...\033[0m"

# Set env variable pointing to current install path
cat <<EOF | sudo tee /etc/systemd/system/sentinel-audio-recorder.service
[Unit]
Description=Start noise-triggered recording on boot and API server
Wants=network-online.target
After=network-online.target

[Service]
Environment=ALSA_LOG_LEVEL=none
EnvironmentFile=-$(pwd)/.env
ExecStart=$(pwd)/.venv/bin/python $(pwd)/src/sentinel_audio_recorder/run_api.py
WorkingDirectory=$(pwd)
Restart=always
RestartSec=5
User=$USER
Group=$USER
StandardOutput=journal
StandardError=journal

# Timeout settings to prevent hung processes
TimeoutStopSec=10
TimeoutStartSec=30
KillMode=mixed
KillSignal=SIGTERM

# Resource limits
LimitNOFILE=65536
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF

echo -e "\033[1;34m🔁 Reloading systemd and enabling service...\033[0m"
sudo systemctl daemon-reload
sudo systemctl enable sentinel-audio-recorder
sudo systemctl start sentinel-audio-recorder

echo -e "\033[1;32m✅ Setup complete! API is running.\033[0m"
echo -e "\033[1;32m🧪 Test it: curl http://localhost:8000/\033[0m"
echo -e "\033[1;32m🔁 Upload sync runs automatically when SENTINEL_UPLOAD_URL is set in .env.\033[0m"
