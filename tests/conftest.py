"""Fixtures for the Life Dashboard tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in every test."""
    yield
