#!/usr/bin/env python3
"""
OpenHAB MCP Server - An MCP server that interacts with a real openHAB instance.

This server uses mcp.server for simplified MCP server implementation and
connects to a real openHAB instance via its REST API.
"""

import os
import sys
import logging
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
from dotenv import load_dotenv
from pydantic import Field

# Import the MCP server implementation
from mcp.server import FastMCP
from mcp.types import TextContent

# Import our modules
from openhab_mcp.models import (
    ItemCreate,
    ItemMetadata,
    Link,
    Tag,
    ThingCreate,
    ThingUpdate,
    RuleCreate,
    RuleUpdate,
)
from openhab_mcp.openhab_client import OpenHABClient
from openhab_mcp.inventory import AdminInventory
from openhab_mcp.overview import build_home_overview as _build_home_overview
from openhab_mcp.audit import (
    save_pattern as _save_pattern,
    delete_pattern as _delete_pattern,
    list_patterns as _list_patterns,
    run_audit as _run_audit,
)
from openhab_mcp.diagnose import diagnose_item as _diagnose_item
from openhab_mcp.rename import rename_item as _rename_item
from openhab_mcp.batch import update_items as _update_items
from openhab_mcp.logs import read_log as _read_log
from openhab_mcp.blueprint import get_thing_context as _get_thing_context
from openhab_mcp.health import analyze_model_health as _analyze_model_health
from urllib.parse import quote as _url_quote

# Load environment variables from .env file
env_file = Path(".env")
if env_file.exists():
    print(f"Loading environment variables from {env_file}", file=sys.stderr)
    load_dotenv(env_file, verbose=True)

# Get MCP server settings from environment variables
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Configure logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Log environment variables (excluding sensitive data)
logger.info("MCP Server Configuration:")
logger.info(f"  MCP_HOST: {MCP_HOST}")
logger.info(f"  MCP_PORT: {MCP_PORT}")
logger.info(f"  LOG_LEVEL: {LOG_LEVEL}")
logger.info(f"  PYTHONPATH: {os.environ.get('PYTHONPATH', 'Not set')}")

# Get OpenHAB connection settings from environment variables
OPENHAB_URL = os.environ.get("OPENHAB_URL", "http://localhost:8080")
OPENHAB_MCP_TRANSPORT = os.environ.get("OPENHAB_MCP_TRANSPORT", "stdio")

# Log OpenHAB settings (excluding sensitive data)
logger.info("OpenHAB Configuration:")
logger.info(f"  OPENHAB_URL: {OPENHAB_URL}")
logger.info(f"  OPENHAB_MCP_TRANSPORT: {OPENHAB_MCP_TRANSPORT}")
logger.info("  OPENHAB_API_TOKEN: Set" if os.environ.get("OPENHAB_API_TOKEN") else "  OPENHAB_API_TOKEN: Not set")
logger.info("  OPENHAB_USERNAME: Set" if os.environ.get("OPENHAB_USERNAME") else "  OPENHAB_USERNAME: Not set")
logger.info("  OPENHAB_PASSWORD: Set" if os.environ.get("OPENHAB_PASSWORD") else "  OPENHAB_PASSWORD: Not set")

OPENHAB_LOG_PATH = os.environ.get("OPENHAB_LOG_PATH")

# Get sensitive variables (not logged)
OPENHAB_API_TOKEN = os.environ.get("OPENHAB_API_TOKEN")
OPENHAB_USERNAME = os.environ.get("OPENHAB_USERNAME")
OPENHAB_PASSWORD = os.environ.get("OPENHAB_PASSWORD")

# Initialize the real OpenHAB client
openhab_client = OpenHABClient(
    base_url=OPENHAB_URL,
    api_token=OPENHAB_API_TOKEN,
    username=OPENHAB_USERNAME,
    password=OPENHAB_PASSWORD,
)

# Initialize MCP after environment is loaded
mcp = FastMCP(
    "OpenHAB MCP Server",
    stateless_http=True,
    host=MCP_HOST,
    port=MCP_PORT,
    log_level=LOG_LEVEL
)
logging.info(f"Starting MCP server on {MCP_HOST}:{MCP_PORT} with CORS enabled")

if not OPENHAB_API_TOKEN and not (OPENHAB_USERNAME and OPENHAB_PASSWORD):
    print(
        "Warning: No authentication credentials found in environment variables.",
        file=sys.stderr,
    )
    print(
        "Set OPENHAB_API_TOKEN or OPENHAB_USERNAME/OPENHAB_PASSWORD in .env file.",
        file=sys.stderr,
    )


# Item Tools
@mcp.tool()
def list_items(
    page: int = Field(
        1,
        description="Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true",
    ),
    page_size: int = Field(15, description="Number of elements shown per page"),
    sort_order: str = Field("asc", description="Sort order", examples=["asc", "desc"]),
    filter_tag: str = Field(
        "",
        description="Optional filter items by tag (either a non-semantic tag or the name of a semantic tag). All available semantic tags can be retrieved from the `list_tags` tool",
        examples=["Location", "Window", "Light", "FrontDoor"],
    ),
    filter_type: str = Field(
        "",
        description="Optional filter items by type",
        examples=["Switch", "Group", "String", "DateTime"],
    ),
    filter_name: str = Field(
        "",
        description="Optional filter items by name. All items that contain the filter value in their name are returned",
        examples=["Kitchen", "LivingRoom", "Bedroom"],
    ),
    filter_fields: List[str] = Field(
        [],
        description="Optional filter items by fields. Item name will always be included by default.",
        examples=["name", "label", "type", "semantic_tags", "non_semantic_tags"],
    ),
    filter_group: str = Field(
        "",
        description="Optional filter items by group name. Returns all members of the group recursively. Use the group item name (e.g. a location like 'Indoor_Room_Bedroom' or an equipment group).",
        examples=["Indoor_Room_Bedroom", "Indoor_Floor_UpperFloor", "loc_house"],
    ),
) -> Dict[str, Any]:
    """
    Gives a list of openHAB items with only basic information. Use this tool
    to get an overview of your items. Use the `get_item_details` tool to get
    more information about a specific item.

    Args:
        page: Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true
        page_size: Number of elements shown per page
        sort_order: Sort order
        filter_tag: Optional filter items by tag (either a non-semantic tag or the name of a semantic tag). All available semantic tags can be retrieved from the `list_tags` tool
        filter_type: Optional filter items by type
        filter_name: Optional filter items by name. All items that contain the filter value in their name are returned
        filter_fields: Optional filter items by fields. Item name will always be included by default.
        filter_group: Optional filter items by group name. Returns all members recursively.
    """
    return openhab_client.list_items(
        page=page,
        page_size=page_size,
        sort_order=sort_order,
        filter_tag=filter_tag,
        filter_type=filter_type,
        filter_name=filter_name,
        filter_fields=filter_fields,
        filter_group=filter_group or None,
    )


