from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
import argparse
import colorsys
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TK = True
except ImportError:
    tk = None
    filedialog = messagebox = ttk = None
    HAS_TK = False
from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFile,
    ImageFilter,
    ImageFont,
    ImageOps,
)
# 일부 데이터가 누락된 사진도 가능한 범위에서 읽습니다.
ImageFile.LOAD_TRUNCATED_IMAGES = True
# 출력 해상도(1080p) 및 서브픽셀 카메라 워킹을 위한 여유 마진 캔버스(16:9 비율)
WIDTH, HEIGHT = 1920, 1080
CANVAS_W, CANVAS_H = 2400, 1350
DEFAULT_FPS = 30
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 시네마틱 xfade 전환 효과 프리셋
TRANSITIONS_CINEMATIC = [
    "smoothleft",
    "dissolve",
    "smoothright",
    "circleopen",
    "smoothup",
    "fade",
    "diagtl",
    "smoothdown",
    "hlslice",
    "diagbr",
]
TRANSITIONS_SOFT = [
    "dissolve",
    "fade",
    "smoothleft",
    "smoothright",
]
TRANSITIONS_DYNAMIC = [
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "circleopen",
    "circleclose",
    "hlslice",
    "vuslice",
    "diagtl",
    "diagbr",
]

class Cancelled(Exception):
    pass

def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled()

# ---------------------------------------------------------------------------
# 크로스 플랫폼 한글 폰트 로더
# ---------------------------------------------------------------------------
_FONT_CACHE = {}

def load_korean_font(size, bold=False, serif=False):
    key = (size, bold, serif)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = []
    if serif:
        candidates.extend([
            "C:/Windows/Fonts/batang.ttc",
            "C:/Windows/Fonts/NanumMyeongjoBold.ttf" if bold else "C:/Windows/Fonts/NanumMyeongjo.ttf",
            "/System/Library/Fonts/Supplemental/AppleMyungjo.ttf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumMyeongjoBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
        ])
    if bold:
        candidates.extend([
            "C:/Windows/Fonts/malgunbd.ttf",
            "C:/Windows/Fonts/NanumGothicBold.ttf",
            "C:/Windows/Fonts/NanumSquareNeo-dEb.ttf",
            "C:/Windows/Fonts/gulim.ttc",
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/Library/Fonts/NanumGothicBold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ])
    else:
        candidates.extend([
            "C:/Windows/Fonts/malgun.ttf",
            "C:/Windows/Fonts/NanumGothic.ttf",
            "C:/Windows/Fonts/gulim.ttc",
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/Library/Fonts/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ])
    for path_str in candidates:
        p = Path(path_str)
        if p.is_file():
            try:
                font = ImageFont.truetype(str(p), size=size)
                _FONT_CACHE[key] = font
                return font
            except Exception:
                continue
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font

def draw_diamond_ornament(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill):
    """폰트에 의존하지 않는 깔끔한 벡터 다이아몬드 심볼을 그립니다."""
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)

# ---------------------------------------------------------------------------
# 사진 메타데이터 및 폴더 분석
# ---------------------------------------------------------------------------
@dataclass
class PhotoMeta:
    path: Path
    capture_ts: float
    date_str: str
    event_date: datetime | None
    event_title: str
    clean_name: str
    width: int = 1920
    height: int = 1080
    @property
    def aspect(self) -> float:
        return self.width / max(self.height, 1)
    @property
    def is_portrait(self) -> bool:
        return self.aspect < 0.92

@dataclass
class SceneSpec:
    kind: str  # "intro", "hero_gallery", "polaroid", "cinema_full", "collage_2", "collage_3"
    photos: list[PhotoMeta] = field(default_factory=list)
    chapter_index: int = 1
    total_chapters: int = 1
    is_new_chapter: bool = False
    scene_index: int = 0
    total_scenes: int = 1
    main_title: str = ""
    sub_title: str = ""
    date_badge: str = ""

def capture_date(path: Path) -> float:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            value = exif.get(36867) or exif.get(306)
            if value:
                return datetime.strptime(str(value)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return datetime.now().timestamp()

def parse_folder_header(path: Path):
    """폴더명 맨 앞의 [YYYY-MM-DD] 또는 YYYY-MM-DD 형식을 파싱하고 깔끔한 행사 제목을 반환합니다."""
    name = path.name.strip()
    match = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$", name)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y-%m-%d")
            title = match.group(2).strip(" -_·") or name
            return dt, title
        except ValueError:
            pass
    match2 = re.match(r"^(\d{4}[.-]\d{2}[.-]\d{2})\s+(.*)$", name)
    if match2:
        raw_date = match2.group(1).replace(".", "-")
        try:
            dt = datetime.strptime(raw_date, "%Y-%m-%d")
            title = match2.group(2).strip(" -_·") or name
            return dt, title
        except ValueError:
            pass
    return None, name

def clean_filename(path: Path) -> str:
    stem = path.stem
    cleaned = re.sub(r"^(?:IMG|DSC|PHOTO|PIC|PXL)?[-_\s]*\d{1,4}[-_\s]+", "", stem, flags=re.IGNORECASE)
    cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
    if not cleaned or cleaned.isdigit():
        return stem.replace("_", " ")
    return cleaned

def collect_photo_groups(folder: Path):
    """
    폴더별 사진을 수집합니다.
    중첩 폴더에 날짜가 없으면 상위 행사 폴더의 날짜와 이름을 사용합니다.
    """
    root = Path(folder)
    groups = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            [
                name for name in directories
                if not (current_path / name).is_symlink()
                and not name.startswith(".")
            ],
            key=str.casefold,
        )
        photos = [
            current_path / name
            for name in filenames
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS
            and (current_path / name).is_file()
        ]
        if not photos:
            continue
        event_date = None
        event_folder = current_path
        event_title = current_path.name
        candidate = current_path
        while True:
            dt, parsed_title = parse_folder_header(candidate)
            if dt is not None:
                event_date = dt
                event_folder = candidate
                event_title = parsed_title
                break
            if candidate == root:
                break
            candidate = candidate.parent
        if event_date is None:
            if current_path != root:
                _, event_title = parse_folder_header(current_path)
            else:
                _, event_title = parse_folder_header(root)
        groups.append((event_date, event_folder, current_path, event_title, photos))
    return groups

def find_photos_with_meta(folder: Path, order: str) -> list[PhotoMeta]:
    root = Path(folder)
    groups = collect_photo_groups(root)
    groups.sort(
        key=lambda group: (
            group[0] is None,
            group[0] or datetime.max,
            group[1].relative_to(root).as_posix().casefold(),
            group[2].relative_to(root).as_posix().casefold(),
        )
    )
    result: list[PhotoMeta] = []
    for event_date, _, _, event_title, photos in groups:
        if order == "촬영 날짜":
            photos.sort(key=lambda p: (capture_date(p), p.name.casefold()))
        else:
            photos.sort(key=lambda p: p.name.casefold())
        for p in photos:
            ts = capture_date(p)
            dt_obj = event_date if event_date is not None else datetime.fromtimestamp(ts)
            date_str = dt_obj.strftime("%Y. %m. %d")
            result.append(
                PhotoMeta(
                    path=p,
                    capture_ts=ts,
                    date_str=date_str,
                    event_date=event_date,
                    event_title=event_title,
                    clean_name=clean_filename(p),
                )
            )
    return result

