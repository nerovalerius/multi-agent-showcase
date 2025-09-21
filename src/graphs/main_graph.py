import os
# import sys; sys.path.append("..")
import asyncio
from pathlib import Path
from dotenv import load_dotenv

from typing import Literal
from typing_extensions import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.output_parsers import StrOutputParser
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langchain.chat_models import init_chat_model
from langchain.tools.retriever import create_retriever_tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.graph.message import add_messages

from src.tools.retrievers import RetrieverFactory
from src.prompts.prompts import PromptsFactory

root_dir = Path(__file__).resolve().parents[2]
env_path = root_dir / ".env"
dynatrace_rules_dir = Path(__file__).resolve().parents[2] / "dynatrace_rules"
dynatrace_master_rules = (dynatrace_rules_dir / "DynatraceMcpIntegration.md").read_text(encoding="utf-8")
dynatrace_query_rules = (dynatrace_rules_dir / "reference" / "DynatraceQueryLanguage.md").read_text(encoding="utf-8")

load_dotenv(dotenv_path=env_path, override=True)
DT_ENVIRONMENT = os.getenv("DT_ENVIRONMENT")
DT_PLATFORM_TOKEN = os.getenv("DT_PLATFORM_TOKEN")


# TODO: apply dynatrace rules.md file somehow to agent
    # TODO: OpenLLMetry oder OpenTelemetry einbauen und in Dynatrace, Traceloop o.a einbauen zur Observability
    # TODO: Guardrails einbauen


class State(MessagesState):
    next: str

async def build_graph() -> StateGraph:

    ############################################
    # Initialize MCP Servers
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
    supervisor_prompt = PromptsFactory.supervisor(dynatrace_master_rules, dynatrace_query_rules)
    telemetry_fetcher_prompt = PromptsFactory.telemetry_fetcher(dynatrace_master_rules, dynatrace_query_rules)
    telemetry_analyst_prompt = PromptsFactory.telemetry_analyst()

    llm = init_chat_model("gpt-5-mini", model_provider="openai")

    telemetry_fetcher_agent = create_react_agent(llm.with_config({"system_prompt": telemetry_fetcher_prompt}), tools=tools)
    telemetry_analyst_agent = create_react_agent(llm.with_config({"system_prompt": telemetry_analyst_prompt}), tools=tools)
        

    async def make_supervisor_node(llm: BaseChatModel, members: list[str], tools: list = None, external_system_prompt: str = None) -> str:
        print("DEBUG: make_supervisor_node", members)

        llm = llm.bind_tools(tools) if tools else llm
        options = ["FINISH"] + members

        if not external_system_prompt:
            system_prompt = (
                "You are a supervisor tasked with managing a conversation between the"
                f" following workers: {members}. Given the following user request,"
                " respond with the worker to act next. Each worker will perform a"
                " task and respond with their results and status. When finished,"
                " respond with FINISH."
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
    
    ############################################
    # Overall-Supervisor
    ############################################
    teams_supervisor_node = await make_supervisor_node(llm, ["telemetry_team"], tools=[retriever_tool], external_system_prompt=supervisor_prompt)

    ############################################
    # Telemetry Team
    ############################################
    telemetry_supervisor_node = await make_supervisor_node(llm, ["telemetry_fetcher", "telemetry_analyst"], tools=[retriever_tool])
    ############################################
    # Telemetry Team Supervisor
    ############################################
    async def call_telemetry_team(state: State) -> Command[Literal["supervisor"]]:
        response = await telemetry_graph.ainvoke({"messages": [state["messages"][-1]]})
        print("call_telemetry_team")
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

    ############################################
    # Telemetry Fetcher
    ############################################
    async def telemetry_fetcher_node(state: State) -> Command[Literal["supervisor"]]:
        print("DEBUG: telemetry_fetcher_node")
        result = await telemetry_fetcher_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="telemetry_fetcher")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    ############################################
    # Telemetry Analyst
    ############################################
    async def telemetry_analyst_node(state: State) -> Command[Literal["supervisor"]]:
        print("DEBUG: telemetry_analyst_node")
        result = await telemetry_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="telemetry_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    # TODO: telemetry_fetcher (uses dynatrace_mcp) – DQL/Grail for logs/traces/metrics (aggregated)
    # TODO: telemetry_analyst – detect patterns/outliers and produce clear insights for the app owner

    # TODO: problems_team
    # TODO: problems_fetcher (uses dynatrace_mcp) – fetch open & recent problems (+impact)
    # TODO: problems_mitigator – derive prioritized actions/runbook steps
    #
    # TODO: security_team
    # TODO: vulns_fetcher (uses dynatrace_mcp) – fetch vulnerabilities/security problems
    # TODO: vulns_triager – risk ranking & fix plan
    #
    # TODO: reporting_team
    # TODO: report_writer – Onboarding Snapshot (Data Inventory, Health, Risks, Mitigation Plan)

    #############################################
    # Build Telemetry Subgraph
    #############################################
    telemetry_builder = StateGraph(State)
    telemetry_builder.add_node("supervisor", telemetry_supervisor_node)
    telemetry_builder.add_node("telemetry_fetcher", telemetry_fetcher_node)
    telemetry_builder.add_node("telemetry_analyst", telemetry_analyst_node)
    telemetry_builder.add_edge(START, "supervisor")
    telemetry_graph = telemetry_builder.compile()

    ############################################
    # Build Main Graph
    ############################################
    super_builder = StateGraph(State)
    super_builder.add_node("supervisor", teams_supervisor_node)
    super_builder.add_node("telemetry_team", call_telemetry_team)
    super_builder.add_edge(START, "supervisor")

    memory = MemorySaver()
    return super_builder.compile(checkpointer=memory)


async def run_cli(graph: StateGraph) -> None:
    async def stream_graph_updates(user_input: str):
        async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]},
                                         config={"recursion_limit": 150, "thread_id": "cli-session"}):
            
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
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break

            await stream_graph_updates(user_input)
        except Exception as e:
            print(f"Error: {e}")
            break

if __name__ == "__main__":
    graph = asyncio.run(build_graph())
    asyncio.run(run_cli(graph))