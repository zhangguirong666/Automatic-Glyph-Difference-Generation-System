import base64
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import torch
from diffusers import (
    ControlNetModel,
    EulerDiscreteScheduler,
    StableDiffusionControlNetImg2ImgPipeline,
    StableDiffusionImg2ImgPipeline,
    StableDiffusionPipeline,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageEnhance, ImageOps


MODEL_DIR = Path(os.environ.get("SD_MODEL_DIR", "/root/autodl-tmp/models/sd15"))
LORA_DIR = Path(os.environ.get("SD_LORA_DIR", "/root/autodl-tmp/models/lora"))
CONTROLNET_CANNY_DIR = Path(os.environ.get("SD_CONTROLNET_CANNY_DIR", "/root/autodl-tmp/models/controlnet/canny"))
OUTPUT_DIR = Path(os.environ.get("SD_OUTPUT_DIR", "/root/autodl-tmp/font_morph_web/lora_workspace/sd_outputs"))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

app = FastAPI(title="Font Morph Local SD API")
_pipe = None
_img2img_pipe = None
_control_pipe = None
_lock = threading.Lock()
_loaded_loras = []


def _ensure_dirs():
    LORA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_pipe():
    global _pipe
    if _pipe is not None:
        return _pipe

    dtype = torch.float16 if DEVICE == "cuda" else torch.float32
    pipe = StableDiffusionPipeline.from_pretrained(
        str(MODEL_DIR),
        torch_dtype=dtype,
        safety_checker=None,
        local_files_only=True,
    )
    pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to(DEVICE)
    if hasattr(pipe, "enable_attention_slicing"):
        pipe.enable_attention_slicing()
    _pipe = pipe
    return _pipe


def _load_img2img_pipe():
    global _img2img_pipe
    if _img2img_pipe is not None:
        return _img2img_pipe

    dtype = torch.float16 if DEVICE == "cuda" else torch.float32
    pipe = StableDiffusionImg2ImgPipeline.from_pretrained(
        str(MODEL_DIR),
        torch_dtype=dtype,
        safety_checker=None,
        local_files_only=True,
    )
    pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to(DEVICE)
    if hasattr(pipe, "enable_attention_slicing"):
        pipe.enable_attention_slicing()
    _img2img_pipe = pipe
    return _img2img_pipe


def _load_control_pipe():
    global _control_pipe
    if _control_pipe is not None:
        return _control_pipe
    if not (CONTROLNET_CANNY_DIR / "config.json").exists():
        raise RuntimeError(f"ControlNet Canny model is not installed: {CONTROLNET_CANNY_DIR}")

    dtype = torch.float16 if DEVICE == "cuda" else torch.float32
    controlnet = ControlNetModel.from_pretrained(
        str(CONTROLNET_CANNY_DIR),
        torch_dtype=dtype,
        variant="fp16",
        use_safetensors=True,
        local_files_only=True,
    )
    pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
        str(MODEL_DIR),
        controlnet=controlnet,
        torch_dtype=dtype,
        safety_checker=None,
        local_files_only=True,
    )
    pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to(DEVICE)
    if hasattr(pipe, "enable_attention_slicing"):
        pipe.enable_attention_slicing()
    _control_pipe = pipe
    return _control_pipe


def _list_loras():
    _ensure_dirs()
    items = []
    for path in sorted(LORA_DIR.glob("*")):
        if path.suffix.lower() not in {".safetensors", ".pt", ".bin"}:
            continue
        meta = {}
        meta_path = path.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        items.append({
            "name": path.stem,
            "alias": meta.get("label") or path.stem,
            "path": str(path),
            "metadata": meta,
        })
    return items


def _parse_lora_tags(prompt):
    tags = []
    clean = prompt or ""
    pattern = re.compile(r"<lora:([^:>]+)(?::([0-9.\\-]+))?>")
    for match in pattern.finditer(clean):
        name = match.group(1).strip()
        try:
            weight = float(match.group(2) or 0.8)
        except Exception:
            weight = 0.8
        tags.append((name, weight))
    clean = pattern.sub("", clean).strip()
    return clean, tags


def _decode_image(image_data):
    if not image_data:
        raise ValueError("init image is required")
    if isinstance(image_data, list):
        image_data = image_data[0]
    if isinstance(image_data, str) and "," in image_data[:80]:
        image_data = image_data.split(",", 1)[1]
    raw = base64.b64decode(image_data)
    return ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")


def _fit_image(image, width, height):
    image = image.convert("RGB")
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _canny_image(image, low=80, high=180):
    arr = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, int(low), int(high))
    edges = np.stack([edges, edges, edges], axis=2)
    return Image.fromarray(edges)


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _infer_material_style(style_hint="", prompt=""):
    value = f"{style_hint or ''} {prompt or ''}".lower()
    if any(token in value for token in ["lava", "magma", "molten"]):
        return "lava"
    if any(token in value for token in ["marble", "veined stone"]):
        return "marble"
    if any(token in value for token in ["pearl", "mother of pearl", "iridescent"]):
        return "pearl"
    if any(token in value for token in ["candy", "jelly", "gummy"]):
        return "candy"
    if any(token in value for token in ["ice", "frozen", "crystal", "frost", "冰"]):
        return "ice"
    if any(token in value for token in ["water", "liquid", "transparent", "水"]):
        return "water"
    if any(token in value for token in ["fire", "flame", "ember", "burning", "火"]):
        return "fire"
    if any(token in value for token in ["leather", "brown", "皮革"]):
        return "leather"
    if any(token in value for token in ["milk", "cream", "牛奶"]):
        return "milk"
    if any(token in value for token in ["jade", "gemstone", "玉石", "玉"]):
        return "jade"
    if any(token in value for token in ["gold", "golden", "foil", "鎏金", "金箔"]):
        return "gold"
    if any(token in value for token in ["ceramic", "porcelain", "celadon", "青瓷", "瓷"]):
        return "ceramic"
    if any(token in value for token in ["glass", "transparent glass", "玻璃"]):
        return "glass"
    if any(token in value for token in ["neon", "cyber", "霓虹"]):
        return "neon"
    if any(token in value for token in ["silk", "satin", "fabric", "丝绸", "织物"]):
        return "silk"
    if any(token in value for token in ["wood", "wooden", "walnut", "木纹", "木"]):
        return "wood"
    if any(token in value for token in ["metal", "chrome", "mecha", "机械", "金属"]):
        return "metal"
    if any(token in value for token in ["ink", "line", "draw", "手绘"]):
        return "ink"
    if "logo" in value:
        return "logo"
    return "material"


