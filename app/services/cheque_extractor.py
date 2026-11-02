"""Cheque extraction service: one or many files -> structured cheque data."""

import asyncio
import logging
import time
from collections import defaultdict

from google.genai import types

from app.core.config import Settings
from app.core.exceptions import ExtractionError
from app.core.timezones import now_uae
from app.prompts import EXTRACTION_PROMPT, SYSTEM_INSTRUCTION
from app.schemas import ChequeEntity, ChequeExtraction, ChequeOcr, FileResult
from app.schemas.cheque import ChequeIssue, IssueCode
from app.services.gemini import build_generation_config, generate_structured, get_gemini_client
from app.services.image_processor import load_page, sharpness
from app.utils.dates import parse_date
from app.utils.files import UploadedFile

logger = logging.getLogger(__name__)

UNREADABLE = "too blurry or low resolution to read"


class ChequeExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = get_gemini_client()
        self.config = build_generation_config(
            settings,
            system_instruction=SYSTEM_INSTRUCTION,
            response_schema=ChequeExtraction,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)

    async def extract_one(self, file: UploadedFile) -> FileResult:
        async with self._semaphore:
            start = time.perf_counter()
            logger.info("Processing %s (%s, %.1f KB)",
                        file.name, file.mime_type, len(file.data) / 1024)
            try:
                result, usage = await generate_structured(
                    client=self.client,
                    model=self.settings.gemini_model,
                    contents=[
                        types.Part.from_bytes(data=file.data, mime_type=file.mime_type),
                        EXTRACTION_PROMPT.replace("{today}", now_uae().strftime("%d-%m-%Y")),
                    ],
                    config=self.config,
                    response_model=ChequeExtraction,
                    max_retries=self.settings.gemini_max_retries,
                )
                result.cheques = await self._drop_unreadable(file, result.cheques)
                elapsed = _elapsed_ms(start)
                logger.info(
                    "Done %s | cheques=%d | tokens: prompt=%d output=%d thinking=%d total=%d | %d ms",
                    file.name, len(result.cheques), usage.prompt_tokens, usage.output_tokens,
                    usage.thinking_tokens, usage.total_tokens, elapsed,
                )
                return FileResult(
                    file_name=file.name,
                    status="success",
                    cheques=result.cheques,
                    processing_time_ms=elapsed,
                    usage=usage,
                )
            except Exception as exc:  # one bad file must not fail the whole batch
                # Full Gemini/internal error goes to the log; the client gets a general message
                logger.exception("Extraction failed for %s", file.name)
                return FileResult(
                    file_name=file.name,
                    status="error",
                    error=ExtractionError.message,
                    error_detail=f"{type(exc).__name__}: {exc}",
                    processing_time_ms=_elapsed_ms(start),
                )

    async def _drop_unreadable(self, file: UploadedFile, cheques: list[ChequeOcr]) -> list[ChequeOcr]:
        """Measured, not model-judged: a page too blurred / low-res to read keeps no values at all.

        The model can invent dates and amounts on images nobody can read, so the decision is
        made from the image's sharpness instead.
        """
        threshold = self.settings.min_image_sharpness
        if threshold <= 0:
            return cheques
        scores = {}
        for page in {c.page or 1 for c in cheques} or {1}:
            try:
                image = await asyncio.to_thread(load_page, file.data, file.mime_type, page,
                                                self.settings.pdf_render_dpi)
                scores[page] = sharpness(image)
            except Exception:  # never fail extraction because of the quality check
                logger.warning("Could not measure image quality of %s page %s",
                               file.name, page, exc_info=True)
        blurry = {page for page, score in scores.items() if score < threshold}
        if not blurry:
            return cheques
        logger.info("%s: too blurred / low resolution (sharpness %s < %s); values dropped",
                    file.name, {p: round(scores[p], 1) for p in blurry}, threshold)
        if not cheques:
            return [ChequeOcr(issues=[ChequeIssue(code=IssueCode.IMAGE_INCOMPLETE, detail=UNREADABLE)])]
        return [c.as_unreadable(UNREADABLE) if (c.page or 1) in blurry else c for c in cheques]

    async def extract_many(self, files: list[UploadedFile]) -> list[FileResult]:
        results = list(await asyncio.gather(*(self.extract_one(f) for f in files)))
        infer_installments([c for r in results for c in r.cheques])
        return results


def infer_installments(cheques: list[ChequeEntity]) -> None:
    """Fill missing installment numbers from date order, per service.

    Assumes all cheques in one request belong to the same lease. Only groups of
    2+ dated cheques are numbered; a written installment number is never overwritten.
    """
    groups: dict = defaultdict(list)
    for cheque in cheques:
        if cheque.cheque_date:
            groups[cheque.service].append(cheque)

    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda c: parse_date(c.cheque_date))
        for number, cheque in enumerate(group, start=1):
            if cheque.installment_number is None:
                cheque.installment_number = number


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
