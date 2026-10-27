"""Application exceptions.

`message` is the only text ever returned to the client. Internal details
(e.g. OCR model errors) are logged, never exposed.
"""

from typing import Optional


class AppError(Exception):
    status_code: int = 500
    message: str = "Something went wrong. Please try again later."

    def __init__(self, message: Optional[str] = None) -> None:
        if message:
            self.message = message
        super().__init__(self.message)


class InvalidRequestError(AppError):
    status_code = 400
    message = "Invalid request."


class FileValidationError(AppError):
    status_code = 400
    message = "Invalid file."


class FileDownloadError(AppError):
    status_code = 400
    message = "Unable to download the file."


class ExtractionError(AppError):
    status_code = 502
    message = "Unable to process this file. Please try again later."
