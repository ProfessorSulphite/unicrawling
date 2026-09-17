"""
edmetriX Backend Catalog Ingestion Uploader (uploader.py)
Pushes crawled and structured university JSON/JSONL catalog payloads to the edmetriX backend API.
"""
import os
import sys
import json
from pathlib import Path
from typing import Optional, Dict, Any, Union

import requests

try:
    from src.config import config
except ImportError:
    from config import config

DEFAULT_EDMETRIX_API_URL = "https://edmetrix-backend.onrender.com/api/v1"


def get_default_master_file() -> Path:
    """Returns the default master catalog file path (.jsonl or .json)."""
    if config.output_jsonl_path.exists():
        return config.output_jsonl_path
    json_path = config.data_outputs_dir / "university_counseling_data.json"
    if json_path.exists():
        return json_path
    return config.output_jsonl_path


def upload_catalog(
    file_path: Optional[Union[str, Path]] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    dry_run: bool = False,
    timeout: int = 180,
    exit_on_error: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Uploads a university catalog file (.json or .jsonl) to the edmetriX backend API.
    If file_path is None, defaults automatically to the master catalog file
    (university_counseling_data.jsonl).

    Args:
        file_path: Path to .json or .jsonl file, or None to use the master catalog file
        api_url: edmetriX base URL (defaults to EDMETRIX_API_URL or fallback)
        api_key: Ingestion secret key (defaults to EDMETRIX_API_KEY or DEV_ACCESS_KEY)
        dry_run: Validate without writing to database
        timeout: Request timeout in seconds
        exit_on_error: If True, calls sys.exit(1) on failure (CLI mode)
    """
    if file_path is None or (isinstance(file_path, str) and not file_path.strip()):
        path = get_default_master_file()
        print(f"ℹ️ No file specified — using master dataset: {path}")
    else:
        path = Path(file_path)

    if not path.exists():
        err = f"Error: Master file not found: {path}. Please run pipeline first or specify an existing file."
        print(err, file=sys.stderr)
        if exit_on_error:
            sys.exit(1)
        raise FileNotFoundError(err)

    # Resolve API URL
    resolved_url = (
        api_url
        or os.environ.get("EDMETRIX_API_URL")
        or DEFAULT_EDMETRIX_API_URL
    ).rstrip("/")

    # Resolve API Key
    resolved_key = (
        api_key
        or os.environ.get("EDMETRIX_API_KEY")
        or os.environ.get("DEV_ACCESS_KEY")
        or ""
    )

    if not resolved_key:
        err = (
            "❌ Ingestion key missing. Please provide --key / -k or set "
            "EDMETRIX_API_KEY or DEV_ACCESS_KEY in your environment."
        )
        print(err, file=sys.stderr)
        if exit_on_error:
            sys.exit(1)
        raise ValueError(err)

    print(f"Reading data from {path}...")
    try:
        with open(path, "r", encoding="utf-8") as f:
            if str(path).endswith(".jsonl"):
                data = [json.loads(line) for line in f if line.strip()]
            else:
                data = json.load(f)
    except Exception as e:
        err = f"❌ Failed to parse JSON data from {path}: {e}"
        print(err, file=sys.stderr)
        if exit_on_error:
            sys.exit(1)
        raise e

    endpoint = f"{resolved_url}/dev/catalog/ingest"
    params = {"dry_run": "true"} if dry_run else {}
    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
    }

    mode_label = "[DRY-RUN] " if dry_run else ""
    print(f"{mode_label}Uploading to {endpoint}...")

    try:
        response = requests.post(endpoint, json=data, params=params, headers=headers, timeout=timeout)
        if response.status_code == 200:
            res_data = response.json()
            summary = res_data.get("summary", {})
            print("\n✅ Ingestion Successful!")
            print(f" • Countries:            {', '.join(summary.get('countries', []))}")
            print(f" • Universities Created: {summary.get('universities_created', 0)}")
            print(f" • Universities Updated: {summary.get('universities_updated', 0)}")
            print(f" • Programs Created:     {summary.get('programs_created', 0)}")
            print(f" • Programs Updated:     {summary.get('programs_updated', 0)}")
            print(f" • Faculties Upserted:   {summary.get('faculties_upserted', 0)}")
            print(f" • Programs Deactivated: {summary.get('programs_deactivated', 0)}")
            print(f" • Warnings:             {summary.get('warning_count', 0)}")
            return res_data
        elif response.status_code in (401, 403):
            err = f"\n❌ Authentication Failed ({response.status_code}): Invalid DEV_ACCESS_KEY."
            print(err, file=sys.stderr)
            if exit_on_error:
                sys.exit(1)
            raise PermissionError(err)
        else:
            err = f"\n❌ Ingestion Failed ({response.status_code}):\n{response.text}"
            print(err, file=sys.stderr)
            if exit_on_error:
                sys.exit(1)
            raise RuntimeError(err)
    except requests.RequestException as e:
        err = f"\n❌ Network error while connecting to edmetriX: {e}"
        print(err, file=sys.stderr)
        if exit_on_error:
            sys.exit(1)
        raise e
