# Connect release notes to preparation

The skill writes the customer-facing copy. Existing release tools own version changes, artifact qualification, and publication. Keep these responsibilities separate so drafting cannot publish a release.

## Public v1 alpha

Use the current [release procedure](../../../../.github/RELEASING.md). Start the draft before preparing the version commit. Save the authored copy outside the source tree, for example as `$RELEASE_STAGE/authored-notes.md`. Record its previous published tag and target commit in the maintainer handoff.

After the candidate's Tests run passes, use the existing `qualify-distributions` command to produce `$RELEASE_STAGE/qualification.json`. Audit any changes between the draft's target and the qualified candidate. Update the copy for that exact version. Keep routine installation commands and platform details in the installation guide, not the release notes; verify any included download link against the qualified wheel filenames. Write customer copy for the moment of publication from the first draft; keep draft labels, unpublished status, and pending download checks in the separate review handoff. Leave `Release verification` out of the authored copy; the helper adds it.

Assemble the complete file for human review:

```sh
python .agents/skills/release-notes/scripts/prepare_notes.py \
  --notes "$RELEASE_STAGE/authored-notes.md" \
  --qualification "$RELEASE_STAGE/qualification.json" \
  --version "${RELEASE_TAG#v}" \
  --source-sha "$RELEASE_SHA" \
  --output "$RELEASE_STAGE/notes.md"
```

The helper checks that the authored title, requested version and source, and local qualification record agree. It requires Highlights first and adds canonical source and Tests links. It creates a new file without replacing the authored copy or an earlier review draft. Use a new output filename when revising it.

This is a local consistency check, not a new CI qualification or proof of human approval. Use the record produced by the existing qualifier, not a hand-written substitute. The helper uses no network, credentials, model API, or publishing command.

Show the complete generated file and resolve substantive feedback before publication. Then the existing `gh release create --notes-file` command consumes that same reviewed file, subject to the release procedure's current source, artifact, and authorization checks. Adding the successful post-publication installation run follows that procedure.

## Existing main automation

`release-please.yml`, `release-candidate.yml`, and the release-readiness controller target `main`. The `publish-notes` command appends a human-approved release assessment after publication; it is not a prepublication authoring step. Do not feed this customer-copy file into that assessment's approval marker or treat the two documents as interchangeable.

The writing skill can supply highlights and changelog text during a main release's preparation. Its local assembly helper currently supports only public alpha qualification records. It does not enable the PyPI publisher, change main's review checks, or reactivate the historical private-preview skills. Any future automatic drafting job should produce a reviewable artifact before publication and use this skill as its writing instructions.
