"""Context managers for resource management in agents.

Provides context managers for safe resource cleanup and lifecycle management
in agent operations.
"""

from contextlib import contextmanager
from typing import Iterator, Any, Optional


@contextmanager
def agent_session() -> Iterator[dict]:
    """Context manager for managing agent sessions.
    
    Ensures proper initialization and cleanup of agent resources.
    
    Yields:
        A session dictionary for tracking agent state.
        
    Example:
        ```
        from xai_sdk.context_managers import agent_session
        
        with agent_session() as session:
            # Use agent in session
            pass
        ```
    """
    session = {"active": True, "resources": []}
    try:
        yield session
    finally:
        session["active"] = False
        # Cleanup resources
        for resource in session.get("resources", []):
            try:
                resource.close()
            except Exception:
                pass


@contextmanager
def temporary_timeout(timeout_seconds: float) -> Iterator[None]:
    """Context manager for temporary timeout settings.
    
    Args:
        timeout_seconds: The timeout duration in seconds.
        
    Yields:
        None
    """
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError(f"Operation timed out after {timeout_seconds}s")
    
    # Note: signal.alarm only works on Unix-like systems
    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(int(timeout_seconds) + 1)
        yield
    finally:
        signal.alarm(0)


@contextmanager
def error_handling_context(
    fallback_value: Optional[Any] = None,
) -> Iterator[dict]:
    """Context manager for comprehensive error handling.
    
    Args:
        fallback_value: Value to use if an error occurs.
        
    Yields:
        A context dictionary for error state tracking.
    """
    context = {
        "errors": [],
        "warnings": [],
        "fallback_value": fallback_value,
    }
    try:
        yield context
    except Exception as e:
        context["errors"].append(str(e))
        if fallback_value is not None:
            pass  # Could use fallback_value here
        raise
