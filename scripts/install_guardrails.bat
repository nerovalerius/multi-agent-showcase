echo Installing Guardrails
uv add guardrails-ai

echo Configuring Guardrails - api key needed
guardrails configure

echo Installing Guardrail Hub Checks 1 of 2
uv run guardrails hub install hub://guardrails/profanity_free

echo Installing Guardrail Hub Checks 2 of 2
uv run guardrails hub install hub://guardrails/toxic_language