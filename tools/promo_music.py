"""Music beds for tools/make_promo.py, synthesised here (no samples, no third-party music).

Each style is arranged to the video's scenes: sparse under the intro, fuller under the cards
and the posts, a build while the curve fills, a downbeat on the graduation and a resolving
chord under the outro. The tempo grid is shifted so that the graduation lands on a bar line.

    simple     the first bed: Am-F-C-G pad, an electric-piano arpeggio, a bell melody
    lofi       jazzy ninth chords on an electric piano, brushes, a warm bass, a little swing
    synthwave  a pumping detuned pad, a 16th arpeggio with echoes, a pulsing bass, soft drums
    cinematic  felt piano, slow strings, glass bells, a timpani build and a wide final chord
    house      chill deep house: ninth chords on the off-beats, a soft kick, a pumping pad

Only numpy: brightness comes from how fast each harmonic dies away, echoes from delayed
copies, the pump from a gain envelope, the room from a convolution by FFT.
"""

import math

import numpy as np

SR = 48000
STYLES = ("simple", "lofi", "synthwave", "cinematic", "house")
NAMES = {"C": -9, "C#": -8, "Db": -8, "D": -7, "D#": -6, "Eb": -6, "E": -5, "F": -4, "F#": -3, "Gb": -3,
         "G": -2, "G#": -1, "Ab": -1, "A": 0, "A#": 1, "Bb": 1, "B": 2}


def hz(name):
    return 440.0 * 2 ** ((NAMES[name[:-1]] + 12 * (int(name[-1]) - 4)) / 12)


def db(x):
    return 10 ** (x / 20)


class Mix:
    def __init__(self, length):
        self.n = int(SR * (length + 1))
        self.length = length
        self.buses = {}

    def bus(self, name):
        if name not in self.buses:
            self.buses[name] = np.zeros((self.n, 2))
        return self.buses[name]

    def put(self, name, sig, at, pan=0.0, gain=1.0):
        i = int(round(at * SR))
        if i >= self.n or i + len(sig) <= 0:
            return
        if i < 0:
            sig, i = sig[-i:], 0
        sig = sig[: self.n - i] * gain
        b = self.bus(name)
        b[i:i + len(sig), 0] += sig * math.cos((pan + 1) * math.pi / 4) * 1.41
        b[i:i + len(sig), 1] += sig * math.sin((pan + 1) * math.pi / 4) * 1.41


# ----------------------------------------------------------------------------- instruments
def _t(dur):
    return np.arange(int(dur * SR)) / SR


def additive(f, dur, harmonics, decay, bright_decay, attack=.005, release=None, detune=0.0, vib=0.0):
    """A tone from `harmonics` partials (1/k loud); partial k fades `bright_decay` times faster per
    step, so the tone darkens as it rings, like a filter closing."""
    t = _t(dur)
    out = np.zeros_like(t)
    vibr = 1 + vib * np.sin(2 * np.pi * 5.2 * t) * np.minimum(1, t / .4)
    for k in range(1, harmonics + 1):
        if f * k > 15000:
            break
        ph = 2 * np.pi * f * k * np.cumsum(vibr) / SR
        a = (1 / k) * np.exp(-t * (k - 1) * bright_decay)
        out += a * np.sin(ph)
        if detune:
            out += a * np.sin(ph * (1 + detune / 1200)) * .8
    env = np.minimum(1, t / attack) * np.exp(-t / decay)
    if release is not None:  # a held note: flat, then a fade
        env = np.minimum(1, t / attack) * np.clip((dur - t) / release, 0, 1)
    return out * env


def rhodes(f, dur, vel=1.0):
    t = _t(dur + 1.5)
    mod = np.sin(2 * np.pi * f * 1.0 * t) * 1.6 * np.exp(-t / .18)  # the tine's bark
    tone = np.sin(2 * np.pi * f * t + mod) + .25 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / .4)
    trem = 1 + .18 * np.sin(2 * np.pi * 4.3 * t)
    env = np.minimum(1, t / .006) * np.exp(-t / (1.2 + .3 * dur)) * np.clip((dur + 1.5 - t) / .3, 0, 1)
    return tone * trem * env * vel


