import asyncio
from langgraph.graph import StateGraph
from langchain.chat_models import init_chat_model
from src.graphs.main_graph import MultiAgentGraphFactory

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