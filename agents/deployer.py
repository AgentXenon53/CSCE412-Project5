# agents/deployer.py
import os
import json
import time
import pathlib
import datetime
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

DOMAIN      = os.getenv("DOMAIN_NAME", "csce412advaithr07.me")
REGION      = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
BUCKET_NAME = DOMAIN  # S3 bucket named after domain
SITE_DIR    = pathlib.Path(".")  # root of repo where HTML files live


# -- AWS clients ---------------------------------------------------------------
s3          = boto3.client("s3",            region_name=REGION)
cf          = boto3.client("cloudfront",    region_name=REGION)
acm         = boto3.client("acm",           region_name="us-east-1")  # ACM must be us-east-1
route53     = boto3.client("route53")


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def tool_create_s3_bucket() -> dict:
    """Create a private S3 bucket and block all public access."""
    print(f"[Deployer] Creating S3 bucket: {BUCKET_NAME}")
    try:
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=BUCKET_NAME)
        else:
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )

        # Block ALL public access
        s3.put_public_access_block(
            Bucket=BUCKET_NAME,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls":       True,
                "IgnorePublicAcls":      True,
                "BlockPublicPolicy":     True,
                "RestrictPublicBuckets": True,
            },
        )
        print(f"[Deployer] [OK] Bucket created and public access blocked.")
        return {"status": "ok", "bucket": BUCKET_NAME}

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"[Deployer] Bucket already exists, continuing.")
            return {"status": "ok", "bucket": BUCKET_NAME}
        return {"status": "error", "detail": str(e)}


def tool_upload_website_files() -> dict:
    """Upload all HTML files from repo root to the S3 bucket."""
    print(f"[Deployer] Uploading website files from {SITE_DIR.resolve()}")

    CONTENT_TYPES = {
        ".html": "text/html",
        ".css":  "text/css",
        ".js":   "application/javascript",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".ico":  "image/x-icon",
        ".svg":  "image/svg+xml",
    }

    uploaded = []
    errors   = []

    # Upload every file in repo root and any subdirectories except hidden/venv
    skip_dirs = {".git", ".venv", "venv", "__pycache__", "outputs", "agents"}
    for path in SITE_DIR.rglob("*"):
        if path.is_dir():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix not in CONTENT_TYPES:
            continue

        key          = str(path).replace("\\", "/")
        content_type = CONTENT_TYPES[path.suffix]

        try:
            s3.upload_file(
                str(path), BUCKET_NAME, key,
                ExtraArgs={"ContentType": content_type},
            )
            print(f"[Deployer]   [OK] {key}")
            uploaded.append(key)
        except ClientError as e:
            print(f"[Deployer]   [FAIL] {key}: {e}")
            errors.append({"file": key, "error": str(e)})

    return {"status": "ok" if not errors else "partial",
            "uploaded": uploaded, "errors": errors}


