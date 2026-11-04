"""Bounded local file inputs and post previews; file bytes never enter the ledger."""

import hashlib
import os
from pathlib import Path
import stat


def _source(path, maximum):
    source = Path(path).expanduser().resolve(strict=True)
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError(f"Attachment must be a regular, nonempty file of at most {maximum} bytes")
        return source, os.fdopen(fd, "rb"), info.st_size
    except BaseException:
        os.close(fd)
        raise


def describe_source(path, maximum):
    source, stream, size = _source(path, maximum)
    if any(ord(character) < 32 or ord(character) == 127 for character in source.name):
        stream.close()
        raise ValueError("Attachment filename contains control characters")
    digest = hashlib.sha256()
    read = 0
    with stream:
        while chunk := stream.read(65536):
            read += len(chunk)
            if read > maximum:
                raise ValueError("Attachment changed size while being inspected")
            digest.update(chunk)
    if read != size:
        raise ValueError("Attachment changed size while being inspected")
    return {"path": str(source), "filename": source.name, "size": size,
            "sha256": digest.hexdigest()}


def source_bytes(source):
    """Hash and send the same buffer; changed local input fails before HTTP."""
    _, stream, size = _source(source["path"], source["size"])
    with stream:
        data = stream.read(source["size"] + 1)
    if (size != source["size"] or len(data) != source["size"]
            or hashlib.sha256(data).hexdigest() != source["sha256"]):
        raise ValueError("Attachment source changed since the send was prepared")
    return data


def preview(post, limit=160):
    text = " ".join(post.get("message", "").split())
    if text:
        return text[:limit]
    files = post.get("files") or []
    names = [" ".join(item.get("name", "").split()) for item in files]
    names = [name for name in names if name]
    count = len(post.get("file_ids") or files)
    if not count:
        return ""
    label = "attachment" if count == 1 else "attachments"
    detail = ": " + ", ".join(names) if names else ""
    return f"[{count} {label}{detail}]"[:limit]
