from __future__ import annotations


def test_health_always_ok(client_no_model):
    r = client_no_model.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_503_when_model_missing(client_no_model):
    r = client_no_model.get("/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert r.json()["state"] == "failed"


def test_health_stays_200_while_model_unavailable(client_no_model):
    """Liveness must not depend on the artifact store being reachable."""
    assert client_no_model.get("/ready").status_code == 503
    assert client_no_model.get("/health").status_code == 200


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


def test_metrics_endpoint_exposes_declared_series(client_ready):
    client_ready.post("/predict", json={"features": [1.0, 2.0, 3.0]})
    body = client_ready.get("/metrics").text
    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "prediction_requests_total",
        "prediction_duration_seconds",
        "model_load_duration_seconds",
        "model_ready",
        "model_info",
    ):
        assert name in body, f"{name} missing from /metrics"
    assert 'model_ready 1.0' in body


def test_metrics_report_not_ready_when_model_missing(client_no_model):
    body = client_no_model.get("/metrics").text
    assert "model_ready 0.0" in body
    assert "model_load_failures_total" in body
