import re
from typing import Callable


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
        sentences = re.split(r"(?<=[.!?])\s+", text_buffer.strip())
        if len(sentences) >= 1:
            combined_sentences = " ".join(sentences[:-1])
            if len(combined_sentences) >= min_sentence_length:
                remaining_text_buffer = sentences[-1]
                return combined_sentences, remaining_text_buffer
        return "", text_buffer

    return sentence_based_text_splitter
