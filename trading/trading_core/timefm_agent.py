from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple
import os

from .models import IntradayAnalysis
from .paths import DEFAULT_CONFIG
from .storage import load_config


_MODEL_CACHE: Dict[Tuple[str, bool], object] = {}
_MODEL_LOCK = Lock()


def load_timefm_config(config_path: Path = DEFAULT_CONFIG) -> Dict:
    payload = load_config(config_path)
    raw = payload.get("timefm", {}) if isinstance(payload, dict) else {}
    cfg = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "model_name": str(cfg.get("model_name", "google/timesfm-2.0-500m-pytorch")).strip(),
        "endpoint": str(cfg.get("endpoint", "https://hf-mirror.com")).strip(),
        "context_length": max(32, int(cfg.get("context_length", 256) or 256)),
        "horizon": max(4, int(cfg.get("horizon", 12) or 12)),
        "min_bars": max(32, int(cfg.get("min_bars", 96) or 96)),
        "normalize_inputs": bool(cfg.get("normalize_inputs", True)),
        "torch_compile": bool(cfg.get("torch_compile", False)),
        "target_blend_weight": min(0.8, max(0.0, float(cfg.get("target_blend_weight", 0.35) or 0.35))),
    }


def _build_legacy_hparams(timesfm, cfg: Dict):
    model_name = str(cfg.get("model_name", "") or "")
    horizon = int(cfg["horizon"])
    context_length = int(cfg["context_length"])
    if "2.0" in model_name:
        return timesfm.TimesFmHparams(
            backend="cpu",
            per_core_batch_size=1,
            horizon_len=horizon,
            context_len=max(context_length, 512),
            num_layers=50,
            use_positional_embedding=False,
        )
    return timesfm.TimesFmHparams(
        backend="cpu",
        per_core_batch_size=1,
        horizon_len=horizon,
        context_len=min(max(context_length, 128), 512),
        input_patch_len=32,
        output_patch_len=128,
        num_layers=20,
        model_dims=1280,
    )


def _get_timefm_model(cfg: Dict) -> Tuple[Optional[object], str]:
    try:
        import timesfm  # type: ignore
    except Exception:
        return None, "timesfm_not_installed"
    endpoint = str(cfg.get("endpoint", "") or "").strip()
    if endpoint:
        os.environ.setdefault("HF_ENDPOINT", endpoint)
        os.environ.setdefault("HUGGINGFACE_HUB_ENDPOINT", endpoint)

    cache_key = (cfg["model_name"], bool(cfg.get("torch_compile", False)))
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached, ""

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached, ""
        try:
            model_cls = getattr(timesfm, "TimesFM_2p5_200M_torch", None)
            forecast_cls = getattr(timesfm, "ForecastConfig", None)
            if model_cls is not None and forecast_cls is not None:
                model = model_cls.from_pretrained(
                    cfg["model_name"],
                    torch_compile=bool(cfg.get("torch_compile", False)),
                )
                model.compile(
                    forecast_cls(
                        max_context=int(cfg["context_length"]),
                        max_horizon=int(cfg["horizon"]),
                        normalize_inputs=bool(cfg.get("normalize_inputs", True)),
                        use_continuous_quantile_head=True,
                        force_flip_invariance=True,
                        infer_is_positive=True,
                        fix_quantile_crossing=True,
                    )
                )
            elif hasattr(timesfm, "TimesFm") and hasattr(timesfm, "TimesFmHparams") and hasattr(timesfm, "TimesFmCheckpoint"):
                hparams = _build_legacy_hparams(timesfm, cfg)
                checkpoint = timesfm.TimesFmCheckpoint(
                    version="torch",
                    huggingface_repo_id=cfg["model_name"],
                )
                model = timesfm.TimesFm(hparams=hparams, checkpoint=checkpoint)
            else:
                return None, "unsupported_timesfm_api"
        except Exception as exc:
            message = f"{type(exc).__name__}:{str(exc).strip()}" if str(exc).strip() else type(exc).__name__
            return None, f"model_load_failed:{message}"
        _MODEL_CACHE[cache_key] = model
        return model, ""


def _forecast_bias(last_price: float, end_price: float, risk_unit: float) -> str:
    threshold = max(risk_unit * 0.6, last_price * 0.0025)
    diff = end_price - last_price
    if diff >= threshold:
        return "bullish"
    if diff <= -threshold:
        return "bearish"
    return "neutral"


def _forecast_confidence(last_price: float, end_price: float, path_high: float, path_low: float, risk_unit: float) -> str:
    directional_move = abs(end_price - last_price)
    path_width = max(path_high - path_low, 0.0)
    if directional_move >= risk_unit * 1.4 and path_width <= max(risk_unit * 2.2, last_price * 0.02):
        return "高"
    if directional_move >= risk_unit * 0.7:
        return "中"
    return "低"


def _coerce_forecast_row(payload) -> List[float]:
    if payload is None:
        return []
    row = payload
    if hasattr(payload, "tolist"):
        row = payload.tolist()
    if isinstance(row, list) and row and isinstance(row[0], list):
        row = row[0]
    values: List[float] = []
    if not isinstance(row, list):
        return values
    for item in row:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue
    return values


