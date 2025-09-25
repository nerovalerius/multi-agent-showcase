import gradio as gr
import asyncio
from langgraph.graph import StateGraph
from langchain.chat_models import init_chat_model
from src.graphs.main_graph import MultiAgentGraphFactory


# Async Generator für Streaming
async def stream_graph_updates(graph: StateGraph, user_input: str):
    async for event in graph.astream(
        {"messages": [{"role": "user", "content": user_input}]},
        config={"recursion_limit": 50, "thread_id": "gradio-session"},
    ):
        for node, value in event.items():
            chunk_lines = [f"➡️ **Switched to node:** `{node}`"]

            if isinstance(value, dict):
                if "messages" in value:
                    for msg in value["messages"]:
                        role = "🤖 Assistant" if msg.type == "ai" else "👤 User"
                        chunk_lines.append(f"{role}: {msg.content}")
                for k, v in value.items():
                    if k != "messages":
                        chunk_lines.append(f"🔍 `{k}` → {v}")
            else:
                chunk_lines.append(f"🔍 Value: {value}")

            # an Chatbot yielden
            yield "\n\n".join(chunk_lines)


def build_app(graph: StateGraph):
    async def bot_reply(user_message, history):
        # neue Zeile für User
        history.append([user_message, ""])
        async for chunk in stream_graph_updates(graph, user_message):
            history[-1][1] += chunk + "\n\n"
            yield history

    with gr.Blocks() as demo:
        gr.Markdown("## 🤖 Multi-Agent Chat")
        chatbot = gr.Chatbot(label="Conversation", height=600)
        msg = gr.Textbox(placeholder="Type your message and press Enter...")
        clear = gr.Button("🗑️ Clear Chat")

        def user_submit(user_message, chat_history):
            return "", chat_history

        msg.submit(
            user_submit, [msg, chatbot], [msg, chatbot]
        ).then(bot_reply, [msg, chatbot], chatbot, queue=True)

        clear.click(lambda: [], None, chatbot)

    return demo


if __name__ == "__main__":
    llm = init_chat_model("gpt-5-mini", model_provider="openai")
    factory = MultiAgentGraphFactory(llm)
    graph = asyncio.run(factory.build_graph())

    app = build_app(graph)
    app.queue()  # wichtig für Streaming
    app.launch()
