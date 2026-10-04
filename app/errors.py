"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, details: object = None):
        super().__init__(message)
        self.message = message
        self.details = details


class UnprocessableError(DomainError):
    status_code = 422
    code = "validation_error"


class ConflictError(DomainError):
    status_code = 409
    code = "request_id_conflict"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"