def _glyph_mask(image, dilate=1, blur=1):
    gray = np.array(image.convert("L"))
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    coverage = float((mask > 0).mean())
    if coverage > 0.65:
        mask = 255 - mask

    if int(dilate) > 0:
        k = max(1, min(int(dilate), 8))
        kernel = np.ones((k, k), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.dilate(mask, kernel, iterations=1)

    if float(blur) > 0:
        radius = max(1, int(round(float(blur) * 2))) * 2 + 1
        mask = cv2.GaussianBlur(mask, (radius, radius), float(blur))

    return Image.fromarray(mask).convert("L")


def _lock_glyph_shape(init_image, generated_image, dilate=1, blur=1):
    mask = _glyph_mask(init_image, dilate=dilate, blur=blur)
    generated = generated_image.convert("RGB").resize(init_image.size, Image.Resampling.LANCZOS)
    background = Image.new("RGB", init_image.size, (255, 255, 255))
    locked = Image.composite(generated, background, mask)
    return locked, mask


def _shift_array(arr, dx, dy):
    out = np.zeros_like(arr)
    h, w = arr.shape[:2]
    src_x0 = max(0, -dx)
    src_x1 = min(w, w - dx)
    dst_x0 = max(0, dx)
    dst_x1 = min(w, w + dx)
    src_y0 = max(0, -dy)
    src_y1 = min(h, h - dy)
    dst_y0 = max(0, dy)
    dst_y1 = min(h, h + dy)
    if src_x0 < src_x1 and src_y0 < src_y1:
        out[dst_y0:dst_y1, dst_x0:dst_x1] = arr[src_y0:src_y1, src_x0:src_x1]
    return out


def _apply_glyph_depth(material, mask, material_intensity=1.6, depth_strength=1.15, style_hint="material"):
    style = _infer_material_style(style_hint)
    img = np.array(material.convert("RGB")).astype(np.float32) / 255.0
    alpha = np.array(mask.convert("L")).astype(np.float32) / 255.0
    binary = (alpha > 0.08).astype(np.uint8)
    if not int(binary.sum()):
        return material.convert("RGB")

    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
    positive = dist[dist > 0]
    scale = float(np.percentile(positive, 96)) if positive.size else 1.0
    height = np.clip(dist / max(scale, 1.0), 0.0, 1.0)
    height = cv2.GaussianBlur(height, (0, 0), 1.6)

    gx = cv2.Sobel(height, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(height, cv2.CV_32F, 0, 1, ksize=3)
    nx = -gx * 3.2 * float(depth_strength)
    ny = -gy * 3.2 * float(depth_strength)
    nz = np.ones_like(height)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-6
    nx, ny, nz = nx / norm, ny / norm, nz / norm

    light = np.array([-0.45, -0.62, 0.86], dtype=np.float32)
    light = light / np.linalg.norm(light)
    dot = nx * light[0] + ny * light[1] + nz * light[2]
    inside = alpha > 0.12
    base = float(dot[inside].mean()) if np.any(inside) else 0.75
    shade_amount = 0.95 * float(depth_strength)
    if style in {"ice", "water", "milk", "glass", "pearl"}:
        shade_amount *= 0.72
    elif style in {"metal", "fire", "gold", "lava"}:
        shade_amount *= 1.22
    shade = 1.0 + (dot - base) * shade_amount
    shade = np.clip(shade, 0.58 if style in {"ice", "water", "milk", "glass", "pearl"} else 0.48, 1.86)

    edge_highlight = np.clip(alpha - _shift_array(alpha, 3, 3), 0.0, 1.0)
    edge_shadow = np.clip(alpha - _shift_array(alpha, -4, -4), 0.0, 1.0)
    inner_edge = np.clip(1.0 - height * 2.8, 0.0, 1.0) * alpha

    contrast_boost = 0.16 * float(material_intensity)
    if style == "leather":
        contrast_boost *= 0.82
    elif style in {"metal", "fire", "gold", "lava", "neon"}:
        contrast_boost *= 1.28
    img = np.clip((img - 0.5) * (1.0 + contrast_boost) + 0.5, 0.0, 1.0)
    img = img * shade[..., None]
    highlight = 0.30 * float(depth_strength)
    shadow = 0.24 * float(depth_strength)
    inner = 0.08 * float(depth_strength)
    if style in {"ice", "water", "glass", "pearl"}:
        highlight *= 1.35
        shadow *= 0.62
        inner *= 1.25
    elif style == "leather":
        highlight *= 0.68
        shadow *= 1.15
    elif style == "milk":
        highlight *= 1.18
        shadow *= 1.05
    elif style in {"metal", "gold", "lava", "fire"}:
        highlight *= 1.18
        shadow *= 1.18
        inner *= 1.18
    img = img + edge_highlight[..., None] * highlight
    img = img * (1.0 - edge_shadow[..., None] * shadow)
    img = img + inner_edge[..., None] * inner
    img = np.clip(img, 0.0, 1.0)
    return Image.fromarray((img * 255).astype(np.uint8), "RGB")


def _norm01(arr):
    arr = np.asarray(arr, dtype=np.float32)
    mn = float(arr.min())
    mx = float(arr.max())
    if mx - mn < 1e-6:
        return np.zeros_like(arr, dtype=np.float32)
    return (arr - mn) / (mx - mn)


def _smooth_noise(h, w, seed=1, sigma=18.0):
    rng = np.random.default_rng(int(seed))
    noise = rng.random((h, w), dtype=np.float32)
    return _norm01(cv2.GaussianBlur(noise, (0, 0), float(sigma)))


def _palette_from_luminance(lum, stops):
    lum = np.clip(lum.astype(np.float32), 0.0, 1.0)
    out = np.zeros((lum.shape[0], lum.shape[1], 3), dtype=np.float32)
    for idx in range(len(stops) - 1):
        p0, c0 = stops[idx]
        p1, c1 = stops[idx + 1]
        band = (lum >= p0) & (lum <= p1)
        if not np.any(band):
            continue
        t = np.clip((lum[band] - p0) / max(p1 - p0, 1e-6), 0.0, 1.0)
        c0 = np.array(c0, dtype=np.float32)
        c1 = np.array(c1, dtype=np.float32)
        out[band] = c0 * (1.0 - t[:, None]) + c1 * t[:, None]
    out[lum < stops[0][0]] = np.array(stops[0][1], dtype=np.float32)
    out[lum > stops[-1][0]] = np.array(stops[-1][1], dtype=np.float32)
    return out


def _random_line_mask(h, w, alpha, seed=1, count=24, thickness=2, mostly_vertical=False):
    rng = np.random.default_rng(int(seed))
    mask = np.zeros((h, w), dtype=np.float32)
    ys, xs = np.where(alpha > 0.12)
    if xs.size < 8:
        return mask
    x_min, x_max = int(xs.min()), int(xs.max())
    y_min, y_max = int(ys.min()), int(ys.max())
    for _ in range(int(count)):
        if mostly_vertical:
            x0 = int(rng.integers(x_min, x_max + 1))
            y0 = int(rng.integers(y_min, y_max + 1))
            x1 = int(np.clip(x0 + rng.normal(0, max(8, w * 0.08)), 0, w - 1))
            y1 = int(np.clip(y0 + rng.normal(0, max(16, h * 0.24)), 0, h - 1))
        else:
            x0 = int(rng.integers(x_min, x_max + 1))
            y0 = int(rng.integers(y_min, y_max + 1))
            angle = float(rng.uniform(-1.4, 1.4))
            length = float(rng.uniform(max(18, min(w, h) * 0.06), max(32, min(w, h) * 0.22)))
            x1 = int(np.clip(x0 + np.cos(angle) * length, 0, w - 1))
            y1 = int(np.clip(y0 + np.sin(angle) * length, 0, h - 1))
        cv2.line(mask, (x0, y0), (x1, y1), 1.0, int(thickness), cv2.LINE_AA)
    mask *= (alpha > 0.08).astype(np.float32)
    return np.clip(mask, 0.0, 1.0)


def _procedural_material_texture(size, mask, style_hint="material", material_intensity=1.8):
    style = _infer_material_style(style_hint)
    w, h = size
    alpha = np.array(mask.convert("L").resize((w, h))).astype(np.float32) / 255.0
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    x = xx / max(w - 1, 1)
    y = yy / max(h - 1, 1)
    center_x = float(x[alpha > 0.1].mean()) if np.any(alpha > 0.1) else 0.5
    center_y = float(y[alpha > 0.1].mean()) if np.any(alpha > 0.1) else 0.5
    dist = np.sqrt((x - center_x) ** 2 + (y - center_y) ** 2)
    fine = _smooth_noise(h, w, 17, 2.2)
    mid = _smooth_noise(h, w, 31, 10.0)
    broad = _smooth_noise(h, w, 57, 30.0)
    intensity = max(0.2, min(float(material_intensity), 3.0))

    if style == "milk":
        swirls = 0.5 + 0.5 * np.sin(20 * x + 12 * y + broad * 9.0)
        ribbons = 0.5 + 0.5 * np.sin(30 * (x - y) + mid * 5.0)
        bubbles = np.clip(_smooth_noise(h, w, 92, 3.2) - 0.68, 0, 1) * 3.8
        lum = np.clip(0.42 + 0.20 * broad + 0.23 * swirls + 0.16 * ribbons + 0.14 * bubbles, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (116, 108, 84)), (0.34, (196, 184, 142)), (0.72, (250, 240, 197)), (1.0, (255, 255, 252))
        ])
        tex += bubbles[..., None] * np.array([44, 42, 28], dtype=np.float32)
    elif style == "ice":
        facets = 0.5 + 0.5 * np.sin(24 * (x + y) + mid * 5.0) * np.sin(18 * (x - y) + broad * 4.0)
        cracks = _random_line_mask(h, w, alpha, seed=111, count=38, thickness=1)
        frost = np.clip(fine * 1.4 + cracks * 1.8, 0, 1)
        lum = np.clip(0.25 + 0.28 * facets + 0.26 * broad + 0.32 * frost, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (3, 46, 112)), (0.34, (18, 136, 218)), (0.70, (120, 228, 255)), (1.0, (255, 255, 255))
        ])
        tex += cracks[..., None] * np.array([120, 220, 255], dtype=np.float32)
    elif style == "water":
        ripples = 0.5 + 0.5 * np.sin(40 * y + 16 * np.sin(9 * x) + broad * 7.0)
        caustic = np.clip(0.5 + 0.5 * np.sin(36 * (x + y) + mid * 6.0), 0, 1)
        lum = np.clip(0.22 + 0.28 * ripples + 0.26 * caustic + 0.18 * broad, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (0, 38, 118)), (0.42, (5, 138, 224)), (0.76, (70, 222, 255)), (1.0, (235, 255, 255))
        ])
    elif style == "fire":
        flame = 1.0 - y + 0.18 * np.sin(18 * x + broad * 7.0) + 0.18 * mid
        tongues = np.clip(0.5 + 0.5 * np.sin(28 * x + 18 * y + mid * 9.0), 0, 1)
        lum = np.clip(0.12 + 0.70 * flame + 0.20 * tongues, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (45, 0, 0)), (0.35, (170, 22, 0)), (0.68, (255, 94, 0)), (0.88, (255, 196, 45)), (1.0, (255, 255, 178))
        ])
    elif style == "lava":
        cracks = _random_line_mask(h, w, alpha, seed=131, count=54, thickness=2)
        molten = np.clip((1.0 - y) * 0.55 + 0.25 * mid + 0.30 * cracks, 0, 1)
        lum = np.clip(0.08 + 0.78 * molten + 0.10 * fine, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (8, 7, 7)), (0.30, (70, 20, 8)), (0.56, (210, 45, 0)), (0.78, (255, 132, 10)), (1.0, (255, 241, 140))
        ])
        tex += cracks[..., None] * np.array([170, 42, 0], dtype=np.float32)
    elif style == "leather":
        grain = 0.48 * fine + 0.34 * mid + 0.18 * np.sin(70 * x + broad * 6.0)
        pores = (fine > 0.78).astype(np.float32) * 0.24
        creases = _random_line_mask(h, w, alpha, seed=414, count=30, thickness=1, mostly_vertical=False)
        lum = np.clip(0.26 + 0.50 * grain - pores, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (32, 18, 8)), (0.44, (96, 52, 22)), (0.76, (166, 102, 46)), (1.0, (230, 174, 98))
        ])
        tex *= (1.0 - creases[..., None] * 0.28)
    elif style == "metal":
        brushed = 0.5 + 0.5 * np.sin(95 * y + mid * 9.0)
        bands = 0.5 + 0.5 * np.sin(18 * (x + y) + broad * 5.0)
        scratches = _random_line_mask(h, w, alpha, seed=515, count=55, thickness=1, mostly_vertical=False)
        streaks = cv2.GaussianBlur(fine, (0, 0), 0.7)
        lum = np.clip(0.12 + 0.46 * brushed + 0.24 * streaks + 0.20 * bands + 0.14 * broad, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (12, 16, 24)), (0.36, (85, 96, 112)), (0.70, (210, 222, 235)), (1.0, (255, 255, 255))
        ])
        tex = tex * (1.0 - scratches[..., None] * 0.36) + scratches[..., None] * np.array([250, 255, 255], dtype=np.float32) * 0.34
    elif style == "jade":
        veins = _random_line_mask(h, w, alpha, seed=222, count=30, thickness=2)
        clouds = 0.55 * broad + 0.30 * mid + 0.15 * fine
        lum = np.clip(0.22 + 0.58 * clouds + 0.18 * veins, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (2, 54, 39)), (0.43, (28, 138, 88)), (0.78, (142, 224, 166)), (1.0, (238, 255, 226))
        ])
        tex += veins[..., None] * np.array([70, 120, 78], dtype=np.float32)
    elif style == "gold":
        hammer = 0.42 * fine + 0.40 * mid + 0.18 * np.sin(44 * (x + y))
        bands = 0.5 + 0.5 * np.sin(32 * y + broad * 4.0)
        lum = np.clip(0.22 + 0.52 * hammer + 0.28 * bands, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (75, 38, 2)), (0.40, (188, 102, 8)), (0.72, (255, 185, 34)), (1.0, (255, 246, 150))
        ])
    elif style == "ceramic":
        crackle = _random_line_mask(h, w, alpha, seed=333, count=46, thickness=1)
        glaze = 0.55 * broad + 0.25 * mid + 0.20 * (1.0 - dist)
        lum = np.clip(0.30 + 0.48 * glaze + 0.16 * fine, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (42, 92, 82)), (0.45, (96, 170, 154)), (0.76, (190, 232, 214)), (1.0, (252, 255, 246))
        ])
        tex *= (1.0 - crackle[..., None] * 0.32)
    elif style == "glass":
        refraction = 0.50 * broad + 0.28 * np.sin(26 * (x + y)) + 0.22 * (1.0 - dist)
        lum = np.clip(0.28 + 0.52 * refraction + 0.14 * fine, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (8, 45, 88)), (0.44, (70, 170, 228)), (0.74, (190, 246, 255)), (1.0, (255, 255, 255))
        ])
    elif style == "neon":
        glow = np.clip(1.0 - dist * 2.0, 0, 1)
        waves = 0.5 + 0.5 * np.sin(20 * x + 28 * y + broad * 6.0)
        lum = np.clip(0.12 + 0.42 * glow + 0.40 * waves, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (10, 2, 34)), (0.36, (42, 36, 170)), (0.68, (20, 225, 255)), (1.0, (255, 48, 216))
        ])
    elif style == "silk":
        weave = 0.5 + 0.5 * np.sin(55 * x + 8 * np.sin(18 * y))
        satin = 0.5 + 0.5 * np.sin(18 * (x + y) + broad * 6.0)
        lum = np.clip(0.18 + 0.35 * weave + 0.36 * satin + 0.10 * mid, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (76, 4, 20)), (0.42, (170, 22, 54)), (0.75, (238, 86, 116)), (1.0, (255, 218, 198))
        ])
    elif style == "wood":
        rings = 0.5 + 0.5 * np.sin(44 * dist + 14 * broad)
        grain = 0.45 * rings + 0.38 * mid + 0.17 * fine
        lum = np.clip(0.20 + 0.62 * grain, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (42, 20, 6)), (0.42, (122, 66, 24)), (0.76, (198, 132, 58)), (1.0, (248, 196, 104))
        ])
    elif style == "marble":
        veins = _random_line_mask(h, w, alpha, seed=616, count=44, thickness=2)
        cloudy = 0.50 * broad + 0.32 * mid + 0.18 * fine
        lum = np.clip(0.44 + 0.42 * cloudy - 0.28 * veins, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (48, 52, 58)), (0.36, (158, 162, 168)), (0.72, (232, 232, 226)), (1.0, (255, 255, 250))
        ])
        tex *= (1.0 - veins[..., None] * 0.24)
    elif style == "pearl":
        iris = np.stack([
            0.5 + 0.5 * np.sin(16 * x + broad * 5.0),
            0.5 + 0.5 * np.sin(18 * y + mid * 5.0),
            0.5 + 0.5 * np.sin(15 * (x + y) + fine * 4.0),
        ], axis=-1)
        base = np.array([236, 226, 208], dtype=np.float32)
        tint = np.array([255, 160, 232], dtype=np.float32) * iris[..., 0:1] * 0.20
        tint += np.array([120, 220, 255], dtype=np.float32) * iris[..., 1:2] * 0.18
        tint += np.array([255, 246, 170], dtype=np.float32) * iris[..., 2:3] * 0.18
        tex = base + tint + (broad[..., None] - 0.5) * 32
    elif style == "candy":
        stripes = 0.5 + 0.5 * np.sin(34 * (x + y) + broad * 8.0)
        gloss = 0.5 + 0.5 * np.sin(18 * x - 12 * y + mid * 5.0)
        lum = np.clip(0.28 + 0.46 * stripes + 0.28 * gloss, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (72, 10, 92)), (0.30, (230, 38, 132)), (0.58, (255, 150, 70)), (0.78, (60, 220, 255)), (1.0, (255, 255, 245))
        ])
    else:
        lum = np.clip(0.20 + 0.42 * broad + 0.28 * mid + 0.10 * fine, 0, 1)
        tex = _palette_from_luminance(lum, [
            (0.0, (28, 32, 44)), (0.48, (92, 104, 128)), (1.0, (235, 240, 248))
        ])

    # Strong visible material, but still preserve the uploaded glyph contour.
    tex = (tex - 128.0) * (1.05 + 0.09 * intensity) + 128.0
    tex = tex * (0.90 + 0.18 * np.clip(intensity / 2.0, 0.0, 1.4))
    return Image.fromarray(np.clip(tex, 0, 255).astype(np.uint8), "RGB")


