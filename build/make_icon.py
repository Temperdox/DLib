"""Generate build/dlib.ico from PIL — red disc with a chunky white 'D'."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / 'dlib.ico'
SIZES = [16, 32, 48, 64, 128, 256]


def render(size: int) -> Image.Image:
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 24)
    draw.ellipse((pad, pad, size - pad, size - pad), fill=(240, 72, 72, 255))

    ring_pad = max(2, size // 12)
    draw.ellipse(
        (ring_pad, ring_pad, size - ring_pad, size - ring_pad),
        outline=(255, 255, 255, 235),
        width=max(1, size // 32),
    )

    # Chunky "D": vertical bar + arc, sized as a fraction of the icon.
    x0, y0 = int(size * 0.32), int(size * 0.28)
    x1, y1 = int(size * 0.72), int(size * 0.72)
    bar_w = max(2, size // 12)
    draw.rectangle((x0, y0, x0 + bar_w, y1), fill=(255, 255, 255, 255))
    draw.arc(
        (x0, y0, x1, y1),
        start=270, end=90,
        fill=(255, 255, 255, 255),
        width=max(2, size // 11),
    )
    return img


def main() -> None:
    images = [render(s) for s in SIZES]
    images[0].save(
        OUT,
        format='ICO',
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    print(f'wrote {OUT} ({len(SIZES)} sizes)')


if __name__ == '__main__':
    main()
