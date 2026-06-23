"""The 'brain': character matching, prompt assembly, batch helpers, contact
sheet compositor."""
import hashlib
import re
import threading
from collections import OrderedDict
from io import BytesIO

from PIL import Image

import config  # noqa: F401


# --------------------------------------------------------------------------- #
#  Character matching
# --------------------------------------------------------------------------- #
def match_characters(prompt, characters):
    """Return characters whose name appears as a whole word in the prompt.

    Also recognises explicit @Name mentions (which is what the cast chips
    insert) and these are treated as an unambiguous tag.
    """
    low = (prompt or "").lower()
    matched = []
    seen = set()
    for c in characters:
        name = (c.get("name") or "").strip()
        cid = c.get("id") or ""
        if not name or cid in seen:
            continue
        # Exact @mention match (case-insensitive).
        if re.search(r"@" + re.escape(name.lower()) + r"\b", low):
            matched.append(c); seen.add(cid); continue
        # bare-word form
        if re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", low):
            matched.append(c); seen.add(cid); continue
    return matched


def strip_tags(prompt):
    """Remove leading @ characters before names so the prompt reads naturally
    when passed to the image model."""
    return re.sub(r"@(\w)", r"\1", prompt or "")


# --------------------------------------------------------------------------- #
#  Batch parsing
# --------------------------------------------------------------------------- #
def split_lines_batch(text, mode="line"):
    """Split a multi-line prompt-batch text into individual prompts.

    mode='line'        — each non-empty line is one prompt
    mode='blank'       — prompts are separated by one or more blank lines
                         (allows multi-line prompts)
    """
    text = (text or "").strip()
    if not text:
        return []
    if mode == "blank":
        parts = re.split(r"\n\s*\n+", text)
    else:
        parts = text.splitlines()
    return [p.strip() for p in parts if p.strip()]


def parse_character_batch(text):
    """Parse a block of text into [{name, description}, ...].

    Each entry is separated by a blank line. The FIRST line of an entry is the
    name (may optionally be wrapped in @ or [] or end with a colon); the rest
    of the entry is the description.
    """
    out = []
    for entry in split_lines_batch(text, mode="blank"):
        lines = [l.strip() for l in entry.splitlines() if l.strip()]
        if not lines:
            continue
        first = lines[0]
        # strip optional decorations like "@Name", "Name:", "[Name]"
        first = re.sub(r"^[\[@]?", "", first)
        first = re.sub(r"[\]:]$", "", first)
        first = re.sub(r":$", "", first).strip()
        # If first line has "Name - description" or "Name: description" form,
        # split it.
        m = re.match(r"^([^\-:—]+?)\s*[\-:—]\s*(.+)$", lines[0])
        if m and len(lines) == 1:
            name = m.group(1).strip().lstrip("@[").rstrip("]:")
            desc = m.group(2).strip()
        else:
            name = first
            desc = " ".join(lines[1:]).strip()
        if name:
            out.append({"name": name, "description": desc})
    return out


# --------------------------------------------------------------------------- #
#  Prompt assembly
# --------------------------------------------------------------------------- #
_MASTER_HINT = (
    "You are rendering ONE frame in a continuous visual series. Every frame must "
    "live in the SAME universe, art style, color palette and lighting language."
)


# When the reference style is a flat 2D / stick-figure / explainer cartoon (like
# the Zenn / "The Bliss Point" look), gpt-image-2 tends to drift toward soft
# shading, gradients and 3D depth. This directive forces it back to the crisp,
# 100%-flat, bold-outline cartoon look. Applied ONLY when the style looks flat,
# so photoreal / 3D styles are unaffected.
_FLAT_STYLE_HINTS = (
    "flat 2d", "flat colour", "flat color", "2d cartoon", "cartoon",
    "stick figure", "stick-figure", "stick man", "vector", "line art",
    "line-art", "marker", "hand-drawn", "hand drawn", "doodle", "explainer",
    "comic", "cel-shad", "cel shad",
)
_FLAT_DIRECTIVE = (
    "FLAT-CARTOON RENDERING — MANDATORY: bold, even BLACK outlines of uniform "
    "weight on every shape; fill each shape with ONE solid flat colour. "
    "ABSOLUTELY NO gradients, NO soft/cel shading, NO ambient occlusion, NO 3D "
    "depth, NO photographic texture, NO drop shadows, NO highlights. Backgrounds "
    "are clean white or a single flat colour block. Characters are simple stick "
    "figures: round bald heads, tiny simple oval/dot eyes, thin single-line "
    "limbs, minimal facial detail. Props are simple shapes with the same bold "
    "outline. Crisp, clean, 2D — like a modern hand-inked explainer cartoon."
)


