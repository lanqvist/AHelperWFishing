"""Draw the app icon (a bobber on rippling water) into packaging/icon.ico.

Usage: uv run python tools/make_icon.py
Drawn at 1024 px and downsampled per size, so small sizes stay crisp.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def draw(size=1024):
    s = size / 1024
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    # Rounded tile: deep water.
    d.rounded_rectangle((32 * s, 32 * s, 992 * s, 992 * s), radius=220 * s, fill=(18, 64, 112, 255))
    d.rounded_rectangle((32 * s, 520 * s, 992 * s, 992 * s), radius=220 * s, fill=(14, 52, 94, 255))
    # Ripples around the bobber.
    for r, alpha in ((380, 70), (300, 110), (225, 160)):
        d.ellipse((512 * s - r * s, 640 * s - r * 0.36 * s, 512 * s + r * s, 640 * s + r * 0.36 * s),
                  outline=(150, 210, 255, alpha), width=max(1, int(22 * s)))  # fmt: skip
    # Bobber: red cap, white body, dark band, antenna.
    d.line((512 * s, 170 * s, 512 * s, 330 * s), fill=(250, 250, 250, 255), width=max(1, int(30 * s)))
    d.ellipse((392 * s, 300 * s, 632 * s, 540 * s), fill=(232, 52, 52, 255))
    d.chord((392 * s, 300 * s, 632 * s, 700 * s), 0, 180, fill=(246, 246, 246, 255))
    d.rectangle((392 * s, 490 * s, 632 * s, 525 * s), fill=(30, 30, 40, 255))
    d.ellipse((440 * s, 330 * s, 500 * s, 390 * s), fill=(255, 150, 150, 200))  # highlight
    return image


def main():
    big = draw()
    frames = []
    for size in SIZES:
        frame = big.resize((size, size), Image.Resampling.LANCZOS)
        if size <= 32:
            frame = frame.filter(ImageFilter.UnsharpMask(radius=1, percent=60, threshold=1))
        frames.append(frame)
    # packaging/: installer and exe resource; package copy: the window icon at run time.
    for target in (ROOT / 'packaging' / 'icon.ico', ROOT / 'src' / 'ahelperw_fishing' / 'icon.ico'):
        frames[-1].save(target, sizes=[(f.width, f.height) for f in frames], append_images=frames[:-1])
        print(target)
    big.resize((256, 256), Image.Resampling.LANCZOS).save(ROOT / 'packaging' / 'icon.png')


if __name__ == '__main__':
    main()
