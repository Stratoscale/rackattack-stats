"""This script does several things:
* Monitors RackAttack's nodes periodically and inserts them to a dedicated
  ElasticSearch DB
* Sends alerts, by mail, about servers which are not behaving properly
* Monitors RackAttack's Allocation failure log records, and insert them info
  a dedicated ElasticSearch DB.
"""


import os
import pytz
import yaml
import time
import errno
import signal
import socket
import logging
import smtplib
import datetime
import traceback
import subprocess
from time import sleep
from StringIO import StringIO
from email.mime.text import MIMEText
from rackattack import clientfactory
from elasticsearch import Elasticsearch, NotFoundError
# from rackattack.common.hoststatemachine import ALLOCATION_LOG_FILENAME
ALLOCATION_LOG_FILENAME = "/var/log/rackattack-allocation-failures.json"

# Interesting configuration
SAMPLE_INTERVAL_NR_SECONDS = 60
EMAIL_SUBSCRIBERS = ("eliran@stratoscale.com",)
SEND_ALERTS_BY_MAIL = True


# Less interesting configuration
RAP_CONFIGURATION_FILENAME = 'rackattack.physical.rack.yaml'
SENDER_EMAIL = "eliran@stratoscale.com"
SMTP_SERVER = 'localhost'
TIMEZONE = 'Asia/Jerusalem'
RAP_PASSWORD = 'strato'
# Cycle length of activation of servers which are offline for no reason
UPDATE_ALLOCATION_FAILURE_DB_PERIOD = 60 * 10
RAP_CONFIGURATION_FILEPATH = '/etc/{}'.format(RAP_CONFIGURATION_FILENAME)
TEMP_DIR_PATH = '/tmp'
# This currently is not doint anything:
ACTIVATE_SERVERS_PERIOD = 60 * 60 * 4


# State stuff
state = {'online_for_no_reason': set(),
         'offline_for_no_reason': set(),
         'allocation_failures': []}
last_time = 0
msg_so_far = ''


def log_msg(msg, level=logging.INFO):
    """Log the given message and add it to the global `msg_so_far` variable"""
    logger = logging.getLogger('rackattack_stats')
    logger.log(level=level, msg=msg)
    global msg_so_far
    msg_so_far += msg + '\n'


def configure_logger():
    logger = logging.getLogger('rackattack_stats')
    logger.setLevel(logging.INFO)


def send_mail(msg):
    msg = MIMEText(msg)
    msg['Subject'] = 'RackAttack Status Alert {}'.format(time.ctime())
    msg['From'] = SENDER_EMAIL
    msg['To'] = ",".join(EMAIL_SUBSCRIBERS)

    # Send the message via our own SMTP server, but don't include the
    # envelope header.
    try:
        s = smtplib.SMTP(SMTP_SERVER)
    except socket.error:
        global SEND_ALERTS_BY_MAIL
        SEND_ALERTS_BY_MAIL = False
        msg = 'Could not connect to an SMTP server at "{}"'.format(SMTP_SERVER)
        raise Exception(msg)
    s.sendmail(msg['From'], EMAIL_SUBSCRIBERS, msg.as_string())
    s.quit()


def pretty_list(lst):
    """Return a pretty string representation of the given list"""
    result = '\t* '
    result += '\n\t* '.join(lst)
    result += "\n"
    return result


def dump_configuration(configuration):
    global RACKATTACK_CONFIG_FILENAME
    yaml.dump(configuration, open(RACKATTACK_CONFIG_FILENAME, 'wb'))


