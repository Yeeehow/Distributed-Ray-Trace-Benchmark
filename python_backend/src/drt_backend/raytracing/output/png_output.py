"""Minimal lossless RGB8 PNG writer using only Python's standard library."""

import os
from os import PathLike
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO
import zlib

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import RenderValidationError


def _chunk(stream: BinaryIO, kind: bytes, payload: bytes) -> None:
    stream.write(struct.pack(">I", len(payload)))
    stream.write(kind)
    stream.write(payload)
    stream.write(struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def save_rgb_png(image: NDArray[np.uint8], path: str | PathLike[str]) -> None:
    """Publish a complete RGB8/filter-0 PNG without overwriting any existing path.

    Parent must exist and the filename must end in .png. A same-directory temporary
    file is flushed/fsynced, then atomically linked to the new destination and
    removed. Linking provides no-clobber publication even if a destination races
    the initial existence check. Unsupported filesystems fail clearly, with cleanup.
    No gamma/ICC/color-management metadata or undeclared image library is used.
    """
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
            or image.shape[2] != 3 or not 0 < image.shape[0] <= 0x7fffffff
            or not 0 < image.shape[1] <= 0x7fffffff):
        raise RenderValidationError("PNG input must be nonempty uint8 (height,width,3) within PNG dimension limits.")
    requested = Path(path)
    if requested.suffix.lower() != ".png":
        raise RenderValidationError("PNG output path must end in .png.")
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(f"PNG destination already exists: {requested}")
    target = requested.resolve()
    if not target.parent.is_dir():
        raise FileNotFoundError(f"PNG output parent does not exist: {target.parent}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".drt-png-", suffix=".tmp",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(b"\x89PNG\r\n\x1a\n")
            height, width = image.shape[:2]
            _chunk(stream, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            compressor = zlib.compressobj(level=6)
            for row in image:
                compressed = compressor.compress(b"\x00" + row.tobytes(order="C"))
                if compressed:
                    _chunk(stream, b"IDAT", compressed)
            _chunk(stream, b"IDAT", compressor.flush())
            _chunk(stream, b"IEND", b"")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
