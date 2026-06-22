"""Thin client around the derouter (OpenAI-compatible) image endpoint,
plus an OpenRouter client for chat-based image models (Sourceful Riverflow).

generate() uses the OpenAI SDK (clean), edit() uses raw multipart so we can
attach reference images (character sheets + previous frame + style anchors).

By default we composite multiple references into ONE contact-sheet PNG and
send it as the single documented `image` multipart field — that's the only
field name the derouter docs document. If your proxy supports repeated
`image[]` fields, set MULTI_IMAGE_EDIT=true in .env and pass the list straight
through; this client supports both modes.
"""
import base64
import json
import sys
import time

import requests
from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, AuthenticationError

import config
import image_queue


def _log(msg):
    print(f"[derouter] {msg}", file=sys.stderr, flush=True)


def _enforce_aspect(image_bytes, size):
    """Force the produced image to the requested aspect/pixels. Lazy-imports
    pipeline to avoid a circular import at module load. Never raises — on any
    failure it returns the original bytes so a render is never lost."""
    try:
        import pipeline
        fixed = pipeline.enforce_aspect(image_bytes, size)
        if fixed and fixed != image_bytes:
            _log(f"enforced aspect -> {size}")
        return fixed
    except Exception as e:
        _log(f"aspect-enforce skipped ({type(e).__name__}: {e})")
        return image_bytes


# Base URLs whose images/edits endpoint is known broken (9Router returns 500);
# edit() renders prompt-only via generate() for these instead of failing.
_EDITS_UNSUPPORTED = set()


