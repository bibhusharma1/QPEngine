# NVIDIA GPU-only runtime enforcement.
#
# Every designated OCR / local-inference operation must run on the NVIDIA
# GPU. This module is the single choke-point that verifies that claim at
# runtime instead of trusting the install:
#
#   1. An NVIDIA device + driver must be discoverable (nvidia-smi).
#   2. PaddlePaddle must be CUDA-compiled and actually initialised on a
#      `gpu:` device (probe inference + device synchronization).
#   3. Any failure raises GpuRuntimeError — the caller is expected to
#      fail the run. There is NO CPU fallback and NO retry of a failed
#      GPU initialisation anywhere in this codebase by design.
#
# CPU is only acceptable for parsing/config/filesystem work
# (everything else in the repo), never for OCR or model inference.

import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache

# Minimum VRAM (bytes) an OCR/Inference job must be able to allocate.
# Configurable because PPStructureV3 needs ~2 GB free; 6 GB laptop cards
# with a desktop running shrink quickly.
MIN_FREE_VRAM_BYTES = (
    int(os.environ.get("QUES_MIN_FREE_VRAM_MB", "1500")) * 1024 * 1024
)


class GpuRuntimeError(RuntimeError):
    """Raised when the NVIDIA GPU-only requirement cannot be satisfied.

    Actionable: message always states the failing check and how to fix
    it. The pipeline catches this at the graph boundary and aborts the
    cleanup run instead of falling back to CPU.
    """


