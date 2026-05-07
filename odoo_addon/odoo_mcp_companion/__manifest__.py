# -*- coding: utf-8 -*-
# Optional, untested-against-live-Odoo companion addon for odoo-mcp.
# See ../README.md for status and intent.

{
    "name": "MCP Companion",
    "version": "1.0.0",
    "summary": "Odoo-side ACL groups and access profiles for odoo-mcp",
    "description": (
        "Adds two security groups (MCP Read Only / MCP Read+Write) and an "
        "mcp.access.profile model so Odoo admins can manage which users an "
        "MCP integration can act as, with server-enforced ACLs that "
        "complement the client-side guardrails in the odoo-mcp Python "
        "package. See odoo_addon/README.md for status."
    ),
    "category": "Tools",
    "author": "deltix consulting",
    "website": "https://github.com/deltix-consulting/odoo-mcp",
    "license": "MIT",
    "depends": ["base", "mail"],
    "data": [
        "security/mcp_security.xml",
        "security/ir.model.access.csv",
        "views/mcp_access_profile_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
