# mushroom.py -- the engine behind code.py (Circuit Playground Express, CircuitPython 10)
#
# The board only has 32 KB of RAM -- too little to compile this file itself.
# Copy the precompiled mushroom.mpy to the CIRCUITPY drive, NOT this .py file
# (if both are there, the board picks the .py and runs out of memory again).
# To rebuild after editing:  mpy-cross mushroom.py   (use CircuitPython 10.x mpy-cross)
#
# How it works: the plant/mushroom is the bottom half of a voltage divider
# (3.3V -> R_FIXED -> A1 -> electrodes -> GND). As its conductance shifts, the
# voltage on A1 moves, and run() turns those movements into USB-MIDI:
#   melody channel  notes triggered by sudden changes
#   drone channel   a held note that shifts when the baseline drifts
#   CC1  (melody)   how far the signal is from its baseline
#   CC11 (drone)    the signal's direction, for a slow swell
#   Program Change on channel 16 = current preset (0-5), so the Pi knows which sounds to use
#   Pitch bend on channel 15 = the smoothed reading itself (0-16383 = 0.0-1.0), 25x/second,
#     sent only while the electrodes have a good signal (used by bowl_resonator.py)
#
# Buttons: A = next preset, B = previous. The choice is remembered after power-off.

import gc
import time
import board
import analogio
import digitalio
import neopixel
import supervisor
import usb_midi

try:
    import microcontroller
    _nvm = microcontroller.nvm
except Exception:
    _nvm = None

# name, root note, scale (semitones), octaves, drone notes (semitones, an octave below root), colour
PRESETS = (
    ("Original", 0, (), 0, (), (40, 255, 85)),       # filled in from code.py's settings
    ("Diatonic", 60, (0, 2, 4, 5, 7, 9, 11), 1, (0, 7, 5, 9), (170, 0, 255)),
    ("Pentatonic", 60, (0, 2, 4, 7, 9), 2, (0, 7, 9, 2), (255, 120, 0)),
    ("Endocrine", 61, (0, 2, 5, 7, 9), 2, (0, 7, 5, 2), (0, 70, 255)),
    ("Chromatic", 60, (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), 1, (0, 7, 5, 2), (255, 0, 110)),
    ("Triad", 60, (0, 4, 7), 2, (0, 7), (0, 220, 200)),
)
PRESET_CH = 15      # MIDI channel 16
DATA_CH = 14        # MIDI channel 15: the reading itself, as 14-bit pitch bend
OPEN_LEVEL = 0.99   # readings above this = electrodes not touching anything
SHORT_LEVEL = 0.01  # readings below this = electrodes touching each other

_PERIOD = 1 << 29
_HALF = 1 << 28
_MASK = _PERIOD - 1


def ticks_diff(a, b):
    return ((a - b + _HALF) & _MASK) - _HALF


def ticks_add(a, d):
    return (a + d) & _MASK