@mcp.tool()
def get_item(
    item_name: str = Field(..., description="Name of the item to get details for"),
) -> Dict[str, Any]:
    """
    Gives a detailed description of an openHAB item. Use this tool to get
    information about a specific item.

    Args:
        item_name: Name of the item to get details for
    """
    return openhab_client.get_item(item_name)


@mcp.tool()
def get_create_item_schema() -> Dict[str, Any]:
    """
    Get the JSON schema for creating an item.
    """
    return ItemCreate.model_json_schema()


@mcp.tool()
def create_item(
    item: ItemCreate = Field(..., description="Item details to create"),
) -> Dict[str, Any]:
    """
    Create a new openHAB item

    Args:
        item: Item details to create
    """
    return openhab_client.create_item(item)


@mcp.tool()
def delete_item(
    item_name: str = Field(..., description="Name of the item to delete"),
) -> bool:
    """
    Delete an openHAB item

    Args:
        item_name: Name of the item to delete
    """
    return openhab_client.delete_item(item_name)


@mcp.tool()
def get_item_state(
    item_name: str = Field(..., description="Name of the item to get state for"),
) -> str:
    """
    Get the state of an openHAB item

    Args:
        item_name: Name of the item to get state for
    """
    return openhab_client.get_item_state(item_name)


@mcp.tool()
def update_item_state(
    item_name: str = Field(..., description="Name of the item to update state for"),
    state: str = Field(
        ...,
        description="State to update the item to as string. Type conversion must be possible for the item type",
        examples=[
            "ON",
            "OFF",
            "140.5",
            "14%",
            "20 kWH",
            "2025-06-03T22:21:13.123Z",
            "This is a text",
        ],
    ),
) -> bool:
    """
    Update the state of an openHAB item

    Args:
        item_name: Name of the item to update state for
        state: State to update the item to. Allowed states depend on the item type
    """
    return openhab_client.update_item_state(item_name, state)


@mcp.tool()
def send_command(
    item_name: str = Field(..., description="Name of the item to send command to"),
    command: str = Field(
        ...,
        description="Command to send to the item. Allowed commands depend on the item type",
        examples=[
            "ON",
            "OFF",
            "140.5",
            "14%",
            "20 kWH",
            "2025-06-03T22:21:13.123Z",
            "This is a text",
        ],
    ),
) -> bool:
    """
    Send a command to an openHAB item

    Args:
        item_name: Name of the item to send command to
        command: Command to send to the item. Allowed commands depend on the item type
    """
    return openhab_client.send_command(item_name, command)


@mcp.tool()
def get_item_persistence(
    item_name: str = Field(..., description="Name of the item to get persistence for"),
    start: str = Field(
        ...,
        description="Start time in UTC/Zulu time format [yyyy-MM-dd'T'HH:mm:ss.SSS'Z']",
        examples=["2025-06-03T22:21:13.123Z"],
    ),
    end: str = Field(
        ...,
        description="End time in UTC/Zulu time format [yyyy-MM-dd'T'HH:mm:ss.SSS'Z']",
        examples=["2025-06-03T22:21:13.123Z"],
    ),
) -> Dict[str, Any]:
    """
    Get the persistence values of an openHAB item between start and end in UTC/Zulu time format
    [yyyy-MM-dd'T'HH:mm:ss.SSS'Z']

    Args:
        item_name: Name of the item to get persistence for
        start: Start time in UTC/Zulu time format [yyyy-MM-dd'T'HH:mm:ss.SSS'Z']
        end: End time in UTC/Zulu time format [yyyy-MM-dd'T'HH:mm:ss.SSS'Z']
    """
    return openhab_client.get_item_persistence(item_name, start, end)


# Item Metadata Tools
@mcp.tool()
def get_item_metadata_namespaces(
    item_name: str = Field(
        ..., description="Name of the item to get metadata namespaces for"
    ),
) -> List[str]:
    """
    Get the namespaces of metadata for a specific openHAB item.

    Args:
        item_name: Name of the item to get metadata namespaces for

    Returns:
        List[str]: A list of metadata namespaces

    Raises:
        ValueError: If no item name is provided or item with the given name does not exist
    """
    return openhab_client.get_item_metadata_namespaces(item_name)


@mcp.tool()
def get_item_metadata(
    item_name: str = Field(..., description="Name of the item to get metadata for"),
    namespace: str = Field(..., description="Namespace of the metadata"),
) -> Dict[str, Any]:
    """
    Get the metadata for a specific openHAB item.

    Args:
        item_name: Name of the item to get metadata for
        namespace: Namespace of the metadata

    Returns:
        Dict[str, Any]: The metadata for the item

    Raises:
        ValueError: If no item name is provided or item with the given name does not exist
    """
    return openhab_client.get_item_metadata(item_name, namespace)


@mcp.tool()
def get_item_metadata_schema() -> Dict[str, Any]:
    """
    Get the JSON schema for creating item metadata.
    """
    return ItemMetadata.model_json_schema()


# Links
@mcp.tool()
def list_links(
    page: int = Field(
        1,
        description="Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true",
    ),
    page_size: int = Field(15, description="Number of elements per page"),
    sort_order: str = Field("asc", description="Sort order", examples=["asc", "desc"]),
    item_name: Optional[str] = Field(
        None, description="Optional filter links by item name"
    ),
) -> Dict[str, Any]:
    """
    List all openHAB item to thing links, optionally filtered by item name with pagination.

    Args:
        page: 1-based page number (default: 1)
        page_size: Number of elements per page (default: 50)
        sort_order: Sort order ("asc" or "desc") (default: "asc")
        item_name: Optional filter links by item name (default: None)
    """
    return openhab_client.list_links(page, page_size, sort_order, item_name)


@mcp.tool()
def get_link(
    item_name: str = Field(..., description="Name of the item to get link for"),
    channel_uid: str = Field(..., description="UID of the channel to get link for"),
) -> Optional[Dict[str, Any]]:
    """
    Get a specific openHAB item to thing link by item name and channel UID.

    Args:
        item_name: Name of the item to get link for
        channel_uid: UID of the channel to get link for
    """
    return openhab_client.get_link(item_name, channel_uid)

@mcp.tool()
def get_create_or_update_link_schema() -> Dict[str, Any]:
    """
    Get the JSON schema for creating a link.
    """
    return Link.model_json_schema()

@mcp.tool()
def create_or_update_link(
    link: Link = Field(..., description="Link to create or update")
) -> Dict[str, Any]:
    """
    Create a new openHAB item to thing link or update an existing one.

    Args:
        link: Link to create or update
    """
    return openhab_client.create_or_update_link(link)


@mcp.tool()
def delete_link(
    item_name: str = Field(..., description="Name of the item to delete link for"),
    channel_uid: str = Field(..., description="UID of the channel to delete link for"),
) -> bool:
    """
    Delete an openHAB item to thing link.

    Args:
        item_name: Name of the item to delete link for
        channel_uid: UID of the channel to delete link for
    """
    return openhab_client.delete_link(item_name, channel_uid)


