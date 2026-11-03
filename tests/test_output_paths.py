from config.outputs import pdf_stem, stage_output_dir


def test_pdf_folders_are_safe_and_collision_resistant():
    assert pdf_stem("final exam.pdf") == "final exam"
    assert pdf_stem("a#b.pdf") != pdf_stem("a_b.pdf")
    assert pdf_stem("exam.PDF") != pdf_stem("exam.pdf")
    assert stage_output_dir("Parser", "a#b.pdf") != stage_output_dir("Parser", "a_b.pdf")
