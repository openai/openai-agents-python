"""Keep the released Agents policy JSON independent of provider value-type changes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from vercel import sandbox as provider


class NetworkTransformer(BaseModel):
    headers: dict[str, str] | None = None


class NetworkPolicyRule(BaseModel):
    transform: list[NetworkTransformer] | None = None


class NetworkPolicySubnets(BaseModel):
    allow: list[str] | None = None
    deny: list[str] | None = None


class NetworkPolicyCustom(BaseModel):
    allow: list[str] | dict[str, list[NetworkPolicyRule]]
    subnets: NetworkPolicySubnets | None = None


NetworkPolicy = Literal["allow-all", "deny-all"] | NetworkPolicyCustom


def to_provider(policy: NetworkPolicy | None) -> provider.NetworkPolicy | None:
    if policy is None:
        return None
    if policy == "allow-all":
        return provider.NetworkPolicy.allow_all()
    if policy == "deny-all":
        return provider.NetworkPolicy.deny_all()
    assert isinstance(policy, NetworkPolicyCustom)
    allow: dict[str, tuple[provider.NetworkPolicyRule, ...]]
    if isinstance(policy.allow, list):
        allow = {domain: () for domain in policy.allow}
    else:
        allow = {
            domain: tuple(
                provider.NetworkPolicyRule(
                    transform=tuple(
                        provider.NetworkPolicyTransform(headers=transform.headers)
                        for transform in rule.transform or ()
                    )
                )
                for rule in rules
            )
            for domain, rules in policy.allow.items()
        }
    subnets = (
        provider.NetworkPolicySubnets(allow=policy.subnets.allow, deny=policy.subnets.deny)
        if policy.subnets is not None
        else None
    )
    return provider.NetworkPolicy.custom(allow, subnets=subnets)
