"""
StreamGremlin Default Sound Synthesizer (CC0 Public Domain)
Erzeugt beim ersten Start automatisch echte, abspielbare Standard-WAV-Sounds
im Ordner `sounds/`, damit StreamGremlin sofort "Out-of-the-Box" funktioniert
und die Sounds direkt ins Git-Repository übernommen werden können.
"""
import math
import os
import random
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100


def _write_wav(filepath: Path, samples: list[float]) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(filepath), "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for s in samples:
            clamped = max(-1.0, min(1.0, s))
            frames.extend(struct.pack("<h", int(clamped * 32767)))
        wav_file.writeframes(frames)


def synth_bonk(filepath: Path) -> None:
    """Klassischer hohler Holz-/Cartoon-Bonk Sound."""
    duration = 0.35
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 14.0)
        freq = 180.0 + 320.0 * math.exp(-t * 28.0)
        val = (
            0.7 * math.sin(2 * math.pi * freq * t)
            + 0.3 * math.sin(2 * math.pi * (freq * 2.4) * t)
        ) * env
        samples.append(val)
    _write_wav(filepath, samples)


def synth_flashbang(filepath: Path) -> None:
    """CS2-artiger Knall gefolgt von hohem Tinnitus-Pfeifton."""
    duration = 2.8
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Initial bang (first 0.25s)
        bang_env = math.exp(-t * 18.0) if t < 0.4 else 0.0
        noise = (random.random() * 2.0 - 1.0) * bang_env * 0.9
        # High-pitched tinnitus ring (2850 Hz)
        ring_env = min(1.0, t / 0.1) * math.exp(-t * 0.9)
        ring = math.sin(2 * math.pi * 2850.0 * t) * ring_env * 0.45
        samples.append(noise + ring)
    _write_wav(filepath, samples)


def synth_airhorn(filepath: Path) -> None:
    """Klassisches MLG/Stadium Airhorn."""
    duration = 1.1
    n = int(SAMPLE_RATE * duration)
    freqs = [480.0, 600.0, 720.0, 960.0]
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 1.0 if t < 0.85 else math.exp(-(t - 0.85) * 12.0)
        attack = min(1.0, t / 0.03)
        val = 0.0
        for f in freqs:
            detune = f * (1.0 + 0.008 * math.sin(2 * math.pi * 6.0 * t))
            # Sawtooth-like rich harmonics
            val += math.sin(2 * math.pi * detune * t) * 0.25
            val += math.sin(4 * math.pi * detune * t) * 0.12
        samples.append(math.tanh(val * 2.2) * env * attack * 0.8)
    _write_wav(filepath, samples)


def synth_evil_laugh(filepath: Path) -> None:
    """Freches 8-Bit Gremlin-Kichern (absteigende Arpeggio-Stufen)."""
    duration = 1.2
    n = int(SAMPLE_RATE * duration)
    notes = [660, 587, 523, 622, 554, 493, 587, 523, 440, 392]
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(notes) - 1, int(t / (duration / len(notes))))
        local_t = (t % (duration / len(notes)))
        env = math.exp(-local_t * 16.0)
        freq = notes[idx]
        # Square wave character
        sq = 1.0 if math.sin(2 * math.pi * freq * t) >= 0 else -1.0
        samples.append(sq * env * 0.45)
    _write_wav(filepath, samples)


def synth_censor_beep(filepath: Path) -> None:
    """Klassischer 1000 Hz TV-Zensur-Piepton."""
    duration = 0.75
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        samples.append(math.sin(2 * math.pi * 1000.0 * t) * 0.6)
    _write_wav(filepath, samples)


def synth_hype_horn(filepath: Path) -> None:
    """Aufsteigende Hype-Train Sirene / Fanfare."""
    duration = 1.5
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        freq = 300.0 + 450.0 * (t / duration) ** 1.3
        env = min(1.0, t / 0.05) * (1.0 if t < 1.2 else math.exp(-(t - 1.2) * 8.0))
        val = (
            0.5 * math.sin(2 * math.pi * freq * t)
            + 0.3 * math.sin(2 * math.pi * freq * 1.5 * t)
            + 0.2 * math.sin(2 * math.pi * freq * 2.0 * t)
        )
        samples.append(val * env * 0.75)
    _write_wav(filepath, samples)


def ensure_default_sounds(sounds_dir: str | Path) -> list[str]:
    """Erstellt die Standard-Sounds, falls sie noch nicht im Ordner liegen."""
    base = Path(sounds_dir)
    base.mkdir(parents=True, exist_ok=True)
    generators = {
        "bonk.wav": synth_bonk,
        "flashbang.wav": synth_flashbang,
        "airhorn.wav": synth_airhorn,
        "evil_laugh.wav": synth_evil_laugh,
        "censor_beep.wav": synth_censor_beep,
        "hype_horn.wav": synth_hype_horn,
    }
    created = []
    for filename, fn in generators.items():
        target = base / filename
        if not target.exists():
            fn(target)
            created.append(filename)
    return created


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "sounds"
    generated = ensure_default_sounds(out)
    print(f"Generated default sounds in {out}: {generated}")