def acticvate_servers(servers, cur_time):
    """Activate the servers in the given list"""
    # Validate that enough time has passed
    global last_time
    if cur_time - last_time < ACTIVATE_SERVERS_PERIOD:
        return
    last_time = cur_time

    log_msg('Trying to activate the servers which are offline for no reason.')

    # Turn off all servers first
    global RACKATTACK_CONFIG_FILENAME
    configuration = yaml.load(open(RACKATTACK_CONFIG_FILENAME, 'rb'))
    for server in configuration['HOSTS']:
        if server['id'] in servers:
            server['offline'] = True
    dump_configuration(configuration)
    # Kill RackAttack
    try:
        pid = int(subprocess.check_output(
            "ps aux | grep 'rackattack\.physical\.main' | awk '{print $2}' ",
            shell=True))
    except ValueError:
        log_msg("Error while activating servers: Cannot find the PID for "
                "RackAttack for some reason.",
                level=logging.ERROR)
        return
    os.kill(pid, signal.SIGHUP)
    time.sleep(5)

    # Now turn on the servers
    for server in configuration['HOSTS']:
        if server['id'] in servers:
                server['offline'] = False
    dump_configuration()
    # Kill RackAttack
    os.kill(pid, signal.SIGHUP)


def check_no_longer_offline_for_no_reason(offline_for_no_reason,
                                          configured_to_be_online,
                                          actually_online):
    """Return a message string describing nodes no longer offline for no reason.
    """
    # Servers that were previously "offline for no reason" and are now not,
    # could be in that state either because:
    # *    They were configured to be offline (after last cycle)
    # *    or because they went back online
    no_longer_offline_for_no_reason = set([host for host in
                                           state['offline_for_no_reason']
                                           if host not in offline_for_no_reason
                                           ])
    # The former case
    no_longer_offline_for_no_reason_case_1 = [host for host in
                                              no_longer_offline_for_no_reason
                                              if host not in
                                              configured_to_be_online]
    if no_longer_offline_for_no_reason_case_1:
        log_msg('The following servers were just now configured to be, and '
                'became offline:')
        log_msg(pretty_list(no_longer_offline_for_no_reason_case_1))
    # The latter case
    no_longer_offline_for_no_reason_case_2 = [host for host in
                                              no_longer_offline_for_no_reason
                                              if host in actually_online]
    if no_longer_offline_for_no_reason_case_2:
        log_msg('The following servers went back online as configured (after '
                'being offline for some reason):')
        log_msg(pretty_list(no_longer_offline_for_no_reason_case_2))


def check_no_longer_online_for_no_reason(online_for_no_reason,
                                         configured_to_be_online,
                                         actually_online):
    """Return a message string describing nodes no longer online for no reason.
    """
    # Servers that were previously "online for no reason" and are now not
    # "online for no reason" could be in that state either because:
    # *    They were configured to be online (after last cycle)
    # *    or because they went back offline
    no_longer_online_for_no_reason = set([host for host in
                                          state['online_for_no_reason'] if
                                          host not in online_for_no_reason])
    # The former case
    no_longer_online_for_no_reason_case_1 = [host for host in
                                             no_longer_online_for_no_reason if
                                             host in configured_to_be_online]
    if no_longer_online_for_no_reason_case_1:
        log_msg('The following servers were just now configured to be, and '
                'became online:')
        log_msg(pretty_list(no_longer_online_for_no_reason_case_1))
    # The latter case
    no_longer_online_for_no_reason_case_2 = [host for host in
                                             no_longer_online_for_no_reason if
                                             host not in actually_online]
    if no_longer_online_for_no_reason_case_2:
        log_msg('The following servers went back offline as configured (after'
                ' being online for some reason):')
        log_msg(pretty_list(no_longer_online_for_no_reason_case_2))


def add_whole_state(online_for_no_reason,
                    offline_for_no_reason,
                    newly_online_for_no_reason,
                    newly_offline_for_no_reason):
    # Only write stuff in case there are new errors (since this is sent by
    # mail
    global msg_so_far
    if msg_so_far:
        if offline_for_no_reason:
            servers_list = ' '.join(offline_for_no_reason)
            cmd = "python ~/return_servers_to_rack.py {}".format(servers_list)
            log_msg("\nRun the following command on rackattack-provider.dc1."
                    "strato: {}".format(cmd))

        if offline_for_no_reason and \
                offline_for_no_reason != newly_offline_for_no_reason:
            log_msg('The entire list of servers which are offline (and are '
                    'not supposed to be) is:')
            log_msg(pretty_list(offline_for_no_reason))

        if online_for_no_reason and \
                online_for_no_reason != newly_online_for_no_reason:
            log_msg('The entire list of servers which are online (and are '
                    'not supposed to be) is:')
            log_msg(pretty_list(online_for_no_reason))