# Thing Tools
@mcp.tool()
def list_bindings() -> List[Dict[str, Any]]:
    """
    Returns all installed bindings with their thing counts.
    Includes bindings with 0 things (useful for triggering discovery).
    Use this to discover valid values for the filter_binding parameter of list_things.
    """
    return openhab_client.list_bindings()


@mcp.tool()
def list_things(
    page: int = Field(
        1,
        description="Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true",
    ),
    page_size: int = Field(15, description="Number of elements per page"),
    sort_order: str = Field("asc", description="Sort order", examples=["asc", "desc"]),
    filter_status: str = Field(
        "",
        description="Optional filter by thing status",
        examples=["ONLINE", "OFFLINE", "UNKNOWN"],
    ),
    filter_binding: str = Field(
        "",
        description="Optional filter by binding ID prefix (the part before the first colon in the thing UID)",
        examples=["shelly", "unifi", "mqtt", "zwave"],
    ),
) -> Dict[str, Any]:
    """
    List openHAB things with basic information with pagination. Use the `get_thing` tool to get
    more information about a specific thing.

    Args:
        page: 1-based page number (default: 1)
        page_size: Number of elements per page (default: 50)
        sort_order: Sort order ("asc" or "desc") (default: "asc")
        filter_status: Optional filter by thing status (e.g. "ONLINE", "OFFLINE")
        filter_binding: Optional filter by binding ID prefix (e.g. "shelly", "unifi", "mqtt")
    """
    return openhab_client.list_things(
        page=page,
        page_size=page_size,
        sort_order=sort_order,
        filter_status=filter_status or None,
        filter_binding=filter_binding or None,
    )


@mcp.tool()
def get_thing(
    thing_uid: str = Field(..., description="UID of the thing to get details for"),
) -> Dict[str, Any]:
    """
    Get the details of a specific openHAB thing by UID.

    Args:
        thing_uid: UID of the thing to get details for
    """
    return openhab_client.get_thing(thing_uid)


@mcp.tool()
def get_create_thing_schema() -> Dict[str, Any]:
    """
    Get the JSON schema for creating a thing.
    """
    return ThingCreate.model_json_schema()


@mcp.tool()
def get_update_thing_schema() -> Dict[str, Any]:
    """
    Get the JSON schema for updating a thing.
    """
    return ThingUpdate.model_json_schema()


@mcp.tool()
def create_thing(
    thing: ThingCreate = Field(..., description="Thing to create"),
) -> Dict[str, Any]:
    """
    Create a new openHAB thing.

    Args:
        thing: Thing to create
    """
    return openhab_client.create_thing(thing)


@mcp.tool()
def update_thing(
    thing: ThingUpdate = Field(..., description="Thing to update"),
) -> Dict[str, Any]:
    """
    Update an existing openHAB thing.

    Args:
        thing: Thing to update
    """
    return openhab_client.update_thing(thing)


@mcp.tool()
def delete_thing(
    thing_uid: str = Field(..., description="UID of the thing to delete"),
) -> bool:
    """
    Delete an openHAB thing.

    Args:
        thing_uid: UID of the thing to delete
    """
    return openhab_client.delete_thing(thing_uid)


@mcp.tool()
def get_thing_channels(
    thing_uid: str = Field(..., description="UID of the thing to get details for"),
    linked_only: bool = Field(
        False, description="If True, only return channels with linked items"
    ),
) -> List[Dict[str, Any]]:
    """
    Get the channels of a specific openHAB thing by UID.

    Args:
        thing_uid: UID of the thing to get details for
        linked_only: If True, only return channels with linked items
    """
    return openhab_client.get_thing_channels(thing_uid, linked_only)


# Rule Tools
@mcp.tool()
def list_rules(
    page: int = Field(
        1,
        description="Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true",
    ),
    page_size: int = Field(15, description="Number of elements per page"),
    sort_order: str = Field("asc", description="Sort order", examples=["asc", "desc"]),
    filter_tag: Optional[str] = Field(
        None, description="Filter rules by tag (default: None)"
    ),
) -> Dict[str, Any]:
    """
    List openHAB rules with basic information with pagination

    Args:
        page: 1-based page number (default: 1)
        page_size: Number of elements per page (default: 15)
        sort_order: Sort order ("asc" or "desc") (default: "asc")
        filter_tag: Filter rules by tag (default: None)
    """
    return openhab_client.list_rules(
        page=page,
        page_size=page_size,
        sort_order=sort_order,
        filter_tag=filter_tag,
    )


@mcp.tool()
def get_rule(
    rule_uid: str = Field(..., description="UID of the rule to get details for"),
) -> Dict[str, Any]:
    """
    Get a specific openHAB rule with more details by UID.

    Args:
        rule_uid: UID of the rule to get details for
    """
    return openhab_client.get_rule(rule_uid)


@mcp.tool()
def get_create_rule_schema() -> dict:
    """
    Get the JSON schema for creating a rule.
    """
    return RuleCreate.model_json_schema()


@mcp.tool()
def get_update_rule_schema() -> dict:
    """
    Get the JSON schema for updating a rule.
    """
    return RuleUpdate.model_json_schema()


@mcp.tool()
def create_rule(
    rule: RuleCreate = Field(..., description="Rule to create")
) -> Dict[str, Any]:
    """
    Create a new openHAB rule.

    Args:
        rule: Rule to create
    """
    return openhab_client.create_rule(rule)


@mcp.tool()
def update_rule(
    rule_updates: RuleUpdate = Field(
        ..., description="Partial updates to apply to the rule"
    ),
) -> Dict[str, Any]:
    """
    Update an existing openHAB rule with partial updates.

    Args:
        rule_updates: Partial updates to apply to the rule
    """
    return openhab_client.update_rule(rule_updates)


@mcp.tool()
def update_rule_script_action(
    rule_uid: str = Field(..., description="UID of the rule to update"),
    action_id: str = Field(..., description="ID of the action to update"),
    script_type: str = Field(..., description="Type of the script"),
    script_content: str = Field(..., description="Content of the script"),
) -> Dict[str, Any]:
    """
    Update a script action in an openHAB rule.

    Args:
        rule_uid: UID of the rule to update
        action_id: ID of the action to update
        script_type: Type of the script
        script_content: Content of the script
    """
    return openhab_client.update_rule_script_action(
        rule_uid, action_id, script_type, script_content
    )


@mcp.tool()
def delete_rule(
    rule_uid: str = Field(..., description="UID of the rule to delete")
) -> bool:
    """
    Delete an openHAB rule.

    Args:
        rule_uid: UID of the rule to delete
    """
    return openhab_client.delete_rule(rule_uid)


@mcp.tool()
def run_rule_now(
    rule_uid: str = Field(..., description="UID of the rule to run")
) -> bool:
    """
    Run a rule immediately

    Args:
        rule_uid: UID of the rule to run
    """
    return openhab_client.run_rule_now(rule_uid)


