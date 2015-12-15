import os
import mock
import logging
from rackattack.tcp import subscribe
from rackattack.stats.main_allocation_stats import AllocationsHandler
from rackattack.stats.tests.insert_some_records import ElasticsearchDBMock


def configure_logger():
    loggers = {"": logging.INFO}
    for loggerName, level in loggers.iteritems():
        logger = logging.getLogger(loggerName)
        logger.setLevel(level)
        handler = logging.StreamHandler()
        handler.setLevel(level)
        logger.addHandler(handler)


def main():
    configure_logger()
    db = ElasticsearchDBMock()
    monitor = mock.Mock()
    _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
    subscription_mgr = subscribe.Subscribe(amqp_url)
    allocation_handler = AllocationsHandler(subscription_mgr, db, monitor)
    allocation_handler.run()

if __name__ == "__main__":
    main()
