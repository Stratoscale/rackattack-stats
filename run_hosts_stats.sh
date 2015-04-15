#!/bin/sh
RAP_URI='rackattack-provider.dc1.strato' RACKATTACK_PROVIDER_URI="tcp://${RAP_URI}" RACKATTACK_PROVIDER=${RACKATTACK_PROVIDER_URI}:1014@${RACKATTACK_PROVIDER_URI}:1015@:${RACKATTACK_PROVIDER_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=~/work/rackattack-physical-dashboard/py python ~/work/rackattack-stats/rackattack/stats/main_hosts_stats.py

