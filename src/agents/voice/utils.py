import re
from collections.abc import Callable


def get_sentence_based_splitter(
    min_sentence_length: int = 20,
) -> Callable[[str], tuple[str, str]]:
    """Returns a function that splits text into chunks based on sentence boundaries.

    Args:
        min_sentence_length: The minimum length of a sentence to be included in a chunk.

    Returns:
        A function that splits text into chunks based on sentence boundaries.
    """

    def sentence_based_text_splitter(text_buffer: str) -> tuple[str, str]:
        """
        A function to split the text into chunks. This is useful if you want to split the text into
        chunks before sending it to the TTS model rather than waiting for the whole text to be
        processed.

        Args:
            text_buffer: The text to split.

        Returns:
            A tuple of the text to process and the remaining text buffer.
        """
        # Capture the separators instead of discarding them, and split the raw buffer
        # rather than a stripped copy: the whitespace the model emitted is what a TTS
        # engine renders as a pause, so collapsing "\n\n" to a single space runs
        # paragraphs together in the audio.
        parts = re.split(r"((?<=[.!?])\s+)", text_buffer)
        # parts alternates sentence, separator, ..., sentence, so the last entry is the
        # incomplete sentence and at least three entries mean a sentence has completed.
        if len(parts) >= 3:
            combined_sentences = "".join(parts[:-2])
            if len(combined_sentences) >= min_sentence_length:
                # Any trailing whitespace stays on the remainder. It separates the text
                # held back from the next streamed delta, and stripping it concatenates the
                # next word onto the last one, so "He " followed by "arrived" is spoken as
                # "Hearrived".
                return combined_sentences, parts[-1]
        return "", text_buffer

    return sentence_based_text_splitter
