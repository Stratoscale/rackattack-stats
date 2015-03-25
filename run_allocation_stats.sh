#!/bin/sh
RAP_URI='rack01-server58' RACKATTACK_PROVIDER=tcp://${RAP_URI}:1014@@amqp://guest:guest@${RAP_URI}:1013/%2F@@http://${RAP_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=~/work/rackattack-physical-dashboard/py python ~/work/rackattack-stats/main_allocation_stats.py
#RACKATTACK_PROVIDER=tcp://rack01-server58:1014@@amqp://guest:guest@rack01-server58:1013/%2F@@http://rack01-server58:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=~/work/rackattack-physical-dashboard/py python ~/work/rackattack-stats/main_allocation_stats.py
