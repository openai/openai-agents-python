# Compare model configurations

Run repeated catalog tasks, save a baseline, and compare a candidate configuration. Check the runner with scripted responses before spending model requests on a live comparison.

## Set up the runner

Install the [SDK preview wheel](../../docs/sdk/installation.md), activate your Python environment, and use the example files from the same release. Run the commands below from the root of that release's source archive. This runner needs no extra packages.

## Check the test setup

First run the scripted cases without credentials:

```sh
python -m examples.evaluation.run --mode scripted --repeats 2 --output harness.json
```

A successful scripted run creates `harness.json` and reports `evidence` set to `harness_only`, four trials, and four passes.

The two cases ask for a known catalog code and a missing code. `ScriptedResponses` chooses the calls and final text, but the real native loop executes `lookup` and sends its result back to the peer. The runner checks that receipt. A passing scripted run checks the runner and tool execution. It does not evaluate model selection, prompt quality, or answer quality.

## Run a live comparison

Live mode sends synthetic prompts and catalog values to the provider and can incur charges. It requires `OPENAI_API_KEY` and never falls back to scripted mode. The model and reasoning setting in `config.json` must be available to your account.

### Record a baseline

Supply your key through your usual secret mechanism, then record a baseline:

```sh
python -m examples.evaluation.run --mode live --config examples/evaluation/config.json --repeats 3 --output baseline.json
```

### Run a candidate

Copy `examples/evaluation/config.json` to `candidate.json` and change only the instructions or model setting you want to compare. Keep the cases and repeats fixed:

```sh
python -m examples.evaluation.run --mode live --config candidate.json --repeats 3 --baseline baseline.json --output candidate-results.json
```

Each live command records six trials. Inspect the output file even if the command exits with a failure.

## Read the results

Result files contain the configuration, cases, catalog, hashes, SDK and Python versions, and timestamp. Each trial records its duration, answer, executed lookup codes, safe exception type, and scores.

Use synthetic data because these files include prompts, answers, and tool values. Keep credentials out of configuration files. The runner omits exception messages and traceback locals.

A trial passes only if it completes, returns the exact answer after trimming surrounding whitespace, and executes the expected lookups in order. Extra or duplicate calls fail even with a correct answer. Timeouts and provider failures also fail the trial. Exit status 1 means at least one trial failed; the file retains its results.

The baseline must match the case hash, catalog hash, mode, repeat count, and scorer version. Scripted and live results cannot be compared. The report shows score regressions, the change in passing trials, and configuration hashes that identify the changed candidate. Record the SDK release and example version in your notes.

### Interpret the comparison

Use these few tasks and repeats to inspect a specific change. They do not estimate reliability, statistical significance, production latency or performance on other tasks. Expand cases around your application's real failure modes and keep a held-out set before drawing broader conclusions.
