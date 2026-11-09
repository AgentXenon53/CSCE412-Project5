# agents/diagnostician.py
import os
import json
import datetime
import pathlib
import boto3
from botocore.exceptions import ClientError
from google import genai
from dotenv import load_dotenv

load_dotenv()

DOMAIN      = os.getenv("DOMAIN_NAME", "csce412advaithr07.me")
BUCKET_NAME = DOMAIN
REGION      = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
GEMINI_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
s3      = boto3.client("s3",         region_name=REGION)
cf      = boto3.client("cloudfront", region_name=REGION)
route53 = boto3.client("route53")

SYSTEM = """
You are a Cloud Infrastructure Diagnostician specializing in AWS static website deployments.
You are given a JSON validation report showing which checks passed and which failed.
You must respond ONLY with a valid JSON object with exactly these fields:
- summary:     (string) one sentence describing the overall state
- failures:    (list[object]) each with:
    - check:        (string) the check name that failed
    - root_cause:   (string) most likely reason for the failure
    - fix_action:   (string) exact corrective action to take
    - fix_tool:     (string) which tool to call: one of:
                    fix_s3_bucket_policy | fix_cloudfront_error_page |
                    fix_route53_alias | fix_cloudfront_https_redirect |
                    fix_s3_public_access_block | no_automated_fix
    - risk:         (string) low | medium | high — risk level of applying this fix
- recommendation: (string) overall next step
Do not include any text outside the JSON object.
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# LLM DIAGNOSIS
# ══════════════════════════════════════════════════════════════════════════════

def diagnose(validation_result: dict, retries: int = 3) -> dict:
    """Send validation results to the LLM and get a structured diagnosis."""
    prompt = (
        f"The following validation checks were run against the deployment "
        f"for domain {DOMAIN}.\n\n"
        f"{json.dumps(validation_result, indent=2)}\n\n"
        f"Diagnose the failures and propose corrective actions."
    )

    for attempt in range(1, retries + 1):
        print(f"[Diagnostician] Attempt {attempt}: sending to LLM...")
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{SYSTEM}\n\n{prompt}",
        )
        raw = response.text.strip()

        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[Diagnostician] JSON parse error: {e}")
            continue

        for field in ("summary", "failures", "recommendation"):
            if field not in data:
                print(f"[Diagnostician] Missing field: {field}")
                break
        else:
            print("[Diagnostician] Valid diagnosis received.")
            return data

    raise ValueError("[Diagnostician] Failed to produce valid diagnosis after retries.")


# ══════════════════════════════════════════════════════════════════════════════
# FIX TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def _get_distribution_id() -> str | None:
    """Find the CloudFront distribution ID for our domain."""
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for dist in page.get("DistributionList", {}).get("Items", []):
            aliases = dist.get("Aliases", {}).get("Items", [])
            if DOMAIN in aliases or f"www.{DOMAIN}" in aliases:
                return dist["Id"]
    return None


def fix_s3_bucket_policy() -> dict:
    """Re-apply the OAC bucket policy so only CloudFront can read the bucket."""
    print("[Diagnostician] fix_s3_bucket_policy: re-applying CloudFront OAC policy...")
    try:
        dist_id = _get_distribution_id()
        if not dist_id:
            return {"status": "error", "detail": "Could not find CloudFront distribution."}

        dist_info = cf.get_distribution(Id=dist_id)
        dist_arn  = dist_info["Distribution"]["ARN"]

        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid":       "AllowCloudFrontOAC",
                "Effect":    "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action":    "s3:GetObject",
                "Resource":  f"arn:aws:s3:::{BUCKET_NAME}/*",
                "Condition": {
                    "StringEquals": {"AWS:SourceArn": dist_arn}
                },
            }],
        }
        s3.put_bucket_policy(Bucket=BUCKET_NAME, Policy=json.dumps(policy))
        print("[Diagnostician] [OK] Bucket policy restored.")
        return {"status": "ok", "action": "s3_bucket_policy_restored"}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def fix_s3_public_access_block() -> dict:
    """Re-enable all S3 public access blocks on the bucket."""
    print("[Diagnostician] fix_s3_public_access_block: re-blocking public access...")
    try:
        s3.put_public_access_block(
            Bucket=BUCKET_NAME,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls":       True,
                "IgnorePublicAcls":      True,
                "BlockPublicPolicy":     True,
                "RestrictPublicBuckets": True,
            },
        )
        print("[Diagnostician] [OK] Public access block restored.")
        return {"status": "ok", "action": "s3_public_access_block_restored"}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def fix_cloudfront_error_page() -> dict:
    """Ensure the CloudFront distribution has the 404->/404.html custom error page."""
    print("[Diagnostician] fix_cloudfront_error_page: updating custom error response...")
    try:
        dist_id  = _get_distribution_id()
        if not dist_id:
            return {"status": "error", "detail": "Could not find CloudFront distribution."}

        dist     = cf.get_distribution_config(Id=dist_id)
        cfg      = dist["DistributionConfig"]
        etag     = dist["ETag"]

        cfg["CustomErrorResponses"] = {
            "Quantity": 1,
            "Items": [{
                "ErrorCode":          404,
                "ResponsePagePath":   "/404.html",
                "ResponseCode":       "404",
                "ErrorCachingMinTTL": 10,
            }],
        }
        cf.update_distribution(DistributionConfig=cfg, Id=dist_id, IfMatch=etag)
        print("[Diagnostician] [OK] CloudFront 404 error page restored.")
        return {"status": "ok", "action": "cloudfront_error_page_restored"}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def fix_cloudfront_https_redirect() -> dict:
    """Set the default cache behavior to redirect HTTP->HTTPS."""
    print("[Diagnostician] fix_cloudfront_https_redirect: enforcing redirect-to-https...")
    try:
        dist_id  = _get_distribution_id()
        if not dist_id:
            return {"status": "error", "detail": "Could not find CloudFront distribution."}

        dist     = cf.get_distribution_config(Id=dist_id)
        cfg      = dist["DistributionConfig"]
        etag     = dist["ETag"]

        cfg["DefaultCacheBehavior"]["ViewerProtocolPolicy"] = "redirect-to-https"
        cf.update_distribution(DistributionConfig=cfg, Id=dist_id, IfMatch=etag)
        print("[Diagnostician] [OK] HTTPS redirect policy restored.")
        return {"status": "ok", "action": "cloudfront_https_redirect_restored"}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def fix_route53_alias(cloudfront_domain: str = None) -> dict:
    """Re-create the Route 53 A-alias records pointing to CloudFront."""
    print("[Diagnostician] fix_route53_alias: restoring DNS alias records...")
    try:
        if not cloudfront_domain:
            dist_id = _get_distribution_id()
            if not dist_id:
                return {"status": "error", "detail": "Could not find CloudFront distribution."}
            dist_info        = cf.get_distribution(Id=dist_id)
            cloudfront_domain = dist_info["Distribution"]["DomainName"]

        zones   = route53.list_hosted_zones_by_name(DNSName=DOMAIN)
        zone    = next(
            (z for z in zones["HostedZones"] if z["Name"].rstrip(".") == DOMAIN), None
        )
        if not zone:
            return {"status": "error", "detail": f"No hosted zone for {DOMAIN}"}

        zone_id = zone["Id"].split("/")[-1]
        changes = []
        for name in [DOMAIN, f"www.{DOMAIN}"]:
            changes.append({
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": name,
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId":         "Z2FDTNDATAQYW2",
                        "DNSName":              cloudfront_domain,
                        "EvaluateTargetHealth": False,
                    },
                },
            })
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={"Comment": "NexaCloud DNS fix", "Changes": changes},
        )
        print("[Diagnostician] [OK] Route 53 alias records restored.")
        return {"status": "ok", "action": "route53_alias_restored"}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


# Map tool names from LLM to actual functions
FIX_TOOLS = {
    "fix_s3_bucket_policy":          fix_s3_bucket_policy,
    "fix_s3_public_access_block":    fix_s3_public_access_block,
    "fix_cloudfront_error_page":     fix_cloudfront_error_page,
    "fix_cloudfront_https_redirect": fix_cloudfront_https_redirect,
    "fix_route53_alias":             fix_route53_alias,
}


# ══════════════════════════════════════════════════════════════════════════════
# HUMAN APPROVAL GATE
# ══════════════════════════════════════════════════════════════════════════════

def request_human_approval(failure: dict) -> bool:
    """Print the proposed fix and ask the human to approve or skip. [1]"""
    print(f"\n[Diagnostician] -- Approval Required -------------------")
    print(f"  Check failed : {failure['check']}")
    print(f"  Root cause   : {failure['root_cause']}")
    print(f"  Proposed fix : {failure['fix_action']}")
    print(f"  Tool         : {failure['fix_tool']}")
    print(f"  Risk level   : {failure['risk'].upper()}")
    answer = input("\n  Apply this fix? [y/N]: ").strip().lower()
    return answer == "y"


# ══════════════════════════════════════════════════════════════════════════════
# SAVE RESULT
# ══════════════════════════════════════════════════════════════════════════════

def save_result(diagnosis: dict, fix_log: list) -> pathlib.Path:
    out_dir  = pathlib.Path("outputs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "diagnosis.json"
    payload  = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent":     "diagnostician",
        "diagnosis": diagnosis,
        "fixes":     fix_log,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Diagnostician] Results saved -> {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUN
# ══════════════════════════════════════════════════════════════════════════════

def run(validation_result: dict = None) -> dict:
    """Entry point called by main.py. Accepts validator output directly."""
    print(f"\n{'='*55}")
    print("[Diagnostician] Starting failure diagnosis...")
    print(f"{'='*55}\n")

    # Load from file if not passed directly
    if validation_result is None:
        val_path = pathlib.Path("outputs/validation.json")
        if not val_path.exists():
            raise FileNotFoundError("[Diagnostician] outputs/validation.json not found.")
        with open(val_path) as f:
            validation_result = json.load(f)

    # If everything passed, nothing to do
    if validation_result.get("all_passed"):
        print("[Diagnostician] All checks passed — no failures to diagnose.")
        result = {"summary": "All checks passed.", "failures": [], "recommendation": "No action needed."}
        save_result(result, [])
        return result

    # LLM diagnosis
    diagnosis = diagnose(validation_result)
    print(f"\n[Diagnostician] Summary: {diagnosis['summary']}")
    print(f"[Diagnostician] Recommendation: {diagnosis['recommendation']}")

    # Apply fixes with human approval gate [1]
    fix_log = []
    for failure in diagnosis.get("failures", []):
        tool_name = failure.get("fix_tool", "no_automated_fix")

        if tool_name == "no_automated_fix":
            print(f"\n[Diagnostician] ⚠ No automated fix for: {failure['check']}")
            print(f"  Manual action required: {failure['fix_action']}")
            fix_log.append({"check": failure["check"], "tool": tool_name, "status": "manual_required"})
            continue

        approved = request_human_approval(failure)

        if not approved:
            print(f"[Diagnostician] Skipped fix for: {failure['check']}")
            fix_log.append({"check": failure["check"], "tool": tool_name, "status": "skipped"})
            continue

        # Execute the approved tool
        tool_fn = FIX_TOOLS.get(tool_name)
        if tool_fn:
            fix_result = tool_fn()
            fix_log.append({
                "check":  failure["check"],
                "tool":   tool_name,
                "result": fix_result,
            })
        else:
            print(f"[Diagnostician] Unknown tool: {tool_name}")
            fix_log.append({"check": failure["check"], "tool": tool_name, "status": "unknown_tool"})

    save_result(diagnosis, fix_log)
    return {"diagnosis": diagnosis, "fixes": fix_log}


if __name__ == "__main__":
    run()