from __future__ import annotations

from app.core.telemetry import _metrics_endpoint


def test_otlp_metrics_endpoint_derivation() -> None:
    assert (
        _metrics_endpoint("https://collector.example.com/v1/traces")
        == "https://collector.example.com/v1/metrics"
    )
    assert (
        _metrics_endpoint("https://collector.example.com")
        == "https://collector.example.com/v1/metrics"
    )
    assert (
        _metrics_endpoint("https://collector.example.com/otlp")
        == "https://collector.example.com/otlp/v1/metrics"
    )
    assert (
        _metrics_endpoint("https://collector.example.com/v1/metrics")
        == "https://collector.example.com/v1/metrics"
    )
