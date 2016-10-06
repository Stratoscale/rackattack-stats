import os
import time
import yaml
import logging
import subprocess
from rackattack.stats import registry
from rackattack.stats import statemachinescanner


SCAN_INTERVAL_NR_SECONDS = 4
REGISTRY_PATH = "/var/lib/rackattackstats/smartscanner-registry.json"
RACKATTACK_LOGS_PATH = "removeme/"

GENERAL_ATTRIBUTES = {"Model Family": str,
                      "Serial Number": str}
SMART_ATTRIBUTES = {"Power_On_Hours": int,
                    "LBAs_Written": int,
                    "LBAs_Read": int}


class SmartScanner:
    def __init__(self, db):
        self._scan_time_registry = registry.Registry(REGISTRY_PATH)
        self._db = db
        self._state_machine = None
        self._initialize_smart_state_machine()

    def run(self):
        while True:
            self._scan_once()
            time.sleep(SCAN_INTERVAL_NR_SECONDS)

    def _scan_once(self):
        logging.info("Scanning log files...")
        results = self._get_smart_results()
        for result in results:
            if self._is_result_new(result):
                self._insert_to_db(result)
        self._scan_time_registry.flush()

    def _parse_scan_result(self, scan_result):
        parsed_result = dict()
        parsable_time = scan_result["start"][0].split(",")[0]
        parsed_result["scan_time"] = time.strptime(parsable_time, "%Y-%m-%d %H:%M:%S")
        for attribute, value in scan_result["matches"]:
            attr_type = self._get_attr_type(attribute)
            if attr_type == int:
                value = int(value)
            attribute = attribute.lower().replace(" ", "_")
            parsed_result[attribute.lower()] = value
        parsed_result["device"] = scan_result["start"][1]
        return parsed_result

    def _get_raw_results(self):
        pattern = GENERAL_ATTRIBUTES.keys() + SMART_ATTRIBUTES.keys() + \
            ["Reading SMART data from", "SMART Error"]
        cmd = ["egrep", "-ra", "|".join(pattern), RACKATTACK_LOGS_PATH]
        cmd = subprocess.Popen(cmd, stdout=subprocess.PIPE, close_fds=True)
        result = cmd.wait()
        if result != 0:
            return ""
        return cmd.stdout.read()

    def _get_smart_results(self):
        raw_results = self._get_raw_results()
        raw_results = self._group_raw_results_by_server(raw_results)
        for server, results in raw_results.iteritems():
            results = self._state_machine.scan(results)
            for result in results:
                result = self._parse_scan_result(result)
                result["server"] = server
                yield result

    def _group_raw_results_by_server(self, raw_results):
        results = dict()
        for line in raw_results.splitlines():
            line = line.strip()
            if not line:
                continue
            maxsplit = 1
            filename, result = line.split(":", maxsplit)
            server = os.path.basename(filename).split("-serial.txt")[0]
            results.setdefault(server, []).append(result)
        return results
        
    def _is_result_new(self, result):
        scan_time = result["scan_time"]
        server = result["server"]
        prev_scan_time = self._scan_time_registry.read(server)
        if prev_scan_time is None or scan_time > prev_scan_time:
            self._scan_time_registry.write(server, scan_time)
            return True
        return False

    def _insert_to_db(self, result):
        self._db.create(index="smart_results", doc_type="smart_result", body=result)

    def _initialize_smart_state_machine(self):
        start_event_pattern = r"([\d\-]+\s[\d\:\,]+).*Reading SMART data from device (.*)\.\.\."
        start_event_pattern = r"([\d\-]+\s[\d\:\,]+).*Reading SMART data from device (.*)\.\.\."
        end_event_pattern = "SMART Error Log Version"
        self._state_machine = statemachinescanner.StateMachineScanner(
            start_event_pattern, end_event_pattern)
        for attr in GENERAL_ATTRIBUTES:
            pattern = r"(%s):\s+(\S+)" % (attr,)
            self._state_machine.add_pattern(pattern)
        for attr in SMART_ATTRIBUTES:
            pattern = r"(%s)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)" % (attr,)
            self._state_machine.add_pattern(pattern)
    
    def _get_attr_type(self, attribute):
        types = GENERAL_ATTRIBUTES
        types.update(SMART_ATTRIBUTES)
        return types[attribute]
