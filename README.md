[![Version](https://img.shields.io/github/v/release/MSL-DA/brunata_online?label=Version)](https://github.com/MSL-DA/brunata_online/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/pytest.yml?branch=main&label=Tests)](https://github.com/MSL-DA/brunata_online/actions/workflows/pytest.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hassfest.yml?branch=main&label=Hassfest)](https://github.com/MSL-DA/brunata_online/actions/workflows/hassfest.yml)
[![HACS](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hacs.yml?branch=main&label=HACS)](https://github.com/MSL-DA/brunata_online/actions/workflows/hacs.yml)

![Brunata logo](images/logo_readme.png)

# Brunata Online for Home Assistant

The **Brunata Integration** for Home Assistant allows you to monitor your Brunata meters (**water**, **heat cost allocator**, **electricity** and **heat meter**) directly in your dashboard. Meters are automatically discovered and grouped under devices for easy management.

Built for [Brunata Online](https://online.brunata.com) accounts. If Brunata Online is available in your country, this integration is expected to work.

> [!IMPORTANT]
> This is a community integration and is **not** affiliated with or supported by Brunata.

---

## ✨ Features

- Automatic discovery of all supported meters on your Brunata Online account.
- Supports water (`m³`), electricity (`kWh`), heat cost allocator (`units`) and heat meters (energy units such as `kWh`, `MWh` or `GJ`).
- Devices are named after the placement you set in Brunata Online, so a meter shows up as `Water - Bathroom (Cold)` rather than its bare meter ID.
- Groups sensors under devices for easy management.
- Standard Home Assistant device classes and state classes, with full `Long Term Statistics` support.
- Polls Brunata Online once an hour at a fixed time between xx:58:30 and xx:59:29. This time is automatically chosen when the integration is installed and remains the same afterwards. How often a *new* reading actually appears depends on the meter's own reporting interval, not on this schedule.

> [!NOTE]
> If you have a meter that isn't supported, please [open an issue](https://github.com/MSL-DA/brunata_online/issues/new?template=bug_report.yml).

---

## 📦 Installation

**Requires Home Assistant 2025.3 or newer.**

<a href="https://my.home-assistant.io/redirect/marketplace_repository/?owner=MSL-DA&repository=brunata_online" target="_blank" rel="noreferrer noopener"><img src="https://my.home-assistant.io/badges/marketplace_repository.svg" alt="Open your Home Assistant instance and open a repository in the Marketplace." /></a>

---

## ⚙️ Configuration

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=brunata" target="_blank" rel="noreferrer noopener"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Open your Home Assistant instance and start setting up a new integration." /></a>

---

## 📖 Documentation

See the [wiki](https://github.com/MSL-DA/brunata_online/wiki) for details on [using the sensors](https://github.com/MSL-DA/brunata_online/wiki/Using-the-sensors) — including which meters work in the Energy dashboard and why consumption graphs show 0 right after setup.
