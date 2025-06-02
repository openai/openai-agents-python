import os
import re
import argparse

# Patterns to find and their replacements
# Using raw strings for regex patterns to handle backslashes correctly
REPLACEMENT_PATTERNS = {
    # Match 'T.List[' not preceded by a dot (to avoid T.list[) or alphanumeric char (to avoid mylist[)
    r"(?<![\w.])list\[": r"typing.List[",
    r"(?<![\w.])dict\[": r"typing.Dict[",
    r"(?<![\w.])tuple\[": r"typing.Tuple[",
    r"(?<![\w.])set\[": r"typing.Set[",
    r"(?<![\w.])frozenset\[": r"typing.FrozenSet[",
    r"(?<![\w.])type\[": r"typing.Type[",
    r"(?<![\w.])Awaitable\[": r"typing.Awaitable[",
    r"(?<![\w.])Sequence\[": r"typing.Sequence[",
    r"(?<![\w.])Iterable\[": r"typing.Iterable[",
    r"(?<![\w.])AsyncIterator\[": r"typing.AsyncIterator[",
    r"(?<![\w.])AsyncGenerator\[": r"typing.AsyncGenerator[",
    # r"(?<![\w.])Generator\[": r"typing.Generator[",
    r"(?<![\w.])Mapping\[": r"typing.Mapping[",
}

IMPORT_TYPING = "import typing"


def add_typing_import(lines):
    """Adds 'import typing as T' to the file content if not already present."""
    import_exists = any(line.startswith(IMPORT_TYPING) for line in lines)
    if import_exists:
        return lines, False

    future_import_indices = [
        i for i, line in enumerate(lines) if line.startswith("from __future__ import")
    ]

    insert_pos = 0
    if future_import_indices:
        insert_pos = max(future_import_indices) + 1
    else:
        # Check for shebang or encoding comment
        if lines and lines[0].startswith("#!"):
            insert_pos = 1
            if len(lines) > 1 and lines[1].startswith("# -*- coding:"):
                insert_pos = 2
        elif lines and lines[0].startswith("# -*- coding:"):
            insert_pos = 1

    # Ensure a blank line after the import if it's not the last line and not followed by a blank line
    lines.insert(insert_pos, IMPORT_TYPING)
    if insert_pos < len(lines) - 1 and lines[insert_pos + 1].strip() != "":
        lines.insert(insert_pos + 1, "")  # Add a blank line after the import
    elif insert_pos == len(lines) - 1:  # if import is the last line, add newline after it
        pass  # Handled by join later

    return lines, True


def process_file(filepath):
    """Processes a single Python file for type hint replacements."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            original_content = f.read()
    except Exception as e:
        print(f"Error reading file {filepath}: {e}")
        return

    modified_content = original_content
    replacements_made = False

    for pattern, replacement in REPLACEMENT_PATTERNS.items():
        new_content, num_subs = re.subn(pattern, replacement, modified_content)
        if num_subs > 0:
            modified_content = new_content
            replacements_made = True

    lines = modified_content.splitlines()
    import_added = False
    if replacements_made:
        lines, import_added = add_typing_import(lines)
        modified_content = "\n".join(lines)
        if (
            not original_content.endswith("\n")
            and modified_content.endswith("\n")
            and len(lines) > 0
        ):  # if original didn't end with newline but we added one
            if lines[-1] == "":  # check if the last line is the blank line we added
                modified_content = "\n".join(lines[:-1])  # remove it
        elif (
            not original_content.endswith("\n")
            and not modified_content.endswith("\n")
            and len(lines) > 0
        ):
            modified_content += "\n"  # ensure a final newline if there was content
        elif (
            original_content.endswith("\n")
            and not modified_content.endswith("\n")
            and len(lines) > 0
        ):
            modified_content += "\n"  # ensure final newline if original had it

    if replacements_made or import_added:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(modified_content)
            print(f"Modified: {filepath}")
        except Exception as e:
            print(f"Error writing file {filepath}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Replace built-in generic types with T.Aliased versions and add 'import typing as T'."
    )
    parser.add_argument("directory", help="Directory to process Python files in.")
    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        print(f"Error: Directory '{args.directory}' not found.")
        return

    for root, _, files in os.walk(args.directory):
        for filename in files:
            if filename.endswith(".py"):
                process_file(os.path.join(root, filename))


if __name__ == "__main__":
    main()
