from pathlib import Path
from typing import Optional
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.tools.retriever import create_retriever_tool
from langchain_text_splitters import RecursiveCharacterTextSplitter 

class RetrieverFactory:


    # Sort files into topics for later creation of relevant tools for each topic
    TOPIC_MAP = {
        # Main
        "DynatraceMcpIntegration.md": ("main", "orchestrator"),
        # Reference
        "DynatraceQueryLanguage.md": ("reference", "common"),
        "DynatraceExplore.md": ("reference", "telemetry"),
        "DynatraceSecurityEvents.md": ("reference", "security"),
        "DynatraceProblemsSpec.md": ("reference", "problems"),
        # Workflows
        "incidentResponse.md": ("workflow", "problems"),
        "DynatraceSecurityCompliance.md": ("workflow", "security"),
        "DynatraceDevOpsIntegration.md": ("workflow", "devops"),
        "dataInvestigation.md": ("workflow", "telemetry"),
        "DynatraceSpanAnalysis.md": ("workflow", "telemetry"),
    }

    @staticmethod
    def build_or_load_index() -> FAISS:
        """
        Either build or load a FAISS index from the dynatrace_rules directory.
        """
        project_root = Path(__file__).resolve().parents[2]
        index_dir = project_root / "dynatrace_rules_index"
        rules_dir = project_root / "dynatrace_rules"
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

        # Try to load existing index
        try:
            return FAISS.load_local(
                index_dir,
                embeddings,
                index_name="index",
                allow_dangerous_deserialization=True, # Required due to FAISS use of pickle
            )
        except Exception:
            docs = []
            for filename in rules_dir.rglob("*.md"):
                rel = filename.relative_to(rules_dir)
                section, topic = RetrieverFactory.TOPIC_MAP.get(
                    rel.name, ("unknown", "common")
                )
                text = filename.read_text(encoding="utf-8")
                splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=100)
                for chunk in splitter.split_text(text):
                    docs.append(
                        {
                            "page_content": chunk,
                            "metadata": {
                                "source": str(rel),
                                "section": section,
                                "topic": topic,
                            },
                        }
                    )

            vectorstore = FAISS.from_texts(
                [d["page_content"] for d in docs],
                embedding=embeddings,
                metadatas=[d["metadata"] for d in docs],
            )

            vectorstore.save_local(index_dir, index_name="index")
            return vectorstore

    @staticmethod
    def create_tool_for_topic(vectorstore: FAISS, topic: str, k: int = 3):
        """ Create a retriever tool for a specific topic."""
        retriever = vectorstore.as_retriever(
            search_kwargs={"k": k, "filter": {"topic": topic}}
        )
        return create_retriever_tool(
            retriever,
            name=f"dynatrace_{topic}_rules",
            description=f"Search Dynatrace {topic} rules and docs.",
        )

    @staticmethod
    def create_tool_dict() -> dict[str, object]:
        vectorstore = RetrieverFactory.build_or_load_index()
        return {
            "telemetry": RetrieverFactory.create_tool_for_topic(vectorstore, "telemetry"),
            "problems": RetrieverFactory.create_tool_for_topic(vectorstore, "problems"),
            "security": RetrieverFactory.create_tool_for_topic(vectorstore, "security"),
            "devops": RetrieverFactory.create_tool_for_topic(vectorstore, "devops"),
            "common": RetrieverFactory.create_tool_for_topic(vectorstore, "common"),
        }
