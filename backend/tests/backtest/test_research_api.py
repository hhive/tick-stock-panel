from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import backtest as api
from app.backtest.factor import FACTOR_COLUMNS


def test_factor_batch_api_rejects_unknown_factor():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    req = api.FactorBatchRequest(factor_names=["unknown"])

    with pytest.raises(HTTPException) as exc_info:
        api.factor_batch(req, request)
    assert exc_info.value.status_code == 400
    assert "unknown" in str(exc_info.value.detail)


def test_factor_batch_request_accepts_full_research_catalog():
    factor_names = [item["id"] for item in FACTOR_COLUMNS]

    request = api.FactorBatchRequest(factor_names=factor_names)

    assert len(request.factor_names) > 16
    assert request.factor_names == factor_names


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def test_candidate_api_create_list_and_update(tmp_path):
    # 候选池按账户分家: 用例显式注入当前账户根 (tmp_path), 不再依赖共享 data_dir
    from app.services import preferences

    token = preferences.set_current_user_root(tmp_path)
    try:
        created = api.candidate_create(api.CandidateCreateRequest(
            kind="factor",
            name="RSI 候选",
            source_id="rsi_14",
            config={"factor_name": "rsi_14"},
            metrics={"ic_mean": 0.03},
            data_as_of=date(2026, 8, 11),
        ), _request())

        assert api.candidates_list(_request())["items"][0]["id"] == created["id"]
        updated = api.candidate_update(
            created["id"],
            api.CandidateUpdateRequest(status="validated"),
            _request(),
        )
        assert updated["status"] == "validated"
    finally:
        preferences.reset_current_user_root(token)


def test_candidate_api_returns_clear_error_for_corrupt_file(tmp_path):
    from app.services import preferences

    token = preferences.set_current_user_root(tmp_path)
    try:
        path = tmp_path / "user_data" / "research_candidates.json"
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")

        with pytest.raises(HTTPException) as exc_info:
            api.candidates_list(_request())
        assert exc_info.value.status_code == 500
        assert "损坏" in str(exc_info.value.detail)
    finally:
        preferences.reset_current_user_root(token)