@mcp.tool()
def set_rule_enabled(
    rule_uid: str = Field(..., description="UID of the rule to enable"),
    enabled: bool = Field(
        ..., description="Whether to enable (True) or disable (False) the rule"
    ),
) -> bool:
    """
    Enable or disable a rule

    Args:
        rule_uid: UID of the rule to enable/disable
        enabled: Whether to enable (True) or disable (False) the rule
    """
    return openhab_client.set_rule_enabled(rule_uid, enabled)


@mcp.tool()
def list_scripts(
    page: int = Field(
        1,
        description="Page number of paginated result set. Page index starts with 1. There are more items when `has_next` is true",
    ),
    page_size: int = Field(15, description="Number of elements per page"),
    sort_order: str = Field("asc", description="Sort order", examples=["asc", "desc"]),
) -> Dict[str, Any]:
    """
    List all openHAB scripts. A script is a rule without a trigger and tag of 'Script'

    Args:
        page: 1-based page number (default: 1)
        page_size: Number of elements per page (default: 15)
        sort_order: Sort order ("asc" or "desc") (default: "asc")
    """
    return openhab_client.list_scripts(
        page=page, page_size=page_size, sort_order=sort_order
    )


@mcp.tool()
def get_script(
    script_id: str = Field(..., description="ID of the script to get details for"),
) -> Dict[str, Any]:
    """
    Get a specific openHAB script with more details by ID. A script is a rule without a trigger and tag of 'Script'

    Args:
        script_id: ID of the script to get details for
    """
    return openhab_client.get_script(script_id)


@mcp.tool()
def create_script(
    script_id: str = Field(..., description="ID of the script to create"),
    script_type: str = Field(..., description="Type of the script"),
    content: str = Field(..., description="Content of the script"),
) -> Dict[str, Any]:
    """
    Create a new openHAB script. A script is a rule without a trigger and tag of 'Script'.

    Args:
        script_id: ID of the script to create
        script_type: Type of the script
        content: Content of the script
    """
    return openhab_client.create_script(script_id, script_type, content)


@mcp.tool()
def update_script(
    script_id: str = Field(..., description="ID of the script to update"),
    script_type: str = Field(..., description="Type of the script"),
    content: str = Field(..., description="Content of the script"),
) -> Dict[str, Any]:
    """
    Update an existing openHAB script. A script is a rule without a trigger and tag of 'Script'.

    Args:
        script_id: ID of the script to update
        script_type: Type of the script
        content: Content of the script
    """
    return openhab_client.update_script(script_id, script_type, content)


@mcp.tool()
def delete_script(
    script_id: str = Field(..., description="ID of the script to delete"),
) -> bool:
    """
    Delete an openHAB script. A script is a rule without a trigger and tag of 'Script'.

    Args:
        script_id: ID of the script to delete
    """
    return openhab_client.delete_script(script_id)


@mcp.tool()
def list_semantic_tags(
    parent_tag_uid: Optional[str] = Field(
        None, description="UID of the parent tag to filter by"
    ),
    category: Optional[str] = Field(
        None,
        description="Category of the tag to filter by",
        examples=["Location", "Equipment", "Point", "Property"],
    ),
) -> List[Dict[str, Any]]:
    """
    List all openHAB tags, optionally filtered by parent tag and category.
    """
    return openhab_client.list_semantic_tags(parent_tag_uid, category)


