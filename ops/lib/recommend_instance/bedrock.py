"""Self-service Amazon Bedrock model enrollment for `platformctl new-model
--source bedrock`.

Unlike the GPU serving path there is nothing to size or schedule: a Bedrock model
runs in AWS. This module

  1. lists what's actually invokable in the deploy region from the LIVE Bedrock
     control-plane APIs (ListFoundationModels + ListInferenceProfiles) — so it is
     correct in any partition (the AWS European Sovereign Cloud included) with no
     hardcoded catalog to drift;
  2. resolves the region/partition-correct invocation id — a cross-region system
     inference profile (e.g. us.amazon.nova-lite-v1:0) where one exists, else the
     bare foundation-model id;
  3. looks up per-token pricing from the AWS Price List API (best-effort — the
     Price List API is commercial-only, so ESC/other partitions fall back to the
     --input-cost/--output-cost overrides or LiteLLM's built-in map);
  4. emits a BedrockModel CR under workloads/models/ that litellm-sync registers
     in LiteLLM — reusing the exact same git-push -> ArgoCD -> registration path
     as the serving tiers.

Nothing is offered by default; users enroll exactly the models they want.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from .paths import MODELS_ROOT, is_valid_model_name
from .pricing import detect_region
from .ux import _palette, _should_use_colour

# Bedrock models are namespaced to `inference` (like the platform-default vLLM
# placement); litellm-sync watches cluster-wide so the namespace is only about
# where the workloads ApplicationSet syncs the file.
BEDROCK_DIR = f"{MODELS_ROOT}/inference"

# A Bedrock invocation id: foundation-model id or inference-profile id, e.g.
# amazon.nova-lite-v1:0 or us.anthropic.claude-3-5-sonnet-20240620-v1:0. Charset
# is letters/digits plus . : - (NO quotes/spaces/newlines) so a resolved value is
# safe to interpolate into the YAML manifest committed + applied by ArgoCD.
_INVOKE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:\-]*\Z")


# --------------------------------------------------------------------------- #
# Partition / endpoint helpers (mirror terraform/30.eks/30.cluster/bedrock.tf)  #
# --------------------------------------------------------------------------- #

def partition_for(region: str) -> str:
    if region.startswith("eusc-"):
        return "aws-eusc"          # AWS European Sovereign Cloud
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def dns_suffix_for(partition: str) -> str:
    return {
        "aws": "amazonaws.com",
        "aws-eusc": "amazonaws.eu",
        "aws-cn": "amazonaws.com.cn",
        "aws-us-gov": "amazonaws.com",
    }.get(partition, "amazonaws.com")


def runtime_endpoint(region: str, partition: str) -> str:
    return f"https://bedrock-runtime.{region}.{dns_suffix_for(partition)}"


def _region_geo(region: str) -> str:
    """The cross-region inference-profile geography prefix for a region."""
    if region.startswith(("eu-", "eusc-")):
        return "eu"
    if region.startswith("ap-"):
        return "apac"
    return "us"   # us-*, ca-*, sa-*, us-gov-*


# --------------------------------------------------------------------------- #
# Live Bedrock catalog                                                         #
# --------------------------------------------------------------------------- #

def _client(service: str, region: str):
    """Return a boto3 client, or None if boto3/credentials are unavailable."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    try:
        return boto3.client(service, region_name=region)
    except Exception:
        return None


def list_text_models(region: str) -> list[dict]:
    """Text-output foundation models invokable in `region` (on-demand or via an
    inference profile), newest-listed first. Raises RuntimeError on API failure."""
    cli = _client("bedrock", region)
    if cli is None:
        raise RuntimeError("boto3 or AWS credentials unavailable — cannot list Bedrock models.")
    try:
        resp = cli.list_foundation_models(byOutputModality="TEXT")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"bedrock:ListFoundationModels failed in {region}: {e}") from e
    out: list[dict] = []
    for m in resp.get("modelSummaries", []) or []:
        # Only models we can actually invoke: on-demand or via an inference
        # profile. Skip PROVISIONED-only and non-ACTIVE lifecycle models.
        types = m.get("inferenceTypesSupported") or []
        if types and not ({"ON_DEMAND", "INFERENCE_PROFILE"} & set(types)):
            continue
        if (m.get("modelLifecycle") or {}).get("status") not in (None, "ACTIVE"):
            continue
        out.append(m)
    return out


