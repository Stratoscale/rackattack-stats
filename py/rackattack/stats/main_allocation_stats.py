import os
import time
import pytz
import Queue
import signal
import pprint
import logging
import datetime
import threading
import traceback
import elasticsearch
from functools import partial
from rackattack.tcp import subscribe


DB_ADDR = "10.0.1.66"
DB_PORT = 9200
MAX_NR_ALLOCATIONS = 150
TIMEZONE = 'Asia/Jerusalem'
DB_RECONNECTION_ATTEMPTS_INTERVAL = 60
EMAIL_SUBSCRIBERS = ("eliran@stratoscale.com",)
MAX_NR_SECONDS_WITHOUT_EVENTS_BEFORE_ALERTING = 3


is_connected = False


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


def send_mail(msg):
    global SEND_ALERTS_BY_MAIL
    if not SEND_ALERTS_BY_MAIL:
        return
    msg = MIMEText(msg)
    msg['Subject'] = 'RackAttack Status Alert {}'.format(time.ctime())
    msg['From'] = SENDER_EMAIL
    msg['To'] = ",".join(EMAIL_SUBSCRIBERS)

    # Send the message via our own SMTP server, but don't include the
    # envelope header.
    try:
        s = smtplib.SMTP(SMTP_SERVER)
    except socket.error:
        SEND_ALERTS_BY_MAIL = False
        msg = 'Could not connect to an SMTP server at "{}"'.format(SMTP_SERVER)
        logging.exception(msg)
        return
    try:
        s.sendmail(msg['From'], EMAIL_SUBSCRIBERS, msg.as_string())
        s.quit()
    except:
        SEND_ALERTS_BY_MAIL = False
        logging.exception("Could not send mail...")


