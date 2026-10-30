"""api/openapi.yaml is generated from the app (api/gen_openapi.py). These fail when the
checked-in file drifts from api.py, or when an error code is raised that the spec
doesn't list."""
import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "api" / "gen_openapi.py"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_openapi", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checked_in_openapi_matches_app():
    gen = _gen()
    want = gen.render()
    have = (REPO / "api" / "openapi.yaml").read_text()
    assert have == want, "api/openapi.yaml is out of date: run `python api/gen_openapi.py`"


def test_openapi_is_valid_and_complete():
    spec = yaml.safe_load((REPO / "api" / "openapi.yaml").read_text())
    assert spec["openapi"].startswith("3.1")
    assert spec["components"]["securitySchemes"]["deviceToken"]["scheme"] == "bearer"
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op.get("summary") and op.get("operationId"), (method, path)
            assert "security" in op, (method, path)
            names = [p["name"].lower() for p in op.get("parameters", [])]
            assert "authorization" not in names, (method, path)
    try:
        from openapi_spec_validator import validate
    except ImportError:
        return
    validate(spec)


def test_every_raised_error_code_is_documented():
    from loft_gateway.api import CODE_STATUS
    src = "".join(p.read_text() for p in (REPO / "gateway" / "loft_gateway").glob("*.py"))
    raised = set(re.findall(r'ApiError\((\d+), "([a-z_]+)"', src))
    raised |= {(m[1], m[0]) for m in re.findall(
        r'JSONResponse\(\{"error": "([a-z_]+)"[^)]*status_code=(\d+)', src)}
    for status, code in raised:
        assert CODE_STATUS.get(code) == int(status), f"{code} ({status}) not in api.ERROR_STATUS"