def piano(f, dur, vel=1.0):
    """A soft felt piano: inharmonic partials, the high ones gone fast, a muted attack."""
    t = _t(dur + 2.5)
    out = np.zeros_like(t)
    for k in range(1, 9):
        fk = f * k * math.sqrt(1 + .0004 * k * k)
        if fk > 12000:
            break
        out += (1 / k ** 1.3) * np.sin(2 * np.pi * fk * t) * np.exp(-t * (.6 + k * .9))
    body = np.exp(-t / (2.2 if f < 300 else 1.6))
    env = np.minimum(1, t / .008) * body * np.clip((dur + 2.5 - t) / .4, 0, 1)
    return out * env * vel


def bell(f, dur, vel=1.0, ratio=3.5):
    t = _t(dur + 2.5)
    mod = np.sin(2 * np.pi * f * ratio * t) * 2.0 * np.exp(-t / .5)
    return np.sin(2 * np.pi * f * t + mod) * np.minimum(1, t / .003) * np.exp(-t / 1.4) * vel


def pluck(f, dur, vel=1.0):
    return additive(f, dur + .4, 12, .25, 9.0, attack=.002) * vel


def pad(fs, dur, vel=1.0, bright=.6, attack=1.0):
    out = None
    for f in fs:
        s = additive(f, dur + attack, 8, 99, bright, attack=attack, release=attack * 1.2, detune=9)
        out = s if out is None else out + s
    return out / max(1, len(fs)) * vel


def strings(fs, dur, vel=1.0, attack=1.8):
    out = None
    for f in fs:
        s = additive(f, dur + attack, 7, 99, .9, attack=attack, release=attack, detune=6, vib=.004)
        out = s if out is None else out + s
    return out / max(1, len(fs)) * vel


def sub(f, dur, vel=1.0, decay=1.5):
    t = _t(dur + .2)
    tone = np.sin(2 * np.pi * f * t) + .3 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / .15)
    return tone * np.minimum(1, t / .01) * np.exp(-t / decay) * np.clip((dur + .2 - t) / .08, 0, 1) * vel


def kick(vel=1.0, soft=True):
    t = _t(.5)
    f = 48 + 90 * np.exp(-t / .035)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / (.18 if soft else .3))
    return s * vel


def noise_band(dur, lo, hi, seed, decay, vel=1.0):
    """Noise between lo and hi Hz (shaped by FFT), with an exponential decay."""
    rnd = np.random.default_rng(seed)
    n = int(dur * SR)
    spec = np.fft.rfft(rnd.normal(0, 1, n))
    fr = np.fft.rfftfreq(n, 1 / SR)
    mask = 1 / (1 + (lo / np.maximum(fr, 1)) ** 4) / (1 + (fr / hi) ** 4)
    s = np.fft.irfft(spec * mask, n)
    s /= np.max(np.abs(s)) or 1
    t = np.arange(n) / SR
    return s * np.minimum(1, t / .002) * np.exp(-t / decay) * vel


def timpani(f, vel=1.0):
    t = _t(2.0)
    fr = f * (1 + .04 * np.exp(-t / .1))
    s = np.sin(2 * np.pi * np.cumsum(fr) / SR) + .4 * np.sin(2 * np.pi * np.cumsum(fr * 1.5) / SR) * np.exp(-t / .3)
    return s * np.minimum(1, t / .004) * np.exp(-t / .9) * vel


def swell(dur, lo=300, hi=6000, seed=9, vel=1.0, reverse=True):
    """A noise swell that rises into the downbeat (a reversed cymbal, roughly)."""
    s = noise_band(dur, lo, hi, seed, 99)
    t = np.arange(len(s)) / SR
    return s * (t / dur) ** 3 * vel


# ----------------------------------------------------------------------------- effects
def reverb(x, seconds=2.4, wet=.3, seed=3, dark=24):
    rnd = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    ir = rnd.normal(0, 1, n) * np.exp(-t / (seconds / 5))
    ir = np.convolve(ir, np.ones(dark) / dark, "same")
    ir /= np.sqrt(np.sum(ir ** 2))
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    w = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:len(x)]
    return x * (1 - wet) + w * wet


def reverb2(bus, seconds=2.4, wet=.3, dark=24):
    return np.stack([reverb(bus[:, 0], seconds, wet, 3, dark), reverb(bus[:, 1], seconds, wet, 4, dark)], axis=1)


def echo(bus, delay, feedback=.4, taps=4, pingpong=True):
    out = bus.copy()
    d = int(delay * SR)
    for k in range(1, taps + 1):
        g = feedback ** k
        src = bus[:-d * k] if d * k < len(bus) else None
        if src is None:
            break
        if pingpong and k % 2:
            out[d * k:, 0] += src[:, 1] * g
            out[d * k:, 1] += src[:, 0] * g
        else:
            out[d * k:] += src * g
    return out