def build_timefm_forecast(
    code: str,
    bars: List[Dict],
    analysis: IntradayAnalysis,
    config_path: Path = DEFAULT_CONFIG,
) -> Dict:
    cfg = load_timefm_config(config_path)
    if not cfg["enabled"]:
        return {"enabled": False, "status": "disabled", "code": code}
    if len(bars) < int(cfg["min_bars"]):
        return {"enabled": True, "status": "insufficient_bars", "code": code}

    try:
        import numpy as np  # type: ignore
    except Exception:
        return {"enabled": True, "status": "numpy_missing", "code": code}

    history = [float(bar.get("close", 0) or 0) for bar in bars if float(bar.get("close", 0) or 0) > 0]
    if len(history) < int(cfg["min_bars"]):
        return {"enabled": True, "status": "insufficient_history", "code": code}

    model, error = _get_timefm_model(cfg)
    if model is None:
        return {"enabled": True, "status": error or "unavailable", "code": code}

    horizon = int(cfg["horizon"])
    context = history[-int(cfg["context_length"]):]
    last_price = float(context[-1])
    try:
        if hasattr(model, "compile"):
            point_forecast, _ = model.forecast(
                horizon=horizon,
                inputs=[np.asarray(context, dtype=float)],
            )
        else:
            point_forecast, _ = model.forecast(
                inputs=[np.asarray(context, dtype=float)],
                freq=[0],
                normalize=bool(cfg.get("normalize_inputs", True)),
                forecast_context_len=len(context),
            )
    except Exception:
        return {"enabled": True, "status": "forecast_failed", "code": code}

    path = _coerce_forecast_row(point_forecast)
    if not path:
        return {"enabled": True, "status": "empty_forecast", "code": code}

    end_price = float(path[-1])
    path_high = float(max(path))
    path_low = float(min(path))
    expected_return_pct = (end_price - last_price) / last_price * 100 if last_price > 0 else 0.0
    bias = _forecast_bias(last_price, end_price, analysis.risk_unit)
    confidence = _forecast_confidence(last_price, end_price, path_high, path_low, analysis.risk_unit)
    summary = (
        f"TimeFM {bias} | {horizon}步后 {end_price:.2f} "
        f"({expected_return_pct:+.2f}%) | 路径区间 {path_low:.2f}-{path_high:.2f} | 置信度{confidence}"
    )
    return {
        "enabled": True,
        "status": "ok",
        "code": code,
        "source": "google-timesfm",
        "horizon": horizon,
        "last_price": round(last_price, 2),
        "end_price": round(end_price, 2),
        "high_price": round(path_high, 2),
        "low_price": round(path_low, 2),
        "return_pct": round(expected_return_pct, 2),
        "bias": bias,
        "confidence": confidence,
        "summary": summary,
    }


def enrich_analysis_with_timefm(
    code: str,
    bars: List[Dict],
    analysis: IntradayAnalysis,
    config_path: Path = DEFAULT_CONFIG,
) -> IntradayAnalysis:
    forecast = build_timefm_forecast(code, bars, analysis, config_path=config_path)
    analysis.forecast_enabled = bool(forecast.get("enabled", False))
    analysis.forecast_source = str(forecast.get("source", "") or "")
    analysis.forecast_horizon = int(forecast.get("horizon", 0) or 0)
    analysis.forecast_end_price = float(forecast.get("end_price", 0) or 0)
    analysis.forecast_high_price = float(forecast.get("high_price", 0) or 0)
    analysis.forecast_low_price = float(forecast.get("low_price", 0) or 0)
    analysis.forecast_return_pct = float(forecast.get("return_pct", 0) or 0)
    analysis.forecast_bias = str(forecast.get("bias", "neutral") or "neutral")
    analysis.forecast_confidence = str(forecast.get("confidence", "低") or "低")
    analysis.forecast_summary = str(forecast.get("summary", forecast.get("status", "")) or "")

    if forecast.get("status") != "ok":
        return analysis

    cfg = load_timefm_config(config_path)
    weight = float(cfg.get("target_blend_weight", 0.35) or 0.35)
    if analysis.forecast_bias == "bullish" and analysis.forecast_high_price > 0 and analysis.t_sell_target > 0:
        adjusted = analysis.t_sell_target * (1 - weight) + analysis.forecast_high_price * weight
        analysis.t_sell_target = round(max(analysis.t_sell_target, adjusted), 2)
    elif analysis.forecast_bias == "bearish" and analysis.forecast_low_price > 0 and analysis.t_buy_target > 0:
        adjusted = analysis.t_buy_target * (1 - weight) + analysis.forecast_low_price * weight
        analysis.t_buy_target = round(max(adjusted, 0.0), 2)

    if analysis.t_buy_target > 0 and analysis.t_sell_target > analysis.t_buy_target:
        analysis.t_spread_pct = round((analysis.t_sell_target - analysis.t_buy_target) / analysis.t_buy_target * 100, 1)
    return analysis
