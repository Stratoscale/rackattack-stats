#!/bin/sh
RAP_ADDR='10.16.3.1' RACKATTACK_PROVIDER_URI="${RAP_ADDR}" RACKATTACK_PROVIDER=tcp://${RACKATTACK_PROVIDER_URI}:1014@@amqp://${RACKATTACK_PROVIDER_URI}:1013/%2F@@:${RACKATTACK_PROVIDER_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=~/work/rackattack-physical-dashboard/py python ~/work/rackattack-stats/standalone_scripts/print_hosts_stats.py