def lowpass(bus, cutoff):
    spec = np.fft.rfft(bus, axis=0)
    fr = np.fft.rfftfreq(len(bus), 1 / SR)
    spec *= (1 / np.sqrt(1 + (fr / cutoff) ** 4))[:, None]
    return np.fft.irfft(spec, len(bus), axis=0)


def pump(n, beat, shift, depth=.55, start=0.0, end=1e9):
    """A sidechain-like gain dip on every beat between start and end."""
    t = np.arange(n) / SR
    ph = ((t - shift) % beat) / beat
    g = 1 - depth * np.exp(-ph * 9)
    on = (t >= start) & (t < end)
    return np.where(on, g, 1.0)


# ----------------------------------------------------------------------------- arrangement
class Grid:
    """Bars and beats, shifted so that `downbeat` falls on a bar line."""

    def __init__(self, bpm, downbeat, length):
        self.beat = 60 / bpm
        self.bar = 4 * self.beat
        k = math.floor(downbeat / self.bar)
        self.shift = downbeat - k * self.bar   # the time of bar 0
        self.first = -1                        # one bar before 0, so the start is covered
        self.last = int((length - self.shift) / self.bar) + 1

    def bars(self):
        for b in range(self.first, self.last + 1):
            yield b, self.shift + b * self.bar


def sections(scenes):
    """Scene name -> (start, end) in seconds."""
    return {name: (s, s + d) for name, s, d in scenes}


def render(style, length, scenes, grad_at, target_db=-30.0):
    if style == "simple":
        out = _simple(length)
    else:
        out = {"lofi": _lofi, "synthwave": _synthwave, "cinematic": _cinematic, "house": _house}[style](length, sections(scenes), grad_at)
    t = np.arange(len(out)) / SR
    fade = np.minimum(1, t / 1.8) * np.clip((length - t) / 2.6, 0, 1)
    out *= fade[:, None]
    rms = float(np.sqrt(np.mean(out[: int(length * SR)] ** 2))) or 1.0
    return out * db(target_db) / rms


def _simple(length, bpm=80):
    m = Mix(length)
    beat = 60 / bpm
    bar = 4 * beat
    chords = [("A2", ["A3", "C4", "E4", "G4"]), ("F2", ["F3", "A3", "C4", "E4"]),
              ("C3", ["G3", "C4", "E4", "G4"]), ("G2", ["G3", "B3", "D4", "F#4"])]
    melody = [("E5", 0, 2), ("D5", 2, 1), ("C5", 3, 1), ("A4", 4, 3), ("C5", 8, 2), ("D5", 10, 2),
              ("E5", 12, 1.5), ("G5", 13.5, .5), ("E5", 14, 2)]
    for b in range(int(length / bar) + 2):
        at = b * bar
        root, notes = chords[b % 4]
        fs = [hz(x) for x in notes]
        m.put("pad", pad(fs, bar, .6, bright=.9), at)
        m.put("pad", sub(hz(root), bar * .9, .55), at)
        for k, idx in enumerate([0, 1, 2, 3, 2, 1, 2, 3]):
            m.put("keys", rhodes(fs[idx] * 2, beat * .5, .2), at + k * beat / 2, -.5 if k % 2 else .5)
        if b % 4 == 0 and b >= 2:
            for name, start, dur in melody:
                m.put("keys", bell(hz(name), dur * beat, .18), at + start * beat, .15)
    mix = m.bus("pad") + m.bus("keys")
    return reverb2(mix, 2.2, .35)


