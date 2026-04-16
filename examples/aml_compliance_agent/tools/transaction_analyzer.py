"""
Transaction pattern analyzer for AML compliance.
Identifies suspicious transaction patterns and red flags.
"""

from typing import List


class Transaction:
    """Represents a single transaction"""
    def __init__(self, 
                 transaction_id: str,
                 amount: float,
                 currency: str,
                 date: str,
                 type: str,
                 counterparty: str,
                 jurisdiction: str):
        self.transaction_id = transaction_id
        self.amount = amount
        self.currency = currency
        self.date = date
        self.type = type
        self.counterparty = counterparty
        self.jurisdiction = jurisdiction


def analyze_transaction_pattern(transactions: List[dict]) -> dict:
    """
    Analyze transaction patterns for suspicious activity.
    
    Args:
        transactions: List of transaction dictionaries
        
    Returns:
        Dictionary with analysis results and red flags
    """
    red_flags = []
    risk_score = 0.0
    
    amounts = [t.get("amount", 0) for t in transactions]
    jurisdictions = [t.get("jurisdiction", "") for t in transactions]
    
    # Check 1: Structuring (amounts just below threshold)
    structuring_threshold = 10000  # Example threshold
    near_threshold_count = sum(1 for a in amounts 
                               if structuring_threshold * 0.8 < a < structuring_threshold)
    if near_threshold_count >= 3:
        red_flags.append("Potential structuring: Multiple transactions near reporting threshold")
        risk_score += 0.3
    
    # Check 2: Rapid movement (layering)
    if len(transactions) >= 5:
        red_flags.append("High transaction frequency: Potential layering activity")
        risk_score += 0.2
    
    # Check 3: High-risk jurisdictions
    high_risk_jurisdictions = {"North Korea", "Iran", "Syria", "Myanmar", "Afghanistan"}
    risky_txns = [t for t in transactions if t.get("jurisdiction", "") in high_risk_jurisdictions]
    if risky_txns:
        red_flags.append(f"Transactions involving high-risk jurisdictions: {len(risky_txns)} found")
        risk_score += 0.4
    
    # Check 4: Round numbers
    round_number_count = sum(1 for a in amounts if a > 1000 and a == int(a) and a % 1000 == 0)
    if round_number_count >= 2:
        red_flags.append("Round number transactions: Potential structuring")
        risk_score += 0.1
    
    # Check 5: Large amounts
    large_txns = [a for a in amounts if a > 50000]
    if large_txns:
        red_flags.append(f"Large transactions detected: {len(large_txns)} above $50,000")
        risk_score += 0.2
    
    return {
        "transaction_count": len(transactions),
        "total_volume": sum(amounts),
        "average_amount": sum(amounts) / len(amounts) if amounts else 0,
        "red_flags": red_flags,
        "risk_score": min(risk_score, 1.0),
        "risk_level": "high" if risk_score > 0.6 else "medium" if risk_score > 0.3 else "low",
    }
