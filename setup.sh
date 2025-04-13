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

echo -e "\033[1;34m🛠️  Installing systemd service...\033[0m"
sudo cp system/sentinel-audio-recorder.service /etc/systemd/system/

# Set env variable pointing to current install path
cat <<EOF | sudo tee /etc/systemd/system/sentinel-audio-recorder.service
[Unit]
Description=sentinel-audio-recorder API server
After=network.target

[Service]
Environment=ALSA_LOG_LEVEL=none
ExecStart=$(pwd)/.venv/bin/python $(pwd)/src/audio_recorder/run_api.py
WorkingDirectory=$(pwd)
Restart=on-failure
User=$USER
Group=$USER
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo -e "\033[1;34m🔁 Reloading systemd and enabling service...\033[0m"
sudo systemctl daemon-reload
sudo systemctl enable sentinel-audio-recorder
sudo systemctl start sentinel-audio-recorder

echo -e "\033[1;32m✅ Setup complete! API is running.\033[0m"
echo -e "\033[1;32m🧪 Test it: curl http://localhost:8000/\033[0m"