def _lofi(length, sec, grad_at, bpm=78):
    m = Mix(length)
    g = Grid(bpm, grad_at, length)
    beat, bar = g.beat, g.bar
    swing = beat * .58 - beat / 2  # how late the off-beat eighth comes
    # ii-V-I-vi in C, with ninths and thirteenths
    chords = [("D2", ["F3", "A3", "C4", "E4"]), ("G2", ["F3", "B3", "E4", "A4"]),
              ("C2", ["E3", "G3", "B3", "D4"]), ("A1", ["G3", "C4", "E4", "B4"])]
    lick = [("E5", 0), ("G5", .5), ("A5", 1), ("G5", 1.75), ("E5", 2.5), ("D5", 3), ("C5", 3.5),
            ("D5", 4.5), ("E5", 5), ("G5", 6), ("E5", 7)]
    cards0, feed1 = sec["cards"][0], sec["feed"][1]
    build0, outro0 = sec["grad"][0], sec["outro"][0]
    for b, at in g.bars():
        root, notes = chords[b % 4]
        fs = [hz(x) for x in notes]
        if at >= outro0 - .01:  # the end: one Cmaj9 left to ring
            fs =[hz(x) for x in ("E3", "G3", "B3", "D4", "G4")]
            m.put("keys", sum_pad(fs, rhodes, bar * 2, .32), at)
            m.put("bass", sub(hz("C2"), bar * 2, .6, decay=3), at)
            break
        building = build0 <= at < grad_at - .01
        # electric piano comping: 1 and the and-of-2 (pushed), quieter in the build
        comp = .26 if not building else .16
        m.put("keys", sum_pad(fs, rhodes, beat * 1.4, comp), at)
        m.put("keys", sum_pad(fs, rhodes, beat * 1.2, comp * .7), at + beat * 1.5 + swing)
        # bass: root, fifth, a walk-up into the next bar
        if at >= sec["pitch"][0] - .01 and not building:
            r = hz(root)
            m.put("bass", sub(r, beat * 1.6, .6), at)
            m.put("bass", sub(r * 1.5, beat * .9, .45), at + beat * 2)
            m.put("bass", sub(r * 2 ** (10 / 12), beat * .5, .35), at + beat * 3.5 + swing)
        # drums from the /track scene, out for the build, back on the downbeat
        if at >= sec["track"][0] - .01 and not building:
            for k in range(4):
                if k in (0, 2):
                    m.put("drums", kick(.7), at + k * beat + (beat * .5 + swing if k == 2 else 0))
                if k in (1, 3):
                    m.put("drums", noise_band(.35, 900, 5000, 30 + b * 4 + k, .09, .35), at + k * beat)
                for half in (0, 1):
                    m.put("drums", noise_band(.08, 6000, 14000, 200 + b * 8 + k * 2 + half, .02, .16),
                          at + k * beat + half * (beat / 2 + swing), .3)
        # the melody over the cards and the posts
        if cards0 - .01 <= at < feed1 and b % 2 == 0:
            for name, bt in lick:
                off = swing if (bt * 2) % 2 == 1 else 0
                m.put("lead", rhodes(hz(name), beat * .6, .22), at + bt * beat + off, .2)
        if building:  # a soft rising swell into the graduation
            m.put("fx", swell(grad_at - at, 500, 7000, vel=.25), at)
    mix = (lowpass(m.bus("keys"), 5000) + lowpass(m.bus("lead"), 6000) + m.bus("bass") * .9
           + lowpass(m.bus("drums"), 7000) * .8 + m.bus("fx"))
    return reverb2(mix, 1.8, .22, dark=40)


def sum_pad(fs, inst, dur, vel):
    return sum(inst(f, dur, vel / len(fs) * 1.6) for f in fs)


