"""
Reproduce issue #821: ValueError when audio buffer is empty

This test reproduces the exact error reported in the issue:
ValueError: need at least one array to concatenate
"""
import numpy as np
import base64


def _audio_to_base64(audio_data):
    """From src/agents/voice/models/openai_stt.py line 42-49"""
    concatenated_audio = np.concatenate(audio_data)  # ❌ This will fail if audio_data is empty!
    if concatenated_audio.dtype == np.float32:
        concatenated_audio = np.clip(concatenated_audio, -1.0, 1.0)
        concatenated_audio = (concatenated_audio * 32767).astype(np.int16)
    audio_bytes = concatenated_audio.tobytes()
    return base64.b64encode(audio_bytes).decode("utf-8")


def simulate_end_turn_original(transcript: str, turn_audio_buffer: list,
                                 trace_include_sensitive_audio_data: bool):
    """
    Simulates the original _end_turn method from openai_stt.py lines 120-135
    """
    if len(transcript) < 1:
        return

    if trace_include_sensitive_audio_data:
        # Original code: No check for empty buffer!
        return _audio_to_base64(turn_audio_buffer)  # ❌ CRASHES HERE


print("="*70)
print("Reproducing Issue #821")
print("="*70)

# Scenario: Transcript is generated but audio buffer is empty
# This can happen due to network issues, timing issues, etc.
transcript = "Hello world"  # We have a transcript
turn_audio_buffer = []  # But NO audio data!
trace_include_sensitive_audio_data = True

print("\nScenario:")
print(f"  Transcript: '{transcript}'")
print(f"  Audio buffer: {turn_audio_buffer} (empty!)")
print(f"  Tracing enabled: {trace_include_sensitive_audio_data}")

print("\nAttempting to call _end_turn()...")

try:
    result = simulate_end_turn_original(transcript, turn_audio_buffer, trace_include_sensitive_audio_data)
    print(f"❌ UNEXPECTED: Should have crashed but got: {result}")
except ValueError as e:
    print(f"\n✅ REPRODUCED THE BUG!")
    print(f"   Error: {e}")
    print(f"\n   This is the exact error from issue #821:")
    print(f"   'ValueError: need at least one array to concatenate'")
    print(f"\n   Traceback location:")
    print(f"   File: src/agents/voice/models/openai_stt.py")
    print(f"   Line: 126 -> _audio_to_base64(self._turn_audio_buffer)")
    print(f"         ↓")
    print(f"   Line: 43  -> np.concatenate(audio_data)")

print("\n" + "="*70)
print("Issue successfully reproduced!")
print("="*70)
