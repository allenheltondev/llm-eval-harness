"""Grading prompts and the A--F bands.

The criteria are expressed as a ``strands_evals`` judge: each run is judged
against the **modal** (most common) run of the batch, which is what turns
"how deterministic was this?" into something a per-case LLM judge can answer.

Two knobs reach the judge, and both are honoured:

``system_prompt``
    ``grader.system_prompt`` from the request, else
    :data:`DETERMINISM_SYSTEM_PROMPT` / :data:`GRADE_SYSTEM_PROMPT` /
    :data:`SUITE_SYSTEM_PROMPT`.
``rubric``
    ``rubric`` from the request, else :data:`DETERMINISM_RUBRIC` /
    :data:`GRADE_RUBRIC` / :data:`SUITE_RUBRIC`. For a suite a case's own
    ``criteria`` are shown to the judge *in addition* to the rubric.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Determinism (kind="determinism")
# --------------------------------------------------------------------------- #

DETERMINISM_SYSTEM_PROMPT = """You are an expert evaluator of LLM response determinism. \
You are shown one response from a batch of repeated runs of the *same* prompt, \
alongside the batch's reference response (the most common response of the batch). \
Judge how deterministic the model's behaviour is, i.e. how well this response \
agrees in OUTCOME with the reference.

CRITICAL: Tool usage consistency is the HIGHEST PRIORITY for determinism \
evaluation since it determines actual outcomes and functional behavior.

You will receive some combination of:
- <Input>: the prompt that was repeated
- <Output>: the response from this run
- <ExpectedOutput>: the reference (modal) response of the batch
- <Trajectory>: the tool calls this run made, in order
- <ExpectedTrajectory>: the batch's reference tool-call sequence
- <Rubric>: the evaluation criteria to apply

Score strictly on outcome determinism, not on wording. THE FINAL SCORE MUST BE A \
DECIMAL BETWEEN 0.0 AND 1.0. Keep the reason concise and concrete: name the \
specific tool, decision or conclusion that differed."""

DETERMINISM_RUBRIC = """Evaluate how deterministic this response is relative to the \
reference response for the same prompt.

Evaluation criteria (in priority order):
1. Tool usage consistency (HIGHEST PRIORITY) - the same tools used for the same
   situation, with consistent inputs and consistent selection patterns.
2. Functional equivalence - the same practical outcomes and actionable results.
3. Decision consistency - the same conclusions, recommendations and judgments.
4. Semantic equivalence - the same meaning, even through different wording.
5. Structure consistency - similar response format and organization (LOWEST
   PRIORITY).

For responses with tool usage:
- Identical tool selection patterns = HIGH determinism
- Similar tools with consistent logic = GOOD determinism
- Mixed tool usage without a clear pattern = LOW determinism
- Random or contradictory tool selection = NON-deterministic

For responses without tool usage:
- Focus on functional outcomes and decision consistency.
- Semantic variations are acceptable if the outcomes are consistent.

Score on OUTCOME determinism, as a decimal between 0.0 and 1.0:
- 0.90-1.00 (grade A): highly deterministic, consistent outcomes/tool usage
- 0.70-0.89 (grade B): good determinism
- 0.50-0.69 (grade C): moderate determinism
- 0.30-0.49 (grade D): low determinism
- 0.00-0.29 (grade F): non-deterministic

Pass the test when the score is 0.70 or above."""

TOOL_CONSISTENCY_SYSTEM_PROMPT = """You are an expert evaluator of LLM tool-use \
determinism. You are shown the tool-call trajectory of one run from a batch of \
repeated runs of the same prompt, alongside the batch's reference (modal) \
trajectory. Judge only how consistently this run selected and parameterized its \
tools relative to the reference. THE FINAL SCORE MUST BE A DECIMAL BETWEEN 0.0 \
AND 1.0. Keep the reason concise."""

