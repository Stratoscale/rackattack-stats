import time
import pytz
import logging
import datetime
import threading
from functools import partial
from rackattack import clientfactory
from elasticsearch import Elasticsearch
from rackattack.tcp.subscribe import Subscribe


TIMEZONE = 'Asia/Jerusalem'
RABBITMQ_CONNECTNION_URL = r'amqp://guest:guest@rack01-server58:1013/%2F'


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


class AllocationsHandler(threading.Thread):
    def __init__(self, db, rackattack_client):
        self._hosts_state = dict()
        self._db = db
        self._hosts_state_lock = threading.Lock()
        self._rackattack_client = rackattack_client
        global RABBITMQ_CONNECTNION_URL
        self._subscription_mgr = Subscribe(RABBITMQ_CONNECTNION_URL)
        self._subscription_mgr._readyEvent.wait()
        logging.info('Subscribing to all hosts allocations.')
        self._subscription_mgr.registerForAllAllocations(self._pika_all_allocations_handler)
        self._allocation_subscriptions = set()
        self._tasks_queue_lock = threading.Lock()
        self._tasks_queue = []
        self._queue_not_empty_event = threading.Event()
        threading.Thread.__init__(self)
        threading.Thread.start(self)

    def run(self):
        while True:
            self._queue_not_empty_event.wait()
            with self._tasks_queue_lock:
                while self._tasks_queue:
                    callback, message, args = self._tasks_queue.pop(0)
                    if args is None:
                        callback(message)
                    else:
                        callback(message, **args)
                self._queue_not_empty_event.clear()

    def _pika_inauguration_handler(self, message):
        with self._tasks_queue_lock:
            self._tasks_queue.append([self._inauguration_handler, message, None])
            self._queue_not_empty_event.set()

    def _inauguration_handler(self, msg):
        logging.debug('Inaugurator message: {}'.format(msg))
        with self._hosts_state_lock:
            if 'id' not in msg:
                logging.error('_inauguration_handler: WTF msg={}.'.format(msg))
                return
            host_id = msg['id']
            try:
                host_state = self._hosts_state[host_id]
            except KeyError:
                logging.error('Got an inauguration message for a host without'
                              ' a known allocation: {}'.format(host_id))
                return

            if msg['status'] == 'done':
                host_state['end_timestamp'] = time.time()
                host_state['inauguration_done'] = True
                logging.info('Host "{}" has inaugurated, congratulations.'.format(host_id))
                self._add_allocation_record_to_db(host_id)
            elif msg['status'] == 'progress' and \
                    msg['progress']['state'] == 'fetching':
                chain_count = msg['progress']['chainGetCount']
                self._hosts_state[host_id]['latest_chain_count'] = chain_count

    def _ubsubscribe_allocation(self, allocation_idx):
        logging.info('Unsubscribing from allocation {}.'.format(allocation_idx))
        try:
            self._allocation_subscriptions.remove(allocation_idx)
        except KeyError:
            logging.info('Already ubsubscribed from allocation #{}.'.format(allocation_idx))

        try:
            self._subscription_mgr.unregisterForAllocation(allocation_idx)
        except AssertionError:
            logging.warning('Could not unsubscribe from allocation {}'.
                            format(allocation_idx))

        # Unregister all inaugurators
        hosts_to_remove = set()
        for host_id, host in self._hosts_state.iteritems():
            if host['allocation_idx'] == allocation_idx:
                hosts_to_remove.add(host_id)
                logging.info('Unsubscribing from inauguration events of "{}".'.format(host_id))
                try:
                    self._subscription_mgr.unregisterForInaugurator(host_id)
                except AssertionError:
                    logging.warning('Could not unregister from inauguration of '
                                    ' "{}".'.format(allocation_idx))

        for host_id in list(hosts_to_remove):
            del self._hosts_state[host_id]

    def _is_allocation_dead(self, allocation_idx):
        result = self._rackattack_client.call('allocation__dead', id=allocation_idx)
        if result is None:
            return False
        return result

    def _are_all_inaugurations_done(self, allocation_idx):
        for host in self._hosts_state.itervalues():
            if not host['inauguration_done']:
                return False
        return True

    def _allocation_handler(self, message, allocation_idx):
        logging.debug('_allocation_handler: {} {}'.format(allocation_idx, message))
        with self._hosts_state_lock:
            if message.get('event', None) == "changedState":
                is_dead = self._is_allocation_dead(allocation_idx)
                if is_dead:
                    logging.info('Allocation {} has died of reason "{}"'.format(allocation_idx, is_dead))
                    self._ubsubscribe_allocation(allocation_idx)
                elif self._are_all_inaugurations_done(allocation_idx):
                    logging.info("Allocation {} has changed its state and it's still alive, but it does not"
                                 " wait for any more inaugurations, so unsubscribing from it.".
                                 format(allocation_idx))
                    self._ubsubscribe_allocation(allocation_idx)
                else:
                    logging.info("Allocation {} has changed its state and it's still alive and waiting for "
                                 " some inaugurations to complete.".format(allocation_idx))
                    self._ubsubscribe_allocation(allocation_idx)
            elif message.get('event', None) == "providerMessage":
                logging.info("Rackattack provider says: %(message)s", dict(message=message['message']))
            elif message.get('event', None) == "withdrawn":
                logging.info("Rackattack provider widthdrew allocation: '%(message)s",
                             dict(message=message['message']))
                self._ubsubscribe_allocation(allocation_idx)
            else:
                logging.error('_allocation_handler: WTF allocation_idx={}, message={}.'.
                              format(allocation_idx, message))

    def _pika_allocation_handler(self, allocation_idx, message):
        with self._tasks_queue_lock:
            self._tasks_queue.append([self._allocation_handler, message,
                                      dict(allocation_idx=allocation_idx)])
            self._queue_not_empty_event.set()

    def _pika_all_allocations_handler(self, message):
        with self._tasks_queue_lock:
            self._tasks_queue.append([self._all_allocations_handler, message, None])
            self._queue_not_empty_event.set()

    def _all_allocations_handler(self, message):
        logging.info('_all_allocations_handler: {}'.format(message))
        global subscription_mgr, subscribe, state
        if message['event'] == 'requested':
            return
        assert message['event'] == 'created'

        allocation_idx = message['allocationID']
        allocation_info = message['allocationInfo']
        requirements = message['requirements']
        with self._hosts_state_lock:
            hosts = message['allocated']
            logging.debug('New allocation: {}.'.format(hosts))
            logging.info('Subscribing to new allocation (#{}).'.format(allocation_idx))
            allocation_handler = partial(self._pika_allocation_handler, allocation_idx)
            self._subscription_mgr.registerForAllocation(allocation_idx, allocation_handler)
            self._allocation_subscriptions.add(allocation_idx)
            logging.info("Subscribing to allocation #{}'s hosts inauguration info.".format(allocation_idx))
            for name, host_id in hosts.iteritems():
                # Update hosts state
                self._hosts_state[host_id] = dict(start_timestamp=time.time(),
                                                  name=name,
                                                  allocation_idx=allocation_idx,
                                                  inauguration_done=False,
                                                  **requirements[name])
                self._hosts_state[host_id].update(allocation_info)
                # Subecribe
                logging.info("Subscribing to inaugurator events of: {}.".
                             format(host_id))
                self._subscription_mgr.registerForInagurator(host_id, self._pika_inauguration_handler)

    def _add_allocation_record_to_db(self, host_id):
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
            # NO info abount Osmosis chain
            pass
        except IndexError:
            pass

        majorioty_chain_type = 'unknown'
        if local_store_count is not None:
            majorioty_chain_type = 'local'
            if remote_store_count is not None and \
                    local_store_count < remote_store_count:
                majorioty_chain_type = 'remote'

        inauguration_period_length = state['end_timestamp'] - \
            state['start_timestamp']
        id = "%d%03d%05d" % (state['start_timestamp'], state['allocation_idx'],
                             int(str(abs(hash(host_id)))[:5]))

        record = dict(timestamp=record_datetime,
                      _timestamp=record_datetime,
                      host_id=host_id,
                      inauguration_period_length=inauguration_period_length,
                      local_store_count=local_store_count,
                      remote_store_count=remote_store_count,
                      majorioty_chain_type=majorioty_chain_type)
        record.update(state)

        self._db.create(index=index, doc_type=doc_type, body=record, id=id)


def main():
    global subscription_mgr
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    db = Elasticsearch()
    client = clientfactory.factory()
    handler = AllocationsHandler(db, client)

    a = threading.Event()
    a.wait()

if __name__ == '__main__':
    main()
