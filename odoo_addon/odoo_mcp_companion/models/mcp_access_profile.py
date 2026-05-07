# -*- coding: utf-8 -*-
"""mcp.access.profile — central record for who can do what via the MCP.

One profile per Odoo user that should be reachable through the odoo-mcp
integration. The profile records the desired access level (read or
read-write) and an optional partner-id scope. Installing the companion
addon does NOT auto-create profiles for any user; an Odoo admin creates
them deliberately.

This module is part of the *optional, untested* odoo_mcp_companion
addon — see odoo_addon/README.md.
"""

from odoo import api, fields, models


class McpAccessProfile(models.Model):
    _name = "mcp.access.profile"
    _description = "MCP Access Profile"
    _rec_name = "user_id"

    user_id = fields.Many2one(
        "res.users",
        string="Odoo User",
        required=True,
        ondelete="cascade",
        index=True,
        help=(
            "The Odoo user whose API key the MCP authenticates with. "
            "This profile defines what they can do via the MCP integration."
        ),
    )
    access_level = fields.Selection(
        [("read", "Read only"), ("readwrite", "Read and Write")],
        string="Access level",
        required=True,
        default="read",
        help=(
            "Read only adds the user to the MCP Read Only group. "
            "Read and Write adds them to the MCP Read+Write group instead."
        ),
    )
    scope_partner_ids = fields.Many2many(
        "res.partner",
        string="Scope to partners",
        help=(
            "If set, record-rule scoping limits the user to records linked "
            "to one of these partners. Empty = no partner-level restriction "
            "(group membership still applies)."
        ),
    )
    notes = fields.Text(
        string="Notes",
        help="Free text for the admin (why this profile exists, when to revoke).",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "user_id_unique",
            "unique(user_id)",
            "There is already an MCP access profile for this user.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._sync_groups()
        return records

    def write(self, vals):
        result = super().write(vals)
        if "access_level" in vals or "user_id" in vals or "active" in vals:
            for rec in self:
                rec._sync_groups()
        return result

    def unlink(self):
        users = self.mapped("user_id")
        result = super().unlink()
        for user in users:
            self._reset_groups_for(user)
        return result

    def _sync_groups(self):
        """Apply the profile's access level to the linked user.

        Removes the user from both MCP groups and adds them back to the
        appropriate one. If the profile is archived (active=False) the
        user is removed from both groups.
        """
        self.ensure_one()
        ro_group = self.env.ref(
            "odoo_mcp_companion.group_mcp_readonly", raise_if_not_found=False
        )
        rw_group = self.env.ref(
            "odoo_mcp_companion.group_mcp_readwrite", raise_if_not_found=False
        )
        ops = []
        if ro_group:
            ops.append((3, ro_group.id))
        if rw_group:
            ops.append((3, rw_group.id))
        if self.active:
            target = rw_group if self.access_level == "readwrite" else ro_group
            if target:
                ops.append((4, target.id))
        if ops:
            self.user_id.sudo().write({"groups_id": ops})

    @api.model
    def _reset_groups_for(self, user):
        """Drop both MCP groups from ``user`` (used on profile unlink)."""
        ro_group = self.env.ref(
            "odoo_mcp_companion.group_mcp_readonly", raise_if_not_found=False
        )
        rw_group = self.env.ref(
            "odoo_mcp_companion.group_mcp_readwrite", raise_if_not_found=False
        )
        ops = []
        if ro_group:
            ops.append((3, ro_group.id))
        if rw_group:
            ops.append((3, rw_group.id))
        if ops:
            user.sudo().write({"groups_id": ops})
