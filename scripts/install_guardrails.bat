echo Installing Guardrails
uv add guardrails-ai

echo Configuring Guardrails - api key needed
guardrails configure

echo Installing Guardrail Hub Checks 1 of 3
uv run guardrails hub install hub://guardrails/profanity_free

echo Installing Guardrail Hub Checks 2 of 3
uv run guardrails hub install hub://guardrails/toxic_language

echo Installing Guardrail Hub Checks 3 of 3
uv run guardrails hub install hub://guardrails/ban_list 