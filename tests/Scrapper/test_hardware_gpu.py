# Hardware-in-the-loop GPU tests — the REAL NVIDIA proof.
#
# These download/execute real OCR models and NEED:
#   - a compatible NVIDIA GPU + driver (see README/DOCS: driver >= 545
#     for the cu126 paddle build),
#   - the GPU dependency stack installed (uv sync --extra ocr-gpu),
#   - an explicit opt-in:  QUES_GPU_HARDWARE_TESTS=1
#     uv run pytest test/test_hardware_gpu.py -q -m hardware
#
# They are consciously NOT part of the default suite — mocked unit
# tests cannot prove real GPU inference and are never claimed to.

import os

import pytest

hardware = pytest.mark.hardware

requires_gpu = pytest.mark.skipif(
    os.environ.get("QUES_GPU_HARDWARE_TESTS") != "1",
    reason="set QUES_GPU_HARDWARE_TESTS=1 on a machine with a supported "
           "NVIDIA GPU to run the real-inference proof tests",
)


@hardware
def test_real_cuda_runtime_report():
    pytest.importorskip("paddle")
    from functions.QuesScrapper.ScrapethePDF.gpu_runtime import ensure_cuda, device_report

    facts = ensure_cuda()
    assert facts["device"] == "gpu:0"
    assert facts["memory_total_mb"] > 0
    assert "gpu:0" in device_report(facts)
    print(device_report(facts))


@hardware
def test_real_ocr_runs_on_gpu(tmp_path):
    pytest.importorskip("paddleocr")
    import pymupdf

    from functions.QuesScrapper.ScrapethePDF import ocr

    # A page with actual text so OCR must find words.
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "State the theorem of Pythagoras. [3]", fontsize=14)
    png = tmp_path / "page_0001.png"
    page.get_pixmap(dpi=250, colorspace=pymupdf.csRGB).save(png)
    doc.close()

    pages = ocr.run_ocr([png])   # constructs + verifies the real GPU parser
    assert pages and pages[0]["blocks"], "OCR must find the written text"
    assert any("theorem" in block["text"].lower()
               for block in pages[0]["blocks"])
    print(f"OCR device proof: {[b['text'] for b in pages[0]['blocks']][:3]}")


@hardware
def test_hidden_gpu_fails_loudly_no_cpu_fallback():
    pytest.importorskip("paddle")
    import importlib

    import functions.QuesScrapper.ScrapethePDF.gpu_runtime as gpu_runtime

    gpu_runtime.ensure_cuda.cache_clear()
    previous = os.environ.get("CUDA_VISIBLE_DEVICES")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    try:
        with pytest.raises(gpu_runtime.GpuRuntimeError) as err:
            gpu_runtime.ensure_cuda()
        assert "CPU" in str(err.value)     # message names the policy
    finally:
        if previous is None:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = previous
        gpu_runtime.ensure_cuda.cache_clear()
