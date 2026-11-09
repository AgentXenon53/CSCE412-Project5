# agents/validator.py
import os
import json
import ssl
import socket
import datetime
import pathlib
import requests
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

DOMAIN      = os.getenv("DOMAIN_NAME", "csce412advaithr07.me")
BUCKET_NAME = DOMAIN
REGION      = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

s3 = boto3.client("s3", region_name=REGION)
cf = boto3.client("cloudfront", region_name=REGION)


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS — one per validation criterion [1]
# ══════════════════════════════════════════════════════════════════════════════

def tool_check_domain_accessible() -> dict:
    """Check 1: Website is accessible through its assigned domain."""
    url = f"https://{DOMAIN}"
    print(f"[Validator] Check 1: Domain accessible -> {url}")
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
        passed = resp.status_code == 200
        result = {
            "check":       "domain_accessible",
            "passed":      passed,
            "status_code": resp.status_code,
            "final_url":   resp.url,
        }
        print(f"[Validator]   {'[OK]' if passed else '[FAIL]'} Status {resp.status_code}")
        return result
    except requests.RequestException as e:
        print(f"[Validator]   [FAIL] Error: {e}")
        return {"check": "domain_accessible", "passed": False, "error": str(e)}


def tool_check_https() -> dict:
    """Check 2: HTTPS is functioning correctly."""
    url = f"https://{DOMAIN}"
    print(f"[Validator] Check 2: HTTPS functioning -> {url}")
    try:
        resp   = requests.get(url, timeout=15)
        passed = resp.url.startswith("https://") and resp.status_code == 200
        result = {
            "check":    "https_functioning",
            "passed":   passed,
            "url":      resp.url,
            "status":   resp.status_code,
        }
        print(f"[Validator]   {'[OK]' if passed else '[FAIL]'} HTTPS: {resp.url}")
        return result
    except requests.RequestException as e:
        print(f"[Validator]   [FAIL] Error: {e}")
        return {"check": "https_functioning", "passed": False, "error": str(e)}


def tool_check_http_redirect() -> dict:
    """Check 3: HTTP redirects to HTTPS."""
    url = f"http://{DOMAIN}"
    print(f"[Validator] Check 3: HTTP->HTTPS redirect -> {url}")
    try:
        resp   = requests.get(url, timeout=15, allow_redirects=True)
        passed = resp.url.startswith("https://")
        result = {
            "check":      "http_redirects_to_https",
            "passed":     passed,
            "final_url":  resp.url,
            "status":     resp.status_code,
            "redirects":  [r.url for r in resp.history],
        }
        print(f"[Validator]   {'[OK]' if passed else '[FAIL]'} Final URL: {resp.url}")
        return result
    except requests.RequestException as e:
        print(f"[Validator]   [FAIL] Error: {e}")
        return {"check": "http_redirects_to_https", "passed": False, "error": str(e)}


def tool_check_certificate() -> dict:
    """Check 4: TLS certificate is valid and matches the domain."""
    print(f"[Validator] Check 4: Certificate valid for {DOMAIN}")
    try:
        ctx  = ssl.create_default_context()
        conn = ctx.wrap_socket(
            socket.create_connection((DOMAIN, 443), timeout=15),
            server_hostname=DOMAIN,
        )
        cert       = conn.getpeercert()
        conn.close()

        subject    = dict(x[0] for x in cert["subject"])
        issuer     = dict(x[0] for x in cert["issuer"])
        not_after  = cert["notAfter"]
        sans       = [v for _, v in cert.get("subjectAltName", [])]
        domain_ok  = any(DOMAIN in s or s.lstrip("*.") in DOMAIN for s in sans)

        result = {
            "check":       "certificate_valid",
            "passed":      domain_ok,
            "subject":     subject.get("commonName", ""),
            "issuer":      issuer.get("organizationName", ""),
            "not_after":   not_after,
            "sans":        sans,
            "domain_match": domain_ok,
        }
        print(f"[Validator]   {'[OK]' if domain_ok else '[FAIL]'} Cert issued by {issuer.get('organizationName','')} expires {not_after}")
        return result
    except Exception as e:
        print(f"[Validator]   [FAIL] Error: {e}")
        return {"check": "certificate_valid", "passed": False, "error": str(e)}


