"""解析层回归测试：用合成样本覆盖各篇真实文章没碰到的边界结构。

跑法：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from w2d import parser  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "edge.html"


def parse_fixture():
    raw = FIXTURE.read_text(encoding="utf-8")
    return raw, parser.parse(
        raw,
        url="https://mp.weixin.qq.com/s/test",
        channel="html",
        sha256="x",
        fetched_at="2026-10-19T00:00:00",
    )


class TestMeta(unittest.TestCase):
    def test_meta(self):
        _, a = parse_fixture()
        self.assertEqual(a.title, "M2 边界结构测试文章")
        self.assertEqual(a.account, "测试公众号")
        self.assertEqual(a.publish_time, "2026-10-19")


class TestCards(unittest.TestCase):
    def test_only_real_background_url_is_card(self):
        _, a = parse_fixture()
        cards = [b for b in a.blocks if b.kind == "card"]
        # 两张真背景图卡片；`background-image: none` 那个不能算
        self.assertEqual(len(cards), 2)
        for c in cards:
            self.assertTrue(c.asset and c.asset.startswith("http"))
        self.assertTrue(all(c.html for c in cards), "卡片应保留 HTML 片段供截图")

    def test_background_none_is_normal_paragraph(self):
        _, a = parse_fixture()
        texts = [
            "".join(r.text for r in b.runs) for b in a.blocks if b.kind == "paragraph"
        ]
        self.assertTrue(any("background-image 是 none" in t for t in texts))


class TestMedia(unittest.TestCase):
    def test_media_downgraded(self):
        _, a = parse_fixture()
        media = [b for b in a.blocks if b.kind == "media"]
        labels = " ".join(b.text_fallback or "" for b in media)
        self.assertEqual(len(media), 3, f"应为 视频/音频/小程序 3 个，实际 {labels}")
        self.assertIn("视频", labels)
        self.assertIn("音频", labels)
        self.assertIn("小程序", labels)
        self.assertTrue(all("未下载" in (b.text_fallback or "") for b in media))

    def test_profile_card(self):
        _, a = parse_fixture()
        quotes = [
            "".join(r.text for r in b.runs) for b in a.blocks if b.kind == "quote"
        ]
        self.assertTrue(any("【公众号名片】测试公众号" in q for q in quotes))


class TestIcons(unittest.TestCase):
    def test_svg_removed(self):
        _, a = parse_fixture()
        text = " ".join(
            "".join(r.text for r in b.runs) for b in a.blocks if b.kind == "paragraph"
        )
        self.assertIn("这段文字后面的图标应该被删掉", text)
        self.assertNotIn("<svg", text)

    def test_data_svg_img_removed(self):
        _, a = parse_fixture()
        for b in a.blocks:
            if b.kind == "image":
                self.assertFalse(str(b.asset).startswith("data:"), "data: 图标不应成为图片块")
        self.assertTrue(any("SVG" in w for w in a.warnings), a.warnings)


class TestTables(unittest.TestCase):
    def test_table_with_th(self):
        _, a = parse_fixture()
        tables = [b for b in a.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 2)
        explicit = tables[0]
        self.assertEqual(explicit.header_rows, 1)
        self.assertFalse(explicit.header_inferred)
        self.assertEqual(len(explicit.rows), 3)
        self.assertEqual(parser._cell_text(explicit.rows[0][0]), "方法")
        self.assertEqual(parser._cell_text(explicit.rows[2][1]), "89.4")

    def test_table_header_inferred(self):
        _, a = parse_fixture()
        tables = [b for b in a.blocks if b.kind == "table"]
        inferred = tables[1]
        self.assertEqual(inferred.header_rows, 1, "首行为文字、次行有数字 → 应推断为表头")
        self.assertTrue(inferred.header_inferred)


class TestImages(unittest.TestCase):
    def test_figure_caption(self):
        _, a = parse_fixture()
        imgs = [b for b in a.blocks if b.kind == "image"]
        caps = [b.caption for b in imgs]
        self.assertIn("图1：带 figcaption 的图片", caps)

    def test_alt_caption_deduped(self):
        _, a = parse_fixture()
        imgs = [b for b in a.blocks if b.kind == "image"]
        # 原文自带「图2 重复图注测试」文字，alt 也是同名 → 图注应被去掉，避免两行
        fig2 = [b for b in imgs if b.asset and "FIG2" in b.asset]
        self.assertEqual(len(fig2), 1)
        self.assertIsNone(fig2[0].caption)

    def test_span_wrapped_image_kept(self):
        _, a = parse_fixture()
        imgs = [b for b in a.blocks if b.kind == "image"]
        self.assertTrue(any("FIG2" in (b.asset or "") for b in imgs))
        self.assertTrue(any("FIG1" in (b.asset or "") for b in imgs))

    def test_list_image_kept(self):
        _, a = parse_fixture()
        imgs = [b for b in a.blocks if b.kind == "image"]
        self.assertTrue(any("FIG3" in (b.asset or "") for b in imgs), "ol 里的图片不能丢")


class TestTextIntegrity(unittest.TestCase):
    def test_deep_nested_text_kept(self):
        _, a = parse_fixture()
        text = " ".join(
            "".join(r.text for r in b.runs) for b in a.blocks if b.kind == "paragraph"
        )
        self.assertIn("深层嵌套里的文字必须保留", text)

    def test_no_empty_paragraphs(self):
        _, a = parse_fixture()
        for b in a.blocks:
            if b.kind == "paragraph":
                self.assertTrue(b.has_text(), "不应出现空段落")

    def test_no_placeholder_text(self):
        _, a = parse_fixture()
        all_text = " ".join(
            "".join(r.text for r in b.runs)
            for b in a.blocks
            if b.kind in ("paragraph", "heading", "quote")
        )
        self.assertNotIn("\xa0", all_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