def check_errornous_servers(items, configuration):
    """Add info about nodes that are configured as online but aren't online"""
    # Parse the configuration file
    configuration = yaml.load(StringIO(configuration))

    # Get info on errornous servers by comparing the stats to the configuration
    configured_to_be_online = \
        [host['id'] for host in configuration['HOSTS'] if not host['offline']]
    actually_online = [host['id'] for host in items]
    online_for_no_reason = set([host for host in actually_online if
                                host not in configured_to_be_online])
    offline_for_no_reason = set([host for host in configured_to_be_online if
                                 host not in actually_online])

    # Report errors
    newly_offline_for_no_reason = set([host for host in offline_for_no_reason
                                       if host not in
                                       state['offline_for_no_reason']])
    if newly_offline_for_no_reason:
        log_msg('The following servers were found offline (although '
                'configured to be online):')
        log_msg(pretty_list(newly_offline_for_no_reason))

    newly_online_for_no_reason = set([host for host in online_for_no_reason if
                                      host not in state['online_for_no_reason']
                                      ])
    if newly_online_for_no_reason:
        log_msg('The following servers were found online (although configured'
                ' to be offline):')
        log_msg(pretty_list(newly_online_for_no_reason))

    check_no_longer_offline_for_no_reason(offline_for_no_reason,
                                          configured_to_be_online,
                                          actually_online)
    check_no_longer_online_for_no_reason(online_for_no_reason,
                                         configured_to_be_online,
                                         actually_online)

    # Update state
    state['offline_for_no_reason'] = offline_for_no_reason
    state['online_for_no_reason'] = online_for_no_reason

    # Add the whole state, for convenience (this might be different than the
    # previously-sent lists, since this is the state, and the lists sent so far
    # were diffs.
    add_whole_state(online_for_no_reason,
                    offline_for_no_reason,
                    newly_online_for_no_reason,
                    newly_offline_for_no_reason)


def fetch_from_rap(filepath):
    # Transfer the file to a local dir
    global TEMP_DIR_PATH
    dest_filepath = os.path.join(TEMP_DIR_PATH, os.path.basename(filepath))
    cmd = 'sshpass -p "{}" scp root@{}:{} {}' \
          .format(RAP_PASSWORD,
                  os.environ['RAP_ADDR'],
                  filepath,
                  dest_filepath)
    subprocess.check_call(cmd, shell=True)

    # Read the local file
    content = ''
    with open(dest_filepath, 'rb') as f:
        content = f.read()
    return content


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


