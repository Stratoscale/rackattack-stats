import mock
import pymongo
import logging
import unittest
import threading
import rackattack
import rackattack.tcp.subscribe
import rackattack.stats.main_allocation_stats


logger = logging.getLogger()


class SubscribeMock(object):
    instances = []
    def __init__(self, unused):
        self.all_allocations_handler = None
        self.allocation_callbacks = dict()
        self.inaugurations_callbacks = dict()
        self.instances.append(self)

    def registerForAllAllocations(self, callback):
        self.all_allocations_handler = callback

    def registerForAllocation(self, idx, callback):
        assert idx not in self.allocation_callbacks
        self.allocation_callbacks[idx] = callback

    def unregisterForAllocation(self, idx):
        del self.allocation_callbacks[idx]

    def registerForInaugurator(self, idx, callback):
        assert idx not in self.inaugurations_callbacks
        self.inaugurations_callbacks[idx] = callback


class Test(unittest.TestCase):
    def setUp(self):
        rackattack.tcp.subscribe.Subscribe = SubscribeMock
        pymongo.MongoClient = mock.Mock()
        self.ready_event = threading.Event()
        self.main_thread= threading.Thread(target=rackattack.stats.main_allocation_stats.main,
                                           args=(self.ready_event,))

    def test_one_allocation(self):
        logger.info("Starting main-allocation-stats's main thread...")
        logger.handlers = list()
        self.main_thread.start()
        logger.info("Waiting for main-allocation-stats' thread to be ready...")
        self.ready_event.wait()
        logger.info("Thread is ready. Inoking callbacks...")
        
        self.main_thread.join()


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    unittest.main()
