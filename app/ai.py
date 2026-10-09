from .document_intelligence import (
    DocumentIntelligenceProvider,
    configured_provider,
    provider,
)


def get_provider() -> DocumentIntelligenceProvider:
    return configured_provider()


__all__ = ["get_provider", "provider"]