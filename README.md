[![Version](https://img.shields.io/github/v/release/MSL-DA/brunata_online?label=Version)](https://github.com/MSL-DA/brunata_online/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/pytest.yml?branch=main&label=Tests)](https://github.com/MSL-DA/brunata_online/actions/workflows/pytest.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hassfest.yml?branch=main&label=Hassfest)](https://github.com/MSL-DA/brunata_online/actions/workflows/hassfest.yml)
[![HACS](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hacs.yml?branch=main&label=HACS)](https://github.com/MSL-DA/brunata_online/actions/workflows/hacs.yml)

![Brunata logo](images/logo_readme.png)

# Brunata Online for Home Assistant

The **Brunata Integration** for Home Assistant allows you to monitor your Brunata meters (**water**, **electricity**, **heat cost allocator** and **heat meter**) directly in your dashboard. Every meter on the account is found during setup and gets a device of its own in Home Assistant.

Built for [Brunata Online](https://online.brunata.com) accounts. If Brunata Online is available in your country, this integration is expected to work.

> [!IMPORTANT]
> This is a community integration and is **not** affiliated with or supported by Brunata.

---

## ✨ Features

- Automatic discovery of all supported meters on your Brunata Online account.
- Supports water (`m³`), electricity (`kWh`), heat cost allocator (`units`) and heat meters (energy units such as `kWh`, `MWh` or `GJ`).
- Devices are named after the placement you set in Brunata Online, so a meter shows up as `Water - Bathroom (Cold)` rather than its bare meter ID.
- One device per meter, carrying that meter's sensors.
- Standard Home Assistant device classes and state classes, with full `Long Term Statistics` support.
- Polls Brunata Online once an hour at a fixed time between xx:59:15 and xx:59:44. This time is automatically chosen when the integration is installed and remains the same afterwards. How often a *new* reading actually appears depends on the meter's own reporting interval, not on this schedule.

> [!NOTE]
> If you have a meter that isn't supported, please [open an issue](https://github.com/MSL-DA/brunata_online/issues/new?template=bug_report.yml).

---

## 📦 Installation

**Requires Home Assistant 2025.3 or newer.**

### Marketplace — Home Assistant 2026.11 or newer

1. Open [Brunata in the Marketplace](https://my.home-assistant.io/redirect/marketplace_repository/?owner=MSL-DA&repository=brunata_online&category=integration), or go to **Settings** → **Marketplace** and search for **Brunata**
2. Install the integration
3. Enter your Brunata email and password
4. Restart Home Assistant if you are asked to

### HACS — Home Assistant 2026.10 or older

1. Open **HACS** in Home Assistant
2. Search for **Brunata** in HACS and click **⋮ Download**
3. Restart Home Assistant
4. Go to **Settings** → **Devices & Services**
5. Click **Add Integration**
6. Search for **Brunata**
7. Enter your Brunata email and password

---

## 📖 Documentation

See the [wiki](https://github.com/MSL-DA/brunata_online/wiki) for details on [using the sensors](https://github.com/MSL-DA/brunata_online/wiki/Using-the-sensors) — including which meters work in the Energy dashboard and why consumption graphs show 0 right after setup.
