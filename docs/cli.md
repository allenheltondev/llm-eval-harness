# The `evalharness` CLI

The command line is the primary way to drive the harness. Everything the HTTP
API can do to a run or an evaluation, the CLI does against the same engine and
the same history store, with no server process in the way. `evalharness serve`
starts the API for anyone who would rather use the web UI, which makes the UI
one front door rather than the front door.

```
evalharness <command> [options]

  run      execute one run
  eval     run a determinism experiment, or grade stored runs
  models   list available models across every provider
  tools    list the registered toolsets
  runs     list stored runs, newest first
  show     print one stored run or evaluation as JSON
  serve    start the HTTP API and UI
```

`make install` (or `uv sync` inside `server/`) installs the console script
declared in `[project.scripts]` into `server/.venv`. Three equivalent ways to
reach it:

```bash
cd server && uv run evalharness ...      # no activation needed
source server/.venv/bin/activate         # then plain `evalharness ...`
cd server && uv run python -m evalharness.cli ...
```

The examples below are written as `evalharness ...` and assume one of the
first two.

## The two streams

**stdout is the product. stderr is the commentary.** Every command obeys this,
and it is the reason the obvious pipeline does the obvious thing:

```bash
evalharness run -m anthropic.claude-sonnet-4-20250514-v1:0 \
  -p 'Summarise the attached incident report' > summary.txt
```

`summary.txt` holds the model's answer and nothing else — no run id, no token
counts, no tool calls — while those still scroll past on the terminal as the
run happens. Progress lines are prefixed with `| ` so they are easy to read
next to whatever else is sharing the terminal, and easy to drop.

`--json` moves machine-readable output onto stdout instead: NDJSON for the
streaming commands (`run`, `eval`), a single JSON document for the rest. It is
byte-for-byte the event stream the API serves, so anything wrapping the harness
— a script, a CI job, an MCP server — reads one format, not two.

`--json` is accepted on either side of the subcommand. People type both.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The run or evaluation finished. |
| `1` | The harness failed — a provider error, an unknown toolset, a missing id. |
| `2` | The invocation was wrong. |
| `130` | Cancelled (Ctrl-C). |

A **grade of F is exit `0`**. The evaluation succeeded; it is telling you the
answer is bad. Reserve `1` for "the harness could not do its job", so `set -e`
in a script means what you want it to mean.

## `run`

```bash
evalharness run -m <model-id> -p 'your prompt'
```

The prompt can also arrive on stdin, which makes the harness an ordinary member
of a pipeline:

```bash
cat prompt.txt | evalharness run -m <model-id>
evalharness run -m <model-id> -p -   < prompt.txt   # the same, said explicitly
```

Stdin is only read when it is *not* a terminal. Running `evalharness run -m x`
at an interactive prompt with no `-p` reports a usage error rather than
silently blocking on a read you cannot see.

What arrives on stdin reaches the model **verbatim** — indentation and trailing
newlines included, since they are meaningful in code, markdown and
delimiter-based templates. Input that is entirely whitespace is rejected as a
usage error rather than sent.

| Option | Meaning |
|---|---|
| `-m`, `--model` | Model id, as listed by `evalharness models`. Required. |
| `--provider` | Which SDK executes the run (default `bedrock`). Must match the model's `source`. |
| `-p`, `--prompt` | The user prompt; `-` or omitted reads stdin. |
| `-s`, `--system` / `--system-file` | The system prompt, inline or from a file. |
| `--toolset` | A toolset from `evalharness tools`. |
| `--max-tool-iterations` | Cap on agent loop turns (default 10). |
| `--temperature`, `--top-p`, `--max-tokens` | Sampling knobs. |
| `--guardrail-id`, `--guardrail-version` | Apply a Bedrock guardrail (Bedrock only). |

Setup failures — an unknown toolset, a guardrail on a non-Bedrock provider —
fail before anything executes and before a run row is written, exactly as they
do over HTTP. Failures *during* a run are reported on the progress stream and
persisted on the run row.

## `eval`

Two shapes, same command. Without `--run` it is a determinism experiment:
execute the prompt `n` times and grade the batch.

```bash
evalharness eval -m <model-id> -p 'your prompt' -n 10
```

With `--run` (repeatable) it grades runs that already exist, executing nothing
new:

```bash
evalharness eval --run 3f2a… --run 9c81… --rubric 'Penalise any tool call.'
```

| Option | Meaning |
|---|---|
| `-n` | Repeats for a determinism experiment, clamped to 2–25 (default 10). |
| `--run RUN_ID` | Grade this stored run instead of executing new ones; repeatable. |
| `--rubric` | Extra rubric text for the judge. |
| `--grader-model`, `--grader-provider`, `--grader-system` | The judge. Independent of the graded runs — an OpenAI judge grading Bedrock runs is a reasonable setup. A non-Bedrock `--grader-provider` **requires** `--grader-model`: the built-in default is a Bedrock model id, and no default is invented for the other providers. |

Plus every `run` option above, which describes the repeats.

The API answers `202` and runs evaluations in the background, because a request
cannot wait fifteen minutes. A CLI invocation *is* the job: it runs in the
foreground, and Ctrl-C cancels the evaluation rather than just stopping your
view of it. The terminal state lands on stdout as JSON; the grade and the
judge's reasoning are summarised on stderr.

## `models`, `tools`

`models` aggregates every provider exactly as `GET /models` does, including its
degrade-to-empty behaviour: a provider that fails to list contributes nothing
and does not fail the command. Providers with no credentials are named on
stderr so an empty table explains itself.

The `PROVIDER` column is the value to pass back as `--provider`.

`tools` lists the registered toolsets and the tool names each exposes to the
model — the valid values for `--toolset`.

## `runs`, `show`

```bash
evalharness runs --limit 50 --model <model-id> --status error
evalharness show <run-id-or-evaluation-id>
```

`runs` pages newest-first; when more rows exist, the cursor to continue from is
printed on stderr. `show` takes either kind of id and looks in both, because
from the outside you have an id and you want to see it.

## `serve`

```bash
evalharness serve --port 8000 --reload
```

Starts the FastAPI app the SPA talks to. The UI is one way to use the harness,
so starting it is a subcommand rather than a separate thing to learn.

## Configuration

The CLI reads the same environment as the server (see the Configuration section
of the README) — `EVALHARNESS_`-prefixed variables, plus the standard
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OLLAMA_HOST` names.

`--db` overrides `EVALHARNESS_DB_PATH` for one invocation, which is the quick
way to keep an experiment's history out of your main store:

```bash
evalharness --db /tmp/scratch.db run -m <model-id> -p 'throwaway'
```

It is exported into the environment rather than held privately, so
`evalharness --db … serve` starts a server reading that same store — uvicorn
imports the app by string, and with `--reload` in a child process, and the
app builds its own settings from the environment at startup.

`--db` also **pins the history backend to SQLite** for that invocation. Naming
a SQLite file while the environment says `EVALHARNESS_HISTORY_BACKEND=dynamodb`
would otherwise create the file and then write every run to DynamoDB anyway,
which is the opposite of the isolated scratch store `--db` exists to give you.

## Why argparse

The CLI uses nothing but the standard library. A `click` or `typer` dependency
would ride into the Lambda artifact, which has a hard 250 MB unzipped ceiling
it is already pruning packages to stay under (see
`docs/serverless-deploy-infra.md`), in exchange for nothing this parser cannot
already do.
