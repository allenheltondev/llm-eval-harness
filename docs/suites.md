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
| `repeats` | no | Runs per case, 1–10 (default 1). A case's score is the mean across its repeats, so more than one catches a case that passes only some of the time. |
| `pass_threshold` | no | A case passes when its score (0–1) reaches this (default 0.7). |
| `rubric` | no | How every case is judged. Defaults to the built-in suite rubric. |
| `grader` | no | The judge: `model_id`, `provider`, `system_prompt` — same as any evaluation. |

A case may have `expected`, `criteria`, both, or neither. With neither it is
judged on the rubric alone: is this a correct, helpful answer?

Limits are **rejected when exceeded, never clamped**: at most 100 cases, 10
repeats, and 200 runs in total (cases × repeats). Quietly dropping cases would
report a pass rate for tests nobody ran.

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

## Reading the result

Every case gets a line:

```
▎ grade C  score=72  3/4 cases passed
▎   PASS  0.95  refund-window
▎   FAIL  0.20  sunday-hours  says 9am; the case requires 10am
▎   PASS  0.90  late-return
▎   ERR     -   off-topic  model unavailable
```

| `status` | Meaning | Counts as passed? |
|---|---|---|
| `passed` | Scored at or above `pass_threshold`. | yes |
| `failed` | Scored below it. The judge's reason is shown. | no |
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

Progress events are the same as any evaluation's, and each run event also
carries the `case_id` it belongs to. Determinism and grade events do not carry
the field at all, so their wire format is unchanged.

## Exit codes

A suite with failing cases exits **`0`**, like an evaluation that earns an F:
the evaluation worked, and it is telling you the answers are wrong. `1` still
means the harness itself failed. Failing a CI job below a pass rate is not built
yet — it would be an explicit option (`--fail-under 0.9`), not a change to what
the existing exit codes mean.

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
twelve). Ids, statuses and scores always survive; if the judge's reasoning and
error messages would push past that, they are cut shorter — dropped entirely
if need be — and the result carries `truncated: true`.

## Not built yet

- **Suites in the web UI.** A suite evaluation already appears in the Evals tab
  with its grade, score and pass summary (“3/4 cases passed; failed: …”), but
  launching one and the per-case table are CLI and API only.
- **`--fail-under`** for CI, as above.
- **Comparing two runs of a suite** — which cases changed between yesterday's
  prompt and today's.
