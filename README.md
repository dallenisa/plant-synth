# Plant Singing Bowl

A DIY biodata-sonification instrument in the spirit of PlantWave: two electrodes on a leaf, a
Circuit Playground Express that measures how easily electricity flows through the plant, and a
Raspberry Pi that turns those changes into sound.

The default sound is a **gently resonating singing bowl**: a warm, continuously "rubbed" bowl whose
brightness, overtones and loudness follow the plant, with a soft mallet strike when someone touches
a leaf. A second, **classic engine** plays the plant as MIDI notes through a General MIDI synth.

```
leaf ──clips──► Circuit Playground Express ──USB-MIDI──► Raspberry Pi 3 ──3.5 mm──► speaker
                (measures the plant, buttons,             (bowl_resonator.py  or
                 lights, MIDI out)                          mushroom_synth.py)
```

> **What you are hearing.** The board measures the electrical resistance between two electrodes on
> the leaf. Much of that signal comes from the contacts themselves: moisture, temperature, mains hum,
> and electrode chemistry. Touching the plant changes it a lot. How that becomes sound is an artistic
> choice, not the plant "speaking" (PlantWave says the same about its own device).

---

## Contents

| Path | Goes on | What it is |
|---|---|---|
| `circuitplayground/code.py` | CIRCUITPY drive | Settings for the board (starting preset, resistor value, scale for the classic engine) |
| `circuitplayground/mushroom.mpy` | CIRCUITPY drive | The board's engine, precompiled for **CircuitPython 10.x** |
| `circuitplayground/mushroom.py` | (source only) | Source of `mushroom.mpy`. **Don't copy it to the board** (see FAQ) |
| `pi/bowl_resonator.py` | Pi home folder | Singing-bowl engine (modal synthesis, numpy, plays through `aplay`) |
| `pi/bowl_settings.txt` | Pi home folder | Settings for the bowl engine |
| `pi/mushroom_synth.py` | Pi home folder | Classic engine: MIDI notes through FluidSynth |
| `pi/make_bowls.py` | anywhere | Builds `bowls.sf2`, the singing-bowl sounds used by the classic engine |
| `pi/systemd/*.service` | `/etc/systemd/system/` | Start either engine automatically at boot |

---

## Hardware

- Adafruit **Circuit Playground Express** (CircuitPython 10.x)
- **Raspberry Pi 3 Model B** with Raspberry Pi OS (64-bit), and powered speakers or headphones on its 3.5 mm jack
- A **1 MΩ resistor** (or ten 100 kΩ in series), a few jumper wires, two **alligator clips**
- Electrodes: small squares of **aluminium foil** folded over the leaf, plus a little **damp paper
  towel** under each for a good contact. For mushrooms, push jumper-wire pins into the stem instead.

### Wiring

```
3.3V pad ──[ 1 MΩ ]── A1 pad ── clip ──► electrode 1 (foil on the leaf)
GND pad  ────────────────────── clip ──► electrode 2 (foil on another part of the leaf/plant)
```

The plant is the lower half of a voltage divider. Set `R_FIXED` in `code.py` to the resistor's value.
The board prints the plant's resistance every 2 s; a reading between about 0.3 and 0.8 works best.

---

## Setup

### 1. Circuit Playground Express

1. Install **CircuitPython 10.x** (double-press reset, drag the `.uf2` from circuitpython.org onto `CPLAYBOOT`).
2. Copy `code.py` and `mushroom.mpy` from `circuitplayground/` onto the **CIRCUITPY** drive.
   If `neopixel` is reported missing, copy `neopixel.mpy` from the Adafruit library bundle into `CIRCUITPY/lib`.
3. In the **Mu** editor choose **CircuitPython** mode, open **Serial**, press **Ctrl+D**. You should see
   `Mushroom MIDI running ... free RAM ...` and a status line every 2 s.

### 2. Raspberry Pi

Flash **Raspberry Pi OS (64-bit)** with Raspberry Pi Imager: device *Raspberry Pi 3*, set a hostname
(e.g. `plantsynth`), user, Wi-Fi and **enable SSH**. Then, over SSH (`ssh <user>@<hostname>.local`):

