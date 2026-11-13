"""Execution location is an explicit, non-negotiable server constraint."""

POLICIES = frozenset({"local_only", "cloud_only"})


def execution_policy(value: object) -> str:
    if not isinstance(value, str) or value not in POLICIES:
        raise ValueError("invalid execution policy")
    return value