class ImageClient:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=None):
        self.api_key = api_key or config.API_KEY
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.model = model or config.MODEL
        self.timeout = timeout or config.TIMEOUT
        # SDK is used for generations; api_key may be empty until set in UI.
        self._sdk = OpenAI(
            api_key=self.api_key or "unset",
            base_url=self.base_url,
            timeout=self.timeout,
        )
        # Lazily-built OpenRouter client used as a fallback when the primary
        # derouter endpoint returns an HTTP 402 billing/wallet error. Only
        # created on first need and only if a key + the toggle are present.
        self._or_fallback = None

    # ------------------------------------------------------------------ #
    #  Billing / 402 handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _status_code(e):
        """Best-effort HTTP status from an OpenAI SDK exception."""
        code = getattr(e, "status_code", None)
        if code:
            return code
        resp = getattr(e, "response", None)
        if resp is not None:
            return getattr(resp, "status_code", None)
        return None

    @classmethod
    def _is_billing_error(cls, e):
        """True if an exception looks like an HTTP 402 wallet/balance error."""
        if cls._status_code(e) == 402:
            return True
        low = (str(e) or "").lower()
        return ("402" in low or "wallet-balance" in low
                or "wallet balance" in low or "slot reservation failed" in low
                or "payment_required" in low or "insufficient_quota" in low)

    def _billing_message(self, e):
        return (
            "Image provider billing error (HTTP 402) at "
            f"{self.base_url}: the derouter wallet balance is too low to "
            "reserve a render slot (\"Slot reservation failed (wallet-balance)\"). "
            "Top up the derouter wallet, lower DEFAULT_QUALITY, or set "
            "OPENROUTER_API_KEY to fall back to a free OpenRouter image model. "
            f"[{self._format_openai_error(e)}]"
        )

    def _openrouter_fallback(self):
        """Return a ready OpenRouterImageClient if fallback is enabled and a
        key is configured, else None."""
        if not config.IMAGE_FALLBACK_ON_402:
            return None
        if not config.OPENROUTER_API_KEY:
            return None
        if self._or_fallback is None:
            self._or_fallback = OpenRouterImageClient()
        return self._or_fallback

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No image API key set. Add DEROUTER_API_KEY to your .env or "
                "paste a key in the Settings panel."
            )

    # ------------------------------------------------------------------ #
    #  Connectivity check — cheap, lists available models.
    # ------------------------------------------------------------------ #
    def ping(self):
        """Hit /models with the configured key to verify auth + reachability.
        Returns {'ok': True, 'models': [...]} or {'ok': False, 'error': str}.
        """
        if not self.api_key:
            return {"ok": False, "error": "no api key set"}
        url = f"{self.base_url}/models"
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20,
            )
        except requests.RequestException as e:
            return {"ok": False, "error": f"connection failed: {e}"}
        if r.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {r.status_code}: {r.text[:300]}",
            }
        try:
            data = r.json()
            ids = [m.get("id") for m in (data.get("data") or [])][:30]
        except Exception:
            ids = []
        return {"ok": True, "models": ids, "configured_model": self.model,
                "base_url": self.base_url}

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_openai_error(e):
        """Pull the real reason out of an OpenAI SDK exception, including
        the raw response body if it's available, so the user sees what
        derouter actually said rather than a generic 'BadRequestError'."""
        klass = type(e).__name__
        msg = str(e) or "no message"
        body = None
        # The SDK exposes the response on most error subclasses.
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text[:600]
            except Exception:
                body = None
        if body:
            return f"{klass}: {msg} | response: {body}"
        return f"{klass}: {msg}"

    # ------------------------------------------------------------------ #
    #  Size normalisation — gpt-image requires both dims divisible by 16
    # ------------------------------------------------------------------ #
    @staticmethod
    def _snap_size(size: str) -> str:
        """Round each dimension DOWN to the nearest multiple of 16 so the API
        never rejects sizes like '1920x1080' (1080 / 16 = 67.5 → 1072)."""
        if not size or size == "auto":
            return size
        try:
            w, h = size.lower().split("x")
            w16 = (int(w) // 16) * 16
            h16 = (int(h) // 16) * 16
            snapped = f"{w16}x{h16}"
            if snapped != size:
                _log(f"size {size} → {snapped} (rounded to nearest ×16)")
            return snapped
        except Exception:
            return size

    # ------------------------------------------------------------------ #
    #  Public
    # ------------------------------------------------------------------ #
    def generate(self, prompt, size=None, quality=None, retry=True, index=0):
        """Text -> image. Returns PNG bytes.

        With ``retry=True`` (default) the call is routed through the shared
        image_queue throttle: bounded concurrency, request spacing, exponential
        backoff on server errors and a global cooldown on rate-limit/429 errors.
        Pass ``retry=False`` when an outer queue is already managing retries.
        """
        if not retry:
            return self._generate_once(prompt, size, quality)
        return image_queue.run_with_retry(
            lambda: self._generate_once(prompt, size, quality),
            index=index, model=self.model, label="generate")

    def _generate_once(self, prompt, size=None, quality=None):
        self._require_key()
        size = self._snap_size(size or config.DEFAULT_SIZE)
        quality = quality or config.DEFAULT_QUALITY
        kwargs = {"model": self.model, "prompt": prompt}
        if size and size != "auto":
            kwargs["size"] = size
        if quality and quality != "auto":
            kwargs["quality"] = quality

        _log(f"generate model={self.model} size={size} quality={quality} "
             f"prompt_len={len(prompt)} base={self.base_url}")
        t0 = time.time()
        try:
            r = self._sdk.images.generate(**kwargs)
        except (APIConnectionError, APITimeoutError) as e:
            raise RuntimeError(
                f"could not reach image API at {self.base_url} — "
                f"{self._format_openai_error(e)}"
            )
        except AuthenticationError as e:
            raise RuntimeError(
                f"image API rejected the key — {self._format_openai_error(e)}"
            )
        except APIError as e:
            # HTTP 402 wallet/balance: try the OpenRouter fallback (if a key is
            # set + the toggle is on), otherwise raise a clear billing message.
            if self._is_billing_error(e):
                fb = self._openrouter_fallback()
                if fb is not None:
                    _log("derouter 402 (wallet-balance) — falling back to "
                         f"OpenRouter model={fb.model}")
                    return fb._generate_once(prompt, size, quality)
                raise RuntimeError(self._billing_message(e))
            raise RuntimeError(
                f"image API error — {self._format_openai_error(e)}"
            )
        dt = time.time() - t0
        _log(f"generate ok in {dt:.1f}s")

        if not r.data or not getattr(r.data[0], "b64_json", None):
            raise RuntimeError(
                f"image API returned no b64_json (got: {r.model_dump_json()[:300]})"
            )
        return _enforce_aspect(base64.b64decode(r.data[0].b64_json), size)

    def edit(self, prompt, images, size=None, quality=None, mask=None,
             retry=True, index=0, multi_image_edit=None):
        """Reference image(s) + prompt -> image. See ``generate`` for the retry
        semantics; ``retry=True`` (default) routes through the image_queue
        throttle (backoff + cooldown + concurrency).

        ``multi_image_edit`` overrides ``config.MULTI_IMAGE_EDIT`` for this call
        only (set True to send refs as repeated `image[]` fields, False to
        composite into a single contact-sheet). Falls back to the env default."""
        use_multi = (config.MULTI_IMAGE_EDIT if multi_image_edit is None
                     else bool(multi_image_edit))
        if not retry:
            return self._edit_once(prompt, images, size=size, quality=quality,
                                   mask=mask, multi_image_edit=use_multi)
        return image_queue.run_with_retry(
            lambda: self._edit_once(prompt, images, size=size, quality=quality,
                                    mask=mask, multi_image_edit=use_multi),
            index=index, model=self.model, label="edit")

    def _edit_once(self, prompt, images, size=None, quality=None, mask=None,
                   multi_image_edit=None):
        """Reference image(s) + prompt -> image. ``images`` is list[bytes].
        ``multi_image_edit`` (None=use config) toggles repeated `image[]` vs
        single contact-sheet delivery.

        ``mask`` (optional PNG bytes) enables inpainting: transparent areas of
        the mask are the regions the model repaints (OpenAI images/edits spec).

        With config.MULTI_IMAGE_EDIT=False (default + only path documented by
        derouter), the caller is expected to have already composited multiple
        refs into a single PNG; we still defensively handle the case where
        len(images)>1 by sending only the first.

        With MULTI_IMAGE_EDIT=True, we send repeated `image[]` fields — only
        do this if you've verified your proxy supports it.

        Returns PNG bytes.
        """
        self._require_key()
        if not images:
            raise ValueError("edit() needs at least one reference image")
        size = self._snap_size(size or config.DEFAULT_SIZE)
        quality = quality or config.DEFAULT_QUALITY

        # Some routers (9Router) serve images/generations but 500 on
        # images/edits. Once a base URL is known not to support edits, render
        # prompt-only via generate() instead of failing every frame — the
        # style_notes text prefix still carries the look, just without the
        # reference-image guidance.
        if self.base_url in _EDITS_UNSUPPORTED:
            _log("edits unsupported on this base — generate() fallback (no refs)")
            return self._generate_once(prompt, size, quality)

        use_multi = (config.MULTI_IMAGE_EDIT if multi_image_edit is None
                     else bool(multi_image_edit))
        files = []
        if use_multi and len(images) > 1:
            for i, img in enumerate(images):
                files.append(("image[]", (f"ref_{i}.png", img, "image/png")))
            mode = f"image[]x{len(images)}"
        else:
            # The documented derouter path: ONE image field.
            files.append(("image", ("ref.png", images[0], "image/png")))
            mode = "image (single)"

        if mask is not None:
            files.append(("mask", ("mask.png", mask, "image/png")))
            mode += " +mask"

        data = {"model": self.model, "prompt": prompt}
        if size and size != "auto":
            data["size"] = size
        if quality and quality != "auto":
            data["quality"] = quality

        url = f"{self.base_url}/images/edits"
        _log(f"edit url={url} model={self.model} size={size} quality={quality} "
             f"refs={len(images)} mode={mode} prompt_len={len(prompt)}")
        t0 = time.time()
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(
                f"could not reach image API at {url} — {type(e).__name__}: {e}"
            )
        dt = time.time() - t0
        _log(f"edit -> HTTP {resp.status_code} in {dt:.1f}s")

        if resp.status_code >= 400:
            # A local router 500ing the edits endpoint in ~2s means the endpoint
            # itself is unsupported (9Router) — remember that and fall back to
            # plain generation now and for every subsequent frame.
            if (resp.status_code == 500
                    and ("localhost" in self.base_url
                         or "127.0.0.1" in self.base_url)):
                _EDITS_UNSUPPORTED.add(self.base_url)
                _log(f"edits endpoint unsupported at {self.base_url} — "
                     f"falling back to generate() for this and future frames")
                return self._generate_once(prompt, size, quality)
            # HTTP 402 wallet/balance: try the OpenRouter fallback (with refs),
            # otherwise raise a clear, actionable billing message.
            if resp.status_code == 402 or self._is_billing_error(resp.text):
                fb = self._openrouter_fallback()
                if fb is not None:
                    _log("derouter 402 (wallet-balance) on edit — falling back "
                         f"to OpenRouter model={fb.model}")
                    return fb._edit_once(prompt, images, size, quality)
                raise RuntimeError(self._billing_message(
                    f"HTTP 402 @ {url} response: {resp.text[:300]}"))
            raise RuntimeError(
                f"image edit failed [HTTP {resp.status_code}] @ {url} "
                f"response: {resp.text[:600]}"
            )
        try:
            out = resp.json()
        except ValueError:
            raise RuntimeError(
                f"image edit returned non-JSON: {resp.text[:400]}"
            )
        if not out.get("data") or not out["data"][0].get("b64_json"):
            raise RuntimeError(
                f"image edit returned no b64_json: {json.dumps(out)[:400]}"
            )
        return _enforce_aspect(base64.b64decode(out["data"][0]["b64_json"]), size)


class OpenRouterImageClient:
    """Image generation via OpenRouter's chat completions API.

    Models like Sourceful Riverflow return images through the standard
    chat/completions endpoint with ``modalities: ["image", "text"]``.
    Images come back as base64 data URLs in the assistant message.
    """

    def __init__(self, api_key=None, base_url=None, model=None, timeout=None):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        m = model or config.OPENROUTER_MODEL
        if m and m not in config.OPENROUTER_MODELS:
            _log(f"model {m!r} not in known list, falling back to {config.OPENROUTER_MODEL}")
            m = config.OPENROUTER_MODEL
        self.model = m
        self.timeout = timeout or config.OPENROUTER_TIMEOUT

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No OpenRouter API key set. Add OPENROUTER_API_KEY to your .env "
                "or paste a key in the Settings panel."
            )

    def ping(self):
        if not self.api_key:
            return {"ok": False, "error": "no api key set"}
        url = f"{self.base_url}/models"
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20,
            )
        except requests.RequestException as e:
            return {"ok": False, "error": f"connection failed: {e}"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "configured_model": self.model, "base_url": self.base_url}

    def _extract_image(self, resp_json):
        """Pull the first base64 image from a chat completion response."""
        choices = resp_json.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {json.dumps(resp_json)[:400]}")
        msg = choices[0].get("message") or {}
        # Format 1: message.images[] array (OpenRouter SDK style)
        images = msg.get("images") or []
        if images:
            url = images[0].get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                b64 = url.split(",", 1)[-1]
                return base64.b64decode(b64)
        # Format 2: inline data URLs in content parts
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        b64 = url.split(",", 1)[-1]
                        return base64.b64decode(b64)
        # Format 3: data URL embedded in text content
        if isinstance(content, str) and "data:image" in content:
            import re
            m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
            if m:
                return base64.b64decode(m.group(1))
        raise RuntimeError(
            f"OpenRouter response contained no image data: {json.dumps(resp_json)[:500]}"
        )

    def generate(self, prompt, size=None, quality=None, retry=True, index=0):
        if not retry:
            return self._generate_once(prompt, size, quality)
        return image_queue.run_with_retry(
            lambda: self._generate_once(prompt, size, quality),
            index=index, model=self.model, label="openrouter-generate")

    @staticmethod
    def _parse_size(size):
        if not size or size == "auto":
            return None, None
        try:
            w, h = size.split("x")
            return int(w), int(h)
        except Exception:
            return None, None

    @staticmethod
    def _size_instruction(size):
        """Turn a WxH size string into a hard prompt instruction."""
        if not size or size == "auto":
            return ""
        try:
            w, h = size.split("x")
            w, h = int(w), int(h)
        except Exception:
            return ""
        if w > h:
            ratio = "16:9 widescreen landscape"
        elif h > w:
            ratio = "9:16 vertical portrait"
        else:
            ratio = "1:1 square"
        return (f"\n\n[OUTPUT REQUIREMENTS] The output image MUST be {ratio} "
                f"aspect ratio, resolution {w}x{h} pixels. Do NOT generate "
                f"square or any other aspect ratio.")

    @staticmethod
    def _force_aspect(img_bytes, size):
        """Crop-then-resize to guarantee exact target dimensions."""
        if not size or size == "auto":
            return img_bytes
        try:
            tw, th = size.split("x")
            tw, th = int(tw), int(th)
        except Exception:
            return img_bytes
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            sw, sh = im.size
            if (sw, sh) == (tw, th):
                return img_bytes
            target_ratio = tw / th
            src_ratio = sw / sh
            if abs(src_ratio - target_ratio) > 0.01:
                if src_ratio > target_ratio:
                    new_w = int(sh * target_ratio)
                    left = (sw - new_w) // 2
                    im = im.crop((left, 0, left + new_w, sh))
                else:
                    new_h = int(sw / target_ratio)
                    top = (sh - new_h) // 2
                    im = im.crop((0, top, sw, top + new_h))
            im = im.resize((tw, th), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception:
            return img_bytes

    @staticmethod
    def _compress_ref(img_bytes, max_side=768):
        """Shrink a reference image to reduce payload size for faster uploads."""
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            w, h = im.size
            if max(w, h) <= max_side:
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=80)
                return buf.getvalue(), "image/jpeg"
            scale = max_side / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            return buf.getvalue(), "image/jpeg"
        except Exception:
            return img_bytes, "image/png"

    def _generate_once(self, prompt, size=None, quality=None):
        self._require_key()
        size = size or config.DEFAULT_SIZE
        sized_prompt = prompt + self._size_instruction(size)
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": sized_prompt}],
            "reasoning": {"effort": "low"},
        }
        _log(f"openrouter generate model={self.model} size={size} prompt_len={len(sized_prompt)}")
        t0 = time.time()
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"could not reach OpenRouter at {url} — {e}")
        dt = time.time() - t0
        _log(f"openrouter generate -> HTTP {resp.status_code} in {dt:.1f}s")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter image gen failed [HTTP {resp.status_code}]: {resp.text[:600]}"
            )
        try:
            raw = self._extract_image(resp.json())
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(
                f"OpenRouter returned non-JSON response [HTTP {resp.status_code}]: "
                f"{resp.text[:300]}"
            ) from e
        return self._force_aspect(raw, size)

    def edit(self, prompt, images, size=None, quality=None, mask=None,
             retry=True, index=0):
        if not retry:
            return self._edit_once(prompt, images, size, quality)
        return image_queue.run_with_retry(
            lambda: self._edit_once(prompt, images, size, quality),
            index=index, model=self.model, label="openrouter-edit")

    def _edit_once(self, prompt, images, size=None, quality=None):
        """Send reference images as inline vision content alongside the prompt."""
        self._require_key()
        if not images:
            return self._generate_once(prompt, size, quality)
        size = size or config.DEFAULT_SIZE
        sized_prompt = prompt + self._size_instruction(size)
        content_parts = []
        for img_bytes in images[:max(1, int(getattr(config, "MAX_REF_IMAGES", 10)))]:
            compressed, mime = self._compress_ref(img_bytes)
            b64 = base64.b64encode(compressed).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        content_parts.append({"type": "text", "text": sized_prompt})
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content_parts}],
            "reasoning": {"effort": "low"},
        }
        _log(f"openrouter edit model={self.model} refs={len(images)} prompt_len={len(prompt)}")
        t0 = time.time()
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"could not reach OpenRouter at {url} — {e}")
        dt = time.time() - t0
        _log(f"openrouter edit -> HTTP {resp.status_code} in {dt:.1f}s")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter image edit failed [HTTP {resp.status_code}]: {resp.text[:600]}"
            )
        try:
            raw = self._extract_image(resp.json())
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(
                f"OpenRouter returned non-JSON edit response [HTTP {resp.status_code}]: "
                f"{resp.text[:300]}"
            ) from e
        return self._force_aspect(raw, size)
