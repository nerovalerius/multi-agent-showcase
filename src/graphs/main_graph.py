import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

from typing import Annotated
from typing_extensions import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import OpenAIEmbeddings
from langchain_core.messages import ToolMessage, AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.prebuilt import ToolNode
from langchain.chat_models import init_chat_model
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_community.vectorstores import FAISS

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent


env_path = Path(__file__).resolve().parents[2] / ".env"
dynatrace_rules_dir = Path(__file__).resolve().parents[2] / "dynatrace_rules"
dynatrace_master_rules = (dynatrace_rules_dir / "DynatraceMcpIntegration.md").read_text(encoding="utf-8")
dynatrace_query_rules = (dynatrace_rules_dir / "reference" / "DynatraceQueryLanguage.md").read_text(encoding="utf-8")

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

        if isinstance(last_message, AIMessage) and getattr(last_message, "tool_calls", None):
            return "tools"
        return END

    tools = await client.get_tools()
    tool_node = ToolNode(tools)

    # print(tool_node)

    llm = init_chat_model("gpt-5-mini", model_provider="openai")
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
        "- If the user request is about summarization, reformulation, explanation, or formatting (e.g. 'convert to JSON'), "
        "then do NOT insert tool-usage instructions. Just refine the request as a plain natural-language instruction.\n\n"
        "### Reference Knowledge\n"
        "{dt_master_rules}\n\n"
        "{dt_query_rules}\n\n"),
        ("user", "{user_input}")
    ])

    refiner = refine_prompt | llm | StrOutputParser()

    async def chatbot(state: State):
        
        system_message = f"""
        You are a Dynatrace observability assistant.
        You have access to MCP tools: verify_dql, execute_dql, generate_dql_from_natural_language, 
        list_problems, list_vulnerabilities, find_entity_by_name, get_entity_details, get_environment_info, 
        send_slack_message, etc.

        ### Core Rules for Dynatrace Assistant
        - Only use a tool if it is strictly required to retrieve or verify data from Dynatrace.
        - If the user asks for reformulation, summarization, explanation, or formatting 
        (e.g. 'make JSON from the last result'), respond directly without invoking any tool.
        - If the user provides an entity **by name**, first resolve it with `find_entity_by_name`.
        - After finding the entityId, call `get_entity_details` to confirm, and only then use the `entityId` in queries.
        - When generating queries:
        1. If you need a DQL statement, use `generate_dql_from_natural_language` with the refined user request.
        2. Always call `verify_dql` before calling `execute_dql`.
        3. Never skip the verify step.
        - Forbidden pattern: never use `for` after fetch. Correct examples:
            fetch logs | filter entity.id == "<ENTITY_ID>" | limit 10
            fetch spans | filter service.name == "<NAME>" | limit 10
            fetch metric.series | filter startsWith(metric.key, "dt.service") | limit 10

        ### Error Handling Strategy
        - If `verify_dql` shows a syntax error → fix automatically.
        - If `execute_dql` returns an empty response:
        1. Retry with a longer timeframe (24h → 7d → 30d).
        2. Try alternative sources (logs → spans → events).
        3. If an entityId is known, make sure you filter by entityId, not just by name.
        4. Relax filters if too restrictive (e.g. remove pod-specific filters).
        - Never just return an empty result.
        - Always explain adjustments you made when retrying.
        - Only stop if absolutely no useful data is available.

        ### Additional Notes
        - Always include `span.events` when investigating failed services.
        - Be proactive: if a query is too narrow or timeframe too short, automatically broaden it.

        ### Reference Knowledge
        {dynatrace_master_rules}

        {dynatrace_query_rules}

        The user query follows next.
        """

        # Add System prompt only once
        system_prompt_added = False
        messages = state["messages"]
        for message in messages:
            if isinstance(message, SystemMessage):
                message.content = system_message
                system_prompt_added = True

        if not system_prompt_added:
            messages = [SystemMessage(content=system_message)] + messages

        # Refine newest message if it is a user prompt
        last_msg = messages[-1]
        if isinstance(last_msg, HumanMessage):
            retrieved_docs = await retriever.ainvoke(last_msg.content)
            rules_context = "\n\n".join([d.page_content for d in retrieved_docs])

            refined = await refiner.ainvoke({
                "user_input": last_msg.content,
                "dt_master_rules": dynatrace_master_rules,
                "dt_query_rules" : dynatrace_query_rules
            })

            # Refine the last message (which is a user prompt)
            messages[-1] = HumanMessage(
                content=f"{refined}\n\n[Rules context]\n{rules_context}"
            )

        # Invoke Model with whole history
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response]}

    ############################################
    # Build Graph
    ############################################
    graph_builder = StateGraph(State)
    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", tool_node)
    graph_builder.add_edge("tools", "chatbot")
    graph_builder.add_edge(START, "chatbot")
    graph_builder.add_conditional_edges("chatbot", should_continue)
    graph_builder.add_edge("chatbot", END)

    memory = MemorySaver()
    return graph_builder.compile(checkpointer=memory)

async def run_cli(graph: StateGraph) -> None:
    async def stream_graph_updates(user_input: str):
        async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]},
                                         config={"thread_id": "cli-session"}):
            for value in event.values():
                msg = value["messages"][-1]  

                if len(msg.content) > 2:
                    print("-" * 20)
                    # Assistant messages
                    if isinstance(msg, AIMessage):   
                        print(f"Assistant: {msg.content}")
                    # Tool messages
                    if isinstance(msg, ToolMessage):
                        print(f"Tool: {msg.content}")

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