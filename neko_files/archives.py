"""Recovered parser core: bounded catalogs, one requested member only; provenance pending."""

from pathlib import Path, PurePosixPath, PureWindowsPath
import gzip
import stat
import tarfile
import zipfile

ARCHIVES = {".zip", ".tar", ".tgz", ".tar.gz", ".gz"}
MAX_EXPANDED = 64 * 1024 * 1024


def extension(name):
    return ".tar.gz" if name.lower().endswith(".tar.gz") else Path(name.lower()).suffix


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 1024 or any(ord(c) < 32 for c in name):
        raise ValueError("压缩包成员名无效")
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).anchor or ".." in path.parts:
        raise ValueError("压缩包包含越界路径")
    return str(path)


def catalog(path, ext, original_name=""):
    entries, seen = [], set()
    total = 0

    def add(name, size, encrypted=False):
        nonlocal total
        name = safe_name(name)
        if name in seen or size < 0:
            raise ValueError("成员名重复或大小无效")
        total += size
        if len(entries) >= 100 or total > MAX_EXPANDED:
            raise ValueError("压缩包超过100文件或64MiB展开限制")
        seen.add(name)
        entries.append({"name": name, "size": size, "encrypted": encrypted})

    if ext == ".zip":
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > 300:
                raise ValueError("压缩包目录超限")
            for member in archive.infolist():
                safe_name(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                ):
                    raise ValueError("拒绝压缩包链接或特殊文件")
                if not member.is_dir():
                    add(member.filename, member.file_size, bool(member.flag_bits & 1))
    elif ext in {".tar", ".tgz", ".tar.gz"}:
        with tarfile.open(path, "r|" if ext == ".tar" else "r|gz") as archive:
            for count, member in enumerate(archive, 1):
                safe_name(member.name)
                if count > 300 or member.offset_data + member.size > MAX_EXPANDED + 1024 * 1024:
                    raise ValueError("压缩包目录/展开超限")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("拒绝压缩包链接或特殊文件")
                if member.isfile():
                    add(member.name, member.size)
    elif ext == ".gz":
        size = 0
        with gzip.open(path, "rb") as stream:
            while block := stream.read(65536):
                size += len(block)
                if size > MAX_EXPANDED:
                    raise ValueError("GZ展开超限")
        add(Path(original_name).name[:-3] or "content.txt", size)
    else:
        raise ValueError("不支持此压缩格式")
    return {"kind": "archive", "entries": entries, "expanded_bytes": total}


def extract_member(path, ext, member_name, destination, original_name="", limit=20 * 1024 * 1024):
    member_name = safe_name(member_name)
    listing = catalog(path, ext, original_name)
    entry = next((i for i in listing["entries"] if i["name"] == member_name), None)
    if entry is None or entry["encrypted"] or entry["size"] > limit:
        raise ValueError("成员不存在、已加密或超过文件限制")
    if (
        destination.parent.resolve() != path.parent.resolve()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ValueError("成员目标路径无效")

    def copy(stream):
        total = 0
        with destination.open("xb") as output:
            destination.chmod(0o600)
            while block := stream.read(65536):
                total += len(block)
                if total > limit:
                    raise ValueError("成员实际展开超限")
                output.write(block)
        if total != entry["size"]:
            raise ValueError("成员实际大小与目录不符")

    try:
        if ext == ".zip":
            with zipfile.ZipFile(path) as archive:
                member = next(i for i in archive.infolist() if safe_name(i.filename) == member_name)
                with archive.open(member) as stream:
                    copy(stream)
        elif ext == ".gz":
            with gzip.open(path, "rb") as stream:
                copy(stream)
        else:
            with tarfile.open(path, "r|" if ext == ".tar" else "r|gz") as archive:
                for member in archive:
                    if member.isfile() and safe_name(member.name) == member_name:
                        with archive.extractfile(member) as stream:
                            copy(stream)
                        break
        if not destination.is_file():
            raise ValueError("未提取到成员")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