def _is_flat_style(*texts):
    blob = " ".join(t for t in texts if t).lower()
    return any(h in blob for h in _FLAT_STYLE_HINTS)


def ref_manifest(ref_meta):
    """When references are sent as SEPARATE images (multi_image_edit=True) the
    per-image captions that the contact-sheet would burn in are not visible to
    the model. Describe the attachments in order as a text manifest so the model
    still knows which image is the style swatch vs a character vs the previous
    frame — preserving 'copy style not composition' + identity guidance."""
    if not ref_meta:
        return ""
    lines = []
    for i, m in enumerate(ref_meta, 1):
        t = m.get("type")
        if t == "style":
            lines.append(f"  Image {i}: STYLE REF (source-video frame) — copy "
                         "its ART STYLE only (palette, line work, shading, "
                         "texture, proportions). Do NOT copy its composition, "
                         "camera, poses or background.")
        elif t == "character":
            nm = (m.get("name") or "").strip()
            lines.append(f"  Image {i}: CHARACTER SHEET for {nm} — match this "
                         "person's face, hair, build and outfit exactly. "
                         "Identity reference ONLY, not a composition.")
        elif t == "previous":
            lines.append(f"  Image {i}: PREVIOUS FRAME — continuity of the SAME "
                         "moment; keep setting/wardrobe/lighting, change only "
                         "the camera angle as the prompt says.")
    if not lines:
        return ""
    return ("ATTACHED REFERENCE IMAGES (in order) — read each for its stated "
            "purpose ONLY:\n" + "\n".join(lines))


def build_full_prompt(master_prompt, shot_prompt, matched, has_previous,
                      style_locked, style_notes="", micro_cut=False,
                      ref_meta=None, protagonist=""):
    parts = [_MASTER_HINT]
    _flat = _is_flat_style(style_notes, master_prompt)

    if (master_prompt or "").strip():
        parts.append("WORLD / STYLE BIBLE:\n" + master_prompt.strip())

    # Explicit text style description from the reference video analysis.
    # Written twice: once as a system-level mandate, once prepended to the shot.
    if (style_notes or "").strip():
        parts.append(
            "MANDATORY ART STYLE — reproduce this EXACTLY. "
            "This style overrides any default tendencies of the model. "
            "Every pixel must match: same rendering technique, same palette, "
            "same line weight, same lighting, same proportions:\n"
            + style_notes.strip()
        )

    if _flat:
        parts.append(_FLAT_DIRECTIVE)

    # PROTAGONIST LOCK — the single most important anti-"wrong-subject" guard.
    # Root cause of the elephant bug: when a scene's VO/prompt mentions a
    # predator or another animal ("predators smell newborns", "lions circle"),
    # the image model made the PREDATOR the hero and dropped the baby elephant
    # entirely — so a video about a baby elephant showed standalone leopards /
    # cheetah cubs. The fix: pin the story's protagonist as the mandatory
    # central subject of EVERY frame. Derive it from the matched character
    # sheets (preferred — they're the canonical cast) or the explicit
    # `protagonist` hint. Any other animal/character named in the prompt is a
    # SECONDARY element reacting to / threatening / observing the protagonist —
    # never a replacement for them.
    _lead = (protagonist or "").strip()
    if not _lead and matched:
        _lead = ", ".join(c["name"] for c in matched)
    if _lead:
        parts.append(
            f"PROTAGONIST LOCK (CRITICAL): the story's main subject is {_lead}. "
            f"{_lead} MUST be clearly visible as the CENTRAL subject of this "
            "frame. If the prompt or narration mentions a predator, another "
            "animal, or any other character (e.g. a lion, leopard, cheetah, "
            "hyena, crocodile, vulture, or a person), that other figure is a "
            "SECONDARY element — it is watching, stalking, threatening, or "
            "reacting to the protagonist, and must be shown TOGETHER WITH the "
            f"protagonist in the same frame. NEVER replace {_lead} with the "
            "predator or other animal. NEVER render the other animal alone. The "
            f"protagonist's species/identity must exactly match the character "
            "sheet — do not substitute a different animal."
        )

    if matched:
        names = ", ".join(c["name"] for c in matched)
        parts.append(
            "CHARACTER REFERENCES: the attached labelled character sheets define "
            f"the canonical look of {names}. Keep each named character's face, "
            "hair, build, outfit and colors identical to their sheet wherever "
            "they appear in this frame. Their SPECIES and anatomy must match the "
            "sheet exactly — do not morph them into a different animal or add "
            "anatomy that contradicts the sheet."
        )

    if micro_cut:
        # Same continuous moment as the attached PREV FRAME — a micro-cut.
        parts.append(
            "MICRO-CUT CONTINUITY: one attached image is the PREVIOUS frame and "
            "this shot is the SAME continuous moment — a tighter angle, push-in, "
            "reverse or small reaction on the SAME subject in the SAME place. "
            "Keep the exact same characters, wardrobe, setting, lighting and "
            "props as that frame; only change the camera framing/angle as the "
            "new prompt describes. It must feel like the very next instant, not a "
            "new scene."
        )
    elif has_previous:
        parts.append(
            "CONTINUITY: this frame is part of an ongoing series. Keep the SAME "
            "art style, palette, grain, line quality and world details as the "
            "rest of the series so it reads like one continuous production — but "
            "this is a NEW moment in the story: give it its OWN composition, "
            "camera angle, staging and action. Do NOT reuse the previous shot's "
            "framing or pose. Advance the scene; never repeat it."
        )

    if style_locked:
        parts.append(
            "STYLE ANCHORS: the FIRST attached reference images are frames taken "
            "straight from the source video (in a labelled grid they are marked "
            "\"STYLE REF\"). Use them for ART STYLE ONLY — the EXACT rendering "
            "technique, line work, colour palette, shading, lighting, texture and "
            "proportions. CRITICAL: do NOT copy their composition, camera angle, "
            "subject placement, poses or background. They are a style swatch, NOT "
            "a layout to reproduce. This new frame must have its OWN distinct "
            "composition driven by the prompt below — never re-stage what a STYLE "
            "REF shows. Copy the LOOK, invent the SHOT. Do not drift toward a "
            "generic or more realistic style. Any character-sheet image defines "
            "character identity ONLY. If the reference look and your defaults "
            "disagree, the source video frames win."
        )

    # Prepend the style notes directly onto the shot prompt as well so it
    # appears twice (instruction block + prompt prefix) — this strongly anchors
    # the model to the right look even when references are ignored.
    raw_shot = strip_tags(shot_prompt).strip()
    if (style_notes or "").strip() and not raw_shot.lower().startswith(
            style_notes.strip()[:20].lower()):
        raw_shot = style_notes.strip().rstrip(".") + ". " + raw_shot

    # When refs are fed as SEPARATE images, the contact-sheet captions don't
    # exist — describe each attachment in order so the model still knows which
    # image is style vs character vs previous frame.
    _manifest = ref_manifest(ref_meta)
    if _manifest:
        parts.append(_manifest)

    parts.append("NEW FRAME TO RENDER:\n" + raw_shot)
    return "\n\n".join(parts)


