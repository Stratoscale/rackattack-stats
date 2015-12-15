import mock
import copy
import json
import logging
import unittest
import greenlet
import threading
import elasticsearch
import rackattack
from rackattack.tcp import publish
from rackattack.tcp import subscribe
from rackattack.tests import mock_pika
from rackattack.tests import one_threaded_publish
from rackattack.stats.main_allocation_stats import AllocationsHandler


logger = logging.getLogger()


class Host:
    def __init__(self, host_id):
        self._id = host_id

    def id(self):
        return self._id


class HostStateMachine:
    def __init__(self, hostImplementation):
        self._hostImplementation = hostImplementation

    def hostImplementation(self):
        return self._hostImplementation


class SubscribeMock(object):
    instances = []

    def __init__(self, amqpURL):
        self.all_allocations_callback = None
        self.inaugurations_callbacks = dict()
        self.inaugurations_callback_history = list()
        self.instances.append(self)
        self.consumer = mock_pika.getBlockingConnectionToFakeBroker().channel()

    def registerForAllAllocations(self, callback):
        self.all_allocations_callback = callback

    def registerForInagurator(self, host_id, callback):
        self.inaugurations_callbacks[host_id] = callback
        self.inaugurations_callback_history.append(callback)

    def unregisterForInaugurator(self, host_id):
        del self.inaugurations_callbacks[host_id]

    def continue_with_thread(self):
        pending_messages_by_exchanges = self.consumer.broker.sendOrderByExchanges
        while pending_messages_by_exchanges:
            exchange = pending_messages_by_exchanges[0]
            message = self.consumer.basic_consume(exchange)
            message = json.loads(message)
            if exchange == publish.Publish.ALL_HOSTS_ALLOCATIONS_EXCHANGE_NAME:
                if self.all_allocations_callback is not None:
                    self.all_allocations_callback(message)


class ElasticsearchDBMock(object):
    def __init__(self, *args, **kwargs):
        self._next_record_id = 0
        self._records = dict()
        self._event = threading.Event()

    def create(self, index, doc_type, body, id=None):
        del doc_type
        del id
        record = dict(body)
        record["_index"] = index
        record["_id"] = self._next_record_id
        self._records[self._next_record_id] = record
        result = dict(_id=self._next_record_id)
        self._next_record_id += 1
        self._event.set()
        return result

    def update(self, index, doc_type, id, body):
        update = body["doc"]
        assert id in self._records
        self._records[id].update(update)
        self._event.set()

    def get_records_by_order_of_creation(self):
        keys = self._records.keys()
        keys.sort()
        for key in keys:
            yield self._records[key]


