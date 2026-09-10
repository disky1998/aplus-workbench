# -*- coding: utf-8 -*-
"""程序图标：运行时画一个，同时供打包生成 .ico"""
from __future__ import annotations

ACCENT = (47, 111, 94, 255)
ACCENT_DARK = (30, 78, 65, 255)


def build_icon_image(size: int = 64):
    """墨绿圆角方块 + A+"""
    from PIL import Image, ImageDraw, ImageFont

    S = max(size, 16)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    r = max(2, int(S * 0.26))
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=ACCENT)
    # 底部压暗，做出一点体积感
    d.rounded_rectangle([0, int(S * 0.62), S - 1, S - 1], radius=r, fill=ACCENT_DARK)
    d.rounded_rectangle([0, int(S * 0.62), S - 1, int(S * 0.62) + r], radius=0, fill=ACCENT)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, outline=(255, 255, 255, 46), width=max(1, S // 32))

    text = "A+"
    font = None
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, int(S * 0.50))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    try:
        bbox = d.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((S - w) / 2 - bbox[0], (S - h) / 2 - bbox[1] - S * 0.03),
               text, font=font, fill=(255, 255, 255, 255))
    except Exception:
        d.text((S * 0.24, S * 0.22), text, font=font, fill=(255, 255, 255, 255))
    return img


def write_ico(path) -> bool:
    """生成多尺寸 .ico，供 PyInstaller 与托盘共用"""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    base = build_icon_image(256)
    base.save(p, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return p.exists()


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "assets/app.ico"
    print("ok" if write_ico(out) else "fail", out)