# ---------------------------------------------------------------------------
# 스마트 씬 플래너 (단독 씬 + 2장/3장 콜라주 + 오프닝 타이틀 자동 구성)
# ---------------------------------------------------------------------------
def build_scenes(
    photos: list[PhotoMeta],
    theme: str,
    smart_collage: bool,
    include_intro: bool,
    custom_title: str = "",
) -> list[SceneSpec]:
    if not photos:
        return []
    chapters = []
    for p in photos:
        if p.event_title not in chapters:
            chapters.append(p.event_title)
    total_chapters = max(len(chapters), 1)
    scenes: list[SceneSpec] = []
    # 1. 오프닝 인트로 타이틀 카드
    if include_intro:
        first_date = min(p.date_str for p in photos)
        last_date = max(p.date_str for p in photos)
        date_range = first_date if first_date == last_date else f"{first_date}  —  {last_date}"
        title_text = custom_title.strip() or (
            chapters[0] if len(chapters) == 1 else f"{chapters[0]} 외 {len(chapters) - 1}개 스토리"
        )
        sub_text = (
            f"총 {len(photos)}장의 순간들  ·  {total_chapters}개의 챕터"
            if total_chapters > 1
            else f"총 {len(photos)}장의 소중한 순간들"
        )
        step = max(1, len(photos) // 4)
        sample_photos = [photos[min(i * step, len(photos) - 1)] for i in range(min(4, len(photos)))]
        scenes.append(
            SceneSpec(
                kind="intro",
                photos=sample_photos,
                chapter_index=1,
                total_chapters=total_chapters,
                is_new_chapter=False,
                main_title=title_text,
                sub_title=sub_text,
                date_badge=date_range,
            )
        )
    # 2. 본편 씬 구성
    idx = 0
    n = len(photos)
    seen_chapters = set()
    single_counter = 0
    while idx < n:
        curr = photos[idx]
        chap_idx = chapters.index(curr.event_title) + 1
        is_new_chap = curr.event_title not in seen_chapters
        seen_chapters.add(curr.event_title)
        same_chap_2 = (idx + 1 < n) and (photos[idx + 1].event_title == curr.event_title)
        same_chap_3 = (
            same_chap_2
            and (idx + 2 < n)
            and (photos[idx + 2].event_title == curr.event_title)
        )
        grouped = False
        if smart_collage and theme != "다큐멘터리 풀스크린":
            if (
                same_chap_3
                and curr.is_portrait
                and photos[idx + 1].is_portrait
                and photos[idx + 2].is_portrait
            ):
                batch = [curr, photos[idx + 1], photos[idx + 2]]
                scenes.append(
                    SceneSpec(
                        kind="collage_3",
                        photos=batch,
                        chapter_index=chap_idx,
                        total_chapters=total_chapters,
                        is_new_chapter=is_new_chap,
                        main_title=custom_title.strip() or curr.event_title,
                        sub_title=" · ".join(dict.fromkeys(p.clean_name for p in batch)),
                        date_badge=curr.date_str,
                    )
                )
                idx += 3
                grouped = True
            elif same_chap_2 and curr.is_portrait and photos[idx + 1].is_portrait:
                batch = [curr, photos[idx + 1]]
                scenes.append(
                    SceneSpec(
                        kind="collage_2",
                        photos=batch,
                        chapter_index=chap_idx,
                        total_chapters=total_chapters,
                        is_new_chapter=is_new_chap,
                        main_title=custom_title.strip() or curr.event_title,
                        sub_title=" · ".join(dict.fromkeys(p.clean_name for p in batch)),
                        date_badge=curr.date_str,
                    )
                )
                idx += 2
                grouped = True
            elif (
                same_chap_2
                and not is_new_chap
                and single_counter >= 2
                and theme in ("시네마틱 마스터피스 (추천)", "감성 폴라로이드 & 필름")
            ):
                batch = [curr, photos[idx + 1]]
                scenes.append(
                    SceneSpec(
                        kind="collage_2",
                        photos=batch,
                        chapter_index=chap_idx,
                        total_chapters=total_chapters,
                        is_new_chapter=is_new_chap,
                        main_title=custom_title.strip() or curr.event_title,
                        sub_title=" · ".join(dict.fromkeys(p.clean_name for p in batch)),
                        date_badge=curr.date_str,
                    )
                )
                idx += 2
                single_counter = 0
                grouped = True
        if grouped:
            continue
        if theme == "감성 폴라로이드 & 필름":
            kind = "polaroid"
        elif theme == "다큐멘터리 풀스크린":
            kind = "cinema_full" if not curr.is_portrait else "hero_gallery"
        elif theme == "모던 3D 갤러리":
            kind = "hero_gallery"
        else:
            if curr.is_portrait:
                kind = "polaroid" if (single_counter % 2 == 1) else "hero_gallery"
            else:
                cycle = single_counter % 3
                if cycle == 0:
                    kind = "hero_gallery"
                elif cycle == 1:
                    kind = "cinema_full"
                else:
                    kind = "polaroid"
        scenes.append(
            SceneSpec(
                kind=kind,
                photos=[curr],
                chapter_index=chap_idx,
                total_chapters=total_chapters,
                is_new_chapter=is_new_chap,
                main_title=custom_title.strip() or curr.event_title,
                sub_title=curr.clean_name,
                date_badge=curr.date_str,
            )
        )
        single_counter += 1
        idx += 1
    photo_scenes = [s for s in scenes if s.kind != "intro"]
    total_photo_scenes = max(len(photo_scenes), 1)
    counter = 1
    for s in scenes:
        if s.kind == "intro":
            s.scene_index = 0
            s.total_scenes = total_photo_scenes
        else:
            s.scene_index = counter
            s.total_scenes = total_photo_scenes
            counter += 1
    return scenes

# ---------------------------------------------------------------------------
# 고품격 그래픽 합성 엔진 (Pillow Supersampled Compositor)
# ---------------------------------------------------------------------------
def load_clean_rgb(path: Path, max_dim: int = 2560) -> Image.Image:
    with Image.open(path) as raw:
        image = ImageOps.exif_transpose(raw)
        image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, (245, 245, 247))
            base.paste(rgba, mask=rgba.getchannel("A"))
            return base
        return image.convert("RGB")

def extract_palette(images: list[Image.Image]):
    """사진에서 생동감 있는 대표 포인트 컬러와 보조 컬러를 추출합니다."""
    samples = []
    for img in images:
        small = img.resize((36, 36), Image.Resampling.BILINEAR)
        for r, g, b in small.getdata():
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            weight = (s + 0.15) * (0.4 + 0.6 * v)
            samples.append((weight, h, s, v))
    if not samples:
        return (245, 166, 35), (74, 144, 226), (30, 38, 56)
    samples.sort(reverse=True, key=lambda x: x[0])
    _, h1, s1, v1 = samples[0]
    ar, ag, ab = colorsys.hsv_to_rgb(h1, min(0.85, max(0.55, s1 * 1.25)), min(0.96, max(0.78, v1 * 1.15)))
    accent = (int(ar * 255), int(ag * 255), int(ab * 255))
    _, h2, s2, _ = samples[min(len(samples) // 3, len(samples) - 1)]
    sr, sg, sb = colorsys.hsv_to_rgb((h2 + 0.08) % 1.0, min(0.75, max(0.45, s2)), 0.72)
    secondary = (int(sr * 255), int(sg * 255), int(sb * 255))
    dr, dg, db = colorsys.hsv_to_rgb(h1, min(0.55, s1 * 0.8), 0.14)
    dark_tint = (int(dr * 255), int(dg * 255), int(db * 255))
    return accent, secondary, dark_tint

def make_rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    w, h = size
    ss = Image.new("L", (w * 2, h * 2), 0)
    draw = ImageDraw.Draw(ss)
    draw.rounded_rectangle([0, 0, w * 2 - 1, h * 2 - 1], radius=radius * 2, fill=255)
    return ss.resize((w, h), Image.Resampling.LANCZOS)

def render_ambient_backdrop(
    images: list[Image.Image],
    accent: tuple[int, int, int],
    secondary: tuple[int, int, int],
    seed: int = 0,
    warm_film: bool = False,
) -> Image.Image:
    """
    단순 블러를 넘어선 앰비언트 컬러 메쉬 + 보케(Bokeh) 라이트 오브 + 비네팅 배경을 합성합니다.
    """
    cw, ch = CANVAS_W, CANVAS_H
    sw, sh = cw // 4, ch // 4
    if len(images) == 1:
        base_small = ImageOps.fit(images[0], (sw, sh), Image.Resampling.BILINEAR)
    else:
        base_small = Image.new("RGB", (sw, sh))
        seg_w = sw // len(images)
        for i, im in enumerate(images):
            part = ImageOps.fit(im, (seg_w + 4, sh), Image.Resampling.BILINEAR)
            base_small.paste(part, (i * seg_w, 0))
    base_small = base_small.filter(ImageFilter.GaussianBlur(radius=16))
    base_small = ImageEnhance.Brightness(base_small).enhance(0.38 if not warm_film else 0.42)
    base_small = ImageEnhance.Color(base_small).enhance(1.38 if not warm_film else 1.20)
    bg = base_small.resize((cw, ch), Image.Resampling.BILINEAR).convert("RGBA")
    gw, gh = cw // 2, ch // 2
    glow = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    rng = random.Random(seed * 97 + 13)
    for gx, gy, gr, col in [
        (int(gw * 0.18), int(gh * 0.22), int(gw * 0.28), (*accent, 48)),
        (int(gw * 0.82), int(gh * 0.78), int(gw * 0.30), (*secondary, 44)),
    ]:
        gdraw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=col)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=36))
    bdraw = ImageDraw.Draw(glow)
    bokeh_colors = [
        (*accent, 34),
        (*secondary, 30),
        (255, 232, 185, 36),
        (255, 255, 255, 22),
    ]
    for _ in range(16):
        edge_mode = rng.randint(0, 3)
        if edge_mode == 0:
            bx = rng.randint(30, int(gw * 0.22))
            by = rng.randint(30, gh - 30)
        elif edge_mode == 1:
            bx = rng.randint(int(gw * 0.78), gw - 30)
            by = rng.randint(30, gh - 30)
        elif edge_mode == 2:
            bx = rng.randint(30, gw - 30)
            by = rng.randint(25, int(gh * 0.20))
        else:
            bx = rng.randint(30, gw - 30)
            by = rng.randint(int(gh * 0.80), gh - 25)
        br = rng.randint(14, 58)
        bcol = rng.choice(bokeh_colors)
        bdraw.ellipse(
            [bx - br, by - br, bx + br, by + br],
            fill=bcol,
            outline=(255, 255, 255, min(60, bcol[3] + 16)),
            width=2,
        )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=5))
    bg = Image.alpha_composite(bg, glow.resize((cw, ch), Image.Resampling.BILINEAR))
    vig = Image.new("L", (gw, gh), 0)
    vdraw = ImageDraw.Draw(vig)
    vdraw.ellipse([int(-gw * 0.08), int(-gh * 0.08), int(gw * 1.08), int(gh * 1.08)], fill=255)
    vig = ImageOps.invert(vig).filter(ImageFilter.GaussianBlur(radius=42))
    vig_full = vig.resize((cw, ch), Image.Resampling.BILINEAR)
    dark_layer = Image.new("RGBA", (cw, ch), (6, 9, 16, 145))
    bg.paste(dark_layer, (0, 0), mask=ImageEnhance.Brightness(vig_full).enhance(0.58))
    return bg

