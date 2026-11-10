#!/usr/bin/env python3
"""
Synthesize singing-bowl sounds and save them as a SoundFont (bowls.sf2) that
FluidSynth -- and mushroom_synth.py -- can play.

Programs in the SoundFont (bank 0):
  0  Crystal bowl     struck quartz bowl: almost a pure tone, long ring, slow shimmer
  1  Tibetan bowl     struck metal bowl: inharmonic overtones (~2.76x, 5.2x, 8.3x the
                      fundamental), each split in two so the tone "wah-wahs" as it rings
  2  Crystal drone    rubbed ("sung") crystal bowl, sustained
  3  Tibetan drone    rubbed metal bowl, sustained
  4  Binaural drone   crystal tone with the right ear BINAURAL_HZ above the left;
                      on headphones the two merge into a slow pulse

Usage:  python3 make_bowls.py [output.sf2]      (needs numpy)

Tweak the numbers below and rerun to change the sound.
"""
import math
import struct
import sys

import numpy as np

SR = 32000            # sample rate of the recordings in the SoundFont
BINAURAL_HZ = 6.0     # left/right difference for the binaural drone
LOOP_S = 4.0          # drone loop length; frequencies snap to 1/LOOP_S Hz so loops are seamless
STRUCK_ROOTS = range(45, 100, 6)    # one recording every 6 semitones, A2..D#7
DRONE_ROOTS = range(39, 70, 6)      # D#2..A4
STRUCK_RELEASE_S = 15.0             # how long a bowl keeps ringing after its note ends
STRUCK_LEVEL = 0.08                 # loudness of struck bowls (RMS of the first 1.5 s)
DRONE_LEVEL = 0.10                  # loudness of drones (RMS)
DRONE_ATTACK_S = 2.5
DRONE_RELEASE_S = 5.0

# (overtone ratio, loudness) for each kind of struck bowl
CRYSTAL_PARTIALS = [(1.0, 1.0), (2.71, 0.10), (5.10, 0.025)]
TIBETAN_PARTIALS = [(1.0, 1.0), (2.76, 0.55), (5.18, 0.30), (8.30, 0.14), (11.9, 0.06)]


def midi_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def snap(f):
    """Round a frequency so it completes a whole number of cycles per drone loop."""
    return round(f * LOOP_S) / LOOP_S


