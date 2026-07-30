"""Region detection and EC2 GPU pricing (AWS Pricing API with on-disk cache and a
static catalog fallback)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .catalog import CATALOG_PRICING_REGION, INSTANCES, PRICING_CACHE_TTL_SEC


# --------------------------------------------------------------------------- #
# Region + pricing                                                            #
# --------------------------------------------------------------------------- #

def detect_region(explicit: str | None) -> str:
    """Explicit > AWS_REGION > AWS_DEFAULT_REGION > boto3 session > catalog default."""
    if explicit:
        return explicit
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        import boto3  # type: ignore
        region = boto3.Session().region_name
        if region:
            return region
    except Exception:
        pass
    return CATALOG_PRICING_REGION


def _pricing_cache_path(region: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "ai-platform" / f"pricing-{region}.json"


def _load_cached_prices(region: str) -> dict[str, float] | None:
    path = _pricing_cache_path(region)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if time.time() - payload.get("timestamp", 0) > PRICING_CACHE_TTL_SEC:
            return None
        return {k: float(v) for k, v in payload.get("prices", {}).items()}
    except (OSError, ValueError, KeyError):
        return None


def _save_cached_prices(region: str, prices: dict[str, float]) -> None:
    path = _pricing_cache_path(region)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": time.time(), "region": region, "prices": prices}))
    except OSError:
        pass  # Non-fatal; we'll just refetch next time.


def _parse_pricelist_entry(price_list_json: str) -> float | None:
    """Extract the hourly OnDemand USD price from one AWS Pricing API record."""
    try:
        data = json.loads(price_list_json)
        for term in data.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is not None:
                    val = float(usd)
                    if val > 0:
                        return val
    except (ValueError, AttributeError, TypeError):
        return None
    return None


def fetch_prices_from_aws(region: str, instance_types: list[str],
                          verbose: bool = False) -> dict[str, float]:
    """Query the AWS Pricing API for on-demand Linux prices in `region`.

    Requires boto3 + valid AWS credentials. Silent no-op if either is missing.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        if verbose:
            print("  ! boto3 not installed; falling back to static us-east-1 prices",
                  file=sys.stderr)
        return {}

    # Pricing API is global but only exposed in us-east-1, ap-south-1, eu-central-1.
    try:
        client = boto3.client("pricing", region_name="us-east-1")
    except Exception as e:
        if verbose:
            print(f"  ! could not initialise Pricing client: {e}", file=sys.stderr)
        return {}

    prices: dict[str, float] = {}
    for it in instance_types:
        try:
            resp = client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType",    "Value": it},
                    {"Type": "TERM_MATCH", "Field": "regionCode",      "Value": region},
                    {"Type": "TERM_MATCH", "Field": "tenancy",         "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw",  "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus",  "Value": "Used"},
                ],
                MaxResults=10,
            )
            for entry in resp.get("PriceList", []):
                # PriceList items are JSON strings in SDK v1, dicts in v2.
                raw = entry if isinstance(entry, str) else json.dumps(entry)
                price = _parse_pricelist_entry(raw)
                if price is not None:
                    prices[it] = price
                    break
        except (BotoCoreError, ClientError) as e:
            if verbose:
                print(f"  ! pricing lookup failed for {it}: {e}", file=sys.stderr)
        except Exception as e:
            if verbose:
                print(f"  ! unexpected error pricing {it}: {e}", file=sys.stderr)
    return prices


def resolve_prices(region: str, refresh: bool, verbose: bool) -> tuple[dict[str, float], str]:
    """Return (prices_by_instance_type, source_label)."""
    if not refresh:
        cached = _load_cached_prices(region)
        if cached:
            return cached, f"cache ({region})"

    instance_types = [i.name for i in INSTANCES]
    fetched = fetch_prices_from_aws(region, instance_types, verbose=verbose)
    if fetched:
        _save_cached_prices(region, fetched)
        return fetched, f"AWS Pricing API ({region})"

    # Fall back to the static catalog, flagging if the user asked for a
    # different region than the catalog's baked-in one.
    static = {i.name: i.price_usd_h for i in INSTANCES}
    if region != CATALOG_PRICING_REGION:
        if verbose:
            print(f"  ! using static {CATALOG_PRICING_REGION} prices — requested {region}",
                  file=sys.stderr)
        return static, f"⚠ static catalog ({CATALOG_PRICING_REGION}, not {region})"
    return static, f"static catalog ({CATALOG_PRICING_REGION})"
