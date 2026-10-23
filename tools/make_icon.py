"""Generate badge/apps/airtime/icon.png (24x24): a badge on a throw arc."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "badge" / "apps" / "airtime" / "icon.png"

img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, 23, 23), radius=5, fill=(30, 70, 200, 255))
for i, (x, y) in enumerate([(3, 19), (5, 13), (8, 9), (12, 7)]):
    d.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(255, 255, 255, 120 + i * 40))
d.rounded_rectangle((13, 5, 21, 15), radius=2, fill=(230, 200, 20, 255), outline=(0, 0, 0, 255))
d.rectangle((15, 7, 19, 10), fill=(40, 40, 40, 255))
d.line((2, 21, 21, 21), fill=(255, 255, 255, 200))
img.save(OUT)
print("wrote", OUT)
