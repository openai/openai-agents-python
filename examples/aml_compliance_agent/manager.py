"""
AML Compliance Manager - Orchestrates the compliance workflow
"""

from __future__ import annotations

import asyncio
try:
    from agents import Runner, trace, gen_trace_id
except ImportError:
    import sys
    sys.path.insert(0, 'src')
    from agents import Runner, trace, gen_trace_id

from .agents.screening_agent import screening_agent, ScreeningResult
from .agents.risk_assessment_agent import risk_assessment_agent, RiskAssessment
from .agents.alert_agent import alert_agent, Alert
from .agents.report_agent import report_agent, ComplianceReport
from .tools.sanctions_checker import check_sanctions_list
from .tools.transaction_analyzer import analyze_transaction_pattern


class AMLComplianceManager:
    """
    Orchestrates the AML compliance workflow:
    1. Customer screening
    2. Risk assessment
    3. Transaction monitoring
    4. Report generation
    """
    
    def __init__(self) -> None:
        pass
    
    async def run_compliance_check(
        self,
        customer_name: str,
        customer_id: str,
        customer_info: dict,
        transactions: list[dict],
    ) -> ComplianceReport:
        """
        Run full compliance check on a customer.
        
        Args:
            customer_name: Customer name
            customer_id: Unique customer ID
            customer_info: Dictionary with customer details
            transactions: List of customer transactions
            
        Returns:
            ComplianceReport with full assessment
        """
        trace_id = gen_trace_id()
        with trace("AML Compliance Check", trace_id=trace_id):
            print(f"\n{'='*60}")
            print(f"AML Compliance Check for: {customer_name}")
            print(f"Customer ID: {customer_id}")
            print(f"{'='*60}\n")
            
            # Step 1: Sanctions Screening
            print("[1/4] Running sanctions screening...")
            screening_result = await self._screen_customer(customer_name, customer_info)
            print(f"      Screening Result: {screening_result.risk_level} risk")
            print(f"      Action: {screening_result.recommended_action}")
            
            # Step 2: Risk Assessment
            print("\n[2/4] Performing risk assessment...")
            risk_result = await self._assess_risk(customer_id, customer_info, screening_result)
            print(f"      Overall Risk: {risk_result.overall_risk_rating}")
            print(f"      Review Frequency: {risk_result.review_frequency}")
            
            # Step 3: Transaction Analysis
            print("\n[3/4] Analyzing transactions...")
            alerts = await self._analyze_transactions(customer_id, transactions)
            print(f"      Alerts Generated: {len(alerts)}")
            for alert in alerts:
                print(f"      - {alert.alert_type} ({alert.severity})")
            
            # Step 4: Generate Report
            print("\n[4/4] Generating compliance report...")
            report = await self._generate_report(
                customer_id,
                screening_result,
                risk_result,
                alerts,
            )
            print(f"      Report Status: {report.compliance_status}")
            print(f"      Next Review: {report.next_review_date}")
            
            print(f"\n{'='*60}")
            print("Compliance Check Complete")
            print(f"Trace ID: {trace_id}")
            print(f"{'='*60}\n")
            
            return report
    
    async def _screen_customer(
        self,
        customer_name: str,
        customer_info: dict,
    ) -> ScreeningResult:
        """Screen customer against sanctions lists"""
        # Use tool to check sanctions
        sanctions_result = check_sanctions_list(customer_name)
        
        # Build screening context for agent
        context = f"""
Customer: {customer_name}
Nationality: {customer_info.get('nationality', 'Unknown')}
Address: {customer_info.get('address', 'Unknown')}
Date of Birth: {customer_info.get('dob', 'Unknown')}

Sanctions Check Result: {sanctions_result}
"""
        
        result = await Runner.run(
            screening_agent,
            context,
        )
        
        return result.final_output
    
    async def _assess_risk(
        self,
        customer_id: str,
        customer_info: dict,
        screening_result: ScreeningResult,
    ) -> RiskAssessment:
        """Assess customer risk level"""
        context = f"""
Customer ID: {customer_id}
Customer Type: {customer_info.get('type', 'Individual')}
Business Activity: {customer_info.get('business_activity', 'Unknown')}
Geographic Location: {customer_info.get('country', 'Unknown')}
Expected Transaction Volume: {customer_info.get('expected_volume', 'Unknown')}

Screening Results:
- Sanctions Match: {screening_result.sanctions_match}
- PEP Status: {screening_result.pep_status}
- Adverse Media: {screening_result.adverse_media_found}
- Screening Risk: {screening_result.risk_level}
"""
        
        result = await Runner.run(
            risk_assessment_agent,
            context,
        )
        
        return result.final_output
    
    async def _analyze_transactions(
        self,
        customer_id: str,
        transactions: list[dict],
    ) -> list[Alert]:
        """Analyze transactions for suspicious activity"""
        if not transactions:
            return []
        
        # Use tool to analyze patterns
        analysis = analyze_transaction_pattern(transactions)
        
        alerts = []
        
        # Generate alert if red flags found
        if analysis["red_flags"]:
            context = f"""
Customer ID: {customer_id}
Transaction Count: {analysis['transaction_count']}
Total Volume: ${analysis['total_volume']:,.2f}
Risk Score: {analysis['risk_score']:.2f}
Risk Level: {analysis['risk_level']}

Red Flags Detected:
{chr(10).join(f"- {flag}" for flag in analysis['red_flags'])}
"""
            
            result = await Runner.run(
                alert_agent,
                context,
            )
            
            alerts.append(result.final_output)
        
        return alerts
    
    async def _generate_report(
        self,
        customer_id: str,
        screening: ScreeningResult,
        risk: RiskAssessment,
        alerts: list[Alert],
    ) -> ComplianceReport:
        """Generate compliance report"""
        context = f"""
Customer ID: {customer_id}

SCREENING RESULTS:
- Customer: {screening.customer_name}
- Sanctions Match: {screening.sanctions_match}
- PEP Status: {screening.pep_status}
- Risk Level: {screening.risk_level}
- Recommended Action: {screening.recommended_action}

RISK ASSESSMENT:
- Overall Rating: {risk.overall_risk_rating}
- Geographic Risk: {risk.geographic_risk}
- Business Risk: {risk.business_risk}
- Risk Factors: {', '.join(risk.risk_factors)}
- Mitigation: {', '.join(risk.mitigation_measures)}

ALERTS: {len(alerts)} generated
{chr(10).join(f"- {a.alert_type} ({a.severity}): {a.description[:100]}..." for a in alerts) if alerts else "No alerts"}
"""
        
        result = await Runner.run(
            report_agent,
            context,
        )
        
        return result.final_output