class Test(unittest.TestCase):
    """
    Possible allocation flows:
        requested
        v requested, rejected
        requested, created
        v requested, created, dead
        requested, created, done
        requested, created, done, dead
    """
    def setUp(self):
        rackattack.tcp.subscribe.Subscribe = SubscribeMock
        SubscribeMock.instances = []
        elasticsearch.Elasticsearch = ElasticsearchDBMock
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
        mock_pika.enableMockedPika(modules=[publish])
        self.tested = self._generate_instance_with_mocked_event_loop()
        self.tested_server_context = greenlet.greenlet(self.tested.run)
        assert self._db is not None
        self._insert_to_db_mock = self._db.create
        instances = SubscribeMock.instances
        self.assertEquals(len(instances), 1)
        self.mgr = SubscribeMock.instances[0]
        self.expected_reported_inaugurated_hosts = []
        self.expected_reported_uninaugurated_hosts = []
        self.expected_reported_allocations = []
        self.uninaugurated_hosts_of_open_reported_allocations = dict()
        self.publish = one_threaded_publish.OneThreadedPublish(mock_pika.DEFAULT_AMQP_URL)
        self.has_thread_started = False
        self._continue_with_server()

    def test_allocation_with_one_host(self):
        req_msg = self.generate_allocation_request_message(nr_hosts=1)
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_with_ten_hosts(self):
        req_msg = self.generate_allocation_request_message(nr_hosts=10)
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_with_no_hosts(self):
        req_msg = self.generate_allocation_request_message(nr_hosts=10)
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
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
        req_msg = self.generate_allocation_request_message(nr_hosts=nr_hosts_in_allocation)
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        inaugurated_hosts_in_first_allocation = ("charlie", "delta", "golf", "hotel", "india", "juliet")
        for host in inaugurated_hosts_in_first_allocation:
            self.generate_inauguration_flow_for_single_host(host, alloc_msg["allocationID"])
        hosts_on_both_allocations = original_hosts_pool[4:4 + nr_hosts_in_allocation]
        self.available_hosts = hosts_on_both_allocations + self.available_hosts
        another_alloc_req = self.generate_allocation_request_message(nr_hosts_in_allocation)
        self.generate_allocation_request_flow(another_alloc_req)
        another_alloc_msg = self.generate_allocation_creation_message_from_request_message(another_alloc_req)
        self.generate_allocation_creation_flow(another_alloc_msg)
        # At this stage, the tested unit should figure that a new allocation uses hosts of an older
        # allocation, so it should unsubscribe from the old allocation.
        self.wait_for_allocation_unregisteration(alloc_msg, still_registered=hosts_on_both_allocations)
        inaugurated_hosts_in_second_allocation = ("golf", "hotel", "kilo", "lima", "oscar", "papa")
        for host in inaugurated_hosts_in_second_allocation:
            self.generate_inauguration_flow_for_single_host(host, another_alloc_msg["allocationID"])
        self.generate_allocation_death_flow(another_alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_inauguration_done_for_an_unallocated_host(self):
        req_msg = self.generate_allocation_request_message()
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        host_id = alloc_msg["allocated"].values()[0]
        inauguration_callback = self.mgr.inaugurations_callback_history[0]
        self.generate_allocation_death_flow(alloc_msg)
        done_message = dict(id=host_id, status='done')
        inauguration_callback(done_message)
        self.validate_db()
        self.validate_open_registerations()

    def test_creation_message_for_an_unknown_allocation(self):
        req_msg = self.generate_allocation_request_message(nr_hosts=10)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg, is_allocation_report_expected=False)
        self.assertNotIn(alloc_msg["allocationID"], self.tested._allocation_subscriptions)
        for host_id in [machine.hostImplementation().id() for machine in alloc_msg["allocated"].values()]:
            self.assertNotIn(host_id, self.mgr.inaugurations_callbacks)
        allocation_id = alloc_msg["allocationID"]
        self.generate_allocation_death_flow(alloc_msg, is_allocation_report_expected=False)
        self.validate_db()
        self.validate_open_registerations()

    def test_too_many_open_allocations(self):
        for i in xrange(rackattack.stats.main_allocation_stats.MAX_NR_ALLOCATIONS):
            req_msg = self.generate_allocation_request_message(nr_hosts=10)
            self.generate_allocation_request_flow(req_msg)
            alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
            self.generate_allocation_creation_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()
        one_too_many = self.generate_allocation_creation_message()
        self.assertNotIn(one_too_many["allocationID"], self.tested._allocation_subscriptions)

    def test_a_lot_of_allocations(self):
        for i in xrange(300):
            req_msg = self.generate_allocation_request_message(nr_hosts=2)
            self.generate_allocation_request_flow(req_msg)
            alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
            self.generate_allocation_creation_flow(alloc_msg)
            self.generate_inauguration_flow_for_all_hosts(alloc_msg)
            self.generate_allocation_death_flow(alloc_msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_request(self):
        msg = self.generate_allocation_request_message(nr_hosts=10)
        self.generate_allocation_request_flow(msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_approval(self):
        msg = self.generate_allocation_request_message(nr_hosts=10)
        self.generate_allocation_request_flow(msg)
        msg = self.generate_allocation_creation_message_from_request_message(msg)
        self.generate_allocation_creation_flow(msg)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_rejection(self):
        msg = self.generate_allocation_request_message(nr_hosts=10)
        self.generate_allocation_request_flow(msg)
        self.generate_allocation_rejection_flow(reason="what a cool reason")
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_died_after_creation(self):
        req_msg = self.generate_allocation_request_message()
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg, reason="withdrawn")
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_died_after_done(self):
        req_msg = self.generate_allocation_request_message()
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_death_flow(alloc_msg, reason="withdrawn")
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_death_for_an_unreported_allocation(self):
        req_msg = self.generate_allocation_request_message()
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_death_flow(alloc_msg, reason="freed", is_allocation_report_expected=False)
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_done(self):
        req_msg = self.generate_allocation_request_message()
        self.generate_allocation_request_flow(req_msg)
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_creation_flow(alloc_msg)
        self.generate_inauguration_flow_for_all_hosts(alloc_msg)
        self.generate_allocation_done_flow(alloc_msg["allocationID"])
        self.validate_db()
        self.validate_open_registerations()

    def test_allocation_done_for_unreported_allocations(self):
        req_msg = self.generate_allocation_request_message()
        alloc_msg = self.generate_allocation_creation_message_from_request_message(req_msg)
        self.generate_allocation_done_flow(0, allocation_report_expected=False)
        self.validate_db()
        self.validate_open_registerations()

    def validate_open_registerations(self):
        """Validate that there's no leak of registerations."""
        subscribed_allocation_ids = set(self.tested._allocation_subscriptions.keys())
        expected_subscribed_allocation_ids = \
            set(self.uninaugurated_hosts_of_open_reported_allocations.keys())
        self.assertEquals(subscribed_allocation_ids, expected_subscribed_allocation_ids)

    def validate_db(self):
        """Validate contents of records in DB by comparing the insert-mock to the expected results.

        This resets the insert_to_db_mock"""
        last_requested_allocation = None
        db_records = self._db.get_records_by_order_of_creation()
        for record in db_records:
            index = record["_index"]
            if index == AllocationsHandler.INAUGURATIONS_INDEX:
                if record["inauguration_done"]:
                    expected_host_id = self.expected_reported_inaugurated_hosts.pop(0)
                else:
                    expected_host_id = self.expected_reported_uninaugurated_hosts.pop(0)
                actual_host_id = record['host_id']
                self.assertEquals(expected_host_id, actual_host_id)
            elif index == AllocationsHandler.ALLOCATIONS_INDEX:
                expected = self.expected_reported_allocations.pop(0)
                actual = record
                logging.info("Expected allocation: {}".format(expected))
                logging.info("Actual allocation: {}".format(actual))
                self.assertEquals(expected["nodes"], actual["nodes"])
                self.assertEquals(expected["allocationInfo"], actual["allocationInfo"])
                self.assertEquals(expected["highest_phase_reached"], actual["highest_phase_reached"])
                self.assertEquals(expected["done"], actual["done"])
                if expected["done"]:
                    self.assertIn("inauguration_duration", actual)
                if expected["highest_phase_reached"] == "created":
                    self.assertEquals(expected["allocation_id"], actual["allocation_id"])
                    last_requested_allocation = expected
                elif expected["highest_phase_reached"] == "dead" and expected["done"]:
                    self.assertIn("test_duration", actual)
                    self.assertIn("allocation_duration", actual)
            else:
                assert False
        self.assertFalse(self.expected_reported_uninaugurated_hosts)
        self.assertFalse(self.expected_reported_inaugurated_hosts)
        self.assertFalse(self.expected_reported_allocations)

    def generate_allocation_done_flow(self, allocation_id, allocation_report_expected=True):
        self.publish.allocationDone(allocation_id)
        self._continue_with_server()
        if allocation_report_expected:
            self.modifiy_expected_highest_phase(allocation_id, phase="done")

    def generate_inauguration_flow_for_all_hosts(self, alloc_msg, nr_progress_messages_per_host=10,
                                                 inauguration_report_expected=True):
        for _, state_machine in alloc_msg['allocated'].iteritems():
            host_id = state_machine.hostImplementation().id()
            self.generate_inauguration_flow_for_single_host(
                host_id, alloc_msg["allocationID"], nr_progress_messages_per_host,
                inauguration_report_expected=inauguration_report_expected)

    def generate_inauguration_flow_for_single_host(self, host_id, allocation_id,
                                                   nr_progress_messages_per_host=10,
                                                   inauguration_report_expected=True):
        for message_nr in xrange(nr_progress_messages_per_host):
            chain_get_count = (message_nr * 10, message_nr * 10)
            progress_message = dict(id=host_id, status=dict(progress=dict(state='fetching',
                                                                          chainGetCount=chain_get_count)))
            if inauguration_report_expected:
                self.mgr.inaugurations_callbacks[host_id](progress_message)
        if inauguration_report_expected:
            self.assertIn(host_id, self.mgr.inaugurations_callbacks)
        else:
            self.assertNotIn(host_id, self.mgr.inaugurations_callbacks)
        done_message = dict(id=host_id, status='done')
        self.mgr.inaugurations_callbacks[host_id](done_message)
        self._continue_with_server()
        if inauguration_report_expected:
            self.assertNotIn(host_id, self.mgr.inaugurations_callbacks)
            self.expected_reported_inaugurated_hosts.append(host_id)
            self.uninaugurated_hosts_of_open_reported_allocations[allocation_id].remove(host_id)

    def generate_new_allocation_flow(self):
        return message

    def generate_allocation_death_flow(self, alloc_msg, is_allocation_report_expected=True, reason="freed"):
        allocation_id = alloc_msg['allocationID']
        self.send_allocation_death_message(allocation_id, reason=reason)
        if is_allocation_report_expected:
            self.wait_for_allocation_unregisteration(alloc_msg, is_allocation_report_expected)
            self.modifiy_expected_highest_phase(allocation_id, phase="dead")
        self.open_allocations_count -= 1

    def modifiy_expected_highest_phase(self, allocation_id, phase):
        potential_allocations = [allocation for allocation in self.expected_reported_allocations if
                                 allocation["allocation_id"] == allocation_id]
        assert len(potential_allocations) == 1
        allocation = potential_allocations[0]
        allocation["highest_phase_reached"] = phase
        if phase == "done":
            allocation["done"] = True

    def send_allocation_death_message(self, allocation_id, reason):
        self.publish.allocationDied(allocationID=allocation_id, reason=reason, moreInfo="Because i wanna")
        self._continue_with_server()

    def wait_for_allocation_unregisteration(self,
                                            alloc_msg,
                                            was_allocation_creation_reported=True,
                                            still_registered=None):
        if still_registered is None:
            still_registered = list()
        allocation_id = alloc_msg['allocationID']
        logger.info("Waiting for unregisteration to allocation {}".format(allocation_id))
        self.assertNotIn(allocation_id, self.tested._allocation_subscriptions)
        logger.info("Unregisteration completed.")
        # TODO wait only for those who were uninaugurated
        for host_id in self.uninaugurated_hosts_of_open_reported_allocations[allocation_id]:
            if host_id not in still_registered:
                logger.info("Waiting for unregisteration to inauguration of {}...".format(host_id))
                self.assertNotIn(host_id, self.mgr.inaugurations_callbacks.keys())
                logger.info("Unregisteration completed.")
        if was_allocation_creation_reported:
            uninaugurated = self.uninaugurated_hosts_of_open_reported_allocations[allocation_id]
            uninaugurated.sort()
            self.expected_reported_uninaugurated_hosts.extend(uninaugurated)
            del self.uninaugurated_hosts_of_open_reported_allocations[allocation_id]

    def prepare_expected_reported_allocation(self, alloc_msg):
        nodes = self.get_expected_nodes_list_from_requirements(alloc_msg["requirements"])
        self.update_expected_nodes_list_with_allocated(nodes, alloc_msg["allocated"])
        allocation_info = dict(allocationInfo=alloc_msg["allocationInfo"],
                               nodes=nodes,
                               highest_phase_reached="created",
                               allocation_id=alloc_msg["allocationID"])
        was_allocation_request_reported = self.expected_reported_allocations and \
            self.expected_reported_allocations[-1]["highest_phase_reached"] == "requested"
        if was_allocation_request_reported:
            allocation = self.expected_reported_allocations[-1]
            allocation.update(allocation_info)
        else:
            allocation = dict()
            allocation.update(allocation_info)
            self.expected_reported_allocations.append(allocation)

    def generate_allocation_creation_flow(self, alloc_msg, is_allocation_report_expected=True):
        allocation_id = alloc_msg['allocationID']
        self.open_allocations_count += 1
        self.total_allocations_count += 1
        if is_allocation_report_expected:
            self.prepare_expected_reported_allocation(alloc_msg)
        allocated = alloc_msg["allocated"]
        self.publish.allocationCreated(alloc_msg["allocationID"], allocated)
        self._continue_with_server()
        self.assertNotIn(allocation_id, self.uninaugurated_hosts_of_open_reported_allocations)
        if self.open_allocations_count > rackattack.stats.main_allocation_stats.MAX_NR_ALLOCATIONS:
            return
        if is_allocation_report_expected:
            self.uninaugurated_hosts_of_open_reported_allocations[allocation_id] = \
                [state_machine.hostImplementation().id() for state_machine in allocated.values()]

    def generate_allocation_request_flow(self, msg):
        nodes = self.get_expected_nodes_list_from_requirements(msg["requirements"])
        expected_reported_allocation = dict(nodes=nodes,
                                            allocationInfo=msg["allocationInfo"],
                                            highest_phase_reached="requested",
                                            done=False)
        self.expected_reported_allocations.append(expected_reported_allocation)
        self.publish.allocationRequested(msg["requirements"], msg["allocationInfo"])
        self._continue_with_server()

    def generate_generic_allocation_message(self, nr_hosts=2):
        message = dict(allocationInfo=dict(cpu="This allocation has got swag."))
        requirements = dict()
        for host_nr in xrange(nr_hosts):
            host_name = 'node{}'.format(host_nr)
            requirements[host_name] = dict(server_coolness='give me a very cool server')
        message['requirements'] = requirements
        return message

    def generate_allocation_request_message(self, nr_hosts=2):
        message = self.generate_generic_allocation_message(nr_hosts)
        message["event"] = "requested"
        message["allocationInfo"] = dict(cpu='This allocation has got swag.')
        return message

    def put_allocation_creation_unique_fields(self, message):
        message["allocationID"] = self.total_allocations_count
        message["event"] = 'created'
        allocated = dict()
        for host_nr in xrange(len(message["requirements"])):
            host_id = self.available_hosts.pop(0)
            host_name = 'node{}'.format(host_nr)
            allocated[host_name] = HostStateMachine(Host(host_id))
        message['allocated'] = allocated

    def generate_allocation_creation_message(self, nr_hosts=2):
        message = self.generate_generic_allocation_message(nr_hosts)
        self.put_allocation_creation_unique_fields(message)
        return message

    def generate_allocation_creation_message_from_request_message(self, message):
        self.put_allocation_creation_unique_fields(message)
        return message

    def generate_allocation_rejection_flow(self, reason):
        self.publish.allocationRejected(reason=reason)
        self._continue_with_server()
        allocation_info = dict()
        allocation_info["highest_phase_reached"] = "rejected"
        allocation_info["reason"] = reason
        allocation = self.expected_reported_allocations[-1]
        allocation.update(allocation_info)

    @classmethod
    def get_expected_nodes_list_from_requirements(cls, requirements):
        result = list()
        for node_name, node_requirements in requirements.iteritems():
            node = dict(node_name=node_name, requirements=node_requirements)
            result.append(node)
        return result

    @classmethod
    def update_expected_nodes_list_with_allocated(cls, nodes, allocated):
        for node in nodes:
            node_name = node["node_name"]
            node["server_name"] = allocated[node_name].hostImplementation().id()

    def _generate_instance_with_mocked_event_loop(self):
        self._db = ElasticsearchDBMock()
        subscription_mgr = subscribe.Subscribe(mock_pika.DEFAULT_AMQP_URL)
        instance = AllocationsHandler(subscription_mgr, self._db, mock.Mock())

        def queueGetWrapper(*args, **kwargs):
            if self.tested._tasks.qsize() > 0:
                item = self.original_get()
            else:
                item = greenlet.getcurrent().parent.switch()
            return item

        self.original_get = instance._tasks.get
        instance._tasks.get = queueGetWrapper
        return instance

    def _continue_with_server(self):
        self.publish.continueWithServer()
        self.mgr.continue_with_thread()
        if self.tested._tasks.qsize() > 0:
            item = self.original_get()
            self.tested_server_context.switch(item)
        elif not self.has_thread_started:
            self.has_thread_started = True
            self.tested_server_context.switch()
        else:
            self.assertFalse(True, "Trying to continue with thread with an empty input queue")

if __name__ == '__main__':
    logger.setLevel(logging.ERROR)
    handler = logging.StreamHandler()
    handler.setLevel(logging.ERROR)
    logger.addHandler(handler)
    unittest.main()
