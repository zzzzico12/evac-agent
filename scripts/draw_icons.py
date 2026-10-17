"""PWA のアイコンを描く（案A〜C）。採用しているのは案C（地図ピンと家）。

使い方: agent/.venv/bin/python scripts/draw_icons.py
"""
import math
from PIL import Image, ImageDraw

SS = 4

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
def hexrgb(h): h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def gradient(S, c1, c2):
    """左上→右下の斜めグラデーション"""
    a, b = hexrgb(c1), hexrgb(c2)
    g = Image.new("RGB", (S, S))
    px = g.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(a, b, (x + y) / (2 * S - 2))
    return g

def gradient_fast(S, c1, c2):
    small = gradient(64, c1, c2)
    return small.resize((S, S), Image.BICUBIC)

def stroke(d, pts, w, fill):
    """太さ w の線を、円を細かく並べて描く（曲線でも縁がなめらかで、端も丸くなる）"""
    r = w / 2
    step = max(1.0, r / 4)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / step))
        for i in range(n + 1):
            x, y = x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n
            d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

def wave(cx, y, width, amp, periods=1.5, n=200):
    x0 = cx - width / 2
    return [(x0 + width * i / n, y + amp * math.sin(2 * math.pi * periods * i / n)) for i in range(n + 1)]

def base(S, c1, c2, maskable):
    bg = gradient_fast(S, c1, c2).convert("RGBA")
    if maskable:
        return bg
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)
    return out

# A：屋根のシェブロンと波（線画）
def variant_a(size, maskable=False):
    S = size * SS
    img = base(S, "#0b1d33", "#16426b", maskable)
    d = ImageDraw.Draw(img)
    k = 0.82 if maskable else 1.0
    cx, cy = S / 2, S / 2
    w = S * 0.075 * k
    span = S * 0.44 * k
    roof = [(cx - span / 2, cy - S * 0.02 * k), (cx, cy - S * 0.22 * k), (cx + span / 2, cy - S * 0.02 * k)]
    stroke(d, roof, w, "#ffffff")
    stroke(d, wave(cx, cy + S * 0.12 * k, S * 0.50 * k, S * 0.028 * k), w * 0.62, "#7cc4ff")
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    stroke(ImageDraw.Draw(layer), wave(cx, cy + S * 0.23 * k, S * 0.50 * k, S * 0.028 * k), w * 0.62, (124, 196, 255, 255))
    layer.putalpha(layer.getchannel("A").point(lambda a: a * 110 // 255))
    img.alpha_composite(layer)
    r = S * 0.028 * k
    d.ellipse([cx - r, cy - S * 0.22 * k - S * 0.11 * k - r, cx + r, cy - S * 0.22 * k - S * 0.11 * k + r], fill="#ff9e2c")
    return img.resize((size, size), Image.LANCZOS)

# B：上向きの矢印（屋根）と水位線。オレンジ一色
def variant_b(size, maskable=False):
    S = size * SS
    img = base(S, "#121417", "#23272e", maskable)
    d = ImageDraw.Draw(img)
    k = 0.82 if maskable else 1.0
    cx, cy = S / 2, S / 2
    w = S * 0.085 * k
    span = S * 0.40 * k
    for color, dy in [("#ff9900", -0.10), ("#a8661a", 0.04)]:
        y = cy + S * dy * k
        stroke(d, [(cx - span / 2, y + S * 0.10 * k), (cx, y - S * 0.06 * k), (cx + span / 2, y + S * 0.10 * k)], w, color)
    stroke(d, [(cx - S * 0.25 * k, cy + S * 0.25 * k), (cx + S * 0.25 * k, cy + S * 0.25 * k)], w * 0.45, "#d9dde3")
    return img.resize((size, size), Image.LANCZOS)

# C：地図ピンの中に家。白抜き
def variant_c(size, maskable=False):
    S = size * SS
    img = base(S, "#0f766e", "#0b3b5c", maskable)
    k = 0.80 if maskable else 1.0
    cx, cy = S / 2, S * 0.47
    R = S * 0.24 * k
    pin = Image.new("L", (S, S), 0)
    pd = ImageDraw.Draw(pin)
    pd.ellipse([cx - R, cy - R, cx + R, cy + R], fill=255)
    tip = cy + R * 1.75
    pd.polygon([(cx - R * 0.80, cy + R * 0.60), (cx + R * 0.80, cy + R * 0.60), (cx, tip)], fill=255)
    # ピンの中を家の形でくり抜く
    hw = R * 0.55
    top = cy - R * 0.50
    eave = cy - R * 0.05
    bot = cy + R * 0.45
    body = hw * 0.68
    pd.polygon([(cx, top), (cx + hw, eave), (cx + body, eave), (cx + body, bot), (cx - body, bot), (cx - body, eave), (cx - hw, eave)], fill=0)
    white = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    img.paste(white, (0, 0), pin)
    return img.resize((size, size), Image.LANCZOS)

if __name__ == "__main__":
    from pathlib import Path

    out = Path(__file__).resolve().parent.parent / "frontend" / "icons"
    variant_c(192).save(out / "icon-192.png")
    variant_c(512).save(out / "icon-512.png")
    variant_c(512, maskable=True).convert("RGB").save(out / "icon-maskable-512.png")
    variant_c(180, maskable=True).convert("RGB").save(out / "apple-touch-icon.png")
    print(f"アイコンを書き出しました → {out}")
