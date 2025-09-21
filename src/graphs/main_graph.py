import os
# import sys; sys.path.append("..")
import asyncio
from pathlib import Path
from dotenv import load_dotenv

from traceloop.sdk import Traceloop

from typing import Literal
from typing_extensions import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent
from langchain.chat_models import init_chat_model
from langchain.tools.retriever import create_retriever_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, START, END, MessagesState

from src.tools.retrievers import RetrieverFactory
from src.prompts.prompts import PromptsFactory

root_dir = Path(__file__).resolve().parents[2]
env_path = root_dir / ".env"
dynatrace_rules_dir = Path(__file__).resolve().parents[2] / "dynatrace_rules"
dynatrace_master_rules = (dynatrace_rules_dir / "DynatraceMcpIntegration.md").read_text(encoding="utf-8")
dynatrace_query_rules = (dynatrace_rules_dir / "reference" / "DynatraceQueryLanguage.md").read_text(encoding="utf-8")
dynatrace_problem_rules = (dynatrace_rules_dir / "reference" / "DynatraceProblemsSpec.md").read_text(encoding="utf-8")
dynatrace_vuln_rules = (dynatrace_rules_dir / "reference" / "DynatraceSecurityEvents.md").read_text(encoding="utf-8")

#####################################
# Load environment variables
#####################################
load_dotenv(dotenv_path=env_path, override=True)
DT_ENVIRONMENT = os.getenv("DT_ENVIRONMENT")
DT_PLATFORM_TOKEN = os.getenv("DT_PLATFORM_TOKEN")
os.environ['OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE'] = "delta"

#####################################
# Initialize Traceloop for Observability
#####################################
Traceloop.init(app_name="multi-agent-showcase")

# TODO: OpenLLMetry oder OpenTelemetry einbauen und in Dynatrace, Traceloop o.a einbauen zur Observability
# TODO: Guardrails einbauen


class State(MessagesState):
    next: str

