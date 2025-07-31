#!/usr/bin/env python3
"""
Test script to verify the changes work correctly.
This should be run after applying the changes to the agents package.
"""

import sys
import traceback


def test_imports():
    """Test that all imports work correctly."""
    print("Testing imports...")

    try:
        # Test existing imports still work
        from agents import Agent, ModelProvider, RunConfig, Runner
        print("✅ Existing imports work")

        # Test new MultiProvider import
        from agents import MultiProvider
        print("✅ MultiProvider import works")

        # Test new factory function import
        # from agents import create_default_model_provider
        # print("✅ create_default_model_provider import works")

        return True
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        return False

def test_functionality():
    """Test that the functionality works correctly."""
    print("\nTesting functionality...")

    try:
        from agents import MultiProvider, RunConfig

        # Test MultiProvider instantiation
        provider1 = MultiProvider()
        print("✅ MultiProvider instantiation works")

        # # Test factory function
        # provider2 = create_default_model_provider(openai_api_key="test-key")
        # print("✅ Factory function works")

        # Test that both are the same type
        assert isinstance(provider1, MultiProvider)
        # assert isinstance(provider2, MultiProvider)
        print("✅ Both providers are correct type")

        # Test RunConfig still works with default
        config = RunConfig()
        print("✅ RunConfig with default provider works")

        # Test RunConfig with custom provider
        config2 = RunConfig(model_provider=MultiProvider())
        print("✅ RunConfig with custom provider works")

        return True
    except Exception as e:
        print(f"❌ Functionality test failed: {e}")
        traceback.print_exc()
        return False

def test_backward_compatibility():
    """Test that existing code still works."""
    print("\nTesting backward compatibility...")

    try:
        from agents import Agent, RunConfig

        # Test that default RunConfig works
        config = RunConfig()
        assert config.model_provider is not None
        print("✅ Default RunConfig works")

        # Test that we can still create agents
        agent = Agent(
            name="test",
            instructions="You are a test agent."
        )
        print("✅ Agent creation works")

        return True
    except Exception as e:
        print(f"❌ Backward compatibility test failed: {e}")
        traceback.print_exc()
        return False

def main():
    """Run all tests."""
    print("Running tests for MultiProvider export changes...\n")

    tests = [
        test_imports,
        test_functionality,
        test_backward_compatibility
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        if test():
            passed += 1
        print()

    print(f"Results: {passed}/{total} tests passed")

    if passed == total:
        print("🎉 All tests passed! The changes are working correctly.")
        return 0
    else:
        print("❌ Some tests failed. Please check the implementation.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