def _synthwave(length, sec, grad_at, bpm=100):
    m = Mix(length)
    g = Grid(bpm, grad_at, length)
    beat, bar = g.beat, g.bar
    s16 = beat / 4
    chords = [("A1", ["A3", "C4", "E4", "B4"]), ("F1", ["F3", "A3", "C4", "E4"]),
              ("C2", ["G3", "C4", "E4", "G4"]), ("E1", ["G#3", "B3", "E4", "B4"])]
    lead = [("E5", 0, 1.5), ("D5", 1.5, .5), ("C5", 2, 1), ("B4", 3, 1), ("A4", 4, 2), ("C5", 6, 1), ("E5", 7, 1),
            ("G5", 8, 1.5), ("F5", 9.5, .5), ("E5", 10, 1), ("C5", 11, 1), ("E5", 12, 4)]
    cards0 = sec["cards"][0]
    build0, outro0 = sec["grad"][0], sec["outro"][0]
    for b, at in g.bars():
        root, notes = chords[b % 4]
        fs = [hz(x) for x in notes]
        if at >= outro0 - .01:
            final = [hz(x) for x in ("A3", "C#4", "E4", "B4", "E5")]
            m.put("pad", pad(final, bar * 2.2, .9, bright=.35, attack=.3), at)
            m.put("bass", sub(hz("A1"), bar * 2, .7, decay=3), at)
            m.put("arp", bell(hz("E6"), 2, .12), at + beat)
            break
        building = build0 <= at < grad_at - .01
        m.put("pad", pad(fs, bar, .8, bright=.5 if at < cards0 else .3, attack=.25), at)
        # the arpeggio: 16ths up two octaves, in from the pitch scene
        if at >= sec["pitch"][0] - .01:
            seq = fs + [f * 2 for f in fs]
            for k in range(16):
                f = seq[(k * 3) % len(seq)] if not building else seq[k % len(seq)]
                vel = (.2 if k % 4 == 0 else .13) * (1 + .5 * ((at - build0) / max(.1, grad_at - build0)) if building else 1)
                m.put("arp", pluck(f, s16 * 1.5, vel), at + k * s16, .35 if k % 2 else -.35)
        # pulsing bass in eighths, from the /track scene
        if at >= sec["track"][0] - .01 and not building:
            r = hz(root)
            for k in range(8):
                m.put("bass", additive(r * (2 if k % 2 else 1), beat * .45, 6, .18, 6, attack=.004), at + k * beat / 2)
        # drums from the cards: four on the floor, a clap on 2 and 4, open hats on the off-beats
        if at >= cards0 - .01 and not building:
            for k in range(4):
                m.put("drums", kick(.8, soft=False), at + k * beat)
                if k in (1, 3):
                    m.put("drums", noise_band(.3, 1200, 6000, 40 + b * 4 + k, .07, .3), at + k * beat)
                m.put("drums", noise_band(.18, 7000, 15000, 300 + b * 4 + k, .05, .13), at + k * beat + beat / 2, .25)
        if cards0 - .01 <= at < sec["feed"][1] and (b - g.first) % 4 == 0:
            for name, st, dur in lead:
                m.put("lead", additive(hz(name), dur * beat, 9, 99, 1.4, attack=.03, release=.25, detune=6, vib=.006) * .16,
                      at + st * beat, .1)
        if building:
            m.put("fx", swell(grad_at - at, 400, 9000, vel=.35), at)
    n = m.n
    pumped = m.bus("pad") * pump(n, beat, g.shift, .5, cards0, outro0)[:, None]
    arp = echo(lowpass(m.bus("arp"), 7000), beat * .75, .38, 4)
    mix = (pumped * .9 + arp * .9 + m.bus("bass") * .8 + lowpass(m.bus("drums"), 9000) * .75
           + echo(m.bus("lead"), beat * .75, .3, 3) + m.bus("fx"))
    return reverb2(mix, 2.6, .25)


def _cinematic(length, sec, grad_at, bpm=72):
    m = Mix(length)
    g = Grid(bpm, grad_at, length)
    beat, bar = g.beat, g.bar
    chords = [("A1", ["A3", "C4", "E4", "B4"]), ("F1", ["F3", "C4", "E4", "A4"]),
              ("C2", ["G3", "C4", "E4", "D5"]), ("G1", ["G3", "B3", "D4", "E5"])]
    motif = [(0, 0), (1, 1), (2, 2), (1, 3), (3, 4), (2, 5), (1, 6), (2, 7)]  # (chord tone, eighth)
    cards0 = sec["cards"][0]
    build0, outro0 = sec["grad"][0], sec["outro"][0]
    for b, at in g.bars():
        root, notes = chords[b % 4]
        fs = [hz(x) for x in notes]
        if at >= grad_at - .01 and at < outro0 - .01 + bar:  # the downbeat: a wide chord, a boom, bells
            wide = [hz(x) for x in ("A2", "E3", "A3", "C#4", "E4", "B4", "E5")]
            m.put("strings", strings(wide, bar * 2.5, 1.3, attack=.15), at)
            m.put("low", timpani(hz("A1"), 1.0), at)
            m.put("low", sub(hz("A1"), bar * 2, .8, decay=4), at)
            for k, n_ in enumerate(("E5", "A5", "C#6", "E6", "B5", "A6")):
                m.put("bells", bell(hz(n_), 1.5, .18), at + k * beat / 3, -.6 + k * .24)
            for k, idx in enumerate([0, 2, 4, 6]):
                m.put("piano", piano(wide[idx] * 2, beat * 2, .35), at + bar + k * beat)
            break
        building = build0 <= at < grad_at - .01
        m.put("strings", strings(fs, bar, .85 if at >= cards0 else .6, attack=1.6 if at < cards0 else 1.0), at - .3)
        m.put("low", sub(hz(root), bar, .5, decay=4), at)
        # the felt-piano motif: in from the pitch scene, an octave up under the cards
        if at >= sec["pitch"][0] - .01:
            up = 2 if at >= cards0 else 1
            for idx, eighth in motif:
                vel = .32 if eighth % 2 == 0 else .24
                m.put("piano", piano(fs[idx] * up, beat * .7, vel), at + eighth * beat / 2, -.2 + idx * .13)
        if at >= sec["track"][0] - .01 and b % 2 == 0 and not building:
            m.put("bells", bell(fs[-1] * 2, 2, .1), at + beat * 2, .4)
        if building:  # timpani rolling faster and louder into the downbeat, strings swelling
            span = grad_at - at
            k, tt = 0, 0.0
            while tt < span - .05:
                prog = tt / span
                m.put("low", timpani(hz("E2"), .25 + .75 * prog ** 2), at + tt)
                tt += beat * (1 - .8 * prog) / 2
                k += 1
            m.put("fx", swell(span, 300, 8000, vel=.4), at)
    mix = (m.bus("strings") * .9 + m.bus("piano") + m.bus("bells") + m.bus("low") * .8 + m.bus("fx"))
    return reverb2(mix, 3.2, .34)