def build_sheet_prompt(master_prompt, name, description, style_notes=""):
    parts = [f'Character model / reference sheet for "{name}".']
    if (description or "").strip():
        parts.append(f"Character description: {description.strip()}.")
    parts.append(
        "Layout: one clean reference sheet containing a full-body turnaround "
        "(front, 3/4, side, back), a large headshot, and a row of facial "
        "expressions. Keep proportions and design identical across every view. "
        "Neutral flat background, soft even lighting. Do NOT make a generic "
        "character sheet: the line weight, proportions, face simplicity, palette, "
        "texture and rendering must match the source-video style exactly."
    )
    parts.append(
        f'Print the name "{name}" clearly as a label at the top of the sheet so '
        "it can be referenced by name later."
    )
    if (style_notes or "").strip():
        parts.append(
            "Art style for this character (must match the reference video look):\n"
            + style_notes.strip()
        )
    if _is_flat_style(style_notes, master_prompt):
        parts.append(_FLAT_DIRECTIVE)
    if (master_prompt or "").strip():
        parts.append("World / style bible:\n" + master_prompt.strip())
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
#  Aspect-ratio enforcement
# --------------------------------------------------------------------------- #
def parse_size(size):
    """'1536x1024' -> (1536, 1024). Returns None for 'auto'/blank/garbage."""
    if not size or str(size).lower() in ("auto", ""):
        return None
    try:
        w, h = str(size).lower().split("x")
        w, h = int(w), int(h)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return None