def _compose_glyph_material(init_image, generated_image, dilate=1, blur=1, material_intensity=1.6, depth_strength=1.15, shadow_strength=0.42, style_hint="material"):
    style = _infer_material_style(style_hint)
    mask = _glyph_mask(init_image, dilate=dilate, blur=blur)
    generated = generated_image.convert("RGB").resize(init_image.size, Image.Resampling.LANCZOS)
    procedural = _procedural_material_texture(init_image.size, mask, style_hint=style, material_intensity=material_intensity)
    generated = _style_tint_image(generated, style, style_hint=style)
    # The procedural layer carries the material identity; SD output contributes
    # a little irregular detail without overpowering the glyph mask.
    material = Image.blend(generated, procedural, 0.90)
    color_gain = 1.0 + 0.22 * float(material_intensity)
    contrast_gain = 1.0 + 0.18 * float(material_intensity)
    sharp_gain = 1.0 + 0.10 * float(material_intensity)
    if style == "leather":
        color_gain *= 0.92
        contrast_gain *= 0.96
    elif style in {"ice", "water"}:
        color_gain *= 1.12
        contrast_gain *= 0.92
    elif style == "milk":
        color_gain *= 0.72
        contrast_gain *= 1.02
    elif style in {"metal", "fire", "gold", "neon"}:
        contrast_gain *= 1.12
        sharp_gain *= 1.12
    elif style in {"lava", "marble", "pearl", "candy"}:
        contrast_gain *= 1.12
        sharp_gain *= 1.10
    elif style in {"jade", "ceramic", "glass"}:
        color_gain *= 1.05
        contrast_gain *= 0.96
    material = ImageEnhance.Color(material).enhance(color_gain)
    material = ImageEnhance.Contrast(material).enhance(contrast_gain)
    material = ImageEnhance.Sharpness(material).enhance(sharp_gain)
    material = _apply_glyph_depth(material, mask, material_intensity=material_intensity, depth_strength=depth_strength, style_hint=style)
    material = _apply_style_palette(material, style_hint=style, prompt=style)

    alpha = np.array(mask.convert("L")).astype(np.float32) / 255.0
    background = np.ones((mask.height, mask.width, 3), dtype=np.float32) * 255.0
    if float(shadow_strength) > 0:
        sx = max(1, int(round(7 * float(depth_strength))))
        sy = max(1, int(round(9 * float(depth_strength))))
        shadow = _shift_array(alpha, sx, sy)
        shadow = cv2.GaussianBlur(shadow, (0, 0), 5.0 + 2.0 * float(depth_strength))
        opacity = min(0.65, 0.18 + 0.34 * float(shadow_strength))
        shadow_color = np.array([38.0, 45.0, 55.0], dtype=np.float32)
        if style == "leather":
            shadow_color = np.array([70.0, 38.0, 24.0], dtype=np.float32)
        elif style in {"ice", "water"}:
            shadow_color = np.array([35.0, 115.0, 165.0], dtype=np.float32)
            opacity *= 0.68
        elif style == "fire":
            shadow_color = np.array([96.0, 30.0, 10.0], dtype=np.float32)
        elif style == "gold":
            shadow_color = np.array([112.0, 70.0, 10.0], dtype=np.float32)
        elif style == "lava":
            shadow_color = np.array([86.0, 18.0, 4.0], dtype=np.float32)
        elif style == "jade":
            shadow_color = np.array([20.0, 94.0, 58.0], dtype=np.float32)
        elif style == "marble":
            shadow_color = np.array([76.0, 80.0, 88.0], dtype=np.float32)
        elif style == "pearl":
            shadow_color = np.array([160.0, 142.0, 170.0], dtype=np.float32)
            opacity *= 0.68
        elif style == "candy":
            shadow_color = np.array([190.0, 34.0, 120.0], dtype=np.float32)
        elif style in {"ceramic", "glass"}:
            shadow_color = np.array([44.0, 132.0, 142.0], dtype=np.float32)
            opacity *= 0.62
        elif style == "neon":
            shadow_color = np.array([74.0, 20.0, 160.0], dtype=np.float32)
            opacity *= 0.85
        elif style == "milk":
            shadow_color = np.array([118.0, 104.0, 74.0], dtype=np.float32)
            opacity *= 1.05
        background = background * (1.0 - shadow[..., None] * opacity) + shadow_color * (shadow[..., None] * opacity)

    if style in {"fire", "neon", "ice", "glass", "lava", "pearl", "candy"}:
        glow = cv2.GaussianBlur(alpha, (0, 0), 10.0 + 5.0 * float(depth_strength))
        glow = np.clip(glow - alpha * 0.55, 0.0, 1.0)
        glow_color = {
            "fire": np.array([255.0, 78.0, 8.0], dtype=np.float32),
            "neon": np.array([80.0, 210.0, 255.0], dtype=np.float32),
            "ice": np.array([84.0, 220.0, 255.0], dtype=np.float32),
            "glass": np.array([130.0, 230.0, 255.0], dtype=np.float32),
            "lava": np.array([255.0, 74.0, 6.0], dtype=np.float32),
            "pearl": np.array([250.0, 210.0, 255.0], dtype=np.float32),
            "candy": np.array([255.0, 55.0, 170.0], dtype=np.float32),
        }[style]
        glow_opacity = 0.40 if style in {"fire", "neon", "lava", "candy"} else 0.24
        background = background * (1.0 - glow[..., None] * glow_opacity) + glow_color * (glow[..., None] * glow_opacity)

    mat = np.array(material).astype(np.float32)
    out = mat * alpha[..., None] + background * (1.0 - alpha[..., None])
    inner_rim = np.clip(alpha - cv2.erode(alpha, np.ones((5, 5), np.uint8), iterations=1), 0.0, 1.0)
    rim_color = {
        "milk": np.array([255.0, 250.0, 222.0], dtype=np.float32),
        "ice": np.array([220.0, 255.0, 255.0], dtype=np.float32),
        "water": np.array([145.0, 240.0, 255.0], dtype=np.float32),
        "fire": np.array([255.0, 232.0, 96.0], dtype=np.float32),
        "lava": np.array([255.0, 154.0, 24.0], dtype=np.float32),
        "leather": np.array([238.0, 168.0, 88.0], dtype=np.float32),
        "metal": np.array([245.0, 252.0, 255.0], dtype=np.float32),
        "gold": np.array([255.0, 236.0, 118.0], dtype=np.float32),
        "jade": np.array([190.0, 255.0, 206.0], dtype=np.float32),
        "ceramic": np.array([226.0, 255.0, 238.0], dtype=np.float32),
        "glass": np.array([235.0, 255.0, 255.0], dtype=np.float32),
        "neon": np.array([255.0, 80.0, 226.0], dtype=np.float32),
        "silk": np.array([255.0, 154.0, 185.0], dtype=np.float32),
        "wood": np.array([255.0, 178.0, 86.0], dtype=np.float32),
        "marble": np.array([255.0, 255.0, 250.0], dtype=np.float32),
        "pearl": np.array([255.0, 232.0, 255.0], dtype=np.float32),
        "candy": np.array([255.0, 245.0, 255.0], dtype=np.float32),
    }.get(style, np.array([235.0, 240.0, 248.0], dtype=np.float32))
    rim_opacity = 0.28 if style not in {"metal", "gold", "lava", "neon"} else 0.38
    out = out * (1.0 - inner_rim[..., None] * rim_opacity) + rim_color * (inner_rim[..., None] * rim_opacity)
    if style in {"milk", "pearl", "marble"}:
        low_rim = np.clip(alpha - _shift_array(alpha, -4, -4), 0.0, 1.0)
        low_color = {
            "milk": np.array([128.0, 116.0, 84.0], dtype=np.float32),
            "pearl": np.array([166.0, 144.0, 172.0], dtype=np.float32),
            "marble": np.array([78.0, 84.0, 92.0], dtype=np.float32),
        }[style]
        out = out * (1.0 - low_rim[..., None] * 0.20) + low_color * (low_rim[..., None] * 0.20)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB"), mask


