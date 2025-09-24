import gradio as gr
import asyncio
from langgraph.graph import StateGraph
from langchain.chat_models import init_chat_model
from src.graphs.main_graph import MultiAgentGraphFactory


# Stream node activity + messages
async def stream_graph_updates(graph: StateGraph, user_input: str):
    reply_chunks = []
    async for event in graph.astream(
        {"messages": [{"role": "user", "content": user_input}]},
        config={"recursion_limit": 50, "thread_id": "gradio-session"},
    ):
        for node, value in event.items():
            reply_chunks.append(f"➡️ **Switched to node:** `{node}`")

            if isinstance(value, dict):
                # Messages explizit
                if "messages" in value:
                    for msg in value["messages"]:
                        role = "🤖 Assistant" if msg.type == "ai" else "👤 User"
                        reply_chunks.append(f"{role}: {msg.content}")

                # Alles andere auch loggen
                for k, v in value.items():
                    if k != "messages":
                        reply_chunks.append(f"🔍 `{k}` → {v}")
            else:
                reply_chunks.append(f"🔍 Value: {value}")

    return "\n\n".join(reply_chunks)


def build_app(graph: StateGraph):
    async def chat_fn(message, history):
        return await stream_graph_updates(graph, message)

    with gr.Blocks() as demo:
        gr.Markdown("## 🤖 Multi-Agent Chat")
        gr.Markdown("LLM-driven multi-agent system with Dynatrace MCP")

        chatbot = gr.Chatbot(label="Conversation", height=600)
        msg = gr.Textbox(placeholder="Type your message and press Enter...")
        clear = gr.Button("🗑️ Clear Chat")

        def user_submit(user_message, chat_history):
            chat_history = chat_history + [[user_message, None]]
            return "", chat_history

        async def bot_reply(chat_history):
            user_message = chat_history[-1][0]
            reply = await stream_graph_updates(graph, user_message)
            chat_history[-1][1] = reply
            return chat_history

        msg.submit(user_submit, [msg, chatbot], [msg, chatbot]).then(
            bot_reply, chatbot, chatbot
        )
        clear.click(lambda: [], None, chatbot)

    return demo


if __name__ == "__main__":
    llm = init_chat_model("gpt-5-mini", model_provider="openai")
    factory = MultiAgentGraphFactory(llm)
    graph = asyncio.run(factory.build_graph())

    app = build_app(graph)
    app.launch()
