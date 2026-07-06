"""Agent 7: API Standards — Consistent response formatting.

Ensures ALL API responses follow the same structure:
{
  "ok": true/false,
  "data": { ... },       // on success
  "error": "message",   // on failure
  "meta": { ... }       // optional metadata
}

Fixes the inconsistency where some endpoints return {"detail": ...}
and others return {"error": ...}.
"""
from typing import Any, Optional, Dict
from fastapi.responses import JSONResponse


def success(data: Any = None, meta: Optional[Dict] = None, status: int = 200) -> JSONResponse:
    """Standard success response."""
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    if meta:
        body["meta"] = meta
    return JSONResponse(content=body, status_code=status)


def error(message: str, status: int = 400, details: Optional[Dict] = None) -> JSONResponse:
    """Standard error response."""
    body = {"ok": False, "error": message}
    if details:
        body["details"] = details
    return JSONResponse(content=body, status_code=status)


def created(data: Any = None) -> JSONResponse:
    """201 Created response."""
    return success(data=data, status=201)


def paginated(items: list, total: int, page: int = 1, per_page: int = 20) -> JSONResponse:
    """Paginated list response."""
    return success(
        data=items,
        meta={
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        },
    )


def accepted(job_id: str, message: str = "Processing") -> JSONResponse:
    """202 Accepted for async operations."""
    return JSONResponse(
        content={"ok": True, "status": "accepted", "job_id": job_id, "message": message},
        status_code=202,
    )


# Common error shortcuts
def not_found(resource: str = "Resource") -> JSONResponse:
    return error(f"{resource} not found", status=404)

def unauthorized(message: str = "Authentication required") -> JSONResponse:
    return error(message, status=401)

def forbidden(message: str = "Access denied") -> JSONResponse:
    return error(message, status=403)

def rate_limited(retry_after: int = 60) -> JSONResponse:
    resp = error("Rate limited. Try again later.", status=429)
    resp.headers["Retry-After"] = str(retry_after)
    return resp

def validation_error(field: str, message: str) -> JSONResponse:
    return error(f"Validation error: {field} - {message}", status=422)
