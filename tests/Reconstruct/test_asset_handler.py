"""Unit tests for functions.ReconstructQues.asset_handler."""

from pathlib import Path
from PIL import Image
import pytest

from docx import Document
from functions.ReconstructQues.asset_handler import AssetHandler


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    """Create a temporary 100x100 dummy PNG image."""
    img_path = tmp_path / "test_diagram.png"
    img = Image.new("RGB", (100, 100), color="blue")
    img.save(img_path)
    return img_path


def test_safe_path_resolution(tmp_path: Path, sample_image: Path):
    """Test safe resolution of relative asset path within base_dir."""
    handler = AssetHandler(base_dir=tmp_path)
    resolved = handler.resolve_path("test_diagram.png")
    assert resolved == sample_image
    assert resolved.exists()


def test_path_traversal_rejection(tmp_path: Path):
    """Test that resolving paths escaping base_dir raises security ValueError."""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    handler = AssetHandler(base_dir=sandbox_dir)

    with pytest.raises(ValueError, match="Security violation"):
        handler.resolve_path("../outside.png")


def test_insert_existing_asset(tmp_path: Path, sample_image: Path):
    """Test successful insertion of an existing image asset."""
    handler = AssetHandler(base_dir=tmp_path)
    doc = Document()

    asset = {
        "asset_id": "fig_1",
        "path": "test_diagram.png",
        "description": "Figure 1: Test architecture",
    }
    inserted = handler.insert_asset(doc, asset, question_label="Q1")

    assert inserted is True
    assert handler.inserted_count == 1
    assert len(handler.missing_assets) == 0
    assert len(doc.inline_shapes) == 1


def test_duplicate_prevention(tmp_path: Path, sample_image: Path):
    """Test that referencing the same asset ID / path twice avoids duplicate insertion."""
    handler = AssetHandler(base_dir=tmp_path)
    doc = Document()

    asset = {
        "asset_id": "fig_dup",
        "path": "test_diagram.png",
    }
    first_insert = handler.insert_asset(doc, asset, question_label="Q1")
    second_insert = handler.insert_asset(doc, asset, question_label="Q2")

    assert first_insert is True
    assert second_insert is False
    assert handler.inserted_count == 1
    assert len(doc.inline_shapes) == 1


def test_missing_asset_non_strict(tmp_path: Path):
    """Test that missing asset in non-strict mode records missing without raising."""
    handler = AssetHandler(base_dir=tmp_path, strict=False)
    doc = Document()

    asset = {
        "asset_id": "missing_1",
        "path": "non_existent_file.png",
    }
    inserted = handler.insert_asset(doc, asset, question_label="Q3")

    assert inserted is False
    assert handler.inserted_count == 0
    assert len(handler.missing_assets) == 1
    assert "non_existent_file.png" in handler.missing_assets[0]


def test_missing_asset_strict_raises(tmp_path: Path):
    """Test that missing asset in strict mode raises FileNotFoundError."""
    handler = AssetHandler(base_dir=tmp_path, strict=True)
    doc = Document()

    asset = {
        "asset_id": "missing_strict",
        "path": "does_not_exist.png",
    }
    with pytest.raises(FileNotFoundError):
        handler.insert_asset(doc, asset, question_label="Q4")

