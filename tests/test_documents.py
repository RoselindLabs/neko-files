from pathlib import Path
from tempfile import TemporaryDirectory

import gzip
import tarfile
import unittest
import zipfile

from neko_files import archives, reading, worker


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_text_encoding_continuation_and_csv_truncation(self):
        text = self.root / "text.txt"
        text.write_bytes("第一行\n第二行".encode("utf-16"))
        parsed = worker.parse(text, ".txt")
        self.assertEqual(parsed["encoding"], "utf-16")
        first = reading.window(parsed, limit=4)
        second = reading.window(parsed, offset=first["next_offset"], limit=100)
        self.assertIn("第二行", first["text"] + second["text"])
        csv = self.root / "wide.csv"
        csv.write_text(",".join(["x"] * 201), encoding="utf-8")
        self.assertTrue(worker.parse(csv, ".csv")["truncated"])

    def test_real_xlsx_sheet_prefix_collision_is_fixed(self):
        from openpyxl import Workbook

        book = Workbook()
        book.active.title = "A"
        book.active.append(["wanted"])
        book.create_sheet("A!B").append(["different sheet"])
        path = self.root / "sheets.xlsx"
        book.save(path)
        book.close()
        parsed = worker.parse(path, ".xlsx")
        selected = reading.window(parsed, sheet="A")
        self.assertEqual(selected["available_units"], 1)
        self.assertIn("wanted", selected["text"])
        self.assertNotIn("different", selected["text"])
        self.assertEqual(parsed["units"][0]["sheet"], "A")
        self.assertEqual(parsed["units"][0]["row"], 1)

    def test_real_pdf_and_docx_parse_and_pdf_render(self):
        import pymupdf
        from docx import Document

        pdf = pymupdf.open()
        pdf.new_page().insert_text((50, 50), "Synthetic PDF")
        path = self.root / "doc.pdf"
        pdf.save(path)
        pdf.close()
        parsed = worker.parse(path, ".pdf")
        self.assertIn("Synthetic PDF", parsed["units"][0]["text"])
        self.assertTrue(worker.render_pdf(path, 1)["image_base64"])
        document = Document()
        document.add_paragraph("Synthetic DOCX")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "table"
        path = self.root / "doc.docx"
        document.save(path)
        parsed = worker.parse(path, ".docx")
        self.assertIn("DOCX", parsed["units"][0]["text"])
        self.assertIn("table", parsed["units"][1]["text"])

    def test_keyword_batches_and_invalid_offsets(self):
        document = {"kind": "lines", "units": [{"label": str(n), "text": "hit"} for n in range(30)]}
        first = reading.window(document, keyword="hit", limit=16000)
        self.assertEqual(first["match_count"], 30)
        self.assertEqual(first["next_match_offset"], 20)
        second = reading.window(document, keyword="hit", match_offset=20, limit=16000)
        self.assertFalse(second["matches_truncated"])
        for args in [{"start": True}, {"offset": -1}, {"offset": 99999}, {"match_offset": 1}]:
            with self.assertRaises(reading.FileError):
                reading.window(document, **args)

    def test_archives_reject_traversal_links_duplicates_and_excess_entries(self):
        for name in ["../x.txt", "/x.txt", "C:\\x.txt"]:
            with self.assertRaises(ValueError):
                archives.safe_name(name)
        path = self.root / "bad.tar"
        with tarfile.open(path, "w") as archive:
            item = tarfile.TarInfo("link")
            item.type = tarfile.SYMTYPE
            item.linkname = "outside"
            archive.addfile(item)
        with self.assertRaises(ValueError):
            archives.catalog(path, ".tar")
        path = self.root / "too-many.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for n in range(101):
                archive.writestr(str(n) + ".txt", "x")
        with self.assertRaises(ValueError):
            archives.catalog(path, ".zip")

    def test_archive_extracts_only_named_member_and_checks_target(self):
        path = self.root / "files.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("one.txt", "one")
            archive.writestr("two.txt", "two")
        target = self.root / "cache.txt"
        archives.extract_member(path, ".zip", "two.txt", target)
        self.assertEqual(target.read_text(), "two")
        self.assertFalse((self.root / "one.txt").exists())
        with self.assertRaises(ValueError):
            archives.extract_member(path, ".zip", "one.txt", target)
        path = self.root / "text.txt.gz"
        with gzip.open(path, "wb") as stream:
            stream.write(b"gzip text")
        self.assertEqual(archives.catalog(path, ".gz", "text.txt.gz")["entries"][0]["size"], 9)
