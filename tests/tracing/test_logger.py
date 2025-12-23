from agents.tracing import logger as tracing_logger


def test_tracing_logger_is_configured() -> None:
    assert tracing_logger.logger.name == "openai.agents.tracing"
