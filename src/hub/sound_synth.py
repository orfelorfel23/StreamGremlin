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
        bang_env = math.exp(-t * 18.0) if t < 0.4 else 0.0
        noise = (random.random() * 2.0 - 1.0) * bang_env * 0.9
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


def synth_fuse_switch(filepath: Path) -> None:
    """Sicherung fliegt raus / Schalterklick mit kurzem Funkenzischen."""
    duration = 0.55
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Snappy mechanical click (2 pulses)
        click1 = math.exp(-t * 70.0) * math.sin(2 * math.pi * 1200 * t) if t < 0.08 else 0.0
        click2 = math.exp(-(t - 0.04) * 85.0) * math.sin(2 * math.pi * 480 * t) if t >= 0.04 else 0.0
        # Brief 50Hz hum and spark that dies abruptly
        spark = (random.random() * 2.0 - 1.0) * math.exp(-t * 22.0) * 0.35 if t < 0.18 else 0.0
        hum = math.sin(2 * math.pi * 50.0 * t) * math.exp(-t * 15.0) * 0.4 if t < 0.22 else 0.0
        samples.append(click1 * 0.7 + click2 * 0.8 + spark + hum)
    _write_wav(filepath, samples)


def synth_heartbeat(filepath: Path) -> None:
    """Dumpfer zweistufiger Herzschlag (Lub-Dub)."""
    duration = 1.4
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Beat 1 (t=0.1s)
        t1 = t - 0.1
        b1 = math.exp(-t1 * 18.0) * math.sin(2 * math.pi * 55.0 * t1) if 0 <= t1 < 0.25 else 0.0
        # Beat 2 (t=0.36s)
        t2 = t - 0.36
        b2 = math.exp(-t2 * 20.0) * math.sin(2 * math.pi * 50.0 * t2) if 0 <= t2 < 0.25 else 0.0
        # Beat 3 (t=0.85s)
        t3 = t - 0.85
        b3 = math.exp(-t3 * 18.0) * math.sin(2 * math.pi * 55.0 * t3) if 0 <= t3 < 0.25 else 0.0
        # Beat 4 (t=1.11s)
        t4 = t - 1.11
        b4 = math.exp(-t4 * 20.0) * math.sin(2 * math.pi * 50.0 * t4) if 0 <= t4 < 0.25 else 0.0
        val = (b1 + b2 * 0.85 + b3 + b4 * 0.85) * 0.95
        samples.append(val)
    _write_wav(filepath, samples)


def synth_earthquake_rumble(filepath: Path) -> None:
    """Tiefes Bass-Erdbeben-Grollen mit Frequenzmodulation."""
    duration = 3.0
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.2) * (1.0 if t < 2.2 else math.exp(-(t - 2.2) * 4.0))
        # Deep 38Hz bass rumble modulated by low noise
        mod = 1.0 + 0.2 * math.sin(2 * math.pi * 7.0 * t)
        rumble = math.sin(2 * math.pi * 38.0 * t * mod) * 0.65
        sub = math.sin(2 * math.pi * 24.0 * t) * 0.4
        noise = (random.random() * 2.0 - 1.0) * 0.22
        samples.append((rumble + sub + noise) * env * 0.85)
    _write_wav(filepath, samples)


def synth_tilt_creak(filepath: Path) -> None:
    """Quietschendes Dreh- und Knarzgeräusch."""
    duration = 0.85
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.04) * math.exp(-t * 2.5)
        freq = 320.0 + 400.0 * math.sin(2 * math.pi * 12.0 * t) + 120.0 * t
        val = math.sin(2 * math.pi * freq * t) * 0.5 + math.sin(4 * math.pi * freq * t) * 0.25
        samples.append(math.tanh(val * 2.0) * env * 0.75)
    _write_wav(filepath, samples)


def synth_bottle_pop(filepath: Path) -> None:
    """Flaschen-Plopp und Zischen (Bierkorken)."""
    duration = 0.9
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Pop
        pop_env = math.exp(-t * 45.0) if t < 0.15 else 0.0
        pop = math.sin(2 * math.pi * (280.0 + 450.0 * math.exp(-t * 50.0)) * t) * pop_env * 0.85
        # Fizz
        fizz_env = (min(1.0, (t - 0.02) / 0.08) * math.exp(-(t - 0.02) * 5.0)) if t >= 0.02 else 0.0
        fizz = (random.random() * 2.0 - 1.0) * fizz_env * 0.35
        samples.append(pop + fizz)
    _write_wav(filepath, samples)


