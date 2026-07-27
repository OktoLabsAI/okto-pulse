"""Community HTTP mapping for the Core Knowledge Governance error."""

from fastapi.responses import JSONResponse

from okto_pulse.core.domain.knowledge_governance import (
    KnowledgeGovernanceInvalidMetadata,
)


def knowledge_governance_error_response(
    error: KnowledgeGovernanceInvalidMetadata,
) -> JSONResponse:
    return JSONResponse(status_code=422, content=error.to_error_dict())


__all__ = [
    "KnowledgeGovernanceInvalidMetadata",
    "knowledge_governance_error_response",
]