def _material_fill_prompt(prompt, style_hint="material"):
    style = _infer_material_style(style_hint, prompt)
    text = re.sub(
        r"\b(preserve exact source character shape|keep original stroke silhouette|single readable glyph|single readable chinese character)\b",
        "",
        prompt or "",
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*,\s*,+", ",", text).strip(" ,")
    parts = [
        "close-up material texture surface",
        "strong visible color",
        "large visible material grains",
        "strong specular highlights",
        "rich tactile detail",
    ]
    if style == "ice":
        parts = [
            "transparent cyan blue ice crystal material",
            "frozen glass refraction",
            "bright cold highlights",
            "frosted crystalline texture",
            "cool blue color only",
        ]
    elif style == "water":
        parts = [
            "transparent blue water material",
            "glossy liquid refraction",
            "wet reflective highlights",
            "flowing ripple texture",
            "cool blue color only",
        ]
    elif style == "leather":
        parts = [
            "warm brown leather material",
            "fine leather grain texture",
            "embossed matte surface",
            "stitched tactile detail",
            "brown and dark tan color only",
        ]
    elif style == "metal":
        parts = [
            "shiny chrome metal material",
            "brushed steel panels",
            "strong mirror highlights",
            "mechanical red accent lights",
            "cold silver metal surface",
        ]
    elif style == "fire":
        parts = [
            "orange flame material",
            "glowing ember texture",
            "hot luminous highlights",
            "burning red and yellow surface",
            "fiery sparks",
        ]
    elif style == "milk":
        parts = [
            "creamy white milk material",
            "glossy smooth liquid surface",
            "soft white highlights",
            "thick cream texture",
            "white color only",
        ]
    elif style == "jade":
        parts = [
            "translucent green jade material",
            "polished gemstone surface",
            "cloudy mineral texture",
            "soft inner green glow",
            "carved stone relief",
        ]
    elif style == "gold":
        parts = [
            "shiny gold foil material",
            "warm metallic golden surface",
            "hammered foil texture",
            "strong luxury highlights",
            "raised gilded relief",
        ]
    elif style == "ceramic":
        parts = [
            "pale celadon ceramic glaze material",
            "porcelain crackle texture",
            "glossy smooth glazed surface",
            "soft green blue highlights",
            "raised porcelain relief",
        ]
    elif style == "glass":
        parts = [
            "transparent clear glass material",
            "sharp refraction",
            "bright glossy highlights",
            "subtle blue glass edges",
            "raised transparent relief",
        ]
    elif style == "neon":
        parts = [
            "neon tube material",
            "glowing cyan magenta light",
            "luminous edge halo",
            "dark glossy core",
            "electric raised glyph",
        ]
    elif style == "silk":
        parts = [
            "red silk fabric material",
            "satin sheen",
            "soft woven textile texture",
            "flowing fabric highlights",
            "raised embroidered relief",
        ]
    elif style == "wood":
        parts = [
            "polished wood grain material",
            "carved walnut surface",
            "warm brown annual rings",
            "tactile wooden relief",
            "raised carved glyph",
        ]
    elif style == "lava":
        parts = [
            "molten lava material",
            "black volcanic crust",
            "bright orange glowing cracks",
            "hot magma texture",
            "dramatic fiery relief",
        ]
    elif style == "marble":
        parts = [
            "white marble stone material",
            "gray veined stone texture",
            "polished carved surface",
            "cold sculptural highlights",
            "raised marble relief",
        ]
    elif style == "pearl":
        parts = [
            "mother of pearl material",
            "iridescent nacre surface",
            "soft rainbow sheen",
            "creamy pearlescent highlights",
            "raised pearl relief",
        ]
    elif style == "candy":
        parts = [
            "glossy candy glass material",
            "bright colorful translucent stripes",
            "sticky sugar shine",
            "thick jelly surface",
            "raised candy relief",
        ]
    else:
        parts.append("dramatic 3D embossed relief")
        parts.append("deep shadows")
    if text:
        parts.append(text)
    parts.extend([
        "texture to fill thick calligraphy strokes",
        "raised bevel edges",
        "not black ink",
        "not monochrome",
    ])
    return ", ".join(parts)


def _material_negative_prompt(negative, prompt="", style_hint="material"):
    style = _infer_material_style(style_hint, prompt)
    additions = [
        "flat black ink",
        "plain black silhouette",
        "monochrome",
        "white texture",
        "gray texture",
        "empty strokes",
    ]
    low_prompt = (prompt or "").lower()
    if style == "ice" or any(token in low_prompt for token in ["ice", "frozen", "crystal", "frost"]):
        additions.extend(["orange", "yellow", "brown", "fire", "leather", "wood"])
    elif style == "water" or any(token in low_prompt for token in ["water", "liquid", "transparent"]):
        additions.extend(["orange", "brown", "fire", "leather"])
    elif style == "fire" or any(token in low_prompt for token in ["fire", "flame", "ember", "burning"]):
        additions.extend(["blue ice", "water", "cold"])
    elif style == "leather":
        additions.extend(["blue ice", "chrome metal", "red glowing metal", "water"])
    elif style == "metal":
        additions.extend(["brown leather", "blue ice", "water", "milk"])
    elif style == "jade":
        additions.extend(["fire", "chrome metal", "brown leather", "flat black"])
    elif style == "gold":
        additions.extend(["silver chrome", "blue ice", "milk white", "flat black"])
    elif style == "ceramic":
        additions.extend(["fire", "brown leather", "chrome metal", "rough wood"])
    elif style == "glass":
        additions.extend(["opaque black", "brown leather", "wood", "muddy texture"])
    elif style == "neon":
        additions.extend(["brown leather", "wood", "plain white", "dull gray"])
    elif style == "silk":
        additions.extend(["chrome metal", "ice crystal", "wood grain", "rough stone"])
    elif style == "wood":
        additions.extend(["chrome metal", "blue ice", "water", "plastic"])
    elif style == "lava":
        additions.extend(["blue ice", "water", "milk white", "cold glass"])
    elif style == "marble":
        additions.extend(["fire", "brown leather", "wood grain", "neon"])
    elif style == "pearl":
        additions.extend(["flat gray", "black ink", "rough stone", "dirty surface"])
    elif style == "candy":
        additions.extend(["flat black", "brown leather", "dull gray", "muddy texture"])
    text = negative or ""
    lower = text.lower()
    missing = [item for item in additions if item not in lower]
    if missing:
        text = (text + ", " if text else "") + ", ".join(missing)
    return text


def _enhance_material_image(image):
    image = image.convert("RGB")
    image = ImageEnhance.Color(image).enhance(1.45)
    image = ImageEnhance.Contrast(image).enhance(1.18)
    image = ImageEnhance.Sharpness(image).enhance(1.12)
    return image


def _apply_style_palette(image, style_hint="material", prompt=""):
    style = _infer_material_style(style_hint, prompt)
    palettes = {
        "leather": (
            [(0.0, (30, 22, 12)), (0.42, (82, 58, 32)), (0.74, (146, 105, 58)), (1.0, (226, 188, 118))],
            0.94,
        ),
        "ice": (
            [(0.0, (8, 72, 150)), (0.42, (28, 175, 235)), (0.76, (135, 235, 255)), (1.0, (255, 255, 255))],
            0.66,
        ),
        "water": (
            [(0.0, (0, 55, 130)), (0.45, (18, 140, 230)), (0.78, (90, 220, 255)), (1.0, (238, 255, 255))],
            0.62,
        ),
        "fire": (
            [(0.0, (58, 8, 0)), (0.42, (196, 38, 0)), (0.75, (255, 130, 14)), (1.0, (255, 236, 95))],
            0.70,
        ),
        "milk": (
            [(0.0, (178, 170, 145)), (0.45, (228, 222, 202)), (0.78, (252, 250, 238)), (1.0, (255, 255, 255))],
            0.74,
        ),
        "metal": (
            [(0.0, (18, 22, 30)), (0.45, (104, 116, 134)), (0.76, (205, 216, 228)), (1.0, (255, 255, 255))],
            0.58,
        ),
        "jade": (
            [(0.0, (8, 58, 42)), (0.42, (32, 136, 92)), (0.74, (122, 214, 164)), (1.0, (236, 255, 232))],
            0.66,
        ),
        "gold": (
            [(0.0, (76, 44, 4)), (0.42, (184, 112, 12)), (0.75, (255, 194, 54)), (1.0, (255, 244, 166))],
            0.70,
        ),
        "ceramic": (
            [(0.0, (48, 92, 84)), (0.44, (104, 168, 154)), (0.75, (184, 226, 212)), (1.0, (250, 255, 246))],
            0.62,
        ),
        "glass": (
            [(0.0, (10, 42, 80)), (0.40, (92, 174, 225)), (0.72, (202, 246, 255)), (1.0, (255, 255, 255))],
            0.52,
        ),
        "neon": (
            [(0.0, (18, 4, 44)), (0.36, (52, 36, 170)), (0.68, (20, 220, 255)), (1.0, (255, 64, 210))],
            0.72,
        ),
        "silk": (
            [(0.0, (70, 4, 18)), (0.42, (168, 28, 52)), (0.76, (234, 96, 126)), (1.0, (255, 216, 198))],
            0.64,
        ),
        "wood": (
            [(0.0, (42, 20, 7)), (0.42, (116, 66, 26)), (0.76, (188, 126, 58)), (1.0, (246, 190, 104))],
            0.68,
        ),
        "lava": (
            [(0.0, (8, 6, 6)), (0.34, (86, 20, 8)), (0.58, (225, 48, 0)), (0.82, (255, 140, 18)), (1.0, (255, 240, 130))],
            0.74,
        ),
        "marble": (
            [(0.0, (54, 58, 64)), (0.40, (158, 162, 168)), (0.74, (232, 232, 226)), (1.0, (255, 255, 250))],
            0.62,
        ),
        "pearl": (
            [(0.0, (178, 164, 184)), (0.42, (222, 210, 224)), (0.72, (246, 234, 218)), (1.0, (255, 252, 245))],
            0.58,
        ),
        "candy": (
            [(0.0, (70, 10, 88)), (0.32, (220, 38, 132)), (0.56, (255, 140, 64)), (0.80, (70, 220, 255)), (1.0, (255, 255, 246))],
            0.72,
        ),
    }
    if style not in palettes:
        return image.convert("RGB")

    stops, amount = palettes[style]
    arr = np.array(image.convert("RGB")).astype(np.float32)
    lum = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]) / 255.0
    graded = np.zeros_like(arr)
    for idx in range(len(stops) - 1):
        p0, c0 = stops[idx]
        p1, c1 = stops[idx + 1]
        band = (lum >= p0) & (lum <= p1)
        if not np.any(band):
            continue
        t = np.clip((lum[band] - p0) / max(p1 - p0, 1e-6), 0.0, 1.0)
        c0 = np.array(c0, dtype=np.float32)
        c1 = np.array(c1, dtype=np.float32)
        graded[band] = c0 * (1.0 - t[:, None]) + c1 * t[:, None]
    graded[lum < stops[0][0]] = np.array(stops[0][1], dtype=np.float32)
    graded[lum > stops[-1][0]] = np.array(stops[-1][1], dtype=np.float32)
    mixed = arr * (1.0 - amount) + graded * amount

    if style == "ice":
        mixed = np.clip(mixed * np.array([0.88, 1.08, 1.18], dtype=np.float32), 0, 255)
    elif style == "leather":
        mixed = np.clip(mixed * np.array([0.92, 1.02, 0.82], dtype=np.float32), 0, 255)
    elif style == "metal":
        mixed = np.clip((mixed - 128.0) * 1.22 + 142.0, 0, 255)
    elif style in {"gold", "neon", "lava", "candy"}:
        mixed = np.clip((mixed - 128.0) * 1.20 + 136.0, 0, 255)
    elif style in {"glass", "jade", "ceramic"}:
        mixed = np.clip(mixed * np.array([0.92, 1.08, 1.04], dtype=np.float32), 0, 255)
    elif style == "pearl":
        mixed = np.clip(mixed * np.array([1.04, 1.00, 1.08], dtype=np.float32) + 8.0, 0, 255)

    return Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8), "RGB")


