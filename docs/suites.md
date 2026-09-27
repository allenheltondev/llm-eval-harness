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
| `repeats` | no | Runs per case, 1–10 (default 1). A case's score is the mean across its repeats, so more than one catches a case that passes only some of the time. |
| `pass_threshold` | no | A case passes when its score (0–1) reaches this (default 0.7). |
| `min_pass_rate` | no | The fraction of cases (0–1) that must pass for `nimbus eval --gate` to exit `0`. Unset means every case. Ignored without `--gate`. |
| `rubric` | no | How every case is judged. Defaults to the built-in suite rubric. |
| `grader` | no | The judge: `model_id`, `provider`, `system_prompt` — same as any evaluation. |

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