##############################################################
# LangGraph
##############################################################
async def build_graph() -> StateGraph:

    ############################################
    # Initialize Tools and Retrievers
    ############################################
    client = MultiServerMCPClient({
        "dynatrace": {
            "command": "npx",
            "args": ["-y", "@dynatrace-oss/dynatrace-mcp-server@latest"],
            "transport": "stdio",
            "env": {
                **os.environ,  # preserve existing env
                "DT_ENVIRONMENT": DT_ENVIRONMENT,
                "DT_PLATFORM_TOKEN": DT_PLATFORM_TOKEN,
            }
        }
    })

    tools = await client.get_tools()
    retriever = RetrieverFactory.create_dynatrace_rules_retriever(search_kwargs={"k": 3})
    retriever_tool = create_retriever_tool(
        retriever,
        name="dynatrace_documentation",
        description="Search Dynatrace knowledge base to improve and verify Dynatrace queries and rules."
    )

    ############################################
    # Initialize LLMs, Prompts and Agents
    ############################################
    llm = init_chat_model("gpt-5-mini", model_provider="openai")

    supervisor_prompt = PromptsFactory.supervisor(dynatrace_master_rules, dynatrace_query_rules)
    telemetry_supervisor_prompt = PromptsFactory.telemetry_supervisor()
    telemetry_fetcher_prompt = PromptsFactory.telemetry_fetcher(dynatrace_master_rules, dynatrace_query_rules)
    telemetry_analyst_prompt = PromptsFactory.telemetry_analyst()
    problems_supervisor_prompt = PromptsFactory.problems_supervisor()
    problems_fetcher_prompt = PromptsFactory.problems_fetcher(dynatrace_master_rules, dynatrace_problem_rules)
    problems_analyst_prompt = PromptsFactory.problems_analyst()
    security_supervisor_prompt = PromptsFactory.security_supervisor()
    security_fetcher_prompt = PromptsFactory.security_fetcher(dynatrace_master_rules, dynatrace_vuln_rules)
    security_analyst_prompt = PromptsFactory.security_analyst()


    telemetry_fetcher_agent = create_react_agent(llm.with_config({"system_prompt": telemetry_fetcher_prompt}), tools=tools)
    telemetry_analyst_agent = create_react_agent(llm.with_config({"system_prompt": telemetry_analyst_prompt}), tools=tools)
    problems_fetcher_agent = create_react_agent(llm.with_config({"system_prompt": problems_fetcher_prompt}), tools=tools)
    problems_analyst_agent = create_react_agent(llm.with_config({"system_prompt": problems_analyst_prompt}), tools=tools)
    security_fetcher_agent = create_react_agent(llm.with_config({"system_prompt": security_fetcher_prompt}), tools=tools)
    security_analyst_agent = create_react_agent(llm.with_config({"system_prompt": security_analyst_prompt}), tools=tools)

        
    ############################################
    # Supervisor Maker
    ############################################
    async def make_supervisor_node(llm: BaseChatModel, members: list[str], tools: list = None, external_system_prompt: str = None) -> str:
        """Creates a supervisor node that can manage a team of workers."""
        llm = llm.bind_tools(tools) if tools else llm
        options = ["FINISH"] + members

        if not external_system_prompt:
            system_prompt = (
                "You are the supervisor for this domain. "
                f"You manage only these workers: {members}. "
                "Do not attempt to handle other domains — that is the job of the top-level supervisor.\n\n"

                "### Rules\n"
                "- Always start with the Fetcher.\n"
                "- Run the Analyst only after the Fetcher has produced raw results.\n"
                "- If the Fetcher already provided results with next-step options for the user, "
                "stop and wait for the user (do not auto-run the Analyst).\n"
                "- Never interpret or execute queries yourself — only route between workers.\n"
                "- Never loop workers infinitely. Each cycle must move forward or stop.\n\n"

                "### Important\n"
                f"- Respond only with exactly one of: {members} or FINISH.\n"
            )
        else:
            system_prompt = external_system_prompt \
            + f"You are a supervisor tasked with managing a conversation between the following workers: {members}."

        class Router(TypedDict):
            """Worker to route to next. If no workers needed, route to FINISH."""
            next: Literal[*options]

        async def supervisor_node(state: State) -> Command[Literal[*members, "__end__"]]:
            """An LLM-based router."""
            messages = [
                {"role": "system", "content": system_prompt},
            ] + state["messages"]
            response = await llm.with_structured_output(Router).ainvoke(messages)
            goto = response["next"]
            if goto == "FINISH":
                # team finished? write report!
                report = await llm.ainvoke([
                    {"role": "system", "content": "Write a concise, human-readable report summarizing all worker outputs."},
                    *state["messages"]
                ])
                return Command(
                    update={
                        "messages": [AIMessage(content=report.content, name="supervisor")]
                    },
                    goto="__end__"
                )

            return Command(goto=goto, update={"next": goto})
        
        return supervisor_node
    
    ##############################################################
    # Overall-Supervisor
    ##############################################################
    teams_supervisor_node = await make_supervisor_node(llm, ["telemetry_team", "security_team", "problems_team"], tools=[retriever_tool], external_system_prompt=supervisor_prompt)

    ############################################
    # Telemetry Team
    ############################################
    telemetry_supervisor_node = await make_supervisor_node(llm, ["telemetry_fetcher", "telemetry_analyst"], tools=[retriever_tool], external_system_prompt=telemetry_supervisor_prompt)
    ###################################
    # Telemetry Team Supervisor
    ###################################
    async def call_telemetry_team(state: State) -> Command[Literal["supervisor"]]:
        response = await telemetry_graph.ainvoke({"messages": [state["messages"][-1]]})
        print("DEBUG: call_telemetry_team")
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=response["messages"][-1].content,
                        name="telemetry_team",
                    )
                ]
            },
            goto="supervisor",
        )

    ###################################
    # Telemetry Fetcher
    ###################################
    async def telemetry_fetcher_node(state: State) -> Command[Literal["supervisor"]]:
        """Fetch telemetry by using the dynatrace_mcp tool to run DQL/Grail queries for logs, traces, and metrics."""
        print("DEBUG: telemetry_fetcher_node")
        result = await telemetry_fetcher_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="telemetry_fetcher")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    ###################################
    # Telemetry Analyst
    ###################################
    async def telemetry_analyst_node(state: State) -> Command[Literal["supervisor"]]:
        """Analyze telemetry data given by the Telemetry Fetcher and produce insights."""
        print("DEBUG: telemetry_analyst_node")
        result = await telemetry_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="telemetry_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )


    ############################################
    # Problems Team
    ############################################
    problems_supervisor_node = await make_supervisor_node(llm, ["problems_fetcher", "problems_analyst"], tools=[retriever_tool], external_system_prompt=problems_supervisor_prompt)
    ###################################
    # Problems Team Supervisor
    ###################################
    async def call_problems_team(state: State) -> Command[Literal["supervisor"]]:
        response = await problems_graph.ainvoke({"messages": [state["messages"][-1]]})
        print("DEBUG: call_problems_team")
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=response["messages"][-1].content,
                        name="problems_team",
                    )
                ]
            },
            goto="supervisor",
        )

    ###################################
    # Problems Fetcher
    ###################################
    async def problems_fetcher_node(state: State) -> Command[Literal["supervisor"]]:
        """Fetch problems by using the dynatrace_mcp tool to run list_problems."""
        print("DEBUG: problems_fetcher_node")
        result = await problems_fetcher_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="problems_fetcher")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    ###################################
    # Problems Analyst
    ###################################
    async def problems_analyst_node(state: State) -> Command[Literal["supervisor"]]:
        # problems_mitigator – derive prioritized actions/runbook steps
        """Analyze problems data given by the Problems Fetcher and produce insights."""
        print("DEBUG: problems_analyst_node")
        result = await problems_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="problems_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )


    ############################################
    # Security Team
    ############################################
    security_supervisor_node = await make_supervisor_node(llm, ["security_fetcher", "security_analyst"], tools=[retriever_tool], external_system_prompt=security_supervisor_prompt)

    ###################################
    # Security Team Supervisor
    ###################################
    async def call_security_team(state: State) -> Command[Literal["supervisor"]]:
        response = await security_graph.ainvoke({"messages": [state["messages"][-1]]})
        print("DEBUG: call_security_team")
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=response["messages"][-1].content,
                        name="security_team",
                    )
                ]
            },
            goto="supervisor",
        )

    ###################################
    # Security Fetcher
    ###################################
    async def security_fetcher_node(state: State) -> Command[Literal["supervisor"]]:
        """Fetch security issues by using the dynatrace_mcp tool to run list_vulnerabilities."""
        print("DEBUG: security_fetcher_node")
        result = await security_fetcher_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="security_fetcher")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    ###################################
    # Security Analyst
    ###################################
    async def security_analyst_node(state: State) -> Command[Literal["supervisor"]]:
        """Analyze vulnerability data given by the Security Fetcher and produce insights."""
        print("DEBUG: security_analyst_node")
        result = await security_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="security_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )


    #############################################
    # Build Telemetry Subgraph
    #############################################
    telemetry_builder = StateGraph(State)
    telemetry_builder.add_node("supervisor", telemetry_supervisor_node)
    telemetry_builder.add_node("telemetry_fetcher", telemetry_fetcher_node)
    telemetry_builder.add_node("telemetry_analyst", telemetry_analyst_node)
    telemetry_builder.add_edge(START, "supervisor")
    telemetry_graph = telemetry_builder.compile()

    #############################################
    # Build Problems Subgraph
    #############################################
    problems_builder = StateGraph(State)
    problems_builder.add_node("supervisor", problems_supervisor_node)
    problems_builder.add_node("problems_fetcher", problems_fetcher_node)
    problems_builder.add_node("problems_analyst", problems_analyst_node)
    problems_builder.add_edge(START, "supervisor")
    problems_graph = problems_builder.compile()

    #############################################
    # Build Security Subgraph
    #############################################
    security_builder = StateGraph(State)
    security_builder.add_node("supervisor", security_supervisor_node)
    security_builder.add_node("security_fetcher", security_fetcher_node)
    security_builder.add_node("security_analyst", security_analyst_node)
    security_builder.add_edge(START, "supervisor")
    security_graph = security_builder.compile()
    
    ############################################
    # Build Main Graph
    ############################################
    super_builder = StateGraph(State)
    super_builder.add_node("supervisor", teams_supervisor_node)
    super_builder.add_node("telemetry_team", call_telemetry_team)
    super_builder.add_node("problems_team", call_problems_team)
    super_builder.add_node("security_team", call_security_team)
    super_builder.add_edge(START, "supervisor")

    memory = MemorySaver()
    return super_builder.compile(checkpointer=memory)


async def run_cli(graph: StateGraph) -> None:
    async def stream_graph_updates(user_input: str):
        async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]},
                                         config={"recursion_limit": 50, "thread_id": "cli-session"}):
            
            print("-----")
            for node, value in event.items():
                print(f"Node: {node}")
                # print messages only if they exist
                if isinstance(value, dict) and "messages" in value:
                    for msg in value["messages"]:
                        msg.pretty_print()

    while True:
        try:
            user_input = input("User: ")
            if user_input.lower() in ["quit", "exit", "q"] or \
                user_input.strip() == "":
                print("Goodbye!")
                break

            await stream_graph_updates(user_input)
        except Exception as e:
            print(f"Error: {e}")
            break

if __name__ == "__main__":
    graph = asyncio.run(build_graph())
    asyncio.run(run_cli(graph))