def run(R_FIXED=1000000, SENSITIVITY=2.5, PRESET=1, ROOT=60, SCALE=(0, 2, 4, 7, 9),
        OCTAVES=2, MELODY_CH=0, DRONE_CH=1, DRONE_DEGREES=(0, 3, 4, 1),
        MAX_VOICES=4, MIN_GAP_MS=120, PLOT=False,
        SAMPLE_MS=10, OVERSAMPLE=16, WINDOW_MS=150, FAST_TAU_MS=80,
        SLOW_TAU_MS=8000, RANGE_TAU_MS=20000, NOISE_ALPHA=0.03,
        NOISE_FLOOR=0.00003, WARMUP_MS=4000, DRONE_DRIFT=0.01,
        DRONE_MAX_HOLD_MS=120000):

    now = supervisor.ticks_ms
    adc = analogio.AnalogIn(board.A1)
    pixels = neopixel.NeoPixel(board.NEOPIXEL, 10, brightness=0.1, auto_write=False)
    midi = usb_midi.ports[1]
    btn_a = digitalio.DigitalInOut(board.BUTTON_A)
    btn_a.switch_to_input(pull=digitalio.Pull.DOWN)
    btn_b = digitalio.DigitalInOut(board.BUTTON_B)
    btn_b.switch_to_input(pull=digitalio.Pull.DOWN)

    presets = list(PRESETS)
    presets[0] = ("Original", ROOT, SCALE, OCTAVES,
                  tuple(SCALE[d % len(SCALE)] for d in DRONE_DEGREES), PRESETS[0][5])
    count = len(presets)

    def send(a, b, c):
        midi.write(bytes((a, b, c)))

    def read():
        s = 0
        for _ in range(OVERSAMPLE):
            s += adc.value
        return s / (OVERSAMPLE * 65535.0)

    voices = []          # [note, release_tick]
    drone_note = None
    NOTES = []
    TOP = 0
    drones = []
    color = (0, 0, 0)

    def load(p):
        nonlocal NOTES, TOP, drones, color
        name, root, scale, octs, offs, color = presets[p]
        NOTES = [root + 12 * o + d for o in range(octs) for d in scale]
        NOTES.append(root + 12 * octs)
        TOP = len(NOTES) - 1
        drones = [root - 12 + d for d in offs]
        return name

    def play(n, vel, dur, t):
        for v in voices:
            if v[0] == n:
                send(0x80 | MELODY_CH, n, 0)
                voices.remove(v)
                break
        if len(voices) >= MAX_VOICES:
            send(0x80 | MELODY_CH, voices[0][0], 0)
            voices.pop(0)
        send(0x90 | MELODY_CH, n, vel)
        voices.append([n, ticks_add(t, dur)])

    def set_drone(i):
        nonlocal drone_note
        n = drones[i % len(drones)]
        if drone_note is not None:
            send(0x80 | DRONE_CH, drone_note, 0)
        send(0x90 | DRONE_CH, n, 64)
        drone_note = n

    def all_off():
        nonlocal drone_note
        for v in voices:
            send(0x80 | MELODY_CH, v[0], 0)
        del voices[:]
        if drone_note is not None:
            send(0x80 | DRONE_CH, drone_note, 0)
            drone_note = None
        send(0xB0 | MELODY_CH, 123, 0)
        if DRONE_CH is not None:
            send(0xB0 | DRONE_CH, 123, 0)

    # ---- which preset to start in: the last one picked with the buttons,
    #      unless PRESET in code.py has been changed since then
    preset = max(1, min(count, PRESET)) - 1
    if _nvm is not None:
        try:
            if _nvm[1] == PRESET and 1 <= _nvm[0] <= count:
                preset = _nvm[0] - 1
        except Exception:
            pass
    name = load(preset)

    def announce():
        midi.write(bytes((0xC0 | PRESET_CH, preset)))

    fa = SAMPLE_MS / FAST_TAU_MS
    sa = SAMPLE_MS / SLOW_TAU_MS
    ra = SAMPLE_MS / RANGE_TAU_MS
    ra_warm = SAMPLE_MS / 1000.0

    x = read()
    fast = slow = last_fast = drone_ref = x
    noise = NOISE_FLOOR * 4
    rng = NOISE_FLOOR * 3
    pos = 0.0
    last_note = -1
    drone_i = 0
    contact = None
    bad = 0              # rises while readings are out of range, falls while they're fine
    blips = 0            # out-of-range samples since the last status line
    x_lo, x_hi = 2.0, -1.0
    levels = [0] * 10
    cc1_last = cc11_last = -1
    a_prev = b_prev = False

    t = now()
    t_start = t_next = t_win = t_last_note = t_cc = t_pix = t_log = t_drone = t_ann = t_btn = t_data = t
    t_show = ticks_add(t, 1500)

    all_off()
    announce()
    gc.collect()
    print("Mushroom MIDI running. R_FIXED = %d ohms, sensitivity %.1f, free RAM %d bytes"
          % (R_FIXED, SENSITIVITY, gc.mem_free()))
    print("Preset %d: %s  (button A = next, B = previous)" % (preset + 1, name))

    while True:
        t = now()
        wait = ticks_diff(t_next, t)
        if wait > 0:
            time.sleep(wait / 1000)
            t = now()
        t_next = ticks_add(t_next, SAMPLE_MS)
        if ticks_diff(t, t_next) > 100:          # fell far behind: resync
            t_next = ticks_add(t, SAMPLE_MS)

        x = read()

        # --- buttons: A = next preset, B = previous ---------------------
        a = btn_a.value
        b = btn_b.value
        step = 1 if (a and not a_prev) else (-1 if (b and not b_prev) else 0)
        a_prev = a
        b_prev = b
        if step and ticks_diff(t, t_btn) > 200:
            t_btn = t
            all_off()
            preset = (preset + step) % count
            name = load(preset)
            drone_i = 0
            last_note = -1
            levels = [0] * 10
            announce()
            t_ann = t
            t_show = ticks_add(t, 1500)
            print("Preset %d: %s" % (preset + 1, name))
            if _nvm is not None:
                try:
                    _nvm[0:2] = bytes((preset + 1, PRESET))
                except Exception:
                    pass

        # --- remind the Pi which preset we're on (it may have started later)
        if ticks_diff(t, t_ann) >= 5000:
            t_ann = t
            announce()

        showing = ticks_diff(t_show, t) > 0

        # --- is there a usable signal? ---------------------------------
        # A sample outside OPEN/SHORT is skipped. Only if the reading stays out
        # of range for about a second is the leaf treated as disconnected, so a
        # touch, a swinging leaf or a burst of mains hum no longer mutes everything.
        inrange = SHORT_LEVEL < x < OPEN_LEVEL
        if inrange:
            bad = bad - 1 if bad > 0 else 0
            if x < x_lo:
                x_lo = x
            if x > x_hi:
                x_hi = x
        else:
            bad = bad + 2 if bad < 260 else 260
            blips += 1
        if not contact and inrange and (contact is None or bad == 0):
            contact = True
            print("Signal OK (reading %.3f). Warming up..." % x)
            fast = slow = last_fast = drone_ref = x
            t_start = t
        elif contact is not False and bad >= 200:
            contact = False
            all_off()
            if x >= OPEN_LEVEL:
                print("No contact: check electrodes/clips (reading %.3f)" % x)
            else:
                print("Shorted, or R_FIXED far too large (reading %.3f)" % x)

        # --- status line every 2 s, even with no signal -----------------
        if ticks_diff(t, t_log) >= 2000:
            t_log = t
            if x >= 0.999:
                est = "open circuit"
            else:
                est = "~%d kOhm" % int(R_FIXED * x / (1 - x) / 1000)
            if contact:
                state = "OK"
            elif inrange:
                state = "RECONNECTING"
            else:
                state = "NO CONTACT" if x >= OPEN_LEVEL else "SHORTED"
            rng_txt = "(%.3f-%.3f)" % (x_lo, x_hi) if x_hi >= x_lo else ""
            print("reading %.3f %s | between electrodes %s | %s | blips %d | voices %d"
                  % (x, rng_txt, est, state, blips, len(voices)))
            x_lo, x_hi, blips = 2.0, -1.0, 0

        if not (inrange and contact):
            if ticks_diff(t, t_pix) >= 40:
                t_pix = t
                if showing:
                    for k in range(10):
                        pixels[k] = color if k <= preset else (0, 0, 0)
                elif not contact:
                    pixels.fill((12, 0, 0))
                pixels.show()
            continue

        warm = ticks_diff(t, t_start) > WARMUP_MS

        # --- filters -----------------------------------------------------
        fast += fa * (x - fast)
        slow += sa * (x - slow)
        sig = fast - slow
        rng += (ra if warm else ra_warm) * (abs(sig) - rng)
        if rng < NOISE_FLOOR * 3:
            rng = NOISE_FLOOR * 3
        pos = max(-1.0, min(1.0, sig / (2.5 * rng)))

        # --- stream the smoothed reading itself, 25 times a second, as a
        #     14-bit pitch bend on MIDI channel 15 (for bowl_resonator.py) ----
        if ticks_diff(t, t_data) >= 40:
            t_data = t
            v = int(fast * 16383)
            v = 0 if v < 0 else (16383 if v > 16383 else v)
            midi.write(bytes((0xE0 | DATA_CH, v & 0x7F, v >> 7)))

        # --- melody: look for sudden changes ----------------------------
        if ticks_diff(t, t_win) >= WINDOW_MS:
            t_win = t
            delta = fast - last_fast
            last_fast = fast
            ratio = abs(delta) / noise
            if PLOT:
                print((delta * 10000, SENSITIVITY * noise * 10000, -SENSITIVITY * noise * 10000))
            if warm and ratio > SENSITIVITY and ticks_diff(t, t_last_note) >= MIN_GAP_MS:
                i = int((pos + 1) * 0.5 * TOP + 0.5)
                if NOTES[i] == last_note:    # nudge repeats in the direction of change
                    i = max(0, min(TOP, i + (1 if delta > 0 else -1)))
                vel = max(35, min(115, 35 + int(14 * (ratio - SENSITIVITY))))
                dur = min(3000, 400 + int(200 * ratio))
                play(NOTES[i], vel, dur, t)
                last_note = NOTES[i]
                t_last_note = t
                levels[i * 9 // TOP] = 255
            if warm:
                d = min(abs(delta), 4 * noise)
                noise += NOISE_ALPHA * (d - noise)
            else:
                noise += 0.2 * (abs(delta) - noise)
            if noise < NOISE_FLOOR:
                noise = NOISE_FLOOR

        # --- release finished notes ------------------------------------
        k = len(voices) - 1
        while k >= 0:
            if ticks_diff(t, voices[k][1]) >= 0:
                send(0x80 | MELODY_CH, voices[k][0], 0)
                voices.pop(k)
            k -= 1

        # --- drone follows the slow baseline ---------------------------
        if DRONE_CH is not None and warm:
            if drone_note is None:
                set_drone(drone_i)
                drone_ref = slow
                t_drone = t
            elif abs(slow - drone_ref) > DRONE_DRIFT or ticks_diff(t, t_drone) > DRONE_MAX_HOLD_MS:
                step = 1 if slow >= drone_ref else -1
                drone_i = (drone_i + step) % len(drones)
                set_drone(drone_i)
                drone_ref = slow
                t_drone = t

        # --- continuous controllers ------------------------------------
        if warm and ticks_diff(t, t_cc) >= 100:
            t_cc = t
            c1 = int(127 * abs(pos))
            if abs(c1 - cc1_last) >= 2:
                send(0xB0 | MELODY_CH, 1, c1)
                cc1_last = c1
            if DRONE_CH is not None:
                c11 = 70 + int(57 * (pos + 1) * 0.5)
                if abs(c11 - cc11_last) >= 2:
                    send(0xB0 | DRONE_CH, 11, c11)
                    cc11_last = c11

        # --- lights: preset number after a press; otherwise a dim dot
        #     follows the signal and pixels flash on notes ---------------
        if ticks_diff(t, t_pix) >= 40:
            t_pix = t
            p = int((pos + 1) * 4.5 + 0.5)
            for k in range(10):
                lv = levels[k] * 4 // 5
                levels[k] = lv
                if showing:
                    pixels[k] = color if k <= preset else (0, 0, 0)
                else:
                    br = max(lv, 40 if k == p else 0)
                    pixels[k] = (color[0] * br >> 8, color[1] * br >> 8, color[2] * br >> 8)
            pixels.show()