def make_framed_photo_card(
    photo: Image.Image,
    target_w: int,
    target_h: int,
    style: str = "glass",
    caption_title: str = "",
    caption_date: str = "",
    accent: tuple[int, int, int] = (245, 166, 35),
) -> Image.Image:
    """
    사진 1장을 입체 드롭 섀도가 포함된 3D 갤러리 카드 또는 감성 폴라로이드 카드로 변환합니다.
    """
    if style == "polaroid":
        img_fitted = ImageOps.contain(photo, (target_w, target_h), Image.Resampling.LANCZOS)
        pw, ph = img_fitted.size
        pad_side = max(22, int(min(pw, ph) * 0.036))
        pad_top = pad_side
        pad_bottom = max(74, int(ph * 0.125))
        card_w = pw + pad_side * 2
        card_h = ph + pad_top + pad_bottom
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        mask = make_rounded_mask((card_w, card_h), radius=14)
        paper = Image.new("RGBA", (card_w, card_h), (251, 249, 244, 255))
        pdraw = ImageDraw.Draw(paper)
        pdraw.rectangle(
            [pad_side - 1, pad_top - 1, pad_side + pw, pad_top + ph],
            outline=(210, 206, 198, 255),
            width=1,
        )
        paper.paste(img_fitted.convert("RGBA"), (pad_side, pad_top))
        if caption_title or caption_date:
            font_meta = load_korean_font(max(16, int(pad_bottom * 0.25)), bold=True)
            font_date = load_korean_font(max(15, int(pad_bottom * 0.22)), bold=False)
            cy = pad_top + ph + pad_bottom // 2
            right_x = card_w - pad_side - 6
            if caption_title and caption_date:
                title_short = caption_title[:20] + ("…" if len(caption_title) > 20 else "")
                date_w = pdraw.textlength(caption_date, font=font_date)
                pdraw.text(
                    (right_x, cy),
                    caption_date,
                    font=font_date,
                    fill=(120, 125, 136, 240),
                    anchor="rm",
                )
                draw_diamond_ornament(
                    pdraw, int(right_x - date_w - 16), cy, 4, (*accent, 220)
                )
                pdraw.text(
                    (int(right_x - date_w - 30), cy),
                    title_short,
                    font=font_meta,
                    fill=(52, 56, 68, 242),
                    anchor="rm",
                )
            elif caption_date:
                pdraw.text(
                    (right_x, cy),
                    caption_date,
                    font=font_date,
                    fill=(110, 115, 126, 240),
                    anchor="rm",
                )
        card.paste(paper, (0, 0), mask=mask)
        return card
    img_fitted = ImageOps.contain(photo, (target_w, target_h), Image.Resampling.LANCZOS)
    pw, ph = img_fitted.size
    radius = max(18, int(min(pw, ph) * 0.032))
    card = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    mask = make_rounded_mask((pw, ph), radius=radius)
    card.paste(img_fitted.convert("RGBA"), (0, 0), mask=mask)
    cdraw = ImageDraw.Draw(card)
    cdraw.rounded_rectangle(
        [1, 1, pw - 2, ph - 2],
        radius=radius,
        outline=(255, 255, 255, 180),
        width=3,
    )
    if pw > 360 and ph > 360:
        inset = 14
        cdraw.rounded_rectangle(
            [inset, inset, pw - 1 - inset, ph - 1 - inset],
            radius=max(8, radius - 8),
            outline=(255, 255, 255, 68),
            width=1,
        )
    return card

