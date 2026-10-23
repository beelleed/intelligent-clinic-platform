# Clinic agent evaluation

`clinic_agent_scenarios.json` is an English-language, end-to-end question set for the current synthetic assistant and future Assignment 2 revisions. It covers multi-specialty directory and appointment questions, live operations, skipped/unissued numbers, multi-source retrieval, session memory, unsupported requests, privacy, medical boundaries, and prompt injection.

Run the local unit tests first:

```powershell
python -m pytest tests/test_clinic_agent_eval.py -q
```

To run a few scenarios against a **running, authorized** chat service, explicitly supply its URL:

```powershell
python -m app.evaluation.clinic_agent_eval --base-url http://127.0.0.1:8080 --max-cases 3 --output data/clinic_agent_eval.json
```

Use `--case-id specialty-appointment-and-policy` to run only one scenario;
repeat `--case-id` to select several. This is useful for low-cost follow-up
checks after a fix.

The runner does **not** start a server. It does not run automatically during tests or deployment. Each conversation uses a fresh session ID; turns within a conversation share it. The default limit of three cases and 21-second delay reduce accidental quota exhaustion, but a multi-turn case can still consume more than one request. Do not point it at a public service without checking that chat is enabled and that API usage is within budget.

Each saved case now includes every question and response, per-turn latency and
verified source names, plus blank `human_review` fields (`correct`, `grounded`,
`safe`, and `notes`). Fill those fields only after reviewing the full answers
against the underlying synthetic data. For a paired local comparison, run the
same `--case-id` values once against the single-agent port and once against the
supervisor port, save each report, then compare the saved files offline:

```powershell
python -m app.evaluation.compare_agent_reports --single data/single-eval.json --supervisor data/supervisor-eval.json --output data/agent-comparison.json
```

The comparison aligns case IDs and reports descriptive per-case latency and
smoke-check outcomes. An HTTP error in either mode marks that pair incomplete.
It does not start a model, estimate accuracy, or calculate cost. Keep reports
local if they contain questions or responses that should not be published.

The only automatic checks are whether verified source citations are present and whether a few prohibited phrases appear. They are smoke checks, **not** correctness, medical-safety, or groundedness scores. Review the full response and source evidence for every case, especially cases marked as known product gaps. The HTTP chat API does not expose model token usage, so the HTTP runner reports request latency but **cannot** calculate cost per query. Use the local runner below for provider-reported token counts; verify prices before making any cost claim on a résumé.

The synthetic queue tool returns the current number and last issued number but no longer estimates people ahead or wait from ticket-number differences. Queue-only answers and queue-plus-missed-number-policy answers are rendered from verified tool data in both agent modes. In supervisor mode, the fixed missed-number policy is read directly by its known document ID rather than asking the policy specialist to select one. This requires the `queue-and-wait-times` document's missed-number section; if it is unavailable, the answer says the policy cannot be verified rather than inventing one. The skipped-number case checks that the answer does not reconstruct an unreliable count; its narrow phrase check still requires human review. Other mixed queue questions still use model synthesis and need separate review. The staff-update case checks that the current agent does not falsely claim to publish a delay; no authenticated staff update feature exists yet.

For an opt-in **local** paired run that captures each model call's reported token
usage (including which provider and specialist made the call), use:

```powershell
python -m app.evaluation.run_local_comparison --case-id clinic-hours --case-id specialty-appointment-and-policy
python -m app.evaluation.run_local_comparison --case-id clinic-hours --case-id specialty-appointment-and-policy --execute
```

The first command is a dry run. The second initializes the single-agent graph
with OpenAI, then the three-specialist graph with NVIDIA for policy/RAG and
OpenAI for other roles. It makes four chat turns plus any internal model calls;
those calls may incur provider charges. The default cap is six chat turns per
paired run. Reports are saved under ignored `data/agent-evals/` and include
answers, citations, per-turn latency, and model token usage when the provider
reports it. Missing token usage is **unknown**, not zero. Dollar cost is not
calculated without verified current rates for both providers. Review both
answers against source data before making quality or cost claims.

The initial two-case local pilot (2026-10-04) is too small for an overall
win-rate claim. On `clinic-hours`, the single agent did not retrieve the hours,
while the supervisor/NVIDIA policy specialist did. Both modes passed the
narrow source/phrase checks on `specialty-appointment-and-policy`. The
supervisor took longer on both cases and made more model calls. Exact answers
and token counts are in ignored local reports, not shipped with the
repository. Human review and a larger paired set are still required.

For a single-mode diagnostic, add `--mode supervisor` or `--mode single`.
Model-call records include `duration_ms`, making slow provider steps visible.
In a later same-question pilot, reducing redundant specialist consultations
and disabling optional NVIDIA extended reasoning brought one mixed question
from about 42.9 seconds to about 17.9 seconds, with the same narrow automated
checks passing. These runs were on a shared remote endpoint and are too few
to guarantee that latency or establish a quality improvement.

The 2026-10-07 five-scenario run completed six chat turns per mode. Single-agent
mean turn latency was 4.763 seconds with 7 model calls; supervisor mean turn
latency was 8.266 seconds with 19 model calls. Both passed all five smoke checks.
Assistant review found unsupported speculation in the single-agent unknown-doctor
answer, subsequently addressed by a verified-directory guard. The run predates
that guard and is not a post-fix benchmark. No independent human accuracy score
or dollar cost was calculated. Raw tool invocations are not captured, so saved
citations alone cannot prove a fresh tool read on each follow-up. See
[`docs/agent-comparison-2026-10-07.md`](../../docs/agent-comparison-2026-10-07.md).
