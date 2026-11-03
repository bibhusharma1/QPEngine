from pathlib import Path


def test_router_imports():
    from src.masterdeorca.document_intelligence.routing.router import DocumentRouter
    r = DocumentRouter()
    assert r is not None


def test_pipeline_interface():
    from src.masterdeorca.document_intelligence.pipeline import DocumentIntelligencePipeline
    p = DocumentIntelligencePipeline()
    assert p is not None
