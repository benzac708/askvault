MOCK_REPLY = "MOCK-REPLY: offline provider. Retrieval and citations are real."


class MockLLM:
    """Deterministic stand-in so CI never calls a paid API.

    It records every call, which is what the tests actually assert against: the
    interesting claim is that the right context reached the provider, not that
    the provider said something clever.
    """

    model = "mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return MOCK_REPLY
