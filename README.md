[![Version](https://img.shields.io/github/v/release/MSL-DA/brunata_online?label=Version)](https://github.com/MSL-DA/brunata_online/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/pytest.yml?branch=main&label=Tests)](https://github.com/MSL-DA/brunata_online/actions/workflows/pytest.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hassfest.yml?branch=main&label=Hassfest)](https://github.com/MSL-DA/brunata_online/actions/workflows/hassfest.yml)
[![HACS](https://img.shields.io/github/actions/workflow/status/MSL-DA/brunata_online/hacs.yml?branch=main&label=HACS)](https://github.com/MSL-DA/brunata_online/actions/workflows/hacs.yml)

![Brunata logo](images/logo_readme.png)

# Brunata for Home Assistant

The **Brunata Integration** for Home Assistant allows you to monitor your Brunata meters (water, energy, and heat cost allocator) directly in your dashboard. Meters are automatically discovered and grouped under devices for easy management.

Built for [Brunata Online](https://online.brunata.com) accounts. If Brunata Online is available in your country, this integration is expected to work.

> [!IMPORTANT]
> This is a community integration and is not affiliated with or supported by Brunata.

---

## ✨ Features

- Automatic discovery of all meters on your Brunata Online account
- Devices are named after the placement you set in Brunata Online, so a meter shows up as `Water - Bathroom (Cold)` rather than an unrecognisable serial number
- Supports water (`m³`, `l`), energy (`kWh`, `MWh`), and heat cost allocator (`units`) meters
- Groups sensors under devices for easy management
- Standard Home Assistant device classes and state classes, with full Long Term Statistics support
- Reliable polling via `DataUpdateCoordinator`, scheduled every hour at xx:59:30

---

## 📦 Installation

### HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Click the three-dot menu (top right) → **Custom Repositories**
3. Add this repository:
```
https://github.com/MSL-DA/brunata_online
```
4. Set category to: **Integration**
5. Click **Add**
6. Search for **Brunata** in HACS and click **Download**
7. Restart Home Assistant

### Manual Installation

1. Download the `brunata` folder from `custom_components/`
2. Copy it to your Home Assistant's `custom_components/` directory
3. Restart Home Assistant

---

## ⚙️ Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for **Brunata**
4. Enter your Brunata email and password

---

## 📖 Documentation

See the [wiki](https://github.com/MSL-DA/brunata_online/wiki) for details on using the sensors — including which meters work in the Energy dashboard and why consumption graphs show 0 right after setup.

---

## 🔗 Credits

Special thanks to the [brunata-api](https://pypi.org/project/brunata-api/) project, which this integration was originally built on. The client now lives in `custom_components/brunata/api.py`, so the integration has no external Python dependencies.
