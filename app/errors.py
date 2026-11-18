import uuid

STATUS_BY_CODE = {
    "UNAUTHORIZED": 401,
    "ACCOUNT_BANNED": 403,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "ORDER_STATE_CONFLICT": 409,
    "BOOK_STATE_CONFLICT": 409,
    "BOOK_ALREADY_LOCKED": 409,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "DUPLICATE_REVIEW": 409,
    "INVALID_CREDENTIALS": 401,
    "INVALID_SECURITY_ANSWER": 400,
    "EMAIL_TAKEN": 409,
    "VALIDATION_ERROR": 422,
    "HANDOVER_CODE_MISMATCH": 422,
    "REFUND_WINDOW_CLOSED": 422,
    "PAYMENT_FAILED": 422,
    "NOT_YOUR_RESOURCE": 403,
    "UPLOAD_REJECTED": 422,
    "REPORT_ALREADY_HANDLED": 409,
    "RATE_LIMITED": 429,
    "PAYLOAD_TOO_LARGE": 413,
    "CSRF_REJECTED": 403,
}


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or STATUS_BY_CODE.get(code, 400)
        self.details = details or []
        self.trace_id = uuid.uuid4().hex[:8]

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "trace_id": self.trace_id,
        }


def fail(code: str, message: str, status: int | None = None, details=None):
    raise AppError(code, message, status, details)
