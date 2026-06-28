"""Pluggable identity verification and wallet abstractions for agent payments.

This module defines the interfaces for agent authorization before financial
API calls. The verifier is pluggable -- swap in any identity system (DID,
JWT, ZKP, API keys) by implementing IdentityVerifier.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Credential:
    """An operator-issued credential granting an agent specific permissions."""

    agent_id: str
    operator_id: str
    permissions: set[str]
    expiry: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "agent_id": self.agent_id,
            "operator_id": self.operator_id,
            "permissions": sorted(self.permissions),
            "expiry": self.expiry,
            **self.metadata,
        })

    @classmethod
    def from_json(cls, raw: str) -> "Credential":
        data = json.loads(raw)
        return cls(
            agent_id=data["agent_id"],
            operator_id=data["operator_id"],
            permissions=set(data.get("permissions", [])),
            expiry=data.get("expiry", 0),
            metadata={
                k: v
                for k, v in data.items()
                if k not in ("agent_id", "operator_id", "permissions", "expiry")
            },
        )


@dataclass
class VerificationResult:
    """Result of a credential verification check."""

    authorized: bool
    agent_id: str = ""
    permissions: set[str] = field(default_factory=set)
    reason: str = ""


class IdentityVerifier(ABC):
    """Interface for agent identity verification.

    Implement this to plug in any identity system: DID, JWT, ZKP, API keys, etc.
    """

    @abstractmethod
    def verify(self, credential: str) -> VerificationResult:
        """Verify a credential and return the result."""


class StructuralVerifier(IdentityVerifier):
    """Development verifier that checks credential structure only.

    NOT for production -- use a real verifier for deployed systems.
    """

    def verify(self, credential: str) -> VerificationResult:
        try:
            cred = Credential.from_json(credential)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return VerificationResult(
                authorized=False, reason=f"invalid credential: {e}"
            )

        if not cred.expiry or cred.expiry <= 0:
            return VerificationResult(
                authorized=False,
                agent_id=cred.agent_id,
                reason="credential missing required expiry",
            )

        if cred.expiry < time.time():
            return VerificationResult(
                authorized=False,
                agent_id=cred.agent_id,
                reason="credential expired",
            )

        if not cred.permissions:
            return VerificationResult(
                authorized=False,
                agent_id=cred.agent_id,
                reason="no permissions granted",
            )

        return VerificationResult(
            authorized=True,
            agent_id=cred.agent_id,
            permissions=cred.permissions,
        )


def authorize_agent(
    credential: str,
    required_permissions: set[str],
    verifier: IdentityVerifier | None = None,
) -> VerificationResult:
    """Check if an agent is authorized to perform an action.

    Args:
        credential: The agent's credential string.
        required_permissions: Permissions needed for this action.
        verifier: Verifier to use. Defaults to StructuralVerifier.

    Returns:
        VerificationResult indicating whether the agent is authorized.
    """
    v = verifier or StructuralVerifier()
    result = v.verify(credential)

    if not result.authorized:
        return result

    missing = required_permissions - result.permissions
    if missing:
        return VerificationResult(
            authorized=False,
            agent_id=result.agent_id,
            permissions=result.permissions,
            reason=f"missing permissions: {sorted(missing)}",
        )

    return result
