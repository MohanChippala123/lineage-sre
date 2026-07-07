"""The demo data stack: a small payments pipeline, its DataHub metadata, and the break scenario.

Pipeline (all DuckDB, mirrors a dbt-style project):

    raw_payments  ──> stg_payments  ──> fct_daily_revenue
    raw_customers ──> stg_customers ──> churn_features ──> churn_model_predictions (ML scoring output)
                        stg_payments ──┘
"""

from __future__ import annotations

import duckdb
from datahub.emitter.mce_builder import make_user_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    BooleanTypeClass,
    CorpUserInfoClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    DateTypeClass,
    NullTypeClass,
    NumberTypeClass,
    OtherSchemaClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    TimeTypeClass,
    UpstreamClass,
    UpstreamLineageClass,
)

from .config import PLATFORM, Settings
from .datahub_client import DataHubClient, dataset_urn_for
from .warehouse import ModelDef, apply_upstream_break, create_raw_tables, create_views, describe_table

USERS = {
    "jane.doe": {"displayName": "Jane Doe", "email": "jane.doe@example.com", "title": "Data Platform Engineer"},
    "mike.ops": {"displayName": "Mike Ops", "email": "mike.ops@example.com", "title": "Analytics Engineer"},
}

RAW_TABLES = {
    "raw_payments": {
        "description": "Raw payments feed ingested nightly from the PayFlow vendor SFTP drop.",
        "owner": "jane.doe",
        "customProperties": {"vendor": "PayFlow", "feed_version": "1", "ingestion": "nightly 02:00 UTC"},
    },
    "raw_customers": {
        "description": "Raw customer master data from the CRM export.",
        "owner": "jane.doe",
        "customProperties": {"source": "CRM export", "ingestion": "nightly 02:00 UTC"},
    },
}

MODELS = [
    ModelDef(
        name="stg_payments",
        depends_on=["raw_payments"],
        sql_file="stg_payments.sql",
        description="Cleaned payments. Contract: payment_id, customer_id, amount_usd, currency, payment_date.",
        owner="mike.ops",
    ),
    ModelDef(
        name="stg_customers",
        depends_on=["raw_customers"],
        sql_file="stg_customers.sql",
        description="Cleaned customer dimension.",
        owner="mike.ops",
    ),
    ModelDef(
        name="fct_daily_revenue",
        depends_on=["stg_payments"],
        sql_file="fct_daily_revenue.sql",
        description="Daily revenue fact table. Powers the executive revenue dashboard.",
        owner="mike.ops",
    ),
    ModelDef(
        name="churn_features",
        depends_on=["stg_customers", "stg_payments"],
        sql_file="churn_features.sql",
        description="Per-customer features consumed by the churn model's nightly scoring job.",
        owner="mike.ops",
    ),
    ModelDef(
        name="churn_model_predictions",
        depends_on=["churn_features"],
        sql_file="",
        description="Nightly churn-risk scores written by the production churn model (v3).",
        owner="mike.ops",
        materialized=False,
    ),
]

PREDICTIONS_SCHEMA = [
    ("customer_id", "INTEGER"),
    ("churn_probability", "DOUBLE"),
    ("scored_at", "TIMESTAMP"),
]


def _field_type(duckdb_type: str) -> SchemaFieldDataTypeClass:
    t = duckdb_type.upper()
    if t.startswith(("DECIMAL", "DOUBLE", "FLOAT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT")):
        return SchemaFieldDataTypeClass(type=NumberTypeClass())
    if t.startswith("VARCHAR") or t in {"TEXT", "STRING"}:
        return SchemaFieldDataTypeClass(type=StringTypeClass())
    if t == "DATE":
        return SchemaFieldDataTypeClass(type=DateTypeClass())
    if t.startswith(("TIMESTAMP", "TIME")):
        return SchemaFieldDataTypeClass(type=TimeTypeClass())
    if t == "BOOLEAN":
        return SchemaFieldDataTypeClass(type=BooleanTypeClass())
    return SchemaFieldDataTypeClass(type=NullTypeClass())


def _schema_aspect(table: str, columns: list[tuple[str, str]]) -> SchemaMetadataClass:
    return SchemaMetadataClass(
        schemaName=table,
        platform=f"urn:li:dataPlatform:{PLATFORM}",
        version=0,
        hash="",
        platformSchema=OtherSchemaClass(rawSchema=""),
        fields=[
            SchemaFieldClass(fieldPath=name, type=_field_type(dtype), nativeDataType=dtype)
            for name, dtype in columns
        ],
    )


def _emit_dataset(emitter, table: str, description: str, owner: str,
                  columns: list[tuple[str, str]], upstreams: list[str],
                  custom_properties: dict | None = None) -> None:
    urn = dataset_urn_for(table)
    aspects = [
        DatasetPropertiesClass(name=table, description=description, customProperties=custom_properties or {}),
        _schema_aspect(table, columns),
        OwnershipClass(owners=[OwnerClass(owner=make_user_urn(owner), type=OwnershipTypeClass.TECHNICAL_OWNER)]),
    ]
    if upstreams:
        aspects.append(
            UpstreamLineageClass(
                upstreams=[
                    UpstreamClass(dataset=dataset_urn_for(up), type=DatasetLineageTypeClass.TRANSFORMED)
                    for up in upstreams
                ]
            )
        )
    for aspect in aspects:
        emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))


def seed_warehouse(settings: Settings) -> None:
    con = duckdb.connect(str(settings.warehouse_path))
    try:
        create_raw_tables(con)
        create_views(con, MODELS, settings.models_dir)
    finally:
        con.close()


def seed_datahub(settings: Settings) -> None:
    client = DataHubClient(settings.gms_url, settings.datahub_token)
    emitter = client.emitter()

    for username, info in USERS.items():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=make_user_urn(username),
                aspect=CorpUserInfoClass(active=True, **info),
            )
        )

    for table, meta in RAW_TABLES.items():
        columns = describe_table(settings.warehouse_path, table)
        _emit_dataset(emitter, table, meta["description"], meta["owner"], columns,
                      upstreams=[], custom_properties=meta["customProperties"])

    for model in MODELS:
        if model.materialized:
            columns = describe_table(settings.warehouse_path, model.name)
        else:
            columns = PREDICTIONS_SCHEMA
        _emit_dataset(emitter, model.name, model.description, model.owner, columns,
                      upstreams=model.depends_on)


def break_scenario(settings: Settings, with_datahub: bool = True) -> None:
    """PayFlow ships feed v2: amount_usd is now amount. Nightly ingestion refreshes DataHub."""
    con = duckdb.connect(str(settings.warehouse_path))
    try:
        apply_upstream_break(con)
    finally:
        con.close()

    if with_datahub:
        client = DataHubClient(settings.gms_url, settings.datahub_token)
        emitter = client.emitter()
        columns = describe_table(settings.warehouse_path, "raw_payments")
        meta = dict(RAW_TABLES["raw_payments"])
        props = dict(meta["customProperties"])
        props["feed_version"] = "2"
        _emit_dataset(emitter, "raw_payments", meta["description"], meta["owner"], columns,
                      upstreams=[], custom_properties=props)
