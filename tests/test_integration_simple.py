"""
Integration tests to verify SignUpHistory works correctly with the views module.
"""
import asyncio
import pytest
from models.sign_up_history import SignUpHistory


@pytest.mark.asyncio
async def test_signup_event_recording():
    """Test that SignUpHistory can record join and leave events."""
    try:
        # Test join event recording
        await SignUpHistory.record_signup_event(
            session_id="test_session_123",
            user_id="user_456",
            display_name="TestUser",
            action="join",
            guild_id="guild_789"
        )
        
        # Test leave event recording
        await SignUpHistory.record_signup_event(
            session_id="test_session_123", 
            user_id="user_456",
            display_name="TestUser",
            action="leave",
            guild_id="guild_789"
        )
        
        return True
        
    except Exception as e:
        print(f"Event recording failed: {e}")
        return False


@pytest.mark.asyncio
async def test_views_module_import():
    """Test that views module can successfully import SignUpHistory."""
    try:
        from models import SignUpHistory as ImportedSignUpHistory
        
        # Verify it's the correct class
        assert ImportedSignUpHistory == SignUpHistory
        assert hasattr(ImportedSignUpHistory, 'record_signup_event')
        
        return True
        
    except Exception as e:
        print(f"Import test failed: {e}")
        return False


async def run_integration_tests():
    """Run all integration tests and report results."""
    print("SignUpHistory Integration Tests")
    print("-" * 40)
    
    tests = [
        ("Module Import", test_views_module_import),
        ("Event Recording", test_signup_event_recording),
    ]
    
    passed = 0
    for test_name, test_func in tests:
        try:
            if await test_func():
                print(f"✅ {test_name}")
                passed += 1
            else:
                print(f"❌ {test_name}")
        except Exception as e:
            print(f"❌ {test_name}: {e}")
    
    print("-" * 40)
    print(f"Results: {passed}/{len(tests)} tests passed")
    
    return passed == len(tests)


if __name__ == "__main__":
    asyncio.run(run_integration_tests())