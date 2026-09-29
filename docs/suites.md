# Test suites

A **suite** is a file of test cases for one prompt: each case is an input and a
description of what a good answer looks like. The harness runs every case, has
the judge grade each answer against *that case's* expectations, and reports
which cases pass.

It is the third kind of evaluation, and it answers a different question from
the other two:

| Kind | Question | Inputs |
|---|---|---|
| `determinism` | Does this prompt give the same answer every time? | one prompt, run `n` times |
| `grade` | How good are these runs? | runs that already exist |
| **`suite`** | **Does it still get these cases right?** | **a file of cases** |

Change the system prompt, the model or the tools, rerun the suite, and see which
cases broke.

## A suite file

YAML or JSON (JSON is valid YAML). A complete, commented example that is kept
valid by the test suite: [`examples/support-suite.yaml`](examples/support-suite.yaml).

```yaml
name: support-basics
run_config:
  model_id: amazon.nova-lite-v1:0
  system_prompt: You are a support agent for an online shoe store...
cases:
  - id: refund-window
    input: I bought shoes 20 days ago. Can I still get my money back?
    expected: No refund after 14 days, but store credit is allowed up to 30 days.
  - id: sunday-hours
    input: What time do you open on Sunday?
    criteria: Must say 10am.
```

| Field | Required | Meaning |
|---|---|---|
| `name` | no | Shown in the result. |
| `run_config` | yes | What every case runs with: the same fields as a run (`model_id`, `provider`, `system_prompt`, `inference`, `toolset`, `max_tool_iterations`, `guardrail`) **except `user_prompt`**, which is rejected — each case's `input` is the prompt. |
| `cases` | yes | 1–100 cases. |
| `cases[].id` | yes | Unique within the suite. Letters, digits, `.`, `_`, `-`; starts with a letter or digit. |
| `cases[].input` | yes | The prompt for this case. |
| `cases[].expected` | no | A reference answer. The judge compares **facts, not wording**: extra correct detail is fine; a contradiction or a missing key fact is not. |
| `cases[].criteria` | no | What *this* answer must do, in plain language (“must say 10am”, “must not promise a refund”). Every criterion must be met. |
| `cases[].assert` | no | Up to 10 deterministic checks every repeat's answer must pass — see [Assertions](#assertions). |
| `cases[].judge` | no | `false` skips the LLM judge for this case: it is scored from its `assert` checks alone. Default `true`. |
| `cases[].critical` | no | `true` marks a case a fallback must never break — see [Fallback readiness](#fallback-readiness). Default `false`. |
| `repeats` | no | Runs per case, 1–10 (default 1). A case's score is the mean across its repeats, so more than one catches a case that passes only some of the time. |
| `pass_threshold` | no | A case passes when its score (0–1) reaches this (default 0.7). |
| `min_pass_rate` | no | The fraction of cases (0–1) that must pass for `nimbus eval --gate` to exit `0`. Unset means every case. Ignored without `--gate`. |
| `calibrate` | no | `true` also checks the judge itself: see [Calibrating the judge](#calibrating-the-judge). `--calibrate` on the command line does the same. |
| `rubric` | no | How every case is judged. Defaults to the built-in suite rubric. |
| `grader` | no | The judge: `model_id`, `provider`, `system_prompt` — same as any evaluation. |
| `panel` | no | Up to 3 more judges (each `model_id`, `provider`, `system_prompt`), beside `grader` — see [A judge panel](#a-judge-panel). |
| `arms` / `matrix` | no | Model-and-prompt variants to compare — see [Fallback readiness](#fallback-readiness). A file that has them still runs its plain `run_config` with `nimbus eval --suite`. |
| `baseline` | no | The arm the others are measured against (default: an arm marked `baseline: true`, else the first). |
| `readiness` | no | What a fallback must meet: `max_regression_rate`, `max_latency_ratio`, `max_cost_ratio`. |

A case may have `expected`, `criteria`, both, or neither. With neither it is
judged on the rubric alone: is this a correct, helpful answer?

Limits are **rejected when exceeded, never clamped**: at most 100 cases, 10
repeats, 200 runs in total (cases × repeats), 10 `assert` checks per case, and
1,000 assertion checks in total (each case's checks × repeats, summed). Quietly
dropping cases would report a pass rate for tests nobody ran.

## Running one

```bash
nimbus eval --suite suite.yaml
```

Run options given on the command line **override** the file's `run_config`, and
only the ones you give — so the same suite runs against another model without
editing it:

```bash
nimbus eval --suite suite.yaml -m anthropic.claude-3-5-haiku --temperature 0
```

`inference` settings merge rather than replace: `--max-tokens 50` keeps the
file's `temperature`. `--rubric` and `--grader-*` override the file's `rubric`
and `grader` the same way.

Options that cannot apply to a suite are errors, not silently ignored: `-p`
(each case is the prompt), `-n` (set `repeats` in the file) and `--run`. A
malformed file is exit `2` with one line naming the file and every bad field:

```
nimbus: --suite suite.yaml: cases.3.input: Field required
```

Over HTTP it is the same request, with the suite inline:

```json
POST /api/v1/evaluations
{"kind": "suite", "suite": {"run_config": {...}, "cases": [...]}, "rubric": "...", "grader": {...}}
```

It runs on either lane. On the cloud lane the whole request is limited to
200,000 bytes (see [below](#limits-on-the-cloud-lane)).

## How a case is judged

Each run is graded individually by the same LLM judge as every other
evaluation, built on `strands-agents-evals`. For each case the judge sees:

- `<Input>` — the case's `input`
- `<Output>` — the application's answer
- `<ExpectedOutput>` — the case's `expected`, when given
- `<Rubric>` — the suite's rubric
- `<CaseCriteria>` — the case's `criteria`, when given

and returns a score between 0 and 1 with a reason.

The per-case criteria are the one thing the library's `OutputEvaluator` does not
do on its own: it takes a single rubric for a whole experiment. They travel on
each `Case` as `expected_assertion` (the library's field for human-authored
success assertions) and a small subclass appends them to the judge prompt
through `_build_prompt`, the library's documented override point.

The judge is **blind**: nothing in that prompt says which model or provider gave
the answer, so a comparison across models (`nimbus eval --arm`, see
[cli.md](cli.md#comparing-models)) is not tilted by the judge recognising a
favourite. A test pins this for every judge.

### A judge panel

One judge has one set of biases; a model judging answers from its own family
tends to favour them. Add judges with `panel:` in the file, or `--panel-judge`
(repeatable, `PROVIDER=MODEL`, replacing the file's list):

```yaml
grader: {model_id: anthropic.claude-sonnet-4-5-v1:0}
panel:
  - {provider: openai, model_id: gpt-4o}
```

Every judge grades every answer. A repeat's score is the **mean** of the judges
that scored it, and each case reports `judge_spread`: the widest gap between its
highest and lowest judge on any repeat, so a case the judges disagree on stands
out. A judge that fails on a repeat the others scored is left out of that mean
and named in the case's reasoning; a repeat no judge scored is a `judge_error`,
never a silent pass. Reasons are prefixed with the judge that gave them.

Each judge is a separate pass of judge spend, priced with its own rates in
`cost.judge_usd`, and every judge must be a different model.
With `calibrate`, every judge is shown the calibration probes and each probe's score in the
result is the mean across the judges that scored it.

## Fallback readiness

Ranking models says which is best. A fallback question is different: *if the primary is
unavailable, what breaks?* And because another model usually needs a prompt of its own, the
thing to test is a **model and prompt pair**, an *arm*. The runnable example is
[`examples/fallback-suite.yaml`](examples/fallback-suite.yaml), kept valid by the test suite.

```yaml
arms:
  - name: primary
    model_id: amazon.nova-pro-v1:0
    system_prompt_file: prompts/primary.md
  - name: fallback
    model_id: amazon.nova-lite-v1:0
    system_prompt_file: prompts/fallback.md
readiness:
  max_regression_rate: 0.10
  max_latency_ratio: 1.5
```

`nimbus eval --suite f.yaml --all-arms` runs each arm as its own stored evaluation and measures
every other arm against the **baseline** (the first arm unless one says `baseline: true` or the
file's `baseline:` names it; `--baseline NAME` overrides). An arm names only what it changes from
`run_config`. `system_prompt_file` is read from the suite file's own directory and nowhere else:
a borrowed suite cannot make nimbus read `~/.aws/credentials` and send it to a model provider.

### What is measured

For each other arm, the cases the baseline passes that it **fails** (its *regressions*), the
cases it fixes, and the ratio of its p95 latency and estimated cost to the baseline's. The
question is the **regression rate**: of the cases the baseline passes, the fraction the arm
now fails. A `critical` case that regresses fails the arm outright, whatever the rate.

The verdict is one of three:

| Verdict | Meaning |
|---|---|
| `not_ready` | The evidence already shows it falls short: it did not complete, a critical case regressed, the regression rate is over the limit, or it is slower or dearer than allowed. |
| `ready` | Even the exact one-sided 95% upper bound on its regression rate is inside the limit. |
| `inconclusive` | It looks fine, but the suite is too small to rule out a rate over the limit. |

`inconclusive` is the point. Zero regressions in 10 cases is consistent with a true regression
rate of about 26%, so a short suite is never allowed to certify a fallback. The reason says how
many cases would: with a 10% limit and no regressions, about **29** baseline-passing cases. A limit
of `0` can never be certified, because a true rate just above zero always remains possible.

### The caveats it addresses

| Caveat | What it does about it |
|---|---|
| Model and prompt effects are tangled | Each arm lists what it changes (`model`, `prompt`, `inference`, `tools`). A `matrix:` runs every model with every prompt and reports each axis's **main effect**, the mean pass rate per value, and which axis moves it more. An arm that changes both at once gets a warning that a gain or loss cannot be credited to either. |
| Answering well is not the same as being ready | Latency and cost ratios are part of the bar. A warning fires when the suite gives the model tools but no case asserts anything about tool calls; add `tool_called`, `tool_sequence` or `no_tool_errors` checks so a fallback that calls tools wrongly cannot pass. |
| A small suite is noise | The verdict above; a warning below 20 cases; and a sign test on the cases the arm and baseline differ on, so a difference is called chance until it is not. |
| One repeat hides flaky cases | A warning for arms that ran each case once. Raise `repeats`. |
| A judge favours its own family | A warning when a judge (or panel member) is from the same model family as an arm. Add a judge from another family to the [panel](#a-judge-panel). |

A grid instead of a list:

```yaml
matrix:
  models:  [{name: pro, model_id: amazon.nova-pro-v1:0}, {name: lite, model_id: amazon.nova-lite-v1:0}]
  prompts: [{name: primary, system_prompt_file: prompts/primary.md}, {name: fallback, system_prompt_file: prompts/fallback.md}]
  baseline: {model: pro, prompt: primary}
```

Cells are named `model/prompt`, the baseline defaults to the first model and prompt, and a
comparison holds at most 12 arms. Effects are main effects only: a prompt that helps one model and
hurts another averages out, and is not reported separately.

What no comparison can tell you: whether the fallback will be *available* when it is needed (rate
limits, regional capacity), or how it behaves on inputs the suite does not cover. The suite bounds
the second by how well it samples real traffic; `nimbus promote RUN_ID --critical` turns a run that
mattered into a case a fallback must not break.

`nimbus compare ID ID…` and the Evals tab's **Compare models** view (`#/evals/compare/…`) run the
same analysis over evaluations that already ran, so a different baseline needs no new model calls.

### Saving the result: the fallback plan

A comparison describes one moment. To let a runbook, a deploy check or an on-call engineer act on
it, write it down:

```bash
nimbus eval --suite fallback-suite.yaml --all-arms --save fallback-plan.json
nimbus compare <eval-id> <eval-id> --baseline primary --suite fallback-suite.yaml --save fallback-plan.json
nimbus plan check fallback-plan.json        # offline, no model calls; exit 3 when stale
```

The plan is a JSON file (commit it next to the suite). `fallbacks` lists only the arms whose verdict
was `ready`, best first; `evidence` keeps the `not_ready` and `inconclusive` ones with their reasons,
so the file also says what was tried and left out. Each entry pins the model, the prompt id and the
evaluation the verdict came from. The plan also pins the suite (a fingerprint of its cases, and
where the file is), the readiness bar, and when it was made.

`plan check` reads the suite file as it is now and reports every way the plan has drifted:

| Finding | Meaning |
|---|---|
| `suite_changed` | a case's input, checks, expected answer or `critical` flag changed, so the verdicts are about different questions |
| `arm_changed` | an arm now runs a different model or prompt than was judged, or is gone from the file |
| `bar_changed` | the file's `readiness:` is not the bar the arms were judged by |
| `expired` | the plan is older than `--max-age-days` (default 30, set with `--save`) |
| `unverifiable` | the suite file is missing (stale), or declares no `arms:` so models and prompts were not rechecked (advice only) |
| `no_fallback` | no arm was ready (advice only) |

Only the first four and a missing file make the plan stale and the exit code `3`. Checking never
proves a fallback still *works*, only that what was proven is what would run; re-running
`--all-arms` and saving again is what makes a stale plan current. Prompt files are read the same
way they are for a run, so a reworded prompt file is caught.

## Assertions

Some requirements do not need a judge: “must mention store credit”, “must be
valid JSON”, “under 200 words”, “must call `freeze_account`”, “must never call
`delete_*`”. A case's `assert:` list settles them by looking — instantly, for
free, and with the same answer every time.

```yaml
cases:
  - id: sunday-hours
    input: What time do you open on Sunday?
    criteria: Must say 10am.          # still judged...
    assert:                           # ...and these must pass too
      - regex: '\b10(:00)?\s*a\.?m\b'
        flags: i
      - not_contains: 9am
      - max_length: 60
        unit: words

  - id: order-json
    input: 'Return order B456 as JSON: {"order_id", "status"}'
    judge: false                      # no judge: scored by the checks alone
    assert:
      - json_schema:
          type: object
          required: [order_id, status]
          properties:
            order_id: {type: string, pattern: '^B\d+$'}
            status: {enum: [delayed, shipped, delivered]}
```

Each entry is written either as `type:` plus its fields, or — shorter — with
the type as its key, whose value fills the check's main field. These two are the
same check:

```yaml
- type: contains
  value: store credit
  case_sensitive: false

- contains: store credit
  case_sensitive: false
```

The key's value may instead be a mapping of the check's fields
(`- tool_called: {name: freeze_account, times: 1}`) — except for `json_schema`,
whose mapping *is* the schema. A check with no fields takes `true`
(`- json_valid: true`).

### On the answer

| Type | Main field | Other fields | Passes when |
|---|---|---|---|
| `contains` | `value` | `case_sensitive` (default `true`) | the answer contains `value` |
| `not_contains` | `value` | `case_sensitive` (default `true`) | it does not |
| `regex` | `pattern` | `flags`: any of `i` `m` `s` `x` | `pattern` is found anywhere in the answer (Python `re.search`; anchor with `^`/`$` to match the whole answer) |
| `equals` | `value` | `case_sensitive` (default `true`), `strip` (default `true`: ignore surrounding whitespace) | the answer is exactly `value` |
| `json_valid` | — | — | the whole answer (surrounding whitespace aside) parses as JSON — an answer wrapped in a Markdown code fence does not |
| `json_schema` | `schema` | — | the answer parses as JSON and validates against `schema`, written inline (Draft 2020-12 unless the schema's own `$schema` names another). `$ref`s must point inside the schema (`#/$defs/...`): nothing is ever fetched |
| `max_length` | `value` | `unit`: `chars` (default) or `words` | the answer is at most `value` long |
| `min_length` | `value` | `unit`: `chars` (default) or `words` | the answer is at least `value` long |
| `max_latency_ms` | `value` | — | the run took at most `value` ms, wall clock, tool calls included |

### On the tool calls

Every run records its tool calls — name, input, output, error and duration —
and these checks read that transcript. Names may be globs (`delete_*`,
`github_*`).

| Type | Main field | Other fields | Passes when |
|---|---|---|---|
| `tool_called` | `name` | `args`, `times`, `min_times`, `max_times` | enough calls match `name` (and `args`): exactly `times`, or between `min_times` and `max_times`; with none of the three, at least one |
| `tool_not_called` | `name` | — | no call matches `name` |
| `tool_sequence` | `tools` (a list) | `mode`: `subsequence` (default) or `exact` | `subsequence`: these calls happened in this order, other calls allowed in between. `exact`: these were *all* the calls, in exactly this order |
| `max_tool_calls` | `value` | — | the run made at most `value` tool calls |
| `no_tool_errors` | — | — | no tool call returned an error |

`args` is a **partial match**: every key you give must be in the call's input
with a matching value; keys you leave out are ignored. Nested objects match the
same way, lists element by element (same length). A value written
`{regex: <pattern>}` matches any string the pattern is found in.

**MCP tools are named with their server's prefix.** A tool `search_issues` on a
saved MCP server named “GitHub” is `mcp-github_search_issues` to the model and
in the transcript (`mcp-<server slug>_<tool>`, so it can never clash with a
built-in tool), and that prefixed name is what an assertion matches —
`tool_called: mcp-github_search_issues`, `tool_not_called: mcp-github_*` to
keep a case away from that server entirely, or `tool_not_called: mcp-*` to
allow built-in tools only. `nimbus mcp test <id>` lists a server's
tools without the prefix; a run's tool transcript shows them with it.

Calls are recorded in the order they *finished*. Calls the model makes one after
another come out in that order; calls it makes in parallel, in one turn, may be
recorded in either order — use `subsequence` or `tool_called` for those rather
than an `exact` sequence.

An agent example, with the built-in `fraud-detection` toolset:

```yaml
name: fraud-agent
run_config:
  model_id: amazon.nova-pro-v1:0
  toolset: fraud-detection
  system_prompt: >-
    You are a fraud analyst. Freeze an account only for confirmed takeover;
    otherwise flag the transaction for review.
  inference: {temperature: 0}
repeats: 3
cases:
  - id: takeover-freezes
    input: >-
      Account A1234 had 14 logins from new countries in an hour, then T9001
      moved $4,800 out. Handle it.
    criteria: Explains why the account was frozen.
    assert:
      - tool_called:
          name: freeze_account
          args:
            account_id: A1234
            severity: {regex: '^(high|critical)$'}
          times: 1
      - tool_sequence: [freeze_account, create_fraud_alert]
      - no_tool_errors: true
      - max_tool_calls: 4

  - id: odd-purchase-is-only-flagged
    input: T5512 on account A2001 is a $60 purchase in a new city. Anything to do?
    judge: false
    assert:
      - tool_called: {name: flag_suspicious_transaction, args: {transaction_id: T5512}}
      - tool_not_called: freeze_account
      - tool_not_called: update_*
```

### How assertions are scored

- **Every check is pass/fail, on every repeat.** Each repeat that answered gets
  one verdict per check, `{type, passed, detail}`, in the order of the list.
- **A case passes only if every repeat that answered passed all its checks, and
  its score meets `pass_threshold`.** The score is still the judge's; the
  checks are a gate on top of it. One failed check makes the case `failed` —
  even if the judge could not score it, because that much is already certain.
- **`judge: false`** skips the judge for that case. Each repeat scores `1.0` if
  it passed every check and `0.0` if not, so the case needs every repeat to
  pass. A suite whose cases all skip the judge never builds one, and runs with
  no judge credentials at all.
- A repeat that failed to run has no verdicts (it already scores `0`, as in any
  suite).
- **A bad check is rejected when the suite is submitted** — an unknown type, a
  regex that does not compile, a JSON Schema that is not one: a `422` over
  HTTP, exit `2` from `nimbus eval --suite`. It never surfaces after the runs
  have been paid for.

A suite whose cases have no `assert:` runs, scores and reports exactly as it
did before assertions existed — its result carries none of the fields below.

## Calibrating the judge

A suite is only as trustworthy as its judge. If the rubric is vague, or the
judge model is lenient, every answer can score well, and a pass rate means
nothing. `calibrate: true` (or `--calibrate`) tests that directly: for every
judged case, the judge also scores two answers whose verdicts are already known.

| Probe | Answer shown to the judge | Should |
|---|---|---|
| `reference` | the case's own `expected` answer (only when it has one) | pass |
| `empty` | an empty answer | fail |

Each probe is judged exactly as the real answers are, with the same input,
reference, criteria, rubric and judge. A probe on the wrong side of
`pass_threshold` is **flagged**:

```
▎ calibration  reference=0.94  empty=0.61  1 flagged
▎   FLAG  0.72  off-topic  judge passes an empty answer
```

A flagged `reference` means the judge would fail the answer you wrote as
correct, so the case's `expected` or `criteria` probably disagree with each
other or with the rubric. A flagged `empty` means the judge passes an answer
with nothing in it, so that case's verdicts can't be trusted.

Calibration never changes a verdict or a score. It costs two extra judge calls
per judged case (one for a case without `expected`), not per repeat, and those
count toward judge spend. Cases with `judge: false` are not calibrated.

In the result, each calibrated case carries `calibration: {reference, empty}`
(`null` for a probe the judge returned no verdict on, which is never flagged),
and the suite carries `calibration: {cases, reference_mean, empty_mean,
flagged}`, with `flagged` listing `{id, probe, score}`. A suite that does not
calibrate has neither.

## Reading the result

Every case gets a line:

```
▎ grade C  score=72  3/4 cases passed
▎   PASS  0.95  refund-window
▎   FAIL  0.20  sunday-hours  says 9am; the case requires 10am
▎   PASS  0.90  late-return
▎   ERR     -   off-topic  model unavailable
```

A case that failed an assertion lists each failed check under its line, with
the repeats it failed on and what was seen the first time:

```
▎   FAIL  0.90  sunday-hours  2 of 9 assertion checks failed
▎           x regex (repeats 1, 3 of 3): no match for "/\\b10(:00)?\\s*a\\.?m\\b/i"
▎           x not_contains (repeat 3 of 3): output contains "9am" at char 14
```

| `status` | Meaning | Counts as passed? |
|---|---|---|
| `passed` | Scored at or above `pass_threshold`, and passed every assertion on every repeat that answered. | yes |
| `failed` | Scored below it (the judge's reason is shown), or failed an assertion (the failed checks are shown). | no |
| `error` | No run of this case completed — the answer never came back. | no |
| `judge_error` | At least one answer came back but the judge returned no verdict for it. | no |

**Only a fully scored case can pass.** A case that did not run, or any of whose
answers went unjudged, has not shown that it works.

**Every repeat counts.** A case's score is the mean over *all* its repeats, and
a repeat that failed to run scores 0 — so with `repeats: 10`, nine failures and
one perfect answer is 0.1, not 1.0. A repeat that ran but was not judged is
missing evidence rather than a zero, so it makes the whole case `judge_error`.

Two headline numbers, answering different questions:

- **`metrics.pass_rate`** — cases passed ÷ all cases. The strict one: `error`
  and `judge_error` count against it.
- **`score`** / **`grade`** — the mean score over the cases the judge *did*
  score, ×100, with the usual A–F bands. How good the answers were, where there
  were answers.

The full result on stdout (and from `GET /evaluations/{id}`) adds, per case,
`score`, `scores` (one per repeat, in run order: `0.0` for a repeat that failed
to run, `null` for one that was not judged), `reasoning` (clipped to 1,000
bytes as stored), `repeats` (the same per-repeat list with each repeat's run:
`{run_id, ran, score}`, so a failed repeat keeps its place and, when its run was
recorded, its link), `run_ids` (the successful runs), `runs: {total,
succeeded}` and `error`; and at the top level
`metrics.cases_total/passed/failed/errored`, `failed_runs` (each with its
`case_id`, and its `run_id` when the failed run was recorded, as in every
evaluation's result), and `suite: {name, repeats, pass_threshold}`.

A case with an `assert:` list also carries:

| Field | Meaning |
|---|---|
| `judged` | `false` when the case set `judge: false`. |
| `assertions` | `{total, passed, failed}`: every check on every repeat that answered. |
| `repeats[].assertions` | One `{type, passed, detail}` per check, in the order of the case's list; `null` for a repeat that did not run. `detail` is a one-line account of what was seen, clipped to 200 bytes as stored. |
| `repeats[].assertions_passed` | `true` when that repeat passed every check; `null` when it did not run. |

and the result's `metrics` gains `assertions_total` and `assertions_failed`
(summed over the cases). A case without assertions carries none of these, and
a suite without any has no new `metrics` keys.

Progress events are the same as any evaluation's, and each run event also
carries the `case_id` it belongs to. Determinism and grade events do not carry
the field at all, so their wire format is unchanged.

## Exit codes

A suite with failing cases exits **`0`**, like an evaluation that earns an F:
the evaluation worked, and it is telling you the answers are wrong. `1` still
means the harness itself failed.

To fail a CI job on the answers, ask for it: `--gate` exits **`3`** when the
pass rate is below the file's `min_pass_rate` (every case, when unset),
`--fail-on-case-failure` when any case does not pass, and `--fail-under SCORE`
when the overall score (0–100) is below `SCORE`. `--junit PATH` writes a JUnit
report with one testcase per case. See [the CLI's exit codes](cli.md#exit-codes)
for the details and a GitHub Actions job.

```bash
nimbus eval --suite cases.yaml --gate --junit evals-junit.xml
```

## Cost and budgets

Every run records an **estimated** cost, `metrics.cost_usd`, next to its token
counts, and every evaluation's result carries a `cost` block:

```json
"cost": {
  "currency": "USD", "estimate": true, "pricing_as_of": "2026-09-27",
  "runs_usd": 0.0412, "judge_usd": 0.0087, "total_usd": 0.0499,
  "judge_tokens": {"input_tokens": 9120, "output_tokens": 1402, ...},
  "max_cost_usd": 0.25
},
"budget_exhausted": false
```

`runs_usd` is the model under test (every attempt, throttled retries
included); `judge_usd` is the grader, measured separately. A `grade`
evaluation executes no runs, so its `runs_usd` is `0`.

**These are estimates, not a bill.** A cost is the model's published on-demand
list price per million tokens (input, output, prompt-cache reads and writes)
times the token counts the provider reported. It knows nothing of negotiated
discounts, free tiers, batch or provisioned pricing, or taxes. Bedrock prices
are `us-east-1`'s; the `global.` inference profile is priced at the base rate,
and for the models that charge more on regional and geographic (`us.`, `eu.`,
...) endpoints (Claude 4.5 and later, Nova 2 Lite) the 10% premium is applied.
The table lives in `server/nimbus/pricing.py`, with its sources and the date it
was checked.

**Unknown is `null`, never `0`.** A model that is not in the table reports
`cost_usd: null`, and any total that includes it is `null` too. Ollama is
priced at `0` because it runs on your hardware.

### Overriding prices

Point `NIMBUS_PRICING_FILE` at a JSON file to add a model or replace a price
without a code change. Prices are USD per 1M tokens:

```json
{
  "models": {
    "bedrock:amazon.nova-pro-v1:0": {"input": 0.8, "output": 3.2, "cache_read": 0.2},
    "openai:gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "my-fine-tune": {"input": 1.0, "output": 2.0},
    "anthropic:claude-opus-4-1": null
  }
}
```

A key is `provider:model_id` or a bare `model_id`, matched as given and in its
normalized form (region prefix, `:0` suffix and snapshot date dropped), so
`bedrock:amazon.nova-pro` covers `us.amazon.nova-pro-v1:0` too. `cache_read`
and `cache_write` are optional and default to the input price. `null` marks a
model as unpriced. The file wins over the built-in table, is re-read when it
changes, and a malformed one is logged and ignored. It must exist where the
runs execute: for a cloud evaluation that is the worker Lambda's environment.

### A budget

`max_cost_usd` caps what an evaluation's runs may spend — in the request
(`"max_cost_usd": 5`), at the top level of a suite file (next to `rubric`), or
with `nimbus eval --max-cost 5`, which overrides the file.

Before each run starts, the engine projects the spend: what has been spent so
far, plus the runs in flight and the new one at the mean cost of the runs
finished so far. Once that would exceed the budget it stops starting runs —
for good — and the evaluation finishes normally: `completed`, graded on the
runs that did happen, with `budget_exhausted: true` and `skipped_runs`. It is
not an error. In a suite, a case that never ran is an `error` case whose error
code is `budget_exhausted`, so it counts against `pass_rate`.

Runs already in flight always finish, and before the first run finishes there is
no estimate, so the first batch of concurrent runs (up to 3) always starts: the
spend can pass the budget by that much. The judge's cost is reported but not
budgeted — it runs after the last run is scheduled, and grading the runs that
did happen is the point. A budget needs a price: `max_cost_usd` on a model the
table does not know is a `400 budget_model_unpriced` up front.

## Limits on the cloud lane

The cloud lane stores the whole request on the evaluation's DynamoDB `META`
item, and the same item later holds the result. An item is capped at 400 KB, so
a cloud request may be at most **200,000 bytes**; anything larger is a
`413 evaluation_too_large` before anything is written. That is roughly a hundred
cases of a couple of kilobytes each. The local lane has no such limit.

The result is held to **180,000 bytes** for the same reason, measured as stored
(JSON with non-ASCII escaped, so a CJK character is six bytes and an emoji
twelve). Ids, statuses, scores and assertion verdicts always survive; if the
judge's reasoning, assertion details and error messages would push past that,
they are cut shorter — dropped entirely (`null`) if need be — and the result
carries `truncated: true`. A text cut short ends in `…`.

## Not built yet

- **Launching suites from the web UI.** A suite evaluation already appears in
  the Evals tab with its grade, score and pass summary (“3/4 cases passed;
  failed: …”), and its page has the per-case table — verdicts, scores,
  assertion results and runs — but launching one is CLI and API only.
- **Comparing two runs of a suite** — which cases changed between yesterday's
  prompt and today's.
