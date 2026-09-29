"""Regression tests for preserving imported EPUB structure during export."""

from __future__ import annotations

import io
import unittest
import zipfile

from core.epub_exporter import EpubExporter
from core.epub_layout import build_epub_layout, split_translated_paragraphs
from core.pdf_exporter import PdfExporter


def _epub_node(
    node_id: str,
    node_type: str,
    source: str,
    translated: str,
    *,
    chapter_file: str,
    structure_type: str,
) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "source": source,
        "translated_text": translated,
        "unit_ids": [f"unit-{node_id}"],
        "attributes": {
            "chapter_file": chapter_file,
            "structure_type": structure_type,
        },
    }


class EpubExportLayoutTests(unittest.TestCase):
    def test_source_epub_contents_keeps_chapter_numbers_with_their_labels(self) -> None:
        node = _epub_node(
            "entries", "paragraph", "Foreword\n\nI The Union\n\n1 Origins",
            "前言\n\nI 联盟\n\n1 起源",
            chapter_file="OEBPS/Text/contents.xhtml", structure_type="toc_entry",
        )
        node["attributes"]["source_block_range"] = {"start": 1, "end": 3, "count": 3}

        markup = EpubExporter(".")._node_markup(node, role="contents")

        self.assertIn('<span class="contents-label">I 联盟</span>', markup)
        self.assertIn('<span class="contents-label">1 起源</span>', markup)
        self.assertNotIn('class="contents-page"', markup)
        self.assertEqual(markup.count('class="contents-entry '), 3)

    def test_translated_blocks_split_only_when_the_saved_count_matches(self) -> None:
        exact = _epub_node(
            "copyright", "paragraph", "first\n\nsecond", "第一段\n\n第二段",
            chapter_file="OEBPS/Text/copyright.xhtml", structure_type="paragraph",
        )
        exact["attributes"]["source_block_range"] = {"start": 1, "end": 2, "count": 2}
        ambiguous = dict(exact)
        ambiguous["translated_text"] = "第一段仍连在一起"
        inconsistent_range = dict(exact)
        inconsistent_range["attributes"] = {
            **exact["attributes"],
            "source_block_range": {"start": 1, "end": 3, "count": 2},
        }

        self.assertEqual(split_translated_paragraphs(exact), ["第一段", "第二段"])
        self.assertEqual(split_translated_paragraphs(ambiguous), ["第一段仍连在一起"])
        self.assertEqual(split_translated_paragraphs(inconsistent_range), ["第一段\n\n第二段"])

    def test_epub_markup_restores_verified_copyright_and_footnote_paragraphs(self) -> None:
        exporter = EpubExporter(".")
        copyright = _epub_node(
            "copyright", "paragraph", "first\n\nsecond", "第一段\n\n第二段",
            chapter_file="OEBPS/Text/copyright.xhtml", structure_type="paragraph",
        )
        copyright["attributes"].update(
            {"source_block_range": {"start": 1, "end": 2, "count": 2}, "class_tokens": ["copyrighttop"]}
        )
        footnote = _epub_node(
            "footnote", "paragraph", "note one\n\nnote two", "注释一\n\n注释二",
            chapter_file="OEBPS/Text/foreword.xhtml", structure_type="footnote",
        )
        footnote["attributes"].update(
            {"source_block_range": {"start": 5, "end": 6, "count": 2}, "class_tokens": ["footnote"]}
        )

        copyright_markup = exporter._node_markup(copyright, role="frontmatter")
        footnote_markup = exporter._node_markup(footnote)

        self.assertIn('class="frontmatter source-centered source-block-group"', copyright_markup)
        self.assertEqual(copyright_markup.count("<p>"), 2)
        self.assertEqual(copyright_markup.count('id="copyright"'), 1)
        self.assertEqual(copyright_markup.count('data-unit-ids="unit-copyright"'), 1)
        self.assertIn('class="footnote-group source-block-group"', footnote_markup)
        self.assertEqual(footnote_markup.count('class="footnote"'), 2)

        following = _epub_node(
            "copyright-next", "paragraph", "more", "后续版权信息",
            chapter_file="OEBPS/Text/copyright.xhtml", structure_type="paragraph",
        )
        chapter = [copyright, following]
        archive = exporter._build_archive(
            title="Example", language="zh-CN", book_id="urn:uuid:example",
            chapters=[chapter], chapter_files=["text/chapter-001.xhtml"],
            trace={"unit_count": 2, "nodes": []}, layout=build_epub_layout(chapter),
        )
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            xhtml = package.read("OEBPS/text/chapter-001.xhtml").decode("utf-8")
            css = package.read("OEBPS/styles.css").decode("utf-8")
        self.assertEqual(xhtml.count('class="copyright-page"'), 1)
        self.assertIn('<body class="copyright-page">', xhtml)
        self.assertIn("body.copyright-page { padding-top: 7em; }", css)

    def test_pdf_epub_layout_centers_copyright_and_keeps_toc_separate(self) -> None:
        nodes = [
            _epub_node(
                "title", "heading", "Title", "书名",
                chapter_file="OEBPS/Text/title.xhtml", structure_type="heading",
            ),
            _epub_node(
                "copyright", "paragraph", "first\n\nsecond", "版权第一段\n\n版权第二段",
                chapter_file="OEBPS/Text/copyright.xhtml", structure_type="paragraph",
            ),
            _epub_node(
                "contents", "heading", "CONTENTS", "目录",
                chapter_file="OEBPS/Text/contents.xhtml", structure_type="heading",
            ),
            _epub_node(
                "entries", "paragraph", "Foreword 1", "前言 1",
                chapter_file="OEBPS/Text/contents.xhtml", structure_type="toc_entry",
            ),
            _epub_node(
                "foreword", "heading", "FOREWORD", "FOREWORD",
                chapter_file="OEBPS/Text/foreword.xhtml", structure_type="heading",
            ),
            _epub_node(
                "note", "paragraph", "1. NLR I/75, 1972, pp. 5–120; 2. earlier study",
                "1. 《新左派评论》I/75，1972年，第5–120页\n\n2. 前文研究",
                chapter_file="OEBPS/Text/foreword.xhtml", structure_type="footnote",
            ),
        ]
        for index, node in enumerate(nodes):
            node["attributes"]["source_block_range"] = {
                "start": index + 1,
                "end": index + 1,
                "count": 1,
            }
        nodes[1]["attributes"].update(
            {"source_block_range": {"start": 2, "end": 3, "count": 2}, "class_tokens": ["copyrighttop"]}
        )
        nodes[5]["attributes"]["source_block_range"] = {"start": 6, "end": 7, "count": 2}

        lines = PdfExporter(".")._document_lines(nodes)
        copyright_lines = [line for line in lines if line[0].startswith("版权")]
        text_lines = [line[0] for line in lines]

        self.assertEqual(len(copyright_lines), 2)
        self.assertTrue(all(line[3] == "center" for line in copyright_lines))
        self.assertLess(text_lines.index("\f"), text_lines.index("版权第一段"))
        self.assertEqual(lines[text_lines.index("版权第一段") - 1][1], 55.0)
        self.assertLess(text_lines.index("版权第二段"), text_lines.index("目录"))
        self.assertLess(text_lines.index("目录"), text_lines.index("FOREWORD"))
        self.assertIn("1. 《新左派评论》I/75，1972年，第5–120页", text_lines)
        self.assertIn("2. 前文研究", text_lines)

    def test_contents_ends_when_epub_chapter_file_changes(self) -> None:
        nodes = [
            _epub_node(
                "contents", "heading", "CONTENTS", "目录",
                chapter_file="OEBPS/Text/contents.xhtml", structure_type="heading",
            ),
            _epub_node(
                "entries", "paragraph", "Foreword 1", "前言 1",
                chapter_file="OEBPS/Text/contents.xhtml", structure_type="toc_entry",
            ),
            _epub_node(
                "acknowledgments", "heading", "ACKNOWLEDGMENTS", "ACKNOWLEDGMENTS",
                chapter_file="OEBPS/Text/frontmatterb.xhtml", structure_type="heading",
            ),
            _epub_node(
                "ack-body", "paragraph", "Acknowledgment text", "致谢正文",
                chapter_file="OEBPS/Text/frontmatterb.xhtml", structure_type="paragraph",
            ),
            _epub_node(
                "foreword", "heading", "FOREWORD", "FOREWORD",
                chapter_file="OEBPS/Text/frontmatter01.xhtml", structure_type="heading",
            ),
            _epub_node(
                "footnote", "paragraph", "1. NLR I/75, 1972, pp. 5–120",
                "1. 《新左派评论》I/75，1972年，第5–120页",
                chapter_file="OEBPS/Text/frontmatter01.xhtml", structure_type="footnote",
            ),
        ]

        layout = build_epub_layout(nodes)

        self.assertEqual(layout.roles["contents"], "contents")
        self.assertEqual(layout.roles["entries"], "contents")
        self.assertNotIn(layout.roles["acknowledgments"], {"contents", "contents-running"})
        self.assertNotIn(layout.roles["ack-body"], {"contents", "contents-running"})
        self.assertNotIn(layout.roles["foreword"], {"contents", "contents-running"})
        self.assertNotIn(layout.roles["footnote"], {"contents", "contents-running"})
        self.assertNotEqual(
            layout.chapter_by_node["contents"], layout.chapter_by_node["acknowledgments"]
        )

    def test_contents_semantics_end_before_next_heading_in_same_file(self) -> None:
        nodes = [
            _epub_node(
                "contents", "heading", "CONTENTS", "目录",
                chapter_file="OEBPS/Text/frontmatter.xhtml", structure_type="heading",
            ),
            _epub_node(
                "entries", "paragraph", "Foreword 1", "前言 1",
                chapter_file="OEBPS/Text/frontmatter.xhtml", structure_type="toc_entry",
            ),
            _epub_node(
                "foreword", "heading", "FOREWORD", "FOREWORD",
                chapter_file="OEBPS/Text/frontmatter.xhtml", structure_type="heading",
            ),
            _epub_node(
                "body", "paragraph", "Body text", "正文",
                chapter_file="OEBPS/Text/frontmatter.xhtml", structure_type="paragraph",
            ),
        ]

        layout = build_epub_layout(nodes)

        self.assertEqual(layout.roles["entries"], "contents")
        self.assertNotIn(layout.roles["foreword"], {"contents", "contents-running"})
        self.assertNotIn(layout.roles["body"], {"contents", "contents-running"})

    def test_pdf_legacy_contents_adapter_leaves_epub_footnotes_intact(self) -> None:
        nodes = [
            _epub_node(
                "contents", "heading", "CONTENTS", "目录",
                chapter_file="OEBPS/Text/contents.xhtml", structure_type="heading",
            ),
            _epub_node(
                "foreword", "heading", "FOREWORD", "FOREWORD",
                chapter_file="OEBPS/Text/frontmatter01.xhtml", structure_type="heading",
            ),
            _epub_node(
                "note", "paragraph", "1. NLR I/75, 1972, pp. 5–120; 2. earlier study",
                "1. 《新左派评论》I/75，1972年，第5–120页；2. 前文研究",
                chapter_file="OEBPS/Text/frontmatter01.xhtml", structure_type="footnote",
            ),
        ]

        displayed = PdfExporter._legacy_layout_nodes(nodes)

        self.assertEqual([node["id"] for node in displayed], [node["id"] for node in nodes])
        self.assertEqual(displayed[2]["translated_text"], nodes[2]["translated_text"])
        self.assertFalse(
            any((node.get("attributes") or {}).get("legacy_display_split") for node in displayed)
        )


if __name__ == "__main__":
    unittest.main()
