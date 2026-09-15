"""Config flow for Life Dashboard.

Phase 1 stub: hassfest requires this module when the manifest sets config_flow.
Phase 3 replaces it with the real flow (name, URL choice, generated secret).
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class LifeDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Life Dashboard."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        return self.async_abort(reason="not_ready")
