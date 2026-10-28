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
    assert not needs_processing([ChequeOcr(rotation_degrees=0, needs_crop=False)])
    assert not needs_processing([ChequeOcr(rotation_degrees=0.4)])  # tiny tilt ignored
    assert needs_processing([ChequeOcr(rotation_degrees=90)])
    assert needs_processing([ChequeOcr(rotation_degrees=-3)])
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
    cheque = ChequeOcr(page=1, bounding_box=[0, 0, 500, 1000], rotation_degrees=90, needs_crop=True)
    out = cv2.imdecode(np.frombuffer(
        process_cheque(jpeg, "image/jpeg", cheque, padding_percent=0, dpi=100, quality=90),
        np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (800, 200)  # 200x800 crop, turned sideways

    doc = pymupdf.open()
    doc.new_page(width=600, height=300)
    pdf = doc.tobytes()
    page = load_page(pdf, "application/pdf", page=5, dpi=72)  # out-of-range page -> last page
    assert page.shape[:2] == (300, 600)


def test_undecodable_image_raises():
    with pytest.raises(ImageProcessingError):
        load_page(b"not an image", "image/jpeg", 1, 100)
