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
from langgraph.graph import StateGraph, START, END, MessagesState

from src.tools.retrievers import RetrieverFactory
from src.tools.mcp_servers import MCPClientFactory
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

class MultiAgentGraphFactory():
    """Factory to create the multi-agent graph with telemetry, problems, and security teams."""

    def __init__(self, llm: BaseChatModel):
        """Initialize the graph factory with the given LLM."""
        self.llm = llm
        self.init_prompts()
        self.init_tools()
        self.init_agents()
        self.init_supervisor_nodes()

    def init_prompts(self) -> None:
        """Initialize all prompts used in the graph."""
        self.supervisor_prompt = PromptsFactory.supervisor(dynatrace_master_rules, dynatrace_query_rules)
        self.telemetry_supervisor_prompt = PromptsFactory.telemetry_supervisor()
        self.telemetry_fetcher_prompt = PromptsFactory.telemetry_fetcher(dynatrace_master_rules, dynatrace_query_rules)
        self.telemetry_analyst_prompt = PromptsFactory.telemetry_analyst()
        self.problems_supervisor_prompt = PromptsFactory.problems_supervisor()
        self.problems_fetcher_prompt = PromptsFactory.problems_fetcher(dynatrace_master_rules, dynatrace_problem_rules)
        self.problems_analyst_prompt = PromptsFactory.problems_analyst()
        self.security_supervisor_prompt = PromptsFactory.security_supervisor()
        self.security_fetcher_prompt = PromptsFactory.security_fetcher(dynatrace_master_rules, dynatrace_vuln_rules)
        self.security_analyst_prompt = PromptsFactory.security_analyst()

    def init_tools(self) -> None:
        """Initialize all tools used in the graph."""
        self.retriever_tool = RetrieverFactory().create_dynatrace_rules_retriever(search_kwargs={"k": 3})
        self.mcp_tools = asyncio.run(MCPClientFactory.create_dynatrace_mcp_client())
        self.tools = self.mcp_tools + [self.retriever_tool]

    def init_agents(self) -> None:
        """Initialize all agents used in the graph."""
        self.telemetry_fetcher_agent = create_react_agent(self.llm.with_config({"system_prompt": self.telemetry_fetcher_prompt}), tools=self.tools)
        self.telemetry_analyst_agent = create_react_agent(self.llm.with_config({"system_prompt": self.telemetry_analyst_prompt}), tools=self.tools)
        self.problems_fetcher_agent = create_react_agent(self.llm.with_config({"system_prompt": self.problems_fetcher_prompt}), tools=self.tools)
        self.problems_analyst_agent = create_react_agent(self.llm.with_config({"system_prompt": self.problems_analyst_prompt}), tools=self.tools)
        self.security_fetcher_agent = create_react_agent(self.llm.with_config({"system_prompt": self.security_fetcher_prompt}), tools=self.tools)
        self.security_analyst_agent = create_react_agent(self.llm.with_config({"system_prompt": self.security_analyst_prompt}), tools=self.tools)

    def init_supervisor_nodes(self) -> None:
        """Initialize all supervisor nodes used in the graph."""
        self.teams_supervisor_node = self.make_supervisor_node(self.llm, ["telemetry_team", "security_team", "problems_team"], tools=[self.retriever_tool], external_system_prompt=self.supervisor_prompt)
        self.telemetry_supervisor_node = self.make_supervisor_node(self.llm, ["telemetry_fetcher", "telemetry_analyst"], tools=[self.retriever_tool], external_system_prompt=self.telemetry_supervisor_prompt)
        self.problems_supervisor_node = self.make_supervisor_node(self.llm, ["problems_fetcher", "problems_analyst"], tools=[self.retriever_tool], external_system_prompt=self.problems_supervisor_prompt)
        self.security_supervisor_node = self.make_supervisor_node(self.llm, ["security_fetcher", "security_analyst"], tools=[self.retriever_tool], external_system_prompt=self.security_supervisor_prompt)

    ############################################
    # Supervisor Maker
    ############################################
    def make_supervisor_node(self, llm: BaseChatModel, members: list[str], tools: list = None, external_system_prompt: str = None) -> str:
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
    
    ############################################
    # Telemetry Team
    ############################################
    async def call_telemetry_team(self, state: State) -> Command[Literal["supervisor"]]:
        response = await self.telemetry_graph.ainvoke({"messages": [state["messages"][-1]]})
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
    async def telemetry_fetcher_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Fetch telemetry by using the dynatrace_mcp tool to run DQL/Grail queries for logs, traces, and metrics."""
        print("DEBUG: telemetry_fetcher_node")
        result = await self.telemetry_fetcher_agent.ainvoke(state)
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
    async def telemetry_analyst_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Analyze telemetry data given by the Telemetry Fetcher and produce insights."""
        print("DEBUG: telemetry_analyst_node")
        result = await self.telemetry_analyst_agent.ainvoke(state)
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
    async def call_problems_team(self, state: State) -> Command[Literal["supervisor"]]:
        response = await self.problems_graph.ainvoke({"messages": [state["messages"][-1]]})
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
    async def problems_fetcher_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Fetch problems by using the dynatrace_mcp tool to run list_problems."""
        print("DEBUG: problems_fetcher_node")
        result = await self.problems_fetcher_agent.ainvoke(state)
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
    async def problems_analyst_node(self, state: State) -> Command[Literal["supervisor"]]:
        # problems_mitigator – derive prioritized actions/runbook steps
        """Analyze problems data given by the Problems Fetcher and produce insights."""
        print("DEBUG: problems_analyst_node")
        result = await self.problems_analyst_agent.ainvoke(state)
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
    async def call_security_team(self, state: State) -> Command[Literal["supervisor"]]:
        response = await self.security_graph.ainvoke({"messages": [state["messages"][-1]]})
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
    async def security_fetcher_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Fetch security issues by using the dynatrace_mcp tool to run list_vulnerabilities."""
        print("DEBUG: security_fetcher_node")
        result = await self.security_fetcher_agent.ainvoke(state)
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
    async def security_analyst_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Analyze vulnerability data given by the Security Fetcher and produce insights."""
        print("DEBUG: security_analyst_node")
        result = await self.security_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="security_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    async def build_graph(self) -> StateGraph:
        #############################################
        # Build Telemetry Subgraph
        #############################################
        self.telemetry_builder = StateGraph(State)
        self.telemetry_builder.add_node("supervisor", self.telemetry_supervisor_node)
        self.telemetry_builder.add_node("telemetry_fetcher", self.telemetry_fetcher_node)
        self.telemetry_builder.add_node("telemetry_analyst", self.telemetry_analyst_node)
        self.telemetry_builder.add_edge(START, "supervisor")
        self.telemetry_graph = self.telemetry_builder.compile()

        #############################################
        # Build Problems Subgraph
        #############################################
        self.problems_builder = StateGraph(State)
        self.problems_builder.add_node("supervisor", self.problems_supervisor_node)
        self.problems_builder.add_node("problems_fetcher", self.problems_fetcher_node)
        self.problems_builder.add_node("problems_analyst", self.problems_analyst_node)
        self.problems_builder.add_edge(START, "supervisor")
        self.problems_graph = self.problems_builder.compile()

        #############################################
        # Build Security Subgraph
        #############################################
        self.security_builder = StateGraph(State)
        self.security_builder.add_node("supervisor", self.security_supervisor_node)
        self.security_builder.add_node("security_fetcher", self.security_fetcher_node)
        self.security_builder.add_node("security_analyst", self.security_analyst_node)
        self.security_builder.add_edge(START, "supervisor")
        self.security_graph = self.security_builder.compile()
        
        ############################################
        # Build Main Graph
        ############################################
        self.super_builder = StateGraph(State)
        self.super_builder.add_node("supervisor", self.teams_supervisor_node)
        self.super_builder.add_node("telemetry_team", self.call_telemetry_team)
        self.super_builder.add_node("problems_team", self.call_problems_team)
        self.super_builder.add_node("security_team", self.call_security_team)
        self.super_builder.add_edge(START, "supervisor")

        memory = MemorySaver()
        return self.super_builder.compile(checkpointer=memory)


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
    llm = init_chat_model("gpt-5-mini", model_provider="openai")
    factory = MultiAgentGraphFactory(llm)
    graph = asyncio.run(factory.build_graph())
    asyncio.run(run_cli(graph))