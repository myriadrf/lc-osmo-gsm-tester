#!/usr/bin/env python3
from osmo_gsm_tester.testenv import *
import os
import ipaddress

epc = tenv.epc()
enb = tenv.enb()
ue = tenv.modem()

epc.subscriber_add(ue)
epc.start()
enb.ue_add(ue)
enb.start(epc)

print('waiting for ENB to connect to EPC...')
wait(epc.enb_is_connected, enb)
print('ENB is connected to EPC')

ue.connect(('001', '01'))
print('waiting for UE to attach...')
wait(ue.is_registered, None)
print('UE is attached')

print("EPC addr: {}".format(str(epc.tun_addr())))

sleep(5)

print("Setting up interface")
connmgr = ue.dbus.interface('org.ofono.ConnectionManager')
contexts = connmgr.GetContexts()
for path, properties in contexts:
    settings = properties['Settings']
    print(settings)
    if "Method" in settings:
        intf = settings['Interface']
        addr = settings['Address']
        gateway = settings['Gateway']
        method = settings['Method']
        util.move_iface_to_netns(intf, ue.netns(), ue.run_dir.new_dir('move_netns'))
        ue.run_netns_wait('flush_ip', ('ip', 'addr', 'flush', 'dev', intf))
        sleep(3)
        ue.run_netns_wait('up_interface', ('ip', 'link', 'set', 'dev', intf, 'up'))
        sleep(3)

        if len(addr) > 0:
                addr += "/" + str(ipaddress.IPv4Network('0.0.0.0/{}'.format(settings["Netmask"])).prefixlen)
        if method == "static":
            ue.run_netns_wait('add_addr', ('ip', 'addr', 'add', addr, 'dev', intf))
            sleep(3)
            ue.run_netns_wait('add_route', ('ip', 'route', 'add', 'default', 'via', gateway, 'dev', intf))

print("Configured interface")


proc = ue.run_netns_wait('ping', ('ping', '-c', '10', epc.tun_addr()))
output = proc.get_stdout()
print(output)
test.set_report_stdout(output)
