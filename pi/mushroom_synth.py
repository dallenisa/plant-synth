#!/usr/bin/env python3
"""
Plant / mushroom synth for Raspberry Pi 3: plays the Circuit Playground's MIDI
through FluidSynth and out of the Pi's headphone jack (or HDMI).

Presets -- switched with the Circuit Playground's buttons (A = next, B = previous):
  1 Original    --melody / --drone General MIDI instruments (vibraphone + warm pad)
  2 Diatonic    crystal bowls, C D E F G A B C
  3 Pentatonic  Tibetan metal bowls, C D E G A
  4 Endocrine   crystal bowls, C# D# F# G# A#
  5 Chromatic   Tibetan metal bowls, all 12 notes
  6 Triad       crystal bowls on C E G, binaural drone (use headphones to hear the pulse)
The bowl sounds live in bowls.sf2, kept in the same folder as this script
(make it with make_bowls.py). Without it, General MIDI stand-ins are used.

One-time setup on Raspberry Pi OS:
    sudo apt install -y fluidsynth fluid-soundfont-gm python3-mido python3-rtmidi
    python3 -m venv --system-site-packages ~/mushroom
    ~/mushroom/bin/pip install pyfluidsynth

Run (Circuit Playground plugged into a Pi USB port):
    ~/mushroom/bin/python mushroom_synth.py -v --device plughw:CARD=Headphones --gain 1.5
    ~/mushroom/bin/python mushroom_synth.py --list          # show MIDI inputs

`aplay -l` lists sound cards; on a Pi 3 the 3.5 mm jack is CARD=Headphones.
If it's quiet: amixer -c Headphones sset PCM 100%  &&  sudo alsactl store

Handy General MIDI programs (0-based) for the Original preset: 8 celesta,
11 vibraphone, 12 marimba, 14 tubular bells, 46 harp, 108 kalimba,
88 new-age pad, 89 warm pad, 91 choir pad, 95 sweep pad.
"""
import argparse
import os
import signal
import sys
import time

import mido
import fluidsynth

HERE = os.path.dirname(os.path.abspath(__file__))
MELODY, DRONE = 0, 1
PRESET_CH = 15          # the Circuit Playground sends its preset as a Program Change on channel 16

# Programs in bowls.sf2, and the General MIDI sounds used if it's missing
CRYSTAL, TIBETAN, CRYSTAL_DRONE, TIBETAN_DRONE, BINAURAL_DRONE = range(5)
GM_STAND_IN = {CRYSTAL: 98, TIBETAN: 14, CRYSTAL_DRONE: 92, TIBETAN_DRONE: 93, BINAURAL_DRONE: 94}

PRESETS = [
    # name, melody program, drone program (None = the --melody / --drone GM instruments)
    ("Original", None, None),
    ("Diatonic - crystal bowls", CRYSTAL, CRYSTAL_DRONE),
    ("Pentatonic - Tibetan bowls", TIBETAN, TIBETAN_DRONE),
    ("Endocrine - crystal bowls", CRYSTAL, CRYSTAL_DRONE),
    ("Chromatic - Tibetan bowls", TIBETAN, TIBETAN_DRONE),
    ("Triad - crystal bowls, binaural drone", CRYSTAL, BINAURAL_DRONE),
]

NOTE_NAMES = "C C# D D# E F F# G G# A A# B".split()


