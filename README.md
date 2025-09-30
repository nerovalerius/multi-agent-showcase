# Multi-Agent Dynatrace Observability Showcase

LLM-driven multi-agent system for Telemetry, Problems, Security, and DevOps. Orchestrated with LangGraph. Data via the Dynatrace MCP server. UI with Gradio, optional CLI.


## Architecture

The system is organized into three layers: a top-level supervisor and domain-specific teams.

![Architecture](https://github.com/nerovalerius/multi-agent-showcase/imgs/blob/main/imarchitecture.jpg?raw=true)

### 1. Teams Supervisor
- Routes user requests to one or more domain teams: Telemetry, Problems, Security, DevOps.
- Ensures each team is called at most once.
- Always ends with FINISH after at least one team responded.

### 2. Domain Teams
Each domain team has its own supervisor and two workers: a Fetcher and an Analyst.  
Workflow: Fetcher → Analyst → FINISH

#### Telemetry Team
- Fetcher: retrieves logs, metrics, spans via Dynatrace MCP tools.
- Analyst: analyzes anomalies, correlates signals, suggests mitigation.

#### Problems Team
- Fetcher: fetches active problems (list_problems).
- Analyst: identifies root causes, impact, prioritizes issues.

#### Security Team
- Fetcher: fetches vulnerabilities (list_vulnerabilities).
- Analyst: ranks risks, groups CVEs, highlights exposure, proposes mitigations.

#### DevOps Team
- Fetcher: retrieves deployment events, SLO/SLI data, error budgets.
- Analyst: evaluates health gates, canary rollbacks, error budget status, suggests remediation.

### 3. Tools
- Dynatrace MCP server:
  - dynatrace_documentation
  - generate_dql_from_natural_language
  - verify_dql
  - execute_dql
  - list_problems
  - list_vulnerabilities
- Retriever tools (FAISS index from dynatrace_rules):
  - telemetry, problems, security, devops, common

### 4. Guardrails
- Active on all user input.
- Blocks toxic language, profanity, banned terms (e.g. datadog, bomb).
- On violation, returns safe response instead of executing requests.

### 5. Execution Flow
1. User input → Teams Supervisor decides which team to call.
2. Domain Supervisor runs: Fetcher → Analyst.
3. Analyst output → back to Teams Supervisor.
4. Teams Supervisor may call another team or route to FINISH.
5. Final combined response returned to user.

## Requirements
- `uv` package manager (`pipx install uv` or `pip install uv`)
- OpenAI API key
- Dynatrace environment URL and platform token

## Quickstart

1. **Initialize project**
   uv init

2. **Install Guardrails**
   powershell
   scripts\install_guardrails.bat

3. **Create `.env` in repo root and fill values**
   Create a file named `.env` at the repository root with:

   env
   # OpenAI
   OPENAI_API_KEY=sk-...

   # Dynatrace
   DT_ENVIRONMENT=https://<env>.apps.dynatrace.com
   DT_PLATFORM_TOKEN=dtp_...
   DT_ACCOUNT_PW=

   # Tracing / Observability
   TRACELOOP_BASE_URL=https://<tenant>.live.dynatrace.com/api/v2/otlp
   TRACELOOP_HEADERS=Authorization=Api-Token <YOUR_TOKEN>
   OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta

   # Optional: LangSmith
   LANGSMITH_TRACING=
   LANGSMITH_ENDPOINT=
   LANGSMITH_API_KEY=
   LANGSMITH_PROJECT=

   # Optional
   GUARDRAILS_API_KEY=

4. **Start Gradio showcase**
   powershell
   scripts\start_gradio_showcase.bat

   Open the printed local URL, default http://127.0.0.1:7860.

### Optional: start CLI
powershell
scripts\start_cli_showcase.bat

## Repository layout
.env
.gitignore
dynatrace_rules/
dynatrace_rules_index/
scripts/
  install_guardrails.bat
  start_cli_showcase.bat
  start_dynatrace_mcp.bat
  start_gradio_showcase.bat
src/
  apps/
    cli_chat.py
    gradio_chat.py
  graphs/
    main_graph.py
  prompts/
    prompts.py
  tools/
    mcp_servers.py
    retrievers.py
  utils/
    __init__.py
.venv/

## Scripts
- scripts/install_guardrails.bat — install Guardrails and hub deps
- scripts/start_gradio_showcase.bat — launch the Gradio UI
- scripts/start_cli_showcase.bat — launch the CLI
- scripts/start_dynatrace_mcp.bat — helper to run the Dynatrace MCP server

## Troubleshooting
- npx not found → install Node.js 18+.
- Dynatrace auth errors → verify DT_ENVIRONMENT and DT_PLATFORM_TOKEN.
- Guardrails hub errors → rerun scripts\install_guardrails.bat.
- FAISS index issues → delete dynatrace_rules_index/ and restart to rebuild.

## License
TBD
