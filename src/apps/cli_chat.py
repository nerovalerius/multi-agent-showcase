import asyncio
import json
from pprint import pprint
from langgraph.graph import StateGraph
from langchain.chat_models import init_chat_model
from src.graphs.main_graph import MultiAgentGraphFactory


def pretty_event(event):
    if isinstance(event, dict):
        for key, value in event.items():
            print(f"[{key}]")
            if isinstance(value, dict) and "messages" in value:
                for m in value["messages"]:
                    if hasattr(m, "pretty_print"):
                        m.pretty_print()  # LangChain eigene Formatierung
                    elif hasattr(m, "pretty_repr"):
                        print(m.pretty_repr())
                    else:
                        pprint(m)
            else:
                try:
                    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
                except Exception:
                    pprint(value)
    else:
        if hasattr(event, "pretty_print"):
            event.pretty_print()
        elif hasattr(event, "pretty_repr"):
            print(event.pretty_repr())
        else:
            pprint(event)


async def run_cli(graph: StateGraph) -> None:
    async def stream_graph_updates(user_input: str):
        async for event in graph.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"recursion_limit": 50, "thread_id": "cli-session"},
        ):
            print("-----")
            pretty_event(event)

    while True:
        try:
            user_input = input("User: ")
            if user_input.lower() in ["quit", "exit", "q"] or user_input.strip() == "":
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
