"""Thin DataHub client: GraphQL for reads + actions, REST emitter for metadata ingestion."""

from __future__ import annotations

import json

import requests
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.emitter.rest_emitter import DatahubRestEmitter

from .config import DATASET_PREFIX, ENV, PLATFORM

GET_DATASET_QUERY = """
query getDataset($urn: String!) {
  dataset(urn: $urn) {
    urn
    name
    platform { name }
    properties {
      description
      customProperties { key value }
    }
    editableProperties { description }
    ownership {
      owners {
        type
        owner {
          ... on CorpUser {
            urn
            username
            properties { displayName email title }
          }
          ... on CorpGroup { urn name }
        }
      }
    }
    schemaMetadata {
      fields { fieldPath nativeDataType description }
    }
  }
}
"""

LINEAGE_QUERY = """
query lineage($input: SearchAcrossLineageInput!) {
  searchAcrossLineage(input: $input) {
    total
    searchResults {
      degree
      entity {
        urn
        type
        ... on Dataset {
          name
          platform { name }
          properties { description }
        }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query search($input: SearchInput!) {
  search(input: $input) {
    searchResults {
      entity {
        urn
        type
        ... on Dataset { name }
      }
    }
  }
}
"""

RAISE_INCIDENT_MUTATION = """
mutation raiseIncident($input: RaiseIncidentInput!) {
  raiseIncident(input: $input)
}
"""

UPDATE_INCIDENT_STATUS_MUTATION = """
mutation updateIncidentStatus($urn: String!, $input: UpdateIncidentStatusInput!) {
  updateIncidentStatus(urn: $urn, input: $input)
}
"""

UPDATE_DESCRIPTION_MUTATION = """
mutation updateDescription($input: DescriptionUpdateInput!) {
  updateDescription(input: $input)
}
"""

INCIDENT_TYPES = {"OPERATIONAL", "FRESHNESS", "VOLUME", "COLUMN", "SQL", "DATA_SCHEMA", "CUSTOM"}


def dataset_urn_for(table_name: str) -> str:
    """URN for a demo table, e.g. stg_payments -> urn:li:dataset:(urn:li:dataPlatform:duckdb,demo.stg_payments,PROD)."""
    return make_dataset_urn(platform=PLATFORM, name=f"{DATASET_PREFIX}.{table_name}", env=ENV)


class DataHubClient:
    def __init__(self, gms_url: str, token: str = ""):
        self.gms_url = gms_url.rstrip("/")
        self.token = token

    # --- plumbing -----------------------------------------------------------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        resp = requests.post(
            f"{self.gms_url}/api/graphql",
            headers=self._headers(),
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"DataHub GraphQL error: {json.dumps(body['errors'])[:2000]}")
        return body["data"]

    def emitter(self) -> DatahubRestEmitter:
        return DatahubRestEmitter(gms_server=self.gms_url, token=self.token or None)

    def ping(self) -> bool:
        try:
            requests.get(f"{self.gms_url}/health", timeout=5).raise_for_status()
            return True
        except requests.RequestException:
            return False

    # --- reads --------------------------------------------------------------

    def get_dataset(self, urn: str) -> dict:
        data = self.graphql(GET_DATASET_QUERY, {"urn": urn})
        dataset = data.get("dataset")
        if not dataset:
            raise RuntimeError(f"Dataset not found in DataHub: {urn}")
        return dataset

    def get_lineage(self, urn: str, direction: str = "UPSTREAM", count: int = 50) -> list[dict]:
        direction = direction.upper()
        if direction not in {"UPSTREAM", "DOWNSTREAM"}:
            raise ValueError("direction must be UPSTREAM or DOWNSTREAM")
        data = self.graphql(
            LINEAGE_QUERY,
            {"input": {"urn": urn, "direction": direction, "query": "*", "start": 0, "count": count}},
        )
        results = data["searchAcrossLineage"]["searchResults"]
        return [
            {
                "urn": r["entity"]["urn"],
                "type": r["entity"]["type"],
                "name": r["entity"].get("name"),
                "platform": (r["entity"].get("platform") or {}).get("name"),
                "degree": r["degree"],
                "description": (r["entity"].get("properties") or {}).get("description"),
            }
            for r in results
        ]

    def search_datasets(self, query: str, count: int = 10) -> list[dict]:
        data = self.graphql(
            SEARCH_QUERY,
            {"input": {"type": "DATASET", "query": query, "start": 0, "count": count}},
        )
        return [
            {"urn": r["entity"]["urn"], "name": r["entity"].get("name")}
            for r in data["search"]["searchResults"]
        ]

    # --- actions (write back to the graph) -----------------------------------

    def raise_incident(self, resource_urn: str, incident_type: str, title: str, description: str) -> str:
        incident_type = incident_type.upper()
        if incident_type not in INCIDENT_TYPES:
            raise ValueError(f"incident_type must be one of {sorted(INCIDENT_TYPES)}")
        data = self.graphql(
            RAISE_INCIDENT_MUTATION,
            {
                "input": {
                    "resourceUrn": resource_urn,
                    "type": incident_type,
                    "title": title,
                    "description": description,
                }
            },
        )
        return data["raiseIncident"]

    def resolve_incident(self, incident_urn: str, message: str) -> bool:
        data = self.graphql(
            UPDATE_INCIDENT_STATUS_MUTATION,
            {"urn": incident_urn, "input": {"state": "RESOLVED", "message": message}},
        )
        return bool(data["updateIncidentStatus"])

    def append_editable_description(self, urn: str, markdown: str) -> None:
        """Append a section to the dataset's editable documentation in DataHub."""
        existing = ""
        try:
            dataset = self.get_dataset(urn)
            existing = (dataset.get("editableProperties") or {}).get("description") or ""
        except RuntimeError:
            pass
        combined = (existing.rstrip() + "\n\n" + markdown.strip()).strip() if existing else markdown.strip()
        self.graphql(
            UPDATE_DESCRIPTION_MUTATION,
            {"input": {"description": combined, "resourceUrn": urn}},
        )