```bash
sudo apt update
sudo apt install -y fluidsynth fluid-soundfont-gm python3-mido python3-rtmidi
python3 -m venv --system-site-packages ~/mushroom
~/mushroom/bin/pip install pyfluidsynth
amixer -c Headphones sset PCM 100% && sudo alsactl store      # full headphone-jack level
```

Get the files from `pi/` into your home folder on the Pi, either by cloning on the Pi:

```bash
git clone https://github.com/dallenisa/plant-synth.git ~/plant-synth
cp ~/plant-synth/pi/*.py ~/plant-synth/pi/bowl_settings.txt ~/plant-synth/pi/systemd/*.service ~/
```

or by copying from **PowerShell on a Windows PC**, run inside the repo's `pi` folder (not inside the SSH session):

```powershell
scp bowl_resonator.py bowl_settings.txt mushroom_synth.py make_bowls.py systemd/plantbowl.service systemd/plantsynth.service <user>@<hostname>.local:~/
```

Optional, for the classic engine's bowl presets: `~/mushroom/bin/python make_bowls.py bowls.sf2`
(a few seconds on a desktop, longer on a Pi 3).

Plug the Circuit Playground into a Pi USB port and try it:

```bash
~/mushroom/bin/python bowl_resonator.py --device plughw:CARD=Headphones
```

### 3. Start at boot

Edit the username in the service file if yours isn't `plantsynth`, then:

```bash
sudo cp plantbowl.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plantbowl
journalctl -u plantbowl -f          # watch it (Ctrl+C stops watching, not the sound)
```

**Switching engines** (only one can use the sound card):

```bash
sudo systemctl disable --now plantbowl && sudo systemctl enable --now plantsynth   # classic
sudo systemctl disable --now plantsynth && sudo systemctl enable --now plantbowl   # bowl
```

Shut down with `sudo shutdown now` and wait for the green light to stop before unplugging.

---

## Using it

### Buttons and lights

**Button A** = next preset, **B** = previous. After a press, the number of lit pixels shows the preset,
and the board remembers it after power-off. A slow dot follows the signal, pixels flash on events,
and **all pixels dim red** means no usable signal (check the clips or contacts).

| # | Colour | Bowl engine | Classic engine |
|---|---|---|---|
| 1 | green | Tibetan metal bowl on the `root` setting, with a fifth and an octave | your scale from `code.py`, vibraphone and warm pad |
| 2 | violet | Diatonic: crystal bowls on C (C, G, C) | C major, crystal bowls |
| 3 | amber | Pentatonic: Tibetan bowls on C (C, G, A) | C pentatonic, Tibetan bowls |
| 4 | blue | Endocrine: crystal bowls on C♯ (C♯, G♯, D♯) | C♯ D♯ F♯ G♯ A♯, crystal bowls |
| 5 | pink | Chromatic: Tibetan bowls in open fourths (C, F, B♭) | all 12 notes, Tibetan bowls |
| 6 | teal | Triad: crystal C-E-G, binaural hum (5 Hz between the ears; use headphones) | C-E-G, crystal bowls and binaural drone |

### How the plant controls the bowl

| The plant… | The bowl… |
|---|---|
| is calm | hums softly (`hum`) and drifts a few cents in pitch and tone with the slow baseline |
| is touched (a clear touch dips the reading 10-15% or more) | answers with a soft mallet strike about 0.3 s later |
| is being handled | brightens, a second bowl blooms in, and the sound swells over a few seconds |
| changes moderately | occasional softer strikes and rare muted wooden touches |
| loses contact | the rubbing stops and the bowl simply rings out |

All thresholds are relative to the plant's own everyday noise, which the engine learns only from
calm moments, so a damp leaf reading 0.3 and a waxy orchid reading 0.95 both work, and handling the
plant doesn't make it less sensitive.

### The status line (every 10 s)

```
electrode 0.5480 (range 0.5316-0.5705, typical change 0.00251) | preset 5 | rub 0.63 | brightness 0.27 | pitch -4.0 c | strikes 1, wood 0 | cpu 22%
```

