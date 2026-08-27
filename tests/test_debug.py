import os
from unittest.mock import patch

from agents._debug import _load_dont_log_model_data, _load_dont_log_tool_data


@patch.dict(os.environ, {})
def test_dont_log_model_data():
    assert _load_dont_log_model_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "0"})
def test_dont_log_model_data_0():
    assert _load_dont_log_model_data() is False


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "1"})
def test_dont_log_model_data_1():
    assert _load_dont_log_model_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "true"})
def test_dont_log_model_data_true():
    assert _load_dont_log_model_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "false"})
def test_dont_log_model_data_false():
    assert _load_dont_log_model_data() is False


@patch.dict(os.environ, {})
def test_dont_log_tool_data():
    assert _load_dont_log_tool_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "0"})
def test_dont_log_tool_data_0():
    assert _load_dont_log_tool_data() is False


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "1"})
def test_dont_log_tool_data_1():
    assert _load_dont_log_tool_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "true"})
def test_dont_log_tool_data_true():
    assert _load_dont_log_tool_data() is True


@patch.dict(os.environ, {"OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "false"})
def test_dont_log_tool_data_false():
    assert _load_dont_log_tool_data() is False
