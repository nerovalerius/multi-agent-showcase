from pathlib import Path
from typing import Optional

from langchain_openai import OpenAIEmbeddings
from langchain_core.retrievers import BaseRetriever
from langchain_community.vectorstores import FAISS
from langchain.tools.retriever import create_retriever_tool
from langchain_text_splitters import RecursiveCharacterTextSplitter 

class RetrieverFactory:
    @staticmethod
    def create_dynatrace_rules_retriever(search_kwargs: Optional[dict] = None,
                                          search_type: Optional[str] = None) -> BaseRetriever:
        """
        Create or load a FAISS retriever for Dynatrace rules.
        
        Args:
            search_kwargs (Optional[dict]): Additional search parameters for the retriever.
            search_type (Optional[str]): Type of search to perform (e.g., "similarity", "mmr").

        Returns:
            BaseRetriever: Configured retriever instance.
        """
        project_root = Path(__file__).resolve().parents[2]
        dynatrace_rules_index_dir = project_root / "dynatrace_rules_index"
        dynatrace_md_rules_dir = project_root / "dynatrace_rules"

        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        search_kwargs = search_kwargs or {"k": 3}

        try:
            vectorstore = FAISS.load_local(
                dynatrace_rules_index_dir,
                embeddings,
                index_name="index",
                allow_dangerous_deserialization=True,  # required due to pickle in docstore
            )
        except Exception as e:
            print(f"Error loading existing index: {e}. Rebuilding the index.")

            # Load documents
            docs = []
            for filename in dynatrace_md_rules_dir.rglob("*.md"):
                with open(filename, "r", encoding="utf-8") as f:
                    docs.append({"content": f.read(), "source": str(filename.relative_to(dynatrace_md_rules_dir))})

            # Split into chunks
            splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=100)
            chunks = []
            for doc in docs:
                for chunk in splitter.split_text(doc["content"]):
                    chunks.append({
                        "page_content": chunk,
                        "metadata": {"source": doc["source"]}
                    })

            # Create Vector Storage
            vectorstore = FAISS.from_texts(
                [c["page_content"] for c in chunks],
                embedding=embeddings,
                metadatas=[c["metadata"] for c in chunks],
            )

            # save
            vectorstore.save_local(dynatrace_rules_index_dir, index_name="index")

        retriever = vectorstore.as_retriever(
            search_kwargs=search_kwargs,
            **({"search_type": search_type} if search_type else {})
        )

        retriever_tool = create_retriever_tool(
            retriever,
            name="dynatrace_documentation",
            description="Search Dynatrace knowledge base to improve and verify Dynatrace queries and rules."
        )

        return retriever_tool