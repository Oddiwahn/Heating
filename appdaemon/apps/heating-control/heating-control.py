import appdaemon.plugins.hass.hassapi as hass
from enum import Enum
import voluptuous as vol
import voluptuous_helper as vol_help
from datetime import datetime, time, timedelta

"""
Sets the thermostats target temperature and switches heating on and off. Also adds the current temperature and heating mode to the thermostats.
For the documentation see https://github.com/bruxy70/Heating
"""

# Here you can change the modes set in the mode selector (in lower case)
MODE_ON = "on"
MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_ECO = "eco"
MODE_VACATION = "vacation"

HYSTERESIS = 1.0  # Difference between the temperature to turn heating on and off (to avoid frequent switching)
TARGET_OFFSET = 1.5 # Offset in target temperature to force valve more open
MIN_TEMPERATURE = 10  # Always turn on if teperature is below
LOG_LEVEL = "ERROR"

# Other constants - do not change
HVAC_HEAT = "heat"
HVAC_OFF = "off"
ATTR_SWITCH_HEATING = "switch_heating"
ATTR_SOMEBODY_HOME = "somebody_home"
ATTR_HEATING_MODE = "heating_mode"
ATTR_TEMPERATURE_VACATION = "temperature_vacation"
ATTR_ROOM_ENTITIES = "room_entities"
ATTR_NIGHTMODE = "night_mode"
ATTR_TEMPERATURE_DAY = "temperature_day"
ATTR_TEMPERATURE_NIGHT = "temperature_night"
ATTR_SENSOR = "sensor"
ATTR_THERMOSTATS = "thermostats"
ATTR_NAME = "name"
ATTR_CURRENT_TEMP = "current_temperature"
ATTR_TARGET_TEMPERATURE = "current_heating_setpoint"
ATTR_HVAC_MODE = "hvac_mode"
ATTR_HVAC_MODES = "hvac_modes"
ATTR_TEMPERATURE = "temperature"
ATTR_UNKNOWN = "unknown"
ATTR_UNAVAILABLE = "unavailable"


