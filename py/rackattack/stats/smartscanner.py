import os
import time
import pytz
import yaml
import logging
import datetime
import subprocess
from rackattack.stats import config
from rackattack.stats import registry
from rackattack.stats import statemachinescanner


SCAN_INTERVAL_NR_SECONDS = 60 * 30
REGISTRY_PATH = "/var/lib/rackattackstats/smartscanner-registry.json"
RACKATTACK_LOGS_PATH = "/var/lib/rackattackphysical/seriallogs/"

GENERAL_ATTRIBUTES = {"Model Family": str,
                      "Serial Number": str,
                      "Rotation Rate": str}
SMART_ATTRIBUTES = {
                    241: dict(name="Total_LBAs_Written", _type=int),
                    242: dict(name="Total_LBAs_Read", _type=int),
                    198: dict(name="Offline_Uncorrectable", _type=int),
                    199: dict(name="UDMA_CRC_Error_Count", _type=int),
                    183: dict(name="Runtime_Bad_Block", _type=int),
                    246: dict(name="Unknown_Attribute", _type=int, display="Total_Sectors_Written"),
                   }


def datetime_from_timestamp(timestamp):
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(config.TIMEZONE).localize(datetime_now)
    return datetime_now


class InvalidTime(Exception): pass


class SmartScanner:
    def __init__(self, db):
        self._scan_time_registry = registry.Registry(REGISTRY_PATH)
        self._db = db
        self._state_machine = None
        self._initialize_smart_state_machine()

    def run(self):
        while True:
            logging.info("Scanning log files...")
            self._scan_once()
            nrMinutes = SCAN_INTERVAL_NR_SECONDS / 60
            msg = "Scheduling next scan to %(nrMinutes)s minutes from now." % \
                  dict(nrMinutes=nrMinutes)
            logging.info(msg)
            time.sleep(SCAN_INTERVAL_NR_SECONDS)

    def _scan_once(self):
        nrNewResults = 0
        results = self._get_smart_results()
        for result in results:
            if self._is_result_new(result):
                self._insert_to_db(result)
                nrNewResults += 1
        logging.info("%(nrNewResults)s new results were inserted during this scan cycle.",
                     dict(nrNewResults=nrNewResults))
        self._scan_time_registry.flush()

    def _parse_scan_result(self, scan_result, server):
        parsed_result = dict()
        parsable_time = scan_result["start"][0].split(",")[0]
        try:
            parsed_result["date"] = time.strptime(parsable_time, "%Y-%m-%d %H:%M:%S")
        except:
            logging.warning("Cannot parse scan time: %s" % (str(parsable_time),))
            raise InvalidTime
        for attribute, value in scan_result["matches"]:
            value = value.replace("\0", "")
            attribute = attribute.replace("\0", "")
            if attribute in GENERAL_ATTRIBUTES:
                _type = GENERAL_ATTRIBUTES[attribute]
                display = attribute
            else:
                try:
                    code = int(attribute)
                    _type = SMART_ATTRIBUTES[code]["_type"]
                    name = SMART_ATTRIBUTES[code]["name"]
                    display = SMART_ATTRIBUTES[code].get("display", name)
                except:
                    logging.warning("Invalid attribute '%(attribute)s.",
                                    dict(attribute=attribute))
                    continue
            try:
                value = _type(value)
            except:
                logging.warning("Cannot parse value '%(value)s'. Server: %(server)s"
                                "Attribute: %(attribute)s",
                                dict(server=server, value=value, attribute=attribute))
                logging.warning("Value '%(value)s' cannot be converted to %(_type)s",
                                dict(value=value, _type=_type))
                continue
            display = display.lower().replace(" ", "_")
            parsed_result[display] = value
        parsed_result["device"] = scan_result["start"][1]
        return parsed_result

    def _get_raw_results(self):
        attrs = list(set(GENERAL_ATTRIBUTES.keys()))
        attrs += ["%d %s" % (code, attr["name"]) for (code, attr) in SMART_ATTRIBUTES.iteritems()]
        attrs += ["Reading SMART data from", "SMART Error"]
        cmd = ["egrep", "-ra", "|".join(attrs), RACKATTACK_LOGS_PATH]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, close_fds=True)
        output, error = proc.communicate()
        if proc.returncode == 1:
            return ""
        elif proc.returncode != 0:
            logging.warn("grep failed for an unknown reason. return code: %(returncode)s. Error: %(error)s."
                         " Command: %(cmd)s",
                         dict(returncode=proc.returncode, error=error, cmd=cmd))
        return output

    def _get_smart_results(self):
        raw_results = self._get_raw_results()
        raw_results = self._group_raw_results_by_server(raw_results)
        for server, results in raw_results.iteritems():
            results = self._state_machine.scan(results)
            for result in results:
                try:
                    parsed_result = self._parse_scan_result(result, server)
                except InvalidTime:
                    continue
                parsed_result["server"] = server
                yield parsed_result

    def _group_raw_results_by_server(self, raw_results):
        results = dict()
        for line in raw_results.splitlines():
            line = line.strip()
            if not line:
                continue
            maxsplit = 1
            try:
                filename, result = line.split(":", maxsplit)
            except ValueError:
                logging.error("Cannot split the following line:")
                logging.error(line)
                continue
            server = os.path.basename(filename).split("-serial.txt")[0]
            results.setdefault(server, []).append(result)
        return results
        
    def _is_result_new(self, result):
        scan_time = result["date"]
        server = result["server"]
        prev_scan_time = self._scan_time_registry.read(server)
        if prev_scan_time is None or scan_time > prev_scan_time:
            self._scan_time_registry.write(server, scan_time)
            return True
        return False

    def _insert_to_db(self, result):
        result["date"] = time.mktime(result["date"])
        result["date"] = datetime_from_timestamp(result["date"])
        logging.debug(result)
        self._db.create(index="smart_stats", doc_type="smart_stat", body=result)

    def _initialize_smart_state_machine(self):
        start_event_pattern = r"(\d{4}\-\d{2}-\d{2}\s\d{2}\:\d{2}\:\d{2}\,\d+?) - \w+? - \w+? - Reading SMART data from device (\/dev\/[a-zA-Z]+?)\.\.\."
        end_event_pattern = "SMART Error Log Version"
        self._state_machine = statemachinescanner.StateMachineScanner(
            start_event_pattern, end_event_pattern)
        for attr in GENERAL_ATTRIBUTES:
            pattern = r"(%s):\s+(.+)" % (attr,)
            self._state_machine.add_pattern(pattern)
        for code, attr in SMART_ATTRIBUTES.iteritems():
            pattern = r"(%d) %s\s+?\S+?\s+?\S+?\s+?\S+?\s+?\S+?\s+?\S+?\s+?\S+?\s+?\S+?\s+?(\S+)" \
                      % (code, attr["name"],)
            self._state_machine.add_pattern(pattern)
