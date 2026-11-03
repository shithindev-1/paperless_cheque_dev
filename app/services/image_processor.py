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
_MAX_MEASURED_TILT = 20.0  # lines steeper than this are not cheque edges / printed lines
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


def sharpness(image: np.ndarray) -> float:
    """Variance of the Laplacian at a fixed 1000 px width; low = blurred or low resolution."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    gray = cv2.resize(gray, (1000, max(1, round(h * 1000 / w))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def encode_jpeg(image: np.ndarray, quality: int) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ImageProcessingError("Cannot encode JPEG")
    return buffer.tobytes()


def encode_pdf(image: np.ndarray, quality: int, dpi: int) -> bytes:
    """One-page PDF holding the image (JPEG-compressed), sized so it prints at `dpi`."""
    h, w = image.shape[:2]
    with pymupdf.open() as doc:
        page = doc.new_page(width=w * 72 / dpi, height=h * 72 / dpi)
        page.insert_image(page.rect, stream=encode_jpeg(image, quality))
        return doc.tobytes(garbage=3, deflate=True)


def process_cheque(
    data: bytes, mime_type: str, cheque: ChequeOcr,
    *, padding_percent: float, dpi: int, quality: int,
) -> bytes:
    """Return the cropped, upright, straightened cheque as a one-page PDF.

    The quarter turn comes from the model (which way the text points); the fine tilt is
    measured from the image's straight lines, falling back to the model's estimate.
    """
    return process_cheque_detailed(data, mime_type, cheque, padding_percent=padding_percent,
                                   dpi=dpi, quality=quality)[0]


def process_cheque_detailed(
    data: bytes, mime_type: str, cheque: ChequeOcr,
    *, padding_percent: float, dpi: int, quality: int,
) -> tuple[bytes, dict]:
    """process_cheque, plus what was done (for the request log)."""
    page = load_page(data, mime_type, cheque.page or 1, dpi)
    image = rotate(crop(page, cheque.bounding_box, padding_percent), cheque.quarter_turn)
    measured = measure_tilt(image)
    tilt = -measured if measured is not None else (cheque.tilt_degrees or 0.0)
    if abs(tilt) >= _MIN_TILT_DEGREES:
        image = _trim_to_cheque(rotate(image, tilt), image.shape, tilt)
    else:
        tilt = 0.0
    info = {"quarter_turn": cheque.quarter_turn,
            "tilt_measured": round(measured, 2) if measured is not None else None,
            "tilt_applied": round(tilt, 2), "output_px": [image.shape[1], image.shape[0]]}
    return encode_pdf(image, quality, dpi), info


def _trim_to_cheque(rotated: np.ndarray, box_shape: tuple, tilt: float) -> np.ndarray:
    """After straightening, cut away the background corners that the crop box contained.

    A tilted w x h cheque fills a W x H box with W = w*cos + h*sin, H = w*sin + h*cos;
    solving for w, h gives the straight cheque, centred in the rotated image.
    """
    big_h, big_w = box_shape[:2]
    c, s = abs(np.cos(np.radians(tilt))), abs(np.sin(np.radians(tilt)))
    denom = c * c - s * s
    w = int((big_w * c - big_h * s) / denom)
    h = int((big_h * c - big_w * s) / denom)
    if w < 10 or h < 10:
        return rotated
    rh, rw = rotated.shape[:2]
    top, left = max(0, (rh - h) // 2), max(0, (rw - w) // 2)
    return rotated[top:top + h, left:left + w]


def measure_tilt(image: np.ndarray) -> Optional[float]:
    """Angle of the long near-horizontal lines (cheque edges, printed lines) in degrees.

    Positive = the content slopes down to the right (rotated clockwise). None when there
    are too few lines to be sure.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    gray = cv2.resize(gray, (1000, max(1, round(h * 1000 / w))), interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=80, minLineLength=250, maxLineGap=10)
    if lines is None:
        return None
    angles, weights = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if angle > 90:
            angle -= 180
        elif angle < -90:
            angle += 180
        if abs(angle) <= _MAX_MEASURED_TILT:
            angles.append(angle)
            weights.append(np.hypot(x2 - x1, y2 - y1))
    if len(angles) < 2:
        return None
    order = np.argsort(angles)  # length-weighted median: robust to a few stray lines
    cumulative = np.cumsum(np.array(weights)[order])
    return float(np.array(angles)[order][np.searchsorted(cumulative, cumulative[-1] / 2)])


def _normalise(degrees: Optional[float]) -> float:
    """Map any angle to (-180, 180]."""
    d = float(degrees or 0) % 360
    return d - 360 if d > 180 else d
