"""FastAPI dependencies."""

from functools import lru_cache

from app.core.config import get_settings
from app.services.cheque_extractor import ChequeExtractor


@lru_cache
def get_cheque_extractor() -> ChequeExtractor:
    return ChequeExtractor(get_settings())
