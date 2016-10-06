import time
import logging
import traceback
import elasticsearch
from rackattack.stats import config


DB_RECONNECTION_ATTEMPTS_INTERVAL = 60


is_connected = False


class ElasticsearchDBWrapper:
    def __init__(self, alert_func=None):
        self._alert_func = alert_func
        self._db = elasticsearch.Elasticsearch([{"host": config.ELASTICSEARCH_DB_ADDR,
                                                 "port": config.ELASTICSEARCH_DB_PORT}])
        self._was_first_connection_attempt_done_yet = False
        self._validate()
        config.configure_logger("elasticsearch.trace", logging.WARNING)

    def create(self, *args, **kwargs):
        self._db.create(*args, **kwargs)

    def update(self, *args, **kwargs):
        self._db.update(*args, **kwargs)

    def handle_disconnection(self):
        msg = "An error occurred while talking to the DB:\n {}. Attempting to reconnect..." \
            .format(traceback.format_exc())
        logging.exception(msg)
        self._alert_func(msg)
        self._validate(db, is_first_reconnection_attempt=False)
        if self._alert_func is not None:
            msg = "Connected to the DB again."
            self._alert_func(msg)

    def _validate(self):
        is_connected = False
        is_reconnection = self._was_first_connection_attempt_done_yet
        db_addr = config.ELASTICSEARCH_DB_ADDR
        db_port = config.ELASTICSEARCH_DB_PORT
        while not is_connected:
            if is_reconnection:
                logging.info("Will try to reconnect again in {} seconds..."
                            .format(DB_RECONNECTION_ATTEMPTS_INTERVAL))
                time.sleep(DB_RECONNECTION_ATTEMPTS_INTERVAL)
                msg = "Reconnecting to the DB (Elasticsearch address: {}:{})...".format(db_addr, db_port)
            else:
                msg = "Connecting to the DB (Elasticsearch address: {}:{})...".format(db_addr, db_port)
            logging.info(msg)
            try:
                db_info = self._db.info()
                logging.info(db_info)
                logging.info("Connected to the DB.")
                is_connected = True
            except elasticsearch.ConnectionError:
                msg = "Failed to connect to the DB."
                logging.exception(msg)
                if not is_reconnection:
                    if self._alert_func is not None:
                        self._alert_func(msg)
                is_reconnection = True