def synth_glitch_static(filepath: Path) -> None:
    """Digitales Störgeräusch und GPU-Glitch."""
    duration = 1.2
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 1.0 if t < 0.9 else math.exp(-(t - 0.9) * 10.0)
        # Sliced digital noise with sudden frequency jumps
        freq_step = int(t * 18.0) % 5
        base_f = [450, 1200, 220, 1850, 680][freq_step]
        sq = 1.0 if math.sin(2 * math.pi * base_f * t) >= 0 else -1.0
        noise = (random.random() * 2.0 - 1.0) * 0.35
        samples.append((sq * 0.45 + noise) * env * 0.7)
    _write_wav(filepath, samples)


def synth_dimension_warp(filepath: Path) -> None:
    """Spaciger Dimensions-Warp Sound."""
    duration = 1.5
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.1) * (1.0 if t < 1.0 else math.exp(-(t - 1.0) * 6.0))
        freq = 800.0 * math.exp(-t * 1.8) + 120.0 * math.sin(2 * math.pi * 18.0 * t)
        val = math.sin(2 * math.pi * freq * t) * 0.6 + math.sin(4 * math.pi * freq * t) * 0.2
        samples.append(val * env * 0.8)
    _write_wav(filepath, samples)


def synth_bitcrush_8bit(filepath: Path) -> None:
    """8-Bit Arcade Chiptune Arpeggio."""
    duration = 0.9
    n = int(SAMPLE_RATE * duration)
    notes = [261.6, 329.6, 392.0, 523.2, 659.2, 783.9, 1046.5]
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(notes) - 1, int(t / (duration / len(notes))))
        freq = notes[idx]
        sq = 1.0 if math.sin(2 * math.pi * freq * t) >= 0 else -1.0
        env = math.exp(-((t % (duration / len(notes)))) * 12.0)
        samples.append(sq * env * 0.5)
    _write_wav(filepath, samples)


def synth_rave_drop(filepath: Path) -> None:
    """Techno Beatdrop mit Bassdrum und Rave-Lead."""
    duration = 2.2
    n = int(SAMPLE_RATE * duration)
    samples = []
    bpm_interval = 60.0 / 145.0  # 145 BPM
    for i in range(n):
        t = i / SAMPLE_RATE
        # 4-to-the-floor kick
        beat_t = t % bpm_interval
        kick_env = math.exp(-beat_t * 22.0)
        kick_f = 140.0 * math.exp(-beat_t * 35.0) + 45.0
        kick = math.sin(2 * math.pi * kick_f * beat_t) * kick_env * 0.85
        # Synth chord stab
        stab_t = t % (bpm_interval * 2)
        stab_env = math.exp(-stab_t * 8.0) * 0.35 if stab_t < 0.2 else 0.0
        stab = (
            math.sin(2 * math.pi * 440.0 * t)
            + math.sin(2 * math.pi * 554.3 * t)
            + math.sin(2 * math.pi * 659.2 * t)
        ) * stab_env * 0.25
        samples.append(kick + stab)
    _write_wav(filepath, samples)


def synth_portal_gun(filepath: Path) -> None:
    """Portal-Gun Umschalt-Sound (Fwoomp... Bloop!)."""
    duration = 0.7
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Fast resonant whoosh up then snap down
        env = min(1.0, t / 0.02) * math.exp(-t * 4.5)
        freq = 300.0 + 800.0 * math.sin(math.pi * min(1.0, t / 0.3))
        val = math.sin(2 * math.pi * freq * t) * 0.65
        # High bubble chirp at end
        chirp = math.sin(2 * math.pi * 1450.0 * t) * math.exp(-(t - 0.15) * 35.0) if t >= 0.15 else 0.0
        samples.append((val + chirp * 0.4) * env * 0.85)
    _write_wav(filepath, samples)


def synth_windows_critical(filepath: Path) -> None:
    """Originalgetreuer Windows-Kritischer-Fehler Akkord (BONG!)."""
    duration = 1.1
    n = int(SAMPLE_RATE * duration)
    freqs = [293.66, 369.99, 440.0, 587.33]  # D-Dur / Resonant Chime
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.005) * math.exp(-t * 3.8)
        val = 0.0
        for idx, f in enumerate(freqs):
            val += math.sin(2 * math.pi * f * t) * (0.35 / (idx + 1))
        samples.append(val * env * 0.9)
    _write_wav(filepath, samples)


