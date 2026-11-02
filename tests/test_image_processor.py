import cv2
import pymupdf
import numpy as np
import pytest

from app.schemas import ChequeOcr
from app.services.image_processor import (
    ImageProcessingError,
    crop,
    load_page,
    needs_processing,
    process_cheque,
    rotate,
)


def _canvas(h=400, w=800) -> np.ndarray:
    return np.full((h, w, 3), 255, dtype=np.uint8)


def test_needs_processing():
    assert not needs_processing([ChequeOcr(text_top="up", needs_crop=False)])
    assert not needs_processing([ChequeOcr(tilt_degrees=0.4)])  # tiny tilt ignored
    assert needs_processing([ChequeOcr(text_top="left")])
    assert needs_processing([ChequeOcr(tilt_degrees=-3)])
    assert needs_processing([ChequeOcr(needs_crop=True)])
    assert needs_processing([ChequeOcr(), ChequeOcr()])  # several cheques in one file


def test_crop_uses_normalised_box_with_padding():
    image = _canvas(1000, 2000)
    out = crop(image, [100, 250, 600, 750], padding_percent=0)
    assert out.shape[:2] == (500, 1000)
    padded = crop(image, [100, 250, 600, 750], padding_percent=10)
    assert padded.shape[0] > 500 and padded.shape[1] > 1000


def test_crop_clamps_and_rejects_bad_boxes():
    image = _canvas()
    assert crop(image, None, 2).shape == image.shape
    assert crop(image, [-50, -50, 1200, 1200], 0).shape == image.shape
    with pytest.raises(ImageProcessingError):
        crop(image, [500, 500, 400, 600], 0)


def test_rotate_quarter_turns_are_clockwise():
    image = _canvas(100, 300)
    image[0:10, 0:10] = (0, 0, 255)  # red marker top-left
    turned = rotate(image, 90)
    assert turned.shape[:2] == (300, 100)
    assert tuple(turned[2, -3]) == (0, 0, 255)  # top-left moves to top-right
    assert rotate(image, 270).shape[:2] == (300, 100)
    assert rotate(image, -90).shape[:2] == (300, 100)
    assert rotate(image, 180).shape[:2] == (100, 300)
    assert rotate(image, 0).shape == image.shape


def test_rotate_small_tilt_expands_canvas():
    image = _canvas(100, 300)
    tilted = rotate(image, 5)
    assert tilted.shape[0] > 100 and tilted.shape[1] > 300
    assert rotate(image, 0.5).shape == image.shape


def test_process_image_and_pdf():
    image = _canvas(400, 800)
    jpeg = cv2.imencode(".jpg", image)[1].tobytes()
    cheque = ChequeOcr(page=1, bounding_box=[0, 0, 500, 1000], text_top="left", needs_crop=True)
    pdf_out = process_cheque(jpeg, "image/jpeg", cheque, padding_percent=0, dpi=100, quality=90)
    assert pdf_out.startswith(b"%PDF")
    out = load_page(pdf_out, "application/pdf", page=1, dpi=100)
    assert abs(out.shape[0] - 800) <= 2 and abs(out.shape[1] - 200) <= 2  # 200x800 crop, turned sideways

    doc = pymupdf.open()
    doc.new_page(width=600, height=300)
    pdf = doc.tobytes()
    page = load_page(pdf, "application/pdf", page=5, dpi=72)  # out-of-range page -> last page
    assert page.shape[:2] == (300, 600)


def test_undecodable_image_raises():
    with pytest.raises(ImageProcessingError):
        load_page(b"not an image", "image/jpeg", 1, 100)


def test_tilt_is_measured_from_the_image():
    from app.services.image_processor import measure_tilt, rotate

    image = np.full((400, 900, 3), 255, np.uint8)
    for y in (80, 160, 240, 320):
        cv2.line(image, (60, y), (840, y), (0, 0, 0), 3)
    assert abs(measure_tilt(image)) < 0.5
    tilted = rotate(image, 6)  # clockwise -> lines slope down to the right
    assert 5 < measure_tilt(tilted) < 7
    assert measure_tilt(np.full((400, 900, 3), 255, np.uint8)) is None  # nothing to measure
