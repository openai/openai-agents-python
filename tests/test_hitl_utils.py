from types import SimpleNamespace

from tests.utils.hitl import RecordingEditor


def test_recording_editor_records_operations() -> None:
    editor = RecordingEditor()
    operation = SimpleNamespace(path="file.txt")

    editor.create_file(operation)
    editor.update_file(operation)
    editor.delete_file(operation)

    assert editor.operations == [operation, operation, operation]
