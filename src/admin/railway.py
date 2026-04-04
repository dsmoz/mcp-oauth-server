"""
Railway GraphQL API helper — fetches services + public domains for a project.

Uses the Railway GraphQL v2 API with a Bearer token.
Returns a list of dicts: {slug, name, domain} where domain is the first
public domain found for the service (or None if no domain is set).
"""
from __future__ import annotations

import httpx

_RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"

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


async def fetch_railway_services(token: str, project_id: str, project_ids: str = "") -> list[dict]:
    """Return list of {id, name, slug, domain, upstream_url} for every Railway service.

    project_ids: comma-separated list of project IDs (overrides project_id when set).
    project_id: single project ID (legacy, used when project_ids is empty).
    """
    if not token:
        return []

    # Build the list of project IDs to query
    if project_ids:
        ids = [p.strip() for p in project_ids.split(",") if p.strip()]
    elif project_id:
        ids = [project_id]
    else:
        return []

    all_services: list[dict] = []
    seen_slugs: set[str] = set()

    async with httpx.AsyncClient(timeout=10) as client:
        for pid in ids:
            services = await _fetch_project_services(client, token, pid)
            for svc in services:
                # Deduplicate by slug — first project wins
                if svc["slug"] not in seen_slugs:
                    seen_slugs.add(svc["slug"])
                    all_services.append(svc)

    return all_services
