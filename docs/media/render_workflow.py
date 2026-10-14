"""Render an illustrative CLI workflow from bundled fonts and sample report.

The example price stays fixed. This is not a recording of an application UI.
No network or market database is used.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = HERE.parents[1] / "car_market/assets/fonts"
PAPER, INK, TEAL = "#F8F7F3", "#183A35", "#277B67"
MUTED, LINE, PALE = "#647A71", "#DCE5DE", "#EAF1EB"
WHITE, NIGHT, MINT = "#FFFFFF", "#152C27", "#BCEBD5"
DURATION_MS, FRAME_MS = 13200, 50
COMMAND = [
    "./start.sh research --make BMW " + chr(92),
    '  --model "218i Active Tourer" ' + chr(92),
    "  --country NL --max-pages 15",
]


@lru_cache(maxsize=96)
def font(size: int, bold: bool = False):
    return ImageFont.truetype(
        str(FONTS / ("Lato-Bold.ttf" if bold else "Lato-Regular.ttf")), size
    )


def ease(value):
    value = min(1, max(0, value))
    return value * value * (3 - 2 * value)


def put(draw, position, value, size, *, bold=False, fill=INK, anchor=None):
    draw.text(position, value, font=font(size, bold), fill=fill, anchor=anchor)


def car(draw, x, y, *, scale=1.0, color=TEAL):
    def xy(points):
        return [(round(x + px * scale), round(y + py * scale)) for px, py in points]

    draw.line(
        xy(
            [
                (0, 48),
                (14, 27),
                (43, 23),
                (61, 0),
                (119, 0),
                (149, 23),
                (174, 30),
                (184, 48),
            ]
        ),
        fill=color,
        width=max(2, round(4 * scale)),
        joint="curve",
    )
    draw.rounded_rectangle(
        (*xy([(0, 44)])[0], *xy([(184, 70)])[0]),
        radius=round(11 * scale),
        outline=color,
        width=max(2, round(4 * scale)),
    )
    for px in (37, 145):
        draw.ellipse(
            (*xy([(px - 13, 58)])[0], *xy([(px + 13, 84)])[0]),
            fill=PAPER,
            outline=color,
            width=max(2, round(4 * scale)),
        )
    draw.line(
        xy([(69, 7), (112, 7), (136, 25), (55, 25), (69, 7)]),
        fill=color,
        width=max(2, round(3 * scale)),
    )


@lru_cache(maxsize=4)
def report_crop(width, height, charts=False):
    source = HERE / "sample-report.png"
    if not source.exists():
        raise FileNotFoundError(
            "Render docs/media/render_sample_report.py before the workflow."
        )
    with Image.open(source) as image:
        image = image.convert("RGB")
        ratio = image.width / 2800
        box = (80, 1400, 2720, 3070) if charts else (80, 100, 2720, 1770)
        crop = image.crop(tuple(round(v * ratio) for v in box))
        crop.thumbnail((width, height), Image.Resampling.LANCZOS)
        return crop


def shell(width, height):
    mobile = width < 800
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    margin = 28 if mobile else 44
    car(draw, margin, 31, scale=0.30)
    put(draw, (margin + 69, 25), "CAR MARKET", 27 if mobile else 32, bold=True)
    put(
        draw,
        (width - margin, 31),
        "CLI + PDF",
        23 if mobile else 26,
        fill=TEAL,
        anchor="ra",
    )
    draw.line((margin, 87, width - margin, 87), fill=LINE, width=1)
    if mobile:
        put(draw, (margin, 111), "Car research,", 49, bold=True)
        put(draw, (margin, 170), "made readable.", 49, bold=True)
        put(draw, (margin, 239), "Netherlands + Germany", 28, fill=MUTED)
    else:
        put(draw, (margin, 112), "Car research, made readable.", 62, bold=True)
        put(draw, (margin, 191), "Netherlands + Germany", 32, fill=MUTED)
    return image


def stage(scene, progress, width, height):
    mobile = width < 800
    layer = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(layer)
    margin = 28 if mobile else 44
    top, bottom = (332, 700) if mobile else (305, 635)
    x1, x2 = margin, width - margin
    pad = 24 if mobile else 32
    inner = x1 + pad
    draw.rounded_rectangle((x1, top + 5, x2, bottom + 5), radius=21, fill=LINE)
    draw.rounded_rectangle(
        (x1, top, x2, bottom), radius=21, fill=NIGHT if scene == 0 else WHITE
    )
    if scene == 0:
        for i, color in enumerate(("#BE8271", "#D4B575", "#80B898")):
            draw.ellipse(
                (inner + i * 20, top + 24, inner + i * 20 + 9, top + 33), fill=color
            )
        put(
            draw,
            (inner + 81, top + 16),
            "YOUR SEARCH",
            22 if mobile else 25,
            fill="#A8C2B5",
            bold=True,
        )
        size, step = (25, 44) if mobile else (36, 52)
        typed = round(sum(map(len, COMMAND)) * ease((progress - 0.12) / 0.55))
        cursor = None
        y = top + 77
        for line in COMMAND:
            shown = line[:typed]
            put(draw, (inner, y), shown, size, fill=MINT)
            if typed < len(line) and cursor is None:
                cursor = (inner + draw.textlength(shown, font=font(size)), y)
            typed = max(0, typed - len(line))
            y += step
        if cursor and int(progress * 18) % 2 == 0:
            cx, cy = cursor
            draw.rectangle((cx + 3, cy + 4, cx + 6, cy + size + 1), fill=MINT)
        if progress > 0.72:
            put(
                draw,
                (inner, top + (252 if mobile else 259)),
                "Search, save, make a report.",
                26 if mobile else 31,
                fill="#A8C2B5",
            )
    elif scene == 1:
        put(
            draw,
            (inner, top + 22),
            "BMW 218i Active Tourer",
            31 if mobile else 43,
            bold=True,
        )
        put(
            draw,
            (inner, top + 69),
            "Example listing · Netherlands",
            24 if mobile else 29,
            fill=MUTED,
        )
        put(draw, (inner, top + 120), "€14,328", 70 if mobile else 79, bold=True)
        put(
            draw,
            (inner, top + 205),
            "Example asking price",
            25 if mobile else 28,
            fill=MUTED,
        )
        if mobile:
            put(draw, (inner, top + 259), "2018   ·   109,600 km", 29, bold=True)
            if progress > 0.30:
                draw.rounded_rectangle(
                    (inner, top + 309, inner + 274, top + 350),
                    radius=12,
                    fill="#F7EBDD",
                )
                put(
                    draw,
                    (inner + 13, top + 314),
                    "Condition unknown",
                    26,
                    fill="#8B572D",
                    bold=True,
                )
        else:
            car(draw, width - margin - 259, top + 140, scale=0.93)
            if progress > 0.23:
                put(draw, (inner + 419, top + 107), "2018", 42, bold=True)
                put(draw, (inner + 419, top + 166), "109,600 km", 35, fill=MUTED)
            if progress > 0.36:
                draw.rounded_rectangle(
                    (inner, top + 257, inner + 227, top + 302), radius=12, fill=PALE
                )
                put(draw, (inner + 14, top + 262), "Manual", 28, fill=TEAL, bold=True)
                draw.rounded_rectangle(
                    (inner + 249, top + 257, inner + 527, top + 302),
                    radius=12,
                    fill="#F7EBDD",
                )
                put(
                    draw,
                    (inner + 264, top + 262),
                    "Condition unknown",
                    28,
                    fill="#8B572D",
                    bold=True,
                )
    else:
        if mobile:
            first = report_crop(width - 2 * margin - 20, bottom - top - 26)
            last = report_crop(width - 2 * margin - 20, bottom - top - 26, True)
            crop = first.copy()
            reveal = round(crop.width * ease((progress - 0.43) / 0.18))
            if reveal:
                crop.paste(last.crop((0, 0, reveal, last.height)), (0, 0))
            layer.paste(
                crop, (margin + (width - 2 * margin - crop.width) // 2, top + 13)
            )
        else:
            first = report_crop(682, bottom - top - 24)
            last = report_crop(682, bottom - top - 24, True)
            crop = first.copy()
            reveal = round(crop.width * ease((progress - 0.43) / 0.18))
            if reveal:
                crop.paste(last.crop((0, 0, reveal, last.height)), (0, 0))
            layer.paste(crop, (margin + 13, top + 12))
            xx = margin + 660
            put(draw, (xx, top + 35), "Your English", 39, bold=True)
            put(draw, (xx, top + 85), "report.", 39, bold=True)
            put(draw, (xx, top + 153), "Prices in context", 29, fill=MUTED)
            put(draw, (xx, top + 194), "Mileage and age", 29, fill=MUTED)
            draw.rounded_rectangle(
                (xx, top + 256, xx + 254, top + 302), radius=12, fill=PALE
            )
            put(draw, (xx + 13, top + 262), "PDF + PNG", 28, fill=TEAL, bold=True)
    return layer


def scene_at(milliseconds):
    if milliseconds < 4400:
        return 0, milliseconds / 4400
    if milliseconds < 8500:
        return 1, (milliseconds - 4400) / 4100
    return 2, (milliseconds - 8500) / 4700


def frame(milliseconds, width, height):
    scene, progress = scene_at(milliseconds)
    canvas = shell(width, height)
    current = stage(scene, progress, width, height)
    top = 295 if width < 800 else 265
    bottom = 700 if width < 800 else 635
    area = (0, top + 29, width, bottom + 10)
    visible = current.crop(area)
    paper = Image.new("RGB", visible.size, PAPER)
    entry = min(1, progress / 0.14)
    if scene and entry < 0.5:
        amount = ease(entry * 2)
        previous = stage(scene - 1, 1, width, height).crop(area)
        visible = Image.blend(previous, paper, amount)
        offset = -round(12 * amount)
    else:
        amount = ease(entry if not scene else (entry - 0.5) * 2)
        visible = Image.blend(paper, visible, amount)
        offset = round(16 * (1 - amount))
    canvas.paste(visible, (0, area[1] + offset))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, bottom + 10, width, height), fill=PAPER)
    margin = 28 if width < 800 else 44
    spacing = (width - 2 * margin) / 3
    for index, label in enumerate(("Search", "Listings", "Report")):
        x = margin + index * spacing
        draw.ellipse((x, top, x + 27, top + 27), fill=TEAL if index == scene else PALE)
        put(
            draw,
            (x + 8, top - 1),
            str(index + 1),
            22,
            bold=True,
            fill=WHITE if index == scene else TEAL,
        )
        put(
            draw,
            (x + 40, top - 3),
            label,
            28 if width < 800 else 31,
            bold=index == scene,
            fill=TEAL if index == scene else MUTED,
        )
    caption = (
        "Start with a model and country.",
        "Keep the facts and the gaps visible.",
        "Open the report. Compare similar cars.",
    )[scene]
    put(draw, (margin, bottom + 16), caption, 26 if width < 800 else 31, fill=MUTED)
    put(
        draw,
        (margin, height - 66),
        "ILLUSTRATIVE DEMO",
        24 if width < 800 else 28,
        bold=True,
        fill=TEAL,
    )
    if width >= 800:
        put(
            draw,
            (width - margin, height - 65),
            "Example data, not live listings",
            25,
            fill=MUTED,
            anchor="ra",
        )
    draw.rounded_rectangle(
        (margin, height - 24, width - margin, height - 20), radius=2, fill=LINE
    )
    draw.rounded_rectangle(
        (
            margin,
            height - 24,
            margin + (width - 2 * margin) * milliseconds / DURATION_MS + 1,
            height - 20,
        ),
        radius=2,
        fill=TEAL,
    )
    if milliseconds > DURATION_MS - 500:
        first = frame(0, width, height)
        amount = ease((milliseconds - DURATION_MS + 500) / 500)
        caption_box = (0, bottom + 10, width, height - 68)
        navigation_box = (0, top - 6, width, top + 34)
        last_caption = canvas.crop(caption_box)
        first_caption = first.crop(caption_box)
        navigation = (
            canvas.crop(navigation_box) if amount < 0.5 else first.crop(navigation_box)
        )
        # Never dissolve different sentences or different navigation weights.
        for image in (canvas, first):
            ImageDraw.Draw(image).rectangle(
                (0, caption_box[1], width, caption_box[3] - 1), fill=PAPER
            )
        canvas = Image.blend(canvas, first, amount)
        caption = last_caption if amount < 0.5 else first_caption
        opacity = 1 - ease(amount * 2) if amount < 0.5 else ease((amount - 0.5) * 2)
        blank = Image.new("RGB", caption.size, PAPER)
        canvas.paste(Image.blend(blank, caption, opacity), caption_box[:2])
        canvas.paste(navigation, navigation_box[:2])
    return canvas


def render(width, height, suffix="", *, posters_only=False):
    poster = HERE / f"car-market-workflow{suffix}.png"
    frame(12150, width, height).save(poster, optimize=True)
    if posters_only:
        return [poster]
    atlas = Image.new("RGB", (width, height * 3))
    for index, ms in enumerate((3600, 7200, 12150)):
        atlas.paste(frame(ms, width, height), (0, index * height))
    palette = atlas.resize((width // 2, height * 3 // 2)).quantize(colors=128)
    frames = [
        frame(ms, width, height).quantize(palette=palette, dither=Image.Dither.NONE)
        for ms in range(0, DURATION_MS, FRAME_MS)
    ]
    animated = HERE / f"car-market-workflow{suffix}.gif"
    frames[0].save(
        animated,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_MS,
        loop=0,
        disposal=1,
        optimize=True,
    )
    return [poster, animated]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posters-only", action="store_true")
    args = parser.parse_args()
    preview = HERE / "sample-report-preview.png"
    with Image.open(HERE / "sample-report.png") as sample:
        excerpt = sample.crop((0, 0, sample.width, 2700))
        excerpt.thumbnail((1120, 1080), Image.Resampling.LANCZOS)
        excerpt.save(preview, optimize=True)
    outputs = render(1120, 760, posters_only=args.posters_only) + render(
        640, 820, "-mobile", posters_only=args.posters_only
    )
    for path in outputs + [preview]:
        print(f"{path.name}: {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
