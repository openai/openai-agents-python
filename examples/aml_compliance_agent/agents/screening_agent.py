from pydantic import BaseModel
try:
    from agents import Agent
except ImportError:
    import sys
    sys.path.insert(0, 'src')
    from agents import Agent

# Agent focused on screening customers against sanctions and watchlists
SCREENING_PROMPT = (
    "You are a compliance screening specialist. Your task is to analyze customer "
    "information and determine if they appear on any sanctions lists, politically "
    "exposed persons (PEP) lists, or adverse media. "
    "\n\n"
    "Given customer details (name, address, date of birth, nationality), you should: "
    "1. Identify potential matches with known sanctions lists (OFAC, UN, EU, etc.) "
    "2. Check for politically exposed persons status "
    "3. Look for any adverse media mentions "
    "4. Provide a clear screening result with confidence level "
    "\n\n"
    "Be thorough but concise. Flag any potential matches for manual review."
)


class ScreeningResult(BaseModel):
    """Result of customer screening against sanctions lists"""
    
    customer_name: str
    """Name of the screened customer"""
    
    sanctions_match: bool
    """Whether potential sanctions list match was found"""
    
    pep_status: str
    """Politically Exposed Person status: 'yes', 'no', or 'unknown'"""
    
    adverse_media_found: bool
    """Whether adverse media mentions were found"""
    
    risk_level: str
    """Overall screening risk: 'low', 'medium', 'high', or 'critical'"""
    
    details: str
    """Detailed explanation of screening findings"""
    
    recommended_action: str
    """Recommended next step: 'clear', 'review', or 'block'"""


screening_agent = Agent(
    name="ScreeningAgent",
    instructions=SCREENING_PROMPT,
    output_type=ScreeningResult,
)
