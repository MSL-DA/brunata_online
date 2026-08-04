# Brunata Home Assistant Integration

The **Brunata Integration** for Home Assistant allows you to monitor your Brunata meters (water, energy, and heat cost allocator) directly in your dashboard. Meters are automatically discovered and grouped under devices for easy management.

The integration is designed for Brunata Online accounts. If [Brunata Online](https://online.brunata.com) is available in your country, this integration is expected to work.

> ⚠️ **NOT OFFICIALLY SUPPORTED BY BRUNATA**

---

## ✨ Features

- Automatically discovers your Brunata meters
- Supports water (`m³`, `l`), energy (`kWh`, `MWh`) and heat cost allocator (`units`) meters
- Groups sensors under devices for easy management
- Uses standard Home Assistant device classes and state classes (Long Term Statistics supported)
- Reliable data fetching using `DataUpdateCoordinator`

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

## 🔗 Credits

Special thanks to the [brunata-api](https://pypi.org/project/brunata-api/) project for providing the Python library that makes this integration possible.

This project is a derivative work based on
https://github.com/vondk/brunata_hacs

The original project was created by vondk and is licensed under the MIT License.
This repository contains modifications and additional functionality by MSL-DA while
continuing to be distributed under the MIT License.

---

## 📄 License

MIT License

See [LICENSE](https://github.com/MSL-DA/brunata_online/blob/main/LICENSE) for details.
