import click

from sentinel_audio_recorder.config import load_config, setup_logging
from sentinel_audio_recorder.recorder import Recorder


@click.group()
def cli():
    """🎛️ Audio Recorder CLI – Start, manage, and control recordings"""
    pass


@cli.command()
@click.option("--duration", type=int, help="Recording duration in seconds.")
@click.option("--card", type=int, help="ALSA input card index (e.g. 1 for UCA222).")
@click.option("--sample-rate", type=int, help="Sample rate to request from the device.")
@click.option("--output-dir", type=click.Path(file_okay=False), help="Directory to store WAV files.")
@click.option("--loop", is_flag=True, default=None, help="Continuously roll recordings over every duration.")
@click.option("--trigger", is_flag=True, default=None, help="Enable noise-activated recording.")
@click.option("--threshold", type=int, help="RMS volume threshold for trigger mode.")
@click.option("--silence-timeout", type=int, help="Seconds of silence before auto-stopping trigger recording.")
@click.option("--config-file", type=click.Path(exists=True, dir_okay=False), help="Path to INI configuration file.")
def start(duration, card, sample_rate, output_dir, loop, trigger, threshold, silence_timeout, config_file):
    """
    ▶️ Start a new recording.

    --loop       : record continuously in duration-based chunks.
    --trigger    : activate recording when noise is detected.
    --threshold  : RMS volume threshold to trigger recording.
    """
    setup_logging()
    config = load_config(config_file)

    duration = duration or config.duration
    output_dir = output_dir or config.recording_dir
    card = card if card is not None else config.device_index
    loop = config.loop if loop is None else loop
    trigger = config.trigger if trigger is None else trigger
    if trigger is None:
        trigger = False
    threshold = threshold if threshold is not None else config.threshold
    silence_timeout = silence_timeout if silence_timeout is not None else config.silence_timeout
    sample_rate = sample_rate or config.sample_rate

    click.echo("🎙️ Starting audio recording...")

    recorder = Recorder(
        card_index=card,
        output_dir=output_dir,
        duration=duration,
        loop=loop,
        trigger=trigger,
        threshold=threshold,
        silence_timeout=silence_timeout,
        sample_rate=sample_rate,
    )
    recorder.record()


@cli.command()
def stop():
    """
    🛑 Stop the current recording (not yet implemented).
    """
    click.echo("❌ Stop is not implemented yet. Use Ctrl+C to stop manually.")