def system_profiles_by_model(region: str) -> dict[str, list[str]]:
    """Map foundation-model id -> [system inference-profile ids] usable from
    `region`. Empty when none exist (e.g. single-region / sovereign partitions)."""
    cli = _client("bedrock", region)
    if cli is None:
        return {}
    mapping: dict[str, list[str]] = {}
    try:
        token = None
        for _ in range(20):  # bounded pagination
            kwargs = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = cli.list_inference_profiles(**kwargs)
            for p in resp.get("inferenceProfileSummaries", []) or []:
                pid = p.get("inferenceProfileId")
                if not pid or (p.get("status") not in (None, "ACTIVE")):
                    continue
                for mdl in p.get("models", []) or []:
                    arn = mdl.get("modelArn", "")
                    fm_id = arn.split("foundation-model/", 1)[-1] if "foundation-model/" in arn else ""
                    if fm_id:
                        mapping.setdefault(fm_id, []).append(pid)
            token = resp.get("nextToken")
            if not token:
                break
    except Exception:  # noqa: BLE001 — profiles are an optimization; degrade to bare ids
        return mapping
    return mapping


def _best_profile(region: str, profile_ids: list[str]) -> str | None:
    """Prefer the profile whose geography matches the region, then a global.
    profile, then any."""
    if not profile_ids:
        return None
    geo = _region_geo(region)
    for pid in profile_ids:
        if pid.startswith(f"{geo}."):
            return pid
    for pid in profile_ids:
        if pid.startswith("global."):
            return pid
    return profile_ids[0]


def default_alias(fm_id: str) -> str:
    """Kebab alias for a foundation-model id, e.g. amazon.nova-lite-v1:0 ->
    nova-lite, anthropic.claude-3-5-sonnet-20240620-v1:0 -> claude-3-5-sonnet."""
    tail = fm_id.split(".", 1)[-1] if "." in fm_id else fm_id  # drop provider
    tail = re.sub(r"[-_.]v\d+.*$", "", tail)                    # drop -v1:0 / -20240620-v1:0
    tail = tail.replace(":", "-").replace(".", "-").replace("_", "-").lower()
    tail = re.sub(r"-+", "-", tail).strip("-")
    return tail


def resolve(region: str, query: str, fms: list[dict],
            profiles: dict[str, list[str]]) -> tuple[dict, str, bool]:
    """Resolve a user query to (foundation_model, invocation_id, used_profile).

    Accepts a foundation-model id, an inference-profile id (with geo prefix), a
    bare model name, or a substring. Raises ValueError with guidance on no/ambiguous
    match."""
    q = query.strip().lower()
    # If the query is itself a geo-prefixed profile id, reduce to its base fm id.
    base_q = re.sub(r"^(us|eu|apac|global)\.", "", q)

    def _matches(fm: dict) -> bool:
        mid = (fm.get("modelId") or "").lower()
        mname = (fm.get("modelName") or "").lower()
        return q in (mid, mname) or base_q == mid or q in mid or base_q in mid \
            or q in mname or default_alias(fm.get("modelId") or "") == q

    exact = [fm for fm in fms if (fm.get("modelId") or "").lower() in (q, base_q)]
    cands = exact or [fm for fm in fms if _matches(fm)]
    if not cands:
        raise ValueError(
            f"no Bedrock model matches '{query}' in {region}. "
            f"Run:  ./platformctl new-model --source bedrock --list-available-models")
    if len(cands) > 1:
        ids = ", ".join(sorted((c.get("modelId") or "") for c in cands)[:8])
        raise ValueError(
            f"'{query}' is ambiguous in {region} — matches: {ids}. "
            f"Re-run with a more specific model id.")
    fm = cands[0]
    fm_id = fm.get("modelId") or ""
    pid = _best_profile(region, profiles.get(fm_id, []))
    return (fm, pid or fm_id, pid is not None)


# --------------------------------------------------------------------------- #
# Pricing (AWS Price List API — best-effort)                                   #
# --------------------------------------------------------------------------- #