def note_name(n):
    return "%s%d" % (NOTE_NAMES[n % 12], n // 12 - 1)


def find_port(hint):
    for name in mido.get_input_names():
        low = name.lower()
        if hint.lower() in low and "through" not in low:
            return name
    return None


class Player:
    """Turns incoming MIDI into FluidSynth calls, switching sounds per preset."""

    def __init__(self, fs, gm, bowls, melody_prog, drone_prog, verbose=False):
        self.fs, self.gm, self.bowls = fs, gm, bowls
        self.melody_prog, self.drone_prog = melody_prog, drone_prog
        self.verbose = verbose
        self.preset = None
        self.bowl_sounds = False

    def set_preset(self, n):
        if n == self.preset or not 0 <= n < len(PRESETS):
            return
        name, mel, dr = PRESETS[n]
        fs = self.fs
        for ch in (MELODY, DRONE):
            fs.cc(ch, 123, 0)                 # let current notes ring out
        if mel is None:
            fs.program_select(MELODY, self.gm, 0, self.melody_prog)
            fs.program_select(DRONE, self.gm, 0, self.drone_prog)
        else:
            for ch, prog in ((MELODY, mel), (DRONE, dr)):
                if self.bowls is not None:
                    fs.program_select(ch, self.bowls, 0, prog)
                else:
                    fs.program_select(ch, self.gm, 0, GM_STAND_IN[prog])
            fs.cc(MELODY, 1, 0)               # no vibrato on bowls
        self.bowl_sounds = mel is not None
        self.preset = n
        print("Preset %d: %s" % (n + 1, name), flush=True)

    def handle(self, msg):
        fs = self.fs
        if msg.type == "program_change" and msg.channel == PRESET_CH:
            self.set_preset(msg.program)
        elif msg.type == "note_on" and msg.velocity > 0:
            vel = msg.velocity
            if self.bowl_sounds and msg.channel == MELODY:
                vel = 60 + vel * 67 // 127    # soft notes stay audible; bowls don't whisper
            fs.noteon(msg.channel, msg.note, vel)
            if self.verbose:
                print("ch%d  %-4s vel %d" % (msg.channel + 1, note_name(msg.note), vel))
        elif msg.type in ("note_on", "note_off"):
            fs.noteoff(msg.channel, msg.note)
        elif msg.type == "control_change":
            if self.bowl_sounds and msg.channel == MELODY and msg.control == 1:
                return                         # mod wheel = vibrato, which makes bowls wobble
            fs.cc(msg.channel, msg.control, msg.value)
        elif msg.type == "program_change":
            fs.program_change(msg.channel, msg.program)

    def all_off(self):
        for ch in range(16):
            self.fs.cc(ch, 123, 0)


def load_soundfonts(fs, gm_path, bowls_path):
    gm = fs.sfload(gm_path)
    if gm == -1:
        sys.exit("Couldn't load soundfont %s (sudo apt install fluid-soundfont-gm)" % gm_path)
    bowls = None
    if os.path.exists(bowls_path):
        bowls = fs.sfload(bowls_path)
        if bowls == -1:
            print("Couldn't load %s -- using General MIDI stand-ins for the bowls" % bowls_path)
            bowls = None
    else:
        print("%s not found -- using General MIDI stand-ins for the bowls" % bowls_path)
    return gm, bowls


def main():
    ap = argparse.ArgumentParser(description="Play plant/mushroom MIDI through FluidSynth")
    ap.add_argument("--soundfont", default="/usr/share/sounds/sf2/FluidR3_GM.sf2")
    ap.add_argument("--bowls", default=os.path.join(HERE, "bowls.sf2"),
                    help="singing-bowl SoundFont (default: bowls.sf2 next to this script)")
    ap.add_argument("--preset", type=int, default=1,
                    help="preset to use until the Circuit Playground reports its own (1-6)")
    ap.add_argument("--port", default="circuit",
                    help="part of the MIDI input name to look for (default: circuit)")
    ap.add_argument("--driver", default="alsa", help="alsa, pulseaudio, pipewire, ...")
    ap.add_argument("--device", default=None, help="audio device, e.g. plughw:CARD=Headphones")
    ap.add_argument("--melody", type=int, default=11, help="GM program for the Original preset's melody")
    ap.add_argument("--drone", type=int, default=89, help="GM program for the Original preset's drone")
    ap.add_argument("--gain", type=float, default=0.5)
    ap.add_argument("--list", action="store_true", help="list MIDI inputs and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="print notes as they play")
    args = ap.parse_args()

    if args.list:
        for name in mido.get_input_names():
            print(name)
        return

    # Let "systemctl stop" shut down cleanly (runs the finally: block below)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    fs = fluidsynth.Synth(gain=args.gain)
    fs.start(driver=args.driver, device=args.device)
    if not getattr(fs, "audio_driver", True):
        # Exit with an error so a boot-time service retries until audio is ready
        sys.exit("Couldn't open audio (driver %s, device %s)" % (args.driver, args.device))
    gm, bowls = load_soundfonts(fs, args.soundfont, args.bowls)
    try:
        fs.set_reverb(0.85, 0.3, 0.9, 0.7)    # roomsize, damping, width, level
    except Exception:
        pass                                   # older pyfluidsynth: default reverb is fine

    player = Player(fs, gm, bowls, args.melody, args.drone, args.verbose)
    player.set_preset(max(1, min(len(PRESETS), args.preset)) - 1)

    waiting_said = False
    try:
        while True:
            name = find_port(args.port)
            if not name:
                if not waiting_said:
                    print("Waiting for a MIDI input matching %r... (inputs: %s)"
                          % (args.port, mido.get_input_names() or "none"))
                    waiting_said = True
                time.sleep(2)
                continue
            waiting_said = False
            print("Listening to", name)
            with mido.open_input(name) as inport:
                last_check = time.monotonic()
                while True:
                    for msg in inport.iter_pending():
                        player.handle(msg)
                    if time.monotonic() - last_check > 2:
                        last_check = time.monotonic()
                        if name not in mido.get_input_names():
                            print("Lost", name)
                            break
                    time.sleep(0.002)
            player.all_off()
    except KeyboardInterrupt:
        pass
    finally:
        player.all_off()
        fs.delete()


if __name__ == "__main__":
    main()
