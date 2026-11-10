# Plant / mushroom MIDI -- Circuit Playground Express (CircuitPython 10)
#
# Needs mushroom.mpy on the CIRCUITPY drive next to this file.
#
# Wiring:
#   3.3V pad --[ R_FIXED ]-- A1 pad --clip--> electrode 1
#   GND pad  -------------------------clip--> electrode 2
#
# Presets -- button A = next, button B = previous. After a press, the number of
# lit pixels shows the preset; the board remembers it after power-off.
#   1 Original    green    your ROOT / SCALE below
#   2 Diatonic    violet   C D E F G A B C  (crystal bowls)
#   3 Pentatonic  amber    C D E G A        (Tibetan bowls)
#   4 Endocrine   blue     C# D# F# G# A#   (crystal bowls)
#   5 Chromatic   pink     all 12 notes     (Tibetan bowls)
#   6 Triad       teal     C E G            (crystal bowls + binaural drone)
#
# Edit the settings below and save; the board restarts with them.
# The serial console prints the resistance between the electrodes every 2 s --
# set R_FIXED close to that number for the best sensitivity.

import gc
gc.collect()
import mushroom

mushroom.run(
    R_FIXED=1000000,          # ohms: total resistance between 3.3V and A1
    SENSITIVITY=2.5,          # lower = more notes (try 1.8 to 4.0)
    PRESET=1,                 # preset to start in (changing this overrides the buttons' saved choice)
    ROOT=60,                  # Original preset: lowest melody note (60 = middle C)
    SCALE=(0, 2, 4, 7, 9),    # Original preset: major pentatonic. Minor: (0, 3, 5, 7, 10)
    OCTAVES=2,                # Original preset
    MELODY_CH=0,              # 0 = MIDI channel 1
    DRONE_CH=1,               # 1 = MIDI channel 2; None = no drone
    PLOT=False,               # True: graph changes vs. thresholds in Mu's plotter
)