def enforce_aspect(image_bytes, size, tol=0.04):
    """Guarantee the returned image matches the requested aspect ratio.

    gpt-image's /images/edits endpoint frequently ignores the `size` param and
    anchors the output to the *reference* image's aspect (so a portrait-shaped
    contact-sheet of refs yields a 9:16 frame even when 1536x1024 was asked).
    This is the root cause of "I selected 16:9 but got 9:16".

    We fix it deterministically, provider-agnostic: if the produced image's
    aspect is off by more than ``tol``, CENTER-CROP it to the target aspect
    (never stretch — that would distort faces), then resize to exactly the
    requested pixels. If it's already correct, only resize to exact dims.
    """
    target = parse_size(size)
    if target is None:
        return image_bytes
    tw, th = target
    target_ar = tw / th
    try:
        im = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return image_bytes
    w, h = im.size
    if w <= 0 or h <= 0:
        return image_bytes
    cur_ar = w / h
    # Only crop when the aspect is genuinely wrong (e.g. landscape asked,
    # portrait returned). A small rounding mismatch is left to the final resize.
    if abs(cur_ar - target_ar) / target_ar > tol:
        if cur_ar > target_ar:
            # too wide -> crop left/right
            new_w = int(round(h * target_ar))
            x0 = (w - new_w) // 2
            im = im.crop((x0, 0, x0 + new_w, h))
        else:
            # too tall (the 9:16 bug) -> crop top/bottom
            new_h = int(round(w / target_ar))
            y0 = (h - new_h) // 2
            im = im.crop((0, y0, w, y0 + new_h))
    # Resize to the exact requested pixel dims so downstream video assembly
    # gets a uniform frame size (mixed sizes break ffmpeg concat / scaling).
    if im.size != (tw, th):
        im = im.resize((tw, th), Image.LANCZOS)
    out = BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


