"""Full artifact checksums with measured progress and mutation detection."""
import hashlib
from pathlib import Path

from app.backend.pipeline.fsio import file_identity as identity


def checksum(path, expected, progress=None):
    total, completed = expected[1], 0
    digest = hashlib.sha256()
    def report():
        if progress:
            progress({"phase": "verifying", "completed": completed, "total": total,
                      "unit": "bytes", "percent": completed * 100 / total if total else 100})
    report()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
            completed += len(block)
            report()
    if identity(path) != expected or completed != total:
        raise ValueError("The selected model artifact changed during verification. Prepare it again.")
    return digest.hexdigest()
