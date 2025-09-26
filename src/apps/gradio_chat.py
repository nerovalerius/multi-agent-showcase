import gradio as gr
import asyncio
from langgraph.graph import StateGraph
from langchain.chat_models import init_chat_model
from src.graphs.main_graph import MultiAgentGraphFactory

# global cache
graph_cache = {"model": None, "graph": None}

async def get_graph(selected_model: str) -> StateGraph:
    """Rebuild graph only if the model changes."""
    if graph_cache["model"] != selected_model:
        llm = init_chat_model(selected_model, model_provider="openai")
        factory = MultiAgentGraphFactory(llm)
        await factory.init_tools_and_agents()
        factory.init_supervisor_nodes()
        graph_cache["graph"] = await factory.build_graph()
        graph_cache["model"] = selected_model
    return graph_cache["graph"]

# Stream node activity + messages
async def stream_graph_updates(graph: StateGraph, user_input: str):
    reply_chunks = []
    async for event in graph.astream(
        {"messages": [{"role": "user", "content": user_input}]},
        config={"recursion_limit": 50, "thread_id": "gradio-session"},
    ):
        for node, value in event.items():
            if isinstance(value, dict):
                if "messages" in value:
                    for msg in value["messages"]:
                        role = "🤖 Assistant" if msg.type == "ai" else "👤 User"
                        reply_chunks.append(f"{role}: {msg.content}")
            else:
                reply_chunks.append(f"🔍 Value: {value}")
    return "\n\n".join(reply_chunks)

def build_app():
    with gr.Blocks() as demo:
        gr.Markdown("## 🤖 Multi-Agent Chat")
        gr.Markdown("LLM-driven multi-agent system with Dynatrace MCP")

        model_selector = gr.Dropdown(
            ["gpt-5-nano", "gpt-5-mini", "gpt-5"],
            value="gpt-5-mini",
            label="Choose Model",
        )

        chatbot = gr.Chatbot(label="Conversation", height=600, type="messages")
        msg = gr.Textbox(placeholder="Type your message and press Enter...")
        clear = gr.Button("🗑️ Clear Chat")

        def user_submit(user_message, chat_history):
            chat_history = chat_history + [{"role": "user", "content": user_message}]
            return "", chat_history

        async def bot_reply(chat_history, selected_model):
            graph = await get_graph(selected_model)
            user_message = chat_history[-1]["content"]
            reply = await stream_graph_updates(graph, user_message)
            chat_history = chat_history + [{"role": "assistant", "content": reply}]
            return chat_history

        msg.submit(user_submit, [msg, chatbot], [msg, chatbot]).then(
            bot_reply, [chatbot, model_selector], chatbot
        )
        clear.click(lambda: [], None, chatbot)

    return demo

if __name__ == "__main__":
    app = build_app()
    app.launch()
