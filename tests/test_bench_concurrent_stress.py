"""Unit tests for bench-concurrent-stress.py — no Docker required."""
import importlib.util
import os
import pytest


def _load():
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'bench-concurrent-stress.py')
    spec = importlib.util.spec_from_file_location("bench_stress", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import():
    mod = _load()
    assert hasattr(mod, '_aggregate')
    assert hasattr(mod, '_evaluate_pass')
    assert hasattr(mod, 'run_stress')
