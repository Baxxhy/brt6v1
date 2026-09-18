"""Infrastructure failures must never become semantic feedback."""


class LLMUnavailableError(RuntimeError):
    """The current model step did not complete; preserve and resume it later."""


def quota_exhausted(body: str) -> bool:
    """Only explicit account/balance errors disable a credential, not rate limits."""
    text = body.lower()
    return any(marker in text for marker in (
        "insufficient_user_quota", "insufficient_quota", "insufficient_balance",
        "insufficient balance", "quota exhausted", "余额不足", "额度不足",
        "用户额度不足", "credit balance is too low",
    ))