def lowpass(x, fc):
    a = math.exp(-2 * math.pi * fc / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


# ------------------------------------------------------------------ struck bowls
def struck(note, kind, rng):
    f0 = midi_hz(note)
    if kind == "crystal":
        partials, length, attack, click, click_fc, pair = CRYSTAL_PARTIALS, 12.0, 0.006, 0.015, 900, 0.35

        def t60(f):
            return min(40.0, max(1.0, 30.0 * (200.0 / f) ** 0.5))

        def split(f):
            return 0.15 + 0.0015 * f
    else:
        partials, length, attack, click, click_fc, pair = TIBETAN_PARTIALS, 10.0, 0.0015, 0.06, 2500, 0.7

        def t60(f):
            return min(35.0, max(0.4, 20.0 * (180.0 / f) ** 0.7))

        def split(f):
            return 0.4 + 0.003 * f

    n = int(length * SR)
    t = np.arange(n) / SR
    y = np.zeros(n)
    for ratio, amp in partials:
        f = f0 * ratio
        if f * 1.15 > 0.45 * SR:          # leave headroom for pitch-shifting up
            continue
        d = split(f)
        env = np.exp(-6.91 * t / t60(f))
        p1, p2 = rng.uniform(0, 2 * math.pi, 2)
        # each mode is a close pair of frequencies -> the slow beating real bowls have
        y += amp * env * (np.sin(2 * math.pi * (f - d / 2) * t + p1)
                          + pair * np.sin(2 * math.pi * (f + d / 2) * t + p2)) / (1 + pair)
    y *= 1 - np.exp(-t / attack)

    m = int(0.12 * SR)                      # mallet "tok"
    burst = lowpass(rng.standard_normal(m) * np.exp(-t[:m] / 0.01), click_fc)
    y[:m] += click * burst / np.max(np.abs(burst))

    fade = int(0.5 * SR)
    y[-fade:] *= np.cos(np.linspace(0, math.pi / 2, fade))
    # match loudness (first 1.5 s) rather than peaks, so crystal and metal sit level
    y *= STRUCK_LEVEL / np.sqrt(np.mean(y[: int(1.5 * SR)] ** 2))
    return y * min(1.0, 0.95 / np.max(np.abs(y)))


# --------------------------------------------------------------------- drones
# (overtone ratio, loudness, beat partner offset Hz, partner loudness)
DRONE_SPECS = {
    "crystal": [(1.0, 1.0, 0.25, 0.3), (2.0, 0.03, 0, 0), (2.71, 0.04, 0.5, 0.5)],
    "tibetan": [(1.0, 1.0, 0.5, 0.45), (2.76, 0.35, 0.75, 0.6), (5.18, 0.12, 1.0, 0.6),
                (8.30, 0.04, 1.25, 0.5)],
    "pure":    [(1.0, 1.0, 0.25, 0.3), (2.71, 0.03, 0, 0)],
}


def drone(f0, kind, rng):
    """One seamless loop: every component completes whole cycles in LOOP_S."""
    n = int(LOOP_S * SR)
    pad = 16
    t = (np.arange(n + 2 * pad) - pad) / SR
    y = np.zeros(len(t))
    for ratio, amp, beat, amp2 in DRONE_SPECS[kind]:
        f = f0 * ratio
        if f * 1.15 > 0.45 * SR:
            continue
        p1, p2 = rng.uniform(0, 2 * math.pi, 2)
        y += amp * np.sin(2 * math.pi * snap(f) * t + p1)
        if beat:
            y += amp * amp2 * np.sin(2 * math.pi * snap(f + beat) * t + p2)
    return y, (pad, pad + n)


def cents_off(f):
    return 1200 * math.log2(snap(f) / f)


# ----------------------------------------------------------- SoundFont writer
def name20(s):
    b = s.encode("ascii")[:19]
    return b + b"\0" * (20 - len(b))


def gen(oper, val):
    return struct.pack("<Hh", oper, int(val))


def gen_range(oper, lo, hi):
    return struct.pack("<HBB", oper, lo, hi)


def timecents(seconds):
    return round(1200 * math.log2(seconds))


KEY_RANGE, PAN, ATTACK, RELEASE, SAMPLE_ID, SAMPLE_MODES, INSTRUMENT = 43, 17, 34, 38, 53, 54, 41


class SoundFont:
    def __init__(self):
        self.pcm = []
        self.pos = 0
        self.samples = []      # shdr records
        self.instruments = []  # (name, [zone gens bytes, ...])

    def sample(self, name, y, root, loop=None, correction=0):
        data = np.clip(np.round(y * 32767), -32768, 32767).astype("<i2").tobytes()
        start = self.pos
        end = start + len(y)
        ls, le = (start + loop[0], start + loop[1]) if loop else (start + 8, end - 8)
        self.pcm.append(data + b"\0" * 92)           # spec: 46 zero points after each sample
        self.pos = end + 46
        self.samples.append(struct.pack("<20sIIIIIBbHH", name20(name), start, end, ls, le,
                                        SR, root, int(round(correction)), 0, 1))
        return len(self.samples) - 1

    def instrument(self, name, zones):
        self.instruments.append((name, zones))

    def write(self, path, title):
        def chunk(cid, data):
            return cid + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")

        def lst(kind, *chunks):
            body = kind + b"".join(chunks)
            return b"LIST" + struct.pack("<I", len(body)) + body

        info = lst(b"INFO",
                   chunk(b"ifil", struct.pack("<HH", 2, 1)),
                   chunk(b"isng", b"EMU8000\0"),
                   chunk(b"INAM", title.encode() + b"\0" * (2 - len(title) % 2)),
                   chunk(b"ISFT", b"make_bowls.py\0"))
        sdta = lst(b"sdta", chunk(b"smpl", b"".join(self.pcm)))

        phdr, pbag, pgen, inst, ibag, igen = [], [], [], [], [], []
        for i, (name, zones) in enumerate(self.instruments):
            phdr.append(struct.pack("<20sHHHIII", name20(name), i, 0, len(pbag), 0, 0, 0))
            pbag.append(struct.pack("<HH", len(pgen), 0))
            pgen.append(gen(INSTRUMENT, i))
            inst.append(struct.pack("<20sH", name20(name), len(ibag)))
            for z in zones:
                ibag.append(struct.pack("<HH", len(igen), 0))
                igen.extend(z)
        phdr.append(struct.pack("<20sHHHIII", name20("EOP"), 0, 0, len(pbag), 0, 0, 0))
        pbag.append(struct.pack("<HH", len(pgen), 0))
        pgen.append(struct.pack("<HH", 0, 0))
        inst.append(struct.pack("<20sH", name20("EOI"), len(ibag)))
        ibag.append(struct.pack("<HH", len(igen), 0))
        igen.append(struct.pack("<HH", 0, 0))
        shdr = self.samples + [struct.pack("<20sIIIIIBbHH", name20("EOS"), 0, 0, 0, 0, 0, 0, 0, 0, 0)]
        nomod = b"\0" * 10

        pdta = lst(b"pdta",
                   chunk(b"phdr", b"".join(phdr)), chunk(b"pbag", b"".join(pbag)),
                   chunk(b"pmod", nomod), chunk(b"pgen", b"".join(pgen)),
                   chunk(b"inst", b"".join(inst)), chunk(b"ibag", b"".join(ibag)),
                   chunk(b"imod", nomod), chunk(b"igen", b"".join(igen)),
                   chunk(b"shdr", b"".join(shdr)))
        body = b"sfbk" + info + sdta + pdta
        with open(path, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def key_zones(roots, i):
    """Each recording covers the notes around it; the ends stretch to cover everything."""
    roots = list(roots)
    lo = 0 if i == 0 else roots[i] - 3
    hi = 127 if i == len(roots) - 1 else roots[i] + 2
    return lo, hi


def main(out="bowls.sf2"):
    rng = np.random.default_rng(7)
    sf = SoundFont()

    for kind, label in (("crystal", "Crystal bowl"), ("tibetan", "Tibetan bowl")):
        zones = []
        for i, root in enumerate(STRUCK_ROOTS):
            sid = sf.sample("%s %d" % (kind[:4], root), struck(root, kind, rng), root)
            lo, hi = key_zones(STRUCK_ROOTS, i)
            zones.append([gen_range(KEY_RANGE, lo, hi), gen(RELEASE, timecents(STRUCK_RELEASE_S)),
                          gen(SAMPLE_MODES, 0), gen(SAMPLE_ID, sid)])
        sf.instrument(label, zones)
        print("built", label)

    drone_env = [gen(ATTACK, timecents(DRONE_ATTACK_S)), gen(RELEASE, timecents(DRONE_RELEASE_S)),
                 gen(SAMPLE_MODES, 1)]
    for kind, label in (("crystal", "Crystal drone"), ("tibetan", "Tibetan drone")):
        zones = []
        for i, root in enumerate(DRONE_ROOTS):
            f0 = midi_hz(root)
            y, loop = drone(f0, kind, rng)
            y *= DRONE_LEVEL / np.sqrt(np.mean(y ** 2))
            sid = sf.sample("%sdrn %d" % (kind[0], root), y, root, loop, -cents_off(f0))
            lo, hi = key_zones(DRONE_ROOTS, i)
            zones.append([gen_range(KEY_RANGE, lo, hi)] + drone_env + [gen(SAMPLE_ID, sid)])
        sf.instrument(label, zones)
        print("built", label)

    zones = []
    for i, root in enumerate(DRONE_ROOTS):
        f0 = midi_hz(root)
        left, loop = drone(f0, "pure", rng)
        right, _ = drone(f0 + BINAURAL_HZ, "pure", rng)
        scale = 0.71 * DRONE_LEVEL / np.sqrt(np.mean(left ** 2))   # two sides -> each a bit quieter
        lo, hi = key_zones(DRONE_ROOTS, i)
        for side, y, pan in (("L", left, -500), ("R", right, 500)):
            sid = sf.sample("bin%s %d" % (side, root), y * scale, root, loop, -cents_off(f0))
            zones.append([gen_range(KEY_RANGE, lo, hi), gen(PAN, pan)] + drone_env
                         + [gen(SAMPLE_ID, sid)])
    sf.instrument("Binaural drone", zones)
    print("built Binaural drone")

    sf.write(out, "Singing Bowls")
    print("wrote", out)


if __name__ == "__main__":
    main(*sys.argv[1:2])
