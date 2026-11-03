# Unit tests for the GPU-only enforcement core (mocked; no real GPU,
# no network — the real-NVIDIA hardware proof lives in
# test_hardware_gpu.py behind QUES_GPU_HARDWARE_TESTS=1).

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import functions.QuesScrapper.ScrapethePDF.gpu_runtime as gpu_runtime
from functions.QuesScrapper.ScrapethePDF.gpu_runtime import (
    GpuRuntimeError,
    ensure_cuda,
    is_cuda_oom,
)


def _fake_paddle(cuda_compiled=True, init_raises=None, sync_raises=None):
    """Paddle mock: importable, optionally CUDA build, can fail init."""
    paddle = MagicMock()
    paddle.__version__ = "9.9.9-fake"
    paddle.device.is_compiled_with_cuda.return_value = cuda_compiled
    paddle.One = None  # marker unused further
    if init_raises is not None:
        paddle.device.set_device.side_effect = init_raises
    paddle.ones.side_effect = lambda *a, **k: MagicMock()
    paddle.device.synchronize = MagicMock(side_effect=sync_raises)
    return paddle


SMI_OK = SimpleNamespace(
    returncode=0,
    stdout="Fake RTX 1234, 595.10, 6144, 5000\n",
    stderr="",
)


@pytest.fixture
def no_smi_cache():
    ensure_cuda.cache_clear()
    yield
    ensure_cuda.cache_clear()


def test_missing_driver_fails_without_cpu_fallback(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: None)

    with pytest.raises(GpuRuntimeError) as err:
        ensure_cuda()
    assert "GPU-only" in str(err.value)
    assert "CPU" in str(err.value)  # message states the no-CPU policy


def test_hidden_gpu_via_environment_fails(monkeypatch, no_smi_cache):
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")  # hides every GPU

    with pytest.raises(GpuRuntimeError) as err:
        ensure_cuda()
    assert "CUDA_VISIBLE_DEVICES" in str(err.value)


def test_smi_failure_fails(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    broken = SimpleNamespace(returncode=1, stdout="", stderr="driver crash")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: broken)

    with pytest.raises(GpuRuntimeError):
        ensure_cuda()


def test_cudnn_low_vram_fails(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    small = SimpleNamespace(returncode=0, stdout="Fake GTX, 595.10, 6144, 100\n", stderr="")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: small)

    with pytest.raises(GpuRuntimeError) as err:
        ensure_cuda()
    assert "free VRAM" in str(err.value)


def test_cpu_paddle_wheel_fails(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: SMI_OK)
    fake = _fake_paddle(cuda_compiled=False)
    monkeypatch.setitem(gpu_runtime.__dict__, "_IMPORT_CACHE", None)
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paddle":
            return fake
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(GpuRuntimeError) as err:
        ensure_cuda()
    assert "no CUDA support" in str(err.value)


def test_cuda_init_failure_fails_without_retry(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: SMI_OK)
    fake = _fake_paddle(init_raises=RuntimeError("driver too old"))
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paddle":
            return fake
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(GpuRuntimeError) as err:
        ensure_cuda()
    assert "CUDA initialisation" in str(err.value)
    assert not fake.utils.run_check.called  # failed init is terminal


def test_healthy_runtime_is_reported(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: SMI_OK)
    fake = _fake_paddle()
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paddle":
            return fake
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    facts = ensure_cuda()
    assert facts["device"] == "gpu:0"
    assert facts["gpu_name"] == "Fake RTX 1234"
    assert facts["driver_version"] == "595.10"
    assert "gpu:0" in gpu_runtime.device_report(facts)
    fake.device.synchronize.assert_called_once()
    fake.utils.run_check.assert_not_called()


def test_cuda_probe_synchronization_failure_fails_closed(monkeypatch, no_smi_cache):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(gpu_runtime.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_runtime.subprocess, "run", lambda *a, **k: SMI_OK)
    fake = _fake_paddle(sync_raises=RuntimeError("CUDA sync failed"))
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paddle":
            return fake
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(GpuRuntimeError, match="CUDA initialisation"):
        ensure_cuda()


def test_cuda_oom_detection():
    assert is_cuda_oom(RuntimeError("CUDA out of memory. Tried to allocate"))
    assert is_cuda_oom(RuntimeError("paddle OutOfMemoryError: GPU memory"))
    assert not is_cuda_oom(ValueError("other failure"))
    assert not is_cuda_oom(None)
