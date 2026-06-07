import click
from pathlib import Path

from sentinel_audio_recorder.recorder import Recorder
from sentinel_audio_recorder.uploader import RecordingUploader, UploadConfig

@click.group()
def cli():
    """🎛️ Audio Recorder CLI – Start, manage, and control recordings"""
    pass


@cli.command()
@click.option("--duration", default=300, show_default=True, type=int,
              help="Recording duration in seconds.")
@click.option("--card", default=None, type=int,
              help="ALSA input card index (e.g. 1 for UCA222).")
@click.option("--loop", is_flag=True, default=False,
              help="Continuously roll recordings over every duration.")
@click.option("--trigger", is_flag=True, default=False,
              help="Enable noise-activated recording.")
@click.option("--threshold", default=1500, show_default=True, type=int,
              help="RMS volume threshold for trigger mode.")
@click.option("--silence-timeout", default=10, show_default=True, type=int,
              help="Seconds of silence before auto-stopping trigger recording.")
def start(duration, card, loop, trigger, threshold, silence_timeout):
    """
    ▶️ Start a new recording.
    
    --loop       : record continuously in duration-based chunks.
    --trigger    : activate recording when noise is detected.
    --threshold  : RMS volume threshold to trigger recording.
    """
    click.echo("🎙️ Starting audio recording...")

    recorder = Recorder(
        card_index=card,
        output_dir="recordings",
        duration=duration,
        loop=loop,
        trigger=trigger,
        threshold=threshold,
        silence_timeout=silence_timeout
    )
    recorder.record()


@cli.command()
def stop():
    """
    🛑 Stop the current recording (not yet implemented).
    """
    click.echo("❌ Stop is not implemented yet. Use Ctrl+C to stop manually.")


@cli.command()
@click.option("--once", "run_once", is_flag=True, default=False,
              help="Run one upload/cleanup pass and exit.")
@click.option("--watch", is_flag=True, default=False,
              help="Continuously upload and clean up recordings.")
@click.option("--url", default=None,
              help="Remote analyser ingest endpoint.")
@click.option("--token", default=None,
              help="Optional bearer token for the remote endpoint.")
@click.option("--recordings-dir", default=None,
              help="Directory containing local WAV recordings.")
def sync(run_once, watch, url, token, recordings_dir):
    """
    🔁 Upload completed recordings and clean local cache if storage is high.
    """
    config = UploadConfig.from_env()
    if url:
        config.upload_url = url
    if token:
        config.upload_token = token
    if recordings_dir:
        config.recordings_dir = Path(recordings_dir)

    uploader = RecordingUploader(config=config)
    if watch:
        click.echo("🔁 Starting continuous upload sync...")
        uploader.run_forever()
        return

    result = uploader.run_once()
    click.echo(f"✅ Sync complete: {result}")
