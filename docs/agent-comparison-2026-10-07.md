# Local agent comparison — 2026-10-07

## 📋 Setup

Five scenarios covered specialty schedules, appointment availability with policy,
procedure status with policy, a two-turn procedure follow-up, and an unknown
doctor. Each mode completed six chat turns. Single mode used OpenAI; supervisor
mode used OpenAI with NVIDIA for policy/RAG. This run used commit `389ddf1` and
predates the subsequent unknown-doctor response guard.

## 📊 Observations

| Measurement | Single agent | Supervisor |
| --- | ---: | ---: |
| Completed chat turns | 6 | 6 |
| Mean turn latency | 4.763 s | 8.266 s |
| Model calls | 7 | 19 |
| Reported model input tokens | 13,572 | 26,897 |
| Reported model output tokens | 1,145 | 3,347 |
| Citation/phrase smoke checks | 5/5 | 5/5 |

Supervisor calls comprised 17 OpenAI and 2 NVIDIA calls. Token totals exclude
embeddings. Latency excludes initialization and the runner's inter-turn delay.
Dollar costs have not been calculated.

Assistant review compared answers with read-only synthetic SQLite records and
policy Markdown. Both modes reported the expected schedules, slots, procedure
facts and booking limitations. Both retained Dr. Chen in the follow-up answer.
The single-agent unknown-doctor answer added unsupported speculation about an
alternate name or roster, and a stray text fragment. Several supervisor answers
were longer than necessary; its appointment-plus-policy turn took 23.789 seconds.

The unknown-doctor issue was subsequently addressed by rendering directory
absence directly from valid, current-turn tool evidence for narrow queue
questions. This post-run change does not retroactively improve the scores above.

## 🔎 Interpretation and limits

The tested cases did not establish a clear supervisor quality advantage. The
single-agent public demo remains the recommended configuration; the supervisor
is retained for experiments with orchestration and provider routing.

These are single observations in a small, fixed-order run, not independent human
accuracy judgments. The two configurations change both architecture and provider
routing. Shared data and cache state, provider variability, and observation time
also differ. Automatic citation checks do not establish correctness or safety.
The runner does not save raw tool invocations, so the saved follow-up citations
cannot alone prove that each turn performed a fresh read.

Raw reports and the detailed assistant review remain in the Git-ignored local
folder `data/agent-evals/paired-20261007-fivecases/`.
