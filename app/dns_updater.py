#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import google.auth.transport.requests
from google.oauth2 import service_account
import requests
import yaml


DNS_API = "https://dns.googleapis.com/dns/v1"
DNS_SCOPE = "https://www.googleapis.com/auth/ndev.clouddns.readwrite"


@dataclass(frozen=True)
class Record:
    name: str
    zone: str
    project: str
    ttl: int
    record_type: str = "A"
    enabled: bool = True


def log(message: str) -> None:
    print(message, flush=True)


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def normalize_dns_name(name: str) -> str:
    return name if name.endswith(".") else f"{name}."


def load_records(config: dict[str, Any]) -> list[Record]:
    default_project = config.get("project")
    records = []

    for raw in config.get("records", []):
        if not isinstance(raw, dict):
            raise ValueError(f"record entries must be mappings: {raw!r}")

        record_type = str(raw.get("type", "A")).upper()
        if record_type != "A":
            raise ValueError(f"only A records are supported, got {record_type}")

        project = raw.get("project", default_project)
        if not project:
            raise ValueError(f"record {raw.get('name')} is missing project")

        records.append(
            Record(
                name=normalize_dns_name(str(raw["name"])),
                zone=str(raw["zone"]),
                project=str(project),
                ttl=int(raw.get("ttl", 300)),
                record_type=record_type,
                enabled=bool(raw.get("enabled", True)),
            )
        )

    return records


def fetch_public_ipv4(sources: list[str], timeout_seconds: float, ipinfo_token: str | None) -> str:
    errors = []
    for source in sources:
        try:
            headers = {}
            if ipinfo_token and "ipinfo.io" in source:
                headers["Authorization"] = f"Bearer {ipinfo_token}"

            response = requests.get(source, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            candidate = response.text.strip()
            ip = ipaddress.ip_address(candidate)
            if ip.version != 4:
                raise ValueError(f"not an IPv4 address: {candidate}")
            return str(ip)
        except Exception as exc:  # noqa: BLE001 - keep trying configured sources
            errors.append(f"{source}: {exc}")

    raise RuntimeError("could not determine public IPv4 from any source: " + "; ".join(errors))


def load_credentials(path: str) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(path, scopes=[DNS_SCOPE])


def authed_session(credentials: service_account.Credentials) -> requests.Session:
    request = google.auth.transport.requests.Request()
    credentials.refresh(request)
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }
    )
    return session


def get_record(session: requests.Session, record: Record) -> dict[str, Any] | None:
    url = (
        f"{DNS_API}/projects/{record.project}/managedZones/{record.zone}"
        f"/rrsets/{record.name}/{record.record_type}"
    )
    response = session.get(url, timeout=20)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def apply_record(session: requests.Session, record: Record, current: dict[str, Any] | None, ip: str) -> None:
    desired = {
        "name": record.name,
        "type": record.record_type,
        "ttl": record.ttl,
        "rrdatas": [ip],
    }
    change: dict[str, Any] = {"additions": [desired]}
    if current is not None:
        change["deletions"] = [current]

    url = f"{DNS_API}/projects/{record.project}/managedZones/{record.zone}/changes"
    response = session.post(url, data=json.dumps(change), timeout=20)
    response.raise_for_status()


def sync_record(session: requests.Session, record: Record, ip: str, dry_run: bool) -> bool:
    if not record.enabled:
        log(f"skip disabled record {record.name} in zone {record.zone}")
        return False

    current = get_record(session, record)
    current_values = [] if current is None else current.get("rrdatas", [])

    if current_values == [ip] and (current or {}).get("ttl") == record.ttl:
        log(f"no change {record.name} {record.record_type} already {ip} ttl={record.ttl}")
        return False

    old_value = ",".join(current_values) if current_values else "<missing>"
    log(f"update {record.name} {record.record_type} {old_value} -> {ip} ttl={record.ttl}")

    if dry_run:
        log(f"dry-run enabled; not applying {record.name}")
        return True

    apply_record(session, record, current, ip)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Google Cloud DNS A records to current public IPv4.")
    parser.add_argument("--config", default=os.environ.get("DNS_UPDATER_CONFIG", "/config/config.yaml"))
    parser.add_argument("--credentials", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/var/secrets/google/credentials.json"))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DNS_UPDATER_DRY_RUN", "false").lower() == "true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    records = load_records(config)
    sources = config.get("ip_sources") or ["https://api.ipify.org"]
    timeout_seconds = float(config.get("ip_source_timeout_seconds", 10))
    ipinfo_token = os.environ.get("IPINFO_TOKEN")

    if not records:
        raise ValueError("no records configured")

    ip = fetch_public_ipv4(sources, timeout_seconds, ipinfo_token)
    log(f"public IPv4 is {ip}")

    credentials = load_credentials(args.credentials)
    session = authed_session(credentials)

    changed = 0
    for record in records:
        if sync_record(session, record, ip, args.dry_run):
            changed += 1

    log(f"complete: {changed} record(s) changed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - log cleanly for CronJob output
        print(f"error: {exc}", file=sys.stderr, flush=True)
        raise

