"""Support for MQTT sensors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import sensor
from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASSES_SCHEMA,
    ENTITY_ID_FORMAT,
    STATE_CLASSES_SCHEMA,
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_FORCE_UPDATE,
    CONF_NAME,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_VALUE_TEMPLATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import subscription
from .config import MQTT_RO_SCHEMA
from .const import CONF_ENCODING, CONF_QOS, CONF_STATE_TOPIC, PAYLOAD_NONE
from .debug_info import log_messages
from .mixins import (
    MqttAvailability,
    MqttEntity,
    async_setup_entity_entry_helper,
    write_state_on_attr_change,
)
from .models import (
    MqttValueTemplate,
    PayloadSentinel,
    ReceiveMessage,
    ReceivePayloadType,
)
from .schemas import MQTT_ENTITY_COMMON_SCHEMA

_LOGGER = logging.getLogger(__name__)

CONF_EXPIRE_AFTER = "expire_after"
CONF_LAST_RESET_VALUE_TEMPLATE = "last_reset_value_template"
CONF_SUGGESTED_DISPLAY_PRECISION = "suggested_display_precision"

MQTT_SENSOR_ATTRIBUTES_BLOCKED = frozenset(
    {
        sensor.ATTR_LAST_RESET,
        sensor.ATTR_STATE_CLASS,
    }
)

DEFAULT_NAME = "MQTT Sensor"
DEFAULT_FORCE_UPDATE = False

_PLATFORM_SCHEMA_BASE = MQTT_RO_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICE_CLASS): vol.Any(DEVICE_CLASSES_SCHEMA, None),
        vol.Optional(CONF_EXPIRE_AFTER): cv.positive_int,
        vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
        vol.Optional(CONF_LAST_RESET_VALUE_TEMPLATE): cv.template,
        vol.Optional(CONF_NAME): vol.Any(cv.string, None),
        vol.Optional(CONF_SUGGESTED_DISPLAY_PRECISION): cv.positive_int,
        vol.Optional(CONF_STATE_CLASS): vol.Any(STATE_CLASSES_SCHEMA, None),
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): vol.Any(cv.string, None),
    }
).extend(MQTT_ENTITY_COMMON_SCHEMA.schema)


def validate_sensor_state_class_config(config: ConfigType) -> ConfigType:
    """Validate the sensor state class config."""
    if (
        CONF_LAST_RESET_VALUE_TEMPLATE in config
        and (state_class := config.get(CONF_STATE_CLASS)) != SensorStateClass.TOTAL
    ):
        raise vol.Invalid(
            f"The option `{CONF_LAST_RESET_VALUE_TEMPLATE}` cannot be used "
            f"together with state class `{state_class}`"
        )

    return config


PLATFORM_SCHEMA_MODERN = vol.All(
    _PLATFORM_SCHEMA_BASE,
    validate_sensor_state_class_config,
)

DISCOVERY_SCHEMA = vol.All(
    _PLATFORM_SCHEMA_BASE.extend({}, extra=vol.REMOVE_EXTRA),
    validate_sensor_state_class_config,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MQTT sensor through YAML and through MQTT discovery."""
    await async_setup_entity_entry_helper(
        hass,
        config_entry,
        MqttSensor,
        sensor.DOMAIN,
        async_add_entities,
        DISCOVERY_SCHEMA,
        PLATFORM_SCHEMA_MODERN,
    )