def paste_card_with_shadow(
    canvas: Image.Image,
    card: Image.Image,
    center_xy: tuple[int, int],
    angle: float = 0.0,
    shadow_offset: tuple[int, int] = (0, 22),
    shadow_blur: int = 28,
    shadow_alpha: int = 165,
):
    """
    카드를 지정된 각도로 회전하고 부드러운 다중 레이어 드롭 섀도와 함께 캔버스에 합성합니다.
    """
    cw, ch = card.size
    pad = shadow_blur * 2 + max(abs(shadow_offset[0]), abs(shadow_offset[1])) + 20
    alpha = card.getchannel("A")
    shadow_small_w = max(1, (cw + pad * 2) // 2)
    shadow_small_h = max(1, (ch + pad * 2) // 2)
    shadow_mask = Image.new("L", (cw + pad * 2, ch + pad * 2), 0)
    shadow_mask.paste(alpha, (pad + shadow_offset[0], pad + shadow_offset[1]))
    shadow_mask = (
        shadow_mask.resize((shadow_small_w, shadow_small_h), Image.Resampling.BILINEAR)
        .filter(ImageFilter.GaussianBlur(radius=max(2, shadow_blur // 2)))
        .resize((cw + pad * 2, ch + pad * 2), Image.Resampling.BILINEAR)
    )
    shadow_mask = ImageEnhance.Brightness(shadow_mask).enhance(shadow_alpha / 255.0)
    comp = Image.new("RGBA", (cw + pad * 2, ch + pad * 2), (0, 0, 0, 0))
    black_layer = Image.new("RGBA", comp.size, (4, 7, 14, 255))
    comp.paste(black_layer, (0, 0), mask=shadow_mask)
    comp.alpha_composite(card, (pad, pad))
    if abs(angle) > 0.05:
        comp = comp.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    dest_x = int(center_xy[0] - comp.width // 2)
    dest_y = int(center_xy[1] - comp.height // 2)
    canvas.alpha_composite(comp, (dest_x, dest_y))

def render_scene_plate(scene: SceneSpec, theme: str) -> tuple[Image.Image, tuple[int, int, int]]:
    """
    각 SceneSpec을 2400x1350 고해상도 마스터 이미지로 합성합니다.
    (서브픽셀 줌·패닝 시 가장자리가 잘리지 않도록 중앙 안전 영역 안에 레이아웃을 배치합니다.)
    """
    loaded_imgs = [load_clean_rgb(p.path) for p in scene.photos]
    accent, secondary, _ = extract_palette(loaded_imgs)
    warm_film = theme == "감성 폴라로이드 & 필름" or scene.kind == "polaroid"
    # 1. 시네마틱 풀스크린 (가로 와이드 사진)
    if scene.kind == "cinema_full" and loaded_imgs:
        hero = ImageOps.fit(loaded_imgs[0], (CANVAS_W, CANVAS_H), Image.Resampling.LANCZOS)
        hero = ImageEnhance.Color(hero).enhance(1.12)
        hero = ImageEnhance.Contrast(hero).enhance(1.06)
        canvas = hero.convert("RGBA")
        grad = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(grad)
        start_y = int(CANVAS_H * 0.58)
        span_y = max(1, CANVAS_H - start_y)
        for y in range(start_y, CANVAS_H):
            t = max(0.0, (y - start_y) / span_y)
            a = int(165 * (t ** 1.6))
            gdraw.line([(0, y), (CANVAS_W, y)], fill=(6, 9, 16, a))
        canvas = Image.alpha_composite(canvas, grad)
        return canvas.convert("RGB"), accent
    canvas = render_ambient_backdrop(
        loaded_imgs, accent, secondary, seed=scene.scene_index, warm_film=warm_film
    )
    # 2. 오프닝 인트로 타이틀 카드 (4코너 부채꼴 + 리얼 프로스트 글래스 백드롭 블러)
    if scene.kind == "intro":
        fan_specs = [
            (int(CANVAS_W * 0.24), int(CANVAS_H * 0.35), -10.5),
            (int(CANVAS_W * 0.76), int(CANVAS_H * 0.35), 10.0),
            (int(CANVAS_W * 0.25), int(CANVAS_H * 0.67), 7.5),
            (int(CANVAS_W * 0.75), int(CANVAS_H * 0.67), -8.0),
        ]
        for i, im in enumerate(loaded_imgs[:4]):
            cx, cy, ang = fan_specs[i % len(fan_specs)]
            card = make_framed_photo_card(
                im, 560, 400, style="polaroid", caption_date=scene.photos[i].date_str, accent=accent
            )
            paste_card_with_shadow(canvas, card, (cx, cy), angle=ang, shadow_alpha=145)
        panel_w, panel_h = 1220, 460
        px1 = (CANVAS_W - panel_w) // 2
        py1 = (CANVAS_H - panel_h) // 2
        behind = canvas.crop((px1, py1, px1 + panel_w, py1 + panel_h)).filter(
            ImageFilter.GaussianBlur(radius=22)
        )
        panel_mask = make_rounded_mask((panel_w, panel_h), radius=32)
        panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
        panel.paste(behind, (0, 0), mask=panel_mask)
        tint = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(tint)
        pdraw.rounded_rectangle(
            [0, 0, panel_w - 1, panel_h - 1],
            radius=32,
            fill=(10, 14, 24, 208),
            outline=(255, 255, 255, 95),
            width=2,
        )
        pdraw.rounded_rectangle(
            [16, 16, panel_w - 17, panel_h - 17],
            radius=22,
            outline=(*accent, 140),
            width=2,
        )
        panel = Image.alpha_composite(panel, tint)
        pdraw = ImageDraw.Draw(panel)
        f_badge = load_korean_font(20, bold=True)
        f_title = load_korean_font(56, bold=True)
        f_date = load_korean_font(28, bold=True)
        f_sub = load_korean_font(23, bold=False)
        badge_text = "CINEMATIC MEMORY ARCHIVE"
        bw = pdraw.textlength(badge_text, font=f_badge)
        pdraw.text((panel_w // 2, 74), badge_text, font=f_badge, fill=(*accent, 255), anchor="mm")
        draw_diamond_ornament(pdraw, int(panel_w // 2 - bw // 2 - 24), 74, 7, (*accent, 255))
        draw_diamond_ornament(pdraw, int(panel_w // 2 + bw // 2 + 24), 74, 7, (*accent, 255))
        title_str = scene.main_title
        if len(title_str) > 20:
            f_title = load_korean_font(44, bold=True)
        pdraw.text((panel_w // 2, 188), title_str, font=f_title, fill=(255, 255, 255, 255), anchor="mm")
        pdraw.rounded_rectangle(
            [panel_w // 2 - 84, 258, panel_w // 2 + 84, 264],
            radius=3,
            fill=(*accent, 240),
        )
        pdraw.text((panel_w // 2, 322), scene.date_badge, font=f_date, fill=(238, 241, 248, 250), anchor="mm")
        pdraw.text((panel_w // 2, 384), scene.sub_title, font=f_sub, fill=(182, 192, 210, 238), anchor="mm")
        paste_card_with_shadow(
            canvas, panel, (CANVAS_W // 2, CANVAS_H // 2), angle=0.0, shadow_blur=36, shadow_alpha=195
        )
        return canvas.convert("RGB"), accent
    # 3. 2장 듀얼 매거진 화보 콜라주 (collage_2)
    if scene.kind == "collage_2" and len(loaded_imgs) >= 2:
        card_style = "polaroid" if warm_film else "glass"
        max_w, max_h = (710, 750) if card_style == "polaroid" else (770, 830)
        c1 = make_framed_photo_card(
            loaded_imgs[0],
            max_w,
            max_h,
            style=card_style,
            caption_title=scene.photos[0].clean_name,
            caption_date=scene.photos[0].date_str,
            accent=accent,
        )
        c2 = make_framed_photo_card(
            loaded_imgs[1],
            max_w,
            max_h,
            style=card_style,
            caption_title=scene.photos[1].clean_name,
            caption_date=scene.photos[1].date_str,
            accent=accent,
        )
        ang1 = -2.2 if (scene.scene_index % 2 == 0) else 1.6
        ang2 = 2.0 if (scene.scene_index % 2 == 0) else -1.8
        if theme == "모던 3D 갤러리":
            ang1, ang2 = -0.8, 0.8
        paste_card_with_shadow(
            canvas, c1, (int(CANVAS_W * 0.33), int(CANVAS_H * 0.43)), angle=ang1, shadow_blur=30
        )
        paste_card_with_shadow(
            canvas, c2, (int(CANVAS_W * 0.67), int(CANVAS_H * 0.45)), angle=ang2, shadow_blur=32
        )
        return canvas.convert("RGB"), accent
    # 4. 3장 에디토리얼 트리프티크 콜라주 (collage_3)
    if scene.kind == "collage_3" and len(loaded_imgs) >= 3:
        card_style = "polaroid" if warm_film else "glass"
        max_w, max_h = (510, 700) if card_style == "polaroid" else (540, 760)
        c_left = make_framed_photo_card(
            loaded_imgs[0],
            max_w,
            max_h,
            style=card_style,
            caption_title=scene.photos[0].clean_name,
            caption_date=scene.photos[0].date_str,
            accent=accent,
        )
        c_mid = make_framed_photo_card(
            loaded_imgs[1],
            int(max_w * 1.05),
            int(max_h * 1.05),
            style=card_style,
            caption_title=scene.photos[1].clean_name,
            caption_date=scene.photos[1].date_str,
            accent=accent,
        )
        c_right = make_framed_photo_card(
            loaded_imgs[2],
            max_w,
            max_h,
            style=card_style,
            caption_title=scene.photos[2].clean_name,
            caption_date=scene.photos[2].date_str,
            accent=accent,
        )
        paste_card_with_shadow(
            canvas, c_left, (int(CANVAS_W * 0.25), int(CANVAS_H * 0.45)), angle=-2.8, shadow_blur=28
        )
        paste_card_with_shadow(
            canvas, c_right, (int(CANVAS_W * 0.75), int(CANVAS_H * 0.45)), angle=2.8, shadow_blur=28
        )
        paste_card_with_shadow(
            canvas, c_mid, (int(CANVAS_W * 0.50), int(CANVAS_H * 0.42)), angle=0.0, shadow_blur=36, shadow_alpha=185
        )
        return canvas.convert("RGB"), accent
    # 5. 단독 폴라로이드 아트 카드 (polaroid)
    if scene.kind == "polaroid":
        ghost = ImageEnhance.Brightness(loaded_imgs[0]).enhance(0.55)
        ghost_card = make_framed_photo_card(ghost, 1260, 700, style="polaroid", accent=accent)
        ghost_angle = 4.5 if (scene.scene_index % 2 == 0) else -4.5
        paste_card_with_shadow(
            canvas,
            ghost_card,
            (int(CANVAS_W * 0.51), int(CANVAS_H * 0.43)),
            angle=ghost_angle,
            shadow_blur=24,
            shadow_alpha=110,
        )
        main_card = make_framed_photo_card(
            loaded_imgs[0],
            1320,
            720,
            style="polaroid",
            caption_title=scene.sub_title,
            caption_date=scene.date_badge,
            accent=accent,
        )
        main_angle = -1.8 if (scene.scene_index % 2 == 0) else 1.8
        paste_card_with_shadow(
            canvas,
            main_card,
            (int(CANVAS_W * 0.50), int(CANVAS_H * 0.43)),
            angle=main_angle,
            shadow_blur=34,
            shadow_alpha=175,
        )
        return canvas.convert("RGB"), accent
    # 6. 기본 모던 3D 갤러리 히어로 카드 (hero_gallery)
    main_card = make_framed_photo_card(
        loaded_imgs[0],
        1600,
        860,
        style="glass",
        accent=accent,
    )
    paste_card_with_shadow(
        canvas,
        main_card,
        (CANVAS_W // 2, int(CANVAS_H * 0.44)),
        angle=0.0,
        shadow_blur=34,
        shadow_alpha=175,
    )
    return canvas.convert("RGB"), accent

def render_lower_third_caption(
    scene: SceneSpec,
    caption_mode: str,
    accent: tuple[int, int, int],
) -> Image.Image | None:
    """
    네이티브 1080p 해상도에서 슬라이드-업 애니메이션으로 등장할
    반투명 글래스모피즘(Glassmorphism) 하단 캡션 카드를 생성합니다.
    """
    if caption_mode == "표시 안 함" or scene.kind == "intro":
        return None
    if caption_mode == "촬영 날짜만":
        headline = scene.date_badge
        subline = f"SCENE {scene.scene_index:02d} / {scene.total_scenes:02d}"
        eyebrow = "CAPTURE DATE"
    elif caption_mode == "폴더명 + 파일명":
        headline = scene.main_title
        subline = f"{scene.sub_title}   ·   {scene.date_badge}"
        eyebrow = f"CHAPTER {scene.chapter_index:02d}" if scene.total_chapters > 1 else "STORY ARCHIVE"
    else:
        headline = scene.main_title
        photo_count_tag = f"  ({len(scene.photos)}장 컬렉션)" if len(scene.photos) > 1 else ""
        subline = f"{scene.date_badge}   |   {scene.sub_title}{photo_count_tag}"
        eyebrow = (
            f"NEW CHAPTER {scene.chapter_index:02d}"
            if scene.is_new_chapter and scene.total_chapters > 1
            else f"SCENE {scene.scene_index:02d} / {scene.total_scenes:02d}"
        )
    f_eye = load_korean_font(15, bold=True)
    f_head = load_korean_font(29, bold=True)
    f_sub = load_korean_font(19, bold=False)
    dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w_eye = dummy.textlength(eyebrow, font=f_eye)
    w_head = dummy.textlength(headline, font=f_head)
    w_sub = dummy.textlength(subline, font=f_sub)
    box_w = int(min(1180, max(480, max(w_head, w_sub + 28, w_eye + 140) + 96)))
    box_h = 134
    pad_x = 16
    pad_top = 44
    pad_bottom = 20  # 서브픽셀 슬라이드-업 여유 마진
    img = Image.new("RGBA", (box_w + pad_x * 2, box_h + pad_top + pad_bottom), (0, 0, 0, 0))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(sh)
    sdraw.rounded_rectangle(
        [pad_x, pad_top + 6, pad_x + box_w, pad_top + box_h + 6],
        radius=22,
        fill=(0, 0, 0, 135),
    )
    sh = sh.filter(ImageFilter.GaussianBlur(radius=8))
    img = Image.alpha_composite(img, sh)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [pad_x, pad_top, pad_x + box_w, pad_top + box_h],
        radius=22,
        fill=(12, 16, 26, 236),
        outline=(255, 255, 255, 88),
        width=2,
    )
    bar_x = pad_x + 22
    draw.rounded_rectangle(
        [bar_x, pad_top + 22, bar_x + 7, pad_top + box_h - 22],
        radius=3,
        fill=(*accent, 255),
    )
    text_x = bar_x + 24
    badge_w = int(w_eye + 40)
    badge_y1, badge_y2 = pad_top + 18, pad_top + 44
    badge_cy = (badge_y1 + badge_y2) // 2
    draw.rounded_rectangle(
        [text_x, badge_y1, text_x + badge_w, badge_y2],
        radius=8,
        fill=(*accent, 46),
        outline=(*accent, 150),
        width=1,
    )
    draw_diamond_ornament(draw, text_x + 14, badge_cy, 5, (*accent, 255))
    draw.text((text_x + 26, badge_cy), eyebrow, font=f_eye, fill=(*accent, 255), anchor="lm")
    draw.text((text_x, pad_top + 68), headline, font=f_head, fill=(255, 255, 255, 255), anchor="lm")
    sub_cy = pad_top + 104
    draw.ellipse([text_x, sub_cy - 5, text_x + 10, sub_cy + 5], fill=(*accent, 235))
    draw.text((text_x + 18, sub_cy), subline, font=f_sub, fill=(202, 210, 226, 245), anchor="lm")
    return img

# ---------------------------------------------------------------------------
# 인터넷 아카이브 CC0 배경음악 자동 검색
# ---------------------------------------------------------------------------
def read_json(url, cancel):
    check_cancel(cancel)
    request = urllib.request.Request(
        url, headers={"User-Agent": "CinematicPhotoStudio/2.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise RuntimeError("음악 검색 응답이 너무 큽니다.")
    check_cancel(cancel)
    return json.loads(data)

def automatic_music(workdir, cancel, log):
    log("Internet Archive에서 CC0 배경음악을 검색합니다.")
    query = (
        'mediatype:audio AND (ambient OR instrumental OR piano) '
        'AND licenseurl:"https://creativecommons.org/publicdomain/zero/1.0/"'
    )
    params = urllib.parse.urlencode({
        "q": query,
        "fl[]": "identifier",
        "rows": 12,
        "output": "json",
    })
    result = read_json(
        "https://archive.org/advancedsearch.php?" + params, cancel
    )
    for item in result.get("response", {}).get("docs", []):
        check_cancel(cancel)
        identifier = item.get("identifier")
        if not identifier:
            continue
        try:
            metadata = read_json(
                "https://archive.org/metadata/"
                + urllib.parse.quote(identifier, safe=""),
                cancel,
            )
            license_value = metadata.get("metadata", {}).get("licenseurl", [])
            licenses = (
                license_value if isinstance(license_value, list)
                else [license_value]
            )
            if not any(
                str(value).replace("http://", "https://").rstrip("/")
                == "https://creativecommons.org/publicdomain/zero/1.0"
                for value in licenses
            ):
                continue
            candidates = []
            for entry in metadata.get("files", []):
                name = entry.get("name", "")
                try:
                    size = int(entry.get("size", 0))
                except (TypeError, ValueError):
                    continue
                if name.lower().endswith(".mp3") and 0 < size <= 50_000_000:
                    candidates.append((size, name))
            if not candidates:
                continue
            _, filename = min(candidates)
            url = (
                "https://archive.org/download/"
                + urllib.parse.quote(identifier, safe="")
                + "/" + urllib.parse.quote(filename, safe="/")
            )
            target = Path(workdir) / "automatic_music.mp3"
            request = urllib.request.Request(
                url, headers={"User-Agent": "CinematicPhotoStudio/2.0"}
            )
            downloaded = 0
            with urllib.request.urlopen(request, timeout=20) as response:
                with target.open("wb") as output:
                    while True:
                        check_cancel(cancel)
                        block = response.read(128 * 1024)
                        if not block:
                            break
                        downloaded += len(block)
                        if downloaded > 50_000_000:
                            raise RuntimeError("음원 다운로드 용량 제한을 넘었습니다.")
                        output.write(block)
            source = (
                f"Source: https://archive.org/details/{identifier}\n"
                f"File: {filename}\n"
                "Declared license: CC0 1.0\n"
                "https://creativecommons.org/publicdomain/zero/1.0/\n"
                "License information is supplied by the archive uploader.\n"
            )
            return target, source
        except Cancelled:
            raise
        except Exception:
            continue
    raise RuntimeError(
        "자동 음악을 다운로드하지 못했습니다.\n"
        "직접 음악 파일을 선택하거나 음악 없이 다시 실행하세요."
    )

# ---------------------------------------------------------------------------
# 서브픽셀(Sub-Pixel) 무진동 카메라 엔진 & FFmpeg 파이프라인
# ---------------------------------------------------------------------------
def compute_subpixel_camera_box(
    scene_index: int,
    frame_idx: int,
    total_frames: int,
    is_intro: bool = False,
) -> tuple[float, float, float, float]:
    """
    FFmpeg zoompan 필터의 정수 픽셀 반올림 떨림(Jitter)을 완전히 제거하기 위해
    64비트 부동소수점(Sub-pixel) 정밀도로 매 프레임의 카메라 크롭 영역(x0, y0, x1, y1)을 계산합니다.
    """
    u = frame_idx / max(total_frames - 1, 1)
    # 부드러운 가감속을 섞은 사인-선형 하이브리드 보간 (시작과 끝이 매끄럽고 중간 이동이 일정함)
    s = 0.45 * u + 0.55 * (0.5 - 0.5 * math.cos(math.pi * u))
    if is_intro:
        z = 1.08 + 0.07 * s
        dx = 0.0
        dy = 0.0
    else:
        mode = scene_index % 4
        if mode == 0:
            # 부드러운 줌인 + 우측 패닝
            z = 1.08 + 0.08 * s
            dx = 54.0 * (s - 0.5)
            dy = -18.0 * (s - 0.5)
        elif mode == 1:
            # 와이드 줌아웃 리빌 + 좌측 패닝
            z = 1.16 - 0.08 * s
            dx = -54.0 * (s - 0.5)
            dy = 18.0 * (s - 0.5)
        elif mode == 2:
            # 시네마틱 수평 글라이드 + 완만한 줌
            z = 1.10 + 0.05 * s
            dx = 74.0 * (s - 0.5)
            dy = 0.0
        else:
            # 에디토리얼 대각선 드리프트
            z = 1.15 - 0.06 * s
            dx = -58.0 * (s - 0.5)
            dy = -22.0 * (s - 0.5)
    w = CANVAS_W / z
    h = CANVAS_H / z
    max_dx = max(0.0, (CANVAS_W - w) * 0.5 - 2.0)
    max_dy = max(0.0, (CANVAS_H - h) * 0.5 - 2.0)
    cx = CANVAS_W * 0.5 + max(-max_dx, min(max_dx, dx))
    cy = CANVAS_H * 0.5 + max(-max_dy, min(max_dy, dy))
    return (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)

def pick_transition(index: int, transition_style: str) -> str:
    if transition_style == "부드러운 디졸브 중심":
        pool = TRANSITIONS_SOFT
    elif transition_style == "다이내믹 슬라이드 & 줌":
        pool = TRANSITIONS_DYNAMIC
    else:
        pool = TRANSITIONS_CINEMATIC
    return pool[index % len(pool)]


def blend_transition_frames(
    frame_a: Image.Image,
    frame_b: Image.Image,
    p: float,
    kind: str,
) -> Image.Image:
    """
    두 씬의 프레임을 메모리 상에서 직접 부드럽게 블렌딩하여
    중간 임시 클립/청크 파일 생성 없이 심리스(Seamless) 화면 전환을 수행합니다.
    """
    e = 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))
    w, h = frame_a.size

    if kind in ("dissolve", "fade"):
        return Image.blend(frame_a, frame_b, e)

    if kind == "smoothleft":
        dx = int(round(w * e))
        out = Image.new("RGB", (w, h))
        out.paste(frame_a, (-dx, 0))
        out.paste(frame_b, (w - dx, 0))
        return out

    if kind == "smoothright":
        dx = int(round(w * e))
        out = Image.new("RGB", (w, h))
        out.paste(frame_a, (dx, 0))
        out.paste(frame_b, (dx - w, 0))
        return out

    if kind == "smoothup":
        dy = int(round(h * e))
        out = Image.new("RGB", (w, h))
        out.paste(frame_a, (0, -dy))
        out.paste(frame_b, (0, h - dy))
        return out

    if kind == "smoothdown":
        dy = int(round(h * e))
        out = Image.new("RGB", (w, h))
        out.paste(frame_a, (0, dy))
        out.paste(frame_b, (0, dy - h))
        return out

    if kind in ("circleopen", "circleclose"):
        sw, sh = w // 4, h // 4
        mask = Image.new("L", (sw, sh), 0 if kind == "circleopen" else 255)
        mdraw = ImageDraw.Draw(mask)
        max_r = math.hypot(sw * 0.5, sh * 0.5) * 1.08
        r = max_r * (e if kind == "circleopen" else (1.0 - e))
        cx, cy = sw * 0.5, sh * 0.5
        mdraw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=255 if kind == "circleopen" else 0,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=6)).resize(
            (w, h), Image.Resampling.BILINEAR
        )
        return Image.composite(frame_b, frame_a, mask)

    if kind in ("hlslice", "vuslice"):
        sw, sh = w // 4, h // 4
        mask = Image.new("L", (sw, sh), 0)
        mdraw = ImageDraw.Draw(mask)
        bars = 6
        if kind == "hlslice":
            bh = sh / bars
            for b in range(bars):
                delay = (b / bars) * 0.25
                lp = max(0.0, min(1.0, (e - delay) / 0.75))
                y0 = int(b * bh)
                y1 = int((b + 1) * bh) + 1
                mdraw.rectangle([0, y0, int(sw * lp), y1], fill=255)
        else:
            bw = sw / bars
            for b in range(bars):
                delay = (b / bars) * 0.25
                lp = max(0.0, min(1.0, (e - delay) / 0.75))
                x0 = int(b * bw)
                x1 = int((b + 1) * bw) + 1
                mdraw.rectangle([x0, 0, x1, int(sh * lp)], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(radius=4)).resize(
            (w, h), Image.Resampling.BILINEAR
        )
        return Image.composite(frame_b, frame_a, mask)

    # diagtl / diagbr
    sw, sh = w // 4, h // 4
    mask = Image.new("L", (sw, sh), 0)
    mdraw = ImageDraw.Draw(mask)
    reach = (sw + sh) * 1.15 * e
    if kind == "diagtl":
        mdraw.polygon([(0, 0), (reach, 0), (0, reach)], fill=255)
    else:
        mdraw.polygon([(sw, sh), (sw - reach, sh), (sw, sh - reach)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=8)).resize(
        (w, h), Image.Resampling.BILINEAR
    )
    return Image.composite(frame_b, frame_a, mask)


class PreparedSceneRenderer:
    """
    단일 씬의 마스터 플레이트와 하단 캡션을 보관하며
    요청된 프레임 번호(f_idx)에 해당하는 1920x1080 무진동 서브픽셀 프레임을 생성합니다.
    """

    def __init__(
        self,
        scene: SceneSpec,
        scene_idx: int,
        total_frames: int,
        fps: int,
        trans_dur: float,
        extra_dur: float,
        theme: str,
        caption_mode: str,
    ):
        self.scene = scene
        self.scene_idx = scene_idx
        self.total_frames = total_frames
        self.fps = fps
        self.plate_img, self.accent = render_scene_plate(scene, theme)
        self.cap_img = render_lower_third_caption(scene, caption_mode, self.accent)

        clip_dur = total_frames / fps
        self.in_start = (trans_dur + 0.12) if scene_idx > 0 else 0.35
        self.in_dur = 0.45
        self.out_start = max(self.in_start + self.in_dur + 0.2, clip_dur - extra_dur - 0.42)
        self.out_dur = 0.38

        self.cap_rgb = None
        self.cap_alpha = None
        self.cap_rest_rgb = None
        self.cap_rest_mask = None
        self.cap_w, self.cap_h = 0, 0
        if self.cap_img is not None:
            self.cap_rgb = self.cap_img.convert("RGB")
            self.cap_alpha = self.cap_img.getchannel("A")
            self.cap_w, self.cap_h = self.cap_img.size
            out_h = self.cap_h - 32
            rest_box = (0.0, 28.0, float(self.cap_w), 28.0 + float(out_h))
            self.cap_rest_rgb = self.cap_rgb.resize(
                (self.cap_w, out_h), resample=Image.Resampling.BILINEAR, box=rest_box
            )
            self.cap_rest_mask = self.cap_alpha.resize(
                (self.cap_w, out_h), resample=Image.Resampling.BILINEAR, box=rest_box
            )

        self.is_cinema_full = scene.kind == "cinema_full"
        self.bar_h = 48

    def render_frame(self, f_idx: int) -> Image.Image:
        box = compute_subpixel_camera_box(
            self.scene_idx, f_idx, self.total_frames, is_intro=(self.scene.kind == "intro")
        )
        frame = self.plate_img.resize(
            (WIDTH, HEIGHT),
            resample=Image.Resampling.BICUBIC,
            box=box,
        )

        if self.is_cinema_full:
            fdraw = ImageDraw.Draw(frame)
            fdraw.rectangle([0, 0, WIDTH, self.bar_h], fill=(8, 10, 16))
            fdraw.rectangle([0, HEIGHT - self.bar_h, WIDTH, HEIGHT], fill=(8, 10, 16))
            fdraw.line([(0, self.bar_h), (WIDTH, self.bar_h)], fill=self.accent, width=2)
            fdraw.line(
                [(0, HEIGHT - self.bar_h), (WIDTH, HEIGHT - self.bar_h)],
                fill=self.accent,
                width=2,
            )

        if self.cap_img is not None:
            sec = f_idx / self.fps
            if self.in_start <= sec <= (self.out_start + self.out_dur):
                if sec < (self.in_start + self.in_dur):
                    p = max(0.0, min(1.0, (sec - self.in_start) / self.in_dur))
                    ease = 1.0 - ((1.0 - p) ** 3)
                    alpha_mul = ease
                    y_float = 26.0 * (1.0 - ease)
                elif sec > self.out_start:
                    p = max(0.0, min(1.0, (sec - self.out_start) / self.out_dur))
                    alpha_mul = 1.0 - p
                    y_float = 0.0
                else:
                    alpha_mul = 1.0
                    y_float = 0.0

                if alpha_mul > 0.01:
                    if y_float <= 0.02 and alpha_mul >= 0.995:
                        frame.paste(self.cap_rest_rgb, (68, 882), self.cap_rest_mask)
                    else:
                        out_h = self.cap_h - 32
                        top_y = 28.0 - y_float
                        sub_box = (0.0, top_y, float(self.cap_w), top_y + float(out_h))
                        cur_rgb = self.cap_rgb.resize(
                            (self.cap_w, out_h), resample=Image.Resampling.BILINEAR, box=sub_box
                        )
                        cur_mask = self.cap_alpha.resize(
                            (self.cap_w, out_h), resample=Image.Resampling.BILINEAR, box=sub_box
                        )
                        if alpha_mul < 0.995:
                            cur_mask = ImageEnhance.Brightness(cur_mask).enhance(alpha_mul)
                        frame.paste(cur_rgb, (68, 882), cur_mask)

        return frame


def run_ffmpeg_cmd(
    arguments: list[str],
    workdir: Path,
    duration: float,
    cancel_event: threading.Event | None,
    progress_cb=None,
    start_prog: float = 0.0,
    span_prog: float = 0.0,
):
    check_cancel(cancel_event)
    error_file = Path(workdir) / "ffmpeg_error.log"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-nostats", "-progress", "pipe:1", "-y",
    ] + arguments

    with error_file.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(
            command,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        finished = threading.Event()

        def watch_cancel():
            while not finished.wait(0.2):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch_cancel, daemon=True)
        watcher.start()
        try:
            for line in process.stdout:
                if line.startswith("out_time_us=") and progress_cb is not None:
                    try:
                        elapsed = int(line.split("=", 1)[1]) / 1_000_000
                        fraction = min(1.0, max(0.0, elapsed / max(duration, 0.001)))
                        progress_cb(start_prog + span_prog * fraction)
                    except ValueError:
                        pass
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            finished.set()
            watcher.join()

    check_cancel(cancel_event)
    if code:
        details = error_file.read_text(encoding="utf-8", errors="replace")[-5000:]
        raise RuntimeError("FFmpeg 처리 실패:\n" + details)


def render_cinematic_video(
    folder: Path,
    output: Path,
    order: str = "촬영 날짜",
    seconds: float = 4.0,
    music_mode: str = "자동 검색",
    music_file: str = "",
    preview: bool = False,
    theme: str = "시네마틱 마스터피스 (추천)",
    smart_collage: bool = True,
    caption_mode: str = "행사(폴더)명 + 촬영 날짜",
    transition_style: str = "다채로운 시네마틱 전환 (추천)",
    include_intro: bool = True,
    custom_title: str = "",
    fps: int = DEFAULT_FPS,
    cancel_event: threading.Event | None = None,
    log_cb=None,
    progress_cb=None,
):
    def log(msg: str):
        if log_cb is not None:
            log_cb(msg)

    def prog(val: float):
        if progress_cb is not None:
            progress_cb(val)

    log("사진 목록과 촬영 정보(EXIF·폴더 날짜)를 분석합니다.")
    photos = find_photos_with_meta(folder, order)
    check_cancel(cancel_event)
    if preview:
        photos = photos[:10]
    if not photos:
        raise RuntimeError("선택한 폴더에 JPG, JPEG, PNG 사진이 없습니다.")

    log("사진 무결성 및 해상도 비율(가로/세로)을 검사합니다.")
    readable_photos: list[PhotoMeta] = []
    for index, meta in enumerate(photos):
        check_cancel(cancel_event)
        try:
            with Image.open(meta.path) as image:
                image.load()
                transposed = ImageOps.exif_transpose(image)
                meta.width, meta.height = transposed.size
            readable_photos.append(meta)
        except (OSError, ValueError, SyntaxError) as error:
            log(f"읽을 수 없어 제외한 사진: {meta.path.name} ({error})")
        prog(5.0 * (index + 1) / len(photos))

    skipped = len(photos) - len(readable_photos)
    photos = readable_photos
    if not photos:
        raise RuntimeError("읽을 수 있는 사진이 없습니다. 원본 사진을 확인하세요.")

    scenes = build_scenes(
        photos,
        theme=theme,
        smart_collage=smart_collage,
        include_intro=include_intro,
        custom_title=custom_title,
    )
    num_scenes = len(scenes)
    log(
        f"검사 완료: 사진 {len(photos)}장 (제외 {skipped}장) → "
        f"총 {num_scenes}개 시네마틱 씬으로 구성했습니다."
    )

    trans_frames = (
        max(6, round(min(0.75, max(0.35, seconds * 0.20)) * fps))
        if num_scenes > 1
        else 0
    )
    trans_dur = trans_frames / fps

    # 각 씬의 프레임 수 산출
    scene_frames_list: list[int] = []
    for idx, sc in enumerate(scenes):
        base_dur = max(3.0, min(3.8, seconds * 0.85)) if sc.kind == "intro" else seconds
        extra_f = trans_frames if idx < num_scenes - 1 else 0
        f_cnt = max(round(base_dur * fps) + extra_f, trans_frames * 2 + 12)
        scene_frames_list.append(f_cnt)

    total_output_frames = sum(scene_frames_list) - max(0, num_scenes - 1) * trans_frames
    final_duration = total_output_frames / fps

    with tempfile.TemporaryDirectory(prefix=".cinema_video_", dir=output.parent) as temporary:
        workdir = Path(temporary)
        music_path = None
        music_source = None

        if music_mode == "자동 검색":
            try:
                music_path, music_source = automatic_music(workdir, cancel_event, log)
            except Cancelled:
                raise
            except Exception as error:
                music_path = None
                music_source = None
                log(f"자동 음원 검색 실패: {error}")
                log("배경음악 없이 영상 제작을 계속합니다.")
        elif music_mode == "직접 선택":
            music_path = Path(music_file).resolve()

        prog(7.0)

        # 단일 FFmpeg 파이프 프로세스로 모든 씬과 화면 전환을 한 번에 스트리밍 인코딩합니다.
        # (중간 임시 클립/청크 파일을 전혀 생성하지 않으므로 사진이 수백 장이어도 빠르고 안정적입니다.)
        merged_video_path = workdir / "merged_video.mp4"
        error_file = workdir / "ffmpeg_stream_error.log"
        stream_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(fps),
            "-i", "pipe:0",
            "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-g", str(fps),
            "-pix_fmt", "yuv420p",
            "-threads", "0",
            str(merged_video_path),
        ]

        fade_in_frames = min(round(0.55 * fps), max(1, total_output_frames // 6))
        fade_out_frames = min(round(0.65 * fps), max(1, total_output_frames // 6))
        written_frames = 0

        def write_output_frame(proc, img: Image.Image):
            nonlocal written_frames
            if written_frames < fade_in_frames:
                factor = (written_frames + 1) / fade_in_frames
                img = ImageEnhance.Brightness(img).enhance(factor)
            elif written_frames >= total_output_frames - fade_out_frames:
                rem_f = total_output_frames - 1 - written_frames
                factor = max(0.0, rem_f / fade_out_frames)
                img = ImageEnhance.Brightness(img).enhance(factor)

            proc.stdin.write(img.tobytes())
            written_frames += 1
            if written_frames % 10 == 0 or written_frames == total_output_frames:
                prog(7.0 + 85.0 * (written_frames / max(1, total_output_frames)))

        with error_file.open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                stream_cmd,
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                cur_renderer = None
                for idx, scene in enumerate(scenes):
                    check_cancel(cancel_event)
                    if scene.kind == "intro":
                        scene_desc = f"오프닝 타이틀 ({scene.main_title})"
                    else:
                        names = ", ".join(p.path.name for p in scene.photos)
                        scene_desc = f"[{scene.kind}] {names}"

                    log(f"씬 렌더링 {idx + 1}/{num_scenes} (서브픽셀 무진동·심리스 전환): {scene_desc}")

                    if cur_renderer is None:
                        extra_d = trans_dur if idx < num_scenes - 1 else 0.0
                        cur_renderer = PreparedSceneRenderer(
                            scene=scene,
                            scene_idx=idx,
                            total_frames=scene_frames_list[idx],
                            fps=fps,
                            trans_dur=trans_dur,
                            extra_dur=extra_d,
                            theme=theme,
                            caption_mode=caption_mode,
                        )

                    start_f = 0 if idx == 0 else trans_frames
                    end_solo_f = scene_frames_list[idx] - (
                        trans_frames if idx < num_scenes - 1 else 0
                    )

                    # 1. 현재 씬 단독 구간 프레임 출력
                    for f_idx in range(start_f, end_solo_f):
                        if f_idx % 6 == 0:
                            check_cancel(cancel_event)
                        frame = cur_renderer.render_frame(f_idx)
                        write_output_frame(process, frame)

                    # 2. 다음 씬과의 심리스 전환(Transition) 구간 프레임 블렌딩 출력
                    if idx < num_scenes - 1 and trans_frames > 0:
                        next_scene = scenes[idx + 1]
                        next_extra_d = trans_dur if (idx + 1) < num_scenes - 1 else 0.0
                        next_renderer = PreparedSceneRenderer(
                            scene=next_scene,
                            scene_idx=idx + 1,
                            total_frames=scene_frames_list[idx + 1],
                            fps=fps,
                            trans_dur=trans_dur,
                            extra_dur=next_extra_d,
                            theme=theme,
                            caption_mode=caption_mode,
                        )
                        t_kind = pick_transition(idx, transition_style)
                        for t_step in range(trans_frames):
                            if t_step % 4 == 0:
                                check_cancel(cancel_event)
                            p = (t_step + 1) / (trans_frames + 1)
                            frame_a = cur_renderer.render_frame(end_solo_f + t_step)
                            frame_b = next_renderer.render_frame(t_step)
                            blended = blend_transition_frames(frame_a, frame_b, p, t_kind)
                            write_output_frame(process, blended)

                        cur_renderer = next_renderer
                    else:
                        cur_renderer = None

                process.stdin.close()
                process.stdin = None
                code = process.wait()
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait()

        check_cancel(cancel_event)
        if code:
            details = error_file.read_text(encoding="utf-8", errors="replace")[-5000:]
            raise RuntimeError("FFmpeg 영상 스트리밍 인코딩 실패:\n" + details)

        log("최종 마스터링 및 배경음악 페이드 믹싱을 적용합니다.")
        temporary_output = workdir / "result.mp4"
        arguments = ["-i", str(merged_video_path)]
        if music_path:
            audio_fade = min(3.0, final_duration / 4)
            arguments += [
                "-stream_loop", "-1", "-i", str(music_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-af",
                f"afade=t=in:st=0:d={audio_fade:.3f},"
                f"afade=t=out:st={max(0.0, final_duration - audio_fade):.3f}:d={audio_fade:.3f}",
                "-c:a", "aac", "-b:a", "192k",
            ]
        else:
            arguments += ["-map", "0:v:0", "-c:v", "copy", "-an"]

        arguments += [
            "-t", f"{final_duration:.6f}",
            "-movflags", "+faststart",
            str(temporary_output),
        ]
        run_ffmpeg_cmd(
            arguments,
            workdir,
            final_duration,
            cancel_event,
            progress_cb=prog,
            start_prog=92.0,
            span_prog=8.0,
        )
        check_cancel(cancel_event)

        if output.exists():
            raise RuntimeError("출력 파일이 이미 존재합니다. 다른 이름을 선택하세요.")
        shutil.move(str(temporary_output), str(output))

        if music_source:
            source_file = output.with_suffix(".music.txt")
            try:
                with source_file.open("x", encoding="utf-8") as file:
                    file.write(music_source)
            except OSError as error:
                log(f"음악 출처 파일 저장 실패: {error}")
                log(music_source)

    prog(100.0)
    return output


# ---------------------------------------------------------------------------
# 모던 스튜디오 Tkinter GUI 애플리케이션
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("시네마틱 포토 스튜디오 Pro — 사진 → 고품격 MP4")
        self.root.geometry("820x780")
        self.root.minsize(760, 720)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.folder = tk.StringVar()
        self.output = tk.StringVar()
        self.order = tk.StringVar(value="촬영 날짜")
        self.seconds = tk.StringVar(value="4.0")
        self.theme = tk.StringVar(value="시네마틱 마스터피스 (추천)")
        self.smart_collage = tk.BooleanVar(value=True)
        self.transition_style = tk.StringVar(value="다채로운 시네마틱 전환 (추천)")
        self.caption_mode = tk.StringVar(value="행사(폴더)명 + 촬영 날짜")
        self.custom_title = tk.StringVar(value="")
        self.include_intro = tk.BooleanVar(value=True)
        self.music_mode = tk.StringVar(value="자동 검색")
        self.music_file = tk.StringVar()
        self.estimate = tk.StringVar(value="사진 폴더를 선택하면 예상 씬 구성과 영상 길이를 분석합니다.")
        self._configure_styles()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg_main = "#141824"
        bg_card = "#1E2436"
        fg_text = "#F3F4F6"
        fg_muted = "#9CA3AF"
        accent = "#F59E0B"
        self.root.configure(bg=bg_main)
        style.configure(".", background=bg_main, foreground=fg_text, font=("Malgun Gothic", 10))
        style.configure("TFrame", background=bg_main)
        style.configure("Card.TLabelframe", background=bg_card, foreground=accent, borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", background=bg_card, foreground=accent, font=("Malgun Gothic", 10, "bold"))
        style.configure("CardInner.TFrame", background=bg_card)
        style.configure("Card.TLabel", background=bg_card, foreground=fg_text)
        style.configure("Muted.TLabel", background=bg_card, foreground=fg_muted, font=("Malgun Gothic", 9))
        style.configure("Header.TLabel", background=bg_main, foreground="#FFFFFF", font=("Malgun Gothic", 15, "bold"))
        style.configure("SubHeader.TLabel", background=bg_main, foreground=fg_muted, font=("Malgun Gothic", 9))
        style.configure("Estimate.TLabel", background=bg_card, foreground="#38BDF8", font=("Malgun Gothic", 10, "bold"))
        style.configure("Card.TCheckbutton", background=bg_card, foreground=fg_text)
        style.map("Card.TCheckbutton", background=[("active", bg_card)])
        style.configure("Accent.TButton", background=accent, foreground="#111827", font=("Malgun Gothic", 10, "bold"), padding=(14, 7))
        style.map("Accent.TButton", background=[("active", "#FBBF24"), ("disabled", "#374151")], foreground=[("disabled", "#9CA3AF")])
        style.configure("Secondary.TButton", background="#2D3748", foreground=fg_text, padding=(12, 7))
        style.map("Secondary.TButton", background=[("active", "#4A5568"), ("disabled", "#1F2937")], foreground=[("disabled", "#6B7280")])
        style.configure("TEntry", fieldbackground="#0F131D", foreground=fg_text, insertcolor=fg_text)
        style.configure("TCombobox", fieldbackground="#0F131D", background="#2D3748", foreground=fg_text)
        style.map("TCombobox", fieldbackground=[("readonly", "#0F131D")], foreground=[("readonly", fg_text)])
        style.configure("Horizontal.TProgressbar", troughcolor="#0F131D", background=accent, thickness=14)
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        header_frame = ttk.Frame(outer)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header_frame, text="✦ 시네마틱 포토 스튜디오 Pro", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header_frame,
            text="서브픽셀 무진동 카메라 엔진 · 스마트 콜라주 · 3D 입체 액자 & 폴라로이드 · 애니메이션 하단 캡션",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        io_card = ttk.LabelFrame(outer, text=" 📁 사진 폴더 및 저장 경로 ", style="Card.TLabelframe", padding=12)
        io_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        io_card.columnconfigure(1, weight=1)
        self._path_row(io_card, 0, "사진 폴더", self.folder, self.choose_folder)
        self._path_row(io_card, 1, "MP4 저장 위치", self.output, self.choose_output)
        ttk.Label(io_card, textvariable=self.estimate, style="Estimate.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        style_card = ttk.LabelFrame(outer, text=" 🎨 비주얼 연출 & 스마트 레이아웃 ", style="Card.TLabelframe", padding=12)
        style_card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        style_card.columnconfigure(1, weight=1)
        style_card.columnconfigure(3, weight=1)
        ttk.Label(style_card, text="연출 테마", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        theme_box = ttk.Combobox(
            style_card,
            textvariable=self.theme,
            values=[
                "시네마틱 마스터피스 (추천)",
                "모던 3D 갤러리",
                "감성 폴라로이드 & 필름",
                "다큐멘터리 풀스크린",
            ],
            state="readonly",
        )
        theme_box.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=5)
        theme_box.bind("<<ComboboxSelected>>", lambda _: self.update_estimate())
        ttk.Label(style_card, text="화면 전환 효과", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 10), pady=5)
        ttk.Combobox(
            style_card,
            textvariable=self.transition_style,
            values=[
                "다채로운 시네마틱 전환 (추천)",
                "부드러운 디졸브 중심",
                "다이내믹 슬라이드 & 줌",
            ],
            state="readonly",
        ).grid(row=0, column=3, sticky="ew", pady=5)
        ttk.Label(style_card, text="사진 정렬", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        sort_box = ttk.Combobox(
            style_card,
            textvariable=self.order,
            values=["촬영 날짜", "파일명"],
            state="readonly",
        )
        sort_box.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=5)
        sort_box.bind("<<ComboboxSelected>>", lambda _: self.update_estimate())
        ttk.Label(style_card, text="씬당 시간(초)", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(style_card, textvariable=self.seconds).grid(row=1, column=3, sticky="ew", pady=5)
        self.seconds.trace_add("write", lambda *_: self.update_estimate())
        cb_frame = ttk.Frame(style_card, style="CardInner.TFrame")
        cb_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            cb_frame,
            text="스마트 콜라주 활성화 (연속된 세로 사진 등을 2~3장 화보 레이아웃으로 자동 조합)",
            variable=self.smart_collage,
            style="Card.TCheckbutton",
            command=self.update_estimate,
        ).pack(side="left", padx=(0, 20))
        cap_card = ttk.LabelFrame(outer, text=" 📝 하단 캡션(자막) 및 오프닝 타이틀 ", style="Card.TLabelframe", padding=12)
        cap_card.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        cap_card.columnconfigure(1, weight=1)
        cap_card.columnconfigure(3, weight=1)
        ttk.Label(cap_card, text="하단 캡션 스타일", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Combobox(
            cap_card,
            textvariable=self.caption_mode,
            values=[
                "행사(폴더)명 + 촬영 날짜",
                "폴더명 + 파일명",
                "촬영 날짜만",
                "표시 안 함",
            ],
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=5)
        ttk.Label(cap_card, text="커스텀 대표 제목", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(cap_card, textvariable=self.custom_title).grid(row=0, column=3, sticky="ew", pady=5)
        ttk.Checkbutton(
            cap_card,
            text="오프닝 인트로 타이틀 카드 자동 생성 (베스트 사진 콜라주 + 행사 제목·기간 표시)",
            variable=self.include_intro,
            style="Card.TCheckbutton",
            command=self.update_estimate,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 2))
        ttk.Label(
            cap_card,
            text="💡 커스텀 대표 제목을 비워두면 [YYYY-MM-DD] 행사 폴더명과 EXIF 촬영일자를 자동으로 인식해 캡션을 띄웁니다.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w")
        music_card = ttk.LabelFrame(outer, text=" 🎵 배경음악 설정 ", style="Card.TLabelframe", padding=12)
        music_card.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        music_card.columnconfigure(1, weight=1)
        ttk.Label(music_card, text="배경음악 모드", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Combobox(
            music_card,
            textvariable=self.music_mode,
            values=["자동 검색", "직접 선택", "없음"],
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", pady=5)
        self._path_row(music_card, 1, "음악 파일 선택", self.music_file, self.choose_music)
        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, sticky="ew", pady=(2, 10))
        self.preview_button = ttk.Button(
            buttons, text="⚡ 10장 빠른 미리보기 생성", style="Secondary.TButton", command=lambda: self.start(True)
        )
        self.preview_button.pack(side="left", padx=(0, 10))
        self.render_button = ttk.Button(
            buttons, text="🎬 전체 시네마틱 영상 제작 시작", style="Accent.TButton", command=lambda: self.start(False)
        )
        self.render_button.pack(side="left", padx=(0, 10))
        self.cancel_button = ttk.Button(
            buttons, text="취소", style="Secondary.TButton", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")
        self.progress = ttk.Progressbar(outer, maximum=100, style="Horizontal.TProgressbar")
        self.progress.grid(row=6, column=0, sticky="ew", pady=(0, 8))
        self.log_box = tk.Text(
            outer,
            height=8,
            wrap="word",
            state="disabled",
            bg="#0F131D",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            relief="flat",
            padx=10,
            pady=8,
            font=("Consolas", 9),
        )
        self.log_box.grid(row=7, column=0, sticky="nsew")
        outer.rowconfigure(7, weight=1)
    def _path_row(self, parent, row, label, variable, command):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5
        )
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="찾기...", style="Secondary.TButton", command=command).grid(
            row=row, column=2, padx=(8, 0), pady=5
        )
    def choose_folder(self):
        selected = filedialog.askdirectory()
        if selected:
            self.folder.set(selected)
            self.update_estimate()
    def choose_output(self):
        selected = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 영상", "*.mp4")],
        )
        if selected:
            self.output.set(selected)
    def choose_music(self):
        selected = filedialog.askopenfilename(
            filetypes=[
                ("음악", "*.mp3 *.wav *.m4a *.flac *.ogg"),
                ("모든 파일", "*.*"),
            ]
        )
        if selected:
            self.music_file.set(selected)
            self.music_mode.set("직접 선택")
    def update_estimate(self):
        try:
            seconds = float(self.seconds.get())
            if not math.isfinite(seconds) or not 1.5 <= seconds <= 60:
                raise ValueError()
            folder = Path(self.folder.get())
            if not self.folder.get() or not folder.is_dir():
                raise ValueError()
            photos = find_photos_with_meta(folder, self.order.get())
            count = len(photos)
            if count == 0:
                self.estimate.set("선택한 폴더에 이미지 파일(JPG, PNG)이 없습니다.")
                return
            est_scenes = max(1, round(count * 0.78)) if self.smart_collage.get() else count
            if self.include_intro.get():
                est_scenes += 1
            duration = est_scenes * seconds
            self.estimate.set(
                f"✦ 총 사진 {count}장 감지  ·  예상 시네마틱 씬 약 {est_scenes}개  ·  "
                f"예상 재생 시간 {int(duration // 60)}분 {duration % 60:.1f}초"
            )
        except (ValueError, OSError):
            self.estimate.set("폴더 경로와 씬당 표시 시간(1.5~60초)을 확인하세요.")
    def log(self, text):
        self.events.put(("log", text))
    def start(self, preview):
        if self.running:
            return
        try:
            if not shutil.which("ffmpeg"):
                raise ValueError("FFmpeg를 설치하고 시스템 Path에 추가하세요.")
            folder = Path(self.folder.get())
            if not self.folder.get() or not folder.is_dir():
                raise ValueError("사진 폴더를 선택하세요.")
            seconds = float(self.seconds.get())
            if not math.isfinite(seconds) or not 1.5 <= seconds <= 60:
                raise ValueError("씬당 시간은 1.5~60초 사이로 입력하세요.")
            if not self.output.get():
                raise ValueError("MP4 저장 위치를 선택하세요.")
            output = Path(self.output.get()).resolve()
            if output.suffix.lower() != ".mp4":
                raise ValueError("저장 파일 확장자는 .mp4여야 합니다.")
            if not output.parent.is_dir():
                raise ValueError("저장할 폴더가 존재하지 않습니다.")
            if preview:
                output = output.with_name(output.stem + "_preview.mp4")
            if output.exists():
                raise ValueError(f"같은 이름의 파일이 이미 존재합니다.\n{output}")
            music_mode = self.music_mode.get()
            music_file = self.music_file.get()
            if music_mode == "직접 선택" and not Path(music_file).is_file():
                raise ValueError("배경음악 파일을 선택하세요.")
        except (ValueError, OSError) as error:
            messagebox.showerror("입력 확인", str(error))
            return
        self.running = True
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.preview_button["state"] = "disabled"
        self.render_button["state"] = "disabled"
        self.cancel_button["state"] = "normal"
        kwargs = dict(
            folder=folder,
            output=output,
            order=self.order.get(),
            seconds=seconds,
            music_mode=music_mode,
            music_file=music_file,
            preview=preview,
            theme=self.theme.get(),
            smart_collage=self.smart_collage.get(),
            caption_mode=self.caption_mode.get(),
            transition_style=self.transition_style.get(),
            include_intro=self.include_intro.get(),
            custom_title=self.custom_title.get(),
            fps=DEFAULT_FPS,
            cancel_event=self.cancel_event,
            log_cb=self.log,
            progress_cb=lambda v: self.events.put(("progress", v)),
        )
        threading.Thread(target=self._worker_wrapper, kwargs=kwargs, daemon=True).start()
    def _worker_wrapper(self, **kwargs):
        try:
            out = render_cinematic_video(**kwargs)
            self.events.put(("done", str(out)))
        except Cancelled:
            self.events.put(("cancelled", "작업을 취소했습니다. 원본 사진은 안전하게 보존됩니다."))
        except Exception as error:
            self.events.put(("error", str(error)))
    def cancel(self):
        if self.running:
            self.cancel_event.set()
            self.cancel_button["state"] = "disabled"
            self.log("취소 요청됨: 현재 진행 중인 FFmpeg 프로세스를 안전하게 종료합니다...")
    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress["value"] = value
                elif kind == "log":
                    self.log_box["state"] = "normal"
                    self.log_box.insert("end", value + "\n")
                    self.log_box.see("end")
                    self.log_box["state"] = "disabled"
                elif kind in ("done", "cancelled", "error"):
                    self.running = False
                    self.preview_button["state"] = "normal"
                    self.render_button["state"] = "normal"
                    self.cancel_button["state"] = "disabled"
                    if kind == "done":
                        self.progress["value"] = 100
                        messagebox.showinfo("제작 완료", f"시네마틱 MP4 영상을 완성했습니다!\n\n{value}")
                    elif kind == "error":
                        messagebox.showerror("오류", value)
                    else:
                        messagebox.showinfo("취소됨", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)
    def close(self):
        if self.running:
            self.cancel()
            messagebox.showinfo("취소 중", "작업 취소가 끝나면 창을 닫아주세요.")
            return
        self.root.destroy()

def main():
    parser = argparse.ArgumentParser(description="시네마틱 포토 스튜디오 Pro (사진 → MP4)")
    parser.add_argument("--cli", action="store_true", help="GUI 없이 커맨드라인 모드로 실행")
    parser.add_argument("--folder", type=str, help="입력 사진 폴더 경로")
    parser.add_argument("--output", type=str, help="출력 MP4 파일 경로")
    parser.add_argument("--seconds", type=float, default=3.5, help="씬당 표시 시간(초)")
    parser.add_argument("--theme", type=str, default="시네마틱 마스터피스 (추천)", help="연출 테마")
    parser.add_argument("--music-mode", type=str, default="없음", help="배경음악 모드 (자동 검색/직접 선택/없음)")
    parser.add_argument("--title", type=str, default="", help="커스텀 대표 타이틀")
    args = parser.parse_args()
    if args.cli:
        if not args.folder or not args.output:
            parser.error("--cli 모드에서는 --folder와 --output 인자가 필요합니다.")
        out_path = Path(args.output).resolve()
        if out_path.exists():
            out_path.unlink()
        render_cinematic_video(
            folder=Path(args.folder).resolve(),
            output=out_path,
            seconds=args.seconds,
            music_mode=args.music_mode,
            theme=args.theme,
            custom_title=args.title,
            log_cb=lambda msg: print(f"[LOG] {msg}", flush=True),
            progress_cb=lambda p: None,
        )
        print(f"완료: {out_path}")
        return
    if not HAS_TK:
        raise RuntimeError(
            "현재 Python 환경에 tkinter 모듈이 설치되어 있지 않습니다. "
            "GUI를 실행하려면 python3-tk를 설치하거나 --cli 옵션을 사용하세요."
        )
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()