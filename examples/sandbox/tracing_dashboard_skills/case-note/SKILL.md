---
name: case-note
description: Writes a short customer case note from provided facts and memory.
---

# Case Note

Use this skill when preparing a short customer case note.

## Steps

1. Use the case facts already provided in the conversation.
2. Do not call additional tools after loading this skill.
3. Include one memory signal if it applies.
4. Return a note with these sections:
   - `## Facts`
   - `## Policy`
   - `## Recommendation`

Keep the note under 90 words.
