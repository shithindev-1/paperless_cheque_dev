import pytest

from app.schemas import ChequeOcr
from app.utils.amount_words import words_to_number


@pytest.mark.parametrize("words, expected", [
    ("Six Thousand Six Hundred Sixty Seven Only", 6667),
    ("Five thousand three hundred thirty three only", 5333),
    ("Ten Thousand Dirhams only.", 10000),
    ("Dirhams Five Thousand Only", 5000),
    ("Six thousand and fifty fils", 6000.5),
    ("Five Thousand Dirhams and Twenty Five Fils Only", 5000.25),
    ("Two Thousand Five Hundred and 50/100", 2500.5),
    ("One Lakh Twenty Thousand", 120000),
    ("Sixty-Seven", 67),
    ("Three Hundred and Fifty", 350),
    ("Five thousant three hundered", 5300),  # small handwriting / OCR misspellings
    ("AED 5,000 only", 5000),
])
def test_words_to_number(words, expected):
    assert words_to_number(words) == expected


@pytest.mark.parametrize("words", [None, "", "Only", "xyzzy blah"])
def test_unreadable_words_give_none(words):
    assert words_to_number(words) is None


def test_numbers_from_the_model_arrive_as_text():
    cheque = ChequeOcr(cheque_amount="6,667", installment_number="2", page="1", tilt_degrees="-2.5")
    assert (cheque.cheque_amount, cheque.installment_number, cheque.page, cheque.tilt_degrees) == (6667, 2, 1, -2.5)
    assert ChequeOcr(installment_number="none").installment_number is None


def test_words_figures_check_uses_backend_conversion():
    ok = ChequeOcr(cheque_date="05-03-2027", cheque_amount="6667",
                   amount_in_words="Six Thousand Six Hundred Sixty Seven Only")
    assert ok.issues == []
    bad = ChequeOcr(cheque_date="05-03-2027", cheque_amount="8000",
                    amount_in_words="Six Thousand Six Hundred Sixty Seven Only")
    assert bad.error == ("Amount in words (Six Thousand Six Hundred Sixty Seven Only) "
                         "does not match figures (8,000).")
