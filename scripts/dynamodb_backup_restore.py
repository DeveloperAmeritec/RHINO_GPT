#!/usr/bin/env python3
"""Crea, respalda y restaura las tablas DynamoDB de RhinoGPT."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


REGION = "us-east-2"
TABLE_NAMES = ("Users", "Chats")
RESTORE_CONFIRMATION = "RESTORE_RHINOGPT_TABLES"
TABLE_SCHEMAS = {
    "Users": {
        "AttributeDefinitions": [
            {"AttributeName": "user_id", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "user_id", "KeyType": "HASH"},
        ],
    },
    "Chats": {
        "AttributeDefinitions": [
            {"AttributeName": "chat_id", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "chat_date", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "chat_id", "KeyType": "HASH"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "UserChatsIndex",
                "KeySchema": [
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "chat_date", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
}
FIELD_REFERENCE = {
    "Users": "user_id:S, full_name:S, user_type:S, personal_profile_id:S, grade:S|NULL, section:S|NULL, monthly_limit:N, current_usage:N, reset_date:S, registration_date:S, last_access_date:S",
    "Chats": "chat_id:S, user_id:S, chat_date:S, question:S, answer:S, status:S, response_id:S, response_time:N, input_tokens:N, output_tokens:N, requested_type:S, user_type:S, personal_profile_id:S, grade:S",
}


def table_exists(client, table_name: str) -> bool:
    try:
        client.describe_table(TableName=table_name)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def create_empty_tables(region: str = REGION) -> None:
    """Crea ambas tablas vacías; nunca elimina ni carga registros."""
    client = boto3.client("dynamodb", region_name=region)
    existing = [name for name in TABLE_NAMES if table_exists(client, name)]
    if existing:
        raise RuntimeError(
            "Creación cancelada: ya existen estas tablas: " + ", ".join(existing)
        )
    for table_name in TABLE_NAMES:
        client.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            **TABLE_SCHEMAS[table_name],
        )
        client.get_waiter("table_exists").wait(TableName=table_name)
        print(f"OK tabla vacía creada: {table_name}")


def scan_all(client, table_name: str) -> list[dict]:
    items: list[dict] = []
    paginator = client.get_paginator("scan")
    for page in paginator.paginate(TableName=table_name, ConsistentRead=True):
        items.extend(page.get("Items", []))
    return items


def backup(output_root: Path, region: str = REGION) -> Path:
    client = boto3.client("dynamodb", region_name=region)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = output_root / f"rhinogpt_dynamodb_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "region": region,
        "tables": [],
    }
    for table_name in TABLE_NAMES:
        items = scan_all(client, table_name)
        file_name = f"{table_name}.json"
        payload = {
            "schema": {
                "TableName": table_name,
                "BillingMode": "PAY_PER_REQUEST",
                **TABLE_SCHEMAS[table_name],
            },
            "items": items,
        }
        (output_dir / file_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest["tables"].append(
            {"name": table_name, "file": file_name, "items": len(items)}
        )
        print(f"OK backup {table_name}: {len(items)} registros")
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Backup completo: {output_dir}")
    return output_dir


def write_items(client, table_name: str, items: list[dict]) -> None:
    for offset in range(0, len(items), 25):
        pending = {
            table_name: [
                {"PutRequest": {"Item": item}} for item in items[offset : offset + 25]
            ]
        }
        while pending:
            response = client.batch_write_item(RequestItems=pending)
            pending = response.get("UnprocessedItems", {})
            if pending:
                time.sleep(1)


def restore(
    backup_dir: Path,
    confirmation: str,
    replace_existing: bool = False,
    region: str = REGION,
) -> None:
    if confirmation != RESTORE_CONFIRMATION:
        raise ValueError(f"Usa --confirm {RESTORE_CONFIRMATION} explícitamente.")
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    client = boto3.client("dynamodb", region_name=region)
    for entry in manifest["tables"]:
        payload = json.loads((backup_dir / entry["file"]).read_text(encoding="utf-8"))
        table_name = entry["name"]
        if table_exists(client, table_name):
            if not replace_existing:
                raise RuntimeError(
                    f"{table_name} ya existe; no se modificó nada. "
                    "Usa --replace-existing solo en una recuperación controlada."
                )
            client.delete_table(TableName=table_name)
            client.get_waiter("table_not_exists").wait(TableName=table_name)
        client.create_table(**payload["schema"])
        client.get_waiter("table_exists").wait(TableName=table_name)
        write_items(client, table_name, payload["items"])
        print(f"OK restore {table_name}: {len(payload['items'])} registros")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=REGION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-schema", help="Crea Users y Chats vacías")
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument(
        "--output", type=Path, default=Path("local_private/backups")
    )
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("backup_dir", type=Path)
    restore_parser.add_argument("--confirm", required=True)
    restore_parser.add_argument("--replace-existing", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "create-schema":
        create_empty_tables(args.region)
    elif args.command == "backup":
        backup(args.output, args.region)
    else:
        restore(args.backup_dir, args.confirm, args.replace_existing, args.region)


if __name__ == "__main__":
    main()
