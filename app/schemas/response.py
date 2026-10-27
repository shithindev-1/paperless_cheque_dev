"""API response models."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_serializer

from app.schemas.cheque import ChequeEntity


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def total(cls, results: list["FileResult"]) -> "TokenUsage":
        return sum((r.usage for r in results if r.usage), cls())

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class FileResult(BaseModel):
    file_name: str
    status: Literal["success", "error"]
    cheques: list[ChequeEntity] = []
    error: Optional[str] = None

    # Internal only — written to logs, never returned to the client
    error_detail: Optional[str] = Field(None, exclude=True)
    usage: Optional[TokenUsage] = Field(None, exclude=True)
    processing_time_ms: Optional[int] = Field(None, exclude=True)

    @model_serializer(mode="wrap")
    def _omit_empty_error(self, handler):
        data = handler(self)
        if data.get("error") is None:
            data.pop("error", None)
        return data

    def log_entry(self) -> dict:
        """Full record for the results log, including internal fields."""
        return {
            **self.model_dump(mode="json"),
            "error_detail": self.error_detail,
            "usage": self.usage.model_dump() if self.usage else None,
            "processing_time_ms": self.processing_time_ms,
        }


class BatchResponse(BaseModel):
    results: list[FileResult]