@mcp.tool()
def get_semantic_tag(
    tag_uid: str = Field(..., description="UID of the tag to get details for"),
    include_subtags: bool = Field(
        False,
        description="Include subtags in the response",
    ),
) -> Union[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Get a specific openHAB tag by uid.

    Args:
        tag_uid: UID of the tag to get details for
    """
    result = openhab_client.get_semantic_tag(tag_uid, include_subtags)
    if result is None:
        return None
    if not include_subtags:
        return result[0] if result else None
    return result


@mcp.tool()
def get_create_semantic_tag_schema() -> dict:
    """
    Get the JSON schema for creating a tag.
    """
    return Tag.model_json_schema()


@mcp.tool()
def create_semantic_tag(
    tag: Tag = Field(..., description="Tag to create")
) -> Dict[str, Any]:
    """
    Create a new openHAB semantic tag.
    Tags can support multiple levels of hierarchy with the pattern 'parent_child'.
    When adding tags to items only the tag name and not the uid is assigned.

    Args:
        tag: Tag to create
    """
    return openhab_client.create_semantic_tag(tag)


@mcp.tool()
def delete_semantic_tag(
    tag_uid: str = Field(..., description="UID of the tag to delete")
) -> bool:
    """
    Delete an openHAB tag.

    Args:
        tag_uid: UID of the tag to delete
    """
    return openhab_client.delete_semantic_tag(tag_uid)


@mcp.tool()
def add_item_semantic_tag(
    item_name: str = Field(..., description="Name of the item to add the tag to"),
    tag_uid: str = Field(..., description="UID of the tag to add"),
) -> bool:
    """
    Add semantic tag to a specific item

    Args:
        item_name: Name of the item to add the tag to
        tag_uid: UID of the tag to add

    Returns:
        bool: True if the tag was added successfully or raises an error

    Raises:
        ValueError: If the item or tag is not found
    """
    return openhab_client.add_item_semantic_tag(item_name, tag_uid)


@mcp.tool()
def remove_item_semantic_tag(
    item_name: str = Field(..., description="Name of the item to remove tag for"),
    tag_uid: str = Field(..., description="UID of the tag to remove"),
) -> bool:
    """
    Remove semantic tag for a specific openHAB item

    Args:
        item_name: Name of the item to remove the tag from
        tag_uid: UID of the tag to remove

    Returns:
        bool: True if the tag was removed successfully or raises an error

    Raises:
        ValueError: If the item or tag is not found
    """
    return openhab_client.remove_item_semantic_tag(item_name, tag_uid)


# Inbox Tools
@mcp.tool()
def list_inbox_things(
    page: int = Field(1, description="Page number (1-based)"),
    page_size: int = Field(15, description="Number of items per page"),
    sort_order: str = Field("asc", description="Sort order (asc or desc)"),
) -> Dict[str, Any]:
    """
    Get a paginated list of discovered things in the inbox

    Args:
        page: Page number (1-based)
        page_size: Number of items per page
        sort_order: Sort order ("asc" or "desc")

    Returns:
        Dictionary containing pagination info and list of inbox items
    """
    return openhab_client.list_inbox_things(
        page=page, page_size=page_size, sort_order=sort_order
    )


@mcp.tool()
def approve_inbox_thing(
    thing_uid: str = Field(..., description="UID of the inbox item to approve"),
    thing_id: str = Field(..., description="ID to assign to the new thing"),
    label: str = Field(..., description="Label for the new thing"),
) -> bool:
    """
    Approve and create a thing from an inbox item

    Args:
        thing_uid: UID of the inbox item
        thing_id: ID to assign to the new thing
        label: Label for the new thing

    Returns:
        bool: True if successful

    Raises:
        ValueError: If the approval fails
    """
    return openhab_client.approve_inbox_thing(thing_uid, thing_id, label)


@mcp.tool()
def ignore_inbox_thing(
    thing_uid: str = Field(..., description="UID of the inbox item to ignore")
) -> bool:
    """
    Mark an inbox item as ignored

    Args:
        thing_uid: UID of the inbox item to ignore

    Returns:
        bool: True if successful

    Raises:
        ValueError: If the operation fails
    """
    return openhab_client.ignore_inbox_thing(thing_uid)


@mcp.tool()
def unignore_inbox_thing(
    thing_uid: str = Field(..., description="UID of the inbox item to unignore")
) -> bool:
    """
    Remove ignore status from an inbox item

    Args:
        thing_uid: UID of the inbox item to unignore

    Returns:
        bool: True if successful

    Raises:
        ValueError: If the operation fails
    """
    return openhab_client.unignore_inbox_thing(thing_uid)


@mcp.tool()
def delete_inbox_thing(
    thing_uid: str = Field(..., description="UID of the inbox item to delete")
) -> bool:
    """
    Delete an item from the inbox

    Args:
        thing_uid: UID of the inbox item to delete

    Returns:
        bool: True if successful

    Raises:
        ValueError: If the deletion fails
    """
    return openhab_client.delete_inbox_thing(thing_uid)


# ===== Admin Inventory & Overview =====

admin_inventory = AdminInventory()


@mcp.tool()
def get_home_overview() -> Dict[str, Any]:
    """Return a compact overview of the entire openHAB model as a group hierarchy.

    The tree starts from root Group items (groups with no parent group) and recurses
    downward. Each node shows:
      - name, label, semantic class (if any)
      - child groups (recursive)
      - items summary: semantic points by type + non-semantic items by openHAB type

    A separate 'no_group' node lists items with no group membership at all.

    Requires refresh_inventory to have been called first.
    This is the recommended first call for orientation — it replaces multiple
    list_items/query_inventory calls for initial discovery.
    """
    return _build_home_overview(admin_inventory)


@mcp.tool()
def save_audit_pattern(
    pattern_id: str = Field(..., description="Unique identifier for this pattern (e.g. 'gf_dimmers')"),
    description: str = Field(..., description="Human-readable description of what the pattern asserts"),
    expect_in_group: str = Field(..., description="Group name that all matching items should be members of"),
    location: Optional[str] = Field(None, description="Location filter (e.g. 'Indoor_Floor_GroundFloor')"),
    equipment: Optional[str] = Field(None, description="Equipment type filter (e.g. 'Lightbulb')"),
    point: Optional[str] = Field(None, description="Point type filter (e.g. 'Control')"),
    item_property: Optional[str] = Field(None, description="Property filter (e.g. 'Temperature')"),
    item_type: Optional[str] = Field(None, description="openHAB item type filter (e.g. 'Switch', 'Dimmer')"),
    group: Optional[str] = Field(None, description="Group membership filter"),
    tag: Optional[str] = Field(None, description="Tag filter"),
    has_semantic: Optional[bool] = Field(None, description="Filter items with/without any semantics"),
    editable: Optional[bool] = Field(None, description="Filter editable/non-editable items"),
) -> Dict[str, Any]:
    """Save an audit pattern asserting that items matching a filter should be in a specific group.

    Use this whenever the user states a membership rule such as:
      "all battery items should be in group X"
      "every light in the ground floor should belong to group Y"
      "these items are missing from that group"

    Patterns are reusable unit tests for the openHAB model. Example:
      "All dimmable lights on the ground floor should be in group gGF_Dimmer"
      → filter: location=Indoor_Floor_GroundFloor, equipment=Lightbulb
      → expect_in_group: gGF_Dimmer

    Run run_audit() to test all saved patterns against the current inventory.
    Missing items and unexpected members are reported as findings.
    """
    return _save_pattern(
        pattern_id=pattern_id,
        description=description,
        expect_in_group=expect_in_group,
        filter_kwargs={
            "location": location, "equipment": equipment, "point": point,
            "item_property": item_property, "item_type": item_type,
            "group": group, "tag": tag, "has_semantic": has_semantic, "editable": editable,
        },
    )


@mcp.tool()
def delete_audit_pattern(
    pattern_id: str = Field(..., description="ID of the pattern to delete"),
) -> Dict[str, Any]:
    """Delete a saved audit pattern."""
    existed = _delete_pattern(pattern_id)
    return {"deleted": pattern_id, "existed": existed}


@mcp.tool()
def list_audit_patterns() -> Dict[str, Any]:
    """List all saved audit patterns."""
    patterns = _list_patterns()
    return {"count": len(patterns), "patterns": patterns}


@mcp.tool()
def run_audit(
    pattern_id: Optional[str] = Field(None, description="Run only this pattern ID. If omitted, all patterns are run."),
) -> Dict[str, Any]:
    """Test audit patterns against the current inventory.

    For each pattern, computes:
      - missing:    items matching the filter that are NOT in the expected group
      - unexpected: members of the expected group that do NOT match the filter

    Requires refresh_inventory to have been called first.
    """
    return _run_audit(admin_inventory, pattern_id=pattern_id)


@mcp.tool()
def refresh_inventory() -> Dict[str, Any]:
    """Reload the admin inventory from openHAB. Call this after bulk item changes
    to ensure diagnose_item and query_inventory reflect current state.

    Returns item count and available semantic filter values.
    """
    raw = openhab_client.get_all_items_raw()
    admin_inventory.build(raw)
    admin_inventory.build_links(openhab_client.get_all_links_raw())
    return {
        "success": True,
        "item_count": admin_inventory.size,
        "available_filters": {
            "locations": admin_inventory.get_available_locations(),
            "equipment": admin_inventory.get_available_equipment(),
            "points": admin_inventory.get_available_points(),
            "properties": admin_inventory.get_available_properties(),
            "item_types": admin_inventory.get_available_types(),
            "categories": admin_inventory.get_available_categories(),
            "metadata_namespaces": admin_inventory.get_available_metadata_namespaces(),
        },
    }


@mcp.tool()
def query_inventory(
    location: Optional[str] = Field(None, description="Semantic location type, e.g. 'Indoor_Room_LivingRoom' or just 'LivingRoom'"),
    equipment: Optional[str] = Field(None, description="Semantic equipment type, e.g. 'LightSource'"),
    point: Optional[str] = Field(None, description="Semantic point type, e.g. 'Control_Switch'"),
    item_property: Optional[str] = Field(None, description="Semantic property, e.g. 'Temperature'"),
    item_type: Optional[str] = Field(None, description="openHAB item type, e.g. 'Switch', 'Dimmer', 'Number'"),
    group: Optional[str] = Field(None, description="Group name — returns members of this group"),
    tag: Optional[str] = Field(None, description="Tag name — returns items carrying this tag"),
    state: Optional[str] = Field(None, description="Exact state value filter"),
    category: Optional[str] = Field(None, description="UI icon category, e.g. 'window', 'light', 'temperature'"),
    has_metadata_ns: Optional[str] = Field(None, description="Only items that HAVE this metadata namespace, e.g. 'alexa', 'listWidget'"),
    missing_metadata_ns: Optional[str] = Field(None, description="Only items that DON'T HAVE this metadata namespace — useful for audit"),
    has_semantic: Optional[bool] = Field(None, description="True = only items with semantic metadata"),
    missing_semantic: Optional[bool] = Field(None, description="True = only items WITHOUT semantic metadata — useful for audit"),
    editable: Optional[bool] = Field(None, description="True = only items editable via REST API; False = only read-only items from .items files"),
) -> Dict[str, Any]:
    """Query all openHAB items using semantic or admin filters.

    Unlike list_items (which paginates the raw REST API), this queries the
    in-memory AdminInventory — fast, combinable filters, no pagination needed.

    Call refresh_inventory first if the inventory may be stale.

    Examples:
    - All lights in living room: equipment='LightSource', location='LivingRoom'
    - All Switch items without semantic tag: item_type='Switch', missing_semantic=True
    - All members of a group: group='gAllLights'
    - All items with Alexa but missing listWidget: has_metadata_ns='alexa', missing_metadata_ns='listWidget'
    - All window items without category set: item_type='Contact', missing_metadata_ns='alexa'
    """
    if admin_inventory.size == 0:
        return {
            "error": "Inventory is empty — call refresh_inventory first.",
            "item_count": 0,
            "items": [],
        }

    names = admin_inventory.get(
        location=location,
        equipment=equipment,
        point=point,
        item_property=item_property,
        item_type=item_type,
        group=group,
        tag=tag,
        state=state,
        category=category,
        has_metadata_ns=has_metadata_ns,
        missing_metadata_ns=missing_metadata_ns,
        has_semantic=has_semantic,
        missing_semantic=missing_semantic,
        editable=editable,
    )

    items = []
    for name in sorted(names):
        raw = admin_inventory.get_item(name)
        if raw:
            sem = raw.get("metadata", {}).get("semantics", {})
            items.append({
                "name": raw.get("name"),
                "label": raw.get("label"),
                "type": raw.get("type"),
                "state": raw.get("state"),
                "category": raw.get("category") or None,
                "groups": raw.get("groupNames", []),
                "tags": raw.get("tags", []),
                "metadata_namespaces": list(raw.get("metadata", {}).keys()),
                "editable": raw.get("editable", False),
                "semantic_value": sem.get("value") if sem else None,
            })

    return {"item_count": len(items), "items": items}


@mcp.tool()
def update_items(
    patch: Dict[str, Any] = Field(..., description=(
        "JSON Merge Patch (RFC 7396). Present fields replace, null deletes, absent = unchanged. "
        "Supported: label, category, tags, groupNames, metadata ({ns: {value,config} or null}). "
        "tags_remove: [...] removes specific tags without touching others (use instead of null which clears all). "
        "groups_remove: [...] removes from specific groups without touching others. "
        "metadata.semantics is rejected — use tags/groups to change semantic class."
    )),
    dry_run: bool = Field(True, description="If True, return the plan without making changes (default: True)"),
    merge: bool = Field(True, description=(
        "True (default): tags/groupNames in patch are ADDED to existing (union). "
        "False: tags/groupNames REPLACE existing values entirely. "
        "null in patch always clears, regardless of merge mode. "
        "Scalar fields (label, category) are always replaced."
    )),
    location: Optional[str] = Field(None, description="Semantic location filter, e.g. 'Indoor_Room_Kitchen'"),
    equipment: Optional[str] = Field(None, description="Semantic equipment filter, e.g. 'Screen_Television'"),
    point: Optional[str] = Field(None, description="Semantic point filter, e.g. 'Control_Switch'"),
    item_property: Optional[str] = Field(None, description="Semantic property filter, e.g. 'Light'"),
    item_type: Optional[str] = Field(None, description="Item type filter, e.g. 'Switch', 'Dimmer'"),
    group: Optional[str] = Field(None, description="Group membership filter"),
    tag: Optional[str] = Field(None, description="Tag filter"),
    state: Optional[str] = Field(None, description="Exact state value filter"),
    category: Optional[str] = Field(None, description="Category filter, e.g. 'light'"),
    has_metadata_ns: Optional[str] = Field(None, description="Only items that have this metadata namespace"),
    missing_metadata_ns: Optional[str] = Field(None, description="Only items that lack this metadata namespace"),
    has_semantic: Optional[bool] = Field(None, description="True = only items with semantic metadata"),
    missing_semantic: Optional[bool] = Field(None, description="True = only items without semantic metadata"),
    editable: Optional[bool] = Field(None, description="True = only REST-editable items"),
    item_names: Optional[List[str]] = Field(None, description="Explicit list of item names (can be combined with other filters)"),
) -> Dict[str, Any]:
    """Apply a JSON Merge Patch to all items matching the given filters.

    This is the primary admin update tool — use it for single items (item_names=["x"])
    or bulk operations (item_type="Switch", missing_metadata_ns="listWidget").

    Always call with dry_run=True first to review what will change.

    Patch examples:
      Set category:         {"category": "lightbulb"}
      Add metadata:         {"metadata": {"listWidget": {"value": "oh-label-card", "config": {}}}}
      Remove metadata:      {"metadata": {"expire": null}}
      Set label + category: {"label": "An/Aus", "category": "light"}
      Replace tags:         {"tags": ["Switch", "Light", "Calculation"]}
      Remove one tag:       {"tags_remove": ["OldTag"]}
      Remove from group:    {"groups_remove": ["old_group"]}
      Clear category:       {"category": null}
    """
    if admin_inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first.", "matched": 0}
    try:
        return _update_items(
            patch=patch,
            inventory=admin_inventory,
            client=openhab_client,
            dry_run=dry_run,
            merge=merge,
            location=location,
            equipment=equipment,
            point=point,
            item_property=item_property,
            item_type=item_type,
            group=group,
            tag=tag,
            state=state,
            category=category,
            has_metadata_ns=has_metadata_ns,
            missing_metadata_ns=missing_metadata_ns,
            has_semantic=has_semantic,
            missing_semantic=missing_semantic,
            editable=editable,
            item_names=item_names,
        )
    except Exception as e:
        return {"error": str(e)}


# ===== Diagnose =====


@mcp.tool()
def diagnose_item(
    item_name: str = Field(..., description="Name of the item to diagnose"),
) -> Dict[str, Any]:
    """Impact analysis for an item: what rules, UI components, and channel links reference it.

    Use this BEFORE renaming or deleting an item to understand what would break.
    The impact_summary tells you which references can be auto-updated and which
    need manual review (e.g. item names inside JavaScript rule scripts).

    Mirrors the openHAB Developer Sidebar's cross-entity search.
    """
    try:
        return _diagnose_item(item_name, openhab_client)
    except Exception as e:
        return {"error": str(e), "item_name": item_name}


@mcp.tool()
def rename_item(
    old_name: str = Field(..., description="Current item name"),
    new_name: str = Field(..., description="Target item name (must not already exist)"),
    dry_run: bool = Field(True, description="If True, return the plan without making changes (default: True)"),
    skip_script_rule_uids: Optional[List[str]] = Field(
        None,
        description="Rule UIDs whose script bodies should NOT be auto-patched. "
                    "These rules are flagged for manual follow-up via update_rule_script_action.",
    ),
) -> Dict[str, Any]:
    """Rename an item and update all its references across rules and UI.

    Workflow:
      1. Call diagnose_item(old_name) to review what will be affected.
      2. Call rename_item(old_name, new_name, dry_run=True) to see the full plan.
      3. Review script_excerpts in the plan — decide which rule UIDs to skip.
      4. Call rename_item(old_name, new_name, dry_run=False) to execute.

    What is updated automatically:
      - Structured rule configs (triggers/conditions/actions with itemName fields)
      - Script bodies where the item name appears as a word (unless rule UID is skipped)
      - UI pages and widgets (JSON search-replace)
      - Channel links (copied to new item, old links removed with old item)
      - Item metadata (all namespaces except semantics, which comes from tags/groups)

    What needs manual follow-up (flagged in manual_review_required):
      - Script bodies in rules listed in skip_script_rule_uids

    The old item is deleted last, after all references are updated.
    """
    try:
        return _rename_item(
            old_name=old_name,
            new_name=new_name,
            client=openhab_client,
            dry_run=dry_run,
            skip_script_rule_uids=skip_script_rule_uids,
        )
    except Exception as e:
        return {"error": str(e), "old_name": old_name, "new_name": new_name}


@mcp.tool()
def manage_logs(
    action: str = Field(
        ...,
        description="Action to perform: list_loggers | set_level | reset_level | read",
    ),
    logger_name: Optional[str] = Field(
        None,
        description="Logger name (e.g. 'org.openhab.binding.zwave'). "
                    "Used by: set_level, reset_level. For list_loggers: optional substring filter.",
    ),
    level: Optional[str] = Field(
        None,
        description="Log level: ERROR | WARN | INFO | DEBUG | TRACE. Used by: set_level.",
    ),
    log_type: str = Field(
        "openhab",
        description="Log file to read: 'openhab' (default) or 'events'. Used by: read.",
    ),
    since: Optional[str] = Field(
        None,
        description="Start of time window. ISO datetime ('2024-01-15T10:00:00') or relative ('1h', '30m', '2d'). Used by: read.",
    ),
    until: Optional[str] = Field(
        None,
        description="End of time window. ISO datetime or relative. Defaults to now. Used by: read.",
    ),
    query: Optional[str] = Field(
        None,
        description="Case-insensitive text filter applied to each log line. Used by: read.",
    ),
    level_filter: Optional[str] = Field(
        None,
        description="Filter lines by log level: ERROR | WARN | INFO | DEBUG | TRACE. Used by: read.",
    ),
    max_lines: int = Field(
        200,
        description="Maximum number of lines to return (default 200, max 1000). Used by: read.",
    ),
) -> Dict[str, Any]:
    """Manage openHAB loggers and read log files.

    Logger management (via REST API — always available):
      list_loggers  — list all configured loggers; logger_name filters by substring
      set_level     — set log level for a logger (creates it if not configured)
      reset_level   — remove logger override (inherits from parent)

    Log reading (requires OPENHAB_LOG_PATH env var + volume mount):
      read          — read openhab.log or events.log with optional time window,
                      text query, and level filter.

    To enable log reading, mount the openHAB log directory into the container and
    set OPENHAB_LOG_PATH=/path/to/logs in your .env file. Example docker-compose:
      volumes:
        - /opt/openhab/userdata/logs:/app/logs:ro
      environment:
        - OPENHAB_LOG_PATH=/app/logs
    """
    if action == "list_loggers":
        try:
            return {"loggers": openhab_client.list_loggers(name_filter=logger_name)}
        except Exception as e:
            return {"error": str(e)}

    elif action == "set_level":
        if not logger_name:
            return {"error": "logger_name is required for set_level"}
        if not level:
            return {"error": "level is required for set_level (ERROR|WARN|INFO|DEBUG|TRACE)"}
        if level.upper() not in {"ERROR", "WARN", "INFO", "DEBUG", "TRACE", "OFF"}:
            return {"error": f"Invalid level '{level}'. Use: ERROR, WARN, INFO, DEBUG, TRACE, OFF"}
        try:
            return openhab_client.set_logger_level(logger_name, level)
        except Exception as e:
            return {"error": str(e)}

    elif action == "reset_level":
        if not logger_name:
            return {"error": "logger_name is required for reset_level"}
        try:
            openhab_client.reset_logger(logger_name)
            return {"reset": logger_name, "note": "Logger removed — inherits level from parent"}
        except Exception as e:
            return {"error": str(e)}

    elif action == "read":
        if not OPENHAB_LOG_PATH:
            return {
                "error": "OPENHAB_LOG_PATH is not configured.",
                "hint": (
                    "Mount the openHAB log directory into the container and set "
                    "OPENHAB_LOG_PATH in your .env file. Example:\n"
                    "  volumes:\n"
                    "    - /opt/openhab/userdata/logs:/app/logs:ro\n"
                    "  environment:\n"
                    "    - OPENHAB_LOG_PATH=/app/logs"
                ),
            }
        try:
            return _read_log(
                log_path=OPENHAB_LOG_PATH,
                log_type=log_type,
                since=since,
                until=until,
                query=query,
                level=level_filter,
                max_lines=max_lines,
            )
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unknown action '{action}'. Use: list_loggers | set_level | reset_level | read"}


@mcp.tool()
def get_thing_context(
    thing_uid: str = Field(
        ...,
        description="UID of the thing (e.g. 'enocean:windowSashHandleSensor:bridge:abc123'). "
                    "Use list_things to find the correct UID.",
    ),
) -> Dict[str, Any]:
    """Get the structural context of a thing: channels, linked items, and position in the semantic model.

    Use this to understand what a thing does and where it lives in the home model —
    not for impact analysis (use diagnose_item for that).

    Returns:
      thing             — basic thing info (UID, label, type, status, configuration)
      thing_channels    — all channels (linked and unlinked) with item types
      linked_channels   — channels with linked items + full item profiles
                          (type, label, semantic tags, group memberships, metadata)
      equipment_context — group hierarchy from the Location anchor down to the items

    Use cases:
      - Understand what a thing does and which items/groups it drives
      - See where a thing fits in the semantic location hierarchy
      - Use as a template when onboarding a new device of the same type
        ("add the new kitchen window sensor exactly like the one in the dining room")
      - Before replace_thing: confirm you have the right thing and understand its links
      - Follow up with diagnose_item on specific linked items to assess rule/UI impact

    Requires refresh_inventory to have been called first.
    """
    if admin_inventory.size == 0:
        return {"error": "Inventory empty — call refresh_inventory first."}
    return _get_thing_context(
        thing_uid=thing_uid,
        client=openhab_client,
        inventory=admin_inventory,
    )


@mcp.tool()
def replace_thing(
    old_thing_uid: str = Field(
        ...,
        description="UID of the thing to be replaced (the broken/old device). Use list_things to find it.",
    ),
    new_thing_uid: str = Field(
        ...,
        description="UID of the replacement thing (the new device, already added to openHAB).",
    ),
    channel_mapping: Optional[Dict[str, str]] = Field(
        None,
        description=(
            "Optional mapping of old channel IDs to new channel IDs, for cases where "
            "the channel IDs differ between old and new thing (different firmware or model variant). "
            "Example: {'status': 'power_state'} maps channel 'status' → 'power_state'. "
            "Omit if channel IDs are identical (same model replacement)."
        ),
    ),
    delete_old_links: bool = Field(
        True,
        description="Delete the links on the old thing after creating new links (default: True).",
    ),
    delete_old_thing: bool = Field(
        False,
        description=(
            "Delete the old thing after re-linking (default: False). "
            "Recommended: leave False, verify everything is ONLINE first, then delete manually."
        ),
    ),
    dry_run: bool = Field(
        True,
        description="If True (default), only show the plan — no changes are made. Always review first.",
    ),
) -> Dict[str, Any]:
    """Re-map all channel links from an old thing to a replacement thing.

    Use this when a device needs to be physically replaced with a new unit
    (e.g. a defective Shelly swapped for an identical model). All Items remain
    unchanged — only the channel links are updated to point to the new thing.
    The old Items keep their names, semantic tags, group memberships, and rules.

    Workflow:
      1. Add the new thing to openHAB (approve from inbox or use create_thing).
      2. Call replace_thing(old_uid, new_uid, dry_run=True) to review the plan.
      3. Call replace_thing(old_uid, new_uid, dry_run=False) to execute.
      4. Verify Items are receiving values (check states, check logs).
      5. Optionally delete the old thing.

    If the new device has different channel IDs (different firmware version or model),
    provide channel_mapping to translate old IDs to new ones.
    """
    try:
        resp = openhab_client.session.get(f"{openhab_client.base_url}/rest/links")
        resp.raise_for_status()
        all_links: List[Dict[str, Any]] = resp.json()
    except Exception as e:
        return {"error": f"Failed to fetch links: {e}"}

    prefix = old_thing_uid + ":"
    old_links = [lnk for lnk in all_links if lnk.get("channelUID", "").startswith(prefix)]

    if not old_links:
        return {
            "error": f"No links found for thing '{old_thing_uid}'.",
            "hint": "Check the thing UID with list_things. Links are matched by channelUID prefix.",
        }

    # Build the re-mapping plan
    plan = []
    for lnk in old_links:
        old_ch_uid = lnk["channelUID"]
        ch_id = old_ch_uid[len(prefix):]
        new_ch_id = (channel_mapping or {}).get(ch_id, ch_id)
        new_ch_uid = f"{new_thing_uid}:{new_ch_id}"
        plan.append({
            "item_name": lnk["itemName"],
            "old_channel_uid": old_ch_uid,
            "new_channel_uid": new_ch_uid,
            "configuration": lnk.get("configuration", {}),
        })

    if dry_run:
        return {
            "dry_run": True,
            "old_thing_uid": old_thing_uid,
            "new_thing_uid": new_thing_uid,
            "links_found": len(plan),
            "will_delete_old_links": delete_old_links,
            "will_delete_old_thing": delete_old_thing,
            "plan": plan,
        }

    # Execute: create new links
    created = []
    errors = []

    for step in plan:
        try:
            openhab_client.session.put(
                f"{openhab_client.base_url}/rest/links"
                f"/{_url_quote(step['item_name'], safe='')}"
                f"/{step['new_channel_uid'].replace('#', '%23')}",
                json={
                    "itemName": step["item_name"],
                    "channelUID": step["new_channel_uid"],
                    "configuration": step["configuration"],
                },
            ).raise_for_status()
            created.append({"item": step["item_name"], "channel": step["new_channel_uid"]})
        except Exception as e:
            errors.append({"item": step["item_name"], "channel": step["new_channel_uid"], "error": str(e)})

    # Delete old links (only items that were successfully re-linked)
    deleted_links = []
    if delete_old_links:
        created_items = {c["item"] for c in created}
        for lnk in old_links:
            if lnk["itemName"] not in created_items:
                continue  # skip if new link failed
            try:
                openhab_client.session.delete(
                    f"{openhab_client.base_url}/rest/links"
                    f"/{_url_quote(lnk['itemName'], safe='')}"
                    f"/{lnk['channelUID'].replace('#', '%23')}",
                ).raise_for_status()
                deleted_links.append(lnk["itemName"])
            except Exception as e:
                errors.append({"delete_link": lnk["itemName"], "error": str(e)})

    # Optionally delete old thing
    thing_deleted = False
    if delete_old_thing and not errors:
        try:
            openhab_client.session.delete(
                f"{openhab_client.base_url}/rest/things/{_url_quote(old_thing_uid, safe='')}",
            ).raise_for_status()
            thing_deleted = True
        except Exception as e:
            errors.append({"delete_thing": old_thing_uid, "error": str(e)})

    return {
        "dry_run": False,
        "old_thing_uid": old_thing_uid,
        "new_thing_uid": new_thing_uid,
        "links_created": len(created),
        "links_deleted": len(deleted_links),
        "thing_deleted": thing_deleted,
        "errors": errors,
        "status": "ok" if not errors else "partial",
    }


@mcp.tool()
def analyze_model_health() -> Dict[str, Any]:
    """Statistical health analysis of the openHAB item model. No configuration needed.

    Runs two analyses automatically derived from the current inventory:

    1. group_membership_anomalies
       Builds a statistical profile for each collection group (item type, name tokens,
       semantic class, co-group memberships, metadata namespaces). Items not in the group
       that closely match the profile are flagged as candidates.
       Score ∈ [0,1]: how well the item matches the average group member.

    2. equipment_completeness
       For each semantic Equipment type (Window, MultiSensor, RollerShutter, …),
       derives the set of semantic points present in ≥60% of instances.
       Equipment groups missing expected points are reported as incomplete.

    Use cases:
      - Find items accidentally left out of aggregation groups
      - Detect equipment with missing sensors/controls compared to similar equipment
      - Identify structural inconsistencies across rooms

    Requires refresh_inventory to have been called first.
    """
    return _analyze_model_health(admin_inventory)


def run_server():
    """Run the MCP server with the configured transport."""
    mcp.run(transport=OPENHAB_MCP_TRANSPORT)

if __name__ == "__main__":
    # This allows running the server directly with: python -m openhab_mcp.openhab_mcp_server
    run_server()
