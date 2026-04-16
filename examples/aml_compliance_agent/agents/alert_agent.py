from pydantic import BaseModel
try:
    from agents import Agent
except ImportError:
    import sys
    sys.path.insert(0, 'src')
    from agents import Agent

# Agent for detecting suspicious transactions and activities
ALERT_PROMPT = (
    "You are a transaction monitoring specialist focused on detecting suspicious "
    "activities that may indicate money laundering, terrorist financing, or other "
    "financial crimes. "
    "\n\n"
    "Analyze transaction patterns and flag suspicious activities including: "
    "1. Unusual transaction amounts or frequencies "
    "2. Structuring (transactions just below reporting thresholds) "
    "3. Rapid movement of funds (layering) "
    "4. Transactions with high-risk jurisdictions "
    "5. Inconsistent activity with customer profile "
    "6. Round-number transactions "
    "7. Transactions involving cash-intensive businesses "
    "\n\n"
    "Provide clear alerts with severity levels and recommended actions."
)


class Alert(BaseModel):
    """Suspicious activity alert"""
    
    alert_id: str
    """Unique alert identifier"""
    
    customer_id: str
    """Customer associated with the alert"""
    
    alert_type: str
    """Type of suspicious activity detected"""
    
    severity: str
    """Alert severity: 'low', 'medium', 'high', or 'critical'"""
    
    description: str
    """Detailed description of the suspicious activity"""
    
    involved_transactions: list[str]
    """List of transaction IDs or descriptions"""
    
    red_flags: list[str]
    """List of identified red flags"""
    
    recommended_action: str
    """Recommended action: 'monitor', 'investigate', 'report', or 'escalate'"""
    
    confidence_score: float
    """Confidence score 0.0-1.0"""


alert_agent = Agent(
    name="AlertAgent",
    instructions=ALERT_PROMPT,
    output_type=Alert,
)
