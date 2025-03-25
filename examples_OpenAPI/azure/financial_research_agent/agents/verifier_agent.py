from pydantic import BaseModel

from src.agents import Agent
from src.agents.model_settings import ModelSettings

# Verifier agent checks the final report for issues like inconsistencies or missing sources
INSTRUCTIONS = (
    "You are a fact-checking editor for financial reports. Review the provided report "
    "and identify any potential issues such as: "
    "1. Inconsistent statements or contradictions "
    "2. Financial figures that seem implausible or don't add up "
    "3. Claims that appear unsubstantiated or would benefit from specific sources "
    "4. Areas where important context might be missing "
    "5. Potential bias in the analysis "
    "\n\nProvide specific examples from the text where you identify issues, and "
    "suggest how these could be addressed. If the report appears sound, note that as well."
)


class VerificationResult(str):
    """The verification feedback as text."""


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.2  # Very low temperature for critical assessment
)

verifier_agent = Agent(
    name="VerifierAgent",
    instructions=INSTRUCTIONS,
    output_type=VerificationResult,
    model_settings=azure_settings,
)
