"""Linear sprint status service.

Fetches the current and previous cycle ("sprint") status for every team
directly from Linear's GraphQL API (https://api.linear.app/graphql).

This package intentionally never uses the Linear MCP tools - all data comes
from raw GraphQL queries via `sprint_status.linear_client`.
"""

__version__ = "0.1.0"
