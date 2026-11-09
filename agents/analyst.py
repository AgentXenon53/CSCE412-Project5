import os
import json
import datetime
import pathlib
from google import genai
from dotenv import load_dotenv

load_dotenv()

DOMAIN         = os.getenv("DOMAIN_NAME", "csce412advaithr07.me")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
gemini_client  = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
DOMAIN = os.getenv("DOMAIN_NAME", "csce412advaithr07.me")

SYSTEM = """
You are a Cloud Requirements Analyst specializing in AWS static website deployments.
You only work with these approved services: S3, CloudFront, Route 53, ACM.

Given a deployment request, respond ONLY with a valid JSON object with exactly these fields:
- summary:              (string)       one-sentence project summary
- requirements:         (list[string]) distinct technical requirements
- service_dependencies: (list[string]) how each AWS service is used
- risks:                (list[string]) potential failure points
- tasks:                (list[object]) ordered deployment tasks, each with:
    - id:          (string) short snake_case identifier
    - agent:       (string) which agent handles it: deployer | validator | diagnostician
    - action:      (string) what must be done
    - depends_on:  (list[string]) task ids that must complete first (empty list if none)
- questions:            (list[string]) clarifying questions if any

Do not include any text outside the JSON object.
""".strip()

REQUIRED_FIELDS = {
    "summary", "requirements", "service_dependencies",
    "risks", "tasks", "questions"
}


def analyze(request: str, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        print(f"[Analyst] Attempt {attempt}: analysing request...")
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{SYSTEM}\n\n{request}",
            )
            raw = response.text.strip()
        except Exception as e:
            print(f"[Analyst] API error: {e}")
            continue

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[Analyst] JSON parse error: {e}")
            continue

        missing = REQUIRED_FIELDS - data.keys()
        if missing:
            print(f"[Analyst] Missing fields: {missing}")
            continue

        print("[Analyst] Valid analysis received.")
        return data

    raise ValueError("[Analyst] Failed to produce valid output after retries.")


def save_plan(data: dict) -> pathlib.Path:
    """Tool: write the analysis to outputs/plan.json."""
    out_dir = pathlib.Path("outputs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "plan.json"

    payload = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent":     "analyst",
        "analysis":  data,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"[Analyst] Plan saved -> {out_path}")
    return out_path


def run() -> dict:
    """Entry point called by main.py."""
    request = (
        f"Deploy a secure five-page static website for the domain {DOMAIN}. "
        "Requirements: private S3 origin, CloudFront CDN delivery, valid ACM TLS "
        "certificate, HTTP-to-HTTPS redirect, Route 53 DNS alias record, "
        "and a custom 404 error page. No anonymous S3 access permitted."
    )

    print(f"\n{'='*55}")
    print("[Analyst] Starting requirements analysis...")
    print(f"{'='*55}")
    print(f"[Analyst] Request: {request}\n")

    result = analyze(request)

    print("\n[Analyst] Analysis:")
    print(json.dumps(result, indent=2))

    save_plan(result)
    return result


if __name__ == "__main__":
    run()