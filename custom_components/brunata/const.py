"""Constants for the Brunata integration."""

# CONF_EMAIL and CONF_PASSWORD come from homeassistant.const where they are
# used; they are not redefined here.

DOMAIN = "brunata"

# The prefix of a meter device's identifier, e.g. ("brunata", "brunata_7822808").
# It lives here because it is written in one module and read back in another:
# sensor.py builds it for DeviceInfo, and __init__.py takes a meter id back out
# of it when Home Assistant asks whether a device may be deleted. Two literals
# would be two chances to drift apart, with nothing failing loudly if they did.
DEVICE_ID_PREFIX = "brunata_"
