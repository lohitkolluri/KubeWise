"""KubeWise forecasting sidecar — gRPC server using statsmodels ETS.

Listens on port 50051 and implements the Forecaster service defined in
proto/forecaster.proto.  Accepts metric history and returns point forecasts
with 95% prediction intervals.
"""

import asyncio
import logging
import os
import warnings
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import grpc
import numpy as np
import pandas as pd
from statsmodels.tsa.api import ETSModel

# Ignore statsmodels convergence warnings for short series.
warnings.filterwarnings("ignore", category=UserWarning, module="statsmodels")

# ── proto stubs (compiled alongside the Go project) ──────────────────────
# The sidecar re-imports the same .proto compiled for Python.
# In production the compiled *_pb2.py lives next to this file or on PYTHONPATH.

import forecaster_pb2 as pb2
import forecaster_pb2_grpc as pb2_grpc

_PORT = int(os.environ.get("FORECASTER_PORT", "50051"))
_HEALTH_PORT = int(os.environ.get("FORECASTER_HEALTH_PORT", "8081"))
_MAX_WORKERS = int(os.environ.get("FORECASTER_WORKERS", "4"))
_MIN_SAMPLES = 10  # minimum points to attempt a forecast


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/readyz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return  # quiet


def _start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    logging.info("health server listening on :%d", port)


def _fit_ets(series: pd.Series, seasonal_periods: int | None):
    """Fit an ETS model; seasonal_periods=None uses trend-only."""
    model = ETSModel(
        series,
        error="add",
        trend="add",
        seasonal="add" if seasonal_periods else None,
        seasonal_periods=seasonal_periods,
    )
    return model.fit(maxiter=500, disp=False)


def _ets_forecast(
    values: list[float],
    horizon: int,
    interval_seconds: float = 15.0,
) -> tuple[list[float], list[float], list[float], list[float], str]:
    """Run ETS (Error-Trend-Seasonality) and return forecast + intervals.

    Returns (timestamps, values, lower_bounds, upper_bounds, error).
    On failure returns empty lists and an error message.
    """
    n = len(values)
    if n < _MIN_SAMPLES:
        return [], [], [], [], f"need >= {_MIN_SAMPLES} points, got {n}"

    series = pd.Series(values)
    seasonal_periods = _infer_seasonal_period(n)
    try:
        fit = _fit_ets(series, seasonal_periods)
    except Exception as exc:
        # Short series often cannot support seasonality — retry trend-only.
        if seasonal_periods is not None:
            try:
                fit = _fit_ets(series, None)
            except Exception as retry_exc:
                return [], [], [], [], f"ETS model failed: {retry_exc}"
        else:
            return [], [], [], [], f"ETS model failed: {exc}"

    try:
        forecast_result = fit.get_prediction(start=n, end=n + horizon - 1)
        pred_mean = forecast_result.predicted_mean.tolist()
        # 95% prediction interval
        pred_lower = forecast_result.se_mean.tolist()
        pred_upper = forecast_result.se_mean.tolist()

        # Build timestamps from the last known time plus interval.
        if len(values) > 1:
            # Use the supplied interval_seconds; fall back to heuristic.
            ts_step = interval_seconds if interval_seconds > 0 else 15.0
        else:
            ts_step = 15.0

        # Placeholder timestamps (caller can ignore and use their own clock).
        timestamps = [float(i + n) * ts_step for i in range(horizon)]

        # Compute confidence intervals from standard error.
        # 95% CI = ±1.96 * se
        ci = 1.96
        lower = [p - ci * s for p, s in zip(pred_mean, pred_lower)]
        upper = [p + ci * s for p, s in zip(pred_mean, pred_upper)]

        return timestamps, pred_mean, lower, upper, ""

    except Exception as exc:
        return [], [], [], [], f"ETS model failed: {exc}"


def _infer_seasonal_period(n: int) -> int | None:
    """Pick a seasonal period; statsmodels ETS needs >= 2 full cycles."""
    for period in (24, 12):
        if n >= 2 * period:
            return period
    return None


class ForecasterServicer(pb2_grpc.ForecasterServicer):
    """gRPC servicer for the Forecaster service."""

    async def Forecast(
        self,
        request: pb2.ForecastRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb2.ForecastResponse:
        logging.info(
            "Forecast request: metric=%s horizon=%d points=%d",
            request.metric_name,
            request.horizon,
            len(request.values),
        )

        timestamps, values, lower, upper, err = _ets_forecast(
            values=list(request.values),
            horizon=int(request.horizon or 12),
            interval_seconds=request.interval_seconds,
        )

        if err:
            logging.warning("Forecast failed for %s: %s", request.metric_name, err)
            return pb2.ForecastResponse(status="error", error_message=err)

        points = [
            pb2.ForecastPoint(
                timestamp=t,
                value=v,
                lower_bound=l,
                upper_bound=u,
            )
            for t, v, l, u in zip(timestamps, values, lower, upper)
        ]

        logging.info(
            "Forecast ok: metric=%s points=%d",
            request.metric_name,
            len(points),
        )
        return pb2.ForecastResponse(points=points, status="ok")


async def serve() -> None:
    """Start the gRPC server and wait for shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s forecaster %(levelname)s %(message)s",
    )

    _start_health_server(_HEALTH_PORT)

    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS),
        options=[
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
        ],
    )
    pb2_grpc.add_ForecasterServicer_to_server(ForecasterServicer(), server)
    listen_addr = f"[::]:{_PORT}"
    server.add_insecure_port(listen_addr)

    logging.info("starting forecaster sidecar on %s", listen_addr)
    await server.start()
    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logging.info("shutting down")
        await server.stop(0)


if __name__ == "__main__":
    asyncio.run(serve())
