# odoo-mcp companion addon (optional, untested)

> **Status: design + skeleton.** This addon ships as code and a
> manifest, but it has not been validated against a live Odoo
> installation by the maintainers. Treat it as a starting point for an
> Odoo-side defense layer, not a drop-in production module.

## Why

The `odoo-mcp` MCP server sits in front of Odoo's external API and
enforces its own security envelope: per-user API key, model denylist,
domain sandbox, field redaction, prod-write guard. That envelope is
client-side: it lives inside the MCP process. For consultants who want
an additional defense layer enforced *server-side* by Odoo itself —
e.g. so an Odoo admin can disable MCP access without touching the
consultant's laptop — this companion addon is the starting point.

## What it adds

- A new security group **"MCP Read Only"** (`group_mcp_readonly`)
  that grants read access to a curated set of business models and
  nothing else. Add it to the Odoo user whose API key the MCP uses to
  enforce read-only at the Odoo ACL layer.
- A new security group **"MCP Read/Write"** (`group_mcp_readwrite`)
  that adds write/create/unlink rights on the same curated set.
- A new model **`mcp.access.profile`** which maps an Odoo user to one
  of the two groups above plus optional record-rule scoping. Lets the
  Odoo admin centralize "who can do what via the MCP" in one place
  rather than juggling group membership across many users.
- A handful of `ir.model.access` rows expressing the read / read-write
  splits. Edit `data/access_rules.xml` to match the model set you
  want to expose — the defaults mirror what the MCP's open-mode
  allowlist treats as "obviously business" (partners, leads, orders,
  invoices, products, tasks, employees).

## What it deliberately does NOT add

- It does **not** replace the MCP's own denylist / write-blocklist /
  redaction. Those stay enforced client-side as defense in depth. If
  you uninstall this addon, the MCP still refuses to write to
  `res.users`, `mail.template`, `ir.attachment`, etc.
- It does **not** introduce a new RPC endpoint. Everything goes
  through Odoo's stock external API; the MCP's transport doesn't
  change.
- It does **not** know the MCP exists at runtime. Pure ACL plumbing.

## Install

This is an Odoo module, not a Python package. To install:

1. Copy `odoo_mcp_companion/` into one of your Odoo addons paths.
2. Restart Odoo so the addon is picked up by `--update`.
3. Apps → Update Apps List → search for "MCP Companion" → Install.
4. Settings → Users → pick the user the MCP authenticates as → add
   them to the **"MCP Read Only"** or **"MCP Read/Write"** group.
5. Optionally create an `mcp.access.profile` record per user with
   tighter record-rule scoping (e.g. one `partner_id` allowed).

## Validation status

| Item | Status |
|---|---|
| Manifest parses on Odoo 18.0 | not yet tested by maintainers |
| Manifest parses on Odoo 17.0 / 16.0 | not yet tested |
| Groups install without conflict | not yet tested |
| `mcp.access.profile` creates / writes | not yet tested |
| Security tests | none — write your own before relying on it |

If you do validate it on a real Odoo, please open a PR with your
findings.

## License

Same as the parent repository (MIT).