`electrode` = smoothed reading (range over the last 10 s); `typical change` = the plant's everyday
noise; `rub` = how hard the bowl is being "rubbed" (0-1); `cpu` = share of one Pi core (keep it under ~80%).

### Bowl settings (`bowl_settings.txt`)

| Setting | Default | What it does |
|---|---|---|
| `sensitivity` | 1.0 | How strongly touches swell the bowl and how small a touch can strike (1–3). Safe to raise: it never drops below the plant's own noise (see FAQ) |
| `smoothing` | 2.0 | Seconds of smoothing on slow tone changes |
| `hum` | 0.1 | Resting hum level: 0 = silent until touched, 0.3 = steady drone |
| `brightness` | 0.40 | 0 dark/warm … 1 bright/shimmery |
| `resonance` | 20 | Seconds the main bowl rings |
| `root` | 50 | Pitch of preset 1 and octave of the others (MIDI note: 50 = D3, 57 = A3, 60 = C4) |
| `pitch_drift` | 6 | Cents of gentle pitch movement (0 = fixed) |
| `strike_density` | 0.35 | 0 = no strikes … 1 = most frequent |
| `wood` | 0.4 | Level of rare wooden touches (0 = none) |
| `reverb` | 0.25 | 0 dry … 0.6 spacious |
| `volume` | 0 | dB; a limiter prevents clipping |

Apply changes with `sudo systemctl restart plantbowl`. Any setting can also be given on the command
line (`--hum 0.2`), which overrides the file.

**Darker:** `brightness 0.2`, `pitch_drift 3`. **Softer:** `volume -6`, `hum 0.05`.
**Less percussive:** `strike_density 0.1`, `wood 0`. **More spacious:** `resonance 30`, `reverb 0.4`.

---

## How it works

**Board (`mushroom.py`).** Reads A1 at 100 Hz with 16x oversampling, filters it, and sends over USB-MIDI:

| MIDI | Meaning | Used by |
|---|---|---|
| ch 1 notes, CC1 | melody notes on sudden changes; distance from baseline | classic |
| ch 2 notes, CC11 | drone that moves with the slow baseline; its swell | classic |
| ch 15 pitch bend | the smoothed reading itself, 14-bit, 25x per second | bowl |
| ch 16 program change | current preset (re-sent every 5 s) | both |

Out-of-range readings shorter than about 1 s are ignored rather than treated as a lost contact.
The engine is precompiled to `.mpy` because the board's 32 KB of RAM is too small to compile it.

**Bowl engine (`bowl_resonator.py`).** A modal synthesizer: each bowl is a set of resonators at
slightly inharmonic mode ratios (Tibetan ×1, 2.76, 5.18, 8.3; crystal ×1, 2.71, 5.1), each mode a
close pair of frequencies so it beats slowly. A coherent "rubbing" drive at each resonance follows
the plant, and strikes are 35-41 ms felt-mallet envelopes. Each audio block is solved exactly in
closed form with numpy (no per-sample Python loops), mixed through a soft partitioned-convolution
reverb and a limiter, and played via `aplay` at 24 kHz (everything audible here is below 5 kHz).
Preset changes crossfade over a few seconds. About 20-35% of one Pi 3 core.

**Testing without a plant:** `--simulate` uses a clearly labelled synthetic signal; `--render out.wav
--seconds 120` writes audio offline; `--log file.csv` records the reading and every event, and
`--input-csv file.csv` replays it.

**Classic engine (`mushroom_synth.py`).** Plays the board's notes through FluidSynth with the General
MIDI soundfont, switching to `bowls.sf2` (sampled crystal and Tibetan bowls) for presets 2-6.

---

## FAQ / troubleshooting

**No sound, and the log says `Failed to open the "default" audio device`.**
Over SSH there is no desktop audio session. Send audio straight to the jack with
`--device plughw:CARD=Headphones` (`aplay -l` lists the cards).

**It's very quiet.**
Run `amixer -c Headphones sset PCM 100% && sudo alsactl store` (the level can reset on reboot), raise
`volume` (+6 to +10), and make sure the speaker is powered. Small speakers barely reproduce low notes,
so try `root = 57` (A3) or 60 (C4).

