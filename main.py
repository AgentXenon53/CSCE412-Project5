# main.py
import json
import pathlib
import datetime
import sys

from agents import analyst, deployer, validator, diagnostician


LOG_PATH = pathlib.Path("outputs/run_log.json")


def save_log(log: dict):
    pathlib.Path("outputs").mkdir(exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    print(f"\n[Orchestrator] Full run log saved → {LOG_PATH}")


def banner(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


def run():
    log = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stages":     {},
    }

    banner("NexaCloud Multi-Agent Deployment System")
    print("[Orchestrator] Starting pipeline...\n")

    # ── STAGE 1: ANALYST ──────────────────────────────────────────────────────
    banner("Stage 1 — Analyst Agent")
    try:
        plan = analyst.run()
        log["stages"]["analyst"] = {"status": "completed", "output": plan}
    except Exception as e:
        print(f"[Orchestrator] ✗ Analyst failed: {e}")
        log["stages"]["analyst"] = {"status": "failed", "error": str(e)}
        save_log(log)
        sys.exit(1)

    # ── STAGE 2: DEPLOYER ─────────────────────────────────────────────────────
    banner("Stage 2 — Deployer Agent")
    print("[Orchestrator] Handing plan to Deployer...\n")

    # Check if a prior deployment result exists and offer to skip
    deploy_result_path = pathlib.Path("outputs/deploy_result.json")
    if deploy_result_path.exists():
        skip = input(
            "[Orchestrator] Existing deploy_result.json found. "
            "Skip re-deployment? [y/N]: "
        ).strip().lower()
        if skip == "y":
            with open(deploy_result_path) as f:
                deploy_result = json.load(f).get("result", {})
            print("[Orchestrator] Skipping deployer — using existing result.")
            log["stages"]["deployer"] = {"status": "skipped", "output": deploy_result}
        else:
            deploy_result = _run_deployer(log)
    else:
        deploy_result = _run_deployer(log)

    if log["stages"]["deployer"]["status"] == "failed":
        save_log(log)
        sys.exit(1)

    # ── STAGE 3: VALIDATOR ────────────────────────────────────────────────────
    banner("Stage 3 — Validator Agent")
    print("[Orchestrator] Running infrastructure validation...\n")
    try:
        val_result = validator.run()
        log["stages"]["validator"] = {"status": "completed", "output": val_result}
        print(f"\n[Orchestrator] Validation: {val_result['passed']}/{val_result['total']} checks passed.")
    except Exception as e:
        print(f"[Orchestrator] ✗ Validator failed: {e}")
        log["stages"]["validator"] = {"status": "failed", "error": str(e)}
        save_log(log)
        sys.exit(1)

    # ── STAGE 4: DIAGNOSTICIAN (only if failures exist) ───────────────────────
    if not val_result.get("all_passed"):
        banner("Stage 4 — Diagnostician Agent")
        print(f"[Orchestrator] {val_result['total'] - val_result['passed']} check(s) failed — invoking Diagnostician...\n")
        try:
            # Load full validation.json for the diagnostician
            with open("outputs/validation.json") as f:
                full_validation = json.load(f)

            diag_result = diagnostician.run(full_validation)
            log["stages"]["diagnostician"] = {"status": "completed", "output": diag_result}

            # Re-run validator after fixes
            print("\n[Orchestrator] Re-running validation after fixes...")
            val_result2   = validator.run()
            log["stages"]["revalidation"] = {
                "status": "completed",
                "output": val_result2,
            }
            print(f"[Orchestrator] Re-validation: {val_result2['passed']}/{val_result2['total']} checks passed.")

        except Exception as e:
            print(f"[Orchestrator] ✗ Diagnostician failed: {e}")
            log["stages"]["diagnostician"] = {"status": "failed", "error": str(e)}
    else:
        banner("Stage 4 — Diagnostician Agent")
        print("[Orchestrator] All validation checks passed — Diagnostician not needed.")
        log["stages"]["diagnostician"] = {"status": "skipped", "reason": "all checks passed"}

    # ── FINAL SUMMARY ─────────────────────────────────────────────────────────
    log["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_log(log)

    banner("Pipeline Complete")
    _print_summary(log)


def _run_deployer(log: dict) -> dict:
    """Helper to run deployer and record result."""
    try:
        result = deployer.run()
        log["stages"]["deployer"] = {"status": "completed", "output": result}
        return result
    except Exception as e:
        print(f"[Orchestrator] ✗ Deployer failed: {e}")
        log["stages"]["deployer"] = {"status": "failed", "error": str(e)}
        return {}


def _print_summary(log: dict):
    print("\n[Orchestrator] Stage summary:")
    for stage, data in log["stages"].items():
        status = data.get("status", "unknown")
        icon   = {"completed": "✓", "skipped": "—", "failed": "✗"}.get(status, "?")
        print(f"  {icon}  {stage:<20} {status}")

    # Final validation outcome
    reval  = log["stages"].get("revalidation")
    val    = log["stages"].get("validator")
    final  = reval or val
    if final and final.get("status") == "completed":
        out = final["output"]
        print(f"\n[Orchestrator] Final validation: {out.get('passed')}/{out.get('total')} checks passed.")
        if out.get("all_passed"):
            print("[Orchestrator] ✓ Deployment fully verified.")
        else:
            print("[Orchestrator] ⚠ Some checks still failing — review outputs/diagnosis.json")

    print(f"\n[Orchestrator] Logs: outputs/run_log.json")


if __name__ == "__main__":
    run()