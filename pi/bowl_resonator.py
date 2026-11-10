#!/usr/bin/env python3
"""
bowl_resonator.py -- the plant plays one gently resonating singing bowl.

LIVE MODE (normal operation)
  The Circuit Playground (mushroom.mpy with the reading stream) sends the smoothed
  electrode reading 25 times a second as a 14-bit pitch bend on MIDI channel 15.
  This script turns it into slow control signals and drives a small modal
  synthesizer, played through ALSA with `aplay`:

    * a main bowl that is continuously "rubbed": a warm fundamental plus three soft,
      slightly inharmonic overtones (x2.76, x5.18, x8.3). Each overtone is a close
      pair of frequencies, so it beats slowly the way real bowls do
    * two smaller bowls, a fifth and an octave above, mostly heard when struck
    * rare, muted wooden touches
    * a soft, diffuse convolution reverb, a gentle limiter and no clipping

  How the plant controls it
    level vs. its own slow baseline -> brightness / overtone balance, a few cents of pitch drift,
                                       how much the fifth bowl sings along (heard within ~1 s)
    amount of movement ("activity") -> how hard the bowl is rubbed: brightness and shimmer at
                                       once, loudness swelling over several seconds like a real bowl
    strong, sudden changes          -> an occasional soft felt-mallet strike (threshold,
                                       hysteresis and a minimum gap; blips are ignored)
    electrodes off / board unplugged -> rubbing stops and the bowl simply rings out
  Every scale is relative to the plant's own recent behaviour, so a damp leaf reading
  0.3 and a waxy orchid reading 0.95 both work without retuning.

BUTTONS (Circuit Playground: A = next, B = previous; the pixel count shows the number)
  1 Original    Tibetan metal bowl on the "root" setting, with a fifth and an octave
  2 Diatonic    crystal bowls on C (C, G, C)
  3 Pentatonic  Tibetan bowls on C (C, G, A)
  4 Endocrine   crystal bowls on C# (C#, G#, D#)
  5 Chromatic   Tibetan bowls on C in open fourths (C, F, Bb)
  6 Triad       crystal bowls on C-E-G; the main bowl hums 5 Hz apart in each ear (binaural
                pulse on headphones)
  Presets 2-6 sit in the octave nearest the "root" setting. A change crossfades over a few
  seconds with one soft confirming strike.

SETTINGS
  bowl_settings.txt next to this script; command-line flags override it.

TESTING (not the plant!)
  --simulate          use a clearly labelled synthetic electrode signal
  --input-csv FILE    replay a recorded signal (time_s,reading per line; see --log)
  --render FILE.wav   write audio offline instead of playing it (needs --simulate or --input-csv)

Needs only numpy, mido + python-rtmidi (already installed for mushroom_synth.py) and aplay.
"""
import argparse
import math
import os
import signal
import subprocess
import sys
import time
import wave
from collections import deque

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_CH = 14                    # MIDI channel 15 carries the reading
PRESET_CH = 15                  # MIDI channel 16: the board's preset (its buttons)
SR = 24000                      # the bowls have nothing above ~5 kHz; keeps a Pi 3 comfortable
BLOCK = 1536                    # 64 ms of audio per step (a multiple of 64)

DEFAULTS = {
    "sensitivity": 1.0,     # how strongly changes in the plant move the sound
    "smoothing": 2.0,       # seconds of smoothing on the reading
    "brightness": 0.40,     # 0 dark ... 1 bright
    "resonance": 20.0,      # seconds the main bowl rings (T60 of the fundamental)
    "strike_density": 0.35, # 0 = no strikes ... 1 = most frequent (still sparse)
    "wood": 0.4,            # level of rare wooden touches (0 = off)
    "reverb": 0.25,         # reverb mix
    "volume": 0.0,          # dB
    "root": 50.0,           # tonal centre (MIDI note, 50 = D3)
    "pitch_drift": 6.0,     # cents of gentle pitch movement
    "hum": 0.1,             # resting level of the bowl's hum (0 = silent until the plant is touched)
}


def load_settings(path):
    s = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = (p.strip() for p in line.split("=", 1))
                if k in s:
                    try:
                        s[k] = float(v)
                    except ValueError:
                        print("bowl_settings: ignoring %s = %r" % (k, v))
                else:
                    print("bowl_settings: unknown setting %r" % k)
    return s


def ema(dt, tau):
    return 1.0 - math.exp(-dt / tau)


