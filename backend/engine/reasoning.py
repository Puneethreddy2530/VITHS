"""
Phase 4 — reasoning.py
Azure OpenAI-powered causal reasoning layer.
  - Explainable alerts: WHY this event is flagged
  - Predictive timeline: WHEN the next event is likely
  - Action recommendation: WHAT the guard should do
  - Privacy-preserving: no images sent to LLM, only metadata

Cite: Lewis et al. (2020) RAG · Azure OpenAI GPT-4o
"""

import json, os
from datetime import datetime
from typing import Optional
from openai import AzureOpenAI

try:
    from dotenv import load_dotenv
    # Load from the physical .env file in the project root
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    load_dotenv(env_path)
except ImportError:
    pass

# ── Azure OpenAI configuration ────────────────────────────────────
AZURE_OPENAI_API_KEY         = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT        = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_VERSION     = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
AZURE_OPENAI_DEPLOYMENT_NAME = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

_FALLBACK_RESPONSES = {
    "HIGH":   {
        "risk_level":          "HIGH",
        "pattern_summary":     "Repeated suspicious activity in this zone",
        "why_flagged":         ["Multiple occurrences detected", "Unusual time of day", "Near entry point"],
        "predicted_next":      "High probability within next 2 hours",
        "recommended_action":  "Deploy security guard to this zone immediately",
    },
    "MEDIUM": {
        "risk_level":          "MEDIUM",
        "pattern_summary":     "Unusual activity detected, monitoring recommended",
        "why_flagged":         ["Motion anomaly detected", "Behavior pattern unusual"],
        "predicted_next":      "Monitor for next 30 minutes",
        "recommended_action":  "Increase surveillance frequency for this zone",
    },
    "LOW":    {
        "risk_level":          "LOW",
        "pattern_summary":     "Minor anomaly, likely false positive",
        "why_flagged":         ["Low confidence detection"],
        "predicted_next":      "No immediate concern",
        "recommended_action":  "No action required",
    },
}


class ReasoningEngine:
    def __init__(self):
        if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
            print("  [WARN] Azure OpenAI credentials not set — using fallback templates")
            self._use_llm = False
        else:
            self._client = AzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                api_version=AZURE_OPENAI_API_VERSION,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
            )
            self._deployment = AZURE_OPENAI_DEPLOYMENT_NAME
            self._use_llm = True
            print("  Azure OpenAI reasoning engine ready.")

    def _apply_forced_risk_clamp(self, event: dict, result: dict) -> dict:
        """Empty-room environmental hits: cap risk and replace dramatic ghost-trigger copy."""
        if event.get("forced_risk") != "LOW":
            return result
        result["risk_level"] = "LOW"
        reasoning_text = " ".join(
            [str(result.get("pattern_summary", ""))]
            + [str(w) for w in (result.get("why_flagged") or [])]
        )
        if "Unusual localized movement" in reasoning_text or "No person detected" in reasoning_text:
            result["pattern_summary"] = (
                "[SYSTEM LOG] Minor environmental shift (lighting/shadow). No human presence. Ignored."
            )
        return result

    def _build_prompt(self, event: dict, similar: list, recurrence: int) -> str:
        # Format similar events (past memory recalls)
        past = []
        for s in similar[:3]:
            e = s["event"]
            past.append({
                "similarity":  s["similarity"],
                "behavior":    e["behavior"],
                "zone_id":     e["zone_id"],
                "timestamp":   e["timestamp"],
            })

        return f"""You are a hostel security AI. Analyze this anomaly event and provide actionable intelligence.

CURRENT EVENT:
- Zone: {event.get('zone_id')}
- Behavior: {event.get('behavior_label')}
- CLIP anomaly score: {event.get('clip_score', 0):.3f}
- Time: {datetime.utcnow().strftime('%H:%M')} ({'night' if datetime.utcnow().hour >= 22 or datetime.utcnow().hour < 6 else 'day'})
- Times this pattern occurred before: {recurrence}

SIMILAR PAST EVENTS (from memory):
{json.dumps(past, indent=2)}

Respond ONLY with a valid JSON object, no markdown, no explanation outside JSON:
{{
  "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "pattern_summary": "one clear sentence",
  "why_flagged": ["reason 1", "reason 2", "reason 3"],
  "predicted_next": "specific prediction about when/where this might recur",
  "recommended_action": "specific actionable instruction for security guard"
}}"""

    def analyze(self, event: dict, similar: list, recurrence: int) -> dict:
        """
        Main reasoning call.
        Returns structured risk assessment.
        Falls back to templates if Azure OpenAI unavailable.
        """
        risk_tier = event.get("risk_tier", "LOW")

        if not self._use_llm:
            out = dict(_FALLBACK_RESPONSES.get(risk_tier, _FALLBACK_RESPONSES["LOW"]))
            return self._apply_forced_risk_clamp(event, out)

        try:
            prompt   = self._build_prompt(event, similar, recurrence)
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": "You are a security analysis AI. Respond only with valid JSON."},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.3,
                max_tokens=512,
            )
            text = response.choices[0].message.content.strip()

            # Strip any accidental markdown fences
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()

            result = json.loads(text)
            # Ensure all keys exist
            for key in ["risk_level", "pattern_summary", "why_flagged",
                        "predicted_next", "recommended_action"]:
                result.setdefault(key, "Unknown")
            return self._apply_forced_risk_clamp(event, result)

        except Exception as e:
            print(f"  [Azure OpenAI fallback] {e}")
            out = dict(_FALLBACK_RESPONSES.get(risk_tier, _FALLBACK_RESPONSES["LOW"]))
            return self._apply_forced_risk_clamp(event, out)

    def format_alert_card(self, event: dict, reasoning: dict) -> str:
        """
        Returns a human-readable alert string for terminal/UI.
        """
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"  ALERT — Zone {event.get('zone_id')}",
            f"  Risk:    {reasoning['risk_level']}",
            f"  Pattern: {reasoning['pattern_summary']}",
            f"  Why:",
        ]
        for r in reasoning.get("why_flagged", []):
            lines.append(f"    · {r}")
        lines += [
            f"  Predict: {reasoning['predicted_next']}",
            f"  Action:  {reasoning['recommended_action']}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)
