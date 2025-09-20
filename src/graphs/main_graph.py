import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

from typing import Annotated
from typing_extensions import TypedDict


from langchain_openai import OpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.schema import HumanMessage, AIMessage, SystemMessage
from langgraph.prebuilt import ToolNode
from langchain.chat_models import init_chat_model
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_community.vectorstores import FAISS

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent


env_path = Path(__file__).resolve().parents[2] / ".env"
dynatrace_rules_dir = Path(__file__).resolve().parents[2] / "dynatrace_rules"
dynatrace_master_rules = dynatrace_rules_dir / "DynatraceMcpIntegration.md"

load_dotenv(dotenv_path=env_path, override=True)
DT_ENVIRONMENT = os.getenv("DT_ENVIRONMENT")
DT_PLATFORM_TOKEN = os.getenv("DT_PLATFORM_TOKEN")

# TODO: apply dynatrace rules.md file somehow to agent
# TODO: OpenLLMetry oder OpenTelemetry einbauen und in Dynatrace, Traceloop o.a einbauen zur Observability
# TODO: Guardrails einbauen

docs = []
for filename in dynatrace_rules_dir.rglob("*.md"):
    with open(filename, "r", encoding="utf-8") as f:
        docs.append({"content": f.read(), "source": str(filename.relative_to(dynatrace_rules_dir))})

# in Chunks splitten
splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=100)
chunks = []
for doc in docs:
    for chunk in splitter.split_text(doc["content"]):
        chunks.append({
            "page_content": chunk,
            "metadata": {"source": doc["source"]}
        })

# Embeddings bauen
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# VectorStore erstellen
vectorstore = FAISS.from_texts(
    [c["page_content"] for c in chunks],
    embedding=embeddings,
    metadatas=[c["metadata"] for c in chunks],
)

# speichern für Wiederverwendung
vectorstore.save_local("dynatrace_rules_index")

retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

class State(TypedDict):
    # Messages have the type "list". The "add_messages" function in the annotation defines how this state key
    # should be updated (in this case, it appends messages to the list, rather than overwriting them)
    messages: Annotated[list, add_messages]


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

    def should_continue(state: State):
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools"
        return END

    tools = await client.get_tools()
    tool_node = ToolNode(tools)

    llm = init_chat_model("gpt-4o-mini")
    llm_with_tools =  llm.bind_tools(tools)

    refine_prompt = ChatPromptTemplate.from_messages([
    ("system",
     "You are a prompt refiner for Dynatrace Observability.\n"
     "You take a raw user query and rewrite it into a clear, precise instruction for the Dynatrace MCP agent.\n\n"
     "Requirements:\n"
     "- Use the Dynatrace Master Rule as the source of truth.\n"
     "- Do NOT generate DQL directly. Instead, clarify intent in natural language for the agent.\n"
     "- Always specify the data source (logs, spans, events, metrics).\n"
     "- Add a reasonable timeframe (default: from:now()-24h) if not specified.\n"
     "- Include entity IDs or names if provided.\n"
     "- Emphasize that queries must be verified with `verify_dql` before `execute_dql`.\n"
     "- Never mention `for` after `fetch` (this is a forbidden pattern).\n"
     "- Return only the refined prompt, no explanations.\n\n"
     "Dynatrace Master Rule:\n{master_rule}\n"),
    ("user", "{user_input}")
    ])

    refiner = refine_prompt | llm | StrOutputParser()

    async def chatbot(state: State):
        last_msg = state["messages"][-1]

        if isinstance(last_msg, HumanMessage):
            # 1. Rule-Snippets aus dem VectorStore holen
            retrieved_docs = await retriever.ainvoke(last_msg.content)
            rules_context = "\n\n".join([d.page_content for d in retrieved_docs])

            refined_user_msg = await refiner.ainvoke({
                "user_input": last_msg.content,
                "master_rule": dynatrace_master_rules
            })

            system_prompt = SystemMessage(
                content=(
                    "You are a Dynatrace observability assistant.\n"
                    "You have access to MCP tools: verify_dql, execute_dql, generate_dql_from_natural_language, "
                    "list_problems, list_vulnerabilities, get_entity_details, get_environment_info, send_slack_message, etc.\n\n"

                    "### Core Rules\n"
                    "- Always verify queries with verify_dql BEFORE executing them.\n"
                    "- Never use 'for' after fetch. Correct patterns are:\n"
                    "    fetch logs | filter entity.id == \"<ENTITY_ID>\" | limit 10\n"
                    "    fetch spans | filter service.name == \"<NAME>\" | limit 10\n"
                    "    fetch metric.series | filter startsWith(metric.key, \"dt.service\") | limit 10\n"
                    "- If verify_dql shows a syntax error → fix automatically.\n"
                    "- If execute_dql returns an empty response:\n"
                    "    1. Retry with larger timeframes (24h → 7d).\n"
                    "    2. Relax filters (e.g. remove specific pod names, broaden entity scope).\n"
                    "    3. Switch data source (logs → spans → events) if relevant.\n"
                    "- Always include span.events when investigating failed services.\n\n"

                    "### Error Handling Strategy\n"
                    "Never just return an empty result.\n"
                    "If no results are found:\n"
                    " - Suggest and try alternative DQL automatically.\n"
                    " - Explain to the user what adjustments you made.\n"
                    " - Only stop if absolutely no useful data is available.\n\n"

                    "### Reference Knowledge\n"
                    f"{dynatrace_master_rules}\n\n"
                    f"{rules_context}\n\n"

                    "The user query follows below."
                )
            )  

            full_state = {"messages": [system_prompt, HumanMessage(content=refined_user_msg)]}
            print(f"DEBUG: refined_user_prompt: {refined_user_msg}")
        else:
            # Don’t refine non-user messages
            full_state = {"messages": state["messages"]}

        response = await llm_with_tools.ainvoke(full_state["messages"])
        return {"messages": [response]}

    ############################################
    # Build Graph
    ############################################
    graph_builder = StateGraph(State)
    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", tool_node)
    graph_builder.add_edge("tools", "chatbot")
    graph_builder.add_edge(START, "chatbot")
    graph_builder.add_conditional_edges(
    "chatbot", 
    should_continue
)
    graph_builder.add_edge("chatbot", END)
    return graph_builder.compile()

async def run_cli(graph: StateGraph) -> None:
    async def stream_graph_updates(user_input: str):
        async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]}):
            for value in event.values():
                print(f"Assistant: {value['messages'][-1].content}")

    while True:
        try:
            user_input = input("User:")
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break

            await stream_graph_updates(user_input)
        except:
            # # fallback if input() is not available
            # user_input = "What do you know about LangGraph?"
            # print (f"User:  {user_input}")
            # await stream_graph_updates(user_input)
            break

if __name__ == "__main__":
    graph = asyncio.run(build_graph())
    asyncio.run(run_cli(graph))