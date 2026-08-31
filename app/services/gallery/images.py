"""Pixel handling for the gallery: validation, thumbnails, and hashing.

Everything here works on bytes and returns bytes or plain values. Nothing in this
module touches the database or the network, so an image can be probed and
thumbnailed without a profile being active -- which is what lets the tests run it
directly and the describer run it on a worker thread.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.files.safety import IMAGE_EXTENSIONS

#: Pillow names formats differently from the extensions people type.
_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


class ImageError(ValueError):
    """The bytes are not an image Neo can work with."""


@dataclass(frozen=True)
class ImageProbe:
    image_format: str
    mime_type: str
    width: int
    height: int


def is_image_mime(mime_type: str | None) -> bool:
    return bool(mime_type) and str(mime_type).lower().startswith("image/")


def is_image_extension(extension: str | None) -> bool:
    return (extension or "").lower() in IMAGE_EXTENSIONS


def _open(content: bytes) -> Image.Image:
    """Decode, or say why not.

    Pillow raises ``DecompressionBombError`` for an image whose declared
    dimensions would allocate absurd amounts of memory. That is a hostile upload,
    not a bug, so it is turned into the same refusal as any other bad file rather
    than a 500.
    """

    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Image.DecompressionBombError as exc:
        raise ImageError("That image declares dimensions too large to open safely.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageError("That file is not a readable image.") from exc
    return image


def probe(content: bytes) -> ImageProbe:
    """Format and dimensions, read from the pixels rather than the filename.

    The avatar path decides an upload is an image by checking that the string
    starts with ``data:image/``. This asks the decoder instead, so a mislabelled
    or disguised file is refused here rather than failing later in the describer.
    """

    image = _open(content)
    fmt = (image.format or "").upper()
    if fmt not in _FORMAT_TO_MIME:
        raise ImageError(f"Unsupported image format: {fmt or 'unknown'}.")
    oriented = ImageOps.exif_transpose(image) or image
    return ImageProbe(
        image_format=fmt,
        mime_type=_FORMAT_TO_MIME[fmt],
        width=oriented.width,
        height=oriented.height,
    )


def _prepare(content: bytes, max_px: int) -> Image.Image:
    """Orient, flatten and downscale -- the shared front half of every derivation."""

    image = ImageOps.exif_transpose(_open(content)) or _open(content)
    if image.mode not in {"RGB", "L"}:
        # A screenshot with transparency composites onto white rather than black:
        # dark-on-transparent text is the common case and must stay readable.
        if image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGBA")
            canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(canvas, image)
        image = image.convert("RGB")
    if max(image.size) > max_px:
        image.thumbnail((max_px, max_px), Image.LANCZOS)
    return image


def make_thumbnail(content: bytes, max_px: int) -> bytes:
    """A small WEBP preview. WEBP because the grid loads hundreds of these."""

    buffer = io.BytesIO()
    _prepare(content, max_px).save(buffer, format="WEBP", quality=82, method=4)
    return buffer.getvalue()


def for_vision(content: bytes, max_px: int) -> str:
    """Base64 JPEG, downscaled to what the vision model will actually use.

    Sending the original is the difference between a describe pass measured in
    seconds and one measured in minutes: a 5 MiB PNG base64-encodes to about
    6.7 MiB of request body, and the model resizes it down anyway.
    """

    buffer = io.BytesIO()
    _prepare(content, max_px).save(buffer, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def perceptual_hash(content: bytes) -> str:
    """64-bit average hash, as 16 hex characters.

    sha256 already catches a byte-identical re-upload. This catches the same
    screenshot re-encoded, resized or re-compressed on its way through a
    messaging app, which sha256 cannot see and a user would still call a
    duplicate.
    """

    image = _prepare(content, 64).convert("L").resize((8, 8), Image.LANCZOS)
    # tobytes() on an 8-bit greyscale image is exactly the 64 pixel values, and
    # unlike getdata() it is not deprecated in Pillow 12 nor missing in 11.
    pixels = list(image.tobytes())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= mean:
            bits |= 1 << index
    return f"{bits:016x}"


def hamming_distance(left: str, right: str) -> int:
    """How far apart two perceptual hashes are, in bits (0 = identical)."""

    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return 64