def synth_keyboard_clatter(filepath: Path) -> None:
    """Rasanter mechanischer Tastatur-Klickteppich (Matrix-Tippen)."""
    duration = 1.8
    n = int(SAMPLE_RATE * duration)
    samples = []
    key_times = [0.05 * k + random.uniform(-0.01, 0.01) for k in range(32)]
    for i in range(n):
        t = i / SAMPLE_RATE
        val = 0.0
        for kt in key_times:
            dt = t - kt
            if 0 <= dt < 0.04:
                val += math.exp(-dt * 120.0) * math.sin(2 * math.pi * (1800.0 + (kt % 3) * 400.0) * dt) * 0.3
        samples.append(math.tanh(val) * 0.75)
    _write_wav(filepath, samples)


def synth_party_horn(filepath: Path) -> None:
    """Party-Tröte Tröööt."""
    duration = 0.95
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.04) * (1.0 if t < 0.7 else math.exp(-(t - 0.7) * 14.0))
        freq = 320.0 + 40.0 * math.sin(2 * math.pi * 14.0 * t)
        val = (
            math.sin(2 * math.pi * freq * t)
            + 0.5 * math.sin(4 * math.pi * freq * t)
            + 0.3 * math.sin(6 * math.pi * freq * t)
        )
        samples.append(math.tanh(val * 1.8) * env * 0.7)
    _write_wav(filepath, samples)


def synth_boing(filepath: Path) -> None:
    """Cartoon Boing-Feder-Sound."""
    duration = 0.75
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 4.2)
        freq = 160.0 + 520.0 * (1.0 - math.exp(-t * 7.0)) + 35.0 * math.sin(2 * math.pi * 32.0 * t)
        val = math.sin(2 * math.pi * freq * t) * env * 0.8
        samples.append(val)
    _write_wav(filepath, samples)


def synth_snail_slurp(filepath: Path) -> None:
    """Träges Schnecken-Schleim Saug-Geräusch (Sluuurp)."""
    duration = 1.3
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.2) * math.exp(-t * 2.2)
        freq = 120.0 + 80.0 * math.sin(2 * math.pi * 4.0 * t)
        mod = 1.0 + 0.5 * math.sin(2 * math.pi * 42.0 * t)
        noise = (random.random() * 2.0 - 1.0) * 0.25
        val = (math.sin(2 * math.pi * freq * t * mod) * 0.5 + noise) * env * 0.7
        samples.append(val)
    _write_wav(filepath, samples)


def synth_f1_whoosh(filepath: Path) -> None:
    """Formel-1 Doppler-Vorbeirauschen (Nyyyeuuuum)."""
    duration = 1.6
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Doppler shift from 850 Hz to 240 Hz
        progress = t / duration
        freq = 850.0 - 610.0 / (1.0 + math.exp(-(progress - 0.45) * 16.0))
        env = math.exp(-((progress - 0.45) * 3.2) ** 2)
        val = (
            math.sin(2 * math.pi * freq * t)
            + 0.4 * math.sin(4 * math.pi * freq * t)
            + 0.2 * math.sin(6 * math.pi * freq * t)
        )
        samples.append(math.tanh(val * 1.5) * env * 0.85)
    _write_wav(filepath, samples)


def synth_whip_whistle(filepath: Path) -> None:
    """Schnelles Pfeif-Zischen / Digital Whip-Whistle (Fwwwwiiiii-SCHT!)."""
    duration = 0.45
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        progress = t / duration
        env = min(1.0, t / 0.02) * math.exp(-t * 7.5)
        # Rising high-velocity whistle then abrupt snap
        freq = 1400.0 + 3800.0 * (progress ** 2.2)
        whistle = math.sin(2 * math.pi * freq * t) * 0.65
        noise = (random.random() * 2.0 - 1.0) * 0.35 * (progress ** 1.5)
        samples.append((whistle + noise) * env * 0.9)
    _write_wav(filepath, samples)


