from guardrails.validators import Validator, ValidationResult, register_validator

@register_validator(name="block_terms", data_type="string")
class BlockTerms(Validator):
    """Validator that blocks specific terms in the input text."""
    def __init__(self, blocked: list[str]):
        super().__init__()
        self.blocked = [b.lower() for b in blocked]

    def validate(self, value: str, metadata: dict = None) -> ValidationResult:
        text = value.lower()
        for term in self.blocked:
            if term in text:
                return ValidationResult(
                    value=value,
                    validated=False,
                    error_message=f"Contains blocked term: {term}",
                )
        return ValidationResult(value=value, validated=True)
