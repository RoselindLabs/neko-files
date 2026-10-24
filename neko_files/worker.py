"""Child process document parser. No macros, eval, shell, network or host imports.

Adapted from Arale-file-reader. Resource limits apply on Linux; not a filesystem sandbox.
"""

from pathlib import Path
import base64
import csv
import io
import json
import sys
import zipfile

TEXT = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".css",
    ".sh",
    ".sql",
    ".c",
    ".cpp",
    ".h",
    ".java",
    ".rs",
    ".go",
    ".srt",
    ".vtt",
}
DOCUMENTS = TEXT | {".pdf", ".docx", ".xlsx", ".csv"}
MAX_CHARS = 1_000_000


def parse(path, ext):
    units, total, truncated = [], 0, False

    def add(label, text, **location):
        nonlocal total, truncated
        remaining = MAX_CHARS - total
        if remaining <= 0 or len(units) >= 20000:
            truncated = True
            return False
        if len(text) > remaining:
            text, truncated = text[:remaining], True
        units.append({"label": str(label), "text": text, **location})
        total += len(text)
        return True

    if ext in {".docx", ".xlsx"}:
        with zipfile.ZipFile(path) as archive:
            if (
                len(archive.infolist()) > 2000
                or sum(m.file_size for m in archive.infolist()) > 64 * 1024 * 1024
            ):
                raise ValueError("Office目录或展开大小超限")
    if ext in TEXT or ext == ".csv":
        raw = path.read_bytes()
        if b"\0" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ValueError("文件不是可读文本")
        text, encoding = None, None
        for candidate in (
            ["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
        ):
            try:
                text, encoding = raw.decode(candidate), candidate
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("文本编码无法识别")
        rows = csv.reader(io.StringIO(text)) if ext == ".csv" else text.splitlines()
        for number, row in enumerate(rows, 1):
            if ext == ".csv" and len(row) > 200:
                truncated = True
            value = " | ".join(row[:200]) if ext == ".csv" else row
            if not add(number, value):
                break
        return {
            "kind": "rows" if ext == ".csv" else "lines",
            "units": units,
            "truncated": truncated,
            "encoding": encoding,
        }
    if ext == ".pdf":
        import pymupdf

        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("PDF已加密，请解密后重发")
            pages = document.page_count
            for number in range(min(pages, 300)):
                if not add(number + 1, document[number].get_text()):
                    break
        return {
            "kind": "pages",
            "units": units,
            "total_pages": pages,
            "truncated": truncated or pages > len(units),
            "needs_ocr": not any(u["text"].strip() for u in units),
        }
    if ext == ".docx":
        from docx import Document
        from docx.text.paragraph import Paragraph

        document = Document(path)
        for number, block in enumerate(document.iter_inner_content(), 1):
            if isinstance(block, Paragraph):
                if not add(f"段落{number}", block.text):
                    break
            else:
                for index, row in enumerate(block.rows, 1):
                    if not add(f"表{number}行{index}", " | ".join(cell.text for cell in row.cells)):
                        break
                if truncated:
                    break
        return {"kind": "paragraphs", "units": units, "truncated": truncated}
    if ext == ".xlsx":
        from openpyxl import load_workbook

        document = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            sheets = document.sheetnames
            for sheet_index, sheet in enumerate(document.worksheets[:20]):
                truncated |= (sheet.max_row or 0) > 5000 or (sheet.max_column or 0) > 100
                for number, row in enumerate(
                    sheet.iter_rows(
                        max_row=min(sheet.max_row or 5000, 5000),
                        max_col=min(sheet.max_column or 100, 100),
                        values_only=True,
                    ),
                    1,
                ):
                    if not add(
                        f"{sheet.title}!{number}",
                        " | ".join(str(c) if c is not None else "" for c in row),
                        sheet=sheet.title,
                        row=number,
                    ):
                        break
                if total >= MAX_CHARS or len(units) >= 20000:
                    truncated |= sheet_index + 1 < len(sheets[:20])
                    break
            return {
                "kind": "sheet_rows",
                "units": units,
                "sheets": sheets[:20],
                "truncated": truncated or len(sheets) > 20,
                "formula_note": "公式为原文，未执行或计算",
            }
        finally:
            document.close()
    raise ValueError("文件格式不支持；不读旧DOC/XLS、可执行文件或视频")


def render_pdf(path, page):
    import pymupdf

    with pymupdf.open(path) as document:
        if document.needs_pass or type(page) is not int or not 1 <= page <= document.page_count:
            raise ValueError("PDF加密或页码无效")
        rect = document[page - 1].rect
        if not 0 < max(rect.width, rect.height) < 1_000_000:
            raise ValueError("页面尺寸无效")
        scale = min(2.0, 1600 / max(rect.width, rect.height))
        for _ in range(4):
            pixmap = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            data = pixmap.tobytes("jpeg", jpg_quality=80)
            if len(data) <= 700 * 1024:
                return {"image_base64": base64.b64encode(data).decode("ascii")}
            scale *= 0.7
    raise ValueError("页面图片过大")


def main():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (40 * 1024**2, 40 * 1024**2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except ImportError:
        raise SystemExit("Production parser requires Linux resource limits") from None
    try:
        path, ext, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 40 * 1024**2:
            raise ValueError("文件不可读或超过上限")
        options = json.loads(sys.stdin.read(8192) or "{}")
        if operation == "render":
            result = render_pdf(path, options["page"])
        elif operation in ["archive-list", "archive-read"]:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from archives import catalog, extract_member

            if operation == "archive-list":
                result = catalog(path, ext, options["name"])
            else:
                extract_member(
                    path,
                    ext,
                    options["member"],
                    Path(options["destination"]),
                    options["name"],
                    options["limit"],
                )
                result = {"extracted": True}
        else:
            result = parse(path, ext)
        print(json.dumps({"success": True, **result}, ensure_ascii=False))
    except Exception:
        # Do not echo library exceptions, private local paths or document content.
        print(json.dumps({"success": False, "error": "文件损坏、格式不符或超过解析上限"}))


if __name__ == "__main__":
    main()
