"""Mock paid API server for the agent wallet example.

Simulates a paid data API that requires agent authorization before serving.
No real payments or network calls -- everything runs locally.
"""

from __future__ import annotations

from wallet import VerificationResult, authorize_agent

# Mock data that the "paid API" serves
_MARKET_DATA = {
    "BTC": {"price": 68420.50, "change_24h": 2.3},
    "ETH": {"price": 3850.25, "change_24h": -0.8},
    "SOL": {"price": 142.80, "change_24h": 5.1},
}


def call_paid_api(
    credential: str,
    symbol: str,
) -> dict:
    """Simulate a paid API call with authorization check.

    Args:
        credential: The agent's credential for authorization.
        symbol: The market data symbol to look up.

    Returns:
        API response dict with data or error.
    """
    # Step 1: Verify authorization
    result = authorize_agent(
        credential=credential,
        required_permissions={"read_data", "financial_small"},
    )

    if not result.authorized:
        return {
            "error": "unauthorized",
            "message": f"Agent '{result.agent_id}' denied: {result.reason}",
            "status": 401,
        }

    # Step 2: Serve the data (agent is authorized)
    data = _MARKET_DATA.get(symbol.upper())
    if not data:
        return {
            "error": "not_found",
            "message": f"No data for symbol '{symbol}'",
            "status": 404,
        }

    return {
        "status": 200,
        "symbol": symbol.upper(),
        "data": data,
        "authorized_agent": result.agent_id,
    }
