"""Structured outputs the agents must return. Parsing is tolerant but always errs on the safe side."""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Step(BaseModel):
    id: int
    agent: Literal["retriever", "executor"]      # the Planner cannot invent agents
    task: str
    why: str


class Plan(BaseModel):
    summary: str
    reply_type: Literal["vendor_email", "internal_answer"]
    steps: list[Step] = Field(min_length=1)
    escalate: Optional[str] = None


def as_text(item) -> str:
    """Agents sometimes return {"fix": "..."} instead of "..."; keep the words either way."""
    if isinstance(item, dict):
        return "; ".join(str(v) for v in item.values())
    return str(item)


class Check(BaseModel):
    rule: str
    result: Literal["PASS", "FAIL", "N/A"]
    note: str = ""

    @field_validator("rule", "note", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else as_text(v)

    @field_validator("result", mode="before")
    @classmethod
    def _normalise(cls, v):
        """Accept reasonable variations. Anything doubtful counts as FAIL (the safe side)."""
        word = str(v).strip().upper().replace("_", " ")
        if word in ("PASS", "PASSED", "OK", "YES", "TRUE", "COMPLIANT"):
            return "PASS"
        if word in ("N/A", "NA", "NOT APPLICABLE", "SKIP", "SKIPPED"):
            return "N/A"
        return "FAIL"     # FAIL, PARTIAL, WARNING, unknown words ...


class Review(BaseModel):
    verdict: Literal["APPROVED", "REJECTED"]
    checks: list[Check] = []
    fixes: list[str] = []
    needs_human: list[str] = []

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v):
        word = str(v).strip().upper()
        return "APPROVED" if word in ("APPROVED", "APPROVE", "PASS", "PASSED") else "REJECTED"

    @field_validator("fixes", "needs_human", mode="before")
    @classmethod
    def _texts(cls, v):
        if v is None:
            return []
        return [as_text(x) for x in (v if isinstance(v, list) else [v])]