def token_prices(region: str, fm: dict) -> tuple[float | None, float | None]:
    """Best-effort (input, output) USD-per-token from the AWS Price List API.

    Returns (None, None) on any uncertainty — the Price List API is commercial
    partition only, its Bedrock schema varies by model family, and we never want
    a pricing miss to block enrollment. The --input-cost/--output-cost flags
    override this."""
    cli = _client("pricing", "us-east-1")  # Price List API: us-east-1 / ap-south-1 / eu-central-1
    if cli is None:
        return (None, None)
    fm_id = fm.get("modelId") or ""
    model_token = default_alias(fm_id).replace("-", "")   # e.g. novalite
    if not model_token:
        return (None, None)
    in_price = out_price = None
    try:
        token = None
        for _ in range(6):  # bounded pagination
            kwargs = dict(
                ServiceCode="AmazonBedrock",
                Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}],
                MaxResults=100,
            )
            if token:
                kwargs["NextToken"] = token
            resp = cli.get_products(**kwargs)
            for raw in resp.get("PriceList", []):
                data = raw if isinstance(raw, dict) else json.loads(raw)
                attrs = (data.get("product") or {}).get("attributes") or {}
                blob = json.dumps(attrs).lower()
                if model_token not in blob.replace("-", "").replace(" ", "").replace(".", ""):
                    continue
                for term in (data.get("terms") or {}).get("OnDemand", {}).values():
                    for dim in (term.get("priceDimensions") or {}).values():
                        desc = (dim.get("description") or "").lower()
                        unit = (dim.get("unit") or "").lower()
                        if "token" not in (desc + unit):
                            continue
                        usd = (dim.get("pricePerUnit") or {}).get("USD")
                        if usd is None:
                            continue
                        per_unit = float(usd)
                        if per_unit <= 0:
                            continue
                        # Bedrock token dims are typically per 1,000 tokens.
                        divisor = 1_000_000.0 if ("1m" in unit or "million" in desc) else 1_000.0
                        per_token = per_unit / divisor
                        if "input" in desc and in_price is None:
                            in_price = per_token
                        elif "output" in desc and out_price is None:
                            out_price = per_token
            token = resp.get("NextToken")
            if not token or (in_price is not None and out_price is not None):
                break
    except Exception:  # noqa: BLE001 — best-effort only
        return (None, None)
    return (in_price, out_price)


# --------------------------------------------------------------------------- #
# CR emission                                                                  #
# --------------------------------------------------------------------------- #

def _fmt_cost(v: float) -> str:
    # Per-token costs are tiny; render without scientific notation and trim zeros.
    return f"{v:.12f}".rstrip("0").rstrip(".") or "0"


def build_yaml(alias: str, invocation_id: str, partition: str, region: str,
               in_cost: float | None, out_cost: float | None) -> tuple[str, str, str, str]:
    """Build the BedrockModel manifest. Returns (name, yaml_path, yaml_body, commit_msg)."""
    if not is_valid_model_name(alias):
        sys.exit(f"error: '{alias}' is not a valid Kubernetes name (RFC 1123 label). "
                 f"Pass --model-name with a valid alias.")
    if not _INVOKE_ID_RE.match(invocation_id):
        sys.exit(f"error: refusing to emit an unsafe Bedrock invocation id: {invocation_id!r}.")

    lines = [
        "# BedrockModel — generated by 'platformctl new-model --source bedrock'.",
        f"# Enrolls Amazon Bedrock model '{invocation_id}' as LiteLLM alias '{alias}'.",
        "# litellm-sync registers it (bedrock/ upstream, IRSA auth). Remove with",
        f"#   ./platformctl new-model --undeploy {alias}",
        "apiVersion: bedrock.ai-platform/v1alpha1",
        "kind: BedrockModel",
        "metadata:",
        f"  name: {alias}",
        "  namespace: inference",
        "spec:",
        f'  model: "bedrock/{invocation_id}"',
        f"  modelName: {alias}",
    ]
    # Sovereign / non-commercial partitions need the explicit runtime endpoint;
    # the standard aws partition uses LiteLLM's default (omit the field).
    if partition != "aws":
        lines.append(f'  bedrockEndpoint: "{runtime_endpoint(region, partition)}"')
    if in_cost is not None:
        lines.append(f'  inputCostPerToken: "{_fmt_cost(in_cost)}"')
    if out_cost is not None:
        lines.append(f'  outputCostPerToken: "{_fmt_cost(out_cost)}"')

    yaml_body = "\n".join(lines) + "\n"
    yaml_path = f"{BEDROCK_DIR}/{alias}.yaml"
    commit_msg = f"feat: enroll Bedrock model {alias} ({invocation_id})"
    return alias, yaml_path, yaml_body, commit_msg


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #

