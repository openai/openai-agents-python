"""Positive public typing contract; checked with focused mypy during adapter work."""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_text_message
from a2a.types import a2a_pb2 as p
from pydantic import BaseModel

from agents.a2a import A2AEndpoint, A2AJobProvider
from agents.experimental.deferred import JobClaim, JobProvider


class Question(BaseModel):
    question: str


async def authorize(claim: JobClaim, operation: str) -> None:
    pass


async def authorization() -> str:
    return "synthetic-token"


async def reserve(claim: JobClaim, operation: str, action_id: str) -> bool:
    return True


def encode(arguments: Question) -> p.Message:
    return new_text_message(arguments.question, role=p.ROLE_USER)


def typed_provider(endpoint: A2AEndpoint) -> JobProvider[Question]:
    return A2AJobProvider(
        endpoint,
        authorize=authorize,
        authorization=authorization,
        reserve=reserve,
        encode=encode,
        project=lambda task: {"answer": "synthetic"},
    )
