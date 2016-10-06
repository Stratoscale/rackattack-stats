import os
import mock
import logging
import argparse
from rackattack.stats import config
from rackattack.tcp import subscribe
from rackattack.stats import smartscanner
from rackattack.stats.main_allocation_stats import AllocationsHandler
from rackattack.stats.tests.insert_some_records import ElasticsearchDBMock


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("script")
    return parser.parse_args()


def main():
    args = get_args()
    config.configure_logger()
    db = ElasticsearchDBMock()
    if args.script == "allocations":
        monitor = mock.Mock()
        _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
        subscription_mgr = subscribe.Subscribe(amqp_url)
        allocation_handler = AllocationsHandler(subscription_mgr, db, monitor)
        allocation_handler.run()
    elif args.script == "smart":
        smart_scanner = smartscanner.SmartScanner(db)
        smart_scanner.run()
    else:
        raise ValueError(args.script)

if __name__ == "__main__":
    main()