def tool_request_acm_certificate() -> dict:
    """Request a TLS certificate covering root domain and www subdomain."""
    print(f"[Deployer] Requesting ACM certificate for {DOMAIN} and www.{DOMAIN}")
    try:
        resp = acm.request_certificate(
            DomainName=DOMAIN,
            ValidationMethod="DNS",
            SubjectAlternativeNames=[f"www.{DOMAIN}"],
            IdempotencyToken="nexacloud01",
        )
        arn = resp["CertificateArn"]
        print(f"[Deployer] [OK] Certificate requested: {arn}")
        print(f"[Deployer] ⚠ You must add the DNS CNAME validation records in Route 53.")
        print(f"[Deployer]   Run: aws acm describe-certificate --certificate-arn {arn}")
        print(f"[Deployer]   Then add the CNAME records shown under DomainValidationOptions.")
        return {"status": "ok", "certificate_arn": arn}
    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def tool_wait_for_certificate(certificate_arn: str, timeout: int = 600) -> dict:
    """Poll ACM until the certificate reaches ISSUED status."""
    print(f"[Deployer] Waiting for certificate to be issued (timeout={timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp   = acm.describe_certificate(CertificateArn=certificate_arn)
        status = resp["Certificate"]["Status"]
        print(f"[Deployer]   Certificate status: {status}")
        if status == "ISSUED":
            print(f"[Deployer] [OK] Certificate issued.")
            return {"status": "ok", "certificate_status": status}
        if status == "FAILED":
            return {"status": "error", "detail": "Certificate validation failed."}
        time.sleep(30)
    return {"status": "error", "detail": "Timed out waiting for certificate."}


def tool_create_cloudfront_distribution(certificate_arn: str) -> dict:
    """Create a CloudFront distribution with OAC, HTTPS-only, and HTTP redirect."""
    print(f"[Deployer] Creating CloudFront distribution for {DOMAIN}")

    origin_id = f"S3-{BUCKET_NAME}"

    try:
        resp = cf.create_distribution(
            DistributionConfig={
                "CallerReference": f"nexacloud-{int(time.time())}",
                "Comment":         f"NexaCloud distribution for {DOMAIN}",
                "Enabled":         True,
                "DefaultRootObject": "index.html",

                "Origins": {
                    "Quantity": 1,
                    "Items": [{
                        "Id":         origin_id,
                        "DomainName": f"{BUCKET_NAME}.s3.{REGION}.amazonaws.com",
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                    }],
                },

                "DefaultCacheBehavior": {
                    "TargetOriginId":       origin_id,
                    "ViewerProtocolPolicy": "redirect-to-https",
                    "CachePolicyId":        "658327ea-f89d-4fab-a63d-7e88639e58f6",  # CachingOptimized
                    "Compress":             True,
                    "AllowedMethods": {
                        "Quantity": 2,
                        "Items":    ["GET", "HEAD"],
                        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                    },
                },

                "CustomErrorResponses": {
                    "Quantity": 1,
                    "Items": [{
                        "ErrorCode":          404,
                        "ResponsePagePath":   "/404.html",
                        "ResponseCode":       "404",
                        "ErrorCachingMinTTL": 10,
                    }],
                },

                "Aliases": {
                    "Quantity": 2,
                    "Items":    [DOMAIN, f"www.{DOMAIN}"],
                },

                "ViewerCertificate": {
                    "ACMCertificateArn":      certificate_arn,
                    "SSLSupportMethod":       "sni-only",
                    "MinimumProtocolVersion": "TLSv1.2_2021",
                },
            }
        )

        dist_id     = resp["Distribution"]["Id"]
        dist_domain = resp["Distribution"]["DomainName"]
        print(f"[Deployer] [OK] Distribution created: {dist_id}")
        print(f"[Deployer]   CloudFront domain: {dist_domain}")
        return {
            "status":            "ok",
            "distribution_id":   dist_id,
            "cloudfront_domain": dist_domain,
        }

    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def tool_attach_s3_oac_policy(distribution_id: str) -> dict:
    """
    Create an Origin Access Control and update the S3 bucket policy so only
    CloudFront can read from the bucket.
    """
    print(f"[Deployer] Creating Origin Access Control (OAC)...")
    try:
        # Create OAC
        oac_resp = cf.create_origin_access_control(
            OriginAccessControlConfig={
                "Name":                          f"OAC-{BUCKET_NAME}",
                "Description":                   f"OAC for {BUCKET_NAME}",
                "SigningProtocol":               "sigv4",
                "SigningBehavior":               "always",
                "OriginAccessControlOriginType": "s3",
            }
        )
        oac_id = oac_resp["OriginAccessControl"]["Id"]
        print(f"[Deployer] [OK] OAC created: {oac_id}")

        # Attach OAC to distribution origin
        dist      = cf.get_distribution_config(Id=distribution_id)
        dist_cfg  = dist["DistributionConfig"]
        etag      = dist["ETag"]
        dist_cfg["Origins"]["Items"][0]["OriginAccessControlId"] = oac_id
        dist_cfg["Origins"]["Items"][0]["S3OriginConfig"]        = {"OriginAccessIdentity": ""}

        cf.update_distribution(
            DistributionConfig=dist_cfg,
            Id=distribution_id,
            IfMatch=etag,
        )
        print(f"[Deployer] [OK] OAC attached to distribution.")

        # Fetch the distribution ARN for the bucket policy
        dist_info  = cf.get_distribution(Id=distribution_id)
        dist_arn   = dist_info["Distribution"]["ARN"]
        account_id = boto3.client("sts").get_caller_identity()["Account"]

        bucket_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid":       "AllowCloudFrontOAC",
                "Effect":    "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action":    "s3:GetObject",
                "Resource":  f"arn:aws:s3:::{BUCKET_NAME}/*",
                "Condition": {
                    "StringEquals": {
                        "AWS:SourceArn": dist_arn,
                    }
                },
            }],
        }

        s3.put_bucket_policy(
            Bucket=BUCKET_NAME,
            Policy=json.dumps(bucket_policy),
        )
        print(f"[Deployer] [OK] Bucket policy updated — only CloudFront OAC can read objects.")
        return {"status": "ok", "oac_id": oac_id}

    except ClientError as e:
        return {"status": "error", "detail": str(e)}


