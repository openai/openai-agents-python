"""
AML Compliance Agent - Local Gemma Version (Offline)

This version uses local Gemma 2B model instead of OpenAI API,
enabling completely offline AML compliance checks.

Usage:
    python -m examples.aml_compliance_agent.main_gemma

Requirements:
    export HF_TOKEN=your_huggingface_token
    export GEMMA_MODEL=google/gemma-2b-it
"""

import os
import sys
import asyncio

# Set HF_TOKEN environment variable before running
# export HF_TOKEN=your_huggingface_token

# Add model_providers to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'model_providers'))

from agents import Agent, Runner
from gemma_local_provider import create_gemma_provider

from .tools.sanctions_checker import check_sanctions_list
from .tools.transaction_analyzer import analyze_transaction_pattern


# Simplified prompts for Gemma
SCREENING_PROMPT = """You are a compliance screening specialist.
Analyze the customer information and determine risk level (low/medium/high/critical).

Customer: {name}
Sanctions Check: {sanctions_result}

Respond in this format:
Risk Level: [level]
Recommended Action: [clear/review/block]
Details: [brief explanation]"""


RISK_PROMPT = """You are a risk assessment analyst.
Assess customer risk based on provided information.

Customer: {customer_id}
Screening Risk: {screening_risk}
Geographic: {country}
Business: {business}

Respond in this format:
Overall Risk: [low/medium/high]
Review Frequency: [annual/biannual/quarterly]
Justification: [brief explanation]"""


ALERT_PROMPT = """You are a transaction monitoring specialist.
Analyze transactions for suspicious activity.

Customer: {customer_id}
Analysis: {analysis}

Respond in this format:
Alert Type: [type or 'none']
Severity: [low/medium/high/critical or 'none']
Description: [brief description]"""


REPORT_PROMPT = """You are a compliance reporting specialist.
Generate a compliance report summary.

Customer: {customer_id}
Screening: {screening}
Risk: {risk}
Alerts: {alerts}

Respond in this format:
Status: [compliant/review_required/non_compliant]
Next Review: [date]
Summary: [brief executive summary]"""


class AMLComplianceManagerGemma:
    """AML Compliance Manager using local Gemma model"""
    
    def __init__(self):
        print("Loading Gemma 2B model...")
        self.provider = create_gemma_provider(
            model_name="google/gemma-2b-it",
            use_4bit=True,
        )
        print("Model loaded!\n")
    
    async def run_compliance_check(self, customer_name, customer_id, customer_info, transactions):
        """Run full compliance check"""
        
        print(f"\n{'='*60}")
        print(f"AML Compliance Check (Local Gemma)")
        print(f"Customer: {customer_name}")
        print(f"ID: {customer_id}")
        print(f"{'='*60}\n")
        
        # Step 1: Screening
        print("[1/4] Running sanctions screening...")
        sanctions = check_sanctions_list(customer_name)
        
        screening_agent = Agent(
            name="ScreeningAgent",
            instructions="You are a compliance screening specialist.",
            model_provider=self.provider,
        )
        
        screening_prompt = SCREENING_PROMPT.format(
            name=customer_name,
            sanctions_result=sanctions,
        )
        
        screening_result = await Runner.run(screening_agent, screening_prompt)
        print(f"      Result: {screening_result.final_output[:200]}...")
        
        # Step 2: Risk Assessment
        print("\n[2/4] Performing risk assessment...")
        
        risk_agent = Agent(
            name="RiskAgent",
            instructions="You are a risk assessment analyst.",
            model_provider=self.provider,
        )
        
        risk_prompt = RISK_PROMPT.format(
            customer_id=customer_id,
            screening_risk="medium" if sanctions['match_found'] else "low",
            country=customer_info.get('country', 'Unknown'),
            business=customer_info.get('business_activity', 'Unknown'),
        )
        
        risk_result = await Runner.run(risk_agent, risk_prompt)
        print(f"      Result: {risk_result.final_output[:200]}...")
        
        # Step 3: Transaction Analysis
        print("\n[3/4] Analyzing transactions...")
        analysis = analyze_transaction_pattern(transactions)
        
        alerts = []
        if analysis['red_flags']:
            alert_agent = Agent(
                name="AlertAgent",
                instructions="You are a transaction monitoring specialist.",
                model_provider=self.provider,
            )
            
            alert_prompt = ALERT_PROMPT.format(
                customer_id=customer_id,
                analysis=analysis,
            )
            
            alert_result = await Runner.run(alert_agent, alert_prompt)
            alerts.append(alert_result.final_output)
            print(f"      Alert: {alert_result.final_output[:200]}...")
        else:
            print("      No alerts generated")
        
        # Step 4: Report
        print("\n[4/4] Generating compliance report...")
        
        report_agent = Agent(
            name="ReportAgent",
            instructions="You are a compliance reporting specialist.",
            model_provider=self.provider,
        )
        
        report_prompt = REPORT_PROMPT.format(
            customer_id=customer_id,
            screening=screening_result.final_output[:100],
            risk=risk_result.final_output[:100],
            alerts=len(alerts),
        )
        
        report_result = await Runner.run(report_agent, report_prompt)
        print(f"      Report: {report_result.final_output[:200]}...")
        
        print(f"\n{'='*60}")
        print("Compliance Check Complete (100% Offline)")
        print(f"{'='*60}\n")
        
        return report_result.final_output


async def main():
    """Run AML compliance with local Gemma"""
    
    print("\n" + "="*70)
    print(" AML Compliance Agent - Local Gemma 2B (Offline)")
    print("="*70 + "\n")
    
    # Check HF_TOKEN
    if not os.getenv('HF_TOKEN'):
        print("Error: HF_TOKEN not set")
        print("Get token from https://huggingface.co/settings/tokens")
        return
    
    # Demo customer
    customer = {
        "name": "John Smith",
        "id": "CUST-001",
        "info": {
            "country": "US",
            "business_activity": "Software Consultant",
        },
        "transactions": [
            {"amount": 5000, "jurisdiction": "US"},
            {"amount": 3000, "jurisdiction": "US"},
        ],
    }
    
    manager = AMLComplianceManagerGemma()
    
    await manager.run_compliance_check(
        customer["name"],
        customer["id"],
        customer["info"],
        customer["transactions"],
    )
    
    print("\nKey Features:")
    print("  ✓ 100% offline - no data leaves your machine")
    print("  ✓ Local Gemma 2B model")
    print("  ✓ Complete AML workflow")
    print("  ✓ Privacy-preserving compliance checks")
    print()


if __name__ == "__main__":
    asyncio.run(main())