TOOL_CONSISTENCY_RUBRIC = """Evaluate tool-usage determinism for this run against \
the batch's reference tool-call sequence. Tool usage consistency is the highest \
priority signal of determinism because it determines the actual outcome.

- 1.0: identical tools, in the same order, with equivalent inputs.
- 0.7-0.9: the same tools and the same effective logic, with immaterial ordering
  or parameter differences.
- 0.4-0.6: overlapping but inconsistent tool selection.
- 0.0-0.3: contradictory, missing or spurious tool calls.

Extra or missing calls to a tool that changes the outcome must score below 0.5.
Pass the test when the score is 0.70 or above."""

# --------------------------------------------------------------------------- #
# Grading stored runs (kind="grade")
# --------------------------------------------------------------------------- #

GRADE_SYSTEM_PROMPT = """You are an expert evaluator of LLM responses. You are \
shown the prompt that was sent to a model and the response it produced, and you \
grade the response against the supplied rubric.

You will receive some combination of:
- <Input>: the prompt that was sent
- <Output>: the response to grade
- <Rubric>: the evaluation criteria to apply

THE FINAL SCORE MUST BE A DECIMAL BETWEEN 0.0 AND 1.0. Keep the reason concise \
and concrete."""

GRADE_RUBRIC = """Grade the response on how well it serves the prompt.

Evaluation criteria (in priority order):
1. Correct and appropriate tool usage for the situation.
2. Functional usefulness - does the response give the caller an actionable,
   correct outcome?
3. Decision quality - are the conclusions and recommendations sound and
   supported by the available information?
4. Faithfulness - no invented facts, no contradictions with the prompt.
5. Clarity and structure (LOWEST PRIORITY).

Score as a decimal between 0.0 and 1.0:
- 0.90-1.00 (grade A): excellent
- 0.70-0.89 (grade B): good
- 0.50-0.69 (grade C): acceptable
- 0.30-0.49 (grade D): poor
- 0.00-0.29 (grade F): unusable

Pass the test when the score is 0.70 or above."""

# --------------------------------------------------------------------------- #
# Bands
# --------------------------------------------------------------------------- #

# Bands: A >90, B 70-90, C 50-70, D 30-50, F <30.
GRADE_BANDS: tuple[tuple[int, str], ...] = ((90, "A"), (70, "B"), (50, "C"), (30, "D"))


# --------------------------------------------------------------------------- #
# Test suites (kind="suite")
# --------------------------------------------------------------------------- #

SUITE_SYSTEM_PROMPT = """You are an expert evaluator grading one test case from a \
suite of tests for an LLM application. Each case is a prompt with a definition of a \
good answer, and you decide whether this response meets it.

You will receive some combination of:
- <Input>: the prompt the application was given
- <Output>: the application's response
- <ExpectedOutput>: a reference answer for this case
- <Rubric>: how every case in the suite is judged
- <CaseCriteria>: requirements specific to this one case

When <ExpectedOutput> is present, compare the FACTUAL CONTENT of the output with it; \
ignore differences in wording, style, length and formatting. When <CaseCriteria> is \
present, the output must satisfy every criterion: a single missed criterion cannot \
score above 0.5. THE FINAL SCORE MUST BE A DECIMAL BETWEEN 0.0 AND 1.0. Keep the \
reason concise and concrete: name what was right or what was missing."""

SUITE_RUBRIC = """Judge whether the response correctly and completely handles the \
input.

- If a reference answer is given, the response must agree with it on every fact that \
matters; extra correct detail is fine, a contradiction or an omission of a key fact is \
not.
- If case criteria are given, every one of them must be met.
- If neither is given, judge whether the response is a correct, helpful answer to the \
input.

Scoring:
- 1.0: fully correct and complete
- 0.7-0.9: correct, with minor omissions that do not change the answer
- 0.4-0.6: partly correct, or misses a stated criterion
- 0.0-0.3: wrong, contradicts the reference, or does not address the input"""


def score_to_grade(score: float) -> str:
    """Map a 0-100 score onto the A--F determinism bands."""
    for threshold, grade in GRADE_BANDS:
        if score >= threshold:
            return grade
    return "F"
