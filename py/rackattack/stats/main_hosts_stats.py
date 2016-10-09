"""This script does several things:
* Monitors RackAttack's nodes periodically and inserts them to a dedicated
  ElasticSearch DB
* Sends alerts, by mail, about servers which are not behaving properly
"""


import os
import pytz
import yaml
import time
import socket
import logging
import smtplib
import datetime
import traceback
import elasticsearch
from time import sleep
import rackattack.tcp.transport
from email.mime.text import MIMEText
from rackattack import clientfactory
from rackattack.stats import config
from rackattack.stats import logconfig
from rackattack.stats import elasticsearchdbwrapper


# Interesting configuration
EMAIL_SUBSCRIBERS = ("eliran@stratoscale.com", "rom@stratoscale.com")
SEND_ALERTS_BY_MAIL = False


# Less interesting configuration
SAMPLE_INTERVAL_NR_SECONDS = 60
SENDER_EMAIL = "eliran@stratoscale.com"
SMTP_SERVER = 'localhost'


# In case we cannot connect to sockets and stuff, this
# changes to False. Needed in order to send mail only when
# switching between states, and not on every failure to
# establish a connection, on connection creation retries)
is_connected = False
msg_so_far = ''

# Connections
rackattack_client = None
db = None


def log_msg(msg, level=logging.INFO):
    """Log the given message and add it to the global `msg_so_far` variable"""
    logger = logging.getLogger('rackattack_stats')
    logger.log(level=level, msg=msg)
    global msg_so_far
    msg_so_far += msg + '\n'


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
        raise Exception(msg)
    s.sendmail(msg['From'], EMAIL_SUBSCRIBERS, msg.as_string())
    s.quit()


def flush_msgs_to_mail():
    global msg_so_far
    if msg_so_far:
        send_mail(msg_so_far)
    msg_so_far = ''


def datetime_from_timestamp(timestamp):
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(config.TIMEZONE).localize(datetime_now)
    return datetime_now


def fetch_nodes_stats(timestamp):
    """Fetch RackAttack stats, add them to db and alert on errors"""
    logger = logging.getLogger('rackattack_stats')

    # Get stats from RackAttack
    logger.debug('Fetching state from RAP...')
    stats = rackattack_client.call('admin__queryStatus')
    logger.info("Got state response from Rackattack.")

    # Insert stats to the DB
    unixtime = int(timestamp * 1000)
    datetime_now = datetime_from_timestamp(timestamp)
    # Use a special index that already counts the states (typos are in
    # rackattack). This is quite ugly, but we haven't managed to craete a
    # query in Kibana which does an average on the count of states (2
    # aggregations)
    states = list(set([host['state'] for host in stats['hosts']]))
    for idx, _state in enumerate(states):
        record = {'state': _state,
                  'states_count': len([host for host in stats['hosts'] if host['state'] == _state]),
                  'date': datetime_now}
        id = "%13d%03d" % (unixtime, idx)
        logger.info("Inserting a record to the DB: {}...".format(record))
        db.create(index='states', doc_type='state_count', body=record, id=id)
        logger.info("Record inserted to DB.")

    pools = list(set([host['pool'] for host in stats['hosts']]))
    for idx, _pool in enumerate(pools):
        record = {'pool': _pool,
                  'count': len([host for host in stats['hosts'] if host['pool'] == _pool]),
                  'date': datetime_now}
        logger.info("Inserting a record to the DB: {}...".format(record))
        db.create(index='pools', doc_type='pool_count', body=record)
        logger.info("Record inserted to DB.")
    flush_msgs_to_mail()


def create_connection(factory_function, service_name):
    logger = logging.getLogger('rackattack_stats')
    client = factory_function()
    logger.info('Connected.')
    return client


def create_connections():
    global rackattack_client, db
    # Reload because somehow the socket gets recreated only when this happens
    reload(rackattack.tcp.transport)
    reload(clientfactory)
    rackattack_client = clientfactory.factory()
    db = elasticsearchdbwrapper.ElasticserachDBWrapper(alert_func=send_mail)


def validate_rackattack_client_connection_is_closed():
    try:
        rackattack_client.close()
    except:
        pass


def socket_error_recovery(is_first_connection_attampt):
    global is_connected
    log_msg("Socket error:", level=logging.ERROR)
    log_msg(traceback.format_exc(), level=logging.ERROR)
    log_msg("Trying to reconnect in about {} seconds."
            .format(SAMPLE_INTERVAL_NR_SECONDS),
            level=logging.ERROR)

    # Alert by mail, if this is the first error after at least one
    # successful execution of fetch_nodes_stats.
    if is_connected or is_first_connection_attampt:
        is_connected = False
        flush_msgs_to_mail()

    validate_rackattack_client_connection_is_closed()


def main():
    global is_connected
    logconfig.configure_logger()
    logger = logging.getLogger('rackattack_stats')
    is_first_connection_attampt = True

    # Fetch stats forever
    while True:
        try:
            if not is_connected:
                logger.info('Attempting to create connections...')
                create_connections()
                logger.info("Connections created successfully.")

            fetch_nodes_stats(time.time())
            if not is_connected and not is_first_connection_attampt:
                send_mail('RackAttack Stats is connected and works again.')
            is_connected = True
        except KeyboardInterrupt:
            break
        except socket.error:
            socket_error_recovery(is_first_connection_attampt)
        except rackattack.tcp.transport.TimeoutError:
            socket_error_recovery(is_first_connection_attampt)
        except rackattack.tcp.transport.RemotelyClosedError:
            socket_error_recovery(is_first_connection_attampt)
        except elasticsearch.ConnectionTimeout:
            socket_error_recovery(is_first_connection_attampt)
        except elasticsearch.ConnectionError:
            socket_error_recovery(is_first_connection_attampt)
        except elasticsearch.exceptions.TransportError:
            socket_error_recovery(is_first_connection_attampt)
        except Exception:
            flush_msgs_to_mail()
            log_msg("Critical error, exiting.")
            log_msg(traceback.format_exc())
            flush_msgs_to_mail()
            break
        finally:
            is_first_connection_attampt = False

        sleep(SAMPLE_INTERVAL_NR_SECONDS)

    validate_rackattack_client_connection_is_closed()


if __name__ == '__main__':
    main()