class AllocationsHandler:
    INAUGURATIONS_INDEX = "inaugurations"
    ALLOCATIONS_INDEX = "allocations_2"

    def __init__(self, subscription_mgr, db):
        self._hosts_state = dict()
        self._db = db
        self._subscription_mgr = subscription_mgr
        logging.info('Subscribing to all hosts allocations.')
        subscription_mgr.registerForAllAllocations(self._pika_all_allocations_handler)
        self._allocation_subscriptions = set()
        self._tasks = Queue.Queue()
        self._latest_allocation_idx = None
        self._host_indices = list()
        self._db_record_id_of_last_requested_allocation = None

        def alert_warn_func(msg):
            logging.warn(msg)
            send_mail(msg)

        def alert_info_func(msg):
            logging.info(msg)
            send_mail(msg)

        self._events_monitor = EventsMonitor(MAX_NR_SECONDS_WITHOUT_EVENTS_BEFORE_ALERTING,
                                             self._alert_info_func,
                                             self._alert_warn_func)

    def run(self):
        self._events_monitor.start()
        while True:
            logging.info('Waiting for a new event (current number of monitored allocations: {})...'
                         .format(len(self._allocation_subscriptions)))
            finishedEvent, callback, message, args = self._tasks.get(block=True)
            if callback is None:
                logging.info('Finished handling events.')
                finishedEvent.set()
                break
            try:
                if args is None:
                    callback(message)
                else:
                    callback(message, **args)
            finally:
                if finishedEvent is not None:
                    finishedEvent.set()
                self._events_monitor.an_event_has_occurred()

    def stop(self, remove_pending_events=False):
        finishedEvent = threading.Event()
        if remove_pending_events:
            while not self._tasks.empty():
                self._tasks.get(block=False)
        self._tasks.put([finishedEvent, None, None, None])

    def finish_all_commands_in_queue(self):
        finishedEvent = threading.Event()
        self._tasks.put([finishedEvent, lambda *a: None, None, None])
        finishedEvent.wait()

    def _pika_inauguration_handler(self, message):
        self._tasks.put([None, self._inauguration_handler, message, None])

    def _inauguration_handler(self, msg):
        host_id = msg['id']
        if host_id not in self._hosts_state:
            logging.error('Got an inauguration message for a host without'
                          ' a known allocation: {}. Ignoring.'.format(host_id))
            return
        host_state = self._hosts_state[host_id]

        if msg['status'] == 'done':
            host_state['end_timestamp'] = time.time()
            logging.info('Host "{}" has finished inauguration. Unsubscribing.'.format(host_id))
            self._hosts_state[host_id]["inauguration_done"] = True
            self._add_inauguration_record_to_db(host_id)
            self._subscription_mgr.unregisterForInaugurator(host_id)
        elif msg['status'] == 'progress' and msg['progress']['state'] == 'fetching':
            logging.info('Progress message for {}'.format(host_id))
            chain_count = msg['progress']['chainGetCount']
            self._hosts_state[host_id]['latest_chain_count'] = chain_count

    def _unsubscribe_allocation(self, allocation_idx):
        """Precondition: allocation is subscribed to."""
        self._allocation_subscriptions.remove(allocation_idx)
        self._subscription_mgr.unregisterForAllocation(allocation_idx)
        allocated_hosts = [host_id for host_id, host in self._hosts_state.iteritems() if
                           host['allocation_idx'] == allocation_idx]
        uninaugurated_hosts = [host_id for host_id in allocated_hosts if
                               not self._hosts_state[host_id]["inauguration_done"]]
        if uninaugurated_hosts:
            logging.info("Inauguration stage for allocation {} ended without finishing inauguration "
                         "of the following hosts: {}.".format(allocation_idx,
                                                              ','.join(uninaugurated_hosts)))
            uninaugurated_hosts.sort()
            for host_id in uninaugurated_hosts:
                logging.info('Unsubscribing from inauguration events of "{}".'.format(host_id))
                self._add_inauguration_record_to_db(host_id)
                self._subscription_mgr.unregisterForInaugurator(host_id)
        for host in allocated_hosts:
            del self._hosts_state[host]

    def _is_allocation_dead(self, allocation_idx):
        result = self._rackattack_client.call('allocation__dead', id=allocation_idx)
        if result is None:
            return False
        return result

    def _allocation_handler(self, message, allocation_idx):
        logging.debug('_allocation_handler: {} {}'.format(allocation_idx, message))
        if allocation_idx not in self._allocation_subscriptions:
            logging.info("Got a status message for an unknown allocation: {}. Ignoging.".format(message))
            return
        if message.get('event', None) == "changedState":
            logging.info('Inauguration stage for allocation {} is over.'.format(allocation_idx))
            self._unsubscribe_allocation(allocation_idx)
        elif message.get('event', None) == "providerMessage":
            logging.info("Rackattack provider says: %(message)s", dict(message=message['message']))
        elif message.get('event', None) == "withdrawn":
            logging.info("Rackattack provider widthdrew allocation: '%(message)s",
                         dict(message=message['message']))
            self._unsubscribe_allocation(allocation_idx)
        else:
            logging.error("Unrecognized message: {}. Quitting.".format(message))
            self.stop()

    def _pika_allocation_handler(self, idx, message):
        self._tasks.put([None, self._allocation_handler, message, dict(allocation_idx=idx)])

    def _pika_all_allocations_handler(self, message):
        self._tasks.put([None, self._all_allocations_handler, message, None], block=True)

    def _store_current_requested_allocation(self, message):
        nr_nodes = len(message["requirements"])
        record = dict(allocationInfo=message['allocationInfo'],
                      nodes=self.get_nodes_list_from_requirements(message['requirements']),
                      nr_nodes=nr_nodes,
                      highest_phase_reached="requested")
        record["date"] = datetime_from_timestamp(time.time())
        record_metadata = self._db.create(index=self.ALLOCATIONS_INDEX,
                                          doc_type='allocation',
                                          body=record)
        self._db_record_id_of_last_requested_allocation = record_metadata["_id"]

    def _store_allocation_creation(self, message):
        nodes = self.get_nodes_list_from_requirements(message["requirements"])
        self.update_nodes_list_with_allocated(nodes, message["allocated"])
        nr_nodes = len(nodes)
        record = dict(nodes=nodes,
                      nr_nodes=nr_nodes,
                      allocationInfo=message["allocationInfo"],
                      highest_phase_reached="created",
                      allocation_id=message["allocationID"])
        if self._db_record_id_of_last_requested_allocation is None:
            record["date"] = datetime_from_timestamp(time.time())
            self._db.create(index=self.ALLOCATIONS_INDEX,
                            doc_type='allocation',
                            body=record)
        else:
            self._db.update(index=self.ALLOCATIONS_INDEX,
                            doc_type='allocation',
                            id=self._db_record_id_of_last_requested_allocation,
                            body=dict(doc=record))

    def _all_allocations_handler(self, message):
        if len(self._allocation_subscriptions) == MAX_NR_ALLOCATIONS:
            logging.error("Something has gone wrong; Too many open allocations. Quitting")
            self.stop(remove_pending_events=True)
            return
        if message['event'] == 'requested':
            self._store_current_requested_allocation(message)
            return
        assert message['event'] == 'created'
        logging.info('New allocation: {}'.format(message))
        self._store_allocation_creation(message)
        idx = message['allocationID']
        if self._latest_allocation_idx is None:
            self._latest_allocation_idx = idx
        elif idx < self._latest_allocation_idx:
            logging.error("Got an allocation index {} which is smaller than the previous one ({}) "
                          "(could RackAttack have been restarted?). Quitting."
                          .format(idx, self._latest_allocation_idx))
            self.stop(remove_pending_events=True)
            return
        info = message['allocationInfo']
        requirements = message['requirements']
        hosts = message['allocated']
        logging.debug('New allocation: {}.'.format(hosts))
        logging.info('Subscribing to new allocation (#{}).'.format(idx))
        allocation_handler = partial(self._pika_allocation_handler, idx)
        self._subscription_mgr.registerForAllocation(idx, allocation_handler)
        logging.info('Susbcribed')
        self._allocation_subscriptions.add(idx)
        allocation_unsubscribed_from_due_to_new_allocation = set()
        for name, host_id in hosts.iteritems():
            if host_id in self._hosts_state:
                existing_allocation = self._hosts_state[host_id]["allocation_idx"]
                assert existing_allocation not in allocation_unsubscribed_from_due_to_new_allocation
                logging.warn("Allocation {} was allocated with a host which is already used by "
                             "another allocation ({}). Unsubscribing from the latter first..."
                             .format(idx, existing_allocation))
                self._unsubscribe_allocation(existing_allocation)
                allocation_unsubscribed_from_due_to_new_allocation.add(existing_allocation)
                assert host_id not in self._hosts_state
            # Update hosts state
            self._hosts_state[host_id] = dict(start_timestamp=time.time(),
                                              name=name,
                                              allocation_idx=idx,
                                              inauguration_done=False,
                                              **requirements[name])
            assert not set(info.keys()).intersection(set(self._hosts_state.keys()))
            self._hosts_state[host_id].update(info)
            logging.info("Subscribing to inaugurator events of: {}.".format(host_id))
            self._subscription_mgr.registerForInagurator(host_id, self._pika_inauguration_handler)
            logging.info("Subscribed.")

    def _add_inauguration_record_to_db(self, host_id):
        index = 'allocations_'
        doc_type = 'allocation_'

        state = self._hosts_state[host_id]
        record_datetime = datetime_from_timestamp(state['start_timestamp'])

        local_store_count = None
        remote_store_count = None
        try:
            chain_count = state['latest_chain_count']
            local_store_count = chain_count.pop(0)
            remote_store_count = chain_count.pop(0)
        except KeyError:
            # NO info about Osmosis chain
            pass
        except IndexError:
            pass

        majority_chain_type = 'unknown'
        if local_store_count is not None:
            majority_chain_type = 'local'
            if remote_store_count is not None and \
                    local_store_count < remote_store_count:
                majority_chain_type = 'remote'
        id = "%d%03d%05d" % (state['start_timestamp'], state['allocation_idx'], self._hostIndex(host_id))

        record = dict(date=record_datetime,
                      host_id=host_id,
                      local_store_count=local_store_count,
                      remote_store_count=remote_store_count,
                      majority_chain_type=majority_chain_type)
        if state["inauguration_done"]:
            record["inauguration_period_length"] = state['end_timestamp'] - state['start_timestamp']
        record.update(state)

        try:
            logging.info("Inserting inauguration to DB (id: {}):\n{}".format(id, pprint.pformat(record)))
            self._db.create(index=self.INAUGURATIONS_INDEX, doc_type='inauguration', body=record, id=id)
        except Exception:
            logging.exception("Inauguration DB record insertion failed. Quitting.")
            self.stop()
            return

    def _hostIndex(self, hostID):
        if hostID not in self._host_indices:
            self._host_indices.append(hostID)
        return self._host_indices.index(hostID)

    @classmethod
    def get_nodes_list_from_requirements(cls, requirements):
        result = list()
        for node_name, node_requirements in requirements.iteritems():
            node = dict(node_name=node_name, requirements=node_requirements)
            result.append(node)
        return result

    @classmethod
    def update_nodes_list_with_allocated(cls, nodes, allocated):
        for node in nodes:
            node_name = node["node_name"]
            node["server_name"] = allocated[node_name]


