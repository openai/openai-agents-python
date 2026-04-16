from pydantic import BaseModel
try:
    from agents import Agent
except ImportError:
    import sys
    sys.path.insert(0, 'src')
    from agents import Agent

# Agent for generating compliance reports
REPORT_PROMPT = (
    "You are a compliance reporting specialist. Your task is to generate comprehensive "
    "AML compliance reports for regulators and internal stakeholders. "
    "\n\n"
    "Given screening results, risk assessments, and alert data, create a structured report including: "
    "1. Executive summary "
    "2. Customer screening results "
    "3. Risk assessment summary "
    "4. Suspicious activity alerts "
    "5. Recommended actions "
    "6. Compliance status "
    "\n\n"
    "Use professional regulatory language. Ensure all findings are clearly documented "
    "with supporting evidence. Flag any items requiring immediate attention."
)


class ComplianceReport(BaseModel):
    """AML compliance report"""
    
    report_id: str
    """Unique report identifier"""
    
    report_date: str
    """Date of report generation"""
    
    customer_id: str
    """Customer being reported on"""
    
    executive_summary: str
    """High-level summary of findings"""
    
    screening_summary: str
    """Summary of screening results"""
    
    risk_assessment: str
    """Risk assessment summary"""
    
    alerts_summary: str
    """Summary of any alerts"""
    
    compliance_status: str
    """Overall compliance status: 'compliant', 'review_required', or 'non_compliant'"""
    
    recommended_actions: list[str]
    """List of recommended actions"""
    
    next_review_date: str
    """Recommended date for next review"""
    
    full_report: str
    """Complete detailed report text"""


report_agent = Agent(
    name="ReportAgent",
    instructions=REPORT_PROMPT,
    output_type=ComplianceReport,
)
