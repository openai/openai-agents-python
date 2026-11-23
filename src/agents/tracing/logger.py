import logging

from .._redaction import current_redactor

logger = logging.getLogger("openai.agents.tracing")
logger.addFilter(lambda record: current_redactor() is None)
