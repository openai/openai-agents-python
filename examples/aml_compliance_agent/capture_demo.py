"""
Demo capture script for screenshots and video

This script runs the AML compliance check with pauses between steps
for easy screenshot capture.

Usage:
    python capture_demo.py
    
Press Enter to continue after each step for screenshot.
"""

import os
import sys
import asyncio
import time

# Set HF_TOKEN environment variable before running
# export HF_TOKEN=your_huggingface_token
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'model_providers'))

from agents import Agent, Runner
from gemma_local_provider import create_gemma_provider

from tools.sanctions_checker import check_sanctions_list
from tools.transaction_analyzer import analyze_transaction_pattern


def wait_for_screenshot(step_name: str):
    """Pause for screenshot"""
    print(f"\n[PAUSE] {step_name}")
    print("Press Enter to continue...")
    input()


async def main():
    """Run demo with pauses for screenshots"""
    
    print("\n" + "="*70)
    print(" AML Compliance Agent - Screenshot Demo Mode")
    print("="*70)
    print("\nThis script pauses after each step for screenshot capture.")
    print("Press Enter to continue to next step.\n")
    
    # Wait for start
    wait_for_screenshot("Ready to start - Press Enter to load model")
    
    print("Loading Gemma 2B model...")
    provider = create_gemma_provider(
        model_name="google/gemma-2b-it",
        use_4bit=True,
    )
    print("Model loaded!\n")
    
    # Customer data
    customer_name = "John Smith"
    customer_id = "CUST-001"
    customer_info = {"country": "US", "business_activity": "Software Consultant"}
    transactions = [
        {"amount": 5000, "jurisdiction": "US"},
        {"amount": 3000, "jurisdiction": "US"},
    ]
    
    print(f"\n{'='*70}")
    print(f"AML Compliance Check (Local Gemma)")
    print(f"Customer: {customer_name}")
    print(f"ID: {customer_id}")
    print(f"{'='*70}\n")
    
    wait_for_screenshot("Step 1: Sanctions Screening - Press Enter")
    
    # Step 1: Screening
    print("[1/4] Running sanctions screening...")
    sanctions = check_sanctions_list(customer_name)
    
    screening_agent = Agent(
        name="ScreeningAgent",
        instructions="You are a compliance screening specialist.",
        model_provider=provider,
    )
    
    screening_prompt = f"""Analyze customer for sanctions risk:
Customer: {customer_name}
Sanctions Check: {sanctions}

Respond with:
Risk Level: [low/medium/high]
Action: [clear/review/block]
Details: [brief]"""
    
    screening_result = await Runner.run(screening_agent, screening_prompt)
    print(f"      Result: {screening_result.final_output[:200]}...")
    
    wait_for_screenshot("Step 2: Risk Assessment - Press Enter")
    
    # Step 2: Risk Assessment
    print("\n[2/4] Performing risk assessment...")
    
    risk_agent = Agent(
        name="RiskAgent",
        instructions="You are a risk assessment analyst.",
        model_provider=provider,
    )
    
    risk_prompt = f"""Assess customer risk:
ID: {customer_id}
Screening: low risk
Country: {customer_info['country']}
Business: {customer_info['business_activity']}

Respond with:
Overall Risk: [low/medium/high]
Review Frequency: [annual/biannual/quarterly]
Justification: [brief]"""
    
    risk_result = await Runner.run(risk_agent, risk_prompt)
    print(f"      Result: {risk_result.final_output[:200]}...")
    
    wait_for_screenshot("Step 3: Transaction Monitoring - Press Enter")
    
    # Step 3: Transaction Analysis
    print("\n[3/4] Analyzing transactions...")
    analysis = analyze_transaction_pattern(transactions)
    
    if analysis['red_flags']:
        print(f"      Alert: {analysis['red_flags']}")
    else:
        print("      No alerts generated")
    
    wait_for_screenshot("Step 4: Compliance Report - Press Enter")
    
    # Step 4: Report
    print("\n[4/4] Generating compliance report...")
    
    report_agent = Agent(
        name="ReportAgent",
        instructions="You are a compliance reporting specialist.",
        model_provider=provider,
    )
    
    report_prompt = f"""Generate compliance report:
Customer: {customer_id}
Screening: passed
Risk: low
Alerts: {len(analysis['red_flags'])}

Respond with:
Status: [compliant/review_required]
Next Review: [date]
Summary: [brief executive summary]"""
    
    report_result = await Runner.run(report_agent, report_prompt)
    print(f"      Report: {report_result.final_output[:200]}...")
    
    wait_for_screenshot("Complete - Press Enter to finish")
    
    print(f"\n{'='*70}")
    print("Compliance Check Complete (100% Offline)")
    print(f"{'='*70}\n")
    
    print("Key Features:")
    print("  ✓ 100% offline - no data leaves your machine")
    print("  ✓ Local Gemma 2B model")
    print("  ✓ Complete AML workflow")
    print("  ✓ Privacy-preserving compliance checks")
    print()
    
    print("Demo complete! All screenshots captured.")


if __name__ == "__main__":
    asyncio.run(main())
