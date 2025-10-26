#!/usr/bin/env python3
"""
OpenHAB MCP - Main entry point
"""
from openhab_mcp.openhab_mcp_server import run_server

if __name__ == "__main__":
    # This runs when the package is executed with: python -m openhab_mcp
    run_server()
