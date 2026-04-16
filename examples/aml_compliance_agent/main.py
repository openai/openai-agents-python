"""
AML Compliance Agent - Main Entry Point

This example demonstrates a multi-agent workflow for Anti-Money Laundering (AML)
compliance using the OpenAI Agents SDK.

Usage:
    python -m examples.aml_compliance_agent.main

Example customer data will be used for demonstration.
"""

import asyncio
from .manager import AMLComplianceManager


async def main() -> None:
    """Run AML compliance check on example customers"""
    
    print("\n" + "="*70)
    print(" AML Compliance Agent Demo")
    print(" OpenAI Agents SDK Example")
    print("="*70 + "\n")
    
    # Example customer data
    test_customers = [
        {
            "name": "John Smith",
            "id": "CUST-001",
            "info": {
                "type": "Individual",
                "nationality": "United States",
                "address": "123 Main St, New York, NY",
                "dob": "1985-03-15",
                "business_activity": "Software Consultant",
                "country": "US",
                "expected_volume": "$50,000/month",
            },
            "transactions": [
                {"transaction_id": "TXN-001", "amount": 5000, "currency": "USD", 
                 "date": "2025-01-15", "type": "deposit", 
                 "counterparty": "ABC Corp", "jurisdiction": "US"},
                {"transaction_id": "TXN-002", "amount": 3000, "currency": "USD", 
                 "date": "2025-01-16", "type": "transfer", 
                 "counterparty": "XYZ Ltd", "jurisdiction": "US"},
                {"transaction_id": "TXN-003", "amount": 2500, "currency": "USD", 
                 "date": "2025-01-17", "type": "withdrawal", 
                 "counterparty": "Self", "jurisdiction": "US"},
            ],
        },
        {
            "name": "Acme Trading Ltd",
            "id": "CUST-002",
            "info": {
                "type": "Business",
                "nationality": "United Kingdom",
                "address": "456 Commerce St, London, UK",
                "business_activity": "Import/Export",
                "country": "UK",
                "expected_volume": "$500,000/month",
            },
            "transactions": [
                {"transaction_id": "TXN-101", "amount": 9500, "currency": "USD", 
                 "date": "2025-01-10", "type": "incoming", 
                 "counterparty": "Unknown Entity", "jurisdiction": "High Risk Country"},
                {"transaction_id": "TXN-102", "amount": 9800, "currency": "USD", 
                 "date": "2025-01-11", "type": "incoming", 
                 "counterparty": "Unknown Entity", "jurisdiction": "High Risk Country"},
                {"transaction_id": "TXN-103", "amount": 9900, "currency": "USD", 
                 "date": "2025-01-12", "type": "incoming", 
                 "counterparty": "Unknown Entity", "jurisdiction": "High Risk Country"},
                {"transaction_id": "TXN-104", "amount": 50000, "currency": "USD", 
                 "date": "2025-01-13", "type": "outgoing", 
                 "counterparty": "Offshore Account", "jurisdiction": "Tax Haven"},
            ],
        },
    ]
    
    manager = AMLComplianceManager()
    
    for customer in test_customers:
        report = await manager.run_compliance_check(
            customer["name"],
            customer["id"],
            customer["info"],
            customer["transactions"],
        )
        
        print(f"\n📋 Full Report Preview:")
        print(f"   {report.executive_summary[:200]}...")
        print()
        input("Press Enter to continue to next customer...")
    
    print("\n" + "="*70)
    print(" Demo Complete!")
    print("="*70)
    print("\nThis example demonstrates:")
    print("  ✓ Multi-agent AML compliance workflow")
    print("  ✓ Sanctions screening")
    print("  ✓ Risk assessment")
    print("  ✓ Transaction monitoring")
    print("  ✓ Compliance reporting")
    print("\nFor production use, integrate with real:")
    print("  • Sanctions databases (OFAC, UN, EU)")
    print("  • Transaction monitoring systems")
    print("  • Customer due diligence platforms")
    print()


if __name__ == "__main__":
    asyncio.run(main())
