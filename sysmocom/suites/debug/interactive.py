#!/usr/bin/env python3
from osmo_gsm_tester.testenv import *
hlr = tenv.hlr()
bts = tenv.bts()
pcu = bts.pcu()
mgw_msc = tenv.mgw()
mgw_bsc = tenv.mgw()
stp = tenv.stp()
ggsn = tenv.ggsn()
sgsn = tenv.sgsn(hlr, ggsn)
msc = tenv.msc(hlr, mgw_msc, stp)
bsc = tenv.bsc(msc, mgw_bsc, stp)

modems = tenv.modems(int(prompt('How many modems?')))

bsc.bts_add(bts)
sgsn.bts_add(bts)

hlr.start()
stp.start()
ggsn.start()
sgsn.start()
msc.start()
mgw_msc.start()
mgw_bsc.start()
bsc.start()

bts.start()
print('Waiting for bts to connect to bsc...')
wait(bsc.bts_is_connected, bts)
print('Waiting for bts to be ready...')
wait(bts.ready_for_pcu)
pcu.start()

for m in modems:
  hlr.subscriber_add(m)
  m.connect(msc.mcc_mnc())

while True:
  cmd = prompt('Enter command: (q)uit (d)ebug (s)ms (g)et-registered (w)ait-registered, call-list [<ms_msisdn>], call-dial <src_msisdn> <dst_msisdn>, call-wait-incoming <src_msisdn> <dst_msisdn>, call-answer <mt_msisdn> <call_id>, call-hangup <ms_msisdn> <call_id>, ussd <command>, data-attach, data-wait, data-detach, data-activate')
  cmd = cmd.strip().lower()

  if not cmd:
    continue

  params = cmd.split()

  if 'quit'.startswith(cmd):
    break

  elif 'debug'.startswith(cmd):
    import pdb; pdb.set_trace()

  elif 'wait-registered'.startswith(cmd):
    try:
      for m in modems:
          wait(m.is_registered, msc.mcc_mnc())
      wait(msc.subscriber_attached, *modems)
    except Timeout:
      print('Timeout while waiting for registration.')

  elif 'get-registered'.startswith(cmd):
    print(msc.imsi_list_attached())
    print('RESULT: %s' %
       ('All modems are registered.' if msc.subscriber_attached(*modems)
        else 'Some modem(s) not registered yet.'))

  elif 'sms'.startswith(cmd):
    for mo in modems:
      for mt in modems:
        mo.sms_send(mt.msisdn(), 'to ' + mt.name())

  elif cmd.startswith('call-list'):
      if len(params) != 1 and len(params) != 2:
        print('wrong format')
        continue
      for ms in modems:
        if len(params) == 1 or str(ms.msisdn()) == params[1]:
          print('call-list: %r %r' % (ms.name(), ms.call_id_list()))

  elif cmd.startswith('call-dial'):
    if len(params) != 3:
      print('wrong format')
      continue
    src_msisdn, dst_msisdn = params[1:]
    for mo in modems:
      if str(mo.msisdn()) == src_msisdn:
        print('dialing %s->%s' % (src_msisdn, dst_msisdn))
        call_id = mo.call_dial(dst_msisdn)
        print('dial success: call_id=%r' % call_id)

  elif cmd.startswith('call-wait-incoming'):
    if len(params) != 3:
      print('wrong format')
      continue
    src_msisdn, dst_msisdn = params[1:]
    for mt in modems:
      if str(mt.msisdn()) == dst_msisdn:
        print('waiting for incoming %s->%s' % (src_msisdn, dst_msisdn))
        call_id = mt.call_wait_incoming(src_msisdn)
        print('incoming call success: call_id=%r' % call_id)

  elif cmd.startswith('call-answer'):
    if len(params) != 3:
      print('wrong format')
      continue
    mt_msisdn, call_id = params[1:]
    for mt in modems:
      if str(mt.msisdn()) == mt_msisdn:
        print('answering %s %r' % (mt.name(), call_id))
        mt.call_answer(call_id)

  elif cmd.startswith('call-hangup'):
    if len(params) != 3:
      print('wrong format')
      continue
    ms_msisdn, call_id = params[1:]
    for ms in modems:
      if str(ms.msisdn()) == ms_msisdn:
        print('hanging up %s %r' % (ms.name(), call_id))
        ms.call_hangup(call_id)

  elif cmd.startswith('ussd'):
    if len(params) != 2:
      print('wrong format')
      continue
    ussd_cmd = params[1]
    for ms in modems:
        print('modem %s: ussd %s' % (ms.name(), ussd_cmd))
        response = ms.ussd_send(ussd_cmd)
        print('modem %s: response=%r' % (ms.name(), response))

  elif cmd.startswith('data-attach'):
    if len(params) != 1:
      print('wrong format')
      continue
    for ms in modems:
        print('modem %s: attach' % ms.name())
        ms.attach()
        wait(ms.is_attached)
        print('modem %s: attached' % ms.name())

  elif cmd.startswith('data-detach'):
    if len(params) != 1:
      print('wrong format')
      continue
    for ms in modems:
        print('modem %s: detach' % ms.name())
        ms.attach()
        wait(lambda: not ms.is_attached())
        print('modem %s: detached' % ms.name())

  elif cmd.startswith('data-activate'):
    if len(params) != 1:
      print('wrong format')
      continue
    for ms in modems:
        print('modem %s: activate' % ms.name())
        response = ms.activate_context()
        print('modem %s: response=%r' % (ms.name(), response))

  else:
      print('Unknown command: %s' % cmd)
