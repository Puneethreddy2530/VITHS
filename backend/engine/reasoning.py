"""
Phase 4 — reasoning.py
Gemini-powered causal reasoning layer.
  - Explainable alerts: WHY this event is flagged
  - Predictive timeline: WHEN the next event is likely
  - Action recommendation: WHAT the guard should do
  - Privacy-preserving: no images sent to Gemini, only metadata

Cite: Lewis et al. (2020) RAG · Gemini 1.5 Flash (free tier)
"""

import json, os
from datetime import datetime
from typing import Optional
import google.generativeai as genai

try:
    from dotenv import load_dotenv
    # Load from the physical .env file in the project root
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    load_dotenv(env_path)
except ImportError:
    pass

# ── Set your Gemini API key here ───────────────────────────────────
# Free tier: https://aistudio.google.com/app/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_KEY_HERE")
GEMINI_MODEL   = "gemini-1.5-flash"   # fast + free tier

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
        if GEMINI_API_KEY == "YOUR_KEY_HERE":
            print("  [WARN] Gemini API key not set — using fallback templates")
            self._use_gemini = False
        else:
            genai.configure(api_key=GEMINI_API_KEY)
            self._gemini     = genai.GenerativeModel(GEMINI_MODEL)
            self._use_gemini = True
            print("  Gemini reasoning engine ready.")

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
        Falls back to templates if Gemini unavailable.
        """
        risk_tier = event.get("risk_tier", "LOW")

        if not self._use_gemini:
            return _FALLBACK_RESPONSES.get(risk_tier, _FALLBACK_RESPONSES["LOW"])

        try:
            prompt   = self._build_prompt(event, similar, recurrence)
            response = self._gemini.generate_content(prompt)
            text     = response.text.strip()

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
            return result

        except Exception as e:
            print(f"  [Gemini fallback] {e}")
            return _FALLBACK_RESPONSES.get(risk_tier, _FALLBACK_RESPONSES["LOW"])

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
