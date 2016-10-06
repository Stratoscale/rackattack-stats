import logging

ELASTICSEARCH_DB_ADDR = "10.0.1.66"
ELASTICSEARCH_DB_PORT = 9200
TIMEZONE = 'Asia/Jerusalem'


def configure_logger(loggerName="", level=logging.INFO):
    logger = logging.getLogger(loggerName)
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    logger.addHandler(handler)
