import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph

from src.graphs.main_graph import MultiAgentGraphFactory

class ChatBot:
    """A simple chatbot class that uses LangGraph and LangChain for conversational AI."""
    AVAILABLE_MODELS = {
        "gpt-5-mini",
        "gpt-5"
        "gpt-5-nano",
    }

    def __init__(self, model_name: str = "gpt-5-mini"):
        """Initialize the ChatBot with the specified LLM model."""
        self.model_name = model_name
        self.memory = MemorySaver()
        self.llm = self._setup_llm()
        self.graph = self._setup_workflow()

    def _setup_llm(self):
        """Initialize the chat model based on the selected model name."""
        root_dir = Path(__file__).resolve().parents[2]
        env_path = root_dir / ".env"
        load_dotenv(dotenv_path=env_path, override=True)
        return init_chat_model("gpt-5-mini", model_provider="openai")

    def _setup_workflow(self):
        """Set up the multi-agent workflow graph."""
        factory = MultiAgentGraphFactory(llm=self.llm, memory_saver=self.memory)
        return asyncio.run(factory.build_graph())

    async def chat(self, message: str, thread_id: str):
        """Stream tokens from the graph as they arrive."""
        async for event in self.graph.astream(
            {"messages": [{"role": "user", "content": message}]},
            config={"recursion_limit": 50, "thread_id": thread_id},
        ):
            for node, value in event.items():
                if isinstance(value, dict) and "messages" in value:
                    for msg in value["messages"]:
                        yield msg.content

    def update_model(self, model_name: str):
        """Update the LLM model used by the chatbot."""
        self.model_name = model_name
        self.llm = self._setup_llm()
        self.app = self._setup_workflow()