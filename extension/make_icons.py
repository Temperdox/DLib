"""Generate extension icons (icon-16/48/128.png) using the DLib brand mark."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent
SIZES = [16, 48, 128]


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
    for s in SIZES:
        path = OUT_DIR / f'icon-{s}.png'
        render(s).save(path, format='PNG')
        print(f'wrote {path}')


if __name__ == '__main__':
    main()
