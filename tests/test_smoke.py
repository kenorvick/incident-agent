"""Smoke test: the package and CLI import."""


def test_imports() -> None:
    import incident_agent
    from incident_agent.cli import app

    assert incident_agent is not None
    assert app is not None
