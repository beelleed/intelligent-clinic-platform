# Resume project description

AI-Powered Clinic Operations and Knowledge Management Platform  
August 2026 – Present

- Built a FastAPI and LangGraph clinic assistant deployed on Google Cloud Run,
  integrating three MCP servers and nine tools for policy retrieval, synthetic
  queue and procedure status, doctor schedules, and anonymous appointment requests.
- Evaluated an OpenAI embedding and FAISS retrieval pipeline on a 30-query
  benchmark, achieving 100% Recall@2 on 20 answerable queries and improving
  unsupported-query rejection from 20% to 90% with LLM relevance validation.
- Designed an experimental LangGraph supervisor with three scoped specialists
  and task-based OpenAI/NVIDIA routing; compared single-agent and supervisor
  responses across five scenarios using latency, reported tokens, and source checks.
- Implemented session isolation, request throttling, validated tool-based
  responses, and regression coverage for API, MCP, retrieval, memory, and failure
  handling; packaged the application in a multi-stage, non-root Docker image.

## 📝 Interview context

The public deployment uses the single-agent workflow. The three-specialist,
dual-provider workflow is a local experiment, not the deployed production mode.
The five-scenario comparison observed higher supervisor latency and model-call
counts without a clear answer-quality advantage. Do not claim that multi-agent
or provider routing improves accuracy, reduces costs, or was deployed to Cloud Run.

The retrieval percentages apply to the original small RAG benchmark, not all
clinic questions. The site uses synthetic data and has no patient-record access.
