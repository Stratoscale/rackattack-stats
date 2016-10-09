import logging


def configure_logger(loggerName="", level=logging.INFO):
    logger = logging.getLogger(loggerName)
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    logger.addHandler(handler)
