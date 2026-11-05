"""Generuje notatnik.ico (uruchom raz: python make_icon.py)."""
from PIL import Image, ImageDraw
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# tło: zaokrąglony kwadrat z pionowym gradientem
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    gd.line([(0, y), (S, y)], fill=(int(49 + 30 * t), int(46 + 10 * t), int(129 - 40 * t), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
# kartka notatnika
d.rounded_rectangle([250, 210, 720, 820], radius=60, fill=(255, 255, 255, 255))
for i, w in enumerate([330, 380, 280, 350]):
    y = 330 + i * 105
    d.rounded_rectangle([320, y, 320 + w - 40, y + 34], radius=17, fill=(196, 199, 222, 255))
# czerwona kropka nagrywania
d.ellipse([560, 560, 840, 840], fill=(255, 255, 255, 255))
d.ellipse([590, 590, 810, 810], fill=(229, 72, 77, 255))
img.save("notatnik.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save("notatnik_preview.png")
print("ok")