def _probe_nvidia_smi():
    """Driver truth straight from the NVIDIA tools (no CUDA libs needed).

    Returns:
        Dict with driver_version, gpu_name, memory_total_mb, or raises.
    """
    smi = shutil.which("nvidia-smi")
    if smi is None:
        raise GpuRuntimeError(
            "No NVIDIA GPU is visible to this machine (nvidia-smi not found). "
            "OCR and model inference are GPU-only: install the NVIDIA driver, "
            "connect an NVIDIA GPU, or run this step on a GPU machine. "
            "CPU execution is not supported for this step."
        )

    result = subprocess.run(
        [smi, "--query-gpu=name,driver_version,memory.total,memory.free",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise GpuRuntimeError(
            f"'nvidia-smi' ran but failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}. "
            "Driver/PCIe problem, or the GPU is claimed by another process. "
            "Fix the driver or free the GPU before running OCR."
        )

    first = result.stdout.strip().splitlines()[0]
    name, driver, total, free = [v.strip() for v in first.split(",")]
    return {
        "gpu_name": name,
        "driver_version": driver,
        "memory_total_mb": int(total),
        "memory_free_mb": int(free),
    }


def _check_free_vram(info):
    """Guarantee some headroom so we fail before CUDA-allocating OOM."""
    if info["memory_free_mb"] * 1024 * 1024 < MIN_FREE_VRAM_BYTES:
        raise GpuRuntimeError(
            f"Only {info['memory_free_mb']} MB free VRAM on the NVIDIA GPU "
            f"({info['gpu_name']}); the OCR stack needs at least "
            f"{MIN_FREE_VRAM_BYTES // 1024 // 1024} MB. Close other "
            "GPU processes (browsers, ML servers) and retry."
        )


@lru_cache(maxsize=1)
def ensure_cuda():
    """Verify the NVIDIA GPU *and* a working CUDA-enabled Paddle runtime.

    Executes, in order:
        1. nvidia-smi probe (driver + device + VRAM headroom).
        2. paddle.device.is_compiled_with_cuda() — the wheel must be a
           CUDA build (an accidental CPU paddle wheel aborts here).
        3. A real CUDA init + a tiny probe computation on the GPU device
           (a wheel built for the wrong CUDA version can still import).
        The real CUDA operation and synchronization above are the runtime
        proof. Paddle 3.3's run_check() adds a static backward-pass check
        that accesses Tensor.place in PIR graph mode and emits a warning;
        it does not add useful coverage for this inference-only pipeline.

    Result is cached — a healthy GPU once means later calls skip the
    (expensive) re-init. A FAILURE is never cached; the next call
    re-verifies from scratch (the check is cheap, and being always
    truthful matters more than microseconds).

    Returns:
        Dict of runtime facts for logging / the processing report:
        {
            gpu_name, driver_version, memory_total_mb, memory_free_mb,
            paddle_version, paddle_cudnn_version, device
        }  (device is always "gpu:0")

    Raises:
        GpuRuntimeError: any check fails, with actionable text. The
            caller must abort and must NOT silently fall back to CPU.
    """
    cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cuda_visible is not None and cuda_visible.strip() in {"", "-1"}:
        raise GpuRuntimeError(
            "CUDA_VISIBLE_DEVICES hides every NVIDIA GPU "
            f"(set to {cuda_visible!r}). OCR is GPU-only in this project — "
            "no CPU fallback is performed: unset CUDA_VISIBLE_DEVICES or "
            "set it to the GPU index (e.g. \"0\") and retry."
        )

    try:
        probe = _probe_nvidia_smi()
        _check_free_vram(probe)
    except GpuRuntimeError:
        raise
    except Exception as exc:
        raise GpuRuntimeError(
            "Could not verify the NVIDIA driver/GPU runtime "
            f"({type(exc).__name__}: {exc}). Check nvidia-smi and the "
            "GPU driver/runtime before starting OCR."
        ) from exc

    try:
        import paddle
    except Exception as exc:  # import error, broken install, etc.
        raise GpuRuntimeError(
            "PaddlePaddle is not usable in this environment "
            f"({type(exc).__name__}: {exc}). OCR cannot start. "
            "Install the GPU stack: uv sync --extra ocr-gpu "
            "(it pins the CUDA wheel index)."
        ) from exc

    if not paddle.device.is_compiled_with_cuda():
        raise GpuRuntimeError(
            "The installed PaddlePaddle wheel has no CUDA support "
            "(is_compiled_with_cuda() is False — a CPU build). "
            "OCR is GPU-only: reinstall with "
            "'uv sync --extra ocr-gpu', which pins "
            "https://www.paddlepaddle.org.cn/packages/stable/cu126/."
        )

    # Force an actual GPU context now — importing the CUDA build is not
    # proof the runtime picks the GPU.
    try:
        paddle.device.set_device("gpu:0")
        probe_a = paddle.ones([2, 3], dtype="float32")
        probe_b = paddle.ones([2, 3], dtype="float32")
        result_probe = (probe_a + probe_b)  # runs inside the GPU context
        paddle.device.synchronize()
        del probe_a, probe_b, result_probe
    except Exception as exc:
        # CUDA init failure (wrong driver/toolkit, register contention,
        # broken libcudart) — fail fast, never retry, never switch to CPU.
        raise GpuRuntimeError(
            "CUDA initialisation on the NVIDIA GPU failed "
            f"({type(exc).__name__}: {exc}). "
            "The driver and the paddle CUDA build must match; check "
            "driver >= 545 for cu126 wheels. No CPU fallback is performed."
        ) from exc

    cudnn = None
    try:
        cudnn = paddle.device.get_cudnn_version()
    except Exception:
        pass

    return {
        "device": "gpu:0",
        "gpu_name": probe["gpu_name"],
        "driver_version": probe["driver_version"],
        "memory_total_mb": probe["memory_total_mb"],
        "memory_free_mb": probe["memory_free_mb"],
        "paddle_version": paddle.__version__,
        "paddle_cudnn_version": cudnn,
    }


def is_cuda_oom(exc):
    """Detect CUDA out-of-memory in an arbitrary caught exception."""
    if exc is None:
        return False
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "out of memory" in text
        or "outofmemory" in text
        or "oom" in text
        or name.startswith("outofmemory")
        or "cuda_memory_error" in text
    )


def runtime_report():
    """Return OCR readiness together with the interpreter serving the app."""
    environment = {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "uv_project_environment": os.environ.get("UV_PROJECT_ENVIRONMENT"),
    }
    try:
        facts = ensure_cuda()
    except Exception as exc:
        message = str(exc) if isinstance(exc, GpuRuntimeError) else (
            f"OCR runtime check failed ({type(exc).__name__}: {exc}). "
            "Check the active uv environment and NVIDIA runtime."
        )
        return {"status": "unavailable", "environment": environment, "error": message}
    return {"status": "available", "environment": environment, "runtime": facts, "error": None}


def device_report(facts=None):
    """Human-readable GPU summary for logs / the processing report."""
    if facts is None:
        facts = ensure_cuda()
    return (
        f"GPU device : {facts['device']} — {facts['gpu_name']} "
        f"({facts['memory_total_mb']} MB VRAM, "
        f"{facts['memory_free_mb']} MB free at start)\n"
        f"Driver     : {facts['driver_version']} / CUDA cu126 wheel build\n"
        f"PaddlePaddle: {facts['paddle_version']} "
        f"(cuDNN {facts['paddle_cudnn_version']})"
    )
