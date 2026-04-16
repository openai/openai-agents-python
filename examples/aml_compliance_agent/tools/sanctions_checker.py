"""
Mock sanctions list checker for demonstration purposes.
In production, this would integrate with real sanctions databases like:
- OFAC SDN List
- UN Consolidated List
- EU Consolidated List
- HMT Sanctions List
"""

from typing import Optional

# Mock sanctions database
SANCTIONS_DB = {
    "john_doe": {
        "name": "John Doe",
        "aliases": ["J. Doe", "JD"],
        "list": "OFAC SDN",
        "reason": "Counter Narcotics Trafficking",
        "date_added": "2023-01-15",
    },
    "acme_corp": {
        "name": "Acme Corporation",
        "aliases": ["Acme Ltd", "Acme Trading"],
        "list": "EU Consolidated",
        "reason": "Arms Embargo",
        "date_added": "2022-08-20",
    },
}


def check_sanctions_list(name: str, address: Optional[str] = None) -> dict:
    """
    Check if a name appears on sanctions lists.
    
    Args:
        name: Customer name to check
        address: Optional address for additional matching
        
    Returns:
        Dictionary with match results
    """
    name_lower = name.lower().replace(" ", "_")
    
    # Exact match
    if name_lower in SANCTIONS_DB:
        return {
            "match_found": True,
            "match_type": "exact",
            "details": SANCTIONS_DB[name_lower],
        }
    
    # Partial match check
    for key, record in SANCTIONS_DB.items():
        if name_lower in key or key in name_lower:
            return {
                "match_found": True,
                "match_type": "partial",
                "details": record,
            }
        # Check aliases
        for alias in record.get("aliases", []):
            if name_lower in alias.lower() or alias.lower() in name_lower:
                return {
                    "match_found": True,
                    "match_type": "alias",
                    "details": record,
                }
    
    return {
        "match_found": False,
        "match_type": None,
        "details": None,
    }
