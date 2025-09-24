import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

class MCPClientFactory:

    @staticmethod
    async def create_dynatrace_mcp_client(DT_ENVIRONMENT: str = None, DT_PLATFORM_TOKEN: str = None) -> list:
        """Create and configure the MultiServerMCPClient for Dynatrace MCP server.
    
        Args:
            DT_ENVIRONMENT (str, optional): Dynatrace environment URL. If not provided,
            DT_PLATFORM_TOKEN (str, optional): Dynatrace platform token. If not provided,

        Returns:
            list: List of tools.
        """
        if not DT_ENVIRONMENT or not DT_PLATFORM_TOKEN:
            project_root = Path(__file__).resolve().parents[2]
            env_path = project_root / ".env"
            load_dotenv(dotenv_path=env_path, override=True)
            DT_ENVIRONMENT = os.getenv("DT_ENVIRONMENT")
            DT_PLATFORM_TOKEN = os.getenv("DT_PLATFORM_TOKEN")
            os.environ['OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE'] = "delta"

        dynatrace_mcp_client = MultiServerMCPClient({
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

        return await dynatrace_mcp_client.get_tools()