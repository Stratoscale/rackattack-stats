import re


class StateMachineScanner:
    STATE_INSIDE_RESULT = 1
    STATE_OUTSIDE_RESULT = 2

    def __init__(self, start_pattern, stop_pattern):
        self._start_pattern = start_pattern
        self._stop_pattern = stop_pattern
        self._patterns = []
        self._current_result = None

    def add_pattern(self, pattern):
        self._patterns.append(pattern)

    def scan(self, content):
        self._initialize_scan()
        for line in content:
            result = self._scan_line(line) 
            if result is not None:
                yield result

    def _initialize_scan(self):
        self._state = self.STATE_OUTSIDE_RESULT

    def _scan_line(self, line):
        result = None
        if self._state == self.STATE_OUTSIDE_RESULT:
            results = re.findall(self._start_pattern, line)
            if results:
                self._current_result = dict(start=results[0])
                self._state = self.STATE_INSIDE_RESULT
        elif self._state == self.STATE_INSIDE_RESULT:
            for pattern in self._patterns:
                results = re.findall(pattern, line)
                if results:
                    self._current_result.setdefault("matches", []).append(results[0])
            results = re.findall(self._stop_pattern, line)
            if results:
                self._current_result["end"] = results[0]
                self._state = self.STATE_OUTSIDE_RESULT
                result = self._current_result
                self._current_result = None
        return result