# ============================================================ electrode input
class InputTracker:
    """Turns the ~25 Hz electrode reading into controls and percussion events.

    Every real touch gets a prompt, clearly audible bowl strike; smaller changes get rarer,
    softer accents. Movement makes the bowl brighter, louder and brings in a second bowl."""

    LOST_AFTER = 1.5        # s without data -> electrodes treated as disconnected

    def __init__(self, s):
        self.sens = max(0.05, s["sensitivity"])
        self.hum = min(1.0, max(0.0, s["hum"]))
        self.smooth = max(0.2, s["smoothing"])
        d = min(1.0, max(0.0, s["strike_density"]))
        self.strikes_on = d > 0
        # thresholds in multiples of the plant's own everyday change (its noise never
        # exceeds about 6 on this scale, whatever its absolute size)
        self.t_hi = 7.0                         # a smaller but real change: occasional soft strike
        self.t_strong = 10.0                    # a clear touch (a dip of ~10% or more): answered at once
        self.t_lo = 2.0                         # must calm below this to re-arm
        self.min_gap = 60.0 / (1 + 7 * d) if d > 0 else 1e9   # for smaller (non-touch) changes
        self.strong_gap = 1.5 + 3.0 * (1 - d)                 # between strikes answering clear touches
        self.wood_gap = max(45.0, 2.5 * self.min_gap)
        self.wood_on = s["wood"] > 0
        self.last_data = -1e9
        self.connected = False
        self.excitation = 0.0
        self.zt = self.act = 0.0
        self.t_last_strike = self.t_last_wood = -1e9
        self.counts = {"strike": 0, "wood": 0}
        self.win_lo, self.win_hi = 9.0, -9.0
        self.n_data = 0
        self._reset(None, 0.0)

    def _reset(self, x, t):
        self.med = deque(maxlen=5)
        self.s_fast = self.s_slow = self.base = x
        self.win = deque()
        self.trend = 0.0
        self.innov_scale = 0.0005
        self.dev_scale = 0.001
        self.jump_scale = 0.0005
        self.act = 1.0
        self.zt = 0.0
        self.ratio = 0.0
        self.ratio_s = 0.0
        self.armed = False
        self.calm_since = None
        self.cand = None
        self.last_peak = 0.0
        self.t_reset = t

    def push(self, t, x):
        """One reading (0..1) at time t (s). Returns a list of (kind, bowl, velocity, delay_s)."""
        if not (0.01 < x < 0.99):                    # outlier / not a real reading
            return []
        if not self.connected or t - self.last_data > self.LOST_AFTER:
            self._reset(x, t)
            self.connected = True
        dt = min(0.2, max(0.01, t - self.last_data))
        self.last_data = t
        self.n_data += 1
        self.win_lo, self.win_hi = min(self.win_lo, x), max(self.win_hi, x)

        self.med.append(x)
        m = sorted(self.med)[len(self.med) // 2]     # 5-sample median: kills short spikes
        self.s_fast += ema(dt, 0.12) * (m - self.s_fast)
        self.s_slow += ema(dt, self.smooth) * (m - self.s_slow)
        settled = t - self.t_reset > 20
        self.base += ema(dt, 45.0 if settled else 8.0) * (self.s_slow - self.base)

        # the plant's own typical wobble, learned only from ordinary moments, so that
        # handling the plant does not make it less sensitive
        dev = self.s_slow - self.base
        if abs(dev) < 2.5 * self.dev_scale or not settled:
            self.dev_scale += ema(dt, 120.0 if settled else 6.0) * (min(abs(dev), 3 * self.dev_scale) - self.dev_scale)
        self.dev_scale = max(self.dev_scale, 0.0005)
        self.zt = math.tanh(self.sens * dev / (2 * self.dev_scale))

        # step detector: the last 0.3 s compared with 0.6-1.1 s ago. A touch is a step that
        # lasts; everyday noise averages out, so smaller touches stand clear of it.
        self.win.append((t, m))
        while t - self.win[0][0] > 1.1:
            self.win.popleft()
        recent = [v for tt, v in self.win if t - tt <= 0.3]
        older = [v for tt, v in self.win if t - tt >= 0.6]
        j = (sum(recent) / len(recent) - sum(older) / len(older)) if older else 0.0
        if abs(j) < 2.5 * self.jump_scale or not settled:
            self.jump_scale += ema(dt, 120.0 if settled else 6.0) * (min(abs(j), 4 * self.jump_scale) - self.jump_scale)
        self.jump_scale = max(self.jump_scale, 0.0002)
        movement = self.sens * abs(j) / self.jump_scale
        self.act += ema(dt, 1.0) * (min(movement, 30.0) - self.act)        # any change brightens/swells
        # strikes listen for *sudden* changes: the step minus its own recent trend. A touch is
        # abrupt; the slow recovery afterwards is a steady trend, so the next touch still stands out.
        innov = j - self.trend
        self.trend += ema(dt, 1.0) * (j - self.trend)
        if abs(innov) < 2.5 * self.innov_scale or not settled:
            self.innov_scale += ema(dt, 120.0 if settled else 6.0) * (min(abs(innov), 4 * self.innov_scale) - self.innov_scale)
        self.innov_scale = max(self.innov_scale, 0.0002)
        self.ratio = self.sens * abs(innov) / self.innov_scale
        j = innov
        self.ratio_s += ema(dt, 0.25) * (self.ratio - self.ratio_s)

        # percussion: threshold + hysteresis + minimum gaps. Re-arming needs the (lightly
        # smoothed) change to settle back toward everyday noise for 0.4 s after a hit.
        events = []
        if self.ratio_s < max(self.t_lo, 0.3 * self.last_peak):
            if self.calm_since is None:
                self.calm_since = t
            elif t - self.calm_since >= 0.4:
                self.armed = True
                self.last_peak = 0.0
        else:
            self.calm_since = None
        if self.cand is None:
            if self.armed and t - self.t_reset > 8 and self.ratio >= self.t_hi:
                self.cand = [self.ratio, j, t]
        else:
            if self.ratio > self.cand[0]:
                self.cand[0], self.cand[1] = self.ratio, j
        if self.cand is not None:
            peak, jj, t0 = self.cand
            clear_touch = peak >= self.t_strong
            # a clear touch is answered at once; smaller changes are followed to their peak
            if clear_touch or self.ratio < 0.7 * peak or t - t0 > 0.5:
                self.cand = None
                n_before = len(events)
                if self.strikes_on and clear_touch and t - self.t_last_strike >= self.strong_gap:
                    vel = min(0.8, 0.45 + 0.1 * math.log2(peak / self.t_strong))
                    events.append(("strike", 0 if jj < 0 else 1, vel, 0.0))
                    if peak >= 2 * self.t_strong:          # a big touch also sets a second bowl ringing
                        events.append(("strike", 1 if jj < 0 else 2, 0.6 * vel, 0.45))
                    self.t_last_strike = t
                    self.counts["strike"] += 1
                elif (self.strikes_on and not clear_touch and peak >= self.t_hi
                      and t - self.t_last_strike >= self.min_gap):
                    vel = min(0.45, 0.25 + 0.08 * math.log2(peak / self.t_hi))
                    events.append(("strike", 0 if jj < 0 else 1, vel, 0.0))
                    self.t_last_strike = t
                    self.counts["strike"] += 1
                elif (self.wood_on and not clear_touch and t - self.t_last_wood >= self.wood_gap
                      and t - self.t_last_strike >= 2.0):
                    # a real but moderate change that came too soon for another strike
                    events.append(("wood", 0, min(0.6, 0.25 + 0.05 * peak), 0.0))
                    self.t_last_wood = t
                    self.counts["wood"] += 1
                if len(events) > n_before:          # only an actual hit needs the calm-down
                    self.armed = False
                    self.last_peak = peak
                    self.calm_since = None
        return events

    def controls(self, t, dt, base_brightness, drift_cents):
        """Synthesis controls for the next audio block."""
        if t - self.last_data > self.LOST_AFTER:
            self.connected = False
        # 0 = everyday noise ... 1 = the plant is being handled (a dead zone ignores ordinary wobble)
        moving = math.tanh(max(0.0, self.act - 2.0) / 4.0) if self.connected else 0.0
        if self.connected:
            target = min(1.0, self.hum + (1.0 - self.hum) * moving + 0.08 * abs(self.zt) * min(1.0, self.hum / 0.3))
        else:
            target = 0.0
        tau = 0.5 if target > self.excitation else (5.0 if self.connected else 3.0)
        self.excitation += ema(dt, tau) * (target - self.excitation)
        e = self.excitation
        z = self.zt if self.connected else 0.0
        # slow drift above/below baseline tints the tone; any movement brightens it
        bright = min(0.98, max(0.02, base_brightness + 0.12 * z + 0.45 * moving + 0.1 * (e - 0.45)))
        return {
            "excitation": e,
            "brightness": bright,
            "cents": drift_cents * z,
            "shimmer": 0.85 + 0.45 * moving,
            "fifth": 0.27 * self.hum + 0.55 * moving + 0.08 * max(0.0, z),   # a second bowl blooms when touched
        }


# ============================================================ the instrument
MATERIALS = {
    # Tibetan / Himalayan metal bowl: warm, slightly inharmonic overtones, noticeable shimmer
    "metal": {"modes": (1.0, 2.76, 5.18, 8.30), "rub": (1.0, 0.42, 0.16, 0.06),
              "strike": (1.0, 0.55, 0.25, 0.10), "split": (0.35, 0.8, 1.5, 2.3),
              "twin": (0.4, 0.6, 0.6, 0.6), "t60": 1.0, "gain": 1.0},
    # quartz crystal bowl: nearly pure tone, longer ring, slow gentle pulsing
    "crystal": {"modes": (1.0, 2.71, 5.10), "rub": (1.0, 0.13, 0.035),
                "strike": (1.0, 0.22, 0.06), "split": (0.22, 0.5, 0.9),
                "twin": (0.3, 0.5, 0.5), "t60": 1.3, "gain": 1.0},
}

# Bowl variations chosen with the Circuit Playground's buttons (same order and lights as before).
#   name, material, pitch class of the main bowl (None = the "root" setting), companion bowls
#   (semitones above the main bowl), binaural hum
PRESETS = [
    ("Original - Tibetan bowl", "metal", None, (7, 12), False),
    ("Diatonic - crystal bowls on C", "crystal", 0, (7, 12), False),
    ("Pentatonic - Tibetan bowls on C", "metal", 0, (7, 9), False),
    ("Endocrine - crystal bowls on C#", "crystal", 1, (7, 14), False),
    ("Chromatic - Tibetan bowls on C, open fourths", "metal", 0, (5, 10), False),
    ("Triad - crystal C-E-G, binaural hum", "crystal", 0, (4, 7), True),
]


class BowlBank:
    WOOD = ((4.53, 0.11, 1.0), (10.2, 0.07, 0.45), (17.6, 0.045, 0.18))  # ratio, T60, amp
    RUB_GAIN = 0.075
    STRIKE_GAIN = 0.12
    WOOD_GAIN = 0.05

    def __init__(self, sr, root_hz, t60, wood_level, material="metal", companions=(7, 12),
                 binaural=False, seed=11):
        m = MATERIALS[material]
        self.sr = sr
        self.rng = np.random.default_rng(seed)
        self.gain = m["gain"]
        bowls = ((0, 1.0, 0.0), (companions[0], 1.3, -0.35), (companions[1], 1.6, 0.35))
        rows = []        # f, bowl, mode, member, amp, pan, t60, split, fixed_split, rub_w, strike_w
        for b, (semi, beat_k, pan) in enumerate(bowls):
            ratio = 2.0 ** (semi / 12.0)
            n_modes = len(m["modes"]) if b == 0 else min(3, len(m["modes"]))   # small bowls: 3 modes
            for k in range(n_modes):
                f = root_hz * ratio * m["modes"][k] * (1 + 0.004 * (b - 1) * k)  # each bowl a little different
                if f * 1.15 > 0.45 * sr:
                    continue
                t = max(1.2, t60 * m["t60"] * (f / root_hz) ** -0.6 * (1.0 if b == 0 else 0.6))
                # each mode = two close frequencies; the weaker twin sets how deep the beating is
                for mem in (0, 1):
                    amp = 1.0 if mem == 0 else m["twin"][k]
                    p = max(-1.0, min(1.0, pan + (-0.22 if mem == 0 else 0.22)))
                    split, fixed = m["split"][k] * beat_k, 0
                    if binaural and b == 0 and k == 0:          # left ear f-2.5 Hz, right ear f+2.5 Hz
                        amp, p, split, fixed = 1.0, (-1.0 if mem == 0 else 1.0), 5.0, 1
                    rows.append((f, b, k, mem, amp, p, t, split, fixed, m["rub"][k], m["strike"][k]))
        for ratio, t, amp in self.WOOD:
            rows.append((root_hz * ratio, 9, 0, 0, amp, 0.1, t, 0.0, 1, 0.0, 0.0))
        a = np.array(rows, dtype=float)
        self.f0, self.bowl, self.mode, self.member = a[:, 0], a[:, 1].astype(int), a[:, 2].astype(int), a[:, 3].astype(int)
        self.mamp, pan, self.t60, self.split = a[:, 4], a[:, 5], a[:, 6], a[:, 7]
        self.fixed_split, self.rub_w, self.strike_w = a[:, 8] > 0, a[:, 9], a[:, 10]
        self.is_wood = self.bowl == 9
        self.wood_level = wood_level
        self.logr = -6.9078 / (self.t60 * sr)
        self.one_minus_r = -np.expm1(self.logr)
        self.panL = np.cos((pan + 1) * np.pi / 4)
        self.panR = np.sin((pan + 1) * np.pi / 4)
        J = len(rows)
        self.y = np.zeros(J, complex)
        self.theta = self.rng.uniform(0, 2 * np.pi, J)
        self.rho = np.zeros(J, complex)
        self.rho_b = np.zeros(4, complex)
        self.bidx = np.where(self.is_wood, 3, self.bowl)
        self.u_prev = np.zeros(J, complex)
        self.pending = []          # [start_sample, env, amps(J complex)]
        self._tables()

    def _tilt(self, b):
        return 2.0 ** ((b - 0.5) * self.mode * 2.0)

    def strike(self, bowl, vel, brightness):
        sel = (self.bowl == bowl)
        hard = min(0.9, brightness * 0.8 + 0.2 * vel)
        amps = np.zeros(len(self.f0), complex)
        w = self.strike_w * 2.0 ** ((hard - 0.5) * self.mode * 1.2)
        amps[sel] = self.STRIKE_GAIN * self.gain * vel * w[sel] * self.mamp[sel]
        amps *= np.exp(1j * self.rng.uniform(0, 2 * np.pi))
        dur = 0.045 - 0.02 * vel                     # 35-41 ms felt mallet: rounded onset
        self._schedule(amps, dur)

    def wood(self, vel):
        amps = np.zeros(len(self.f0), complex)
        amps[self.is_wood] = self.WOOD_GAIN * self.wood_level * vel * self.mamp[self.is_wood]
        self._schedule(amps, 0.004)

    def _schedule(self, amps, dur):
        n = max(8, int(dur * self.sr))
        env = np.hanning(n + 2)[1:-1]
        self.pending.append([0, env / env.sum(), amps])

    def _drive(self, B, c):
        """Rubbing drive at the start and end of the block (it ramps linearly between them),
        pending strike envelopes, and this block's resonator frequencies."""
        bdt = B / self.sr
        # frequencies: gentle pitch drift + beating pairs whose speed follows "shimmer"
        shimmer = np.where(self.fixed_split, 1.0, c["shimmer"])
        detune = np.where(self.member == 0, -0.5, 0.5) * self.split * shimmer
        f = self.f0 * 2.0 ** (c["cents"] / 1200.0) + detune
        f[self.is_wood] = self.f0[self.is_wood]
        omega = 2 * np.pi * f / self.sr
        # the rubbing "hand" wavers a little: mostly shared by all modes of a bowl (so the
        # plant-driven overtone balance stays audible), plus a small independent part
        k = math.exp(-bdt / 1.2)

        def wander(prev, size):
            return k * prev + math.sqrt(1 - k * k) * (self.rng.standard_normal(size) +
                                                      1j * self.rng.standard_normal(size)) / math.sqrt(2)
        self.rho = wander(self.rho, len(f))
        self.rho_b = wander(self.rho_b, 4)
        # only the main bowl is rubbed fully; the first companion hums along on its
        # fundamental; everything else sounds only when struck
        bowl_rub = np.where(self.bowl == 0, 1.0, np.where((self.bowl == 1) & (self.mode == 0), c["fifth"], 0.0))
        amp = (self.RUB_GAIN * self.gain * c["excitation"] * bowl_rub * self.rub_w
               * self._tilt(c["brightness"]) * self.mamp)
        u_new = amp * self.one_minus_r * (0.6 + 0.3 * self.rho_b[self.bidx] + 0.1 * self.rho)
        u0, self.u_prev = self.u_prev, u_new
        # strikes / wooden touches: smooth envelopes, only on the resonators they hit
        hits = {}
        keep = []
        for p in self.pending:
            start, env, amps = p
            seg = env[start:start + B]
            rows = np.nonzero(amps)[0]
            for r_, a_ in zip(rows, amps[rows]):
                h = hits.setdefault(r_, np.zeros(B, complex))
                h[:len(seg)] += a_ * seg
            p[0] += len(seg)
            if p[0] < len(env):
                keep.append(p)
        self.pending = keep
        return u0, u_new, hits, omega

    def _tables(self):
        """Per-resonator tables that only depend on the (fixed) decay rates."""
        n = np.arange(BLOCK)
        lr = np.outer(self.logr, n)
        self.Rn = np.exp(lr)                          # r^n
        self.Rinv = np.exp(-lr)                       # r^-n
        self.S0 = np.cumsum(self.Rinv, axis=1)        # sum_k<=n r^-k
        self.S1 = np.cumsum(self.Rinv * n / BLOCK, axis=1)   # sum_k<=n (k/B) r^-k
        self.lo_n = np.arange(64)
        self.hi_n = np.arange(BLOCK // 64) * 64

    def _solve(self, u0, u1, hits, omega):
        """Exact block solution of y[n] = r e^{iw} y[n-1] + u[n] e^{i(theta + w n)} with
        u[n] = u0 + (u1 - u0) n/B (+ strike envelopes):
           y[n] = r^n e^{iwn} (a y[-1] + e^{i theta} C[n]),  C[n] = sum_k<=n r^-k u[k]
        The ramp part of C uses precomputed sums; only struck resonators need a running sum."""
        B = BLOCK
        act = (np.abs(self.y) > 3e-6) | (u0 != 0) | (u1 != 0)
        for r_ in hits:
            act[r_] = True
        if not act.any():
            self.theta = np.mod(self.theta + omega * B, 2 * np.pi)
            return np.zeros((2, B))
        idx = np.nonzero(act)[0]
        om = omega[idx]
        # e^{iwn} for n < B from two small tables (one pass instead of a running product)
        ew = (np.exp(1j * np.outer(om, self.hi_n))[:, :, None] * np.exp(1j * np.outer(om, self.lo_n))[:, None, :]).reshape(len(idx), B)
        C = u0[idx, None] * self.S0[idx] + (u1 - u0)[idx, None] * self.S1[idx]
        if hits:
            pos = {r_: i for i, r_ in enumerate(idx)}
            for r_, h in hits.items():
                C[pos[r_]] += np.cumsum(h * self.Rinv[r_])
        a = np.exp(self.logr[idx] + 1j * om)
        Z = (a * self.y[idx])[:, None] + np.exp(1j * self.theta[idx])[:, None] * C
        y = (self.Rn[idx] * ew) * Z
        self.y[idx] = y[:, -1]
        self.y[~act] = 0.0
        self.theta = np.mod(self.theta + omega * B, 2 * np.pi)
        return np.vstack([self.panL[idx], self.panR[idx]]) @ y.real

    def precharge(self, c, fraction=0.8):
        """Start already singing near the steady level for controls c (used when this bank
        replaces another, so the change crossfades without a gap)."""
        bowl_rub = np.where(self.bowl == 0, 1.0, np.where((self.bowl == 1) & (self.mode == 0), c["fifth"], 0.0))
        amp = (self.RUB_GAIN * self.gain * c["excitation"] * bowl_rub * self.rub_w
               * self._tilt(c["brightness"]) * self.mamp)
        self.y = fraction * 0.6 * amp * np.exp(1j * self.theta)
        self.u_prev = amp * self.one_minus_r * 0.6

    def render(self, B, c):
        return self._solve(*self._drive(B, c))


class Reverb:
    """Uniformly partitioned convolution with a soft, warm synthetic room."""

    def __init__(self, sr, B, length=1.6, seed=5):
        rng = np.random.default_rng(seed)
        n = int(length * sr)
        t = np.arange(n) / sr
        freqs = np.fft.rfftfreq(n, 1 / sr)
        bands = [(0, 100, 1.0, 0.2), (100, 700, 2.6, 1.0), (700, 2000, 2.3, 0.85),
                 (2000, 5000, 1.5, 0.45), (5000, sr / 2 + 1, 0.8, 0.15)]       # Hz range, RT60, gain
        irs = []
        for ch in range(2):
            ir = np.zeros(n)
            for lo, hi, rt, g in bands:
                spec = np.fft.rfft(rng.standard_normal(n))
                spec[(freqs < lo) | (freqs >= hi)] = 0
                ir += g * np.fft.irfft(spec, n) * np.exp(-6.9078 * t / rt)
            pre = int(0.015 * sr)
            ir = np.r_[np.zeros(pre), ir[:n - pre]]
            ir[pre:pre + int(0.02 * sr)] *= np.linspace(0, 1, int(0.02 * sr))
            irs.append(ir / np.sqrt(np.sum(ir ** 2)))
        P = -(-n // B)
        hp = np.zeros((2, P * B))
        hp[:, :n] = np.array(irs)
        self.H = np.fft.rfft(hp.reshape(2, P, B), n=2 * B, axis=2).astype(np.complex64)  # (2, P, B+1)
        self.X = np.zeros((P, B + 1), np.complex64)
        self.inbuf = np.zeros(2 * B)
        self.idx = 0
        self.P, self.B = P, B

    def process(self, x):
        B, i = self.B, self.idx
        self.inbuf[:B] = self.inbuf[B:]
        self.inbuf[B:] = x
        self.X[i] = np.fft.rfft(self.inbuf)
        # sum_p X[i-p] H[p], using two reversed views of the ring buffer (no copies)
        Y = np.einsum("pk,cpk->ck", self.X[i::-1], self.H[:, :i + 1])
        if i + 1 < self.P:
            Y += np.einsum("pk,cpk->ck", self.X[:i:-1], self.H[:, i + 1:])
        self.idx = (i + 1) % self.P
        return np.fft.irfft(Y, n=2 * B, axis=1)[:, B:]


class Engine:
    CEILING = 0.70          # limiter ceiling (about -3 dBFS)
    MASTER = 2.8            # calibrated so volume = 0 gives about -25 dBFS typical, peaks near -9 dBFS

    FADE_S = 4.0            # seconds the previous bowls take to fade out after a preset change

    def __init__(self, s, sr=SR, preset=0):
        self.s, self.sr = s, sr
        self.root = max(43.0, min(62.0, s["root"]))
        self.tracker = InputTracker(s)
        self.reverb = Reverb(sr, BLOCK)
        self.mix = max(0.0, min(0.8, s["reverb"]))
        self.gain = 10 ** (s["volume"] / 20)
        self.lim = 1.0
        self.t = 0.0
        self.last_ctrl = None
        self.peak = 0.0
        self.limited_blocks = 0
        self.bank, self.preset, self.fading = None, None, []
        self.fade_in = 1.0
        self.queue = []
        self.set_preset(preset)

    def root_note(self, pitch_class):
        """The main bowl's MIDI note: the root setting, or the given pitch class in the octave nearest it."""
        if pitch_class is None:
            return self.root
        n = min((k for k in range(int(self.root) - 6, int(self.root) + 7) if k % 12 == pitch_class),
                key=lambda k: abs(k - self.root))
        return max(43, min(62, n))

    def set_preset(self, i):
        i = max(0, min(len(PRESETS) - 1, int(i)))
        if i == self.preset:
            return
        name, material, pc, companions, binaural = PRESETS[i]
        note = self.root_note(pc)
        self.root_hz = 440.0 * 2 ** ((note - 69) / 12)
        bank = BowlBank(self.sr, self.root_hz, max(4.0, min(60.0, self.s["resonance"])), max(0.0, self.s["wood"]),
                        material, companions, binaural, seed=11 + i)
        if self.bank is not None and self.t > 6.0:
            # crossfade: the old bowls stop being rubbed and fade away while the new ones start
            # already singing softly and fade in; a soft strike on the new main bowl confirms the
            # button press. Banks still fading from earlier presses fade out within one block.
            for f in self.fading:
                f[2] = True
            self.fading.append([self.bank, self.fade_in, False])    # from wherever its own fade-in got to
            if self.last_ctrl:
                bank.precharge(self.last_ctrl)
            bank.strike(0, 0.3, 0.4)
            self.fade_in = 0.0
        self.bank, self.preset = bank, i
        print("Preset %d: %s (main bowl %.1f Hz)" % (i + 1, name, self.root_hz), flush=True)

    def step(self, t_now, events):
        for ev in events:
            self.queue.append((self.t + (ev[3] if len(ev) > 3 else 0.0), ev))
        due = [e for when, e in self.queue if when <= self.t]
        self.queue = [(when, e) for when, e in self.queue if when > self.t]
        for kind, bowl, vel, *_ in due:
            if kind == "strike":
                self.bank.strike(bowl, vel, self.last_ctrl["brightness"] if self.last_ctrl else 0.4)
            else:
                self.bank.wood(vel)
        c = self.tracker.controls(t_now, BLOCK / self.sr, self.s["brightness"], self.s["pitch_drift"])
        self.last_ctrl = c
        dry = self.bank.render(BLOCK, c)
        if self.fade_in < 1.0:
            g0, self.fade_in = self.fade_in, min(1.0, self.fade_in + BLOCK / (1.5 * self.sr))
            dry = dry * np.linspace(g0, self.fade_in, BLOCK)
        if self.fading:
            quiet = dict(c, excitation=0.0)
            keep = []
            for f in self.fading:
                g0 = f[1]
                g1 = 0.0 if f[2] else max(0.0, g0 - BLOCK / (self.FADE_S * self.sr))
                dry = dry + f[0].render(BLOCK, quiet) * np.linspace(g0, g1, BLOCK)
                f[1] = g1
                if g1 > 0:
                    keep.append(f)
            self.fading = keep
        wet = self.reverb.process(0.5 * (dry[0] + dry[1]))
        out = (dry * (1 - 0.35 * self.mix) + wet * (1.1 * self.mix)) * (self.gain * self.MASTER)
        pk = float(np.max(np.abs(out)))
        want = min(1.0, self.CEILING / pk) if pk > 0 else 1.0
        if want < self.lim:
            new = want
            self.limited_blocks += 1
        else:
            new = self.lim + ema(BLOCK / self.sr, 1.0) * (1.0 - self.lim)
        g = np.full(BLOCK, new)
        g[:64] = np.linspace(self.lim, new, 64)
        self.lim = new
        out *= g
        out = np.where(np.abs(out) > 0.9, np.sign(out) * (0.9 + 0.09 * np.tanh((np.abs(out) - 0.9) / 0.09)), out)
        self.peak = max(self.peak, float(np.max(np.abs(out))))
        self.t += BLOCK / self.sr
        return out


# ============================================================ inputs
class MidiSource:
    """Live electrode reading from the Circuit Playground (pitch bend, channel 15)."""

    def __init__(self, hint="circuit"):
        import mido
        self.mido, self.hint = mido, hint
        self.port, self.name = None, None
        self.next_try = 0.0
        self.last_check = 0.0
        self.other_msgs = 0
        self.presets = []

    def take_presets(self):
        p, self.presets = self.presets, []
        return p

    def _find(self):
        for name in self.mido.get_input_names():
            low = name.lower()
            if self.hint in low and "through" not in low:
                return name
        return None

    def poll(self, now):
        if self.port is None:
            if now < self.next_try:
                return []
            self.next_try = now + 2.0
            name = self._find()
            if name:
                try:
                    self.port, self.name = self.mido.open_input(name), name
                    print("Listening to", name, flush=True)
                except Exception as e:
                    print("Couldn't open %s: %s" % (name, e), flush=True)
            return []
        if now - self.last_check > 2.0:
            self.last_check = now
            if self.name not in self.mido.get_input_names():
                print("Lost", self.name, flush=True)
                self.port.close()
                self.port = None
                return []
        vals = []
        for msg in self.port.iter_pending():
            if msg.type == "pitchwheel" and msg.channel == DATA_CH:
                vals.append((msg.pitch + 8192) / 16383.0)
            elif msg.type == "program_change" and msg.channel == PRESET_CH:
                self.presets.append(msg.program)          # buttons: the board re-sends this every 5 s
            else:
                self.other_msgs += 1
        k = len(vals)
        return [(now - 0.04 * (k - 1 - i), v) for i, v in enumerate(vals)]


class SimSource:
    """SIMULATED electrode signal for testing -- not the plant. Modelled on real logs:
    a reading near 0.33 drifting slowly (contacts drying), small wandering, sensor noise,
    occasional touches that dip it by up to ~45% for several seconds."""

    LABEL = "SIMULATED INPUT (not the plant)"

    def __init__(self, seed=21):
        self.rng = np.random.default_rng(seed)
        self.t = 0.0
        self.wander = 0.0
        self.touch = []
        self.next_touch = 25.0
        self.next_bump = 8.0

    def poll(self, now):
        out = []
        while self.t + 0.04 <= now:
            self.t += 0.04
            t = self.t
            r = self.rng
            self.wander += 0.04 / 8.0 * (-self.wander) + 0.0009 * r.standard_normal() * math.sqrt(0.04)
            if t >= self.next_touch:
                self.touch.append((t, r.uniform(0.25, 0.45), r.uniform(2.0, 6.0)))
                self.next_touch = t + r.uniform(20, 70)
            if t >= self.next_bump:
                self.touch.append((t, r.uniform(0.01, 0.04), r.uniform(0.3, 1.5)))
                self.next_bump = t + r.uniform(6, 25)
            base = 0.33 + 0.015 * math.sin(2 * math.pi * t / 400) + 0.00004 * t
            dip = 0.0
            for t0, depth, hold in self.touch:
                if t >= t0:
                    rise = 1 - math.exp(-(t - t0) / 0.5)
                    fall = math.exp(-max(0.0, t - t0 - hold) / 3.0)
                    dip += depth * rise * fall
            self.touch = [p for p in self.touch if t - p[0] - p[2] < 30]
            x = base * (1 - dip) + self.wander + 0.0005 * r.standard_normal()
            out.append((t, round(min(0.98, max(0.02, x)) * 16383) / 16383))
        return out


class CsvSource:
    def __init__(self, path):
        rows = []
        with open(path) as f:
            for line in f:
                p = line.strip().split(",")
                try:
                    rows.append((float(p[0]), float(p[1])))
                except (ValueError, IndexError):
                    continue
        self.rows, self.i = rows, 0
        self.t0 = rows[0][0] if rows else 0.0

    def poll(self, now):
        out = []
        while self.i < len(self.rows) and self.rows[self.i][0] - self.t0 <= now:
            out.append((self.rows[self.i][0] - self.t0, self.rows[self.i][1]))
            self.i += 1
        return out


# ============================================================ outputs
class AplaySink:
    def __init__(self, device, sr):
        cmd = ["aplay", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", str(sr),
               "--buffer-time=300000"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, x):
        self.p.stdin.write((np.clip(x, -1, 1) * 32767).astype("<i2").T.tobytes())

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()


class WavSink:
    def __init__(self, path, sr):
        self.w = wave.open(path, "wb")
        self.w.setnchannels(2)
        self.w.setsampwidth(2)
        self.w.setframerate(sr)

    def write(self, x):
        self.w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").T.tobytes())

    def close(self):
        self.w.close()


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description="The plant plays a resonating singing bowl.")
    ap.add_argument("--settings", default=os.path.join(HERE, "bowl_settings.txt"))
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=None, help="default %s" % v)
    ap.add_argument("--device", default="default", help="ALSA device, e.g. plughw:CARD=Headphones")
    ap.add_argument("--port", default="circuit", help="part of the MIDI input name to look for")
    ap.add_argument("--simulate", action="store_true", help="use a SIMULATED signal instead of the plant")
    ap.add_argument("--input-csv", help="replay a recorded signal (time_s,reading)")
    ap.add_argument("--render", help="write a WAV file instead of playing (needs --simulate or --input-csv)")
    ap.add_argument("--seconds", type=float, default=180.0, help="length of --render")
    ap.add_argument("--log", help="append time_s,reading,excitation,brightness,cents,event lines to this CSV")
    ap.add_argument("--preset", type=int, default=1,
                    help="bowl variation 1-6 to start with (live: the board's buttons then take over)")
    args = ap.parse_args()

    s = load_settings(args.settings)
    for k in DEFAULTS:
        v = getattr(args, k)
        if v is not None:
            s[k] = v

    if args.input_csv:
        src, label = CsvSource(args.input_csv), "REPLAYED INPUT from %s" % args.input_csv
    elif args.simulate:
        src, label = SimSource(), SimSource.LABEL
    elif args.render:
        sys.exit("--render needs --simulate or --input-csv")
    else:
        src, label = MidiSource(args.port), "live electrode input"

    eng = Engine(s, preset=args.preset - 1)
    print("Singing bowl | %s" % label, flush=True)
    print("Settings: " + ", ".join("%s=%g" % kv for kv in s.items()), flush=True)
    if label != "live electrode input":
        print("*** %s ***" % label, flush=True)

    sink = WavSink(args.render, SR) if args.render else AplaySink(args.device, SR)
    log = open(args.log, "a") if args.log else None
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    t_start = time.monotonic()
    t_status, cpu, nblocks = 0.0, 0.0, 0
    try:
        while True:
            now = eng.t if args.render else time.monotonic() - t_start
            if args.render and now >= args.seconds:
                break
            events = []
            readings = src.poll(now)
            if isinstance(src, MidiSource):
                for p in src.take_presets():
                    eng.set_preset(p)
            for t, x in readings:
                ev = eng.tracker.push(t, x)
                events += ev
                if log:
                    log.write("%.3f,%.5f,%.3f,%.3f,%.1f,%s\n" % (
                        t, x, eng.tracker.excitation, eng.last_ctrl["brightness"] if eng.last_ctrl else 0,
                        eng.last_ctrl["cents"] if eng.last_ctrl else 0, "|".join("%s%d@%.2f" % e[:3] for e in ev)))
            t0 = time.perf_counter()
            out = eng.step(now, events)
            cpu += time.perf_counter() - t0
            nblocks += 1
            sink.write(out)
            if now - t_status >= 10.0:
                t_status = now
                tr, c = eng.tracker, eng.last_ctrl
                if tr.connected:
                    inp = "electrode %.4f (range %.4f-%.4f, typical change %.5f)" % (
                        tr.s_slow, tr.win_lo, tr.win_hi, tr.jump_scale)
                elif isinstance(src, MidiSource) and src.port is not None and tr.n_data == 0:
                    inp = "connected, but no electrode data -- copy the new mushroom.mpy to the board"
                elif isinstance(src, MidiSource) and src.port is None:
                    inp = "waiting for the Circuit Playground..."
                else:
                    inp = "no signal from the electrodes (bowl ringing out)"
                tag = "[%s] " % label.split(" (")[0] if label != "live electrode input" else ""
                print("%s%s | preset %d | rub %.2f | brightness %.2f | pitch %+.1f c | strikes %d, wood %d | cpu %d%%" % (
                    tag, inp, eng.preset + 1, c["excitation"], c["brightness"], c["cents"], tr.counts["strike"],
                    tr.counts["wood"], 100 * cpu / max(1e-9, nblocks * BLOCK / SR)), flush=True)
                tr.win_lo, tr.win_hi = 9.0, -9.0
                cpu, nblocks = 0.0, 0
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        print("Audio output stopped -- is another program (e.g. the plantsynth service) using the sound card?",
              flush=True)
        sys.exit(1)                     # non-zero, so the service tries again
    finally:
        sink.close()
        if log:
            log.close()
    if args.render:
        print("rendered %.0f s to %s | peak %.2f | limiter active in %d blocks | strikes %d, wood %d" % (
            eng.t, args.render, eng.peak, eng.limited_blocks, eng.tracker.counts["strike"], eng.tracker.counts["wood"]))


if __name__ == "__main__":
    main()