def create_subscription():
    _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
    subscription_mgr = subscribe.Subscribe(amqp_url)
    return subscription_mgr


def configure_logger():
    loggers = {"": logging.INFO, "elasticsearch.trace": logging.WARNING}
    for loggerName, level in loggers.iteritems():
        logger = logging.getLogger(loggerName)
        logger.setLevel(level)
        handler = logging.StreamHandler()
        handler.setLevel(level)
        logger.addHandler(handler)


def validate_db_connection(db, is_first_connection_attempt=True):
    is_connected = False
    is_reconnection = not is_first_connection_attempt
    while not is_connected:
        if is_reconnection:
            logging.info("Will try to reconnect again in {} seconds..."
                         .format(DB_RECONNECTION_ATTEMPTS_INTERVAL))
            time.sleep(DB_RECONNECTION_ATTEMPTS_INTERVAL)
            msg = "Reconnecting to the DB (Elasticsearch address: {}:{})...".format(DB_ADDR, DB_PORT)
        else:
            msg = "Connecting to the DB (Elasticsearch address: {}:{})...".format(DB_ADDR, DB_PORT)
        logging.info(msg)
        try:
            db_info = db.info()
            logging.info(db_info)
            logging.info("Connected to the DB.")
            is_connected = True
        except elasticsearch.ConnectionError:
            msg = "Failed to connect to the DB."
            logging.exception(msg)
            if is_first_connection_attempt and not is_reconnection:
                send_mail(msg)
            is_reconnection = True


def handle_db_disconnection(self, db):
    msg = "An error occurred while talking to the DB:\n {}. Attempting to reconnect..." \
        .format(traceback.format_exc())
    logging.exception(msg)
    send_mail(msg)
    validate_db_connection(db, is_first_reconnection_attempt=False)
    msg = "Connected to the DB again."
    send_mail(msg)


def main():
    configure_logger()
    db = elasticsearch.Elasticsearch([{"host": DB_ADDR, "port": DB_PORT}])
    validate_db_connection(db)
    subscription_mgr = create_subscription()
    allocation_handler = AllocationsHandler(subscription_mgr, db)
    while True:
        try:
            allocation_handler.run()
            break
        except elasticsearch.ConnectionTimeout:
            handle_db_disconnection(db)
        except elasticsearch.ConnectionError:
            handle_db_disconnection(db)
        except elasticsearch.exceptions.TransportError:
            handle_db_disconnection(db)
        except KeyboardInterrupt:
            break
        except Exception:
            msg = "Critical error, exiting.\n\n"
            logging.exception(msg)
            msg += traceback.format_exc()
            send_mail(msg)
            sys.exit(1)
    logging.info("Done.")

if __name__ == '__main__':
    main()
