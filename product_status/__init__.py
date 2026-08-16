"""Linear product status service.

Fetches the current and previous cycle ("sprint") status for every team, and
project summaries (status, dates, milestones) for a given project label,
directly from Linear's GraphQL API (https://api.linear.app/graphql).

This package intentionally never uses the Linear MCP tools - all data comes
from raw GraphQL queries via `product_status.linear_client`.
"""

__version__ = "0.1.0"
