#!/bin/sh
RAP_URI='rackattack-provider.dc1.strato' RACKATTACK_PROVIDER_URI="tcp://${RAP_URI}" RACKATTACK_PROVIDER=${RACKATTACK_PROVIDER_URI}:1014@${RACKATTACK_PROVIDER_URI}:1015@:${RACKATTACK_PROVIDER_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=`dirname '$0'`/py python ~/work/rackattack-stats/py/rackattack/stats/main_hosts_stats.py

