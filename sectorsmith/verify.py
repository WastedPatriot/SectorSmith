"""Shared verification verdict carried in every job result as ``result["verify"]``.

Shape: {"status": "passed" | "problems" | "failed" | "not_run", "summary": str, "details": [str, ...]}
"""
from __future__ import annotations

PASSED, PROBLEMS, FAILED, NOT_RUN = "passed", "problems", "failed", "not_run"
STATUSES = (PASSED, PROBLEMS, FAILED, NOT_RUN)
LABELS = {PASSED: "Verified", PROBLEMS: "Passed with problems", FAILED: "Failed", NOT_RUN: "Not verified"}


def verdict(status: str, summary: str, details=None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown verify status {status!r}")
    return {"status": status, "summary": summary, "details": [str(d) for d in (details or [])]}


def not_run(summary: str = "Not verified") -> dict:
    return verdict(NOT_RUN, summary)


def label(v: dict | None) -> str:
    return LABELS.get((v or {}).get("status", NOT_RUN), LABELS[NOT_RUN])
