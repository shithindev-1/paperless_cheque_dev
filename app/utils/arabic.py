"""Arabic text helpers: Arabic-Indic digits and a rough Arabic -> English spelling (no I/O)."""

import re

# ٠-٩ (Arabic-Indic) and ۰-۹ (Eastern / Persian) -> 0-9; Arabic decimal / thousands separators
_DIGITS = str.maketrans({
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
    "٫": ".", "٬": ",", "،": ",",
})


def western_digits(text: str) -> str:
    """'١٠/١١/٢٠٢٦' -> '10/11/2026', '٦٬٢٥٠٫٠٠' -> '6,250.00'. Other characters are kept."""
    return text.translate(_DIGITS)


# Each letter as it is usually spelled in English names. Long vowels / hamza seats become vowels,
# which the sound comparison drops anyway; ع is usually not written in English (عمر -> Omar).
_LETTERS = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ء": "", "ؤ": "u", "ئ": "i", "ى": "a",
    "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "dh",
    "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "ع": "", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n",
    "ه": "h", "ة": "a", "و": "w", "ي": "y", "ـ": "",
}
_DIACRITICS = re.compile(r"[ً-ٰٟ]")  # short-vowel marks, shadda, sukun


def arabic_to_latin(text: str) -> str:
    """Rough English spelling of an Arabic name, for sound comparison only.

    'محمد علي' -> 'mhmd ly'; 'عبدالله' -> 'bdallah'. Word-final ه / ة is spelled 'ah'
    (Abdullah, Fatimah), which the sound comparison treats like 'a' (Abdulla, Fatima).
    """
    words = []
    for word in _DIACRITICS.sub("", text).split():
        latin = "".join(_LETTERS.get(ch, ch) for ch in word[:-1])
        last = word[-1]
        latin += "ah" if last in "هة" else _LETTERS.get(last, last)
        words.append(latin)
    return " ".join(words)
