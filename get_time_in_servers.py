import os
import time

all = ['rack01-server15',
 'rack01-server49',
 'rack05-server57',
 'rack05-server24',
 'rack01-server45',
 'rack05-server17',
 'rack05-server58',
 'rack05-server46',
 'rack02-server02',
 'rack02-server32',
 'rack05-server44',
 'rack05-server54',
 'rack04-server01',
 'rack05-server55',
 'rack05-server29',
 'rack02-server33',
 'rack01-server27',
 'rack01-server13',
 'rack05-server56',
 'rack04-server60',
 'rack05-server53']

nr_hosts = len(all)
for host_idx, host in enumerate(all):

    cmd = "ipmitool -I lanplus -U root -P strato -H {}-ipmi.stratolab sel time get".format(host)
    print "{}/{}: {}".format(host_idx + 1, nr_hosts, cmd)
    result = os.system(cmd)
    if result != 0:
        print "Error executing command for {}: {}".format(host, result)
        time.sleep(0.5)
