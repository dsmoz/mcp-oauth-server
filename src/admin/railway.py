"""
Railway GraphQL API helper — fetches services + public domains for a project.

Uses the Railway GraphQL v2 API with a Bearer token.
Returns a list of dicts: {slug, name, domain} where domain is the first
public domain found for the service (or None if no domain is set).
"""
from __future__ import annotations

import httpx

_RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"

# Slugs to skip during workspace-wide discovery — the gateway itself, etc.
_EXCLUDED_SLUGS = {"mcp-oauth-server"}

_WORKSPACE_PROJECTS_QUERY = """
query WorkspaceProjects {
  me {
    projects {
      edges {
        node {
          id
          name
        }
      }
    }
  }
}
"""

_SERVICES_QUERY = """
query ProjectServices($projectId: String!) {
  project(id: $projectId) {
    services {
      edges {
        node {
          id
          name
          serviceInstances {
            edges {
              node {
                domains {
                  serviceDomains {
                    domain
                  }
                  customDomains {
                    domain
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


async def _fetch_project_services(client: httpx.AsyncClient, token: str, project_id: str) -> list[dict]:
    """Fetch services for a single Railway project."""
    resp = await client.post(
        _RAILWAY_GQL,
        json={"query": _SERVICES_QUERY, "variables": {"projectId": project_id}},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errors"):
        raise ValueError(data["errors"][0].get("message", "GraphQL error"))

    edges = (
        data.get("data", {})
            .get("project", {})
            .get("services", {})
            .get("edges", [])
    )

    services = []
    for edge in edges:
        node = edge.get("node", {})
        svc_id = node.get("id", "")
        name = node.get("name", "")
        slug = name.lower().replace(" ", "-").replace("_", "-")

        # Collect domains from all service instances
        domain = None
        for inst_edge in (node.get("serviceInstances") or {}).get("edges", []):
            inst = inst_edge.get("node", {})
            domains_obj = inst.get("domains") or {}
            custom = [d["domain"] for d in (domains_obj.get("customDomains") or []) if d.get("domain")]
            service_d = [d["domain"] for d in (domains_obj.get("serviceDomains") or []) if d.get("domain")]
            domain = (custom + service_d + [None])[0]
            if domain:
                break

        upstream_url = f"https://{domain}/mcp" if domain else None

        services.append({
            "id": svc_id,
            "name": name,
            "slug": slug,
            "domain": domain,
            "upstream_url": upstream_url,
        })

    return services


async def _fetch_all_workspace_project_ids(client: httpx.AsyncClient, token: str) -> list[str]:
    """Return every project ID accessible to the Railway token."""
    resp = await client.post(
        _RAILWAY_GQL,
        json={"query": _WORKSPACE_PROJECTS_QUERY},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ValueError(data["errors"][0].get("message", "GraphQL error"))

    edges = (
        data.get("data", {}).get("me", {}).get("projects", {}).get("edges", [])
    )
    return [edge["node"]["id"] for edge in edges if edge.get("node", {}).get("id")]


async def fetch_railway_services(
    token: str,
    project_id: str = "",
    project_ids: str = "",
    service_prefix: str = "mcp-",
) -> list[dict]:
    """Return list of {id, name, slug, domain, upstream_url} for every Railway service.

    Discovery modes:
    1. `project_ids` (comma-separated) — explicit allow-list, overrides workspace
       discovery. Use when you need to restrict the catalogue to specific projects.
    2. Unset → workspace-wide auto-discovery (default): queries every project the
       Railway token can see and returns services whose name starts with
       `service_prefix` ("mcp-"). New MCP deploys appear in the catalogue
       without any env var edit.

    `project_id` is deprecated — Railway auto-injects this as the current
    service's own project ID, so relying on it would always just list the
    oauth-server's own services. Kept in the signature for backward compat
    but ignored unless `project_ids` is empty AND the discovery query fails.
    """
    if not token:
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        if project_ids:
            ids = [p.strip() for p in project_ids.split(",") if p.strip()]
            filter_by_prefix = False
        else:
            try:
                ids = await _fetch_all_workspace_project_ids(client, token)
            except Exception:
                # Fall back to the legacy single project ID if workspace query fails
                ids = [project_id] if project_id else []
            filter_by_prefix = True

        all_services: list[dict] = []
        seen_slugs: set[str] = set()

        for pid in ids:
            try:
                services = await _fetch_project_services(client, token, pid)
            except Exception:
                # One bad project shouldn't kill discovery for the rest
                continue
            for svc in services:
                if filter_by_prefix and not svc["slug"].startswith(service_prefix):
                    continue
                if filter_by_prefix and svc["slug"] in _EXCLUDED_SLUGS:
                    continue
                if svc["slug"] in seen_slugs:
                    continue
                seen_slugs.add(svc["slug"])
                all_services.append(svc)

    return all_services
