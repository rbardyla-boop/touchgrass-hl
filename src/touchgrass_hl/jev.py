"""Optional TypeSafe/Jev client.

Jev never places orders. If it is disabled or unreachable the rest of the bot
keeps running and the RULES_PLUS_JEV lane fails closed.
"""

from __future__ import annotations

from typing import Any

import httpx

from touchgrass_hl.models import JevResult

# Official question type strings are lowercase, per the TypeSafe HTTP API.
QUESTIONS: dict[str, dict[str, Any]] = {
    "cluster_quality": {
        "type": "score",
        "instructions": "Rate the quality of this smart-wallet convergence candidate.",
        "criteria": [
            "0 = noise or weak coincidence",
            "1 = weak evidence",
            "2 = meaningful convergence",
            "3 = strong convergence",
            "4 = exceptional convergence with strong historical support",
        ],
    },
    "behavior_fit": {
        "type": "noul",
        "instructions": (
            "Does the current activity broadly match historically successful "
            "behavior among the participating wallets?"
        ),
    },
    "regime": {
        "type": "choice",
        "instructions": "Which market regime best describes the supplied state?",
        "criteria": {
            "TREND_CONTINUATION": "Price is continuing an established directional move.",
            "BREAKOUT": "Price is breaking a range or level with expansion.",
            "MEAN_REVERSION": "Price is extended and likely to revert.",
            "SHORT_SQUEEZE": "Short covering is driving a sharp upward move.",
            "LONG_SQUEEZE": "Long liquidation is driving a sharp downward move.",
            "RANGE": "Price is oscillating without a directional break.",
            "DISORDERED": "Choppy, unstable, or internally inconsistent price action.",
            "UNCLEAR": "The supplied state does not identify a regime.",
        },
    },
    "contradiction": {
        "type": "noul",
        "instructions": (
            "Does the supplied market and wallet state contain meaningful evidence "
            "contradicting the proposed directional trade?"
        ),
    },
    "information_sufficient": {
        "type": "noul",
        "instructions": (
            "Is the supplied information sufficient to evaluate this candidate "
            "without relying heavily on important missing data?"
        ),
    },
}


def build_systemone_request(model: str, state: dict[str, Any]) -> dict[str, Any]:
    return {"model": model, "state": state, "questions": QUESTIONS}


def parse_systemone_response(payload: dict[str, Any], *, model_requested: str) -> JevResult:
    if not isinstance(payload, dict):
        return JevResult("error", model_requested, None, {}, {}, "response_not_object")
    model = payload.get("model")
    answers = payload.get("answers")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not isinstance(model, str) or not isinstance(answers, dict):
        return JevResult(
            "error",
            model_requested,
            model if isinstance(model, str) else None,
            {},
            usage,
            "missing_model_or_answers",
            payload,
        )
    required = {
        "cluster_quality": "score",
        "behavior_fit": "noul",
        "regime": "choice",
        "contradiction": "noul",
        "information_sufficient": "noul",
    }
    parsed: dict[str, Any] = {}
    for name, expected in required.items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != expected:
            return JevResult(
                "error",
                model_requested,
                model,
                {},
                usage,
                f"bad_answer:{name}",
                payload,
            )
        if expected == "score" and "score" not in answer:
            return JevResult("error", model_requested, model, {}, usage, f"bad_answer:{name}", payload)
        if expected == "noul" and "noul" not in answer:
            return JevResult("error", model_requested, model, {}, usage, f"bad_answer:{name}", payload)
        if expected == "choice" and "choice" not in answer:
            return JevResult("error", model_requested, model, {}, usage, f"bad_answer:{name}", payload)
        parsed[name] = answer
    return JevResult(
        status="ok",
        model_requested=model_requested,
        model_returned=model,
        answers=parsed,
        usage={
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
        error=None,
        raw=payload,
    )


def jev_policy_vetoes(
    result: JevResult | None,
    *,
    min_cluster_quality: float,
    min_behavior_fit: float,
    max_contradiction: float,
    min_information_sufficient: float,
) -> list[str]:
    """Deterministic reading of Jev judgments. Jev itself cannot approve an order."""
    if result is None or result.status != "ok":
        return ["JEV_UNAVAILABLE"]
    vetoes = []
    score = float(result.answers["cluster_quality"]["score"])
    behavior = float(result.answers["behavior_fit"]["noul"])
    contradiction = float(result.answers["contradiction"]["noul"])
    info = float(result.answers["information_sufficient"]["noul"])
    if score < min_cluster_quality:
        vetoes.append("JEV_CLUSTER_QUALITY_LOW")
    if behavior < min_behavior_fit:
        vetoes.append("JEV_BEHAVIOR_MISMATCH")
    if contradiction > max_contradiction:
        vetoes.append("JEV_CONTRADICTION")
    if info < min_information_sufficient:
        vetoes.append("JEV_INFORMATION_INSUFFICIENT")
    return vetoes


class JevClient:
    def __init__(self, *, enabled: bool, api_key: str, model: str, base_url: str, timeout_s: float) -> None:
        self.enabled = enabled
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def list_models(self) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "not_run", "reason": "JEV_API_KEY unset"}
        url = f"{self.base_url}/v1/models"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            return {"status": "ok", "body": response.json()}

    async def evaluate(self, state: dict[str, Any]) -> JevResult:
        if not self.enabled or not self.api_key:
            return JevResult(
                status="unavailable",
                model_requested=self.model,
                model_returned=None,
                answers={},
                usage={},
                error="JEV_UNAVAILABLE",
            )
        request = build_systemone_request(self.model, state)
        url = f"{self.base_url}/v1/systemone"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(url, headers=self._headers(), json=request)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return JevResult(
                status="unavailable",
                model_requested=self.model,
                model_returned=None,
                answers={},
                usage={},
                error=f"JEV_UNAVAILABLE:{type(exc).__name__}",
            )
        parsed = parse_systemone_response(payload, model_requested=self.model)
        if parsed.status != "ok":
            parsed.status = "unavailable" if parsed.error else parsed.status
            if parsed.status != "ok":
                # Malformed success responses fail closed the same way as an outage.
                parsed.error = parsed.error or "JEV_UNAVAILABLE"
        return parsed