class MqttSensor(MqttEntity, RestoreSensor):
    """Representation of a sensor that can be updated using MQTT."""

    _default_name = DEFAULT_NAME
    _entity_id_format = ENTITY_ID_FORMAT
    _attr_last_reset: datetime | None = None
    _attributes_extra_blocked = MQTT_SENSOR_ATTRIBUTES_BLOCKED
    _expiration_trigger: CALLBACK_TYPE | None = None
    _expire_after: int | None
    _expired: bool | None
    _template: Callable[[ReceivePayloadType, PayloadSentinel], ReceivePayloadType]
    _last_reset_template: Callable[[ReceivePayloadType], ReceivePayloadType]

    async def mqtt_async_added_to_hass(self) -> None:
        """Restore state for entities with expire_after set."""
        last_state: State | None
        last_sensor_data: SensorExtraStoredData | None
        if (
            (_expire_after := self._expire_after) is not None
            and _expire_after > 0
            and (last_state := await self.async_get_last_state()) is not None
            and last_state.state not in [STATE_UNKNOWN, STATE_UNAVAILABLE]
            and (last_sensor_data := await self.async_get_last_sensor_data())
            is not None
            # We might have set up a trigger already after subscribing from
            # MqttEntity.async_added_to_hass(), then we should not restore state
            and not self._expiration_trigger
        ):
            expiration_at = last_state.last_changed + timedelta(seconds=_expire_after)
            remain_seconds = (expiration_at - dt_util.utcnow()).total_seconds()

            if remain_seconds <= 0:
                # Skip reactivating the sensor
                _LOGGER.debug("Skip state recovery after reload for %s", self.entity_id)
                return
            self._expired = False
            self._attr_native_value = last_sensor_data.native_value

            self._expiration_trigger = async_call_later(
                self.hass, remain_seconds, self._value_is_expired
            )
            _LOGGER.debug(
                (
                    "State recovered after reload for %s, remaining time before"
                    " expiring %s"
                ),
                self.entity_id,
                remain_seconds,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Remove exprire triggers."""
        # Clean up expire triggers
        if self._expiration_trigger:
            _LOGGER.debug("Clean up expire after trigger for %s", self.entity_id)
            self._expiration_trigger()
            self._expiration_trigger = None
            self._expired = False
        await MqttEntity.async_will_remove_from_hass(self)

    @staticmethod
    def config_schema() -> vol.Schema:
        """Return the config schema."""
        return DISCOVERY_SCHEMA

    def _setup_from_config(self, config: ConfigType) -> None:
        """(Re)Setup the entity."""
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_force_update = config[CONF_FORCE_UPDATE]
        self._attr_suggested_display_precision = config.get(
            CONF_SUGGESTED_DISPLAY_PRECISION
        )
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        self._attr_state_class = config.get(CONF_STATE_CLASS)

        self._expire_after = config.get(CONF_EXPIRE_AFTER)
        if self._expire_after is not None and self._expire_after > 0:
            self._expired = True
        else:
            self._expired = None

        self._template = MqttValueTemplate(
            self._config.get(CONF_VALUE_TEMPLATE), entity=self
        ).async_render_with_possible_json_value
        self._last_reset_template = MqttValueTemplate(
            self._config.get(CONF_LAST_RESET_VALUE_TEMPLATE), entity=self
        ).async_render_with_possible_json_value

    def _prepare_subscribe_topics(self) -> None:
        """(Re)Subscribe to topics."""
        topics: dict[str, dict[str, Any]] = {}

        def _update_state(msg: ReceiveMessage) -> None:
            # auto-expire enabled?
            if self._expire_after is not None and self._expire_after > 0:
                # When self._expire_after is set, and we receive a message, assume
                # device is not expired since it has to be to receive the message
                self._expired = False

                # Reset old trigger
                if self._expiration_trigger:
                    self._expiration_trigger()

                # Set new trigger
                self._expiration_trigger = async_call_later(
                    self.hass, self._expire_after, self._value_is_expired
                )

            payload = self._template(msg.payload, PayloadSentinel.DEFAULT)
            if payload is PayloadSentinel.DEFAULT:
                return
            new_value = str(payload)
            if self._numeric_state_expected:
                if new_value == "":
                    _LOGGER.debug("Ignore empty state from '%s'", msg.topic)
                elif new_value == PAYLOAD_NONE:
                    self._attr_native_value = None
                else:
                    self._attr_native_value = new_value
                return
            if self.device_class in {None, SensorDeviceClass.ENUM}:
                self._attr_native_value = new_value
                return
            try:
                if (payload_datetime := dt_util.parse_datetime(new_value)) is None:
                    raise ValueError
            except ValueError:
                _LOGGER.warning(
                    "Invalid state message '%s' from '%s'", msg.payload, msg.topic
                )
                self._attr_native_value = None
                return
            if self.device_class == SensorDeviceClass.DATE:
                self._attr_native_value = payload_datetime.date()
                return
            self._attr_native_value = payload_datetime

        def _update_last_reset(msg: ReceiveMessage) -> None:
            payload = self._last_reset_template(msg.payload)

            if not payload:
                _LOGGER.debug("Ignoring empty last_reset message from '%s'", msg.topic)
                return
            try:
                last_reset = dt_util.parse_datetime(str(payload))
                if last_reset is None:
                    raise ValueError
                self._attr_last_reset = last_reset
            except ValueError:
                _LOGGER.warning(
                    "Invalid last_reset message '%s' from '%s'", msg.payload, msg.topic
                )

        @callback
        @write_state_on_attr_change(
            self, {"_attr_native_value", "_attr_last_reset", "_expired"}
        )
        @log_messages(self.hass, self.entity_id)
        def message_received(msg: ReceiveMessage) -> None:
            """Handle new MQTT messages."""
            _update_state(msg)
            if CONF_LAST_RESET_VALUE_TEMPLATE in self._config:
                _update_last_reset(msg)

        topics["state_topic"] = {
            "topic": self._config[CONF_STATE_TOPIC],
            "msg_callback": message_received,
            "qos": self._config[CONF_QOS],
            "encoding": self._config[CONF_ENCODING] or None,
        }

        self._sub_state = subscription.async_prepare_subscribe_topics(
            self.hass, self._sub_state, topics
        )

    async def _subscribe_topics(self) -> None:
        """(Re)Subscribe to topics."""
        await subscription.async_subscribe_topics(self.hass, self._sub_state)

    @callback
    def _value_is_expired(self, *_: datetime) -> None:
        """Triggered when value is expired."""
        self._expiration_trigger = None
        self._expired = True
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return true if the device is available and value has not expired."""
        # mypy doesn't know about fget: https://github.com/python/mypy/issues/6185
        return MqttAvailability.available.fget(self) and (  # type: ignore[attr-defined]
            self._expire_after is None or not self._expired
        )
