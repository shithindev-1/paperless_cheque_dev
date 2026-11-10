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


class AuthenticationError(AppError):
    status_code = 401
    message = "Invalid or missing API key."


class InvalidRequestError(AppError):
    status_code = 400
    message = "Invalid request."


class InvalidPaymentsError(AppError):
    status_code = 422
    message = ("Invalid 'payments'. Send a JSON list of payments, each with Amount and a valid "
               "DueDate, e.g. [{\"Amount\": 9333, \"DueDate\": \"2026-01-01\"}].")


class InvalidChequeInfoError(AppError):
    status_code = 422
    message = ("Invalid 'cheque_info'. Send a JSON object, e.g. {\"ChequepayeeName\": \"...\", "
               "\"ChequeHolderName\": \"...\", \"ChequeHolderNameAr\": \"...\", \"ChequeHolderIBAN\": \"...\"}.")


class ChequeInfoInPaymentError(AppError):
    status_code = 422
    message = "Send cheque_info once at the top level of the request, not inside a payment."


class FileValidationError(AppError):
    status_code = 400
    message = "Invalid file."


class FileDownloadError(AppError):
    status_code = 400
    message = "Unable to download the file."


class ExtractionError(AppError):
    status_code = 502
    message = "Unable to process this file. Please try again later."
