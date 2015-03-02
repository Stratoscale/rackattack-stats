"""This monitors RackAttack's nodes periodically, inserts them to a dedicated
DB, and sends alerts, by mail, about servers which are not behaving properly"""


import pytz
import yaml
import time
import socket
import logging
import smtplib
import datetime
import traceback
from time import sleep
from email.mime.text import MIMEText
from rackattack import clientfactory
from elasticsearch import Elasticsearch


SAMPLE_INTERVAL_NR_SECONDS = 60
EMAIL_SUBSCRIBERS = ("eliran@stratoscale.com",)
RACKATTACK_CONFIG_FILENAME = '/etc/rackattack.physical.rack.yaml'
DB_NAME = 'rackattack_stats'
SEND_ALERTS_BY_MAIL = True
SENDER_EMAIL = "eliran@stratoscale.com"
SMTP_SERVER = 'localhost'
TIMEZONE = 'Asia/Jerusalem'


state = {'online_for_no_reason': set(),
         'offline_for_no_reason': set()}


def configure_logger():
    logger = logging.getLogger('rackattack_stats')
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    logger.addHandler(stream_handler)
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


def check_no_longer_offline_for_no_reason(offline_for_no_reason,
                                          configured_to_be_online,
                                          actually_online):
    """Return a message string describing nodes no longer offline for no reason.
    """
    msg = ''

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
        msg += 'The following servers were just now configured to be (and ' \
            'are) offline:\n'
        msg += str(no_longer_offline_for_no_reason_case_1) + "\n"
    # The latter case
    no_longer_offline_for_no_reason_case_2 = [host for host in
                                              no_longer_offline_for_no_reason
                                              if host in actually_online]
    if no_longer_offline_for_no_reason_case_2:
        msg += 'The following servers went back online as configured (after ' \
            'being offline for some reason):\n'
        msg += str(no_longer_offline_for_no_reason_case_2) + "\n"

    return msg


def check_no_longer_online_for_no_reason(online_for_no_reason,
                                         configured_to_be_online,
                                         actually_online):
    """Return a message string describing nodes no longer online for no reason.
    """
    # Servers that were previously "online for no reason" and are now not
    # "online for no reason" could be in that state either because:
    # *    They were configured to be online (after last cycle)
    # *    or because they went back offline

    msg = ''
    no_longer_online_for_no_reason = set([host for host in
                                          state['online_for_no_reason'] if
                                          host not in online_for_no_reason])
    # The former case
    no_longer_online_for_no_reason_case_1 = [host for host in
                                             no_longer_online_for_no_reason if
                                             host in configured_to_be_online]
    if no_longer_online_for_no_reason_case_1:
        msg += 'The following servers were just now configured to be (and ' \
            'are) online:\n'
        msg += str(no_longer_online_for_no_reason_case_1) + "\n"
    # The latter case
    no_longer_online_for_no_reason_case_2 = [host for host in
                                             no_longer_online_for_no_reason if
                                             host not in actually_online]
    if no_longer_online_for_no_reason_case_2:
        msg += 'The following servers went back offline as configured (after' \
            ' being online for some reason):\n'
        msg += str(no_longer_online_for_no_reason_case_2) + "\n"

    return msg


def cross_with_nodes_configuration(items):
    """Add info about nodes that are configured as online but aren't online"""
    # Parse the configuration file

    configuration = yaml.load(open(RACKATTACK_CONFIG_FILENAME, 'rb'))

    # Get info on errornous servers by comparing the stats to the configuration
    configured_to_be_online = \
        [host['id'] for host in configuration['HOSTS'] if not host['offline']]
    actually_online = [host['id'] for host in items]
    online_for_no_reason = set([host for host in actually_online if
                                host not in configured_to_be_online])
    offline_for_no_reason = set([host for host in configured_to_be_online if
                                 host not in actually_online])

    # Report errors
    msg = ''
    newly_offline_for_no_reason = set([host for host in offline_for_no_reason
                                       if host not in
                                       state['offline_for_no_reason']])
    if newly_offline_for_no_reason:
        msg += 'The following servers were found offline (although ' \
            'configured to be online):\n'
        msg += str(newly_offline_for_no_reason) + "\n"

    newly_online_for_no_reason = set([host for host in online_for_no_reason if
                                      host not in state['online_for_no_reason']
                                      ])
    if newly_online_for_no_reason:
        msg += 'The following servers were found online (although configured' \
            ' to be offline):\n'
        msg += str(newly_online_for_no_reason) + "\n"

    msg += check_no_longer_offline_for_no_reason(offline_for_no_reason,
                                                 configured_to_be_online,
                                                 actually_online)
    msg += check_no_longer_online_for_no_reason(online_for_no_reason,
                                                configured_to_be_online,
                                                actually_online)

    # Update state
    state['offline_for_no_reason'] = offline_for_no_reason
    state['online_for_no_reason'] = online_for_no_reason

    if msg:
        logger = logging.getLogger('rackattack_stats')
        logger.info(msg)
        global SEND_ALERTS_BY_MAIL
        if SEND_ALERTS_BY_MAIL:
            send_mail(msg)


def fetch_stats(rackattack_client, db):
    """Fetch RackAttack stats, add them to db and alert on errors"""
    unixtime = int(time.time() * 1000)
    cur_time = pytz.timezone(TIMEZONE).localize(datetime.datetime.now())
    # Get stats from RackAttack
    stats = rackattack_client.call('admin__queryStatus')

    # Insert stats to the DB
    for collection_name, items in stats.iteritems():
        logger = logging.getLogger('rackattack_stats')
        logger.debug('Inserting {} records to to collection "{}"'
                     .format(len(items), collection_name))

        # Insert each item as a single record
        for index, item in enumerate(items):
            # generate a unique id (id field is mandatory :/ )
            id = "%d%03d" % (unixtime, index)
            item['timestamp'] = cur_time
            item['_timestamp'] = cur_time

            collection_name_to_index = {'hosts': 'hosts_stats',
                                        'allocations': 'allocations'}
            db.create(index=collection_name_to_index[collection_name],
                      doc_type=collection_name,
                      body=item,
                      id=id)

    # Use a special index that already counts the states (typos are in
    # rackattack)
    states = ('INAUGURATION_DONE', 'CHECKED_IN',
              'SLOW_RECLAIMATION_IN_PROGRESS', 'INAUGURATION_LABEL_PROVIDED',
              'QUICK_RECLAIMATION_IN_PROGRESS')
    for state in states:
        record = {'state': state,
                  'states_count': len([host for host in stats['hosts'] if
                                       host['state'] == state]),
                  'timestamp': cur_time,
                  '_timestamp': cur_time}
        id = "%d%03d" % (unixtime, index)
        db.create(index='states', doc_type='state_count', body=record, )

    # Alert about servers that died and stuff
    cross_with_nodes_configuration(stats['hosts'])


def main():
    configure_logger()
    # Connect to RackAttack
    client = clientfactory.factory()
    # Connect to ElasticSearch
    db = Elasticsearch()

    # Fetch stats forever
    while True:
        try:
            fetch_stats(client, db)
        except KeyboardInterrupt:
            return
        except Exception:
            logger = logging.getLogger('rackattack_stats')
            logger.error(traceback.format_exc())
        finally:
            sleep(SAMPLE_INTERVAL_NR_SECONDS)


if __name__ == '__main__':
    main()
