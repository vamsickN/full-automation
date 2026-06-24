#!/usr/bin/env python3
"""Generate Continuity Studio app icon (gradient infinity-loop 'continuity' mark).
Outputs a multi-size .ico + a 512px PNG. Pure PIL, no external assets."""
import math
from PIL import Image, ImageDraw

S = 1024  # supersample canvas, downscale at the end for crisp AA

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def rounded_rect_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m

# --- background: diagonal dark gradient rounded square ---
bg = Image.new("RGB", (S, S), (0, 0, 0))
px = bg.load()
c_tl = (26, 22, 46)    # deep indigo
c_br = (12, 14, 28)    # near-black blue
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        px[x, y] = lerp(c_tl, c_br, t)

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
mask = rounded_rect_mask(S, int(S * 0.22))
icon.paste(bg, (0, 0), mask)

# --- the mark: an infinity / continuous-loop ribbon (continuity) ---
# Draw a lemniscate (figure-8) as a thick gradient stroke.
draw = ImageDraw.Draw(icon)
cx, cy = S / 2, S / 2
a = S * 0.30          # lobe size
stroke = int(S * 0.085)
N = 1400
grad_start = (150, 70, 240)   # purple
grad_mid   = (60, 130, 255)   # electric blue
grad_end   = (40, 225, 235)   # cyan

pts = []
for i in range(N + 1):
    th = (i / N) * 2 * math.pi
    denom = 1 + math.sin(th) ** 2
    x = cx + (a * math.cos(th)) / denom
    y = cy + (a * math.sin(th) * math.cos(th)) / denom
    pts.append((x, y))

# stroke as overlapping circles, color cycling across the loop
for i, (x, y) in enumerate(pts):
    t = i / N
    if t < 0.5:
        col = lerp(grad_start, grad_mid, t * 2)
    else:
        col = lerp(grad_mid, grad_end, (t - 0.5) * 2)
    r = stroke / 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=col + (255,))

# --- three small 'frame' dots tracing the loop (filmstrip continuity hint) ---
for frac, fr_col in [(0.0, grad_start), (0.5, grad_mid), (0.25, grad_end)]:
    idx = int(frac * N)
    x, y = pts[idx]
    rr = stroke * 0.34
    draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(255, 255, 255, 235))

# re-apply rounded mask so the stroke doesn't bleed past corners
final = Image.new("RGBA", (S, S), (0, 0, 0, 0))
final.paste(icon, (0, 0), mask)

# downscale for AA
png = final.resize((512, 512), Image.LANCZOS)
png.save("assets/icon.png")

# multi-size ICO
ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
final.resize((256, 256), Image.LANCZOS).save("assets/icon.ico", sizes=ico_sizes)
print("wrote assets/icon.png (512) and assets/icon.ico", ico_sizes)