def _style_tint_image(image, prompt, style_hint="material"):
    style = _infer_material_style(style_hint, prompt)
    lower = (prompt or "").lower()
    tint = None
    amount = 0.0
    if style == "ice" or any(token in lower for token in ["ice", "frozen", "crystal", "frost"]):
        tint, amount = (95, 215, 255), 0.48
        image = ImageEnhance.Brightness(image).enhance(1.12)
    elif style == "water" or any(token in lower for token in ["water", "liquid", "transparent"]):
        tint, amount = (55, 170, 245), 0.40
        image = ImageEnhance.Brightness(image).enhance(1.06)
    elif style == "fire" or any(token in lower for token in ["fire", "flame", "ember", "burning"]):
        tint, amount = (255, 72, 12), 0.42
        image = ImageEnhance.Contrast(image).enhance(1.22)
    elif style == "leather" or any(token in lower for token in ["leather", "brown"]):
        tint, amount = (115, 55, 25), 0.46
        image = ImageEnhance.Brightness(image).enhance(0.90)
    elif style == "milk":
        tint, amount = (248, 248, 236), 0.50
        image = ImageEnhance.Brightness(image).enhance(1.20)
    elif style == "metal" or any(token in lower for token in ["metal", "chrome", "mecha"]):
        tint, amount = (190, 198, 210), 0.16
        image = ImageEnhance.Contrast(image).enhance(1.15)
        image = ImageEnhance.Sharpness(image).enhance(1.18)
    elif style == "jade":
        tint, amount = (78, 190, 132), 0.34
        image = ImageEnhance.Brightness(image).enhance(1.08)
    elif style == "gold":
        tint, amount = (255, 188, 44), 0.38
        image = ImageEnhance.Contrast(image).enhance(1.18)
    elif style == "ceramic":
        tint, amount = (158, 220, 206), 0.30
        image = ImageEnhance.Brightness(image).enhance(1.08)
    elif style == "glass":
        tint, amount = (160, 230, 255), 0.28
        image = ImageEnhance.Brightness(image).enhance(1.12)
    elif style == "neon":
        tint, amount = (82, 34, 220), 0.36
        image = ImageEnhance.Contrast(image).enhance(1.28)
    elif style == "silk":
        tint, amount = (190, 26, 54), 0.36
        image = ImageEnhance.Contrast(image).enhance(1.10)
    elif style == "wood":
        tint, amount = (126, 70, 26), 0.38
        image = ImageEnhance.Brightness(image).enhance(0.94)
    if tint:
        overlay = Image.new("RGB", image.size, tint)
        image = Image.blend(image, overlay, amount)
    image = _apply_style_palette(image, style_hint=style, prompt=prompt)
    return image


