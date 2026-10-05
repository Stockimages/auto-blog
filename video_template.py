"""
video_template.py — "black bars + yellow words" video template for DecorVibe.

Layout (same as the reference template, scaled to any canvas):
  * thin yellow progress bar along the very top
  * black top bar with the hook text (white, one highlighted word in yellow)
  * a square photo window with slow Ken Burns zoom and short caption chunks
  * black bottom bar with a call-to-action line
  * a random transition between photos (41 different ones, never repeated
    in one video) with a "swoosh" sound on every photo change
  * voiceover via Gemini TTS ("Zephyr") with automatic fallback handled by
    the caller

Needs only Pillow and ffmpeg (both already used by auto_blog.py).
Fonts: put fonts/Anton-Regular.ttf and fonts/Poppins-ExtraBold.ttf (both free,
SIL Open Font License) next to auto_blog.py for the exact template look; if
they're missing, a bold system font is used instead.
"""
import base64
import io
import math
import os
import random
import re
import subprocess
import wave

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FPS = 30
TRANSITION_SECONDS = 0.6
YELLOW = (254, 210, 26)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
DARK = (22, 22, 22)

HEADLINE_FONTS = [
    "fonts/Anton-Regular.ttf",
    "/usr/share/fonts/truetype/google-fonts/Anton-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
CAPTION_FONTS = [
    "fonts/Poppins-ExtraBold.ttf",
    "fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-ExtraBold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


# --------------------------------------------------------------------------
# Fonts and text
# --------------------------------------------------------------------------
def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue   # empty/corrupt file: try the next candidate
    return ImageFont.load_default()


def _clean(word):
    return re.sub(r"[^A-Za-z0-9$%]", "", word).upper()


def _wrap(words, font, max_w):
    space = font.getlength(" ")
    lines, cur_w = [[]], 0.0
    for w in words:
        ww = font.getlength(w)
        add = ww if not lines[-1] else ww + space
        if lines[-1] and cur_w + add > max_w:
            lines.append([w])
            cur_w = ww
        else:
            lines[-1].append(w)
            cur_w += add
    return lines


def pick_highlight_words(text):
    """One word to colour yellow: a price/number if there is one, else the longest word."""
    words = [w for w in text.split() if _clean(w)]
    for w in words:
        if any(c.isdigit() for c in w) or "$" in w:
            return [_clean(w)]
    stop = {"THE", "AND", "THIS", "THAT", "WITH", "FROM", "YOUR", "FOR", "INTO", "ARE", "YOU", "HOW"}
    cands = [w for w in words if _clean(w) not in stop and len(_clean(w)) >= 4]
    return [_clean(max(cands, key=lambda w: len(_clean(w))))] if cands else []


def render_highlight_block(text, highlights, width, height, max_lines=3, max_font=130, min_font=30):
    """Centered white text, highlighted words in yellow, auto-sized to fit width x height."""
    text = " ".join(text.upper().split())
    words = text.split()
    hl = {_clean(h) for h in highlights}
    pad_x = int(width * 0.05)
    font = _font(HEADLINE_FONTS, min_font)
    lines = [words]
    lh = min_font
    for size in range(max_font, min_font - 1, -4):
        font = _font(HEADLINE_FONTS, size)
        lines = _wrap(words, font, width - 2 * pad_x)
        asc, desc = font.getmetrics()
        lh = int((asc + desc) * 1.02)
        if len(lines) <= max_lines and lh * len(lines) <= height - 20:
            break
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    space = font.getlength(" ")
    y = (height - lh * len(lines)) // 2
    for line in lines:
        line_w = sum(font.getlength(w) for w in line) + space * (len(line) - 1)
        x = (width - line_w) / 2
        for w in line:
            d.text((x, y), w, font=font, fill=YELLOW if _clean(w) in hl else WHITE)
            x += font.getlength(w) + space
        y += lh
    return img


def chunk_caption(text, max_words=4, max_chars=26):
    chunks, cur = [], []
    for w in text.split():
        if cur and (len(cur) >= max_words or len(" ".join(cur + [w])) > max_chars):
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
        if w.endswith((".", "!", "?")):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks or [text]


def render_caption(text, style, width):
    """style 'outline' = white text with black outline; 'box' = dark text on a yellow rounded box."""
    size = 62
    font = _font(CAPTION_FONTS, size)
    while font.getlength(text) > width - 140 and size > 30:
        size -= 3
        font = _font(CAPTION_FONTS, size)
    img = Image.new("RGBA", (width, int(size * 2.4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tw = font.getlength(text)
    x, y = (width - tw) / 2, size * 0.6
    if style == "box":
        px, py = 30, 14
        d.rounded_rectangle([x - px, y - py, x + tw + px, y + size * 1.15 + py], radius=22, fill=YELLOW)
        d.text((x, y), text, font=font, fill=DARK)
    else:
        sw = max(4, size // 9)
        d.text((x, y), text, font=font, fill=WHITE, stroke_width=sw, stroke_fill=BLACK)
    return img


# --------------------------------------------------------------------------
# Transitions. Every function takes (A, B, p): two RGB square images and the
# progress p in (0, 1); returns the blended RGB image.
# --------------------------------------------------------------------------
def _ease(p):
    p = max(0.0, min(1.0, p))
    return p * p * (3 - 2 * p)


def _bounce(t):
    t = max(0.0, min(1.0, t))
    if t < 1 / 2.75:
        return 7.5625 * t * t
    if t < 2 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375


def _solid(S, color):
    return Image.new("RGB", (S, S), color)


def _mask(S, fn):
    m = Image.new("L", (S, S), 0)
    fn(ImageDraw.Draw(m))
    return m


def _zoom(img, z):
    S = img.width
    side = S / max(1.0, z)
    o = (S - side) / 2
    return img.transform((S, S), Image.EXTENT, (o, o, o + side, o + side), Image.BILINEAR)


def _blur(img, r):
    if r < 0.6:
        return img
    S = img.width
    small = img.resize((max(8, S // 4), max(8, S // 4)), Image.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(r / 4)).resize((S, S), Image.BILINEAR)


def _zoom_blur(img, strength):
    acc = img
    for k in range(1, 5):
        acc = Image.blend(acc, _zoom(img, 1 + strength * k / 4), 1.0 / (k + 1))
    return acc


def _star_points(cx, cy, R, points=5):
    pts = []
    for i in range(points * 2):
        ang = -math.pi / 2 + i * math.pi / points
        r = R if i % 2 == 0 else R * 0.5
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _heart_points(cx, cy, k):
    pts = []
    for i in range(120):
        t = i / 120 * 2 * math.pi
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * k, cy - y * k + k * 2))
    return pts


def t_soft_fade(A, B, p):
    return Image.blend(A, B, _ease(p))


def t_slide_in(A, B, p):
    S = A.width
    out = A.copy()
    out.paste(B, (int(S * (1 - _ease(p))), 0))
    return out


def t_push(A, B, p):
    S = A.width
    s = int(S * _ease(p))
    out = Image.new("RGB", (S, S))
    out.paste(A, (-s, 0))
    out.paste(B, (S - s, 0))
    return out


def t_rise_up(A, B, p):
    S = A.width
    out = A.copy()
    out.paste(B, (0, int(S * (1 - _ease(p)))))
    return out


def t_zoom_in(A, B, p):
    e = _ease(p)
    return Image.blend(_zoom(A, 1 + 0.7 * e), B, e)


def t_zoom_out(A, B, p):
    e = _ease(p)
    return Image.blend(A, _zoom(B, 1 + 0.7 * (1 - e)), e)


def t_wipe(A, B, p):
    S = A.width
    x = int(S * _ease(p))
    return Image.composite(B, A, _mask(S, lambda d: d.rectangle([0, 0, x, S], fill=255)))


def t_circle_open(A, B, p):
    S = A.width
    r = _ease(p) * S * 0.78
    c = S / 2
    return Image.composite(B, A, _mask(S, lambda d: d.ellipse([c - r, c - r, c + r, c + r], fill=255)))


def t_blur(A, B, p):
    e = _ease(p)
    return Image.blend(_blur(A, 46 * e), _blur(B, 46 * (1 - e)), e)


def t_flash(A, B, p):
    S = A.width
    base = A if p < 0.5 else B
    w = (1 - abs(2 * p - 1)) ** 3
    return Image.blend(base, _solid(S, WHITE), w)


def t_doors_open(A, B, p):
    S = A.width
    sh = int(S / 2 * _ease(p))
    out = B.copy()
    out.paste(A.crop((0, 0, S // 2, S)), (-sh, 0))
    out.paste(A.crop((S // 2, 0, S, S)), (S // 2 + sh, 0))
    return out


def t_slide_from_left(A, B, p):
    S = A.width
    out = A.copy()
    out.paste(B, (-int(S * (1 - _ease(p))), 0))
    return out


def t_drop_down(A, B, p):
    S = A.width
    out = A.copy()
    out.paste(B, (0, -int(S * (1 - _ease(p)))))
    return out


def t_spin(A, B, p):
    S = A.width
    e = _ease(p)
    size = max(4, int(S * e))
    spun = B.rotate(360 * (1 - e), resample=Image.BILINEAR).resize((size, size), Image.BILINEAR)
    out = A.copy()
    out.paste(spun, ((S - size) // 2, (S - size) // 2))
    return out


def t_flip_card(A, B, p):
    S = A.width
    out = _solid(S, BLACK)
    if p < 0.5:
        w = max(2, int(S * math.cos(math.pi * p)))
        img = A.resize((w, S), Image.BILINEAR)
    else:
        w = max(2, int(S * -math.cos(math.pi * p)))
        img = B.resize((w, S), Image.BILINEAR)
    out.paste(img, ((S - w) // 2, 0))
    return out


def t_squeeze(A, B, p):
    S = A.width
    w = max(2, int(S * (1 - _ease(p))))
    out = B.copy()
    out.paste(A.resize((w, S), Image.BILINEAR), ((S - w) // 2, 0))
    return out


def t_whip_pan(A, B, p):
    S = A.width
    e = _ease(p)
    s = int(S * e)
    wide = Image.new("RGB", (S * 2, S))
    wide.paste(A, (0, 0))
    wide.paste(B, (S, 0))
    frame = wide.crop((s, 0, s + S, S))
    amt = 1 + 14 * math.sin(math.pi * p)
    smear = frame.resize((max(8, int(S / amt)), S), Image.BILINEAR).resize((S, S), Image.BILINEAR)
    return smear


def t_glitch(A, B, p):
    S = A.width
    base = A if p < 0.5 else B
    k = math.sin(math.pi * p)
    rng = random.Random(int(p * 997))
    out = base.copy()
    for _ in range(7):
        y = rng.randint(0, S - 40)
        h = rng.randint(14, 90)
        dx = int(rng.uniform(-1, 1) * 140 * k)
        out.paste(ImageChops.offset(base.crop((0, y, S, min(S, y + h))), dx, 0), (0, y))
    r, g, b = out.split()
    shift = int(28 * k)
    r = ImageChops.offset(r, shift, 0)
    b = ImageChops.offset(b, -shift, 0)
    return Image.merge("RGB", (r, g, b))


def t_pixels(A, B, p):
    S = A.width
    base = A if p < 0.5 else B
    ps = max(1, int(1 + 70 * math.sin(math.pi * p)))
    if ps <= 1:
        return base
    return base.resize((max(2, S // ps), max(2, S // ps)), Image.BOX).resize((S, S), Image.NEAREST)


def t_blinds(A, B, p):
    S = A.width
    n = 8
    sh = S / n
    e = _ease(p)

    def draw(d):
        for k in range(n):
            d.rectangle([0, int(k * sh), S, int(k * sh + sh * e) + 1], fill=255)

    return Image.composite(B, A, _mask(S, draw))


def t_diagonal_wipe(A, B, p):
    S = A.width
    e = _ease(p) * 2 * S
    return Image.composite(B, A, _mask(S, lambda d: d.polygon([(0, 0), (e, 0), (0, e)], fill=255)))


def t_split_open(A, B, p):
    S = A.width
    sh = int(S / 2 * _ease(p))
    out = B.copy()
    out.paste(A.crop((0, 0, S, S // 2)), (0, -sh))
    out.paste(A.crop((0, S // 2, S, S)), (0, S // 2 + sh))
    return out


def t_dip_to_black(A, B, p):
    S = A.width
    blk = _solid(S, BLACK)
    return Image.blend(A, blk, p * 2) if p < 0.5 else Image.blend(blk, B, p * 2 - 1)


def t_square_open(A, B, p):
    S = A.width
    h = _ease(p) * S / 2 + 1
    c = S / 2
    return Image.composite(B, A, _mask(S, lambda d: d.rectangle([c - h, c - h, c + h, c + h], fill=255)))


def t_push_up(A, B, p):
    S = A.width
    s = int(S * _ease(p))
    out = Image.new("RGB", (S, S))
    out.paste(A, (0, -s))
    out.paste(B, (0, S - s))
    return out


def t_push_down(A, B, p):
    S = A.width
    s = int(S * _ease(p))
    out = Image.new("RGB", (S, S))
    out.paste(A, (0, s))
    out.paste(B, (0, -S + s))
    return out


def t_wipe_from_right(A, B, p):
    S = A.width
    x = int(S * (1 - _ease(p)))
    return Image.composite(B, A, _mask(S, lambda d: d.rectangle([x, 0, S, S], fill=255)))


def t_wipe_up(A, B, p):
    S = A.width
    y = int(S * (1 - _ease(p)))
    return Image.composite(B, A, _mask(S, lambda d: d.rectangle([0, y, S, S], fill=255)))


def t_diamond_open(A, B, p):
    S = A.width
    r = _ease(p) * S * 1.02
    c = S / 2
    return Image.composite(B, A, _mask(S, lambda d: d.polygon([(c, c - r), (c + r, c), (c, c + r), (c - r, c)], fill=255)))


def t_star_open(A, B, p):
    S = A.width
    R = _ease(p) * S * 1.7
    return Image.composite(B, A, _mask(S, lambda d: d.polygon(_star_points(S / 2, S / 2, R), fill=255)))


def t_heart_open(A, B, p):
    S = A.width
    k = _ease(p) * S / 4.2
    return Image.composite(B, A, _mask(S, lambda d: d.polygon(_heart_points(S / 2, S / 2, k), fill=255)))


def t_clock_sweep(A, B, p):
    S = A.width
    end = -90 + 360 * _ease(p)
    return Image.composite(B, A, _mask(S, lambda d: d.pieslice([-S, -S, 2 * S, 2 * S], -90, end, fill=255)))


def t_falling_bars(A, B, p):
    S = A.width
    n = 10
    bw = S / n
    out = A.copy()
    e = _ease(p)
    for k in range(n):
        q = max(0.0, min(1.0, (e - (k / n) * 0.5) / 0.5))
        x0, x1 = int(k * bw), int((k + 1) * bw) + 1
        out.paste(B.crop((x0, 0, x1, S)), (x0, int(-(1 - q) * S)))
    return out


def t_checker_boxes(A, B, p):
    S = A.width
    n = 8
    cs = S / n
    e = _ease(p)

    def draw(d):
        for r in range(n):
            for c in range(n):
                start = 0.0 if (r + c) % 2 == 0 else 0.5
                f = max(0.0, min(1.0, (e - start) / 0.5))
                if f <= 0:
                    continue
                cx, cy = (c + 0.5) * cs, (r + 0.5) * cs
                h = cs / 2 * f * 1.02
                d.rectangle([cx - h, cy - h, cx + h, cy + h], fill=255)

    return Image.composite(B, A, _mask(S, draw))


def t_zoom_blur(A, B, p):
    e = _ease(p)
    return Image.blend(_zoom_blur(A, 0.35 * e), _zoom_blur(B, 0.35 * (1 - e)), e)


def t_bounce_in(A, B, p):
    S = A.width
    out = A.copy()
    out.paste(B, (0, int(-S * (1 - _bounce(p)))))
    return out


def t_shake(A, B, p):
    S = A.width
    base = A if p < 0.5 else B
    amp = 46 * math.sin(math.pi * p)
    rng = random.Random(int(p * 991))
    return ImageChops.offset(base, int(rng.uniform(-amp, amp)), int(rng.uniform(-amp, amp)))


def t_red_curtain(A, B, p):
    S = A.width
    base = (A if p < 0.5 else B).copy()
    cover = 1 - abs(2 * p - 1)
    w = int(S / 2 * min(1.0, cover * 1.15))
    if w <= 0:
        return base
    d = ImageDraw.Draw(base)
    for side in (0, 1):
        x0 = 0 if side == 0 else S - w
        d.rectangle([x0, 0, x0 + w, S], fill=(170, 18, 34))
        for x in range(x0, x0 + w, 22):
            d.rectangle([x, 0, x + 8, S], fill=(120, 10, 24))
            d.rectangle([x + 12, 0, x + 15, S], fill=(205, 40, 56))
    return base


def t_stretch(A, B, p):
    S = A.width
    e = _ease(p)

    def stretched(img, f):
        h = max(S, int(S * f))
        big = img.resize((S, h), Image.BILINEAR)
        o = (h - S) // 2
        return big.crop((0, o, S, o + S))

    return Image.blend(stretched(A, 1 + 3 * e), stretched(B, 1 + 3 * (1 - e)), e)


def t_fade_white(A, B, p):
    S = A.width
    wht = _solid(S, WHITE)
    return Image.blend(A, wht, _ease(p * 2)) if p < 0.5 else Image.blend(wht, B, _ease(p * 2 - 1))


def t_turn_in(A, B, p):
    S = A.width
    ang = 90 * (1 - _ease(p))
    rot = B.rotate(ang, center=(0, S), resample=Image.BILINEAR)
    m = Image.new("L", (S, S), 255).rotate(ang, center=(0, S), resample=Image.BILINEAR)
    return Image.composite(rot, A, m)


TRANSITIONS = [
    t_soft_fade, t_slide_in, t_push, t_rise_up, t_zoom_in, t_zoom_out, t_wipe, t_circle_open,
    t_blur, t_flash, t_doors_open, t_slide_from_left, t_drop_down, t_spin, t_flip_card,
    t_squeeze, t_whip_pan, t_glitch, t_pixels, t_blinds, t_diagonal_wipe, t_split_open,
    t_dip_to_black, t_square_open, t_push_up, t_push_down, t_wipe_from_right, t_wipe_up,
    t_diamond_open, t_star_open, t_heart_open, t_clock_sweep, t_falling_bars, t_checker_boxes,
    t_zoom_blur, t_bounce_in, t_shake, t_red_curtain, t_stretch, t_fade_white, t_turn_in,
]


# --------------------------------------------------------------------------
# Swoosh sound (generated, no audio file needed) and Gemini TTS
# --------------------------------------------------------------------------
SR = 24000


def _swoosh_samples(dur=0.55):
    n = int(SR * dur)
    rng = random.Random(11)
    lo = hi = 0.0
    out = []
    for i in range(n):
        t = i / n
        x = rng.uniform(-1, 1)
        a_hi = 0.04 + 0.62 * t      # band sweeps upward like a swoosh
        a_lo = 0.008 + 0.10 * t
        hi += a_hi * (x - hi)
        lo += a_lo * (x - lo)
        env = math.sin(math.pi * t) ** 2 * (1 - 0.25 * t)
        out.append((hi - lo) * env)
    peak = max(abs(v) for v in out) or 1.0
    return [v / peak * 0.8 for v in out]


def build_swoosh_track(times, total_seconds, out_path):
    """A wav with one swoosh centred on each photo-change time."""
    n_total = int((total_seconds + 1) * SR)
    track = [0.0] * n_total
    sw = _swoosh_samples()
    for t in times:
        start = int((t - 0.27) * SR)
        for i, v in enumerate(sw):
            j = start + i
            if 0 <= j < n_total:
                track[j] += v
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            int(max(-1.0, min(1.0, v)) * 32767).to_bytes(2, "little", signed=True) for v in track
        ))
    return out_path


def synthesize_gemini_voiceover(text, out_path, api_key, voice=None, models=None):
    """
    Gemini text-to-speech (voice "Zephyr" by default). Writes an mp3 to
    out_path and returns True; returns False on any failure so the caller can
    fall back to another voice. Never raises.
    """
    import requests

    voice = voice or os.environ.get("GEMINI_TTS_VOICE", "Zephyr")
    models = models or [
        m for m in (os.environ.get("GEMINI_TTS_MODEL"), "gemini-2.5-flash-preview-tts",
                    "gemini-2.5-pro-preview-tts") if m
    ]
    prompt = "Read this as a warm, light, friendly voiceover at a relaxed, natural pace: " + text
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    for model in models:
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key}, json=body, timeout=180,
            )
            if not res.ok:
                print(f"Gemini voice: {model} returned {res.status_code}.")
                continue
            part = res.json()["candidates"][0]["content"]["parts"][0]
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if not data:
                continue
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            m = re.search(r"rate=(\d+)", mime)
            rate = m.group(1) if m else "24000"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", "pipe:0",
                 "-c:a", "libmp3lame", "-b:a", "128k", out_path],
                input=base64.b64decode(data), check=True, capture_output=True,
            )
            return True
        except Exception as e:
            print(f"Gemini voice failed on {model}: {e}")
    return False


# --------------------------------------------------------------------------
# The video
# --------------------------------------------------------------------------
class _Slide:
    pass


def _prepare_slides(image_specs, S):
    B = int(S * 1.3)
    slides, t = [], 0.0
    for i, spec in enumerate(image_specs):
        s = _Slide()
        im = Image.open(io.BytesIO(spec["bytes"])).convert("RGB")
        scale = B / min(im.size)
        im = im.resize((max(B, round(im.width * scale)), max(B, round(im.height * scale))), Image.LANCZOS)
        left, top = (im.width - B) // 2, (im.height - B) // 2
        s.base = im.crop((left, top, left + B, top + B))
        s.B = B
        s.dir = 1 if i % 2 == 0 else -1
        s.dur = max(0.8, float(spec["duration"]))
        s.start, s.end = t, t + s.dur
        t = s.end
        s.chunks = []
        words = chunk_caption(spec.get("caption") or "")
        if spec.get("caption"):
            weights = [max(1, len(c)) for c in words]
            tot = float(sum(weights))
            c0 = s.start
            for k, (c, wgt) in enumerate(zip(words, weights)):
                c1 = c0 + s.dur * wgt / tot
                s.chunks.append((c0, c1, c, "outline" if k % 2 == 0 else "box"))
                c0 = c1
        slides.append(s)
    return slides, t


def _slide_frame(slide, local_t, S):
    u = max(0.0, min(1.0, local_t / slide.dur))
    L = slide.B * (0.96 - 0.12 * u)
    cx = slide.B / 2 + slide.dir * (u - 0.5) * slide.B * 0.05
    cy = slide.B / 2
    return slide.base.transform((S, S), Image.EXTENT, (cx - L / 2, cy - L / 2, cx + L / 2, cy + L / 2), Image.BILINEAR)


def build_template_video(image_specs, audio_path, work_dir, width=1080, height=1920,
                         top_text="", bottom_text="", bottom_highlight=None,
                         music_path=None, seed=None):
    """
    Renders the whole template video and returns the MP4 bytes.

    image_specs: [{"bytes": image bytes, "caption": str, "duration": seconds}, ...]
    """
    os.makedirs(work_dir, exist_ok=True)
    W, H = width, height
    S = W
    bars = H - S
    top_h = int(bars * 0.55)
    bot_h = bars - top_h
    line = 6

    slides, total = _prepare_slides(image_specs, S)
    n_frames = int(round(total * FPS))

    # static background: black, bars with text, yellow separator lines
    bg = Image.new("RGB", (W, H), BLACK)
    top_block = render_highlight_block(top_text, pick_highlight_words(top_text), W, top_h - 24, max_lines=3)
    bg.paste(top_block, (0, 12), top_block)
    bottom_block = render_highlight_block(
        bottom_text, bottom_highlight or pick_highlight_words(bottom_text), W, bot_h - 24, max_lines=2)
    bg.paste(bottom_block, (0, top_h + S + 12), bottom_block)
    d = ImageDraw.Draw(bg)
    d.rectangle([0, top_h - line, W, top_h - 1], fill=YELLOW)
    d.rectangle([0, top_h + S, W, top_h + S + line - 1], fill=YELLOW)

    rng = random.Random(seed)
    order = TRANSITIONS[:]
    rng.shuffle(order)
    boundaries = [s.end for s in slides[:-1]]
    transitions = [order[i % len(order)] for i in range(len(boundaries))]
    half = TRANSITION_SECONDS / 2

    swoosh_path = build_swoosh_track(boundaries, total, os.path.join(work_dir, "swoosh.wav"))

    caption_cache = {}

    def caption_img(text, style):
        key = (text, style)
        if key not in caption_cache:
            caption_cache[key] = render_caption(text, style, W)
        return caption_cache[key]

    out_path = os.path.join(work_dir, "template_final.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", audio_path]
    if music_path:
        cmd += ["-stream_loop", "-1", "-i", music_path]
    cmd += ["-i", swoosh_path]
    sw_idx = 3 if music_path else 2
    if music_path:
        af = (f"[1:a]volume=1.0[v];[2:a]volume=0.12[m];[{sw_idx}:a]volume=0.8[s];"
              f"[v][m][s]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        af = (f"[1:a]volume=1.0[v];[{sw_idx}:a]volume=0.8[s];"
              f"[v][s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    cmd += ["-filter_complex", af, "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out_path]

    err_path = os.path.join(work_dir, "ffmpeg_err.txt")
    with open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for fi in range(n_frames):
                t = fi / FPS
                active = next((k for k, s in enumerate(slides) if s.start <= t < s.end), len(slides) - 1)
                trans_k = next((j for j, b in enumerate(boundaries) if abs(t - b) <= half), None)
                if trans_k is not None:
                    b = boundaries[trans_k]
                    p = (t - (b - half)) / TRANSITION_SECONDS
                    A = _slide_frame(slides[trans_k], t - slides[trans_k].start, S)
                    Bf = _slide_frame(slides[trans_k + 1], max(0.0, t - slides[trans_k + 1].start), S)
                    win = transitions[trans_k](A, Bf, p)
                    cap_slide = slides[trans_k] if p < 0.5 else slides[trans_k + 1]
                else:
                    win = _slide_frame(slides[active], t - slides[active].start, S)
                    cap_slide = slides[active]

                chunk = None
                for c in cap_slide.chunks:
                    if c[0] <= t < c[1]:
                        chunk = c
                        break
                if chunk is None and cap_slide.chunks:
                    chunk = cap_slide.chunks[-1] if t >= cap_slide.end - 1e-6 else cap_slide.chunks[0]
                if chunk is not None:
                    cim = caption_img(chunk[2], chunk[3])
                    win = win.copy()
                    win.paste(cim, (0, S - cim.height - int(S * 0.04)), cim)

                canvas = bg.copy()
                canvas.paste(win, (0, top_h))
                ImageDraw.Draw(canvas).rectangle([0, 0, int(W * t / total), 9], fill=YELLOW)
                proc.stdin.write(canvas.tobytes())
            proc.stdin.close()
            code = proc.wait(timeout=300)
        except Exception:
            proc.kill()
            raise
    if code != 0:
        with open(err_path, "rb") as f:
            raise RuntimeError("ffmpeg failed: " + f.read().decode(errors="ignore")[-800:])
    with open(out_path, "rb") as f:
        return f.read()
