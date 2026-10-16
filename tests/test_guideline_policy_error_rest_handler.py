"""Spec bdfdc682 TS3 — REST boundary answers ``GuidelinePolicyPersistenceError``.

The incident of 2026-09-16 surfaced ``GuidelinePolicySubjectConflict``
(``semantic_subject_mutation_conflict``) as an HTTP 500 traceback.  The
handler installed by ``install_guideline_policy_error_handler`` projects the
whole family through the canonical Core mapper: conflict subclasses answer a
retryable 409, the bare persistence failure answers 503, and nothing in the
family is ever a 500.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import okto_pulse.core.ports.guideline_policy as guideline_policy_ports
from okto_pulse.community.api import ideations as ideations_api
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.ideations import router as ideations_router
from okto_pulse.community.app import install_guideline_policy_error_handler
from okto_pulse.core.inbound.guideline_policy_error import (
    guideline_policy_http_status,
    project_guideline_policy_error,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyPersistenceError,
    GuidelinePolicySubjectConflict,
    GuidelinePolicyVersionConflict,
)

ANSWER_PATH = "/api/v1/ideations/ideation-1/qa/qa-1/answer"


class _CountingUow:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _app(uow: _CountingUow) -> FastAPI:
    app = FastAPI()
    install_guideline_policy_error_handler(app)
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="rest-user", realm_id="local"
    )
    app.dependency_overrides[require_user] = lambda: "rest-user"

    async def _uow():
        yield uow

    app.dependency_overrides[get_unit_of_work] = _uow
    app.include_router(ideations_router, prefix="/api/v1")
    return app


def _raise(error: Exception):
    async def reject(*_args, **_kwargs):
        raise error

    return reject


def _persistence_error_classes() -> list[type[GuidelinePolicyPersistenceError]]:
    classes = [
        obj
        for _name, obj in inspect.getmembers(guideline_policy_ports, inspect.isclass)
        if issubclass(obj, GuidelinePolicyPersistenceError)
    ]
    return sorted(classes, key=lambda cls: cls.__name__)


def test_semantic_subject_mutation_conflict_is_a_retryable_409(monkeypatch) -> None:
    uow = _CountingUow()
    error = GuidelinePolicySubjectConflict("semantic_subject_mutation_conflict")
    monkeypatch.setattr(
        ideations_api.AnswerIdeationQuestionUseCase, "execute", _raise(error)
    )

    with TestClient(_app(uow)) as client:
        response = client.post(ANSWER_PATH, json={"selected": ["opt_1"]})

    assert response.status_code == 409
    body = response.json()
    assert body["outcome"] == "error"
    assert body["code"] == "conflict"
    assert body["retryable"] is True
    assert body["next_action"] == "refresh_and_retry"
    assert body["details"]["reason_code"] == "semantic_subject_mutation_conflict"
    assert uow.commit_calls == 0


def test_subject_version_conflict_is_a_retryable_409(monkeypatch) -> None:
    uow = _CountingUow()
    error = GuidelinePolicyVersionConflict("subject_version_conflict")
    monkeypatch.setattr(
        ideations_api.AnswerIdeationQuestionUseCase, "execute", _raise(error)
    )

    with TestClient(_app(uow)) as client:
        response = client.post(ANSWER_PATH, json={"selected": ["opt_1"]})

    assert response.status_code == 409
    body = response.json()
    assert body["retryable"] is True
    assert body["details"]["reason_code"] == "subject_version_conflict"
    assert uow.commit_calls == 0


@pytest.mark.parametrize(
    "error_class",
    _persistence_error_classes(),
    ids=lambda cls: cls.__name__,
)
def test_every_persistence_error_maps_through_the_core_mapper(
    monkeypatch, error_class
) -> None:
    try:
        error = error_class("policy_persistence_probe")
    except TypeError:
        pytest.skip(f"{error_class.__name__} needs a richer constructor")

    expected_status = guideline_policy_http_status(error)
    expected_body = project_guideline_policy_error(error)
    assert expected_status != 500
    if error_class is GuidelinePolicyPersistenceError:
        assert expected_status == 503

    uow = _CountingUow()
    monkeypatch.setattr(
        ideations_api.AnswerIdeationQuestionUseCase, "execute", _raise(error)
    )
    with TestClient(_app(uow)) as client:
        response = client.post(ANSWER_PATH, json={"selected": ["opt_1"]})

    assert response.status_code == expected_status
    assert response.status_code != 500
    assert response.json() == expected_body
    assert uow.commit_calls == 0
