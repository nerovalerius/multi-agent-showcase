import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import MemorySaver
from src.graphs.main_graph import MultiAgentGraphFactory


class ChatBot:
    """Dynatrace assistant using LangGraph + LangChain."""

    AVAILABLE_MODELS = {
        "gpt-5-mini",
        "gpt-5",
        "gpt-5-nano",
    }

    def __init__(self, model_name: str = "gpt-5-mini"):
        self.model_name = model_name
        self.memory = MemorySaver()
        self.llm = self._setup_llm()
        self.graph = None  # build later, async

    def _setup_llm(self):
        root_dir = Path(__file__).resolve().parents[2]
        env_path = root_dir / ".env"
        load_dotenv(dotenv_path=env_path, override=True)
        return init_chat_model(self.model_name, model_provider="openai", streaming=True)

    async def setup_graph(self):
        """Build graph asynchronously (only once)."""
        if self.graph is None:
            factory = MultiAgentGraphFactory(llm=self.llm, memory_saver=self.memory)
            self.graph = await factory.build_graph()
            print("[DEBUG ChatBot] Graph built", flush=True)

    async def chat(self, message: str, thread_id: str):
        """Stream assistant responses event by event."""
        # ensure graph exists
        if self.graph is None:
            await self.setup_graph()

        print(f"[DEBUG ChatBot.chat] started with message={message}, thread_id={thread_id}", flush=True)

        got_output = False
        async for event in self.graph.astream(
            {"messages": [{"role": "user", "content": message}]},
            config={"recursion_limit": 50, "thread_id": thread_id},
        ):
            print(f"[DEBUG ChatBot.chat] event: {event}", flush=True)

            for node, value in event.items():
                if isinstance(value, dict) and "messages" in value:
                    for msg in value["messages"]:
                        content = getattr(msg, "content", None)
                        if content:
                            got_output = True
                            print(f"[DEBUG ChatBot.chat] yielding content={content}", flush=True)
                            yield str(content)

        if not got_output:
            print("[DEBUG ChatBot.chat] no output messages", flush=True)
            yield "⚠️ No response from graph"

    async def update_model(self, model_name: str):
        """Update the LLM and rebuild graph."""
        self.model_name = model_name
        self.llm = self._setup_llm()
        self.graph = None
        await self.setup_graph()
        print(f"[DEBUG ChatBot] model updated to {model_name}", flush=True)