class HeatingControl(hass.Hass):
    def initialize(self):
        """Read all parameters. Set listeners. Initial run"""

        # Configuration validation schema
        ROOM_SCHEMA = vol.Schema(
            {
                vol.Required(ATTR_SENSOR): vol_help.existing_entity_id(self),
                vol.Required(ATTR_NIGHTMODE): vol_help.existing_entity_id(self),
                vol.Required(ATTR_TEMPERATURE_DAY): vol_help.existing_entity_id(self),
                vol.Required(ATTR_TEMPERATURE_NIGHT): vol_help.existing_entity_id(self),
                vol.Required(ATTR_THERMOSTATS): vol.All(
                    vol_help.ensure_list, [vol_help.existing_entity_id(self)]
                ),
            },
        )
        APP_SCHEMA = vol.Schema(
            {
                vol.Required("module"): str,
                vol.Required("class"): str,
                vol.Required(ATTR_ROOM_ENTITIES): vol.All(vol_help.ensure_list, [ROOM_SCHEMA]),
                vol.Required(ATTR_SWITCH_HEATING): vol_help.existing_entity_id(self),
                vol.Required(ATTR_SOMEBODY_HOME): vol_help.existing_entity_id(self),
                vol.Required(ATTR_TEMPERATURE_VACATION): vol_help.existing_entity_id(self),
                vol.Required(ATTR_HEATING_MODE): vol_help.existing_entity_id(self),
            },
            extra=vol.ALLOW_EXTRA,
        )
        __version__ = "0.0.2"  # pylint: disable=unused-variable
        self.__log_level = LOG_LEVEL
        try:
            config = APP_SCHEMA(self.args)
        except vol.Invalid as err:
            self.error(f"Invalid format: {err}", level="ERROR")
            return

        # Read and store configuration
        self.__switch_heating = config.get(ATTR_SWITCH_HEATING)
        self.__room_entities = config.get(ATTR_ROOM_ENTITIES)
        self.__somebody_home = config.get(ATTR_SOMEBODY_HOME)
        self.__heating_mode = config.get(ATTR_HEATING_MODE)
        self.__temperature_vacation = config.get(ATTR_TEMPERATURE_VACATION)

        # Listen to events
        self.listen_state(self.somebody_home_changed, self.__somebody_home)
        self.listen_state(self.heating_changed, self.__switch_heating)
        self.listen_state(
            self.vacation_temperature_changed, self.__temperature_vacation
        )
        self.listen_state(self.mode_changed, self.__heating_mode)
        sensors = []
        thermostats = []
        # Listen to events for temperature sensors and thermostats
        for currEntityGroup in self.__room_entities:
            self.listen_state(self.daynight_changed, currEntityGroup[ATTR_NIGHTMODE])
            self.listen_state(self.target_changed, currEntityGroup[ATTR_TEMPERATURE_DAY])
            self.listen_state(self.target_changed, currEntityGroup[ATTR_TEMPERATURE_NIGHT])
            if currEntityGroup[ATTR_SENSOR] not in sensors:
                sensor = currEntityGroup[ATTR_SENSOR]
                sensors.append(sensor)
                device, entity = self.split_entity(sensor)
                if device == "climate":
                    self.listen_state(self.temperature_changed, sensor, attribute=ATTR_CURRENT_TEMP)
                else:
                    self.listen_state(self.temperature_changed, sensor)

        # Initial update
        self.__update_heating()
        self.__update_thermostats()
        self.log("Ready for action...")

    def mode_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: mode changed on/off/auto/eco/vacation"""
        heating = self.is_heating()
        self.__update_heating()
        if heating == self.is_heating():
            self.log("Heating changed, updating thermostats")
            self.__update_thermostats()

    def heating_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: boiler state changed - update information on thermostats"""
        self.__update_thermostats()

    def vacation_temperature_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: target vacation temperature"""
        if self.get_mode() == MODE_VACATION:
            self.__update_heating()
            self.__update_thermostats()

    def somebody_home_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: house is empty / somebody came home"""
        if new.lower() == "on":
            self.log("Somebody came home.", level=self.__log_level)
        elif new.lower() == "off":
            self.log("Nobody home.", level=self.__log_level)
        self.__update_heating(force=True)
        self.__update_thermostats()

    def thermostat_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: make sure thermostats do not get blank"""
        self.log("thermostat changed")
        if new is None or new == ATTR_UNKNOWN or new == ATTR_UNAVAILABLE:
            self.__update_thermostats(thermostat_entity=entity)

    def temperature_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: target temperature changed"""
        self.log("temperature changed")
        self.__update_heating()
        self.__update_thermostats(sensor_entity=entity)

    def daynight_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: day/night changed"""
        self.__update_heating()
        self.log("updating daynight")
        for currEntityGroup in self.__room_entities:
            if currEntityGroup[ATTR_NIGHTMODE] == entity:
                self.log(f"for sensor {currEntityGroup[ATTR_SENSOR]}")
                self.__update_thermostats(sensor_entity=currEntityGroup[ATTR_SENSOR])

    def target_changed(self, entity, attribute, old, new, kwargs):
        """Event handler: target temperature"""
        self.log("target changed")
        self.__update_heating()
        for currEntityGroup in self.__room_entities:
            if (
                currEntityGroup[ATTR_TEMPERATURE_DAY] == entity
                or currEntityGroup[ATTR_TEMPERATURE_NIGHT] == entity
            ):
                self.__update_thermostats(sensor_entity=currEntityGroup[ATTR_SENSOR])

    def __check_temperature(self) -> (float, bool, bool):
        """Check temperature of all sensors. Are some bellow? Are all above? What is the minimum temperature"""
        some_below = False
        all_above = True
        minimum = None
        vacation_temperature = float(self.get_state(self.__temperature_vacation))
        for currEntityGroup in self.__room_entities:
            device, entity = self.split_entity(currEntityGroup[ATTR_SENSOR])
            if device == "climate":
                sensor_data = self.get_state(currEntityGroup[ATTR_SENSOR], attribute=ATTR_CURRENT_TEMP)
            else:
                sensor_data = self.get_state(currEntityGroup[ATTR_SENSOR])
            if (
                sensor_data is None
                or sensor_data == ATTR_UNKNOWN
                or sensor_data == ATTR_UNAVAILABLE
            ):
                continue
            temperature = float(sensor_data)
            if self.get_mode() == MODE_VACATION:
                target = vacation_temperature
            else:
                target = self.__get_target_room_temp(currEntityGroup)
            if temperature < target:
                all_above = False
            if temperature < (target - HYSTERESIS):
                some_below = True
            if minimum == None or temperature < minimum:
                minimum = temperature
        return minimum, some_below, all_above

    def is_heating(self) -> bool:
        """Is teh boiler heating?"""
        return bool(self.get_state(self.__switch_heating).lower() == "on")

    def is_somebody_home(self) -> bool:
        """Is somebody home?"""
        return bool(self.get_state(self.__somebody_home).lower() == "on")

    def get_mode(self) -> str:
        """Get heating mode off/on/auto/eco/vacation"""
        return self.get_state(self.__heating_mode).lower()

    def __set_heating(self, heat: bool):
        """Set the relay on/off"""
        is_heating = self.is_heating()
        if heat:
            if not is_heating:
                self.log("Turning heating on.", level=self.__log_level)
                self.turn_on(self.__switch_heating)
        else:
            if is_heating:
                self.log("Turning heating off.", level=self.__log_level)
                self.turn_off(self.__switch_heating)

    def __get_target_room_temp(self, currEntityGroup) -> float:
        """Returns target room temparture, based on day/night switch (not considering vacation)"""
        if bool(self.get_state(currEntityGroup[ATTR_NIGHTMODE]).lower() == "off"):
            return float(self.get_state(currEntityGroup[ATTR_TEMPERATURE_DAY]))
        else:
            return float(self.get_state(currEntityGroup[ATTR_TEMPERATURE_NIGHT]))

    def __get_target_temp(self, sensor: str = None, termostat: str = None) -> float:
        """Get target temperature (basd on day/night/vacation)"""
        if self.get_mode() == MODE_VACATION:
            return float(self.get_state(self.__temperature_vacation))
        if sensor is None and termostat is None:
            return None
        for currEntityGroup in self.__room_entities:
            if sensor is not None:
                if currEntityGroup[ATTR_SENSOR] == sensor:
                    return self.__get_target_room_temp(currEntityGroup)
            else:
                if termostat in currEntityGroup[ATTR_THERMOSTATS]:
                    return self.__get_target_room_temp(currEntityGroup)
        return None

    def __update_heating(self, force: bool = False):
        """Turn boiled on/off"""
        minimum, some_below, all_above = self.__check_temperature()
        mode = self.get_mode()

        if minimum < MIN_TEMPERATURE:
            self.__set_heating(True)
            return
        if mode == MODE_ON:
            self.__set_heating(True)
            return
        if mode == MODE_OFF:
            self.__set_heating(False)
            return
        if mode == MODE_AUTO and self.is_somebody_home():
            self.__set_heating(True)
            return
        if force:
            if self.is_somebody_home():
                if not all_above:
                    self.__set_heating(True)
            else:
                if not some_below:
                    self.__set_heating(False)
        else:
            if self.is_heating():
                if all_above:
                    self.__set_heating(False)
            else:
                if some_below:
                    self.__set_heating(True)

    def __update_thermostats(
        self, thermostat_entity: str = None, sensor_entity: str = None
    ):
        """Set the thermostats target temperature, current temperature and heating mode"""
        vacation = self.get_mode() == MODE_VACATION
        vacation_temperature = float(self.get_state(self.__temperature_vacation))

        for currEntityGroup in self.__room_entities:
            if (
                (thermostat_entity is None and sensor_entity is None)
                or (thermostat_entity in currEntityGroup[ATTR_THERMOSTATS])
                or (sensor_entity == currEntityGroup[ATTR_SENSOR])
            ):
                self.log(f"updating sensor {currEntityGroup[ATTR_SENSOR]}")
                # get current temperature
                device, entity = self.split_entity(currEntityGroup[ATTR_SENSOR])
                if device == "climate":
                    current_temperature = self.get_state(currEntityGroup[ATTR_SENSOR], attribute=ATTR_CURRENT_TEMP)
                else:
                    current_temperature = float(self.get_state(currEntityGroup[ATTR_SENSOR]))
                # set new target temperatures
                if vacation:
                    new_target_temperature = float(self.get_state(self.__temperature_vacation))
                    set_temperature = new_target_temperature
                else:
                    set_temperature = self.__get_target_room_temp(currEntityGroup)
                    if current_temperature < (set_temperature - HYSTERESIS):
                        new_target_temperature = set_temperature + TARGET_OFFSET
                    elif current_temperature > set_temperature:
                        new_target_temperature = set_temperature - TARGET_OFFSET
                    else:
                        new_target_temperature = None
                if self.is_heating():
                    new_mode = HVAC_HEAT
                else:
                    new_mode = HVAC_OFF
                for thermostat in currEntityGroup[ATTR_THERMOSTATS]:
                    curr_target_temperature = float(self.get_state(thermostat, attribute=ATTR_TEMPERATURE))
                    if new_target_temperature is None:
                        new_target_temperature = curr_target_temperature
                    curr_mode = self.get_state(thermostat)
                    # only update the thermostat when taget temperature has changed
                    if curr_target_temperature != new_target_temperature:
                        self.log(
                            f"Updating thermostat {thermostat}: "
                            f"with temperature setpoint = {set_temperature} + {set_temperature - new_target_temperature}[C]"
                #            f", and mode = {new_mode}"
                        )
                        self.__set_thermostat(thermostat, new_target_temperature, new_mode)


    def __set_thermostat(
        self, entity_id: str, target_temp: float, mode: str
    ):
        """Set the thermostat attributes and state"""
        if target_temp is None:
            target_temp = self.__get_target_temp(termostat=entity_id)
        if mode is None:
            if self.is_heating():
                mode = HVAC_HEAT
            else:
                mode = HVAC_OFF
        if target_temp is not None and mode is not None:
            attrs = {}
            attrs[ATTR_TEMPERATURE] = target_temp
#            leave this out. Set EITHER mode (on/off) OR temperature
#            attrs[ATTR_HVAC_MODE] = mode
#            self.call_service("climate/set_temperature", entity_id=entity_id, hvac_mode=mode, temperature=target_temp)
            self.call_service("climate/set_temperature", entity_id=entity_id, temperature=target_temp)
#            this is not working since AD4, it deletes everything EXCEPT the new attributes
#            self.set_state(entity_id, state=mode, attributes=attrs) 