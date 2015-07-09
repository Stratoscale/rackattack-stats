import mock
import copy
import pymongo
import logging
import unittest
import threading
import rackattack
from rackattack.tcp import subscribe
from rackattack.stats.main_allocation_stats import AllocationsHandler


logger = logging.getLogger()


class SubscribeMock(object):
    instances = []

    def __init__(self, unused):
        self.all_allocations_handler = None
        self.allocations_callbacks = dict()
        self.inaugurations_callbacks = dict()
        self.instances.append(self)
        self.inaugurations_register_wait_conditions = dict()
        self.allocations_wait_conditions = dict()

    def registerForAllAllocations(self, callback):
        self.all_allocations_handler = callback

    def registerForAllocation(self, idx, callback):
        self.allocations_callbacks[idx] = callback
        self.allocations_wait_conditions[idx].set()

    def unregisterForAllocation(self, idx):
        del self.allocations_callbacks[idx]
        self.allocations_wait_conditions[idx].set()

    def registerForInagurator(self, host_id, callback):
        self.inaugurations_callbacks[host_id] = callback
        self.inaugurations_register_wait_conditions[host_id].set()

    def unregisterForInaugurator(self, host_id):
        self.inaugurations_register_wait_conditions[host_id].set()


class Test(unittest.TestCase):
    def setUp(self):
        rackattack.tcp.subscribe.Subscribe = SubscribeMock
        SubscribeMock.instances = []
        self.db = mock.Mock()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.main_thread = threading.Thread(target=rackattack.stats.main_allocation_stats.main,
                                            args=(self.ready_event, self.stop_event))
        self.open_allocations_count = 0
        self.total_allocations_count = 0
        self.available_hosts = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel',
                                'india', 'juliet', 'kilo', 'lima', 'mike', 'november', 'oscar', 'papa',
                                'quebec', 'romeo', 'sierra', 'tango', 'uniform', 'victor', 'whiskey',
                                'xray', 'yankee', 'zooloo']
        for i in xrange(10000):
            self.available_hosts.append('host_{}'.format(i))
        self.assertEquals(len(self.available_hosts), len(set(self.available_hosts)))
        logger.info("Starting main-allocation-stats's main thread...")
        logger.handlers = list()
        subscription_mgr = subscribe.Subscribe("asdasd@@asdasd@@asdasd")
        self.tested = AllocationsHandler(subscription_mgr, self.db, self.ready_event, self.stop_event)
        logger.info("Waiting for allocation handler thread to be ready...")
        self.tested.start()
        self.ready_event.wait()
        logger.info("Thread is ready.")
        self._insert_to_db_mock = self.db.inaugurations.insert
        instances = SubscribeMock.instances
        self.assertEquals(len(instances), 1)
        self.mgr = SubscribeMock.instances[0]
        self.expected_reported_inaugurated_hosts = []
        self.uninaugurated_hosts_of_open_reported_allocations = dict()

    def tearDown(self):
        self.tested.stop()
        self.tested.join()

    def test_one_allocation(self):
        alloc_msg = self.generate_allocation_creation_message(nr_hosts=10)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_with_one_host(self):
        alloc_msg = self.generate_allocation_creation_message(nr_hosts=1)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_with_no_hosts(self):
        alloc_msg = self.generate_allocation_creation_message(nr_hosts=0)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_of_hosts_which_belong_to_another_alive_allocation(self):
        """
        server   | inaugurated | allocation #1 | allocation #2 | Nr. times should be reported
        ---------+-------------+---------------+---------------+-----------------------------
        alpha    | X           | V             | X             | 0
        bravo    | X           | V             | X             | 0
        charlie  | V           | V             | X             | 1
        delta    | V           | V             | X             | 1
        echo     | X           | V             | V             | 0
        foxtrot  | X           | V             | V             | 0
        golf     | on both     | V             | V             | 2
        hotel    | on both     | V             | V             | 2
        india    | on #1       | V             | V             | 1
        juliet   | on #1       | V             | V             | 1
        kilo     | on #2       | V             | V             | 1
        lima     | on #2       | V             | V             | 1
        mike     | X           | X             | V             | 0
        november | X           | X             | V             | 0
        oscar    | V           | X             | V             | 1
        papa     | V           | X             | V             | 1
        """
        nr_hosts_in_allocation = 12
        original_hosts_pool = copy.copy(self.available_hosts)
        alloc_msg = self.generate_allocation_creation_message(nr_hosts=nr_hosts_in_allocation)
        self.generate_allocation_creation_flow(alloc_msg)
        inaugurated_hosts_in_first_allocation = ("charlie", "delta", "golf", "hotel", "india", "juliet")
        for host in inaugurated_hosts_in_first_allocation:
            self.generate_inauguration_flow_for_single_host(host)
        hosts_on_both_allocations = original_hosts_pool[4:4 + nr_hosts_in_allocation]
        self.available_hosts = hosts_on_both_allocations + self.available_hosts
        another_alloc_msg = self.generate_allocation_creation_message(nr_hosts_in_allocation)
        self.prepare_wait_events_for_allocation_unregisteration(alloc_msg)
        self.generate_allocation_creation_flow(another_alloc_msg)
        # At this stage, the tested unit should figure that a new allocation uses hosts of an older
        # allocation, so it should unsubscribe from the old allocation.
        self.wait_for_allocation_unregisteration(alloc_msg)
        inaugurated_hosts_in_second_allocation = ("golf", "hotel", "kilo", "lima", "oscar", "papa")
        for host in inaugurated_hosts_in_second_allocation:
            self.generate_inauguration_flow_for_single_host(host)
        self.generate_allocation_death_flow(another_alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_inauguration_done_for_an_unallocated_host(self):
        alloc_msg = self.generate_allocation_creation_message()
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        host_id = alloc_msg["allocated"].values()[0]
        inauguration_callback = self.mgr.inaugurations_callbacks[host_id]
        self.generate_allocation_death_flow(alloc_msg)
        done_message = dict(id=host_id, status='done')
        inauguration_callback(done_message)
        self.validate_db()
        self.validate_open_registerations()
        # TODO: before validations of registerations, queue should be empty

    def test_status_message_for_an_unknown_allocation(self):
        alloc_msg = self.generate_allocation_creation_message(nr_hosts=10)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        allocation_id = alloc_msg["allocationID"]
        allocation_callback_backup = self.mgr.allocations_callbacks[allocation_id]
        self.generate_allocation_death_flow(alloc_msg)
        self.mgr.allocations_callbacks[allocation_id] = allocation_callback_backup
        self.send_allocation_death_message(allocation_id)
        del self.mgr.allocations_callbacks[allocation_id]
        self.validate_db()
        self.validate_open_registerations()

    def test_too_many_open_allocations(self):
        for i in xrange(rackattack.stats.main_allocation_stats.MAX_NR_ALLOCATIONS):
            alloc_msg = self.generate_allocation_creation_message()
            self.generate_allocation_creation_flow(alloc_msg)
            self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()
        one_too_many = self.generate_allocation_creation_message()
        self.generate_allocation_creation_flow(one_too_many)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg, inauguration_report_expected=False)
        self.assertNotIn(one_too_many["allocationID"],
                         self.uninaugurated_hosts_of_open_reported_allocations)
        self.assertFalse(self.expected_reported_inaugurated_hosts)

    def test_a_lot_of_allocations(self):
        for i in xrange(300):
            alloc_msg = self.generate_allocation_creation_message(nr_hosts=2)
            self.generate_allocation_creation_flow(alloc_msg)
            self.generate_inauguration_flow_for_all_hosts(alloc_msg)
            self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def validate_open_registerations(self):
        """Validate that there's no leak of registerations."""
        subscribed_allocation_ids = set(self.mgr.allocations_callbacks)
        expected_subscribed_allocation_ids = set(self.uninaugurated_hosts_of_open_reported_allocations)
        self.assertEquals(subscribed_allocation_ids, expected_subscribed_allocation_ids)

    def validate_db(self):
        """Validate contents of records in DB by comparing the insert-mock to the expected results.

        This resets the insert_to_db_mock"""
        args = self._insert_to_db_mock.call_args_list
        while self.expected_reported_inaugurated_hosts:
            expected_host_id = self.expected_reported_inaugurated_hosts.pop(0)
            actual_inauguration_details = args.pop(0)[0][0]
            actual_host_id = actual_inauguration_details['host_id']
            self.assertEquals(expected_host_id, actual_host_id)
        self.assertFalse(args)
        self._insert_to_db_mock.reset_mock()

    def generate_inauguration_flow_for_all_hosts(self, alloc_msg, nr_progress_messages_per_host=10,
                                                 inauguration_report_expected=True):
        for _, host_id in alloc_msg['allocated'].iteritems():
            self.generate_inauguration_flow_for_single_host(
                host_id, nr_progress_messages_per_host,
                inauguration_report_expected=inauguration_report_expected)

    def generate_inauguration_flow_for_single_host(self, host_id, nr_progress_messages_per_host=10,
                                                   inauguration_report_expected=True):
        for message_nr in xrange(nr_progress_messages_per_host):
            chain_get_count = (message_nr * 10, message_nr * 10)
            progress_message = dict(id=host_id, status=dict(progress=dict(state='fetching',
                                                                          chainGetCount=chain_get_count)))
            if inauguration_report_expected:
                self.mgr.inaugurations_callbacks[host_id](progress_message)
        self.assertIn(host_id, self.mgr.inaugurations_callbacks)
        if inauguration_report_expected:
            self.prepare_wait_event_for_inauguration_unregisteration(host_id)
        done_message = dict(id=host_id, status='done')
        self.mgr.inaugurations_callbacks[host_id](done_message)
        if inauguration_report_expected:
            self.wait_for_inauguration_unregisteration(host_id)
            self.expected_reported_inaugurated_hosts.append(host_id)

    def generate_new_allocation_flow(self):
        return message

    def prepare_wait_events_for_allocation_unregisteration(self, alloc_msg):
        allocation_id = alloc_msg['allocationID']
        self.mgr.allocations_wait_conditions[allocation_id] = threading.Event()

    def prepare_wait_event_for_inauguration_unregisteration(self, host_id):
        self.mgr.inaugurations_register_wait_conditions[host_id] = threading.Event()

    def generate_allocation_death_flow(self, alloc_msg, was_allocation_creation_reported=True):
        allocation_id = alloc_msg['allocationID']
        self.prepare_wait_events_for_allocation_unregisteration(alloc_msg)
        self.send_allocation_death_message(allocation_id)
        self.wait_for_allocation_unregisteration(alloc_msg, was_allocation_creation_reported)
        self.open_allocations_count -= 1

    def send_allocation_death_message(self, allocation_id):
        self.mgr.allocations_callbacks[allocation_id](message=dict(event='changedState'))

    def wait_for_allocation_unregisteration(self, alloc_msg, was_allocation_creation_reported=True):
        allocation_id = alloc_msg['allocationID']
        logger.info("Waiting for unregisteration to allocation {}".format(allocation_id))
        self.mgr.allocations_wait_conditions[allocation_id].wait()
        del self.mgr.allocations_wait_conditions[allocation_id]
        logger.info("Unregisteration completed.")
        # TODO wait only for those who were uninaugurated
        for host_id in self.uninaugurated_hosts_of_open_reported_allocations[allocation_id]:
            logger.info("Waiting for unregisteration to inauguration of {}...".format(host_id))
            self.mgr.inaugurations_register_wait_conditions[host_id].wait()
            logger.info("Unegisteration completed.")
        if was_allocation_creation_reported:
            del self.uninaugurated_hosts_of_open_reported_allocations[allocation_id]
        else:
            self.assertNotIn(allocation_id, self.open_reporeted_allocations)

    def wait_for_inauguration_unregisteration(self, host_id):
        logger.info("Waiting for unregisteration to inauguration of {}...".format(host_id))
        self.mgr.inaugurations_register_wait_conditions[host_id].wait()
        logger.info("Unegisteration completed.")

    def generate_allocation_creation_flow(self, alloc_msg):
        allocation_id = alloc_msg['allocationID']
        self.mgr.allocations_wait_conditions[allocation_id] = threading.Event()
        for _, host_id in alloc_msg['allocated'].iteritems():
            self.mgr.inaugurations_register_wait_conditions[host_id] = threading.Event()
        self.open_allocations_count += 1
        self.total_allocations_count += 1
        self.mgr.all_allocations_handler(alloc_msg)
        self.assertNotIn(allocation_id, self.uninaugurated_hosts_of_open_reported_allocations)
        if self.open_allocations_count > rackattack.stats.main_allocation_stats.MAX_NR_ALLOCATIONS:
            return
        self.uninaugurated_hosts_of_open_reported_allocations[allocation_id] = \
            copy.copy(alloc_msg["allocated"].values())
        logger.info("Waiting for registeration to allocation {}...".format(allocation_id))
        self.mgr.allocations_wait_conditions[alloc_msg['allocationID']].wait()
        logger.info("Registeration completed.")
        for _, host_id in alloc_msg['allocated'].iteritems():
            logger.info("Waiting for registeration to inauguration of {}...".format(host_id))
            self.mgr.inaugurations_register_wait_conditions[host_id].wait()
            logger.info("Registeration completed.")

    def generate_allocation_creation_message(self, nr_hosts=2):
        message = dict(allocationID=self.total_allocations_count,
                       allocationInfo=dict(cpu='This allocation has got swag.'),
                       event='created')
        allocated = dict()
        requirements = dict()
        for host_nr in xrange(nr_hosts):
            host_id = self.available_hosts.pop(0)
            host_name = 'node{}'.format(host_nr)
            allocated[host_name] = host_id
            requirements[host_name] = dict(server_coolness='give me a very cool server')
        message['requirements'] = requirements
        message['allocated'] = allocated
        return message

if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    unittest.main()