def _list_available(region: str, args: argparse.Namespace) -> int:
    C = _palette(_should_use_colour(args))
    partition = partition_for(region)
    try:
        fms = list_text_models(region)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    profiles = system_profiles_by_model(region)

    print(f"\n{C.BOLD}Amazon Bedrock text models available in "
          f"{region}{C.RESET} {C.DIM}(partition {partition}){C.RESET}")
    print(f"{C.DIM}Enroll one with: ./platformctl new-model --source bedrock "
          f"<model> --deploy{C.RESET}\n")
    header = f"  {'ALIAS':<24} {'INVOCATION ID (use this)':<48} {'$/1K IN':>9} {'$/1K OUT':>9}"
    print(f"{C.DIM}{header}{C.RESET}")
    print(f"{C.DIM}  {'-'*24} {'-'*48} {'-'*9} {'-'*9}{C.RESET}")

    for fm in fms:
        fm_id = fm.get("modelId") or ""
        pid = _best_profile(region, profiles.get(fm_id, []))
        invocation = pid or fm_id
        alias = default_alias(fm_id)
        in_c, out_c = token_prices(region, fm)
        in_s = f"${in_c*1000:.5f}" if in_c is not None else "—"
        out_s = f"${out_c*1000:.5f}" if out_c is not None else "—"
        print(f"  {alias:<24} {invocation:<48} {in_s:>9} {out_s:>9}")

    ep_note = (f" Runtime endpoint {runtime_endpoint(region, partition)} is baked "
               f"into each CR." if partition != "aws" else "")
    print(f"\n{C.DIM}'INVOCATION ID' is a cross-region inference profile where one "
          f"exists in this region, else the base model id.{ep_note}{C.RESET}")
    print(f"{C.DIM}Pricing is best-effort from the AWS Price List API ('—' = not "
          f"resolved / not available in this partition; override with "
          f"--input-cost/--output-cost).{C.RESET}")
    if fms:
        ex = default_alias(fms[0].get("modelId") or "")
        print(f"\n{C.BOLD}Example:{C.RESET}  {C.CYAN}./platformctl new-model "
              f"--source bedrock {ex} --deploy{C.RESET}")
    return 0


def _enroll(region: str, args: argparse.Namespace) -> int:
    C = _palette(_should_use_colour(args))
    partition = partition_for(region)
    if not args.model:
        sys.stderr.write("error: a Bedrock model id/name is required "
                         "(or use --list-available-models).\n")
        return 2
    try:
        fms = list_text_models(region)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    profiles = system_profiles_by_model(region)
    try:
        fm, invocation_id, used_profile = resolve(region, args.model, fms, profiles)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    alias = getattr(args, "model_name", None) or default_alias(fm.get("modelId") or "")

    # Pricing: explicit overrides win; else best-effort from the Price List API.
    in_cost = float(args.input_cost) if getattr(args, "input_cost", None) is not None else None
    out_cost = float(args.output_cost) if getattr(args, "output_cost", None) is not None else None
    if in_cost is None and out_cost is None:
        in_cost, out_cost = token_prices(region, fm)

    prof_note = "cross-region inference profile" if used_profile else "base foundation-model id"
    print(f"\n{C.BOLD}Bedrock model:{C.RESET} {fm.get('modelId')} "
          f"{C.DIM}({fm.get('providerName','')}){C.RESET}")
    print(f"  {C.BOLD}Invocation id:{C.RESET} {invocation_id}  {C.DIM}({prof_note}){C.RESET}")
    print(f"  {C.BOLD}LiteLLM alias:{C.RESET} {alias}")
    if partition != "aws":
        print(f"  {C.BOLD}Runtime endpoint:{C.RESET} {runtime_endpoint(region, partition)}")
    price_line = (
        f"in ${in_cost*1000:.5f}/1K, out ${out_cost*1000:.5f}/1K"
        if (in_cost is not None and out_cost is not None)
        else "not resolved (LiteLLM will use its built-in map; set --input-cost/--output-cost for accuracy)"
    )
    print(f"  {C.BOLD}Price:{C.RESET} {price_line}")
    print(f"  {C.DIM}Requires Bedrock model access enabled for this model in the "
          f"console; ./platformctl status --check validates the live call.{C.RESET}")

    name, yaml_path, yaml_body, commit_msg = build_yaml(
        alias, invocation_id, partition, region, in_cost, out_cost)

    if not args.deploy:
        print(f"\n{C.BOLD}You'll enroll this{C.RESET} {C.DIM}→ {yaml_path}{C.RESET}")
        for ln in yaml_body.rstrip().splitlines():
            print(f"  {C.DIM}│{C.RESET} {ln}")
        print(f"\n{C.BOLD}Enroll it:{C.RESET}  {C.CYAN}./platformctl new-model "
              f"--source bedrock {args.model} --deploy{C.RESET}")
        print(f"  {C.DIM}writes the CR, commits, pushes, and triggers ArgoCD — "
              f"litellm-sync then registers it on the /v1 API.{C.RESET}")
        return 0

    # Bedrock has no HF weights — make sure the shared deploy path never tries to
    # provision an hf-token Secret from an ambient $HF_TOKEN.
    args.hf_token = None
    from .gitops import deploy_model
    return deploy_model(name, yaml_path, yaml_body, commit_msg, args)


def run_bedrock(args: argparse.Namespace) -> int:
    """Entry point for `new-model --source bedrock` (dispatched from cli.main)."""
    region = detect_region(getattr(args, "region", None))
    if getattr(args, "list_available_models", False):
        return _list_available(region, args)
    return _enroll(region, args)
