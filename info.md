# Seedboxes.cc Integration for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
[![maintainer][maintenance-shield]][maintainer]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

![logo][logoimg]

This custom component integrates [Seedboxes.cc](https://seedboxes.cc/) with Home Assistant.

Some examples of the type of information provided:

- disk quota and usage;
- monthly traffic;
- server IP address;
- torrent client and seedbox status.

**This component will set up the following platforms.**

| Platform | Description |
| --- | --- |
| `sensor` | Seedbox status and usage sensors |

{% if not installed %}
## Installation

1. Select **Download** in HACS and restart Home Assistant.
2. Open **Settings → Devices & services → Add integration**.
3. Search for **Seedboxes.cc**.

{% endif %}

## Configuration

All configuration is carried out in the Home Assistant UI. Enter the
Seedboxes.cc account username and password. The integration discovers the
seedbox ID and session cookie automatically, keeps the cookie up to date, and
uses the saved credentials once when a real session expiry requires renewal.

Automatic username/password sign-in does not support two-factor authentication
(2FA/MFA). If Seedboxes.cc presents Turnstile or another browser verification,
the integration asks for the value of the browser cookie named `session_id`.
It then tries to discover the seedbox ID automatically and requests the ID only
as a fallback.

Home Assistant cannot read a cookie from a separate browser. Never share the
cookie, password, or full Cookie header. See the
[complete installation and security guide](README.md) before configuring the
integration.

<!---->

[logoimg]: https://raw.githubusercontent.com/oOBenjaminOo/ha-seedboxes-cc/main/seedbox_logo.png
[buymecoffee]: https://www.buymeacoffee.com/swartjean
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/oOBenjaminOo/ha-seedboxes-cc.svg?style=for-the-badge
[commits]: https://github.com/oOBenjaminOo/ha-seedboxes-cc/commits/main
[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/swartjean/ha-seedboxes-cc.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-oOBenjaminOo-blue.svg?style=for-the-badge
[maintainer]: https://github.com/oOBenjaminOo
[releases-shield]: https://img.shields.io/github/v/release/oOBenjaminOo/ha-seedboxes-cc?style=for-the-badge
[releases]: https://github.com/oOBenjaminOo/ha-seedboxes-cc/releases