# --------------------------------------------------------------------------- #
#  Contact sheet fallback
# --------------------------------------------------------------------------- #
def contact_sheet(images, labels=None, max_cols=2, cell=896, bg=(17, 17, 19),
                  target_size=None):
    """Composite reference images into one grid. When ``labels`` is given, each
    cell gets a readable caption (e.g. "STYLE REF", "CHAR: Maya", "PREV FRAME")
    so the image model can tell the style anchor from a character sheet from the
    previous frame — critical for faithfully copying the reference art style
    instead of blending all the refs together."""
    from PIL import ImageDraw, ImageFont
    imgs = []
    for b in images:
        try:
            imgs.append(Image.open(BytesIO(b)).convert("RGB"))
        except Exception:
            continue
    if not imgs:
        raise ValueError("no valid reference images to composite")

    labels = list(labels or [])
    n = len(imgs)

    def _make_font(px):
        for _fname in ("arialbd.ttf", "Arial_Bold.ttf",
                       "DejaVuSans-Bold.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(_fname, px)
            except Exception:
                continue
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def _is_style(idx):
        return idx < len(labels) and "STYLE REF" in (labels[idx] or "").upper()

    def _paste(canvas, draw, im, ox, oy, box, lbl, lbl_px):
        """Paste ``im`` fitted into a ``box``-square at (ox, oy), captioned."""
        im = im.copy()
        im.thumbnail((box, box))
        x = ox + (box - im.width) // 2
        y = oy + (box - im.height) // 2
        canvas.paste(im, (x, y))
        if not lbl:
            return
        font = _make_font(lbl_px)
        if font is None:
            return
        try:
            tw = int(draw.textlength(lbl, font=font))
        except Exception:
            tw = len(lbl) * lbl_px
        pad = max(3, box // 80)
        bar_h = lbl_px + pad * 2
        draw.rectangle([ox, oy, ox + tw + pad * 3, oy + bar_h], fill=(0, 0, 0))
        draw.text((ox + pad, oy + pad), lbl, fill=(255, 214, 110), font=font)

    # The STYLE REF is the source of truth for the look. When it is present
    # alongside other refs, give it its own LARGE dedicated cell across the top
    # of the sheet so its rendering technique / palette / linework dominate the
    # grid instead of being squished into a tiny equal-sized thumbnail. The
    # remaining refs (character sheets, previous frame) sit in a smaller row
    # below as supporting identity/continuity material.
    style_idx = next((i for i in range(n) if _is_style(i)), None)
    if style_idx is not None and n > 1:
        others = [i for i in range(n) if i != style_idx]
        ocols = min(max_cols, len(others)) or 1
        small = max(1, cell // 2)              # supporting refs are half-size
        big_w = ocols * small                  # style cell spans the full width
        big_h = cell                           # full-size square for the style ref
        orows = (len(others) + ocols - 1) // ocols
        canvas = Image.new("RGB", (big_w, big_h + orows * small), bg)
        draw = ImageDraw.Draw(canvas)
        big_lbl = max(26, big_h // 24)
        _paste(canvas, draw, imgs[style_idx], 0, 0, big_w,
               labels[style_idx] if style_idx < len(labels) else "STYLE REF — COPY THIS LOOK",
               big_lbl)
        # constrain the (possibly wide) style image to the big cell height
        sm_lbl = max(18, small // 26)
        for j, oi in enumerate(others):
            r, c = divmod(j, ocols)
            _paste(canvas, draw, imgs[oi], c * small, big_h + r * small, small,
                   labels[oi] if oi < len(labels) else "", sm_lbl)
        canvas = _fit_canvas_to_aspect(canvas, target_size, bg)
        out = BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()

    # Uniform grid (no style ref, or a single image).
    cols = 1 if n == 1 else min(max_cols, n)
    rows = (n + cols - 1) // cols
    canvas = Image.new("RGB", (cols * cell, rows * cell), bg)
    draw = ImageDraw.Draw(canvas)
    lbl_px = max(22, cell // 28)
    for idx, im in enumerate(imgs):
        r, c = divmod(idx, cols)
        _paste(canvas, draw, im, c * cell, r * cell, cell,
               labels[idx] if idx < len(labels) else "", lbl_px)
    canvas = _fit_canvas_to_aspect(canvas, target_size, bg)
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _fit_canvas_to_aspect(canvas, target_size, bg):
    """Letterbox/pillarbox the ref grid onto a canvas with the TARGET aspect.

    The edit model anchors its output aspect to the input image. If we hand it a
    portrait-shaped grid while the user asked for 16:9, we get a portrait frame.
    Padding the grid into a target-aspect canvas biases the model toward the
    right orientation (enforce_aspect() is still the hard guarantee downstream).
    """
    target = parse_size(target_size)
    if target is None:
        return canvas
    tw, th = target
    target_ar = tw / th
    w, h = canvas.size
    cur_ar = w / h
    if abs(cur_ar - target_ar) / target_ar <= 0.02:
        return canvas
    if cur_ar < target_ar:
        new_w = int(round(h * target_ar))
        out = Image.new("RGB", (new_w, h), bg)
        out.paste(canvas, ((new_w - w) // 2, 0))
    else:
        new_h = int(round(w / target_ar))
        out = Image.new("RGB", (w, new_h), bg)
        out.paste(canvas, (0, (new_h - h) // 2))
    return out


# Bounded LRU cache of vision-downsized bytes keyed by (sha1(source), max_side,
# quality). The same rendered frames are re-downsized on every plan_edit /
# edit-plan / character-check request over a sequence (one PIL decode + resize +
# JPEG encode per frame, measured ~12 ms each → ~0.5 s wasted per 40-frame
# request). Memoising by content hash makes repeat requests effectively free
# while staying correct: a different image (different bytes) gets a different key.
_VISION_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_VISION_CACHE_MAX = 256
_VISION_CACHE_LOCK = threading.Lock()


def _downsize_for_vision_uncached(image_bytes, max_side, quality):
    try:
        im = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return image_bytes
    w, h = im.size
    scale = min(1.0, max_side / max(w, h))
    if scale >= 1.0:
        # No downscale needed. Re-encoding a small source as JPEG only inflates
        # the payload (measured 1.7–1.8× larger for ≤1024px frames) for no
        # quality gain, so return the original bytes — they're already a valid,
        # sniffable image and smaller on the wire.
        return image_bytes
    im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = BytesIO()
    im.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def downsize_for_vision(image_bytes, max_side=1024, quality=85):
    """Make an image small enough to be efficient for Claude vision calls.

    Memoised by source-content hash: repeated calls on the same frame (every
    edit-plan / consistency-check request re-downsizes the whole sequence)
    return the cached result instead of re-decoding and re-encoding.
    """
    if not image_bytes:
        return image_bytes
    key = f"{hashlib.sha1(image_bytes).hexdigest()}:{max_side}:{quality}"
    with _VISION_CACHE_LOCK:
        hit = _VISION_CACHE.get(key)
        if hit is not None:
            _VISION_CACHE.move_to_end(key)
            return hit
    result = _downsize_for_vision_uncached(image_bytes, max_side, quality)
    with _VISION_CACHE_LOCK:
        _VISION_CACHE[key] = result
        _VISION_CACHE.move_to_end(key)
        while len(_VISION_CACHE) > _VISION_CACHE_MAX:
            _VISION_CACHE.popitem(last=False)
    return result