def tool_create_route53_alias(cloudfront_domain: str) -> dict:
    """Create Route 53 A-alias records pointing the domain at CloudFront."""
    print(f"[Deployer] Looking up Route 53 hosted zone for {DOMAIN}...")
    try:
        zones = route53.list_hosted_zones_by_name(DNSName=DOMAIN)
        zone  = next(
            (z for z in zones["HostedZones"] if z["Name"].rstrip(".") == DOMAIN),
            None,
        )
        if not zone:
            return {"status": "error", "detail": f"No hosted zone found for {DOMAIN}"}

        zone_id = zone["Id"].split("/")[-1]
        print(f"[Deployer] Found hosted zone: {zone_id}")

        changes = []
        for name in [DOMAIN, f"www.{DOMAIN}"]:
            changes.append({
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": name,
                    "Type": "A",
                    "AliasTarget": {
                        # CloudFront hosted zone ID is always Z2FDTNDATAQYW2
                        "HostedZoneId":         "Z2FDTNDATAQYW2",
                        "DNSName":              cloudfront_domain,
                        "EvaluateTargetHealth": False,
                    },
                },
            })

        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={"Comment": "NexaCloud alias records", "Changes": changes},
        )
        print(f"[Deployer] [OK] Alias records created for {DOMAIN} and www.{DOMAIN}")
        return {"status": "ok", "zone_id": zone_id}

    except ClientError as e:
        return {"status": "error", "detail": str(e)}

def tool_add_acm_validation_records(certificate_arn: str) -> dict:
    """Automatically add ACM DNS validation CNAME records to Route 53."""
    print(f"[Deployer] Fetching ACM validation records...")
    
    # Wait briefly for ACM to generate the CNAME records
    for attempt in range(10):
        resp  = acm.describe_certificate(CertificateArn=certificate_arn)
        opts  = resp["Certificate"].get("DomainValidationOptions", [])
        ready = [o for o in opts if "ResourceRecord" in o]
        if ready:
            break
        print(f"[Deployer]   Waiting for validation records to generate... ({attempt+1}/10)")
        time.sleep(10)
    else:
        return {"status": "error", "detail": "ACM did not generate validation records in time."}

    # Look up hosted zone
    zones = route53.list_hosted_zones_by_name(DNSName=DOMAIN)
    zone  = next(
        (z for z in zones["HostedZones"] if z["Name"].rstrip(".") == DOMAIN), None
    )
    if not zone:
        return {"status": "error", "detail": f"No hosted zone found for {DOMAIN}"}
    
    zone_id = zone["Id"].split("/")[-1]
    changes = []
    
    for opt in ready:
        rec = opt["ResourceRecord"]
        changes.append({
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": rec["Name"],
                "Type": rec["Type"],
                "TTL":  300,
                "ResourceRecords": [{"Value": rec["Value"]}],
            },
        })

    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Comment": "ACM validation records", "Changes": changes},
    )
    print(f"[Deployer] [OK] Validation CNAME records added to Route 53 automatically.")
    return {"status": "ok", "records_added": len(changes)}

# ══════════════════════════════════════════════════════════════════════════════
# SAVE RESULT
# ══════════════════════════════════════════════════════════════════════════════

def save_result(result: dict) -> pathlib.Path:
    out_dir  = pathlib.Path("outputs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "deploy_result.json"
    payload  = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent":     "deployer",
        "result":    result,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Deployer] Result saved -> {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUN
# ══════════════════════════════════════════════════════════════════════════════

def run() -> dict:
    """Entry point called by main.py. Returns the full deploy result dict."""
    print(f"\n{'='*55}")
    print("[Deployer] Starting deployment pipeline...")
    print(f"{'='*55}\n")

    result = {}

    # 1 — S3 bucket
    r = tool_create_s3_bucket()
    result["s3"] = r
    if r["status"] == "error":
        save_result(result)
        return result

    # 2 — Upload files
    r = tool_upload_website_files()
    result["upload"] = r

    # 3 — ACM certificate
    r = tool_request_acm_certificate()
    result["acm"] = r
    if r["status"] == "error":
        save_result(result)
        return result

    cert_arn = r["certificate_arn"]

    # 4
    r = tool_add_acm_validation_records(cert_arn)
    result["acm_validation"] = r
    if r["status"] == "error":
        save_result(result)
        return result

    r = tool_wait_for_certificate(cert_arn, timeout=600)
    result["acm_wait"] = r
    if r["status"] == "error":
        save_result(result)
        return result

    # 5 — CloudFront distribution
    r = tool_create_cloudfront_distribution(cert_arn)
    result["cloudfront"] = r
    if r["status"] == "error":
        save_result(result)
        return result

    dist_id      = r["distribution_id"]
    cf_domain    = r["cloudfront_domain"]

    # 6 — Attach OAC + bucket policy
    r = tool_attach_s3_oac_policy(dist_id)
    result["oac"] = r

    # 7 — Route 53 alias
    r = tool_create_route53_alias(cf_domain)
    result["route53"] = r

    # Summary
    result["summary"] = {
        "domain":           DOMAIN,
        "bucket":           BUCKET_NAME,
        "certificate_arn":  cert_arn,
        "distribution_id":  dist_id,
        "cloudfront_domain": cf_domain,
        "status":           "deployed",
    }

    print(f"\n[Deployer] [OK] Deployment complete.")
    print(f"[Deployer]   Site will be live at https://{DOMAIN} once DNS propagates.")

    save_result(result)
    return result


if __name__ == "__main__":
    run()