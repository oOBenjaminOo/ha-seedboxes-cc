"""Constants for the Seedboxes.cc integration."""

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform

NAME = "Seedboxes.cc"
DOMAIN = "seedboxes_cc"
VERSION = "2.0.0-beta.3"

ISSUE_URL = "https://github.com/oOBenjaminOo/ha-seedboxes-cc/issues"

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONF_SEEDBOX_ID = "seedbox_id"
CONF_SCAN_PERIOD = "scan_period"

DEFAULT_SCAN_PERIOD = 900
MIN_SCAN_PERIOD = 300
DEFAULT_NAME = "Seedbox"

NAME_STATUS = "Status"
NAME_DISK_QUOTA_FREE = "Disk Quota Free"
NAME_DISK_QUOTA_USED = "Disk Quota Used"
NAME_DISK_QUOTA_USED_PCT = "Disk Quota Used Percent"
NAME_MONTHLY_TRAFFIC = "Monthly Traffic"
NAME_DISK_SIZE = "Disk Size"
NAME_IP_ADDRESS = "IP Address"
NAME_TORRENT_CLIENT = "Torrent Client"

SENSOR_UNITS = {
    NAME_DISK_QUOTA_FREE: "GB",
    NAME_DISK_QUOTA_USED: "GB",
    NAME_DISK_QUOTA_USED_PCT: "%",
    NAME_MONTHLY_TRAFFIC: "GB",
    NAME_DISK_SIZE: "GB",
}

SENSOR_ICONS = {
    NAME_STATUS: "mdi:cloud",
    NAME_DISK_QUOTA_FREE: "mdi:harddisk",
    NAME_DISK_QUOTA_USED: "mdi:harddisk",
    NAME_DISK_QUOTA_USED_PCT: "mdi:harddisk",
    NAME_MONTHLY_TRAFFIC: "mdi:upload-network",
    NAME_DISK_SIZE: "mdi:harddisk",
}

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
Welcome to the Seedboxes.cc integration!
If you have any issues, open one here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
