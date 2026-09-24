# The `nimbus` CLI

The command line is the primary way to drive the harness. Everything the HTTP
API can do to a run or an evaluation, the CLI does against the same engine and
the same history store, with no server process in the way. `nimbus serve`
starts the API for anyone who would rather use the web UI, which makes the UI
one front door rather than the front door.

```
nimbus <command> [options]

  run      execute one run
  eval     run a determinism experiment, grade stored runs, or run a test suite
  models   list available models across every provider
  tools    list the registered toolsets
  runs     list stored runs, newest first
  show     print one stored run or evaluation as JSON
  login    sign in to a deployed Nimbus stack; commands then run there
  logout   sign out; commands run on this machine again
  whoami   where commands run: which stack, and as whom
  init     write a starter test suite to edit into your own
  doctor   check your setup: history, model providers, the signed-in stack
  serve    start the HTTP API the web UI talks to

  --version     the installed version
  -v, --verbose also show the log (provider and AWS diagnostics) on stderr
  --local       this machine, even when signed in to a stack
```

## Installing

`nimbus` is a Python 3.12 package; [uv](https://docs.astral.sh/uv/) installs it
as a tool on your `PATH`, usable from any directory:

```bash
uv tool install "git+https://github.com/allenheltondev/llm-eval-harness#subdirectory=server"
uv tool upgrade nimbus-evals                     # later, for the newest
```

Then `nimbus doctor` says what is set up and what to do about the rest:

```
nimbus 0.1.0 (Python 3.12.3)

ok  history    /home/ada/.local/share/nimbus/history.db
ok  bedrock    42 models in us-east-1
--  anthropic  not configured
               -> set ANTHROPIC_API_KEY
--  openai     not configured
               -> set OPENAI_API_KEY
--  ollama     not configured
               -> set OLLAMA_HOST
--  stack      not signed in; commands run on this machine
               -> `nimbus login --url https://<your-stack>` to run on a deployed stack
```

`ok` works, `--` is optional and not set up, `!!` is set up and broken — and
only `!!` makes it exit `1` (as does having nothing at all that can run a
model). `--json` gives the same checks as a document.

Working in a checkout of this repository instead, `make install` (or `uv sync`
inside `server/`) installs it into `server/.venv`:

```bash
cd server && uv run nimbus ...           # no activation needed
source server/.venv/bin/activate         # then plain `nimbus ...`
cd server && uv run python -m nimbus.cli ...
```

## A first suite

```bash
nimbus init                              # writes suite.yaml: four cases for a support prompt
nimbus models                            # pick a model id
nimbus eval --suite suite.yaml -m <model-id>
```

`init [FILE]` writes a commented example ([the same one as
docs/examples](examples/support-suite.yaml)) to edit into your own; `-m` puts
a model id in it, and it never replaces an existing file without `--force`.
[docs/suites.md](suites.md) is the reference.

## The two streams

**stdout is the product. stderr is the commentary.** Every command obeys this,
and it is the reason the obvious pipeline does the obvious thing:

```bash
nimbus run -m anthropic.claude-sonnet-4-20250514-v1:0 \
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

A **grade of F is exit `0`**, and so is a suite with failing cases. The
evaluation succeeded; it is telling you the answer is bad. Reserve `1` for "the harness could not do its job", so `set -e`
in a script means what you want it to mean.

## `run`

```bash
nimbus run -m <model-id> -p 'your prompt'
```

The prompt can also arrive on stdin, which makes the harness an ordinary member
of a pipeline:

```bash
cat prompt.txt | nimbus run -m <model-id>
nimbus run -m <model-id> -p -   < prompt.txt        # the same, said explicitly
```

Stdin is only read when it is *not* a terminal. Running `nimbus run -m x`
at an interactive prompt with no `-p` reports a usage error rather than
silently blocking on a read you cannot see.

What arrives on stdin reaches the model **verbatim** — indentation and trailing
newlines included, since they are meaningful in code, markdown and
delimiter-based templates. Input that is entirely whitespace is rejected as a
usage error rather than sent.

| Option | Meaning |
|---|---|
| `-m`, `--model` | Model id, as listed by `nimbus models`. Required. |
| `--provider` | Which SDK executes the run (default `bedrock`). Must match the model's `source`. |
| `-p`, `--prompt` | The user prompt; `-` or omitted reads stdin. |
| `-s`, `--system` / `--system-file` | The system prompt, inline or from a file. |
| `--toolset` | A toolset from `nimbus tools`. |
| `--max-tool-iterations` | Cap on agent loop turns (default 10). |
| `--temperature`, `--top-p`, `--max-tokens` | Sampling knobs. |
| `--guardrail-id`, `--guardrail-version` | Apply a Bedrock guardrail (Bedrock only). |

Setup failures — an unknown toolset, a guardrail on a non-Bedrock provider —
fail before anything executes and before a run row is written, exactly as they
do over HTTP. Failures *during* a run are reported on the progress stream and
persisted on the run row.

## `eval`

Three shapes, same command. Without `--run` or `--suite` it is a determinism
experiment: execute the prompt `n` times and grade the batch.

```bash
nimbus eval -m <model-id> -p 'your prompt' -n 10
```

With `--run` (repeatable) it grades runs that already exist, executing nothing
new:

```bash
nimbus eval --run 3f2a… --run 9c81… --rubric 'Penalise any tool call.'
```

With `--suite` it runs a file of test cases and grades each answer against
that case's own expectations — see **[suites.md](suites.md)**:

```bash
nimbus eval --suite cases.yaml                 # the file's model
nimbus eval --suite cases.yaml -m <other>      # same cases, another model
```

| Option | Meaning |
|---|---|
| `-n` | Repeats for a determinism experiment, clamped to 2–25 (default 10). |
| `--suite FILE` | Run a test suite from a YAML or JSON file. Run options given on the command line override the file's `run_config`; `-p`, `-n` and `--run` are errors with it. |
| `--run RUN_ID` | Grade this stored run instead of executing new ones; repeatable. |
| `--rubric` | Extra rubric text for the judge. |
| `--remote` | Insist on the stack you signed in to with `login` (already the default once signed in); fails rather than running here when you are not — see [Running on a deployed stack](#running-on-a-deployed-stack). |
| `--detach` | On a stack: submit, print the evaluation's id and link, and return without following it. |
| `--grader-model`, `--grader-provider`, `--grader-system` | The judge. Independent of the graded runs — an OpenAI judge grading Bedrock runs is a reasonable setup. A non-Bedrock `--grader-provider` **requires** `--grader-model`: the built-in default is a Bedrock model id, and no default is invented for the other providers. |

Plus every `run` option above, which describes the repeats.

The API answers `202` and runs evaluations in the background, because a request
cannot wait fifteen minutes. A CLI invocation *is* the job: it runs in the
foreground, and Ctrl-C cancels the evaluation rather than just stopping your
view of it. The terminal state lands on stdout as JSON; the grade and the
judge's reasoning are summarised on stderr.

## Running on a deployed stack

Until you sign in, everything runs on this machine and is kept in its history.
Once you `nimbus login` to a deployed stack, **commands run there**: `run`,
`eval`, `runs`, `show`, `models` and `tools` all talk to the stack, so what you
run is stored in *its* history and shows up in its web UI next to what was
started there — labelled **CLI** — with the full per-case results, the
configuration it ran with, and links to every run. Every such command says so
on stderr (`| on https://… as ada@example.com`).

```bash
nimbus login --url https://d1234abcd.cloudfront.net        # once; prompts for email + password
nimbus eval --suite suite.yaml                             # runs on the stack, followed here
nimbus eval --detach -m <model-id> -p '...'                # submit and return
nimbus runs                                                # the stack's history
nimbus --local run -m <model-id> -p '...'                  # this machine, for one command
nimbus whoami                                              # ada@example.com @ https://…
nimbus logout                                              # back to this machine for good
```

What you see is the same as on this machine: a run's text streams to stdout,
an evaluation's progress goes to stderr and its terminal state to stdout as
JSON (plus a `url`), the exit codes are the same, and `--json` streams the same
NDJSON. The difference is where it runs and is kept. An evaluation's link to its
page in the web UI is printed when it starts and again when it finishes
(`https://…/#/evals/<id>`); the page opens straight to it.

- **Where it runs.** The CLI asks the server (`GET /health`) and uses the lane
  its own UI would: the cloud lane on a deployed stack, the server's own
  process on a laptop's `nimbus serve`. Model credentials are the
  server's, not yours — `nimbus models` lists what the *stack* can use; suites,
  `--run` and every run option work as they do locally, with the server's usual
  limits (a cloud request is capped at 200,000 bytes).
- **Ctrl-C cancels it on the server**, as it would locally — the run or
  evaluation is the server's, and stopping only your view would leave it
  running unwatched. Use `eval --detach` to submit and walk away; follow it in
  the UI.
- **A dropped connection is not a failed evaluation.** A long evaluation can
  outlive one HTTP request; the CLI reconnects and carries on from where it
  was (the server replays the event log to each new subscriber), and only
  gives up — pointing you at the link — after repeated failures to connect.
- **`--local` and `--db` mean this machine.** `--db` names a local history
  file, so it implies `--local`. `eval --remote` is the opposite: it insists on
  the stack, and fails rather than running here when you are not signed in.

### Signing in

`login` reads the stack's user pool from its `/health` and signs in with the
same email and password as the web UI (the pool is invitation-only; `make
create-user` invites someone). The password is read without echo, or from
stdin with `--password-stdin` for scripts, and is never stored. An invited
user's first sign-in asks for a permanent password, which needs a terminal.
`--url` is remembered, so a later `login` only asks for the password.

The login is saved at `~/.config/nimbus/login.json` (or under
`$XDG_CONFIG_HOME`, or `$NIMBUS_CONFIG_DIR`), readable by you alone
(mode `0600`). It holds the ID token the API checks and the refresh token the
CLI uses to renew it, so it lasts until the refresh token does (30 days by
default) and a command never asks you to sign in mid-way. `logout`
revokes the refresh token and deletes the file. Plain `http://` URLs are
refused except for `localhost`, so the token never crosses a network
unencrypted. A server with sign-in switched off (a local `serve`) needs no
password: `login --url http://localhost:8000` just remembers it.

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
nimbus runs --limit 50 --model <model-id> --status error
nimbus show <run-id-or-evaluation-id>
```

`runs` pages newest-first; when more rows exist, the cursor to continue from is
printed on stderr. `show` takes either kind of id and looks in both, because
from the outside you have an id and you want to see it.

## `serve`

```bash
nimbus serve --port 8000 --reload
```

Starts the FastAPI app the SPA talks to. The UI is one way to use the harness,
so starting it is a subcommand rather than a separate thing to learn.

## Configuration

The CLI reads the same environment as the server (see the Configuration section
of the README) — `NIMBUS_`-prefixed variables, plus the standard
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OLLAMA_HOST` names.

The command was called `evalharness` before the rename to Nimbus. The old
`EVALHARNESS_*` variables are still read (after their `NIMBUS_*` names), the
old history file keeps being used until a new one exists, and a saved login
moves from `~/.config/evalharness` to `~/.config/nimbus` on first use. Only the
command name itself changed with nothing left behind: run `nimbus` where you
ran `evalharness`.

History is kept in `~/.local/share/nimbus/history.db` (`$XDG_DATA_HOME/nimbus/`
when that is set), one store for every directory you run `nimbus` from. A
`./data/nimbus.db` in the directory it starts in — a checkout's `server/` — is
used instead when it exists, so existing history stays where it was.

Library warnings (a provider that is not set up, an AWS credential lookup) stay
off the terminal; the commands explain those themselves. `-v` shows the full
log on stderr when you need the detail.

`--db` overrides `NIMBUS_DB_PATH` for one invocation, which is the quick
way to keep an experiment's history out of your main store:

```bash
nimbus --db /tmp/scratch.db run -m <model-id> -p 'throwaway'
```

It is exported into the environment rather than held privately, so
`nimbus --db … serve` starts a server reading that same store — uvicorn
imports the app by string, and with `--reload` in a child process, and the
app builds its own settings from the environment at startup.

`--db` also **pins the history backend to SQLite** for that invocation. Naming
a SQLite file while the environment says `NIMBUS_HISTORY_BACKEND=dynamodb`
would otherwise create the file and then write every run to DynamoDB anyway,
which is the opposite of the isolated scratch store `--db` exists to give you.

## Smoke-testing a deployment

`scripts/deploy_smoke.py` checks that a deployed stack actually serves requests —
`/health` answering with its real payload (so the Lambda Web Adapter booted and
FastAPI is up), auth switched on with the Cognito pool published, a protected
route returning the API's own 401 envelope, and CloudFront routing deep SPA
links to `index.html`.

```bash
python3 scripts/deploy_smoke.py --url https://your-distribution.cloudfront.net
```

Standard library only, no credentials, no model calls, no spend — every deploy
runs it. It does **not** cover anything needing a real token: whether
`Authorization` survives CloudFront, NDJSON streaming, or Cognito sign-in. The
header question is unanswerable this way on purpose, since a missing and an
invalid token deliberately return the same message.

Not to be confused with `make smoke` (`scripts/live_smoke.py`), which is gated
behind `RUN_LIVE_BEDROCK=1`, needs AWS credentials and makes real Bedrock calls.

## Why argparse

The CLI uses nothing but the standard library. A `click` or `typer` dependency
would ride into the Lambda artifact, which has a hard 250 MB unzipped ceiling
it is already pruning packages to stay under (see
`docs/serverless-deploy-infra.md`), in exchange for nothing this parser cannot
already do.
