from agents.sandbox.util.iterator_io import IteratorIO


def test_zero_length_readinto_does_not_advance_iterator() -> None:
    chunks = iter([b"archive"])
    finalized = False

    def on_close() -> None:
        nonlocal finalized
        finalized = True

    stream = IteratorIO(chunks, on_close=on_close)

    assert stream.readinto(bytearray()) == 0
    assert finalized is False
    assert next(chunks) == b"archive"

    stream.close()
    assert finalized is True
