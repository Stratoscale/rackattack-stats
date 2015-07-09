import os
import time
import pytz
import Queue
import logging
import pymongo
import datetime
import threading
from functools import partial
from rackattack.tcp import subscribe


MAX_NR_ALLOCATIONS = 100
TIMEZONE = 'Asia/Jerusalem'


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


class AllocationsHandler(threading.Thread):
    def __init__(self, subscription_mgr, db, ready_event, stop_event):
        self._hosts_state = dict()
        self._db = db
        self._subscription_mgr = subscription_mgr
        logging.info('Subscribing to all hosts allocations.')
        subscription_mgr.registerForAllAllocations(self._pika_all_allocations_handler)
        self._allocation_subscriptions = set()
        self._tasks = Queue.Queue()
        self._ready_event = ready_event
        self._latest_allocation_idx = None
        self._stop_event = stop_event
        threading.Thread.__init__(self)

    def run(self):
        self._ready_event.set()
        while True:
            logging.info('Waiting for a new event...')
            finishedEvent, callback, message, args = self._tasks.get(block=True)
            if callback is None:
                logging.info('Finished handling events.')
                finishedEvent.set()
                break
            if args is None:
                callback(message)
            else:
                callback(message, **args)
            if finishedEvent is not None:
                finishedEvent.set()

    def stop(self, remove_pending_events=False):
        finishedEvent = threading.Event()
        if remove_pending_events:
            while not self._tasks.empty():
                self._tasks.get(block=False)
        self._tasks.put([finishedEvent, None, None, None])

    def _pika_inauguration_handler(self, message):
        self._tasks.put([None, self._inauguration_handler, message, None])

    def _inauguration_handler(self, msg):
        logging.debug('Inaugurator message: {}'.format(msg))
        host_id = msg['id']
        if host_id not in self._hosts_state:
            logging.error('Got an inauguration message for a host without'
                          ' a known allocation: {}. Ignoring.'.format(host_id))
            return
        host_state = self._hosts_state[host_id]

        if msg['status'] == 'done':
            host_state['end_timestamp'] = time.time()
            logging.info('Host "{}" has finished inauguration. Unsubscribing.'.format(host_id))
            self._add_inauguration_record_to_db(host_id)
            self._subscription_mgr.unregisterForInaugurator(host_id)
            self._hosts_state[host_id]["inauguration_done"] = True
        elif msg['status'] == 'progress' and \
                msg['progress']['state'] == 'fetching':
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
            for host_id in uninaugurated_hosts:
                logging.info('Unsubscribing from inauguration events of "{}".'.format(host_id))
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

    def _all_allocations_handler(self, message):
        if message['event'] == 'requested':
            return
        assert message['event'] == 'created'
        logging.info('New allocation: {}'.format(message))
        idx = message['allocationID']
        if self._latest_allocation_idx is None:
            self._latest_allocation_idx = idx
        elif idx < self._latest_allocation_idx:
            logging.error("Got an allocation index {} which is smaller than the previous one ({}) "
                          "(could RackAttack have been restarted?). Quitting."
                          .format(idx, self._latest_allocation_idx))
            self.stop(remove_pending_events=True)
            return
        if len(self._allocation_subscriptions) == MAX_NR_ALLOCATIONS:
            logging.error("Something has gone wrong; Too many open allocations. Quitting")
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

        inauguration_period_length = state['end_timestamp'] - state['start_timestamp']
        id = "%d%03d%05d" % (state['start_timestamp'], state['allocation_idx'],
                             int(str(abs(hash(host_id)))[:5]))

        record = dict(timestamp=record_datetime,
                      _timestamp=record_datetime,
                      host_id=host_id,
                      inauguration_period_length=inauguration_period_length,
                      local_store_count=local_store_count,
                      remote_store_count=remote_store_count,
                      majority_chain_type=majority_chain_type)
        record.update(state)

        try:
            logging.info("Inserting record to DB: {}".format(record))
            self._db.inaugurations.insert(record)
        except Exception:
            logging.exception("Inauguration DB record insertion failed. Quitting.")
            self.stop()
            return


def create_connections():
    _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
    subscription_mgr = subscribe.Subscribe(amqp_url)
    return subscription_mgr


def configure_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)


def main(ready_event=None, stop_event=threading.Event()):
    configure_logger()

    if ready_event is None:
        ready_event = threading.Event()

    db = pymongo.MongoClient().rackattack_stats
    logging.info("Initializing allocations handler....")
    subscription_mgr = create_connections()
    handler = AllocationsHandler(subscription_mgr, db, ready_event, stop_event)
    handler.start()
    ready_event.wait()
    logging.info("Allocations handler is ready.")
    stop_event.wait()
    handler.stop()
    handler.join()
    logging.info("Done.")

if __name__ == '__main__':
    main()
