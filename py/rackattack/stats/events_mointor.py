import time
import signal


class EventsMonitor:
    def __init__(self, max_nr_seconds_without_events_before_alerting, alert_info_func, alert_warn_func):
        self._max_nr_seconds_without_events_before_alerting = max_nr_seconds_without_events_before_alerting
        signal.signal(signal.SIGALRM, self._handle_alarm)
        self._time_of_last_event = None
        self._is_no_events_mode_on = False
        self._alert_info_func = alert_info_func
        self._alert_warn_func = alert_warn_func

    def start(self):
        self._time_of_last_event = time.time()
        signal.alarm(self._max_nr_seconds_without_events_before_alerting)

    def an_event_has_occurred(self):
        if self._is_no_events_mode_on:
            msg = "An event has occurred again now (first one since {})" \
                  .format(time.ctime(self._time_of_last_event))
            self._alert_info_func(msg)
            self._is_no_events_mode_on = False
            signal.alarm(self._max_nr_seconds_without_events_before_alerting)
        self._time_of_last_event = time.time()

    def _handle_alarm(self, *args):
        nr_seconds_passed_since_last_event = time.time() - self._time_of_last_event
        if nr_seconds_passed_since_last_event >= self._max_nr_seconds_without_events_before_alerting:
            msg = "No events since {}.".format(time.ctime(self._time_of_last_event))
            self._alert_warn_func(msg)
            self._is_no_events_mode_on = True
        else:
            nr_seconds_till_timeout = int(self._max_nr_seconds_without_events_before_alerting -
                                          nr_seconds_passed_since_last_event + 1)
            signal.alarm(nr_seconds_till_timeout)