def _apply_loras(pipe, tags):
    global _loaded_loras
    if hasattr(pipe, "unload_lora_weights"):
        try:
            pipe.unload_lora_weights()
        except Exception:
            pass
    _loaded_loras = []

    available = {item["name"]: item for item in _list_loras()}
    adapter_names = []
    adapter_weights = []
    for idx, (name, weight) in enumerate(tags[:4]):
        item = available.get(name)
        if not item:
            continue
        adapter = f"lora_{idx}_{re.sub(r'[^a-zA-Z0-9_]+', '_', name)}"
        pipe.load_lora_weights(str(Path(item["path"]).parent), weight_name=Path(item["path"]).name, adapter_name=adapter)
        adapter_names.append(adapter)
        adapter_weights.append(weight)
        _loaded_loras.append({"name": name, "weight": weight})

    if adapter_names and hasattr(pipe, "set_adapters"):
        pipe.set_adapters(adapter_names, adapter_weights=adapter_weights)


@app.get("/sdapi/v1/options")
async def options():
    _ensure_dirs()
    return {
        "sd_model_checkpoint": str(MODEL_DIR),
        "sd_lora_dir": str(LORA_DIR),
        "controlnet_canny": {
            "path": str(CONTROLNET_CANNY_DIR),
            "installed": (CONTROLNET_CANNY_DIR / "config.json").exists(),
            "mode": "canny",
        },
        "device": DEVICE,
        "loras": _list_loras(),
        "loaded_loras": _loaded_loras,
    }


