from pydantic import BaseModel
try:
    from agents import Agent
except ImportError:
    import sys
    sys.path.insert(0, 'src')
    from agents import Agent

# Agent for assessing customer risk levels
RISK_ASSESSMENT_PROMPT = (
    "You are a risk assessment analyst specializing in AML compliance. "
    "Your task is to evaluate customer risk profiles based on various factors. "
    "\n\n"
    "Consider the following risk factors: "
    "1. Customer type (individual, business, financial institution) "
    "2. Geographic risk (country of residence, nationality) "
    "3. Business activity (industry, transaction patterns) "
    "4. Transaction volume and frequency "
    "5. Source of funds "
    "6. Screening results (sanctions, PEP, adverse media) "
    "\n\n"
    "Assign appropriate risk ratings and provide clear justification. "
    "Recommend enhanced due diligence measures where necessary."
)


class RiskAssessment(BaseModel):
    """Customer risk assessment result"""
    
    customer_id: str
    """Unique customer identifier"""
    
    overall_risk_rating: str
    """Final risk rating: 'low', 'medium', 'high', or 'prohibited'"""
    
    geographic_risk: str
    """Geographic risk level: 'low', 'medium', or 'high'"""
    
    business_risk: str
    """Business/activity risk level: 'low', 'medium', or 'high'"""
    
    customer_risk: str
    """Customer-specific risk level: 'low', 'medium', or 'high'"""
    
    risk_factors: list[str]
    """List of identified risk factors"""
    
    mitigation_measures: list[str]
    """Recommended risk mitigation measures"""
    
    review_frequency: str
    """Recommended review frequency: 'annual', 'biannual', 'quarterly'"""
    
    justification: str
    """Detailed justification for the risk rating"""


risk_assessment_agent = Agent(
    name="RiskAssessmentAgent",
    instructions=RISK_ASSESSMENT_PROMPT,
    output_type=RiskAssessment,
)
