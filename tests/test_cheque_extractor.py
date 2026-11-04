import asyncio

from app.schemas import ChequeExtraction, TokenUsage
from app.services.cheque_extractor import ChequeExtractor
from app.utils.files import UploadedFile

WORDS = "Fourteen Thousand Only"


def _extractor(settings, *readings):
    """ChequeExtractor whose model call returns the given readings in order (no API)."""
    settings.min_image_sharpness = 0  # the test image is blank
    extractor = object.__new__(ChequeExtractor)
    extractor.settings = settings
    extractor._semaphore = asyncio.Semaphore(1)
    calls = []

    async def read(file):
        calls.append(file.name)
        return ChequeExtraction(cheques=readings[len(calls) - 1]), TokenUsage(total_tokens=10)

    extractor._read = read
    return extractor, calls


def _file():
    return UploadedFile("c.jpg", b"\xff\xd8\xff fake", "image/jpeg")


async def test_empty_reading_is_read_again_and_fuller_one_kept(settings):
    empty = [{"amount_in_words": WORDS}]
    full = [{"cheque_date": "10-08-2027", "cheque_amount": "14000", "amount_in_words": WORDS}]
    extractor, calls = _extractor(settings, empty, full)
    result = await extractor.extract_one(_file())
    assert len(calls) == 2
    assert result.cheques[0].cheque_amount == 14000 and result.usage.total_tokens == 20


async def test_normal_reading_is_not_repeated(settings):
    full = [{"cheque_date": "10-08-2027", "cheque_amount": "14000", "amount_in_words": WORDS}]
    extractor, calls = _extractor(settings, full)
    await extractor.extract_one(_file())
    assert len(calls) == 1


async def test_unclear_date_and_amount_is_an_answer_not_a_failure(settings):
    unclear = [{"amount_in_words": WORDS, "unclear_fields": ["cheque_date", "cheque_amount"]}]
    extractor, calls = _extractor(settings, unclear)
    await extractor.extract_one(_file())
    assert len(calls) == 1


async def test_first_reading_kept_when_retry_is_no_better(settings):
    empty = [{"amount_in_words": WORDS}]
    extractor, calls = _extractor(settings, empty, [])
    result = await extractor.extract_one(_file())
    assert len(calls) == 2 and result.cheques[0].amount_in_words == WORDS