@app.get("/sdapi/v1/loras")
async def loras():
    return _list_loras()


@app.post("/sdapi/v1/txt2img")
async def txt2img(request: Request):
    data = await request.json()
    prompt, lora_tags = _parse_lora_tags(str(data.get("prompt") or ""))
    negative = str(data.get("negative_prompt") or "")
    width = max(256, min(int(data.get("width") or 768), 1024))
    height = max(256, min(int(data.get("height") or 768), 1024))
    steps = max(1, min(int(data.get("steps") or 24), 80))
    cfg = max(1.0, min(float(data.get("cfg_scale") or 7.0), 20.0))
    seed_value = data.get("seed", None)
    if seed_value in (None, "", -1):
        seed_value = int.from_bytes(os.urandom(4), "little")
    seed_value = int(seed_value)

    with _lock:
        pipe = _load_pipe()
        _apply_loras(pipe, lora_tags)
        generator = torch.Generator(device=DEVICE).manual_seed(seed_value)
        start = time.time()
        image = pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=generator,
        ).images[0]
        elapsed = time.time() - start

    image_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out_path = OUTPUT_DIR / f"{image_id}.png"
    image.save(out_path)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    return JSONResponse({
        "images": [encoded],
        "parameters": {
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg,
            "seed": seed_value,
            "lora_tags": lora_tags,
        },
        "info": json.dumps({
            "seed": seed_value,
            "elapsed_sec": round(elapsed, 2),
            "saved": str(out_path),
            "loaded_loras": _loaded_loras,
        }, ensure_ascii=False),
    })


