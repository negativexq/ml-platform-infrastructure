from __future__ import annotations


def test_health_always_ok(client_no_model):
    r = client_no_model.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_503_when_model_missing(client_no_model):
    r = client_no_model.get("/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False


def test_ready_200_when_model_loaded(client_ready):
    r = client_ready.get("/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True


def test_predict_ok(client_ready):
    r = client_ready.post("/predict", json={"features": [1.0, 2.0, 3.0]})
    assert r.status_code == 200
    body = r.json()
    assert "prediction" in body
    assert isinstance(body["prediction"], float)


def test_predict_validation_error(client_ready):
    r = client_ready.post("/predict", json={"features": []})
    assert r.status_code == 422


def test_predict_503_when_not_ready(client_no_model):
    r = client_no_model.post("/predict", json={"features": [1.0, 2.0, 3.0]})
    assert r.status_code == 503
