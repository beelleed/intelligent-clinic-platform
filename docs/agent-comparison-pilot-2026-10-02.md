# Three-case agent comparison pilot (2026-10-02)

This is a **small exploratory run**, not a benchmark or a clinical accuracy
measurement. Both local modes received the same first three questions in
`app/evaluation/clinic_agent_scenarios.json`, one independent session per case.
The model and synthetic clinic data were configured locally; no public Cloud
Run service or real patient data was used. Each question was run only once per
mode, so sampling variance and changing operational timestamps remain.

The first sandboxed attempt returned model-connection HTTP 502 responses and
was excluded. The successful comparison was run locally with model network
access. Raw responses and latencies are in ignored local files
`tmp/clinic-eval-single-20261002.json` and
`tmp/clinic-eval-supervisor-20261002.json`.

| Scenario | Single agent | Three-agent supervisor |
| --- | --- | --- |
| Current queue | 6.2 s; missing queue-status source. The answer reported a tool error instead of the current number. | 7.0 s; source check passed. It reported ticket 18 and a source update time. |
| Dermatology and ENT doctors/locations | 17.0 s; directory found the doctors, but schedule calls used invalid IDs `1` and `2`, so locations were missing. | 21.6 s; routed only to policy knowledge, then incorrectly said it could not list doctors or schedules. |
| Dermatology slot, location, and booking policy | 3.8 s; used an invalid/unsupported `synthetic_derm` identifier for availability and omitted schedule and policy sources. | 17.4 s; routed only to policy knowledge, omitted live appointment/directory/schedule sources, and could not answer current availability. |

The narrow automatic source checks passed **0/3** for the single agent and
**1/3** for the supervisor. Mean request latency was **9.0 s** versus
**15.3 s**, respectively. These are not statistical estimates: the sample is
too small, and the checks do not establish factual correctness, grounding, or
medical safety. Manual review found incomplete answers in both modes. A
booking-phrase check initially flagged negated/conditional wording; the
scenario pattern was narrowed after inspection, and the saved responses were
regraded without additional model requests.

## What to fix before expanding the pilot

1. Make the supervisor's routing select operations and appointments for
   questions about clinician identity, schedules, and available slots. Policy
   retrieval alone cannot answer those live-data questions.
2. Reduce wrong-ID tool calls in the single-agent path. The doctor directory
   returns IDs such as `dr-rivera` and `dr-park`; the observed schedule calls
   used `1` and `2` instead.
3. Capture structured tool-call traces and token usage before claiming a cost
   or routing-efficiency comparison. Current API responses expose neither.
4. Repeat the same scenarios after fixes, then expand to the safety and
   uncertainty cases with human review. Do not report automated checks as
   clinical accuracy.

## Follow-up fixes and retests

The single-agent path now retrieves directory IDs and explicitly requested
queue, schedule, appointment, and policy evidence before the model answers.
The supervisor has a minimum domain route, and specialist reports expose the
successful operations/appointment tool data to the manager. Concurrent
synthetic database initialization is serialized, and an unknown appointment
doctor ID now returns a tool error rather than an empty availability list.

In a later three-case run, both modes satisfied the narrow source checks on
all three cases. Manual review still found a booking-wording error in the
single-agent mixed question and a missing-slot explanation in the supervisor
mixed question. Each was corrected and rechecked on that one case: both later
answers named Dr. Rivera's two advertised synthetic slots at 09:00 and 09:30
on 2026-10-08, gave Specialty Suite as her recurring location, and said that
viewing a slot does not create a booking. The supervisor's later answer was
still verbose and speculated about queue status as an appointment signal; its
prompt now explicitly prohibits that inference. This last prompt change has
not been re-measured with a live model.

These are iterative, single-run observations, not a controlled accuracy or
cost comparison. The ignored raw local reports remain under `tmp/` and are
not included in the repository.
