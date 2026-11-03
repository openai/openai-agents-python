# Compare live model trials

Install the [SDK preview wheel](../../docs/sdk/installation.md), activate your Python environment, and use the example files from the same release. Run the commands below from the root of that release's source archive. This runner needs no extra packages.

First check the harness without credentials:

```sh
python -m examples.evaluation.run --mode scripted --repeats 2 --output harness.json
```

The two cases ask for a known catalog code and a missing code. `ScriptedResponses` chooses the calls and final text, but the real native loop executes `lookup` and sends its result back to the peer. The runner checks that receipt. A passing scripted run proves the harness and tool plumbing. It supplies no evidence of model selection, prompt quality or answer quality.

For a live comparison, inject your own `OPENAI_API_KEY` through your normal secret mechanism, select a model available to your account in `config.json`, and run:

```sh
python -m examples.evaluation.run --mode live --config examples/evaluation/config.json --repeats 3 --output baseline.json
```

Copy `config.json` to `candidate.json` and change only the instructions or model setting you want to compare. Keep the cases and repeats fixed:

```sh
python -m examples.evaluation.run --mode live --config candidate.json --repeats 3 --baseline baseline.json --output candidate-results.json
```

Live mode sends the synthetic prompts and catalog values to the configured provider and can incur charges. It requires `OPENAI_API_KEY` and never falls back to scripted mode. The example configuration names a model and reasoning setting explicitly; availability depends on your account. Live model quality was not measured by the no-key checks shipped with this example.

Each result file records the configuration, cases, catalog, hashes, SDK/Python versions and timestamp. Each trial records its duration, final answer, executed lookup codes, safe exception type and scores. Use synthetic data: result files deliberately contain prompts, model answers and tool values. Do not put credentials in configuration files. The runner does not serialize exception messages or traceback locals.

The scorer requires a completed trial, an exact final answer after trimming surrounding whitespace, and the expected ordered list of executed lookups. An extra lookup or duplicate call fails tool behavior even if the final answer is correct. A timeout or provider failure fails the trial and remains visible in the result file. Exit status 1 means at least one trial failed; the file still contains those trials.

A baseline must use the same case hash, catalog hash, mode, repeat count and scorer version. The comparison reports individual score regressions and the change in passed-trial count; configuration hashes show which candidate changed. The runner rejects mixed scripted/live evidence. Record the SDK release and example version alongside your experiment notes.

These few tasks and repeats help inspect a specific change. They do not estimate reliability, statistical significance, production latency or performance on other tasks. Expand cases around your application's real failure modes and keep a held-out set before drawing broader conclusions.
