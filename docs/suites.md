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
evalharness eval --suite suite.yaml
```

Run options given on the command line **override** the file's `run_config`, and
only the ones you give — so the same suite runs against another model without
editing it:

```bash
evalharness eval --suite suite.yaml -m anthropic.claude-3-5-haiku --temperature 0
```

`inference` settings merge rather than replace: `--max-tokens 50` keeps the
file's `temperature`. `--rubric` and `--grader-*` override the file's `rubric`
and `grader` the same way.

Options that cannot apply to a suite are errors, not silently ignored: `-p`
(each case is the prompt), `-n` (set `repeats` in the file) and `--run`. A
malformed file is exit `2` with one line naming the file and every bad field:

```
evalharness: --suite suite.yaml: cases.3.input: Field required
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
| `judge_error` | It ran, but the judge returned no verdict. | no |

**Only a scored case can pass.** A case that did not run, or whose answer was
never judged, has not shown that it works.

Two headline numbers, answering different questions:

- **`metrics.pass_rate`** — cases passed ÷ all cases. The strict one: `error`
  and `judge_error` count against it.
- **`score`** / **`grade`** — the mean score over the cases the judge *did*
  score, ×100, with the usual A–F bands. How good the answers were, where there
  were answers.

The full result on stdout (and from `GET /evaluations/{id}`) adds, per case,
`score`, `scores` (one per repeat), `reasoning` (clipped to 1,000 characters),
`run_ids`, `runs: {total, succeeded}` and `error`; and at the top level
`metrics.cases_total/passed/failed/errored`, `failed_runs` (each with its
`case_id`), and `suite: {name, repeats, pass_threshold}`.

Progress events are the same as any evaluation's, and each run event also
carries the `case_id` it belongs to. Determinism and grade events do not carry
the field at all, so their wire format is unchanged.

## Exit codes

A suite with failing cases exits **`0`**, like an evaluation that earns an F:
the evaluation worked, and it is telling you the answers are wrong. `1` still
means the harness itself failed. Failing a CI job below a pass rate is not built
yet — it would be an explicit option (`--fail-under 0.9`), not a change to what
the existing exit codes mean.

## Limits on the cloud lane

The cloud lane stores the whole request on the evaluation's DynamoDB `META`
item, and the same item later holds the result. An item is capped at 400 KB, so
a cloud request may be at most **200,000 bytes**; anything larger is a
`413 evaluation_too_large` before anything is written. That is roughly a hundred
cases of a couple of kilobytes each. The local lane has no such limit. Per-case
reasoning is clipped for the same reason: so the result still fits.

## Not built yet

- **Suites in the web UI.** A suite evaluation already appears in the Evals tab
  with its grade, score and pass summary (“3/4 cases passed; failed: …”), but
  launching one and the per-case table are CLI and API only.
- **`--fail-under`** for CI, as above.
- **Comparing two runs of a suite** — which cases changed between yesterday's
  prompt and today's.