def fetch_nodes_stats(rackattack_client, db, timestamp):
    """Fetch RackAttack stats, add them to db and alert on errors"""
    logger = logging.getLogger('rackattack_stats')

    global msg_so_far
    msg_so_far = ''

    unixtime = int(timestamp * 1000)
    datetime_now = datetime_from_timestamp(timestamp)

    logger.debug('Fetching configuration from RAP...')
    configuration = fetch_from_rap(RAP_CONFIGURATION_FILEPATH)

    # Get stats from RackAttack
    stats = rackattack_client.call('admin__queryStatus')

    # Insert stats to the DB
    for collection_name, items in stats.iteritems():
        logger.debug('Inserting {} records to to collection "{}"'
                     .format(len(items), collection_name))

        # Insert each item as a single record
        for index, item in enumerate(items):
            # generate a unique id (id field is mandatory :/ )
            id = "%d%03d" % (unixtime, index)
            item['timestamp'] = datetime_now
            item['_timestamp'] = datetime_now

            collection_name_to_index = {'hosts': 'hosts_stats',
                                        'allocations': 'allocations'}
            db.create(index=collection_name_to_index[collection_name],
                      doc_type=collection_name,
                      body=item,
                      id=id)

    # Use a special index that already counts the states (typos are in
    # rackattack). This is quite ugly, but we haven't managed to craete a
    # query in Kibana which does an average on the count of states (2
    # aggregations)
    states = ('INAUGURATION_DONE', 'CHECKED_IN',
              'SLOW_RECLAIMATION_IN_PROGRESS', 'INAUGURATION_LABEL_PROVIDED',
              'QUICK_RECLAIMATION_IN_PROGRESS')
    for _state in states:
        record = {'state': _state,
                  'states_count': len([host for host in stats['hosts'] if
                                       host['state'] == _state]),
                  'timestamp': datetime_now,
                  '_timestamp': datetime_now}
        id = "%d%03d" % (unixtime, index)
        db.create(index='states', doc_type='state_count', body=record)

    # Alert about servers that died and stuff
    check_errornous_servers(stats['hosts'], configuration)

    if msg_so_far:
        global SEND_ALERTS_BY_MAIL
        if SEND_ALERTS_BY_MAIL:
            send_mail(msg_so_far)


def fetch_allocation_failures(db):
    log = fetch_from_rap(ALLOCATION_LOG_FILENAME)
    log = yaml.load(StringIO(log))

    index_name = 'allocation_failures'
    doc_type = 'allocation_failure'

    # Validate that the state is contained in the log (from the beginning)
    if len(log) < len(state['allocation_failures']):
        state['allocation_failures'] = []
    else:
        for idx, state_log_record in enumerate(state['allocation_failures']):
            if state_log_record != log[idx]:
                # Reset the state
                state['allocation_failures'] = []
                break

    # Insert the new records in the log (which are not in the state)
    new_records = log[len(state['allocation_failures']):]
    for timestamp, host_id in new_records:
        record_datetime = datetime_from_timestamp(timestamp)
        # Validate first that such record does not exist (this might occur
        # if the script started executing, with the same DB, after crashing).
        # I would use the upsert method of elasticsearch, but there's no such
        # method in elasticserachpy as far as i know.
        # Convert record timestamp to ElasticSearch's format
        record_datetime_str = str(record_datetime).replace(' ', 'T')
        query = "host_id:\"{}\" AND timestamp:\"{}\"". \
            format(host_id, record_datetime_str)
        try:
            result = db.search(index=index_name, doc_type=doc_type, q=query)
            if result['hits']['total'] > 0:
                # Skip this log record since it already exists in the DB
                print '\n\n\t\tSkipping\n\n'
                continue
        except NotFoundError:
            pass

        # Insert the record at last
        record = {"_timestamp": record_datetime,
                  "timestamp": record_datetime,
                  "host_id": host_id}
        db.create(index=index_name, doc_type=doc_type, body=record)


def main():
    configure_logger()
    logger = logging.getLogger('rackattack_stats')
    # Connect to RackAttack
    client = clientfactory.factory()
    # Connect to ElasticSearch
    db = Elasticsearch()

    # Fetch stats forever
    while True:
        try:
            fetch_nodes_stats(client, db, time.time())
        except KeyboardInterrupt:
            return
        except socket.error, e:
            logger.error(traceback.format_exc())
            if isinstance(e.args, tuple):
                if e[0] in (errno.EPIPE, errno.EBADF):
                    logger.error("Detected disconnection (error {}), trying "
                                 "to reconnect...".format(e[0]))
                    client.close()
                    client = clientfactory.factory()
                else:
                    logger.error("Unknown socket error: {}".format(str(e)))
            else:
                logger.error("Unknown socket error: {}".format(str(e)))
        except Exception:
            logger.error(traceback.format_exc())
        finally:
            sleep(SAMPLE_INTERVAL_NR_SECONDS)


if __name__ == '__main__':
    main()