def _house(length, sec, grad_at, bpm=118):
    """Chill deep house: Am9 - Fmaj9 - Cmaj7 - G6, soft four on the floor, chord stabs on the
    off-beats, a syncopated sub bass, a pumping pad; the drums drop out for the build."""
    m = Mix(length)
    g = Grid(bpm, grad_at, length)
    beat, bar = g.beat, g.bar
    chords = [("A1", ["G3", "C4", "E4", "B4"]), ("F1", ["A3", "C4", "E4", "G4"]),
              ("C2", ["G3", "B3", "E4", "G4"]), ("G1", ["G3", "B3", "D4", "E4"])]
    hook = [("E5", 0), ("G5", .75), ("A5", 1.5), ("G5", 2.5), ("E5", 3), ("D5", 4.5), ("E5", 5.25), ("C5", 6),
            ("D5", 7)]
    pitch0, track0, cards0 = sec["pitch"][0], sec["track"][0], sec["cards"][0]
    build0, outro0 = sec["grad"][0], sec["outro"][0]
    for b, at in g.bars():
        root, notes = chords[b % 4]
        fs = [hz(x) for x in notes]
        if at >= outro0 - .01:
            final = [hz(x) for x in ("A3", "C4", "E4", "G4", "B4")]
            m.put("pad", pad(final, bar * 2.5, .9, bright=.5, attack=.2), at)
            m.put("bass", sub(hz("A1"), bar * 2, .6, decay=3), at)
            m.put("stab", sum_pad(final, rhodes, beat * 3, .35), at)
            break
        building = build0 <= at < grad_at - .01
        m.put("pad", pad(fs, bar, .55, bright=.7, attack=.4), at)
        # chord stabs on the off-beats, from the pitch scene
        if at >= pitch0 - .01:
            for k in range(4):
                m.put("stab", sum_pad(fs, rhodes, beat * .45, .28 if not building else .2), at + k * beat + beat / 2,
                      .25 if k % 2 else -.25)
        # a syncopated sub bass from the /track scene
        if at >= track0 - .01 and not building:
            r = hz(root)
            for st, dur in ((0, .7), (1.5, .4), (2.5, .7), (3.5, .4)):
                m.put("bass", sub(r, beat * dur, .6, decay=.6), at + st * beat)
        # drums: kick from /track, hats and claps from the cards
        if at >= track0 - .01 and not building:
            for k in range(4):
                m.put("drums", kick(.75), at + k * beat)
                if at >= cards0 - .01:
                    m.put("drums", noise_band(.16, 7000, 15000, 500 + b * 4 + k, .045, .14), at + k * beat + beat / 2, .2)
                    if k in (1, 3):
                        m.put("drums", noise_band(.3, 1100, 5500, 60 + b * 4 + k, .08, .26), at + k * beat)
        if cards0 - .01 <= at < sec["feed"][1] and (b - g.first) % 2 == 0:
            for name, bt in hook:
                m.put("lead", pluck(hz(name), beat * .5, .16), at + bt * beat, .15)
        if building:
            m.put("fx", swell(grad_at - at, 500, 9000, vel=.3), at)
    n = m.n
    pumped = m.bus("pad") * pump(n, beat, g.shift, .5, track0, outro0)[:, None]
    mix = (pumped * .8 + lowpass(m.bus("stab"), 4500) + m.bus("bass") * .85 + lowpass(m.bus("drums"), 9000) * .7
           + echo(m.bus("lead"), beat * .75, .35, 3) + m.bus("fx"))
    return reverb2(mix, 2.0, .24)
