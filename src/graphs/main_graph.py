from pathlib import Path
from dotenv import load_dotenv
from traceloop.sdk import Traceloop

from typing import Literal
from typing_extensions import TypedDict, NotRequired

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent
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
dynatrace_incident_response = (dynatrace_rules_dir / "workflows" / "DynatraceIncidentResponse.md").read_text(encoding="utf-8")
dynatrace_investigation_checklist = (dynatrace_rules_dir / "workflows" / "DynatraceInvestigationChecklist.md").read_text(encoding="utf-8")
dynatrace_security_compliance = (dynatrace_rules_dir / "workflows" / "DynatraceSecurityCompliance.md").read_text(encoding="utf-8")

#####################################
# Load environment variables
#####################################
load_dotenv(dotenv_path=env_path, override=True)

#####################################
# Initialize Traceloop for Observability
#####################################
Traceloop.init(app_name="multi-agent-showcase")

# TODO: Guardrails einbauen
# TODO: fetcher soll die dynatrace_documentation maximal 2 mal aufrufen

class State(MessagesState):
    next: str

class MultiAgentGraphFactory():
    """Factory to create the multi-agent graph with telemetry, problems, and security teams."""

    def __init__(self, llm: BaseChatModel, memory_saver: MemorySaver = MemorySaver()) -> None:
        """Initialize the graph factory with the given LLM."""
        self.llm = llm
        self.memory_saver = memory_saver
        self.mcp_tools = None
        self.retriever_tool = None
        self.tools = None

    async def init_tools_and_agents(self) -> None:
        """Initialize all tools used in the graph."""
        self.retriever_tool = RetrieverFactory().create_dynatrace_rules_retriever(search_kwargs={"k": 4})
        self.mcp_tools = await MCPClientFactory.create_dynatrace_mcp_client()
        self.tools = self.mcp_tools + [self.retriever_tool]
        self._init_agents()

    def _init_agents(self) -> None:
        """Initialize all agents used in the graph."""
        self.telemetry_fetcher_agent = create_react_agent(self.llm, tools=self.tools, prompt=PromptsFactory.telemetry_fetcher())
        self.telemetry_analyst_agent = create_react_agent(self.llm, tools=[self.retriever_tool], prompt=PromptsFactory.telemetry_analyst())
        self.problems_fetcher_agent = create_react_agent(self.llm, tools=self.tools, prompt=PromptsFactory.problems_fetcher())
        self.problems_analyst_agent = create_react_agent(self.llm, tools=[self.retriever_tool], prompt=PromptsFactory.problems_analyst())
        self.security_fetcher_agent = create_react_agent(self.llm, tools=self.tools, prompt=PromptsFactory.security_fetcher())
        self.security_analyst_agent = create_react_agent(self.llm, tools=[self.retriever_tool], prompt=PromptsFactory.security_analyst())
        self.devops_fetcher_agent = create_react_agent(self.llm, tools=self.tools, prompt=PromptsFactory.devops_fetcher())
        self.devops_analyst_agent = create_react_agent(self.llm, tools=[self.retriever_tool], prompt=PromptsFactory.devops_analyst())
        
    def init_supervisor_nodes(self) -> None:
        """Initialize all supervisor nodes used in the graph."""
        self.teams_supervisor_node = self.make_supervisor_node(self.llm, ["telemetry_team", "security_team", "problems_team", "devops_team"],
                                                                tools=[self.retriever_tool],
                                                                external_system_prompt=PromptsFactory.supervisor(),
                                                                emit_finish_message=True,
                                                                name="teams_supervisor")
        self.telemetry_supervisor_node = self.make_supervisor_node(self.llm, ["telemetry_fetcher", "telemetry_analyst"],
                                                                    tools=[self.retriever_tool],
                                                                    external_system_prompt=PromptsFactory.telemetry_supervisor(),
                                                                    emit_finish_message=False,
                                                                    name="telemetry_supervisor")
        self.problems_supervisor_node = self.make_supervisor_node(self.llm, ["problems_fetcher", "problems_analyst"],
                                                                    tools=[self.retriever_tool],
                                                                    external_system_prompt=PromptsFactory.problems_supervisor(),
                                                                    emit_finish_message=False,
                                                                    name="problems_supervisor")
        self.security_supervisor_node = self.make_supervisor_node(self.llm, ["security_fetcher", "security_analyst"],
                                                                    tools=[self.retriever_tool],
                                                                    external_system_prompt=PromptsFactory.security_supervisor(),
                                                                    emit_finish_message=False,
                                                                    name="security_supervisor")
        self.devops_supervisor_node = self.make_supervisor_node(self.llm, ["devops_fetcher", "devops_analyst"],
                                                                    tools=[self.retriever_tool],
                                                                    external_system_prompt=PromptsFactory.devops_supervisor(),
                                                                    emit_finish_message=False,
                                                                    name="devops_supervisor")

    ############################################
    # Supervisor Maker
    ############################################
    def make_supervisor_node(
        self,
        llm: BaseChatModel,
        members: list[str],
        tools: list = None,
        external_system_prompt: str = None,
        emit_finish_message: bool = False, 
        name: str = ""
    ) -> str:
        llm = llm.bind_tools(tools) if tools else llm
        options = ["FINISH"] + members

        base_prompt = (
            "You are a supervisor managing: {members}. "
            "Given the user request, respond with the worker to act next."
        ).format(members=members)

        master_supervisor = (
            "If no worker is needed, choose FINISH and provide a short useful final_message. "
            "If the request is vague or about capabilities, FINISH with a one-sentence capability summary "
            "or one clarifying question (entity, timeframe). Never FINISH without final_message."
            "If the user asks for a summary, provide it with a useful final_message and choose FINISH"
        ) if emit_finish_message else ""

        system_prompt = base_prompt + (external_system_prompt or "") + master_supervisor
    
        class Router(TypedDict):
            next: Literal[*options]
            if emit_finish_message:
                final_message: str

        async def supervisor_node(state: State) -> Command[Literal[*members, "__end__"]]:
            print(f"DEBUG: {name}")
            print("DEBUG:          |")
            messages = [{"role": "system", "content": system_prompt}] + state["messages"]
            response = await llm.with_structured_output(Router).ainvoke(messages)
            nxt = response["next"]
            if nxt == "FINISH":
                if emit_finish_message:
                    print("DEBUG:        User")
                    print("DEBUG:          |")
                    msg = response.get("final_message")
                    return Command(
                        goto=END,
                        update={
                            "messages": [AIMessage(content=msg, name="supervisor")],
                            "next": "FINISH",
                        },
                    )
                return Command(goto=END, update={"next": "FINISH"})
            return Command(goto=nxt, update={"next": nxt})

        return supervisor_node
    
    ############################################
    # Telemetry Team
    ############################################
    async def call_telemetry_team(self, state: State) -> Command[Literal["supervisor"]]:
        response = await self.telemetry_graph.ainvoke({"messages": [state["messages"][-1]]})
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
        print("DEBUG: telemetry_fetcher")
        print("DEBUG:          |")
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
        print("DEBUG: telemetry_analyst")
        print("DEBUG:          |")
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
        print("DEBUG: problems_fetcher")
        print("DEBUG:          |")
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
        """Analyze problems data given by the Problems Fetcher and produce insights."""
        print("DEBUG: problems_analyst")
        print("DEBUG:          |")
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
        print("DEBUG: security_fetcher")
        print("DEBUG:          |")
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
        result = await self.security_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="security_analyst")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )
    
    ############################################
    # DevOps Team
    ############################################
    async def call_devops_team(self, state: State) -> Command[Literal["supervisor"]]:
        response = await self.devops_graph.ainvoke({"messages": [state["messages"][-1]]})
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=response["messages"][-1].content,
                        name="devops_team",
                    )
                ]
            },
            goto="supervisor",
        )

    ###################################
    # DevOps Fetcher
    ###################################
    async def devops_fetcher_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Fetch DevOps/SRE data such as deployments, SLO/SLI, error budgets, and pipeline health using the dynatrace_mcp tool."""
        print("DEBUG: devops_fetcher")
        print("DEBUG:          |")
        result = await self.devops_fetcher_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="devops_fetcher")]
            },
            # Always report back to supervisor when done
            goto="supervisor"
        )

    ###################################
    # DevOps Analyst
    ###################################
    async def devops_analyst_node(self, state: State) -> Command[Literal["supervisor"]]:
        """Analyze DevOps/SRE data from the Fetcher and produce insights such as health gate status, error budget consumption, and mitigation recommendations."""
        print("DEBUG: devops_analyst")
        print("DEBUG:          |")
        result = await self.devops_analyst_agent.ainvoke(state)
        return Command(
            update={
                "messages": [AIMessage(content=result["messages"][-1].content, name="devops_analyst")]
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

        #############################################
        # Build DevOps Subgraph
        #############################################
        self.devops_builder = StateGraph(State)
        self.devops_builder.add_node("supervisor", self.devops_supervisor_node)
        self.devops_builder.add_node("devops_fetcher", self.devops_fetcher_node)
        self.devops_builder.add_node("devops_analyst", self.devops_analyst_node)
        self.devops_builder.add_edge(START, "supervisor")
        self.devops_graph = self.devops_builder.compile()
        
        ############################################
        # Build Main Graph
        ############################################
        self.super_builder = StateGraph(State)
        self.super_builder.add_node("supervisor", self.teams_supervisor_node)
        self.super_builder.add_node("telemetry_team", self.call_telemetry_team)
        self.super_builder.add_node("problems_team", self.call_problems_team)
        self.super_builder.add_node("security_team", self.call_security_team)
        self.super_builder.add_node("devops_team", self.call_devops_team)
        self.super_builder.add_edge(START, "supervisor")

        return self.super_builder.compile(checkpointer=self.memory_saver)