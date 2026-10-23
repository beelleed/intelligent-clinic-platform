# Assignment 2 gap analysis (working plan)

This plan uses the assignment brief shared by the student. Confirm the final rubric with the instructor before treating it as authoritative. The public clinic site remains a synthetic English portfolio project and is not connected to a real clinic.

| Requirement in supplied brief | Current project | Next implementation step |
| --- | --- | --- |
| At least three specialized agents with Supervisor or Hierarchical orchestration | The deployed/default mode is one LangGraph agent. An opt-in local `CLINIC_AGENT_MODE=supervisor` compiles policy/RAG, operations, and read-only appointments specialists behind a manager with minimum domain routing. | Expand real-call evaluation and cross-agent traces before treating this as a completed graded deliverable. |
| Two or more LLM providers with intelligent routing | Opt-in local `CLINIC_PROVIDER_ROUTING=domain` sends policy/RAG specialist work to NVIDIA NIM and keeps the supervisor, operations, and appointments on OpenAI. A two-case local live pilot confirmed both providers can answer via the graph. The default remains OpenAI-only. | Expand human-reviewed paired tests and compare quality, latency, and cost/query. Domain routing is deterministic, not yet an empirically optimized policy. |
| Pinecone default or Chroma equivalent with ingestion | FAISS semantic index and Markdown ingestion. | Add the instructor-approved vector store and a repeatable ingestion/reindex path. Preserve FAISS as a baseline for comparison. |
| Interactive UI with SSE | The browser consumes `POST /chat/stream` progress and final-answer events; `/chat` remains a JSON API. | Token-level model streaming and richer per-specialist progress remain optional extensions, subject to the final rubric. |
| Google Cloud Run deployment | The `lumi` service was deployed on 2026-10-22 with the updated visitor UI and single-agent OpenAI mode. Supervisor/provider-routing experiments are not deployed. | Deploy an assignment-specific revision only after testing and confirming the rubric; do not expose real patient data. |
| Evaluation of latency, accuracy, and cost/query | 30-question legacy RAG retrieval evaluation, an initial two-case pilot, and a 2026-10-07 comparison covering five scenarios / six turns per mode with token and latency capture. This is not an accuracy study or dollar-cost benchmark. | Expand human-reviewed cases, verify current provider prices, and compare baseline versus new architecture. Never label automated smoke checks as clinical accuracy. |
| MCP sharing or A2A | Three MCP servers expose nine typed tools. The opt-in appointments specialist shares the doctor-directory tool with operations, while booking is excluded from its scope. | Capture cross-agent MCP traces in the final evaluation. |

## Product rationale

The project is a synthetic **multi-specialty outpatient** prototype, not a hospital-wide system or a deployment for a real clinic. It addresses a concrete information gap: patients need service locations, appointment availability, current queue and doctor-availability information, while clinic staff need a controlled way to publish changes. Deterministic operational state and staff approvals remain outside the model; agents handle multi-source questions and source-grounded explanations. The synthetic queue tool now reports the current and last issued numbers without inferring people ahead or wait from their difference; skipped numbers make that inference unreliable. A real clinic would still need an authoritative queue feed and ticket-level records before confirming individual issuance or calling order.

## Immediate sequence

1. Establish the end-to-end baseline with `clinic_agent_scenarios.json`; retain responses and human review notes.
2. Test the opt-in supervisor on the same scenarios and document routing, source fidelity, and failure cases. A single-agent comparison is useful but not explicitly required by the supplied brief.
3. Expand the provider-routed pilot and usage accounting, then add an approved vector store; record latency, quality, and verified cost results. SSE progress/final-answer delivery is implemented, but token streaming is not.
4. Add staff-controlled status updates with authentication and human approval only if the final assignment scope calls for them. A model must never publish a procedure extension on its own.

The scenario runner is intentionally local/opt-in, because live calls use model tokens and a public deployment may have stricter request quotas. See `app/evaluation/README.md`.
The initial three-case comparison and its limitations are recorded in
`docs/agent-comparison-pilot-2026-10-02.md`; the later five-scenario comparison is
in `docs/agent-comparison-2026-10-07.md`.