def synth_access_denied(filepath: Path) -> None:
    """Access Denied Buzzer (Zwei kurze, tiefe Dissonanz-Töne)."""
    duration = 0.65
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Two buzz pulses
        pulse = 1.0 if (0.02 <= t <= 0.24) or (0.34 <= t <= 0.56) else 0.0
        f1, f2 = 185.0, 196.0  # Dissonant minor second buzz
        sq1 = 1.0 if math.sin(2 * math.pi * f1 * t) >= 0 else -1.0
        sq2 = 1.0 if math.sin(2 * math.pi * f2 * t) >= 0 else -1.0
        samples.append((sq1 * 0.35 + sq2 * 0.35) * pulse)
    _write_wav(filepath, samples)


def synth_key_jam(filepath: Path) -> None:
    """Mechanisches Tasten-Klemmen / Verkeilen."""
    duration = 0.6
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # Metallic thud followed by fast stuck chatter
        thud = math.exp(-t * 28.0) * math.sin(2 * math.pi * 320.0 * t) * 0.7
        chatter = 0.0
        for k in range(5):
            dt = t - (0.05 + k * 0.06)
            if 0 <= dt < 0.03:
                chatter += math.exp(-dt * 90.0) * math.sin(2 * math.pi * 1400.0 * dt) * 0.25
        samples.append(thud + chatter)
    _write_wav(filepath, samples)


def synth_jumpscare_screech(filepath: Path) -> None:
    """Harsh Jumpscare Screech / Schock-Kreischen."""
    duration = 1.6
    n = int(SAMPLE_RATE * duration)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.01) * (1.0 if t < 0.8 else math.exp(-(t - 0.8) * 4.0))
        # Dissonant FM cluster
        f1 = 880.0 + 350.0 * math.sin(2 * math.pi * 45.0 * t)
        f2 = 1175.0 + 400.0 * math.sin(2 * math.pi * 58.0 * t)
        f3 = 1480.0 + 500.0 * math.sin(2 * math.pi * 67.0 * t)
        val = (
            math.sin(2 * math.pi * f1 * t)
            + math.sin(2 * math.pi * f2 * t)
            + math.sin(2 * math.pi * f3 * t)
        ) / 3.0
        noise = (random.random() * 2.0 - 1.0) * 0.45
        samples.append(math.tanh((val + noise) * 2.5) * env * 0.85)
    _write_wav(filepath, samples)


def ensure_default_sounds(sounds_dir: str | Path, overwrite: bool = False) -> list[str]:
    """Erstellt alle Standard-Sounds, falls sie noch nicht im Ordner liegen."""
    base = Path(sounds_dir)
    base.mkdir(parents=True, exist_ok=True)
    generators = {
        "bonk.wav": synth_bonk,
        "flashbang.wav": synth_flashbang,
        "airhorn.wav": synth_airhorn,
        "evil_laugh.wav": synth_evil_laugh,
        "censor_beep.wav": synth_censor_beep,
        "hype_horn.wav": synth_hype_horn,
        "fuse_switch.wav": synth_fuse_switch,
        "heartbeat.wav": synth_heartbeat,
        "earthquake_rumble.wav": synth_earthquake_rumble,
        "tilt_creak.wav": synth_tilt_creak,
        "bottle_pop.wav": synth_bottle_pop,
        "glitch_static.wav": synth_glitch_static,
        "dimension_warp.wav": synth_dimension_warp,
        "bitcrush_8bit.wav": synth_bitcrush_8bit,
        "rave_drop.wav": synth_rave_drop,
        "portal_gun.wav": synth_portal_gun,
        "windows_critical.wav": synth_windows_critical,
        "keyboard_clatter.wav": synth_keyboard_clatter,
        "party_horn.wav": synth_party_horn,
        "boing.wav": synth_boing,
        "snail_slurp.wav": synth_snail_slurp,
        "f1_whoosh.wav": synth_f1_whoosh,
        "whip_whistle.wav": synth_whip_whistle,
        "access_denied_buzzer.wav": synth_access_denied,
        "key_jam_click.wav": synth_key_jam,
        "jumpscare_screech.wav": synth_jumpscare_screech,
    }
    created = []
    for filename, fn in generators.items():
        target = base / filename
        if overwrite or not target.exists():
            fn(target)
            created.append(filename)
    return created


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "sounds"
    generated = ensure_default_sounds(out)
    print(f"Generated default sounds in {out}: {len(generated)} files.")