**The board shows `MemoryError` and nothing runs.**
The Circuit Playground can't compile the engine itself. Use `mushroom.mpy`, and make sure
`mushroom.py` is **not** on CIRCUITPY (the board prefers the `.py` if both are present).
`mushroom.mpy` is built for CircuitPython 10.x; for another major version, rebuild it with the matching
`mpy-cross mushroom.py`.

**The board's reading is above 0.9, or says `NO CONTACT`.**
The contact is too dry. Put damp paper towel under the foil and clip onto the flat part of the leaf.
If the plant's resistance is still far above the resistor, add resistance in series and update `R_FIXED`.

**All the pixels go red and the sound drops out when I touch the plant.**
The reading briefly jumps out of range: the leaf or clip moves, or your hand adds mains hum. Blips under
about 1 s are ignored. If it keeps happening, re-wet the contacts and tape the wires so the clips don't
pull on the leaf.

**The bowl engine says `connected, but no electrode data`.**
The board has older firmware. Copy the current `mushroom.mpy` to CIRCUITPY.

**There's a constant bowl sound.**
Lower `hum` (0 = silent until the plant is touched). If it started after raising `sensitivity`,
update `bowl_resonator.py`: older versions let the plant's resting noise through at higher settings.

**Touches aren't sensitive enough.**
Raise `sensitivity` (1 to 3). Touches then swell the bowl more and smaller touches can strike it. The
engine never sets its thresholds below the noise it learns from the plant at rest, so a higher value
doesn't add hum or strikes when nobody is touching. Measured in simulation, with a leaf whose reading
wobbles about ±0.02 at rest and `hum = 0.1`:

| `sensitivity` | bowl level at rest | swell from a touch that dips the reading 5% / 7% / 10% | strikes per 10 touches that dip 7% / 10% / 15% | strikes with nobody touching |
|---|---|---|---|---|
| 1 | 0.12 | 0.30 / 0.44 / 0.61 | 2 / 8 / 10 | about 2 per hour |
| 2 | 0.14 | 0.45 / 0.65 / 0.82 | 2 / 9 / 10 | about 2 per hour |
| 3 | 0.15 | 0.56 / 0.77 / 0.90 | 2 / 9 / 10 | about 2 per hour |

Levels run from 0 (silent) to 1 (full). Touches that move the reading less than the plant's own noise
can't be told apart from it at any setting, so they swell the bowl without striking it. Less noise at
the contacts (damp, still, away from power cables) makes lighter touches register.

**The buttons don't change the sound.**
They change presets in both engines (see the table above). Check the log shows `Preset N: ...` when you
press. If not, the board may be running older firmware.

**Crackles or dropouts.**
Check `cpu` in the status line. Above ~80%, stop other programs, or lower `resonance` / switch off `wood`.

**`ssh: Could not resolve hostname`.**
Use `user@hostname.local` (with `@`, not a dot) or the Pi's IP address (`hostname -I` on the Pi,
or your router's device list).

**`scp` says `Could not resolve hostname c:`.**
You ran `scp` inside the SSH session on the Pi. Run it in PowerShell on the PC. If your browser saved
a second download as `file (1).py`, quote that name.

**Pasting a multi-line command into the terminal breaks it.**
Paste blocks without leading spaces, or use the ready-made files in `pi/systemd/` with `sudo cp`.

**The Pi keeps dropping off Wi-Fi.**
Turn off Wi-Fi power saving:
`sudo nmcli connection modify "$(nmcli -g NAME connection show --active | head -1)" 802-11-wireless.powersave 2 && sudo reboot`

**Which Mu mode?** CircuitPython. **Which Pi?** A Pi 3 (headphone jack, 4 USB ports); a Pi Zero has no
analog audio and too little CPU for the synth.

---

## Credits

Inspired by [PlantWave](https://plantwave.com) and the open-source
[Biodata Sonification](https://www.tindie.com/products/e4progress/biodata-sonification-midi-interface-arduino-shield/)
project by Electricity for Progress, whose design measures the plant with a 555 timer
(more sensitive than the voltage divider used here).

---

## License

Released under the [MIT License](LICENSE): free to use, modify and share, as long as the copyright
notice is kept.