@app.post("/sdapi/v1/img2img")
async def img2img(request: Request):
    data = await request.json()
    prompt, lora_tags = _parse_lora_tags(str(data.get("prompt") or ""))
    negative = str(data.get("negative_prompt") or "")
    width = max(256, min(int(data.get("width") or 768), 1024))
    height = max(256, min(int(data.get("height") or 768), 1024))
    width = width - (width % 8)
    height = height - (height % 8)
    steps = max(1, min(int(data.get("steps") or 24), 80))
    cfg = max(1.0, min(float(data.get("cfg_scale") or 7.0), 20.0))
    denoise = max(0.05, min(float(data.get("denoising_strength") or data.get("strength") or 0.65), 1.0))
    control_enabled = _as_bool(data.get("controlnet_enabled", True), default=True)
    control_strength = max(0.0, min(float(data.get("controlnet_conditioning_scale") or 1.15), 2.0))
    glyph_lock_enabled = _as_bool(data.get("glyph_lock_enabled", True), default=True)
    glyph_mask_dilate = max(0, min(int(data.get("glyph_mask_dilate") or 2), 8))
    glyph_mask_blur = max(0.0, min(float(data.get("glyph_mask_blur") or 1.0), 4.0))
    material_fill_enabled = _as_bool(data.get("material_fill_enabled", True), default=True)
    material_intensity = max(0.2, min(float(data.get("material_intensity") or 2.35), 3.0))
    depth_strength = max(0.0, min(float(data.get("depth_strength") or 1.75), 2.5))
    shadow_strength = max(0.0, min(float(data.get("shadow_strength") or 0.65), 1.0))
    style_hint = _infer_material_style(data.get("style_hint") or "", f"{prompt} {' '.join(name for name, _ in lora_tags)}")
    canny_low = int(data.get("canny_low") or 80)
    canny_high = int(data.get("canny_high") or 180)
    seed_value = data.get("seed", None)
    if seed_value in (None, "", -1):
        seed_value = int.from_bytes(os.urandom(4), "little")
    seed_value = int(seed_value)

    init_images = data.get("init_images") or data.get("images") or []
    init_image = _fit_image(_decode_image(init_images), width, height)
    control_image = _canny_image(init_image, canny_low, canny_high)

    with _lock:
        pipe = _load_pipe() if material_fill_enabled else (_load_control_pipe() if control_enabled else _load_img2img_pipe())
        _apply_loras(pipe, lora_tags)
        generator = torch.Generator(device=DEVICE).manual_seed(seed_value)
        start = time.time()
        if material_fill_enabled:
            generation_prompt = _material_fill_prompt(prompt, style_hint=style_hint)
            generation_negative = _material_negative_prompt(negative, generation_prompt, style_hint=style_hint)
            image = pipe(
                prompt=generation_prompt,
                negative_prompt=generation_negative,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=generator,
            ).images[0]
            image = _enhance_material_image(image)
            image = _style_tint_image(image, generation_prompt, style_hint=style_hint)
        elif control_enabled:
            generation_prompt = prompt
            generation_negative = negative
            image = pipe(
                prompt=prompt,
                negative_prompt=negative,
                image=init_image,
                control_image=control_image,
                strength=denoise,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=cfg,
                controlnet_conditioning_scale=control_strength,
                generator=generator,
            ).images[0]
        else:
            generation_prompt = prompt
            generation_negative = negative
            image = pipe(
                prompt=prompt,
                negative_prompt=negative,
                image=init_image,
                strength=denoise,
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=generator,
            ).images[0]
        elapsed = time.time() - start

    raw_image = image
    glyph_mask = None
    if glyph_lock_enabled:
        if material_fill_enabled:
            image, glyph_mask = _compose_glyph_material(
                init_image,
                raw_image,
                dilate=glyph_mask_dilate,
                blur=glyph_mask_blur,
                material_intensity=material_intensity,
                depth_strength=depth_strength,
                shadow_strength=shadow_strength,
                style_hint=style_hint,
            )
        else:
            image, glyph_mask = _lock_glyph_shape(
                init_image,
                raw_image,
                dilate=glyph_mask_dilate,
                blur=glyph_mask_blur,
            )

    image_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out_path = OUTPUT_DIR / f"{image_id}.png"
    control_path = OUTPUT_DIR / f"{image_id}_control_canny.png"
    raw_path = OUTPUT_DIR / f"{image_id}_raw_sd.png"
    mask_path = OUTPUT_DIR / f"{image_id}_glyph_mask.png"
    image.save(out_path)
    control_image.save(control_path)
    if glyph_lock_enabled:
        raw_image.save(raw_path)
    if glyph_mask is not None:
        glyph_mask.save(mask_path)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    return JSONResponse({
        "images": [encoded],
        "parameters": {
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg,
            "seed": seed_value,
            "lora_tags": lora_tags,
            "denoising_strength": denoise,
            "controlnet_enabled": control_enabled,
            "controlnet_conditioning_scale": control_strength,
            "glyph_lock_enabled": glyph_lock_enabled,
            "glyph_mask_dilate": glyph_mask_dilate,
            "glyph_mask_blur": glyph_mask_blur,
            "material_fill_enabled": material_fill_enabled,
            "material_intensity": material_intensity,
            "depth_strength": depth_strength,
            "shadow_strength": shadow_strength,
            "style_hint": style_hint,
            "material_prompt": generation_prompt,
            "material_negative_prompt": generation_negative,
        },
        "info": json.dumps({
            "seed": seed_value,
            "elapsed_sec": round(elapsed, 2),
            "saved": str(out_path),
            "control_image": str(control_path),
            "raw_sd_image": str(raw_path) if glyph_lock_enabled else "",
            "glyph_mask": str(mask_path) if glyph_mask is not None else "",
            "glyph_lock_enabled": glyph_lock_enabled,
            "material_fill_enabled": material_fill_enabled,
            "material_intensity": material_intensity,
            "depth_strength": depth_strength,
            "shadow_strength": shadow_strength,
            "style_hint": style_hint,
            "loaded_loras": _loaded_loras,
        }, ensure_ascii=False),
    })


@app.get("/health")
async def health():
    return {"ok": True, "model_dir": str(MODEL_DIR), "device": DEVICE}