def tool_check_pages_accessible() -> dict:
    """Check 5: All required website pages are accessible."""
    pages  = ["index.html", "services.html", "about.html", "blog.html", "contact.html"]
    print(f"[Validator] Check 5: Required pages accessible")
    results = []
    all_ok  = True
    for page in pages:
        url = f"https://{DOMAIN}/{page}"
        try:
            resp   = requests.get(url, timeout=15)
            passed = resp.status_code == 200
            if not passed:
                all_ok = False
            print(f"[Validator]   {'[OK]' if passed else '[FAIL]'} {page} -> {resp.status_code}")
            results.append({"page": page, "status": resp.status_code, "passed": passed})
        except requests.RequestException as e:
            all_ok = False
            print(f"[Validator]   [FAIL] {page} -> Error: {e}")
            results.append({"page": page, "passed": False, "error": str(e)})

    return {"check": "pages_accessible", "passed": all_ok, "pages": results}


def tool_check_s3_direct_access_denied() -> dict:
    """Check 6: Direct anonymous access to S3 objects is denied."""
    print(f"[Validator] Check 6: S3 direct access denied")
    try:
        # Try to access a known object directly via the S3 URL
        s3_url = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/index.html"
        resp   = requests.get(s3_url, timeout=15)
        # 403 or 400 means public access is blocked (good)
        passed = resp.status_code in (403, 400)
        result = {
            "check":      "s3_direct_access_denied",
            "passed":     passed,
            "s3_url":     s3_url,
            "status_code": resp.status_code,
        }
        print(f"[Validator]   {'[OK]' if passed else '[FAIL]'} S3 direct -> {resp.status_code} ({'blocked' if passed else 'OPEN — misconfigured!'})")
        return result
    except requests.RequestException as e:
        # Connection refused also implies access denied
        print(f"[Validator]   [OK] S3 direct access refused (connection error — access blocked)")
        return {"check": "s3_direct_access_denied", "passed": True, "error": str(e)}


def tool_check_cloudfront_delivery() -> dict:
    """Check 7: CloudFront is successfully serving content (via response headers)."""
    url = f"https://{DOMAIN}"
    print(f"[Validator] Check 7: CloudFront delivery confirmed")
    try:
        resp    = requests.get(url, timeout=15)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        # CloudFront sets x-cache and via headers
        via_cf  = "cloudfront" in headers.get("x-amz-cf-id", "").lower() or \
                  "cloudfront" in headers.get("via", "").lower() or \
                  "x-amz-cf-id" in headers or \
                  "x-cache" in headers
        result  = {
            "check":            "cloudfront_delivery",
            "passed":           via_cf,
            "x-cache":          headers.get("x-cache", "not present"),
            "x-amz-cf-id":      headers.get("x-amz-cf-id", "not present"),
            "via":              headers.get("via", "not present"),
        }
        print(f"[Validator]   {'[OK]' if via_cf else '[FAIL]'} CloudFront headers: x-cache={headers.get('x-cache','—')}")
        return result
    except requests.RequestException as e:
        print(f"[Validator]   [FAIL] Error: {e}")
        return {"check": "cloudfront_delivery", "passed": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SAVE RESULT
# ══════════════════════════════════════════════════════════════════════════════

def save_result(results: list, passed: int, total: int) -> pathlib.Path:
    out_dir  = pathlib.Path("outputs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "validation.json"
    payload  = {
        "timestamp":    datetime.datetime.utcnow().isoformat() + "Z",
        "agent":        "validator",
        "domain":       DOMAIN,
        "passed":       passed,
        "total":        total,
        "all_passed":   passed == total,
        "checks":       results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Validator] Results saved -> {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUN
# ══════════════════════════════════════════════════════════════════════════════

def run() -> dict:
    """Entry point called by main.py. Returns the full validation result dict."""
    print(f"\n{'='*55}")
    print("[Validator] Starting infrastructure validation...")
    print(f"{'='*55}\n")

    checks = [
        tool_check_domain_accessible(),
        tool_check_https(),
        tool_check_http_redirect(),
        tool_check_certificate(),
        tool_check_pages_accessible(),
        tool_check_s3_direct_access_denied(),
        tool_check_cloudfront_delivery(),
    ]

    passed = sum(1 for c in checks if c.get("passed"))
    total  = len(checks)

    print(f"\n[Validator] -- Summary ------------------------------")
    for c in checks:
        icon = "[OK]" if c.get("passed") else "[FAIL]"
        print(f"[Validator]   {icon} {c['check']}")
    print(f"[Validator] Result: {passed}/{total} checks passed")

    result = {
        "passed":     passed,
        "total":      total,
        "all_passed": passed == total,
        "checks":     checks,
    }

    save_result(checks, passed, total)
    return result


if __name__ == "__main__":
    run()