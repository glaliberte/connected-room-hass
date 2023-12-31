# ConnectedRoom

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![pre-commit][pre-commit-shield]][pre-commit]
[![Black][black-shield]][black]

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]

**ConnectedRoom will allow you to automate your lights to any sports. It also offers text-to-speech notifications for goals, touchdown, game start and more.**

![ConnectedRoom][connected-room-logo]

## Installation

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `connectedroom`.
4. Download _all_ the files from the `custom_components/connectedroom/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant
7. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "ConnectedRoom"

## Create an API key

In order to use ConnectedRoom, you need an API key. To create one, go to https://app.connectedroom.io/api-keys .

If you do not have a ConnectedRoom account, you can create one here: https://app.connectedroom.io/get-started

<!---->

## Credits

This project was generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) template.

Code template was mainly taken from [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint][integration_blueprint] template

---

[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[black]: https://github.com/psf/black
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/glaliberte/connected-room-hass.svg?style=for-the-badge
[commits]: https://github.com/glaliberte/connected-room-hass/commits/main
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[connected-room-logo]: connectedroom-logo-light-outlined.png
[license-shield]: https://img.shields.io/github/license/glaliberte/connected-room-hass.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40glaliberte-blue.svg?style=for-the-badge
[pre-commit]: https://github.com/pre-commit/pre-commit
[pre-commit-shield]: https://img.shields.io/badge/pre--commit-enabled-brightgreen?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/glaliberte/connected-room-hass.svg?style=for-the-badge
[releases]: https://github.com/glaliberte/connected-room-hass/releases
[user_profile]: https://github.com/glaliberte
