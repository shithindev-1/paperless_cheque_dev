"""Crop and straighten cheque images with OpenCV (pure functions, no I/O).

Coordinates come from the OCR model: a bounding box normalised to 0-1000 in the
original page / image, and a clockwise rotation in degrees. The box is cropped
first, then the crop is rotated, so the box always refers to the unrotated page.
"""

from typing import Optional

import cv2
import pymupdf
import numpy as np

from app.schemas import ChequeOcr

_SCALE = 1000
_MIN_TILT_DEGREES = 1.0  # smaller tilts are left alone
_WHITE = (255, 255, 255)


class ImageProcessingError(Exception):
    """The page could not be decoded or the layout is unusable."""


def needs_processing(cheques: list[ChequeOcr]) -> bool:
    """Process when one file holds several cheques, or the model asks to crop / rotate."""
    if len(cheques) > 1:
        return True
    return any(c.needs_crop or abs(_normalise(c.rotation_degrees)) >= _MIN_TILT_DEGREES
               for c in cheques)


def load_page(data: bytes, mime_type: str, page: int, dpi: int) -> np.ndarray:
    """Decode an image, or render one PDF page, to a BGR array."""
    if mime_type == "application/pdf":
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                index = min(max(page, 1), doc.page_count) - 1
                pix = doc[index].get_pixmap(dpi=dpi, alpha=False)
                rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as exc:
            raise ImageProcessingError(f"Cannot render PDF page {page}: {exc}") from exc

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageProcessingError(f"Cannot decode {mime_type} image")
    return image


def crop(image: np.ndarray, box: Optional[list[int]], padding_percent: float) -> np.ndarray:
    """Crop to the normalised [ymin, xmin, ymax, xmax] box plus padding. No box = whole image."""
    if not box or len(box) != 4:
        return image
    h, w = image.shape[:2]
    ymin, xmin, ymax, xmax = (max(0, min(_SCALE, int(v))) for v in box)
    if ymax <= ymin or xmax <= xmin:
        raise ImageProcessingError(f"Invalid bounding box {box}")

    pad_y = (ymax - ymin) * padding_percent / 100
    pad_x = (xmax - xmin) * padding_percent / 100
    top = max(0, int((ymin - pad_y) * h / _SCALE))
    bottom = min(h, int(round((ymax + pad_y) * h / _SCALE)))
    left = max(0, int((xmin - pad_x) * w / _SCALE))
    right = min(w, int(round((xmax + pad_x) * w / _SCALE)))
    if bottom - top < 2 or right - left < 2:
        raise ImageProcessingError(f"Bounding box {box} is too small")
    return image[top:bottom, left:right]


def rotate(image: np.ndarray, degrees_clockwise: Optional[float]) -> np.ndarray:
    """Rotate clockwise: exact quarter turns, then any remaining tilt (canvas expanded, white fill)."""
    degrees = _normalise(degrees_clockwise)
    quarters = round(degrees / 90)
    tilt = degrees - quarters * 90

    turn = {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if quarters % 4:
        image = cv2.rotate(image, turn[quarters % 4])

    if abs(tilt) < _MIN_TILT_DEGREES:
        return image
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), -tilt, 1.0)  # OpenCV: positive = counter-clockwise
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = int(h * sin + w * cos), int(h * cos + w * sin)
    matrix[0, 2] += new_w / 2 - w / 2
    matrix[1, 2] += new_h / 2 - h / 2
    return cv2.warpAffine(image, matrix, (new_w, new_h),
                          flags=cv2.INTER_CUBIC, borderValue=_WHITE)


def encode_jpeg(image: np.ndarray, quality: int) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ImageProcessingError("Cannot encode JPEG")
    return buffer.tobytes()


def process_cheque(
    data: bytes, mime_type: str, cheque: ChequeOcr,
    *, padding_percent: float, dpi: int, quality: int,
) -> bytes:
    """Return the cropped, upright cheque as JPEG bytes."""
    page = load_page(data, mime_type, cheque.page or 1, dpi)
    return encode_jpeg(rotate(crop(page, cheque.bounding_box, padding_percent),
                              cheque.rotation_degrees), quality)


def _normalise(degrees: Optional[float]) -> float:
    """Map any angle to (-180, 180]."""
    d = float(degrees or 0) % 360
    return d - 360 if d > 180 else d
