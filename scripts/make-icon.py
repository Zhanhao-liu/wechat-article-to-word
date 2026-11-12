"""生成应用图标：assets/w2d.ico + docs/assets/app-icon.png

设计要点（为 16px 也清晰）：
  - 蓝底圆角方块（高对比，深色任务栏 / 浅色桌面都看得清）
  - 居中的白色粗体 "W"（小尺寸下比多字母更易辨认）
  - 左下角叠一条浅色"文档线"，暗示导出成文档

用法：.venv\\Scripts\\python.exe scripts\\make-icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "assets" / "w2d.ico"
PNG = ROOT / "docs" / "assets" / "app-icon.png"

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
BASE = 512  # 先画大图再缩放，边缘更干净

TOP = (108, 126, 176)     # #6C7EB0
BOTTOM = (58, 74, 116)    # #3A4A74
ACCENT = (147, 197, 253)  # 文档线


def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("msyhbd.ttc", "arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return grad.resize((size, size))


def build_base() -> Image.Image:
    img = _gradient(BASE, TOP, BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)

    # 大号白色 W（占比不要太大，留出下方文档线的位置）
    f = _font(int(BASE * 0.46))
    d.text((BASE // 2, int(BASE * 0.42)), "W", font=f, fill=(255, 255, 255, 255), anchor="mm")

    # 底部两条文档线，暗示「导出为文档」
    bar_h = int(BASE * 0.048)
    d.rounded_rectangle(
        [int(BASE * 0.26), int(BASE * 0.70), int(BASE * 0.74), int(BASE * 0.70) + bar_h],
        radius=bar_h // 2, fill=ACCENT,
    )
    d.rounded_rectangle(
        [int(BASE * 0.26), int(BASE * 0.79), int(BASE * 0.58), int(BASE * 0.79) + bar_h],
        radius=bar_h // 2, fill=(255, 255, 255, 190),
    )

    # 最后套圆角遮罩，保证没有内容溢出到方块之外
    mask = Image.new("L", (BASE, BASE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, BASE - 1, BASE - 1], radius=BASE // 5, fill=255
    )
    out = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def write_ico(img: Image.Image, path: Path, sizes: tuple[int, ...]) -> None:
    """手动写 ICO（32 位 BMP 帧）。

    不能用 PIL 的 save(format="ICO")：它把帧存成 PNG 压缩，
    而 Windows 资源管理器对小尺寸只认 BMP 帧 —— 结果是图标显示为空白。
    """
    import struct

    frames: list[tuple[int, bytes]] = []
    for s in sizes:
        frame = img.resize((s, s), Image.LANCZOS).convert("RGBA")
        pixels = frame.tobytes("raw", "BGRA")  # ICO 用 BGRA
        # 1bpp AND 掩码：alpha=0 处为 1（透明）
        alpha = frame.getchannel("A")
        mask_row_bytes = ((s + 31) // 32) * 4
        mask = bytearray()
        for y in range(s):
            bits = bytearray(mask_row_bytes)
            for x in range(s):
                if alpha.getpixel((x, y)) < 128:
                    bits[x // 8] |= 0x80 >> (x % 8)
            mask += bits
        # BITMAPINFOHEADER：高度要写成 2 倍（XOR 图 + AND 掩码）
        header = struct.pack(
            "<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0
        )
        frames.append((s, header + pixels + bytes(mask)))

    count = len(frames)
    out = bytearray(struct.pack("<HHH", 0, 1, count))  # reserved, type=icon, count
    offset = 6 + 16 * count
    for s, blob in frames:
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for _, blob in frames:
        out += blob
    path.write_bytes(bytes(out))


def main() -> None:
    base = build_base()
    ICO.parent.mkdir(parents=True, exist_ok=True)
    write_ico(base, ICO, SIZES)

    PNG.parent.mkdir(parents=True, exist_ok=True)
    base.resize((256, 256), Image.LANCZOS).save(PNG)

    # 自检：每个尺寸都不该是空白，且帧必须是 BMP（不是 PNG）
    import struct

    raw = ICO.read_bytes()
    cnt = struct.unpack("<H", raw[4:6])[0]
    png_frames = 0
    for i in range(cnt):
        off = 6 + i * 16
        *_, size, offset = struct.unpack("<BBBBHHII", raw[off:off + 16])
        if raw[offset:offset + 8] == b"\x89PNG\r\n\x1a\n":
            png_frames += 1
    print(f"ICO: {ICO}  {ICO.stat().st_size / 1024:.1f} KB  帧数 {cnt}  PNG 帧 {png_frames}")
    assert png_frames == 0, "存在 PNG 帧，Windows 可能显示空白图标"

    with Image.open(ICO) as im:
        for s in sorted(im.info.get("sizes", [])):
            im.size = s
            im.load()
            frame = im.convert("RGBA")
            alpha = frame.getchannel("A")
            opaque = sum(alpha.histogram()[64:]) * 100 // (frame.width * frame.height)
            assert opaque > 40, f"{s} 尺寸几乎透明"
            print(f"  {s}x{s}: 不透明 {opaque}%  OK")
    print(f"PNG: {PNG}")


if __name__ == "__main__":
    main()
