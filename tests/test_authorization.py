import hashlib
import json
import uuid

from app.db import connection


def _create_person(client, name):
    token = uuid.uuid4().hex
    household = client.post(
        "/households",
        json={"external_id": f"AUTH-HH-{token}", "village_id": "Auth Village"},
    ).json()
    person = client.post(
        "/people",
        json={
            "external_id": f"AUTH-P-{token}",
            "household_id": household["id"],
            "name": name,
        },
    ).json()
    return household, person


def _configure_principal(monkeypatch, bearer, subject, person_ids, household_ids=()):
    token_hash = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    config = [
        {
            "token_sha256": token_hash,
            "subject": subject,
            "role": "asha",
            "person_ids": list(person_ids),
            "household_ids": list(household_ids),
        }
    ]
    monkeypatch.setenv("ASHA_ENV", "production")
    monkeypatch.setenv("ASHA_AUTH_TOKENS_JSON", json.dumps(config))


def test_production_requires_bearer_token_but_keeps_health_public(client, monkeypatch):
    _, person = _create_person(client, "Authorized Person")
    _configure_principal(
        monkeypatch,
        "sufficiently-long-test-token",
        "asha-one",
        [person["id"]],
    )

    assert client.get("/health").status_code == 200
    assert client.get(f"/people/{person['id']}").status_code == 401
    assert (
        client.get(
            f"/people/{person['id']}",
            headers={"Authorization": "Bearer sufficiently-long-test-token"},
        ).status_code
        == 200
    )


def test_person_scope_is_enforced_for_routes_and_search(client, monkeypatch):
    household_one, person_one = _create_person(client, "Authorized Alpha")
    person_two = client.post(
        "/people",
        json={
            "external_id": f"AUTH-P-{uuid.uuid4().hex}",
            "household_id": household_one["id"],
            "name": "Private Beta",
        },
    ).json()
    client.post(
        "/events",
        json={
            "external_id": f"AUTH-EV-{uuid.uuid4().hex}",
            "person_id": person_two["id"],
            "event_type": "private_visit",
            "event_date": "2026-10-01",
        },
    )
    _configure_principal(
        monkeypatch,
        "scope-test-token",
        "asha-alpha",
        [person_one["id"]],
    )
    headers = {"Authorization": "Bearer scope-test-token"}

    assert client.get(f"/people/{person_one['id']}", headers=headers).status_code == 200
    assert client.get(f"/people/{person_two['id']}", headers=headers).status_code == 404
    assert (
        client.get(
            f"/people/{person_two['id']}/health-summary",
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/people/{person_two['id']}/timeline",
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/households/{household_one['id']}/timeline",
            headers=headers,
        ).status_code
        == 404
    )

    search = client.get("/search", params={"q": "Private Beta"}, headers=headers)
    assert search.status_code == 200
    assert search.json()["people"] == []
    assert search.json()["events"] == []
    assert (
        client.get("/search", params={"q": "private_visit"}, headers=headers)
        .json()["events"]
        == []
    )
    assert (
        client.get(
            "/search",
            params={"q": "Private Beta", "person_id": person_two["id"]},
            headers=headers,
        ).status_code
        == 404
    )

    forbidden_write = client.post(
        "/events",
        headers=headers,
        json={
            "external_id": f"AUTH-EV-{uuid.uuid4().hex}",
            "person_id": person_two["id"],
            "event_type": "home_visit",
        },
    )
    assert forbidden_write.status_code == 404


def test_scoped_write_cannot_claim_another_actor_or_self_verify(client, monkeypatch):
    _, person = _create_person(client, "Write Scope Person")
    _configure_principal(
        monkeypatch,
        "write-scope-token",
        "asha-authenticated",
        [person["id"]],
    )
    headers = {"Authorization": "Bearer write-scope-token"}

    response = client.post(
        "/events",
        headers=headers,
        json={
            "external_id": f"AUTH-EV-{uuid.uuid4().hex}",
            "person_id": person["id"],
            "event_type": "home_visit",
            "verification_status": "verified",
            "verified_by": "forged-reviewer",
        },
    )
    assert response.status_code == 403

    response = client.post(
        "/events",
        headers=headers,
        json={
            "external_id": f"AUTH-EV-{uuid.uuid4().hex}",
            "person_id": person["id"],
            "event_type": "home_visit",
            "verification_status": "unverified",
            "verified_by": "forged-reviewer",
        },
    )
    assert response.status_code == 200
    assert response.json()["asha_id"] == "asha-authenticated"
    assert response.json()["verified_by"] is None
    assert response.json()["verification_status"] == "unverified"

    with connection() as conn:
        audit = conn.execute(
            """SELECT actor_id, action FROM audit_log
               WHERE entity_type='event' AND entity_id=?""",
            (str(response.json()["id"]),),
        ).fetchone()
    assert audit["actor_id"] == "asha-authenticated"
    assert audit["action"] == "create_event"


def test_missing_or_malformed_production_auth_config_fails_closed(client, monkeypatch):
    monkeypatch.setenv("ASHA_ENV", "production")
    monkeypatch.delenv("ASHA_AUTH_TOKENS_JSON", raising=False)

    assert client.get("/people/1").status_code == 503

    monkeypatch.setenv("ASHA_AUTH_TOKENS_JSON", "not-json")
    assert client.get("/people/1").status_code == 503


def test_duplicate_upload_does_not_disclose_an_inaccessible_document(
    client,
    monkeypatch,
):
    _, allowed_person = _create_person(client, "Allowed Upload Person")
    _, restricted_person = _create_person(client, "Restricted Upload Person")
    content = b"synthetic duplicate document " + uuid.uuid4().hex.encode()
    original = client.post(
        "/documents",
        files={"file": ("record.png", content, "image/png")},
    )
    assert original.status_code == 200
    linked = client.post(
        f"/documents/{original.json()['document_id']}/link-person",
        json={
            "person_id": restricted_person["id"],
            "linked_by": "reviewer",
        },
    )
    assert linked.status_code == 200
    _configure_principal(
        monkeypatch,
        "duplicate-scope-token",
        "asha-allowed",
        [allowed_person["id"]],
    )

    duplicate = client.post(
        "/documents",
        headers={"Authorization": "Bearer duplicate-scope-token"},
        files={"file": ("different-name.png", content, "image/png")},
    )

    assert duplicate.status_code == 409
    assert "outside your access scope" in duplicate.json()["detail"]
    assert "document_id" not in duplicate.json()
