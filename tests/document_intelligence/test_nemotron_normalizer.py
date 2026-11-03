from src.masterdeorca.document_intelligence.llm.scraper_parser.normalizer import normalize_nemotron_content
from src.masterdeorca.document_intelligence.llm.scraper_parser.nemotron_normalizer import NemotronNormalizer


def test_normalize_sample():
    sample = """<x_0.5312><y_0.1531><class_Page-header>Unit 1</class_Page-header>
Question 1: What is $2+2$?
<x_0.1><y_0.2><class_Table>\\begin{table}\\begin{tabular}{|c|c|}\\hline A&B\\\\\\hline 1&2\\\\\\hline \\end{tabular}\\end{table}</class_Table>
"""
    res = normalize_nemotron_content(sample)
    assert "Unit 1" in res["text"]
    assert res["coordinates"]
    assert res["classes"]
    assert res["tables"]
    assert res["math"]


def test_normalized_coordinates_keep_source_page():
    content = (
        "<class_Page-header>Page 1</class_Page-header>\n"
        "<x_0.1><y_0.2>first</x_0.1><y_0.2>\n"
        "<class_Page-header>Page 2</class_Page-header>\n"
        "<x_0.3><y_0.4>second</x_0.3><y_0.4>"
    )
    result = NemotronNormalizer().normalize(content)
    assert result["coordinate_details"] == [
        {"page": 1, "x": 0.1, "y": 0.2},
        {"page": 2, "x": 0.3, "y": 0.4},
    ]
