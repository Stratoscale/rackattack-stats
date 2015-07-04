#!/bin/sh
RAP_ADDR='rack01-server58' RACKATTACK_PROVIDER_URI="${RAP_ADDR}" RACKATTACK_PROVIDER=tcp://${RACKATTACK_PROVIDER_URI}:1014@@amqp://${RACKATTACK_PROVIDER_URI}:1013/%2F@@:${RACKATTACK_PROVIDER_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=~`dirname '$0'`/py python ~/work/rackattack-stats/standalone_scripts/print_hosts_stats.py
