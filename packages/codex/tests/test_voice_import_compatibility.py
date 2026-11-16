"""Published legacy speech imports keep explicit Classic ownership beside native APIs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from tests.test_classic_imports import run_isolated

MODULE_CASES = [
    ("voice.input", "AudioInput"),
    ("voice.model", "STTModel"),
    ("voice.pipeline", "VoicePipeline"),
    ("voice.pipeline_config", "VoicePipelineConfig"),
    ("voice.result", "StreamedAudioResult"),
    ("voice.events", "VoiceStreamEventAudio"),
    ("voice.utils", "get_sentence_based_splitter"),
    ("voice.workflow", "SingleAgentVoiceWorkflow"),
    ("voice.models.openai_stt", "OpenAISTTModel"),
    ("voice.models.openai_tts", "OpenAITTSModel"),
    ("voice.models.openai_model_provider", "OpenAIVoiceModelProvider"),
    ("realtime.agent", "RealtimeAgent"),
    ("realtime.runner", "RealtimeRunner"),
    ("realtime.model", "RealtimeModelConfig"),
    ("realtime.config", "RealtimeRunConfig"),
    ("realtime.events", "RealtimeSessionEvent"),
    ("realtime.items", "RealtimeItem"),
    ("realtime.session", "RealtimeSession"),
    ("realtime.handoffs", "realtime_handoff"),
    ("realtime.openai_realtime", "OpenAIRealtimeWebSocketModel"),
    ("realtime.model_events", "RealtimeModelAudioEvent"),
    ("realtime.model_inputs", "RealtimeModelSendAudio"),
]


@pytest.mark.parametrize(
    "name", ["SingleAgentVoiceWorkflow", "SingleAgentWorkflowCallbacks", "VoiceWorkflowHelper"]
)
def test_voice_workflow_exports_have_classic_identity(name):
    from agents import classic, voice

    assert getattr(voice, name) is getattr(classic.voice, name)
    assert name in voice.__all__


def test_realtime_facade_preserves_every_classic_export():
    import agents
    from agents import realtime
    from agents.classic import realtime as classic
    from agents.realtime import native

    assert realtime.__all__ == classic.__all__
    for name in classic.__all__:
        assert getattr(realtime, name) is getattr(classic, name), name
        assert name in dir(realtime)
    assert native.RealtimeSession is not classic.RealtimeSession
    assert native.RealtimeSession.__module__ == "agents.realtime.native"
    assert agents.Agent is not importlib.import_module("agents.classic").Agent
    assert not hasattr(agents, "Runner")


def test_native_realtime_import_does_not_load_classic():
    private = str(Path(__file__).resolve().parents[1] / "src")
    run_isolated(
        f"""
import sys
sys.path.insert(0, {private!r})
sys.modules['griffe'] = None
import agents.realtime.native as realtime

assert 'RealtimeSession' in dir(realtime)
assert not any(n.startswith('agents.classic') for n in sys.modules)
from agents.realtime.native import *
assert RealtimeSession is realtime.RealtimeSession
assert 'RealtimeAgent' not in globals()
assert not any(n.startswith('agents.classic') for n in sys.modules)
"""
    )


@pytest.mark.parametrize(("module", "name"), MODULE_CASES)
def test_original_voice_module_import_retains_classic_objects(module, name):
    public = importlib.import_module("agents." + module)
    classic = importlib.import_module("agents.classic." + module)
    assert getattr(public, name) is getattr(classic, name)


async def test_original_voice_workflow_runs_classic_without_native(monkeypatch):
    import numpy as np

    from agents.classic import Agent
    from agents.classic.testing import ScriptedModel, assistant_message
    from agents.classic.voice.testing import ScriptedSTTModel, ScriptedTTSModel
    from agents.voice import (
        AudioInput,
        SingleAgentVoiceWorkflow,
        VoicePipeline,
        VoicePipelineConfig,
    )

    monkeypatch.setitem(sys.modules, "agents._native", None)
    model = ScriptedModel([[assistant_message("The retained voice reply.")]])
    stt = ScriptedSTTModel("The retained question.")
    tts = ScriptedTTSModel([[b"\x01\x00"]])
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(Agent(name="Legacy", model=model)),
        stt_model=stt,
        tts_model=tts,
        config=VoicePipelineConfig(tracing_disabled=True),
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    events = [event async for event in result.stream()]
    assert any(event.type == "voice_stream_event_audio" for event in events)
    assert "".join(call.text for call in tts.calls) == "The retained voice reply."
    model.assert_complete()
    stt.assert_complete()
    tts.assert_complete()


async def test_realtime_factory_keeps_classic_session_and_audio(monkeypatch):
    from agents.classic.realtime import RealtimeSession as ClassicSession
    from agents.classic.realtime.testing import ScriptedRealtimeModel
    from agents.realtime import RealtimeAgent, RealtimeRunner, RealtimeSession
    from agents.realtime.native import RealtimeSession as NativeSession

    monkeypatch.setitem(sys.modules, "agents._native", None)
    model = ScriptedRealtimeModel(strict=False)
    runner = RealtimeRunner(
        RealtimeAgent(name="Legacy", instructions="Reply."),
        model=model,
        config={"tracing_disabled": True},
    )
    session = await runner.run()
    assert RealtimeSession is ClassicSession
    assert type(session) is RealtimeSession
    assert not isinstance(session, NativeSession)
    async with session:
        await session.send_audio(b"\x01\x00", commit=True)
    assert len(model.connect_calls) == 1
    assert any(getattr(event, "audio", None) == b"\x01\x00" for event in model.sent_events)
    assert model.closed
    model.assert_complete()
