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


TIMEZONE = 'Asia/Jerusalem'


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


class AllocationsHandler(threading.Thread):
    def __init__(self, db, ready_event):
        self._hosts_state = dict()
        self._db = db
        _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
        self._subscription_mgr = subscribe.Subscribe(amqp_url)
        logging.info('Subscribing to all hosts allocations...')
        self._subscription_mgr.registerForAllAllocations(self._pika_all_allocations_handler)
        logging.info('Subscribed.')
        self._allocation_subscriptions = set()
        self._tasks = Queue.Queue()
        self.ready_event = ready_event
        threading.Thread.__init__(self)

    def run(self):
        while True:
            self.ready_event.set()
            logging.info('Waiting for a new event...')
            callback, message, args = self._tasks.get(block=True)
            if callback is None:
                logging.info('Finished handling events.')
                break
            if args is None:
                callback(message)
            else:
                callback(message, **args)

    def stop(self):
        self._tasks.put([None, None, None])

    def _pika_inauguration_handler(self, message):
        self._tasks.put([self._inauguration_handler, message, None])

    def _inauguration_handler(self, msg):
        logging.debug('Inaugurator message: {}'.format(msg))
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
        if message.get('event', None) == "changedState":
            logging.info('Inauguration for allocation {} is over.'.format(allocation_idx))
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

    def _pika_allocation_handler(self, idx, message):
        self._tasks.put([self._allocation_handler, message, dict(allocation_idx=idx)])

    def _pika_all_allocations_handler(self, message):
        self._tasks.put([self._all_allocations_handler, message, None], block=True)

    def _all_allocations_handler(self, message):
        logging.info('_all_allocations_handler: {}'.format(message))
        global subscription_mgr, subscribe, state
        if message['event'] == 'requested':
            return
        assert message['event'] == 'created'

        idx = message['allocationID']
        info = message['allocationInfo']
        requirements = message['requirements']
        hosts = message['allocated']
        logging.debug('New allocation: {}.'.format(hosts))
        logging.info('Subscribing to new allocation (#{}).'.format(idx))
        allocation_handler = partial(self._pika_allocation_handler, idx)
        self._subscription_mgr.registerForAllocation(idx, allocation_handler)
        logging.info('Susbcribed')
        self._allocation_subscriptions.add(idx)
        for name, host_id in hosts.iteritems():
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
        logging.info("Inserting to DB: {}".format(record))

        collection = self._db.inaugurations
        try:
            collection.insert_one(record)
        except Exception:
            print '\n\n\n\nError while inserting record \n\n\n\n'


def create_connections():
    _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
    subscription_mgr = Subscribe(amqp_url)


def main(ready_event=None, stop_event=threading.Event()):
    global subscription_mgr
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    if ready_event is None:
        ready_event = threading.Event()

    db = pymongo.MongoClient()
    logging.info("Initializing allocations handler....")
    handler = AllocationsHandler(db, ready_event)
    handler.start()
    ready_event.wait()
    stop_event.wait()
    handler.stop()
    handler.join()
    logging.info("Done.")

if __name__ == '__main__':
    main()
