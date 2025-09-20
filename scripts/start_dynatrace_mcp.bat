@echo off
echo === Starting Dynatrace Chat Project ===

:: Load environment variables from parent directory .env
if exist ..\.env (
  for /f "tokens=* delims=" %%a in (..\.env) do set %%a
)

:: Start MCP server directly via npx (latest version), port 3001
echo Starting MCP Server on port 3001...
start cmd /k npx -y @dynatrace-oss/dynatrace-mcp-server@latest --http --port 3001

:: Wait a few seconds for MCP to boot
timeout /t 5 /nobreak >nul
