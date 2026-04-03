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
"""


async def fetch_railway_services(token: str, project_id: str) -> list[dict]:
    """Return list of {id, name, slug, domain, upstream_url} for every Railway service."""
    if not token or not project_id:
        return []

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _RAILWAY_GQL,
            json={"query": _SERVICES_QUERY, "variables": {"projectId": project_id}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

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

        domains_obj = node.get("domains") or {}
        # Prefer custom domain over generated railway domain
        custom = [d["domain"] for d in (domains_obj.get("customDomains") or []) if d.get("domain")]
        service = [d["domain"] for d in (domains_obj.get("serviceDomains") or []) if d.get("domain")]
        domain = (custom + service + [None])[0]

        upstream_url = f"https://{domain}/sse" if domain else None

        services.append({
            "id": svc_id,
            "name": name,
            "slug": slug,
            "domain": domain,
            "upstream_url": upstream_url,
        })

    return services
