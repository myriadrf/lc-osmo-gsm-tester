#!/usr/bin/env python3
from osmo_gsm_tester.testenv import *

hlr = tenv.hlr()
bts = tenv.bts()
mgw_msc = tenv.mgw()
mgw_bsc = tenv.mgw()
stp = tenv.stp()
msc = tenv.msc(hlr, mgw_msc, stp)
bsc = tenv.bsc(msc, mgw_bsc, stp)
ms_mo = tenv.modem()
ms_mt = tenv.modem()

hlr.start()
stp.start()

# Set MSC to route emergency call to ms_mt:
msc.set_emergency_call_msisdn(ms_mt.msisdn())
msc.start()

mgw_msc.start()
mgw_bsc.start()

bsc.bts_add(bts)
bsc.start()

bts.start()
wait(bsc.bts_is_connected, bts)

hlr.subscriber_add(ms_mo)
hlr.subscriber_add(ms_mt)

ms_mo.connect(msc.mcc_mnc())
ms_mt.connect(msc.mcc_mnc())

ms_mo.log_info()
ms_mt.log_info()

print('waiting for modems to attach...')
wait(ms_mo.is_registered, msc.mcc_mnc())
wait(ms_mt.is_registered, msc.mcc_mnc())
wait(msc.subscriber_attached, ms_mo, ms_mt)

assert len(ms_mo.call_id_list()) == 0 and len(ms_mt.call_id_list()) == 0
# Calling emergency number should be redirected to ms_mt as configured further above:
emerg_numbers = ms_mo.emergency_numbers()
assert len(emerg_numbers) > 0
print('dialing Emergency Number %s' % (emerg_numbers[0]))
mo_cid = ms_mo.call_dial(emerg_numbers[0])
mt_cid = ms_mt.call_wait_incoming(ms_mo)
print('dial success')

assert not ms_mo.call_is_active(mo_cid) and not ms_mt.call_is_active(mt_cid)
ms_mt.call_answer(mt_cid)
wait(ms_mo.call_is_active, mo_cid)
wait(ms_mt.call_is_active, mt_cid)
print('answer success, call established and ongoing')

sleep(5) # maintain the call active for 5 seconds

assert ms_mo.call_is_active(mo_cid) and ms_mt.call_is_active(mt_cid)
ms_mt.call_hangup(mt_cid)
wait(lambda: len(ms_mo.call_id_list()) == 0 and len(ms_mt.call_id_list()) == 0)
print('hangup success')
